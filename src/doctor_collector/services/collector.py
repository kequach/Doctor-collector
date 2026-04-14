"""Core service that collects therapists, applies filters, and manages state."""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from doctor_collector.clients.therapie import TherapieClient
from doctor_collector.models.therapist import CollectionResult, TherapistProfile

if TYPE_CHECKING:
    from doctor_collector.config import AppConfig

logger = logging.getLogger(__name__)

_DEFAULT_STATE_PATH = Path(
    os.environ.get("STATE_FILE", Path.cwd() / ".contacted_therapists.json"),
)
_DEFAULT_CSV_PATH = Path(os.environ.get("CSV_FILE", Path.cwd() / "therapists.csv"))

_CSV_FIELDS = ["name", "email", "therapist_type", "website", "profile_url"]


class TherapistCollector:
    """Fetches therapist profiles, filters them, and manages state/CSV."""

    def __init__(
        self,
        config: AppConfig,
        *,
        state_file: Path | None = None,
        csv_file: Path | None = None,
    ) -> None:
        self._config = config
        self._client = TherapieClient(config)
        self.last_result: CollectionResult | None = None
        self._state_path = state_file or _DEFAULT_STATE_PATH
        self._csv_path = csv_file or _DEFAULT_CSV_PATH
        self._contacted_emails: set[str] = self._load_contacted()

    @property
    def csv_path(self) -> Path:
        return self._csv_path

    @property
    def contacted_emails(self) -> set[str]:
        return set(self._contacted_emails)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # State persistence (contacted emails)
    # ------------------------------------------------------------------

    def _load_contacted(self) -> set[str]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            contacted = data.get("contacted_emails", [])
            logger.info("Loaded %d contacted emails from %s", len(contacted), self._state_path)
            return set(contacted)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return set()

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "contacted_emails": sorted(self._contacted_emails),
        }
        self._state_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.debug("Saved state to %s", self._state_path)

    def mark_contacted(self, emails: set[str]) -> None:
        self._contacted_emails |= emails
        self._save_state()

    # ------------------------------------------------------------------
    # CSV persistence
    # ------------------------------------------------------------------

    def _save_csv(self, therapists: list[TherapistProfile]) -> None:
        with self._csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            for t in therapists:
                writer.writerow({
                    "name": t.name,
                    "email": t.email or "",
                    "therapist_type": t.therapist_type,
                    "website": t.website or "",
                    "profile_url": t.profile_url,
                })
        logger.info("Saved %d therapists to %s", len(therapists), self._csv_path)

    def load_csv(self) -> list[TherapistProfile]:
        """Load therapist data from the CSV file (for --contact without --collect)."""
        if not self._csv_path.exists():
            return []
        therapists: list[TherapistProfile] = []
        with self._csv_path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                therapists.append(TherapistProfile(
                    name=row.get("name", ""),
                    email=row.get("email") or None,
                    therapist_type=row.get("therapist_type", ""),
                    website=row.get("website") or None,
                    profile_url=row.get("profile_url", ""),
                ))
        logger.info("Loaded %d therapists from %s", len(therapists), self._csv_path)
        return therapists

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    async def collect(self) -> CollectionResult:
        """Run a full collection: scrape, filter, save to CSV."""
        if not self._config.therapie.post_code:
            logger.warning("No post_code configured — skipping collection")
            result = CollectionResult()
            self.last_result = result
            return result

        all_profiles = await self._client.fetch_therapist_listings()
        logger.info("Scraped %d total profiles", len(all_profiles))

        matching = self._apply_filters(all_profiles)
        logger.info("%d profiles passed filters", len(matching))

        new_therapists = [
            p for p in matching
            if p.email and p.email not in self._contacted_emails
        ]

        result = CollectionResult(
            total_profiles_scraped=len(all_profiles),
            total_matching=len(matching),
            therapists=matching,
            new_therapists=new_therapists,
        )

        if matching:
            self._save_csv(matching)

        self.last_result = result
        return result

    def _apply_filters(self, profiles: list[TherapistProfile]) -> list[TherapistProfile]:
        exclude = self._config.filters.exclude_types
        results: list[TherapistProfile] = []

        for profile in profiles:
            if not profile.email:
                continue

            excluded = any(
                keyword.lower() in profile.therapist_type.lower()
                for keyword in exclude
                if keyword
            )
            if excluded:
                logger.debug("Excluded %s (type: %s)", profile.name, profile.therapist_type)
                continue

            results.append(profile)

        return results
