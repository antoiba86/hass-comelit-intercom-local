"""Token extraction from the device's HTTP backup archive.

Based on https://github.com/nicolas-fricke/ha-component-comelit-intercom
"""

from __future__ import annotations

import asyncio
import gzip
import io
import logging
import re
import tarfile

import aiohttp

from .exceptions import TokenExtractionError

_LOGGER = logging.getLogger(__name__)

TOKEN_PATTERN = re.compile(r'9:4:"([a-f0-9]{32})"', re.IGNORECASE)


async def extract_token(
    host: str,
    password: str = "comelit",  # nosemgrep: hardcoded-password-default-argument
    http_port: int = 8080,
) -> str | None:
    """Extract the 32-char hex authentication token from the device backup.

    The Comelit web interface uses IP-based sessions — once we authenticate
    from an IP address, all subsequent requests from that IP are authorized.
    """
    base_url = f"http://{host}:{http_port}"
    timeout = aiohttp.ClientTimeout(total=60, connect=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Step 1: Login to establish IP-based session
        _LOGGER.debug("Logging in to %s", base_url)
        login_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"{base_url}/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        async with session.post(
            f"{base_url}/do-login.html",
            data={"l-pwd": password},
            headers=login_headers,
        ) as resp:
            if resp.status != 200:
                raise TokenExtractionError(f"Login failed with status {resp.status}")
            login_content = await resp.text()
            if "Access granted" not in login_content:
                raise TokenExtractionError("Login failed — check password")

        _LOGGER.debug("Login successful")

        # Step 2: Create a fresh backup
        _LOGGER.debug("Creating backup")
        backup_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{base_url}/config-backup.html",
        }
        async with session.post(
            f"{base_url}/create-backup.html",
            headers=backup_headers,
        ) as resp:
            create_response = await resp.text()
            if "Backup successfully created" not in create_response:
                _LOGGER.error("Backup creation failed: %s", create_response)
                raise TokenExtractionError("Backup creation failed")

        # Wait for the device to finish creating the backup file
        await asyncio.sleep(2)

        # Step 3: Find backup link
        _LOGGER.debug("Listing backups")
        async with session.get(f"{base_url}/config-backup.html") as resp:
            if resp.status != 200:
                raise TokenExtractionError(f"Backup page returned status {resp.status}")
            html = await resp.text()

        backup_files = re.findall(r"([0-9]+\.tar\.gz)", html)

        if not backup_files:
            raise TokenExtractionError(
                f"No backup files found on device. "
                f"Page content (first 500 chars): {html[:500]}"
            )

        # Use the latest backup (highest number)
        backup_files.sort()
        latest_backup = backup_files[-1]
        _LOGGER.debug("Using latest backup: %s", latest_backup)

        # Step 4: Download the archive
        archive_url = f"{base_url}/{latest_backup}"
        _LOGGER.debug("Downloading backup from %s", archive_url)
        async with session.get(archive_url) as resp:
            if resp.status != 200:
                raise TokenExtractionError(f"Backup download failed with status {resp.status}")
            archive_data = await resp.read()

        _LOGGER.debug("Downloaded %d bytes", len(archive_data))

        # Step 5: Extract token from users.cfg
        return _parse_token_from_archive(archive_data)


def _parse_token_from_archive(archive_data: bytes) -> str | None:
    """Parse the authentication token from a backup tar.gz archive."""
    members_seen: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as tar:
            for member in tar.getmembers():
                members_seen.append(member.name)
                if member.name.endswith("users.cfg"):
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    raw = f.read()

                    # Some firmware versions gzip users.cfg without a .gz extension
                    if raw[:2] == b"\x1f\x8b":
                        raw = gzip.decompress(raw)

                    content = raw.decode("utf-8", errors="replace")
                    _LOGGER.debug("users.cfg size: %d bytes", len(content))

                    matches = TOKEN_PATTERN.findall(content)
                    if matches:
                        # Skip null tokens (all zeros)
                        for token in matches:
                            if token != "00000000000000000000000000000000":
                                _LOGGER.debug("Extracted token: %s...%s", token[:4], token[-4:])  # nosemgrep: python-logger-credential-disclosure
                                return token

                    raise TokenExtractionError(
                        f"Token pattern not found in users.cfg "
                        f"(file size: {len(content)} bytes)"
                    )

    except tarfile.TarError as e:
        raise TokenExtractionError(f"Failed to read backup archive: {e}") from e

    raise TokenExtractionError(
        f"users.cfg not found in backup archive. "
        f"Members seen: {members_seen}"
    )
