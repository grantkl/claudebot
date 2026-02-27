#!/bin/bash
# Refresh Claude OAuth credentials from macOS Keychain and restart the bot container
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER_DIR="$PROJECT_DIR/.claude-container"
HOST_CREDS="$HOME/.claude.json"

echo "$(date) - Refreshing ClaudeBot credentials and restarting..."

mkdir -p "$CONTAINER_DIR"

# Extract OAuth token from macOS Keychain
KEYCHAIN_CREDS=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)
if [ -z "$KEYCHAIN_CREDS" ]; then
    echo "$(date) - ERROR: Could not read Claude Code credentials from Keychain"
    exit 1
fi

# Write keychain credentials for the container
echo "$KEYCHAIN_CREDS" > "$CONTAINER_DIR/credentials.json"

# Copy account metadata from .claude.json
python3 -c "
import json
with open('$HOST_CREDS') as f:
    data = json.load(f)
minimal = {k: data[k] for k in ['oauthAccount'] if k in data}
with open('$CONTAINER_DIR/claude.json', 'w') as f:
    json.dump(minimal, f, indent=2)
"

# Restart the container
docker restart claudebot

echo "$(date) - ClaudeBot restarted successfully"
