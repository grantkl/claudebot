"""Tests for scripts/deploy-watcher.py timeout handling.

These tests define the expected structure of the watcher script:

* ``handle_trigger()`` — the body of the poll loop, extracted into a callable
  that performs exactly one iteration: if the trigger file exists, run the
  deploy script and handle its outcome. ``main()`` is expected to call it in a
  loop with a sleep between iterations. Nothing here calls ``main()``.
* ``DEPLOY_TIMEOUT`` — module-level constant holding the subprocess timeout in
  seconds (1800), passed to ``subprocess.run(..., timeout=DEPLOY_TIMEOUT)``.
* ``log(msg)`` — module-level logging helper. Tests patch it to capture output,
  so timeout diagnostics must go through it.
* The timeout notification must be sent with ``urllib.request.urlopen`` (so the
  module does ``import urllib.request`` rather than
  ``from urllib.request import urlopen``) and must pass a
  ``urllib.request.Request`` object, since the Authorization header can only be
  set on a Request.

The script's filename contains a hyphen, so it is loaded via importlib rather
than a normal import.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WATCHER_PATH = PROJECT_ROOT / "scripts" / "deploy-watcher.py"

WEBHOOK_URL = "http://localhost:8081/webhook/deploy-result"


def _load_watcher() -> types.ModuleType:
    """Import scripts/deploy-watcher.py under a synthetic module name."""
    spec = importlib.util.spec_from_file_location("deploy_watcher", WATCHER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy_watcher"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def watcher() -> types.ModuleType:
    return _load_watcher()


@pytest.fixture
def trigger_file(tmp_path: Path) -> Path:
    """A trigger file containing the deploy requester's Slack user id."""
    path = tmp_path / "deploy.trigger"
    path.write_text(json.dumps({"user_id": "U123ABC", "reason": "merge PR #99"}))
    return path


def _timeout_exc(
    stdout: str | None = "partial stdout",
    stderr: str | None = "partial stderr",
) -> subprocess.TimeoutExpired:
    exc = subprocess.TimeoutExpired(cmd=["deploy.sh"], timeout=1800)
    exc.stdout = stdout
    exc.stderr = stderr
    return exc


def _logged_text(mock_log: MagicMock) -> str:
    return "\n".join(str(call.args[0]) for call in mock_log.call_args_list)


def _request_from(mock_urlopen: MagicMock) -> Any:
    """The urllib Request passed to the first urlopen call."""
    assert mock_urlopen.call_count >= 1, "expected a webhook notification POST"
    return mock_urlopen.call_args_list[0].args[0]


def _payload_from(mock_urlopen: MagicMock) -> dict[str, Any]:
    req = _request_from(mock_urlopen)
    data = req.data
    if isinstance(data, bytes):
        data = data.decode()
    return json.loads(data)


# ---------------------------------------------------------------------------
# Timeout value
# ---------------------------------------------------------------------------
class TestDeployTimeout:
    def test_module_constant_is_1800(self, watcher: types.ModuleType) -> None:
        """The deploy timeout is 30 minutes, exposed as a module constant."""
        assert watcher.DEPLOY_TIMEOUT == 1800

    def test_subprocess_run_receives_1800(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        """subprocess.run is called with the 1800s timeout."""
        completed = subprocess.CompletedProcess(
            args=["deploy.sh"], returncode=0, stdout="ok", stderr=""
        )
        with (
            patch.object(watcher, "TRIGGER_FILE", str(trigger_file)),
            patch.object(watcher, "log"),
            patch.object(watcher.subprocess, "run", return_value=completed) as mock_run,
        ):
            watcher.handle_trigger()

        assert mock_run.call_count == 1
        assert mock_run.call_args.kwargs["timeout"] == 1800


# ---------------------------------------------------------------------------
# Timeout handling: logging, notification, cleanup
# ---------------------------------------------------------------------------
@patch.dict("os.environ", {"WEBHOOK_SECRET": "test-secret"}, clear=True)
class TestTimeoutHandling:
    def _run_timeout(
        self,
        watcher: types.ModuleType,
        trigger_file: Path,
        exc: subprocess.TimeoutExpired | None = None,
    ) -> tuple[MagicMock, MagicMock]:
        """Drive one handle_trigger() iteration where the deploy times out."""
        with (
            patch.object(watcher, "TRIGGER_FILE", str(trigger_file)),
            patch.object(watcher, "PROJECT_DIR", str(trigger_file.parent)),
            patch.object(watcher, "log") as mock_log,
            patch.object(
                watcher.subprocess, "run", side_effect=exc or _timeout_exc()
            ),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            watcher.handle_trigger()
        return mock_log, mock_urlopen

    def test_logs_partial_stdout_and_stderr(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        """The output captured before the kill shows where the deploy hung."""
        exc = _timeout_exc(
            stdout="git pull: Already up to date.",
            stderr="=> [builder 3/9] RUN pip install torch",
        )
        mock_log, _ = self._run_timeout(watcher, trigger_file, exc)

        logged = _logged_text(mock_log)
        assert "git pull: Already up to date." in logged
        assert "=> [builder 3/9] RUN pip install torch" in logged

    def test_tolerates_missing_partial_output(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        """exc.stdout/exc.stderr may be None; that must not raise."""
        mock_log, mock_urlopen = self._run_timeout(
            watcher, trigger_file, _timeout_exc(stdout=None, stderr=None)
        )

        assert mock_log.call_count >= 1
        assert mock_urlopen.call_count == 1

    def test_posts_failure_notification(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        """A failure notification is POSTed to the deploy-result webhook."""
        _, mock_urlopen = self._run_timeout(watcher, trigger_file)

        req = _request_from(mock_urlopen)
        assert req.full_url == WEBHOOK_URL
        assert req.get_method() == "POST"

    def test_notification_payload_fields(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        """Payload carries the requester's user_id, failure status, and a
        message naming the timeout."""
        _, mock_urlopen = self._run_timeout(watcher, trigger_file)

        payload = _payload_from(mock_urlopen)
        assert payload["user_id"] == "U123ABC"
        assert payload["status"] == "failure"
        message = payload["message"].lower()
        assert "timed out" in message or "timeout" in message

    def test_notification_authorization_header(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        """The request carries the shared webhook secret as a Bearer token."""
        _, mock_urlopen = self._run_timeout(watcher, trigger_file)

        req = _request_from(mock_urlopen)
        assert req.get_header("Authorization") == "Bearer test-secret"

    def test_removes_trigger_file(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        """The trigger is cleared so the watcher doesn't loop on a hung deploy."""
        assert trigger_file.exists()
        self._run_timeout(watcher, trigger_file)
        assert not trigger_file.exists()

    def test_notification_sent_before_trigger_removed(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        """user_id comes from the trigger file, so the read must happen while
        the file still exists."""
        seen: dict[str, bool] = {}

        def _record(*args: Any, **kwargs: Any) -> MagicMock:
            seen["trigger_existed"] = trigger_file.exists()
            return MagicMock()

        with (
            patch.object(watcher, "TRIGGER_FILE", str(trigger_file)),
            patch.object(watcher, "PROJECT_DIR", str(trigger_file.parent)),
            patch.object(watcher, "log"),
            patch.object(watcher.subprocess, "run", side_effect=_timeout_exc()),
            patch("urllib.request.urlopen", side_effect=_record) as mock_urlopen,
        ):
            watcher.handle_trigger()

        assert mock_urlopen.call_count == 1
        assert seen.get("trigger_existed") is True

    def test_urlopen_failure_does_not_propagate(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        """A dead webhook endpoint must not crash the watcher loop, and the
        trigger must still be cleaned up."""
        with (
            patch.object(watcher, "TRIGGER_FILE", str(trigger_file)),
            patch.object(watcher, "PROJECT_DIR", str(trigger_file.parent)),
            patch.object(watcher, "log"),
            patch.object(watcher.subprocess, "run", side_effect=_timeout_exc()),
            patch("urllib.request.urlopen", side_effect=OSError("connection refused")),
        ):
            watcher.handle_trigger()

        assert not trigger_file.exists()


# ---------------------------------------------------------------------------
# Webhook secret resolution
# ---------------------------------------------------------------------------
class TestWebhookSecretResolution:
    def _timeout_with_env(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> MagicMock:
        with (
            patch.object(watcher, "TRIGGER_FILE", str(trigger_file)),
            patch.object(watcher, "PROJECT_DIR", str(trigger_file.parent)),
            patch.object(watcher, "log"),
            patch.object(watcher.subprocess, "run", side_effect=_timeout_exc()),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            watcher.handle_trigger()
        return mock_urlopen

    @patch.dict("os.environ", {"WEBHOOK_SECRET": "from-env"}, clear=True)
    def test_prefers_environment_variable(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        (trigger_file.parent / ".env").write_text("WEBHOOK_SECRET=from-dotenv\n")

        mock_urlopen = self._timeout_with_env(watcher, trigger_file)

        req = _request_from(mock_urlopen)
        assert req.get_header("Authorization") == "Bearer from-env"

    @patch.dict("os.environ", {}, clear=True)
    def test_falls_back_to_project_dotenv(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        """With no env var, the secret is parsed out of the project .env."""
        (trigger_file.parent / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-123\nWEBHOOK_SECRET=from-dotenv\nOTHER=x\n"
        )

        mock_urlopen = self._timeout_with_env(watcher, trigger_file)

        req = _request_from(mock_urlopen)
        assert req.get_header("Authorization") == "Bearer from-dotenv"

    @patch.dict("os.environ", {}, clear=True)
    def test_strips_quotes_from_dotenv_value(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        (trigger_file.parent / ".env").write_text('WEBHOOK_SECRET="quoted-secret"\n')

        mock_urlopen = self._timeout_with_env(watcher, trigger_file)

        req = _request_from(mock_urlopen)
        assert req.get_header("Authorization") == "Bearer quoted-secret"


# ---------------------------------------------------------------------------
# Non-timeout paths stay as they are
# ---------------------------------------------------------------------------
@patch.dict("os.environ", {"WEBHOOK_SECRET": "test-secret"}, clear=True)
class TestNonTimeoutPaths:
    def test_no_trigger_file_is_a_noop(
        self, watcher: types.ModuleType, tmp_path: Path
    ) -> None:
        missing = tmp_path / "deploy.trigger"
        with (
            patch.object(watcher, "TRIGGER_FILE", str(missing)),
            patch.object(watcher, "log"),
            patch.object(watcher.subprocess, "run") as mock_run,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            watcher.handle_trigger()

        mock_run.assert_not_called()
        mock_urlopen.assert_not_called()

    def test_success_does_not_notify(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        """deploy.sh sends its own webhook on success; the watcher must not
        double-notify."""
        completed = subprocess.CompletedProcess(
            args=["deploy.sh"], returncode=0, stdout="Already up to date.", stderr=""
        )
        with (
            patch.object(watcher, "TRIGGER_FILE", str(trigger_file)),
            patch.object(watcher, "PROJECT_DIR", str(trigger_file.parent)),
            patch.object(watcher, "log"),
            patch.object(watcher.subprocess, "run", return_value=completed),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            watcher.handle_trigger()

        mock_urlopen.assert_not_called()

    def test_nonzero_exit_removes_stale_trigger(
        self, watcher: types.ModuleType, trigger_file: Path
    ) -> None:
        """Existing behavior: a crashed deploy.sh leaves the trigger behind and
        the watcher clears it so it doesn't retry every poll."""
        completed = subprocess.CompletedProcess(
            args=["deploy.sh"], returncode=1, stdout="", stderr="boom"
        )
        with (
            patch.object(watcher, "TRIGGER_FILE", str(trigger_file)),
            patch.object(watcher, "PROJECT_DIR", str(trigger_file.parent)),
            patch.object(watcher, "log"),
            patch.object(watcher.subprocess, "run", return_value=completed),
            patch("urllib.request.urlopen"),
        ):
            watcher.handle_trigger()

        assert not trigger_file.exists()
