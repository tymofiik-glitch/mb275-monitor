#!/bin/bash
# Sets up and starts the MB275 availability monitor as a background service
# (launchd) on this Mac. Run this from a normal Terminal window on your
# actual Mac (not through Claude) -- macOS background services can only be
# registered by a real Terminal session.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.tymofii.mb275monitor"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"

echo "Project directory: $PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install it first, e.g.:"
  echo "  brew install python3"
  echo "(install Homebrew from https://brew.sh if you don't have it)"
  exit 1
fi

if [ ! -f "$PROJECT_DIR/config.json" ]; then
  cp "$PROJECT_DIR/config.example.json" "$PROJECT_DIR/config.json"
  echo
  echo "Created config.json from the example."
  echo "Open $PROJECT_DIR/config.json and fill in:"
  echo "  - telegram_bot_token (from @BotFather)"
  echo "  - telegram_chat_id   (your numeric Telegram user/chat id)"
  echo "Then run this script again."
  exit 1
fi

if grep -q "PUT_YOUR_BOT_TOKEN_HERE\|PUT_YOUR_CHAT_ID_HERE" "$PROJECT_DIR/config.json"; then
  echo "config.json still has placeholder values. Fill in telegram_bot_token"
  echo "and telegram_chat_id, then run this script again."
  exit 1
fi

echo "Setting up Python virtual environment..."
cd "$PROJECT_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

echo "Installing Playwright's bundled Chromium (fallback browser)..."
python3 -m playwright install chromium

PYTHON_BIN="$PROJECT_DIR/.venv/bin/python3"

echo "Running one test check (this opens a browser in the background, may take ~10-20s)..."
"$PYTHON_BIN" "$PROJECT_DIR/monitor.py" --once
echo "Test check finished -- see $PROJECT_DIR/monitor.log for details."
echo "If it logged an error about Telegram, double-check your bot token/chat id."

echo "Installing the background service (checks every 5 minutes, runs at login)..."
sed -e "s#__PYTHON_BIN__#${PYTHON_BIN}#g" -e "s#__PROJECT_DIR__#${PROJECT_DIR}#g" \
  "$PROJECT_DIR/com.tymofii.mb275monitor.plist.template" > "$PLIST_DST"

launchctl bootout "gui/$(id -u)" "$PLIST_DST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/${LABEL}"

echo
echo "Done. The monitor now runs every 5 minutes in the background and will"
echo "restart automatically after you log in / reboot."
echo "Logs: $PROJECT_DIR/monitor.log"
echo "To stop it later, run: bash \"$PROJECT_DIR/uninstall.sh\""
