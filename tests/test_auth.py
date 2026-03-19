"""Unit tests for authentication flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.comelit_intercom_local.auth import authenticate
from custom_components.comelit_intercom_local.exceptions import AuthenticationError


class TestAuthenticate:
    @pytest.mark.asyncio
    async def test_authenticate_success(self):
        """authenticate() succeeds when response-code is 200."""
        channel = MagicMock()
        client = MagicMock()
        client.open_channel = AsyncMock(return_value=channel)
        client.send_json = AsyncMock(
            return_value={"response-code": 200, "response-string": "OK"}
        )

        await authenticate(client, "abcdef1234567890abcdef1234567890")

        client.open_channel.assert_called_once_with("UAUT", client.open_channel.call_args[0][1])
        client.send_json.assert_called_once()
        msg = client.send_json.call_args[0][1]
        assert msg["message"] == "access"
        assert msg["user-token"] == "abcdef1234567890abcdef1234567890"

    @pytest.mark.asyncio
    async def test_authenticate_raises_on_non_200(self):
        """authenticate() raises AuthenticationError when response-code != 200."""
        channel = MagicMock()
        client = MagicMock()
        client.open_channel = AsyncMock(return_value=channel)
        client.send_json = AsyncMock(
            return_value={"response-code": 403, "response-string": "Forbidden"}
        )

        with pytest.raises(AuthenticationError, match="403"):
            await authenticate(client, "sometoken")

    @pytest.mark.asyncio
    async def test_authenticate_raises_on_missing_code(self):
        """authenticate() raises AuthenticationError when response-code is absent (defaults to 0)."""
        channel = MagicMock()
        client = MagicMock()
        client.open_channel = AsyncMock(return_value=channel)
        client.send_json = AsyncMock(return_value={})

        with pytest.raises(AuthenticationError, match="Authentication failed"):
            await authenticate(client, "sometoken")

    @pytest.mark.asyncio
    async def test_authenticate_includes_reason_in_error(self):
        """AuthenticationError message includes response-string from device."""
        channel = MagicMock()
        client = MagicMock()
        client.open_channel = AsyncMock(return_value=channel)
        client.send_json = AsyncMock(
            return_value={"response-code": 401, "response-string": "Invalid token"}
        )

        with pytest.raises(AuthenticationError, match="Invalid token"):
            await authenticate(client, "badtoken")

    @pytest.mark.asyncio
    async def test_authenticate_sends_correct_channel_type(self):
        """authenticate() opens the UAUT channel."""
        channel = MagicMock()
        client = MagicMock()
        client.open_channel = AsyncMock(return_value=channel)
        client.send_json = AsyncMock(return_value={"response-code": 200})

        from custom_components.comelit_intercom_local.channels import ChannelType
        await authenticate(client, "token")

        client.open_channel.assert_called_once_with("UAUT", ChannelType.UAUT)
