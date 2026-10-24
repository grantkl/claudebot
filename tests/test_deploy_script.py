"""Tests for the deploy script's docker compose invocation.

scripts/deploy.sh is bash, so these tests assert on the script's text: they
locate the `docker compose up` command and inspect its arguments.
"""

import re
import shlex
from pathlib import Path

import pytest

DEPLOY_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy.sh"


def _compose_up_args() -> list[str]:
    """Return the argument tokens of the `docker compose up` command in deploy.sh.

    Joins backslash line continuations so a command spanning multiple lines is
    parsed as one, then strips shell redirections from the token list.
    """
    text = DEPLOY_SCRIPT.read_text()
    joined = re.sub(r"\\\n\s*", " ", text)
    code = "\n".join(
        line for line in joined.splitlines() if not line.lstrip().startswith("#")
    )

    matches = re.findall(r"docker\s+compose\s+up\b[^\n)|]*", code)
    assert len(matches) == 1, (
        f"expected exactly one `docker compose up` command in deploy.sh, got {matches}"
    )

    tokens = shlex.split(matches[0])
    return [t for t in tokens if not re.match(r"^\d*[<>]", t)]


class TestDeployComposeCommand:
    def test_script_exists(self) -> None:
        assert DEPLOY_SCRIPT.is_file(), f"missing deploy script at {DEPLOY_SCRIPT}"

    def test_uses_no_deps(self) -> None:
        """--no-deps keeps dependency services (e.g. memory) from being rebuilt.

        Rebuilding `memory` redownloads PyTorch and blows the deploy watcher's
        timeout, so the bot deploy must not touch dependency services.
        """
        assert "--no-deps" in _compose_up_args()

    def test_builds_detached(self) -> None:
        args = _compose_up_args()
        assert args[:3] == ["docker", "compose", "up"]
        assert "-d" in args
        assert "--build" in args

    def test_targets_claudebot_service(self) -> None:
        """The service name is the only non-flag argument after `up`."""
        args = _compose_up_args()
        services = [t for t in args[3:] if not t.startswith("-")]
        assert services == ["claudebot"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
