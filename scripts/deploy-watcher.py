#!/usr/bin/env python3
"""Poll for deploy.trigger file and run deploy.sh when found."""

import os
import subprocess
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
TRIGGER_FILE = os.path.join(PROJECT_DIR, "data", "deploy.trigger")
DEPLOY_SCRIPT = os.path.join(SCRIPT_DIR, "deploy.sh")
POLL_INTERVAL = 2


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}", flush=True)


def main() -> None:
    log("Deploy watcher started")
    log(f"Watching: {TRIGGER_FILE}")

    while True:
        if os.path.exists(TRIGGER_FILE):
            log("Trigger file detected, starting deploy...")
            result = subprocess.run(
                [DEPLOY_SCRIPT],
                capture_output=True,
                text=True,
            )
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
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
