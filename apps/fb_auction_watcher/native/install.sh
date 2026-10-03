#!/bin/bash
# Registers the Claude Code bridge (fbaw_claude_host.py) with Chrome on this Mac.
#
# Writes one file: ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/
# com.erjoha.fbaw.claude.json, which tells Chrome where the host script is and that only the
# FB Auction Watcher extension may start it. Run `./install.sh --uninstall` to remove it.
#
# Usage: ./install.sh [extension-id]
#   The ID is on the extension's card in chrome://extensions. Without it, the script computes
#   the ID Chrome gives an unpacked extension loaded from this repo's dist/ folder.
set -euo pipefail

NAME="com.erjoha.fbaw.claude"
HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="$HERE/fbaw_claude_host.py"
DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
MANIFEST="$DIR/$NAME.json"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$MANIFEST"
  echo "Removed $MANIFEST"
  exit 0
fi

if [[ -n "${1:-}" ]]; then
  EXT_ID="$1"
else
  # Unpacked extensions get an ID from the SHA-256 of their folder's path, hex digits mapped to a-p.
  DIST="$(cd "$HERE/../dist" && pwd)"
  EXT_ID="$(/usr/bin/python3 -c 'import hashlib,sys; h=hashlib.sha256(sys.argv[1].encode()).hexdigest()[:32]; print("".join(chr(97+int(c,16)) for c in h))' "$DIST")"
fi
if [[ ! "$EXT_ID" =~ ^[a-p]{32}$ ]]; then
  echo "That doesn't look like an extension ID: $EXT_ID" >&2
  exit 1
fi

chmod +x "$HOST"
mkdir -p "$DIR"
cat > "$MANIFEST" <<JSON
{
  "name": "$NAME",
  "description": "FB Auction Watcher: ask Claude Code (claude -p) to read what the rules can't",
  "path": "$HOST",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
JSON
echo "Wrote $MANIFEST"
echo "Allowed extension: $EXT_ID (check it matches the ID in chrome://extensions)"
