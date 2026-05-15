"""Tests for the nginx auth-proxy config template.

Validates proxy/default.conf.template as plain text. The proxy must
re-resolve api.anthropic.com via Docker's embedded DNS resolver rather
than caching a single IP at startup, so proxy_pass must use a variable
upstream and a `resolver` directive must be present.
"""

import re
from pathlib import Path

import pytest

TEMPLATE_PATH = Path(__file__).parent.parent / "proxy" / "default.conf.template"


@pytest.fixture(scope="module")
def template_text() -> str:
    """Return the raw contents of the nginx config template."""
    return TEMPLATE_PATH.read_text()


def test_template_file_exists() -> None:
    """The proxy config template file is present at the expected path."""
    assert TEMPLATE_PATH.is_file(), f"missing template: {TEMPLATE_PATH}"


class TestResolverDirective:
    def test_resolver_uses_docker_embedded_dns(self, template_text: str) -> None:
        """A resolver directive points at Docker's embedded DNS (127.0.0.11)."""
        assert "resolver 127.0.0.11" in template_text

    def test_resolver_has_valid_ttl(self, template_text: str) -> None:
        """The resolver directive sets a valid= TTL so DNS is re-resolved."""
        match = re.search(
            r"resolver\s+127\.0\.0\.11[^;]*\bvalid=\S+", template_text
        )
        assert match is not None, "resolver directive missing valid= TTL"

    def test_resolver_disables_ipv6(self, template_text: str) -> None:
        """The resolver directive disables IPv6 lookups (ipv6=off)."""
        match = re.search(
            r"resolver\s+127\.0\.0\.11[^;]*\bipv6=off", template_text
        )
        assert match is not None, "resolver directive missing ipv6=off"


class TestVariableUpstream:
    def test_sets_upstream_variable(self, template_text: str) -> None:
        """An nginx variable holds the upstream hostname."""
        assert 'set $upstream "api.anthropic.com";' in template_text

    def test_proxy_pass_uses_variable(self, template_text: str) -> None:
        """proxy_pass references the $upstream variable, forcing re-resolution."""
        assert "proxy_pass https://$upstream;" in template_text

    def test_proxy_pass_not_literal_hostname(self, template_text: str) -> None:
        """proxy_pass must NOT use a literal hostname (it caches one IP)."""
        assert "proxy_pass https://api.anthropic.com;" not in template_text


class TestPreservedDirectives:
    @pytest.mark.parametrize(
        "directive",
        [
            "proxy_ssl_server_name on",
            "proxy_set_header Host api.anthropic.com",
            'Authorization "Bearer ${CLAUDE_CODE_OAUTH_TOKEN}"',
            "proxy_buffering off",
            "proxy_read_timeout 300s",
            "proxy_connect_timeout 10s",
        ],
    )
    def test_directive_preserved(self, template_text: str, directive: str) -> None:
        """Existing proxy behavior directives are still present."""
        assert directive in template_text
