"""Module that provides utilities for interacting with the Bunkr API."""
from __future__ import annotations

import asyncio
import re
import aiohttp
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

import aiohttp

from src.config import BUNKR_API, DOWNLOAD_API, JS_VARS_COMP

if TYPE_CHECKING:
    from bs4 import BeautifulSoup


_DEFAULT_MAX_RETRIES = 5
_DEFAULT_BASE_DELAY = 2.0
_DEFAULT_TIMEOUT = 30


def unescape_js_path(value: str) -> str:
    """Normalize JavaScript-escaped URL fragments."""
    return value.replace(r"\/", "/").replace(r"\\", "\\")


def extract_page_vars(soup: BeautifulSoup) -> dict[str, str]:
    """Extract CDN/runtime variables from inline script tags."""
    for script in soup.find_all("script"):
        if script.string and "var jsCDN" in script.string:
            matches = JS_VARS_COMP.findall(script.string)
            return {
                key: unescape_js_path(value).strip("'\"")
                for key, value in matches
            }

    return {}


def extract_file_id(soup: BeautifulSoup) -> str | None:
    """Extract file identifier from HTML script metadata."""
    script = soup.find("script")
    if not script:
        return None

    return script.get("data-file-id")


async def get_download_response(
    session: aiohttp.ClientSession,
    file_id: str,
) -> str | None:
    """Fetch unsigned download URL for non-landing page assets.

    Used for file types that do not expose CDN variables (e.g. archives, videos).

    Retries with exponential backoff on network-related failures.
    Returns None instead of raising if all attempts fail, so the caller
    can skip the file gracefully without aborting the whole session.
    """
    for attempt in range(1, _DEFAULT_MAX_RETRIES + 1):
        try:
            async with session.post(
                DOWNLOAD_API,
                json={"id": file_id},
                timeout=aiohttp.ClientTimeout(total=_DEFAULT_TIMEOUT),
            ) as response:
                response.raise_for_status()
                data = await response.json()

            # Guard against unexpected API response shapes so that a schema change
            # raises a warning rather than an unhandled KeyError.
            base_url = data.get("mediafiles")
            path = data.get("path")

            if not base_url or not path:
                return None

            parsed_url = urlparse(base_url)
            return urlunparse(parsed_url._replace(path=path))

        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt < _DEFAULT_MAX_RETRIES:
                await asyncio.sleep(_DEFAULT_BASE_DELAY * (2 ** (attempt - 1)))

    return None


async def get_api_response(
    session: aiohttp.ClientSession,
    soup: BeautifulSoup | None = None,
    max_retries: int = 5,
    base_delay: float = 2.0,
) -> str | None:
    """Fetch encryption data from the Bunkr API.

    Retries up to *max_retries* times with exponential back-off on any
    network or timeout error, so slow or unstable connections do not crash
    the entire download process.

    Args:
        session: An active aiohttp client session.
        soup: Parsed HTML of the Bunkr item page.
        max_retries: Maximum number of attempts before giving up.
        base_delay: Base delay in seconds for exponential back-off.

    Returns:
        A signed CDN URL string on success, or ``None`` if the API could
        not be reached after all retries.
    """
    js_vars = extract_js_vars(soup)
    if not js_vars:
        return None

    Retries the signing API call with exponential backoff on network failures.
    Returns None if the media URL cannot be resolved or all signing attempts
    fail, allowing the caller to skip the file without crashing the session.
    """
    page_vars = extract_page_vars(soup) if soup else {}
    cdn_url = page_vars.get("jsCDN")

    # Only use the direct download endpoint when no JS vars are present,
    # which indicates an asset type without a standard landing page.
    file_id = extract_file_id(soup) if soup and not page_vars else None
    unsigned_url = await get_download_response(session, file_id) if file_id else None

    if not cdn_url and not unsigned_url:
        return None

    js_cdn_path = urlparse(js_cdn).path

    last_error: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            async with session.get(
                BUNKR_API,
                params={"path": js_cdn_path},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                sign_data = await response.json()

            token = sign_data.get("token")
            ex = sign_data.get("ex")
            if token and ex:
                return f"{js_cdn}?token={token}&ex={ex}"
            return js_cdn

        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                print(
                    f"[API] Attempt {attempt}/{max_retries} failed "
                    f"({type(exc).__name__}). Retrying in {delay:.0f}s\u2026"
                )
                await asyncio.sleep(delay)

    print(f"[API] All {max_retries} attempts failed. Last error: {last_error}")
    return None
