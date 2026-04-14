"""Console notification channel — prints therapist summaries to stdout."""

from __future__ import annotations

import sys

from doctor_collector.models.therapist import TherapistProfile

_USE_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def _format_therapist(t: TherapistProfile, index: int) -> str:
    header = _c("1", f"  {index}. {t.name}")
    lines = [header]

    if t.therapist_type:
        lines.append(f"     {_c('36', t.therapist_type)}")
    if t.email:
        lines.append(f"     Email:   {_c('32', t.email)}")
    if t.website:
        lines.append(f"     Website: {t.website}")
    if t.profile_url:
        lines.append(f"     Profile: {t.profile_url}")

    return "\n".join(lines)


class ConsoleNotifier:
    """Prints therapist summaries to stdout for preview / dry-run."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled

    def is_enabled(self) -> bool:
        return self._enabled

    async def send(self, therapists: list[TherapistProfile]) -> None:
        if not therapists:
            print("\n  No therapists to display.\n")
            return

        print(_c("1;36", f"\n{'=' * 60}"))
        print(_c("1;36", f"  Doctor Collector — {len(therapists)} therapist(s)"))
        print(_c("1;36", f"{'=' * 60}"))

        for i, t in enumerate(therapists, 1):
            print()
            print(_format_therapist(t, i))

        print()
        print(_c("2", "  https://github.com/kequach/Doctor-collector"))
        print()
