"""CLI entry-point: ``python -m doctor_collector [--collect] [--contact] [--web]``."""

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
        "  python -m doctor_collector --web                  Start local web UI\n"
        "  python -m doctor_collector --collect              Collect only\n"
        "  python -m doctor_collector --contact              Contact only after reviewing CSV\n",
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
    parser.add_argument(
        "--web",
        action="store_true",
        help="Start the local web UI instead of running a CLI collection/contact action.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for --web (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for --web (default: 8000).",
    )
    args = parser.parse_args()

    if args.web:
        from doctor_collector.web import run_web
        from doctor_collector.workflow import WorkflowError

        try:
            run_web(args.config, host=args.host, port=args.port)
        except WorkflowError as exc:
            logger.error(str(exc))
            sys.exit(1)
        return

    if not args.collect and not args.contact:
        parser.print_help()
        print("\nError: specify --web or at least one of --collect or --contact.")
        sys.exit(1)

    if args.collect and args.contact:
        print("Error: collect first, review therapists.csv, then run --contact separately.")
        sys.exit(1)

    asyncio.run(_run(args.config, collect=args.collect, contact=args.contact))


async def _run(config_path: str | None, *, collect: bool, contact: bool) -> None:
    from doctor_collector.workflow import (
        WorkflowError,
        collect_therapists,
        contact_collected_therapists,
    )

    try:
        if collect:
            result = await collect_therapists(config_path, notify=True)
            print(
                f"  Scraped {result.total_profiles_scraped} profiles, "
                f"{result.total_matching} matched your filters."
            )
            if result.csv_saved:
                print(f"  Results saved to {result.saved_to}")
            else:
                print(f"  Existing CSV left unchanged at {result.saved_to}")
            print()

        if contact:
            result = await contact_collected_therapists(config_path)
            if result.to_contact == 0:
                print("  No active, new therapist email addresses to contact.")
            else:
                print(
                    f"  {result.to_contact} therapist(s) to contact "
                    f"({result.already_contacted} already contacted)."
                )
                print(f"  Successfully contacted {result.contacted} therapist(s).")
    except WorkflowError as exc:
        logger.error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
