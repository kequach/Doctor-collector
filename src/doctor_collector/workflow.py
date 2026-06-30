"""Application workflows shared by the CLI and local web UI."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from doctor_collector.config import load_config
from doctor_collector.models.therapist import TherapistProfile
from doctor_collector.notifications.console import ConsoleNotifier
from doctor_collector.services.collector import TherapistCollector
from doctor_collector.services.contactor import TherapistContactor


class WorkflowError(RuntimeError):
    """Raised when a user-facing workflow cannot be started or completed."""


StopCheck = Callable[[], bool]
StopWait = Callable[[float], bool]


@dataclass(frozen=True)
class CollectSummary:
    total_profiles_scraped: int
    total_matching: int
    saved_to: Path
    therapists: int
    csv_saved: bool


@dataclass(frozen=True)
class ContactSummary:
    to_contact: int
    already_contacted: int
    contacted: int


async def collect_therapists(
    config_path: str | Path | None = None,
    *,
    notify: bool = True,
    csv_file: Path | None = None,
    state_file: Path | None = None,
    apply_env_overrides: bool = True,
    stop_requested: StopCheck | None = None,
    stop_wait: StopWait | None = None,
) -> CollectSummary:
    """Run collection through the existing collector service."""
    config = load_config(config_path, apply_env_overrides=apply_env_overrides)
    if not config.therapie.post_code:
        raise WorkflowError("No post_code configured - set therapie.post_code in config.yaml")

    collector = TherapistCollector(
        config,
        state_file=state_file,
        csv_file=csv_file,
        stop_requested=stop_requested,
        stop_wait=stop_wait,
    )
    notifier = ConsoleNotifier(enabled=notify)

    try:
        result = await collector.collect()
        if notifier.is_enabled():
            await notifier.send(result.therapists)

        return CollectSummary(
            total_profiles_scraped=result.total_profiles_scraped,
            total_matching=result.total_matching,
            saved_to=collector.csv_path,
            therapists=len(result.therapists),
            csv_saved=collector.last_csv_saved,
        )
    finally:
        await collector.close()


async def contact_collected_therapists(
    config_path: str | Path | None = None,
    *,
    csv_file: Path | None = None,
    state_file: Path | None = None,
    apply_env_overrides: bool = True,
    expected_csv_signature: str | None = None,
) -> ContactSummary:
    """Send emails to collected therapists that have not already been contacted."""
    config = load_config(config_path, apply_env_overrides=apply_env_overrides)
    collector = TherapistCollector(config, state_file=state_file, csv_file=csv_file)

    try:
        contactor = TherapistContactor(config)
        if not contactor.is_enabled():
            raise WorkflowError(
                "Contact is not fully configured - set contact.smtp_user, "
                "contact.smtp_password, and contact.from_address in config.yaml"
            )

        therapists = (
            _load_reviewed_csv(collector.csv_path, expected_csv_signature)
            if expected_csv_signature is not None
            else collector.load_csv()
        )
        if not therapists:
            raise WorkflowError("No therapist data found - run with --collect first")

        already_contacted = collector.contacted_emails
        to_contact = [t for t in therapists if t.email and t.email not in already_contacted]

        if not to_contact:
            return ContactSummary(
                to_contact=0,
                already_contacted=len(already_contacted),
                contacted=0,
            )

        contacted = await contactor.contact(to_contact)
        if not contacted:
            raise WorkflowError("No emails were sent successfully.")

        collector.mark_contacted({t.email for t in contacted if t.email})

        return ContactSummary(
            to_contact=len(to_contact),
            already_contacted=len(already_contacted),
            contacted=len(contacted),
        )
    finally:
        await collector.close()


def _load_reviewed_csv(path: Path, expected_signature: str) -> list[TherapistProfile]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        actual_signature = "missing"
        data = b""
    else:
        actual_signature = f"sha256:{hashlib.sha256(data).hexdigest()}"

    if actual_signature != expected_signature:
        raise WorkflowError("CSV changed after review - please review the current CSV again")

    text = data.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    therapists: list[TherapistProfile] = []
    for row in reader:
        therapists.append(TherapistProfile(
            name=row.get("name", ""),
            email=row.get("email") or None,
            therapist_type=row.get("therapist_type", ""),
            website=row.get("website") or None,
            profile_url=row.get("profile_url", ""),
        ))
    return therapists
