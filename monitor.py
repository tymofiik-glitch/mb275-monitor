#!/usr/bin/env python3
"""
MB275 floorplan availability monitor.

Periodically opens the MB275 online-leasing floorplans page in a real
(headless) browser -- needed because the site sits behind Cloudflare's
bot-check and a plain HTTP request gets blocked -- and sends a Telegram
message the moment a studio/apartment becomes available (or the listing
changes while already available).

Config comes from environment variables when present (used in CI, e.g.
GitHub Actions secrets), otherwise falls back to config.json (used for a
local Mac run via launchd).

Usage:
    python3 monitor.py            # runs forever, checking every N seconds
    python3 monitor.py --once     # runs a single check and exits (for testing)
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "monitor.log"

# Text that is present on the page whenever NO floorplans are available.
UNAVAILABLE_MARKER = "floor plan details not available for this property"
# Strings that should always be present when the real page (not a Cloudflare
# challenge / error page) has loaded successfully.
PAGE_LOADED_MARKERS = ["floor plans", "mb275"]
# Extra positive signal that a unit can be booked.
BOOK_MARKERS = ["book now", "apply now"]

MAX_LOG_BYTES = 5 * 1024 * 1024  # rotate/trim the log once it gets this big
FAILURE_ALERT_THRESHOLD = 6  # consecutive failed checks before pinging about it


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            LOG_PATH.write_text(LOG_PATH.read_text(errors="ignore")[-1_000_000:])
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    env_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if env_token and env_chat_id:
        return {
            "telegram_bot_token": env_token,
            "telegram_chat_id": env_chat_id,
            "url": os.environ.get(
                "MB275_URL",
                "https://mb275.securerc.co.uk/onlineleasing/mb275/floorplans.aspx",
            ),
            "check_interval_seconds": int(os.environ.get("CHECK_INTERVAL_SECONDS", "300")),
            "headless": os.environ.get("HEADLESS", "true").lower() != "false",
            "channel": os.environ.get("BROWSER_CHANNEL", "") or None,
        }

    if not CONFIG_PATH.exists():
        log(f"Missing config file: {CONFIG_PATH}. Copy config.example.json to config.json and fill it in.")
        sys.exit(1)
    with CONFIG_PATH.open() as f:
        cfg = json.load(f)
    cfg.setdefault("url", "https://mb275.securerc.co.uk/onlineleasing/mb275/floorplans.aspx")
    cfg.setdefault("check_interval_seconds", 300)
    cfg.setdefault("headless", True)
    cfg.setdefault("channel", "chrome")  # use the real installed Chrome if possible
    if not cfg.get("telegram_bot_token") or not cfg.get("telegram_chat_id"):
        log("config.json is missing telegram_bot_token / telegram_chat_id.")
        sys.exit(1)
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "available": False,
        "last_hash": None,
        "last_notified_hash": None,
        "consecutive_failures": 0,
        "alerted_about_failures": False,
    }


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except OSError as e:
        log(f"Could not save state: {e}")


def send_telegram(cfg: dict, text: str) -> None:
    token = cfg["telegram_bot_token"]
    chat_id = cfg["telegram_chat_id"]
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(2):
        try:
            resp = requests.post(
                api_url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": False,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                return
            log(f"Telegram API returned {resp.status_code}: {resp.text[:300]}")
        except requests.RequestException as e:
            log(f"Telegram send failed (attempt {attempt + 1}): {e}")
        time.sleep(2)


def fetch_page(cfg: dict):
    """Returns (success: bool, text: str, book_links: list[str])."""
    with sync_playwright() as p:
        browser = None
        launch_kwargs = dict(
            headless=cfg["headless"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        channel = cfg.get("channel")
        if channel:
            try:
                browser = p.chromium.launch(channel=channel, **launch_kwargs)
            except Exception as e:
                log(f"Could not launch channel={channel} ({e}); falling back to bundled Chromium.")
        if browser is None:
            browser = p.chromium.launch(**launch_kwargs)

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
        try:
            page.goto(cfg["url"], wait_until="networkidle", timeout=45_000)
        except PlaywrightTimeoutError:
            # Fall back to whatever loaded, some Cloudflare interstitials never
            # go fully "networkidle".
            pass

        # Give Cloudflare's JS challenge (if any) a moment to resolve, and any
        # client-rendered availability widget a moment to paint.
        page.wait_for_timeout(4000)

        try:
            text = page.inner_text("body")
        except Exception:
            text = page.content()

        book_links = []
        try:
            for marker in BOOK_MARKERS:
                locator = page.get_by_text(re.compile(marker, re.IGNORECASE))
                count = min(locator.count(), 10)
                for i in range(count):
                    el = locator.nth(i)
                    href = el.get_attribute("href")
                    if not href:
                        # the text might be inside a button nested in a link
                        href = el.locator("xpath=ancestor-or-self::a[1]").get_attribute("href")
                    if href:
                        book_links.append(href)
        except Exception as e:
            log(f"Could not extract book-now links: {e}")

        context.close()
        browser.close()

        lower = text.lower()
        loaded_ok = any(m in lower for m in PAGE_LOADED_MARKERS)
        return loaded_ok, text, list(dict.fromkeys(book_links))


def run_check(cfg: dict, state: dict) -> None:
    loaded_ok, text, book_links = fetch_page(cfg)

    if not loaded_ok:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        log(f"Page did not load as expected (failure #{state['consecutive_failures']}).")
        if (
            state["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD
            and not state.get("alerted_about_failures")
        ):
            send_telegram(
                cfg,
                "MB275 monitor: the last several checks failed to load the real page "
                "(possibly blocked/challenged). You may want to check it manually:\n"
                f"{cfg['url']}",
            )
            state["alerted_about_failures"] = True
        return

    state["consecutive_failures"] = 0
    state["alerted_about_failures"] = False

    lower = text.lower()
    available_now = UNAVAILABLE_MARKER not in lower
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    was_available = state.get("available", False)
    already_notified_this_hash = state.get("last_notified_hash") == content_hash

    if available_now and not already_notified_this_hash and (not was_available or content_hash != state.get("last_hash")):
        snippet = text.strip()
        snippet = re.sub(r"\n{3,}", "\n\n", snippet)
        if len(snippet) > 600:
            snippet = snippet[:600] + "…"

        message_lines = [
            "🏠 MB275: possible availability change!",
            cfg["url"],
            "",
            snippet,
        ]
        if book_links:
            message_lines += ["", "Book now link(s):"] + book_links
        send_telegram(cfg, "\n".join(message_lines))
        state["last_notified_hash"] = content_hash
        log("Sent availability notification.")
    elif not available_now:
        log("No availability.")
    else:
        log("Available, but already notified about this exact content.")

    state["available"] = available_now
    state["last_hash"] = content_hash


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    args = parser.parse_args()

    cfg = load_config()
    state = load_state()

    if args.once:
        run_check(cfg, state)
        save_state(state)
        return

    log(f"Starting monitor loop, checking every {cfg['check_interval_seconds']}s.")
    while True:
        start = time.time()
        try:
            run_check(cfg, state)
        except Exception:
            log("Unhandled exception during check:\n" + traceback.format_exc())
        save_state(state)
        elapsed = time.time() - start
        sleep_for = max(5, cfg["check_interval_seconds"] - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
