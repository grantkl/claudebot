#!/usr/bin/env python3
"""Poll for deploy.trigger file and run deploy.sh when found."""

import json
import os
import subprocess
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
TRIGGER_FILE = os.path.join(PROJECT_DIR, "data", "deploy.trigger")
DEPLOY_SCRIPT = os.path.join(SCRIPT_DIR, "deploy.sh")
WEBHOOK_URL = "http://localhost:8081/webhook/deploy-result"
POLL_INTERVAL = 2
DEPLOY_TIMEOUT = 1800


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}", flush=True)


def _webhook_secret() -> str:
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if secret:
        return secret
    try:
        with open(os.path.join(PROJECT_DIR, ".env")) as f:
            for line in f:
                if line.startswith("WEBHOOK_SECRET="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _trigger_user_id() -> str:
    try:
        with open(TRIGGER_FILE) as f:
            return json.load(f).get("user_id", "")
    except (OSError, ValueError):
        return ""


def notify_failure(message: str) -> None:
    """DM the deploy requester. deploy.sh does this itself when it exits on
    its own; the watcher only has to cover the case where it was killed."""
    user_id = _trigger_user_id()
    secret = _webhook_secret()
    if not user_id:
        log("No user_id in trigger file, skipping notify")
        return
    if not secret:
        log("WEBHOOK_SECRET not set, skipping notify")
        return

    payload = json.dumps(
        {
            "user_id": user_id,
            "status": "failure",
            "message": message,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    ).encode()
    request = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=10)
    except OSError as exc:
        log(f"Webhook notify failed: {exc}")


def handle_trigger() -> None:
    """One poll iteration: deploy if a trigger is waiting."""
    if not os.path.exists(TRIGGER_FILE):
        return

    log("Trigger file detected, starting deploy...")
    try:
        result = subprocess.run(
            [DEPLOY_SCRIPT],
            capture_output=True,
            text=True,
            timeout=DEPLOY_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        log(f"Deploy timed out after {exc.timeout}s, killing")
        # Whatever the deploy managed to print before the kill is the only
        # clue about where it hung.
        if exc.stdout:
            log(f"partial stdout: {exc.stdout}")
        if exc.stderr:
            log(f"partial stderr: {exc.stderr}")
        notify_failure(f"Deploy timed out after {exc.timeout}s and was killed.")
        if os.path.exists(TRIGGER_FILE):
            os.remove(TRIGGER_FILE)
        return

    if result.stdout:
        log(result.stdout.strip())
    if result.stderr:
        log(f"stderr: {result.stderr.strip()}")
    if result.returncode == 0:
        log("Deploy finished successfully")
    else:
        log(f"Deploy failed with exit code {result.returncode}")
        # deploy.sh cleans up the trigger on success, but on
        # crash the file may be left behind.  Remove it here
        # to avoid retrying a broken deploy every 2 seconds.
        if os.path.exists(TRIGGER_FILE):
            os.remove(TRIGGER_FILE)
            log("Removed stale trigger file after failed deploy")


def main() -> None:
    log("Deploy watcher started")
    log(f"Watching: {TRIGGER_FILE}")

    while True:
        handle_trigger()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
