"""Async HTTP client for therapie.de — no browser automation required.

The email address on each profile page is stored in a ``data-contact-email``
attribute on the contact button, obfuscated with a simple Caesar cipher
(each character shifted by +1).  We decode it directly from the HTML
instead of clicking the button with Selenium.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from doctor_collector.models.therapist import TherapistProfile

if TYPE_CHECKING:
    from doctor_collector.config import AppConfig

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.therapie.de"
_PROFILE_LINK_RE = re.compile(r"/profil/")
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_MAX_CONCURRENT = 8
_MAX_HTTP_RETRIES = 3
_RATE_LIMIT_STATUS_CODE = 429
_DEFAULT_RATE_LIMIT_DELAY_SECONDS = 60.0
_MAX_RATE_LIMIT_DELAY_SECONDS = 300.0
_TRANSIENT_STATUS_CODES = {500, 502, 503, 504}
_TRANSIENT_RETRY_DELAY_SECONDS = 2.0
_REQUEST_ERROR_RETRY_DELAY_SECONDS = 2.0


def _decode_email(encoded: str) -> str:
    """Decode the obfuscated email from ``data-contact-email``.

    therapie.de uses a character-shift cipher where each character's code
    point is incremented by 1.  Reversing it is trivial.
    """
    return "".join(chr(ord(c) - 1) for c in encoded)


class TherapieRateLimitError(RuntimeError):
    """Raised when therapie.de keeps returning HTTP 429 after retries."""


class TherapieRequestError(RuntimeError):
    """Raised when a request keeps failing after retries."""


class TherapieClient:
    """Scrapes therapist listings and profiles from therapie.de.

    All requests use httpx (async HTTP) — no browser automation needed.
    Emails are decoded from an obfuscated HTML attribute on the profile page.
    Profile pages are fetched concurrently (up to 8 at a time) for speed.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._http = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )
        self._sem = asyncio.Semaphore(_MAX_CONCURRENT)
        self._request_lock = asyncio.Lock()
        self._rate_limit_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._rate_limited_until = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    def _build_start_url(self) -> str:
        t = self._config.therapie
        params = {
            "ort": t.post_code,
            "page": t.start_page,
            "search_radius": t.search_radius_km,
            "therapieangebot": t.therapy_form,
            "verfahren": t.therapy_type,
        }
        return f"{_BASE_URL}/therapeutensuche/ergebnisse/?{urlencode(params)}"

    async def fetch_therapist_listings(self) -> list[TherapistProfile]:
        """Crawl all listing pages and extract full profiles."""
        cfg = self._config.therapie
        all_therapists: list[TherapistProfile] = []
        current_url: str | None = self._build_start_url()
        page_num = 1

        while current_url and page_num <= cfg.max_pages:
            logger.info("Crawling listing page %d: %s", page_num, current_url)
            try:
                profile_urls, next_url = await self._parse_listing_page(current_url)
                logger.info("Found %d profiles on page %d", len(profile_urls), page_num)

                profiles = await self._fetch_profiles_batch(profile_urls)
                all_therapists.extend(profiles)

                current_url = next_url
                page_num += 1

            except TherapieRateLimitError as exc:
                logger.warning("Stopping crawl: %s", exc)
                break
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "Failed to crawl listing page %s: HTTP %d",
                    current_url,
                    exc.response.status_code,
                )
                break
            except TherapieRequestError as exc:
                logger.warning("Stopping crawl after request errors: %s", exc)
                break
            except httpx.RequestError as exc:
                logger.warning(
                    "Failed to crawl listing page %s: %s",
                    current_url,
                    self._format_request_error(exc),
                )
                break
            except Exception:
                logger.exception("Failed to crawl listing page: %s", current_url)
                break

        logger.info("Crawling complete — %d profiles collected", len(all_therapists))
        return all_therapists

    async def _fetch_profiles_batch(self, urls: list[str]) -> list[TherapistProfile]:
        """Fetch multiple profile pages concurrently with a semaphore limit."""

        async def _limited(url: str) -> TherapistProfile | None:
            async with self._sem:
                try:
                    return await self._extract_profile(url)
                except TherapieRateLimitError as exc:
                    logger.warning("Skipping profile after rate limit: %s", exc)
                    return None
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        "Skipping profile %s after HTTP %d",
                        url,
                        exc.response.status_code,
                    )
                    return None
                except httpx.RequestError as exc:
                    logger.warning(
                        "Skipping profile %s after request error: %s",
                        url,
                        self._format_request_error(exc),
                    )
                    return None
                except TherapieRequestError as exc:
                    logger.warning("Skipping profile after request errors: %s", exc)
                    return None
                except Exception:
                    logger.exception("Failed to extract profile: %s", url)
                    return None

        results = await asyncio.gather(*[_limited(u) for u in urls])
        profiles = [p for p in results if p is not None]
        for p in profiles:
            logger.info("Extracted: %s", p.name)
        return profiles

    async def _parse_listing_page(self, url: str) -> tuple[list[str], str | None]:
        """Fetch a listing page and return (profile_urls, next_page_url)."""
        response = await self._get(url)
        soup = BeautifulSoup(response.content, "html.parser")

        profile_urls: list[str] = []
        results_list = soup.find("ul", class_="search-results-list")
        if results_list:
            for entry in results_list.find_all("li"):
                link = entry.find("a", href=_PROFILE_LINK_RE)
                if link and link.get("href"):
                    profile_urls.append(_BASE_URL + link["href"])

        next_url: str | None = None
        try:
            pagenav = soup.find("ul", attrs={"id": "pagenav-bottom"})
            if pagenav:
                next_li = pagenav.find("li", class_="next")
                if next_li:
                    next_link = next_li.find("a")
                    if next_link and next_link.get("href"):
                        next_url = _BASE_URL + next_link["href"]
        except Exception:
            logger.debug("No next page link found")

        return profile_urls, next_url

    async def _extract_profile(self, profile_url: str) -> TherapistProfile:
        """Fetch a profile page and extract all fields from the HTML."""
        response = await self._get(profile_url)
        soup = BeautifulSoup(response.content, "html.parser")

        return TherapistProfile(
            name=self._extract_name(soup),
            website=self._extract_website(soup),
            email=self._extract_email(soup),
            therapist_type=self._extract_type(soup),
            profile_url=profile_url,
        )

    async def _get(self, url: str) -> httpx.Response:
        """GET a URL with pacing, retrying, and therapie.de rate-limit handling."""
        for attempt in range(1, _MAX_HTTP_RETRIES + 2):
            await self._wait_for_rate_limit()
            await self._wait_for_request_slot()

            try:
                response = await self._http.get(url)
            except httpx.RequestError as exc:
                if attempt <= _MAX_HTTP_RETRIES:
                    delay = _REQUEST_ERROR_RETRY_DELAY_SECONDS * attempt
                    logger.warning(
                        "Request error for %s: %s; retrying in %.0f seconds (%d/%d)",
                        url,
                        self._format_request_error(exc),
                        delay,
                        attempt,
                        _MAX_HTTP_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue

                raise TherapieRequestError(
                    f"request to {url} failed after {_MAX_HTTP_RETRIES} retries: "
                    f"{self._format_request_error(exc)}"
                ) from exc

            if response.status_code == _RATE_LIMIT_STATUS_CODE:
                delay = self._retry_after_seconds(response)
                if delay is None:
                    delay = self._rate_limit_delay_seconds(attempt)
                await self._set_rate_limit(delay)

                if attempt <= _MAX_HTTP_RETRIES:
                    logger.warning(
                        "therapie.de rate limit for %s; waiting %.0f seconds before retry %d/%d",
                        url,
                        delay,
                        attempt,
                        _MAX_HTTP_RETRIES,
                    )
                    continue

                raise TherapieRateLimitError(
                    f"therapie.de returned HTTP 429 for {url} after "
                    f"{_MAX_HTTP_RETRIES} retries"
                )

            if (
                response.status_code in _TRANSIENT_STATUS_CODES
                and attempt <= _MAX_HTTP_RETRIES
            ):
                delay = _TRANSIENT_RETRY_DELAY_SECONDS * attempt
                logger.warning(
                    "Transient HTTP %d for %s; retrying in %.0f seconds (%d/%d)",
                    response.status_code,
                    url,
                    delay,
                    attempt,
                    _MAX_HTTP_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            response.raise_for_status()
            return response

        raise RuntimeError("unreachable HTTP retry state")

    async def _wait_for_request_slot(self) -> None:
        delay = self._config.therapie.request_delay_seconds
        if delay <= 0:
            return

        async with self._request_lock:
            loop = asyncio.get_running_loop()
            sleep_for = self._next_request_at - loop.time()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            self._next_request_at = loop.time() + delay

    async def _wait_for_rate_limit(self) -> None:
        while True:
            async with self._rate_limit_lock:
                sleep_for = self._rate_limited_until - asyncio.get_running_loop().time()

            if sleep_for <= 0:
                return

            await asyncio.sleep(sleep_for)

    async def _set_rate_limit(self, delay: float) -> None:
        async with self._rate_limit_lock:
            loop = asyncio.get_running_loop()
            self._rate_limited_until = max(self._rate_limited_until, loop.time() + delay)

    @staticmethod
    def _rate_limit_delay_seconds(attempt: int) -> float:
        delay = _DEFAULT_RATE_LIMIT_DELAY_SECONDS * (2 ** (attempt - 1))
        return min(delay, _MAX_RATE_LIMIT_DELAY_SECONDS)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None

        try:
            delay = float(raw)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
            except (TypeError, ValueError, IndexError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()

        return min(max(delay, 0.0), _MAX_RATE_LIMIT_DELAY_SECONDS)

    @staticmethod
    def _format_request_error(exc: httpx.RequestError) -> str:
        message = str(exc).strip()
        if message:
            return f"{type(exc).__name__}: {message}"
        return type(exc).__name__

    @staticmethod
    def _extract_name(soup: BeautifulSoup) -> str:
        try:
            name_div = soup.find("div", attrs={"class": "therapist-name"})
            if name_div:
                name_span = name_div.find("span", attrs={"itemprop": "name"})
                if name_span:
                    return name_span.text.strip()
        except Exception:
            logger.debug("Name extraction failed")
        return "Name not found"

    @staticmethod
    def _extract_website(soup: BeautifulSoup) -> str | None:
        try:
            web_div = soup.find("div", class_="contact-web")
            if web_div:
                link = web_div.find("a")
                if link and link.get("href"):
                    return link["href"]
        except Exception:
            logger.debug("No website found")
        return None

    @staticmethod
    def _extract_email(soup: BeautifulSoup) -> str | None:
        """Decode the email from the contact button's data attribute."""
        try:
            button = soup.find("button", attrs={"id": "contact-button"})
            if button:
                encoded = button.get("data-contact-email")
                if encoded:
                    return _decode_email(encoded)
        except Exception:
            logger.debug("No email found")
        return None

    @staticmethod
    def _extract_type(soup: BeautifulSoup) -> str:
        try:
            name_div = soup.find("div", attrs={"class": "therapist-name"})
            if name_div:
                desc = name_div.find("h2", attrs={"itemprop": "description"})
                if desc:
                    return desc.text.strip()
        except Exception:
            logger.debug("No therapist type found")
        return ""
