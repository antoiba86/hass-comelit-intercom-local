"""Unit tests for token extraction — no device needed."""

from __future__ import annotations

import gzip
import io
import tarfile

import pytest

from custom_components.comelit_intercom_local.exceptions import TokenExtractionError
from custom_components.comelit_intercom_local.token import _parse_token_from_archive


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
