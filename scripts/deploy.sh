#!/bin/bash
# Pull latest code and rebuild the bot container, writing result to a JSON file
set -e

# Ensure Docker Desktop binaries are on PATH (script may run from a file
# watcher that doesn't inherit the user's shell profile).
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESULT_FILE="$PROJECT_DIR/data/deploy.result"
TRIGGER_FILE="$PROJECT_DIR/data/deploy.trigger"
WEBHOOK_URL="http://localhost:8081/webhook/deploy-result"

notify_webhook() {
    local status="$1"
    local message="$2"
    local timestamp
    timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    if [ ! -f "$TRIGGER_FILE" ]; then
        echo "$(date) - no trigger file, skipping notify"
        return
    fi

    local user_id
    user_id="$(python3 -c "import json,sys
try:
    d = json.load(open('$TRIGGER_FILE'))
    print(d.get('user_id', ''))
except Exception:
    print('')" 2>/dev/null)" || user_id=""

    if [ -z "$user_id" ]; then
        echo "$(date) - no user_id in trigger, skipping notify"
        return
    fi

    if [ -z "${WEBHOOK_SECRET:-}" ] && [ -f "$PROJECT_DIR/.env" ]; then
        WEBHOOK_SECRET="$(grep -E '^WEBHOOK_SECRET=' "$PROJECT_DIR/.env" | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
    fi

    if [ -z "${WEBHOOK_SECRET:-}" ]; then
        echo "$(date) - WEBHOOK_SECRET not set, skipping notify"
        return
    fi

    local payload
    payload="$(python3 -c "import json,sys
print(json.dumps({
    'user_id': sys.argv[1],
    'status': sys.argv[2],
    'message': sys.argv[3],
    'timestamp': sys.argv[4],
}))" "$user_id" "$status" "$message" "$timestamp")"

    local curl_exit=0
    curl -sS --max-time 10 -X POST \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $WEBHOOK_SECRET" \
        -d "$payload" \
        "$WEBHOOK_URL" >/dev/null || curl_exit=$?
    echo "$(date) - webhook notify exit=$curl_exit"
}

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
    notify_webhook "failure" "git pull failed: $GIT_OUTPUT"
    cleanup
    exit 1
}
echo "$(date) - git pull: $GIT_OUTPUT"

# Rebuild only the bot container — other services (auth-proxy, memory)
# don't change with bot code and rebuilding them adds several minutes
# (memory redownloads PyTorch) which was causing deploy trigger timeouts.
DOCKER_OUTPUT=$(docker compose up -d --build claudebot 2>&1) || {
    echo "$(date) - docker compose failed: $DOCKER_OUTPUT"
    write_result "failure" "docker compose failed: $DOCKER_OUTPUT"
    notify_webhook "failure" "docker compose failed: $DOCKER_OUTPUT"
    cleanup
    exit 1
}
echo "$(date) - docker compose: done"

write_result "success" "$GIT_OUTPUT"
notify_webhook "success" "$GIT_OUTPUT"
cleanup

echo "$(date) - Deploy completed successfully"
