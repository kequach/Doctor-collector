"""CLI entry-point: ``python -m doctor_collector [--collect] [--contact]``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Doctor Collector — collect therapist data and/or contact them",
        epilog="Examples:\n"
        "  python -m doctor_collector --collect              Collect only\n"
        "  python -m doctor_collector --contact              Contact only (uses previous data)\n"
        "  python -m doctor_collector --collect --contact    Collect then contact\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        help="Scrape therapist profiles from therapie.de, filter, and save to CSV.",
    )
    parser.add_argument(
        "--contact",
        action="store_true",
        help="Send contact emails to collected therapists not yet contacted.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (default: config.yaml in current directory).",
    )
    args = parser.parse_args()

    if not args.collect and not args.contact:
        parser.print_help()
        print("\nError: specify at least one of --collect or --contact.")
        sys.exit(1)

    asyncio.run(_run(args.config, collect=args.collect, contact=args.contact))


async def _run(config_path: str | None, *, collect: bool, contact: bool) -> None:
    from doctor_collector.config import load_config
    from doctor_collector.notifications.console import ConsoleNotifier
    from doctor_collector.services.collector import TherapistCollector
    from doctor_collector.services.contactor import TherapistContactor

    config = load_config(config_path)
    collector = TherapistCollector(config)
    notifier = ConsoleNotifier(enabled=True)

    try:
        if collect:
            if not config.therapie.post_code:
                logger.error("No post_code configured — set therapie.post_code in config.yaml")
                sys.exit(1)

            result = await collector.collect()
            await notifier.send(result.therapists)

            print(
                f"  Scraped {result.total_profiles_scraped} profiles, "
                f"{result.total_matching} matched your filters."
            )
            if result.therapists:
                print(f"  Results saved to {collector.csv_path}")
            print()

        if contact:
            contactor = TherapistContactor(config)
            if not contactor.is_enabled():
                logger.error(
                    "Contact is not fully configured — set contact.smtp_user, "
                    "contact.smtp_password, and contact.from_address in config.yaml"
                )
                sys.exit(1)

            therapists = collector.load_csv()
            if not therapists:
                logger.error("No therapist data found — run with --collect first")
                sys.exit(1)

            already_contacted = collector.contacted_emails
            to_contact = [t for t in therapists if t.email and t.email not in already_contacted]

            if not to_contact:
                print("  All therapists have already been contacted.")
            else:
                already = len(already_contacted)
                print(f"  {len(to_contact)} therapist(s) to contact ({already} already contacted).")
                contacted = await contactor.contact(to_contact)
                contacted_emails = {t.email for t in contacted if t.email}
                collector.mark_contacted(contacted_emails)
                print(f"  Successfully contacted {len(contacted)} therapist(s).")

    finally:
        await collector.close()


if __name__ == "__main__":
    main()
