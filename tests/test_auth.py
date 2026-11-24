"""Tests for login.gov authentication module."""

import os
from unittest.mock import patch

import pytest

from usa_spending_mcp_server.auth import (
    LOGINGOV_SANDBOX_CONFIG_URL,
    LoginGovTokenVerifier,
    create_logingov_auth,
)


class TestLoginGovTokenVerifier:
    """Tests for LoginGovTokenVerifier."""

    def test_init(self):
        """Test verifier initialization."""
        verifier = LoginGovTokenVerifier(
            userinfo_url="https://example.com/userinfo",
            timeout_seconds=5,
        )
        assert verifier.userinfo_url == "https://example.com/userinfo"
        assert verifier.timeout_seconds == 5

    def _mock_http_client(self, response=None, exc=None):
        """Build a mock httpx.AsyncClient context manager."""
        from unittest.mock import AsyncMock

        mock_client = AsyncMock()
        if exc is not None:
            mock_client.get.side_effect = exc
        else:
            mock_client.get.return_value = response
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_client
        return mock_cm, mock_client

    @pytest.mark.asyncio
    async def test_verify_valid_token_returns_access_token(self):
        """Valid userinfo response yields an AccessToken keyed by sub."""
        import httpx

        from usa_spending_mcp_server import auth as auth_module

        response = httpx.Response(200, json={"sub": "user-123", "email": "u@example.com"})
        mock_cm, mock_client = self._mock_http_client(response=response)
        verifier = LoginGovTokenVerifier(userinfo_url="https://example.com/userinfo")
        with patch.object(auth_module.httpx, "AsyncClient", return_value=mock_cm):
            token = await verifier.verify_token("bearer-token-abc")
        assert token is not None
        assert token.client_id == "user-123"
        assert token.token == "bearer-token-abc"
        assert "openid" in token.scopes
        _, kwargs = mock_client.get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer bearer-token-abc"

    @pytest.mark.asyncio
    async def test_verify_rejected_token_returns_none(self):
        """Non-200 userinfo response means the token is invalid."""
        import httpx

        from usa_spending_mcp_server import auth as auth_module

        response = httpx.Response(401, json={"error": "invalid_token"})
        mock_cm, _ = self._mock_http_client(response=response)
        verifier = LoginGovTokenVerifier(userinfo_url="https://example.com/userinfo")
        with patch.object(auth_module.httpx, "AsyncClient", return_value=mock_cm):
            assert await verifier.verify_token("bad-token") is None

    @pytest.mark.asyncio
    async def test_verify_missing_sub_returns_none(self):
        """Userinfo without a sub claim cannot identify a principal."""
        import httpx

        from usa_spending_mcp_server import auth as auth_module

        response = httpx.Response(200, json={"email": "u@example.com"})
        mock_cm, _ = self._mock_http_client(response=response)
        verifier = LoginGovTokenVerifier(userinfo_url="https://example.com/userinfo")
        with patch.object(auth_module.httpx, "AsyncClient", return_value=mock_cm):
            assert await verifier.verify_token("token-without-sub") is None

    @pytest.mark.asyncio
    async def test_verify_timeout_returns_none(self):
        """Userinfo timeouts fail closed (None, not an exception)."""
        import httpx

        from usa_spending_mcp_server import auth as auth_module

        mock_cm, _ = self._mock_http_client(exc=httpx.TimeoutException("slow"))
        verifier = LoginGovTokenVerifier(userinfo_url="https://example.com/userinfo")
        with patch.object(auth_module.httpx, "AsyncClient", return_value=mock_cm):
            assert await verifier.verify_token("any-token") is None


class TestLoginGovNonce:
    """Tests for the login.gov nonce injection."""

    def test_short_txn_id_gets_random_nonce(self):
        """Transactions shorter than 22 chars get a generated nonce."""
        import fastmcp.server.auth.oidc_proxy as oidc_proxy_module

        from usa_spending_mcp_server.auth import LoginGovOIDCProxy

        proxy = LoginGovOIDCProxy.__new__(LoginGovOIDCProxy)
        txn: dict = {}
        with patch.object(
            oidc_proxy_module.OIDCProxy,
            "_build_upstream_authorize_url",
            return_value="https://idp.example.com/authorize?client_id=x",
        ):
            url = LoginGovOIDCProxy._build_upstream_authorize_url(proxy, "short", txn)
        assert "nonce=" in url
        assert len(txn["nonce"]) >= 22
        assert f"nonce={txn['nonce']}" in url

    def test_long_txn_id_reused_as_nonce(self):
        """Transactions of 22+ chars are reused as the nonce (stable)."""
        import fastmcp.server.auth.oidc_proxy as oidc_proxy_module

        from usa_spending_mcp_server.auth import LoginGovOIDCProxy

        proxy = LoginGovOIDCProxy.__new__(LoginGovOIDCProxy)
        txn: dict = {}
        txn_id = "a" * 24
        with patch.object(
            oidc_proxy_module.OIDCProxy,
            "_build_upstream_authorize_url",
            return_value="https://idp.example.com/authorize?client_id=x",
        ):
            url = LoginGovOIDCProxy._build_upstream_authorize_url(proxy, txn_id, txn)
        assert txn["nonce"] == txn_id
        assert url.endswith(f"nonce={txn_id}")


class TestCreateLogingovAuth:
    """Tests for create_logingov_auth factory function."""

    def test_returns_none_when_auth_disabled(self):
        """Test that auth is disabled when AUTH_ENABLED=false."""
        with patch.dict(os.environ, {"AUTH_ENABLED": "false"}):
            result = create_logingov_auth()
            assert result is None

    def test_returns_none_when_auth_disabled_case_insensitive(self):
        """Test that AUTH_ENABLED=False (capitalized) also disables auth."""
        with patch.dict(os.environ, {"AUTH_ENABLED": "False"}):
            assert create_logingov_auth() is None

    def test_auth_enabled_by_default(self):
        """Test that auth is required when AUTH_ENABLED is unset."""
        env = {
            "LOGINGOV_CLIENT_ID": "test-client",
            "JWT_SIGNING_KEY": "test-key",
        }
        with patch.dict(os.environ, env, clear=True), pytest.raises(ValueError, match="BASE_URL"):
            create_logingov_auth()

    def test_raises_when_missing_base_url(self):
        """Test that missing BASE_URL raises ValueError."""
        env = {
            "AUTH_ENABLED": "true",
            "LOGINGOV_CLIENT_ID": "test-client",
            "JWT_SIGNING_KEY": "test-key",
        }
        with patch.dict(os.environ, env, clear=True), pytest.raises(ValueError, match="BASE_URL"):
            create_logingov_auth()

    def test_raises_when_missing_client_id(self):
        """Test that missing LOGINGOV_CLIENT_ID raises ValueError."""
        env = {
            "AUTH_ENABLED": "true",
            "BASE_URL": "https://example.com",
            "JWT_SIGNING_KEY": "test-key",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="LOGINGOV_CLIENT_ID"),
        ):
            create_logingov_auth()

    def test_raises_when_missing_jwt_key(self):
        """Test that missing JWT_SIGNING_KEY raises ValueError."""
        env = {
            "AUTH_ENABLED": "true",
            "BASE_URL": "https://example.com",
            "LOGINGOV_CLIENT_ID": "test-client",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="JWT_SIGNING_KEY"),
        ):
            create_logingov_auth()

    def test_returns_pkce_proxy_when_configured(self):
        """A configured environment yields a public PKCE OIDC proxy.

        The real OIDCProxy constructor fetches the IdP discovery document,
        so construction kwargs are captured via a stand-in.
        """
        from usa_spending_mcp_server import auth as auth_module

        captured: dict = {}

        class FakeProxy(auth_module.LoginGovOIDCProxy):
            def __init__(self, **kwargs):
                captured.update(kwargs)

        env = {
            "AUTH_ENABLED": "true",
            "BASE_URL": "https://example.com",
            "LOGINGOV_CLIENT_ID": "test-client",
            "JWT_SIGNING_KEY": "test-key",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                auth_module, "get_userinfo_url_from_config", return_value="https://example.com/userinfo"
            ),
            patch.object(auth_module, "LoginGovOIDCProxy", FakeProxy),
        ):
            proxy = create_logingov_auth()
        assert isinstance(proxy, FakeProxy)
        # Public client: PKCE, no client-secret auth method
        assert captured["client_id"] == "test-client"
        assert captured["token_endpoint_auth_method"] == "none"
        assert captured["client_secret"] == "unused"
        assert captured["base_url"] == "https://example.com"
        assert captured["jwt_signing_key"] == "test-key"
        assert captured["extra_authorize_params"]["scope"] == "openid email"
        assert captured["extra_authorize_params"]["acr_values"] == "urn:acr.login.gov:auth-only"
        assert isinstance(captured["token_verifier"], auth_module.LoginGovTokenVerifier)

    def test_proxy_is_oidc_proxy_subclass(self):
        """The login.gov proxy preserves the FastMCP OIDCProxy base."""
        from fastmcp.server.auth.oidc_proxy import OIDCProxy

        from usa_spending_mcp_server.auth import LoginGovOIDCProxy

        assert issubclass(LoginGovOIDCProxy, OIDCProxy)
        # The subclass overrides authorize-URL building to inject the nonce
        assert (
            LoginGovOIDCProxy._build_upstream_authorize_url
            is not OIDCProxy._build_upstream_authorize_url
        )


class TestConstants:
    """Tests for module constants."""

    def test_sandbox_config_url(self):
        """Test sandbox config URL is correct."""
        assert "identitysandbox.gov" in LOGINGOV_SANDBOX_CONFIG_URL
        assert ".well-known/openid-configuration" in LOGINGOV_SANDBOX_CONFIG_URL
