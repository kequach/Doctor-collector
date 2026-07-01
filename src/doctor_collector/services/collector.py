"""Core service that collects therapists, applies filters, and manages state."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from doctor_collector.clients.therapie import TherapieClient
from doctor_collector.models.therapist import CollectionResult, TherapistProfile

if TYPE_CHECKING:
    from doctor_collector.config import AppConfig

logger = logging.getLogger(__name__)

_CSV_FIELDS = ["name", "email", "therapist_type", "website", "profile_url", "excluded"]
_TRUE_CSV_VALUES = {"1", "true", "yes", "y", "on", "ja", "x"}
StopCheck = Callable[[], bool]
StopWait = Callable[[float], bool]


def get_default_csv_path() -> Path:
    return Path(os.environ.get("CSV_FILE", Path.cwd() / "therapists.csv"))


def get_default_state_path() -> Path:
    return Path(os.environ.get("STATE_FILE", Path.cwd() / ".contacted_therapists.json"))


def load_therapists_csv(path: Path) -> list[TherapistProfile]:
    """Load therapist data from a CSV file."""
    if not path.exists():
        return []

    therapists = parse_therapists_csv(path.read_text(encoding="utf-8"))
    logger.info("Loaded %d therapists from %s", len(therapists), path)
    return therapists


def parse_therapists_csv(text: str) -> list[TherapistProfile]:
    """Parse therapist data from CSV text, including optional exclusion flags."""
    therapists: list[TherapistProfile] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        therapists.append(_therapist_from_csv_row(row))
    return therapists


def save_therapists_csv(path: Path, therapists: list[TherapistProfile]) -> None:
    """Write therapist data to CSV using the current review columns."""
    rows = [
        {
            "name": therapist.name,
            "email": therapist.email or "",
            "therapist_type": therapist.therapist_type,
            "website": therapist.website or "",
            "profile_url": therapist.profile_url,
            "excluded": "yes" if therapist.excluded else "",
        }
        for therapist in therapists
    ]
    _write_csv_atomic(path, _CSV_FIELDS, rows)


def load_therapists_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Load raw CSV rows so web review edits can preserve user-added columns."""
    if not path.exists():
        return _CSV_FIELDS.copy(), []

    reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")))
    fieldnames = list(reader.fieldnames or _CSV_FIELDS)
    rows: list[dict[str, str]] = []
    for row in reader:
        if None in row:
            raise ValueError("CSV row has more values than the header defines")
        rows.append({field: row.get(field) or "" for field in fieldnames})
    return _with_excluded_field(fieldnames), rows


def save_therapists_csv_rows(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> None:
    """Write raw CSV review rows while preserving extra columns."""
    _write_csv_atomic(path, _with_excluded_field(fieldnames), rows)


def _therapist_from_csv_row(row: dict[str, str | None]) -> TherapistProfile:
    return TherapistProfile(
        name=row.get("name") or "",
        email=row.get("email") or None,
        therapist_type=row.get("therapist_type") or "",
        website=row.get("website") or None,
        profile_url=row.get("profile_url") or "",
        excluded=_csv_bool(row.get("excluded")),
    )


def _csv_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUE_CSV_VALUES


def _with_excluded_field(fieldnames: list[str]) -> list[str]:
    deduped = list(dict.fromkeys(fieldnames))
    if "excluded" not in deduped:
        deduped.append("excluded")
    return deduped


def _write_csv_atomic(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = tempfile.NamedTemporaryFile(
        "w",
        newline="",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(temp_file.name)
    try:
        with temp_file:
            writer = csv.DictWriter(temp_file, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


class TherapistCollector:
    """Fetches therapist profiles, filters them, and manages state/CSV."""

    def __init__(
        self,
        config: AppConfig,
        *,
        state_file: Path | None = None,
        csv_file: Path | None = None,
        stop_requested: StopCheck | None = None,
        stop_wait: StopWait | None = None,
    ) -> None:
        self._config = config
        self._client = TherapieClient(
            config,
            stop_requested=stop_requested,
            stop_wait=stop_wait,
        )
        self.last_result: CollectionResult | None = None
        self.last_csv_saved = False
        self._state_path = state_file or get_default_state_path()
        self._csv_path = csv_file or get_default_csv_path()
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
        save_therapists_csv(self._csv_path, therapists)
        logger.info("Saved %d therapists to %s", len(therapists), self._csv_path)

    def load_csv(self) -> list[TherapistProfile]:
        """Load therapist data from the CSV file (for --contact without --collect)."""
        return load_therapists_csv(self._csv_path)

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    async def collect(self) -> CollectionResult:
        """Run a full collection: scrape, filter, save to CSV."""
        self.last_csv_saved = False
        if not self._config.therapie.post_code:
            logger.warning("No post_code configured — skipping collection")
            result = CollectionResult()
            self.last_result = result
            return result

        all_profiles = await self._client.fetch_therapist_listings()
        crawl_completed = getattr(self._client, "last_crawl_completed", True)
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

        if crawl_completed:
            self._save_csv(matching)
            self.last_csv_saved = True
        else:
            logger.warning(
                "Collection did not complete; leaving existing CSV unchanged"
            )

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
