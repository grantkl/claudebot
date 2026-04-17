#!/bin/bash
# Pull latest code and rebuild the bot container, writing result to a JSON file
set -e

# Ensure Docker Desktop binaries are on PATH (script may run from a file
# watcher that doesn't inherit the user's shell profile).
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESULT_FILE="$PROJECT_DIR/data/deploy.result"
TRIGGER_FILE="$PROJECT_DIR/data/deploy.trigger"

cleanup() {
    rm -f "$TRIGGER_FILE"
}

write_result() {
    local status="$1"
    local message="$2"
    local timestamp
    timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    python3 -c "
import json, sys
with open('$RESULT_FILE', 'w') as f:
    json.dump({'status': sys.argv[1], 'message': sys.argv[2], 'timestamp': sys.argv[3]}, f, indent=2)
" "$status" "$message" "$timestamp"
}

echo "$(date) - Starting deploy..."

# Pull latest changes
cd "$PROJECT_DIR"
GIT_OUTPUT=$(git pull --ff-only 2>&1) || {
    echo "$(date) - git pull failed: $GIT_OUTPUT"
    write_result "failure" "git pull failed: $GIT_OUTPUT"
    cleanup
    exit 1
}
echo "$(date) - git pull: $GIT_OUTPUT"

# Rebuild and restart containers
DOCKER_OUTPUT=$(docker compose up -d --build 2>&1) || {
    echo "$(date) - docker compose failed: $DOCKER_OUTPUT"
    write_result "failure" "docker compose failed: $DOCKER_OUTPUT"
    cleanup
    exit 1
}
echo "$(date) - docker compose: done"

write_result "success" "$GIT_OUTPUT"
cleanup

echo "$(date) - Deploy completed successfully"
