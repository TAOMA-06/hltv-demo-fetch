#!/usr/bin/env bash
# Launch a dedicated Chrome profile with a CDP port for --engine cdp.
#
#   ./scripts/chrome-cdp.sh
#   # in another terminal:
#   ./scripts/fetch-hltv.sh --engine cdp --cdp-url http://127.0.0.1:9222
#
# First run: log in to hltv.org inside the opened window (if you need premium /
# older demos). The profile is separate from your daily Chrome profile because
# Chrome 136+ refuses remote debugging on the default profile.
set -euo pipefail

PORT="${CDP_PORT:-9222}"
if [ -n "${CHROME_PROFILE:-}" ]; then
  PROFILE="$CHROME_PROFILE"
elif [ "$(uname -s)" = "Darwin" ]; then
  PROFILE="$HOME/Library/Caches/hltv-demo-fetch/chrome-cdp"
else
  PROFILE="${XDG_CACHE_HOME:-$HOME/.cache}/hltv-demo-fetch/chrome-cdp"
fi
mkdir -p "$PROFILE"

find_chrome() {
  for candidate in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "$(command -v google-chrome 2>/dev/null || true)" \
    "$(command -v google-chrome-stable 2>/dev/null || true)" \
    "$(command -v chromium 2>/dev/null || true)" \
    "$(command -v chromium-browser 2>/dev/null || true)" \
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

CHROME="$(find_chrome || true)"
if [ -z "$CHROME" ]; then
  echo "找不到 Chrome/Chromium。安装 Google Chrome，或手动运行：" >&2
  echo '  <chrome> --remote-debugging-port=9222 --user-data-dir=...' >&2
  exit 1
fi

echo "Chrome CDP: http://127.0.0.1:$PORT"
echo "profile:    $PROFILE"
echo "如果第一次用，请在这个窗口里登录 hltv.org；之后保持窗口打开。"
exec "$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check
