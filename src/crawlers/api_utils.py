"""Module that provides utilities for interacting with the Bunkr API."""
from __future__ import annotations

import asyncio
import re
import aiohttp
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from src.config import BUNKR_API, JS_VARS_REGEX

if TYPE_CHECKING:
    from bs4 import BeautifulSoup


def unescape_js_path(value: str) -> str:
    """Unescape common JavaScript-escaped URL fragments."""
    return value.replace(r"\/", "/")


def extract_js_vars(soup: BeautifulSoup) -> dict[str, str]:
    """Extract runtime variables embedded in Bunkr inline JavaScript."""
    for script in soup.find_all("script"):
        if script.string and "var jsCDN" in script.string:
            matches = re.compile(JS_VARS_REGEX, re.DOTALL).findall(script.string)
            return {key: unescape_js_path(value).strip("\"'") for key, value in matches}
    return {}


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

    js_cdn = js_vars.get("jsCDN")
    if not js_cdn:
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
