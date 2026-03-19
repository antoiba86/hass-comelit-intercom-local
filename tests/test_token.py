"""Unit tests for token extraction — no device needed."""

from __future__ import annotations

import gzip
import io
import tarfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.exceptions import TokenExtractionError
from custom_components.comelit_intercom_local.token import (
    _parse_token_from_archive,
    extract_token,
)


def _make_tar_gz(files: dict[str, bytes]) -> bytes:
    """Build an in-memory tar.gz with the given filename→content mapping."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


VALID_USERS_CFG = b'9:4:"abcdef1234567890abcdef1234567890"'


class TestParseTokenFromArchive:
    def test_parse_token_success(self):
        archive = _make_tar_gz({"config/users.cfg": VALID_USERS_CFG})
        token = _parse_token_from_archive(archive)
        assert token == "abcdef1234567890abcdef1234567890"

    def test_parse_token_missing_users_cfg(self):
        archive = _make_tar_gz({"config/other.cfg": b"irrelevant content"})
        with pytest.raises(TokenExtractionError, match="users.cfg not found"):
            _parse_token_from_archive(archive)

    def test_parse_token_missing_users_cfg_lists_members(self):
        archive = _make_tar_gz({"config/other.cfg": b"data", "config/network.cfg": b"data"})
        with pytest.raises(TokenExtractionError, match="other.cfg"):
            _parse_token_from_archive(archive)

    def test_parse_token_no_match_in_users_cfg(self):
        archive = _make_tar_gz({"config/users.cfg": b"no token here"})
        with pytest.raises(TokenExtractionError, match="Token pattern not found"):
            _parse_token_from_archive(archive)

    def test_parse_token_skips_null_token(self):
        null_token = b'9:4:"00000000000000000000000000000000"'
        valid_token = b'9:4:"abcdef1234567890abcdef1234567890"'
        archive = _make_tar_gz({"config/users.cfg": null_token + b"\n" + valid_token})
        token = _parse_token_from_archive(archive)
        assert token == "abcdef1234567890abcdef1234567890"

    def test_parse_token_all_null_tokens(self):
        null_token = b'9:4:"00000000000000000000000000000000"'
        archive = _make_tar_gz({"config/users.cfg": null_token})
        with pytest.raises(TokenExtractionError, match="Token pattern not found"):
            _parse_token_from_archive(archive)

    def test_parse_token_gzipped_users_cfg(self):
        compressed = gzip.compress(VALID_USERS_CFG)
        archive = _make_tar_gz({"config/users.cfg": compressed})
        token = _parse_token_from_archive(archive)
        assert token == "abcdef1234567890abcdef1234567890"

    def test_parse_token_bad_archive(self):
        with pytest.raises(TokenExtractionError, match="Failed to read backup archive"):
            _parse_token_from_archive(b"not a valid tar.gz")


# ---------------------------------------------------------------------------
# extract_token — HTTP flow with mocked aiohttp
# ---------------------------------------------------------------------------


def _make_mock_response(status: int = 200, text: str = "", content: bytes = b""):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.read = AsyncMock(return_value=content)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_session(*responses):
    """Build a mock aiohttp.ClientSession that returns responses in order."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    response_iter = iter(responses)

    def _get_next(*args, **kwargs):
        return next(response_iter)

    session.post = MagicMock(side_effect=_get_next)
    session.get = MagicMock(side_effect=_get_next)
    return session


class TestExtractToken:
    @pytest.mark.asyncio
    async def test_extract_token_success(self):
        """Full happy-path: login → create backup → list → download → parse."""
        archive = _make_tar_gz({"config/users.cfg": VALID_USERS_CFG})

        login_resp = _make_mock_response(200, "Access granted")
        backup_resp = _make_mock_response(200, "Backup successfully created")
        list_resp = _make_mock_response(200, '<a href="12345.tar.gz">12345.tar.gz</a>')
        dl_resp = _make_mock_response(200, content=archive)

        session = _make_session(login_resp, backup_resp, list_resp, dl_resp)

        with patch(
            "custom_components.comelit_intercom_local.token.aiohttp.ClientSession",
            return_value=session,
        ):
            with patch("asyncio.sleep", AsyncMock()):
                token = await extract_token("192.168.1.1", "comelit", 8080)

        assert token == "abcdef1234567890abcdef1234567890"

    @pytest.mark.asyncio
    async def test_extract_token_login_fails_bad_status(self):
        """Raises TokenExtractionError when login returns non-200."""
        login_resp = _make_mock_response(403, "Forbidden")
        session = _make_session(login_resp)

        with patch(
            "custom_components.comelit_intercom_local.token.aiohttp.ClientSession",
            return_value=session,
        ):
            with pytest.raises(TokenExtractionError, match="Login failed with status 403"):
                await extract_token("192.168.1.1")

    @pytest.mark.asyncio
    async def test_extract_token_login_fails_wrong_content(self):
        """Raises TokenExtractionError when login response lacks 'Access granted'."""
        login_resp = _make_mock_response(200, "Invalid password")
        session = _make_session(login_resp)

        with patch(
            "custom_components.comelit_intercom_local.token.aiohttp.ClientSession",
            return_value=session,
        ):
            with pytest.raises(TokenExtractionError, match="Login failed"):
                await extract_token("192.168.1.1")

    @pytest.mark.asyncio
    async def test_extract_token_backup_creation_fails(self):
        """Raises TokenExtractionError when backup creation response is unexpected."""
        login_resp = _make_mock_response(200, "Access granted")
        backup_resp = _make_mock_response(200, "something unexpected")
        session = _make_session(login_resp, backup_resp)

        with patch(
            "custom_components.comelit_intercom_local.token.aiohttp.ClientSession",
            return_value=session,
        ):
            with patch("asyncio.sleep", AsyncMock()):
                with pytest.raises(TokenExtractionError, match="Backup creation failed"):
                    await extract_token("192.168.1.1")

    @pytest.mark.asyncio
    async def test_extract_token_no_backup_files(self):
        """Raises TokenExtractionError when no .tar.gz files listed."""
        login_resp = _make_mock_response(200, "Access granted")
        backup_resp = _make_mock_response(200, "Backup successfully created")
        list_resp = _make_mock_response(200, "<html>No backups here</html>")
        session = _make_session(login_resp, backup_resp, list_resp)

        with patch(
            "custom_components.comelit_intercom_local.token.aiohttp.ClientSession",
            return_value=session,
        ):
            with patch("asyncio.sleep", AsyncMock()):
                with pytest.raises(TokenExtractionError, match="No backup files found"):
                    await extract_token("192.168.1.1")

    @pytest.mark.asyncio
    async def test_extract_token_download_fails(self):
        """Raises TokenExtractionError when archive download returns non-200."""
        login_resp = _make_mock_response(200, "Access granted")
        backup_resp = _make_mock_response(200, "Backup successfully created")
        list_resp = _make_mock_response(200, '<a href="99.tar.gz">99.tar.gz</a>')
        dl_resp = _make_mock_response(404)
        session = _make_session(login_resp, backup_resp, list_resp, dl_resp)

        with patch(
            "custom_components.comelit_intercom_local.token.aiohttp.ClientSession",
            return_value=session,
        ):
            with patch("asyncio.sleep", AsyncMock()):
                with pytest.raises(TokenExtractionError, match="Backup download failed"):
                    await extract_token("192.168.1.1")

    @pytest.mark.asyncio
    async def test_extract_token_backup_page_fails(self):
        """Raises TokenExtractionError when backup listing returns non-200."""
        login_resp = _make_mock_response(200, "Access granted")
        backup_resp = _make_mock_response(200, "Backup successfully created")
        list_resp = _make_mock_response(500, "Server Error")
        session = _make_session(login_resp, backup_resp, list_resp)

        with patch(
            "custom_components.comelit_intercom_local.token.aiohttp.ClientSession",
            return_value=session,
        ):
            with patch("asyncio.sleep", AsyncMock()):
                with pytest.raises(TokenExtractionError, match="Backup page returned status 500"):
                    await extract_token("192.168.1.1")
