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
from typing import TYPE_CHECKING

import httpx
from bs4 import BeautifulSoup

from doctor_collector.models.therapist import TherapistProfile

if TYPE_CHECKING:
    from doctor_collector.config import AppConfig

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.therapie.de"
_PROFILE_LINK_RE = re.compile(r"/profil/")
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _decode_email(encoded: str) -> str:
    """Decode the obfuscated email from ``data-contact-email``.

    therapie.de uses a character-shift cipher where each character's code
    point is incremented by 1.  Reversing it is trivial.
    """
    return "".join(chr(ord(c) - 1) for c in encoded)


class TherapieClient:
    """Scrapes therapist listings and profiles from therapie.de.

    All requests use httpx (async HTTP) — no browser automation needed.
    Emails are decoded from an obfuscated HTML attribute on the profile page.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._http = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _build_start_url(self) -> str:
        t = self._config.therapie
        return (
            f"{_BASE_URL}/therapeutensuche/ergebnisse/"
            f"?ort={t.post_code}&page={t.start_page}"
            f"&therapieangebot={t.therapy_form}&verfahren={t.therapy_type}"
        )

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

                for profile_url in profile_urls:
                    try:
                        profile = await self._extract_profile(profile_url)
                        all_therapists.append(profile)
                        logger.info("Extracted: %s", profile.name)
                    except Exception:
                        logger.exception("Failed to extract profile: %s", profile_url)

                    await asyncio.sleep(cfg.request_delay_seconds)

                current_url = next_url
                page_num += 1

            except Exception:
                logger.exception("Failed to crawl listing page: %s", current_url)
                break

        logger.info("Crawling complete — %d profiles collected", len(all_therapists))
        return all_therapists

    async def _parse_listing_page(self, url: str) -> tuple[list[str], str | None]:
        """Fetch a listing page and return (profile_urls, next_page_url)."""
        response = await self._http.get(url)
        response.raise_for_status()
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
        response = await self._http.get(profile_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        return TherapistProfile(
            name=self._extract_name(soup),
            website=self._extract_website(soup),
            email=self._extract_email(soup),
            therapist_type=self._extract_type(soup),
            profile_url=profile_url,
        )

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
