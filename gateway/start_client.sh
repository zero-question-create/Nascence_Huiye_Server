#!/bin/bash
# Open the Nascence standalone dialogue client (no server required)
# The client auto-connects to the last-used server port (localStorage memory)
CLIENT_FILE="$(cd "$(dirname "$0")/.." && pwd)/client/index.html"
echo "Opening standalone dialogue client: $CLIENT_FILE"
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "file://$CLIENT_FILE" >/dev/null 2>&1
elif [ "$(uname)" = "Darwin" ] && command -v open >/dev/null 2>&1; then
  open "file://$CLIENT_FILE" >/dev/null 2>&1
else
  echo "[Tip] No GUI detected. Open manually: file://$CLIENT_FILE"
fi
