"""Service for sending contact emails to therapists."""

from __future__ import annotations

import logging
from email.header import Header
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from doctor_collector.models.therapist import TherapistProfile

if TYPE_CHECKING:
    from doctor_collector.config import AppConfig

logger = logging.getLogger(__name__)

_SMTP_TIMEOUT = 30


class TherapistContactor:
    """Sends personalised contact emails to therapists via SMTP."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def is_enabled(self) -> bool:
        cfg = self._config.contact
        return (
            bool(cfg.smtp_host)
            and bool(cfg.from_address)
            and bool(cfg.smtp_user)
            and bool(cfg.smtp_password)
        )

    async def contact(self, therapists: list[TherapistProfile]) -> list[TherapistProfile]:
        """Send contact emails. Returns the list of successfully contacted therapists."""
        if not therapists:
            logger.info("No therapists to contact")
            return []

        logger.info("Contacting %d therapist(s)", len(therapists))

        try:
            import aiosmtplib
        except ImportError:
            msg = "aiosmtplib is not installed — run: pip install aiosmtplib"
            logger.error(msg)
            raise RuntimeError(msg)

        cfg = self._config.contact
        implicit_tls = cfg.use_tls and cfg.smtp_port == 465
        starttls = cfg.use_tls and not implicit_tls

        successfully_contacted: list[TherapistProfile] = []

        try:
            smtp = aiosmtplib.SMTP(
                hostname=cfg.smtp_host,
                port=cfg.smtp_port,
                use_tls=implicit_tls,
                start_tls=starttls,
                timeout=_SMTP_TIMEOUT,
            )
            await smtp.connect()
            if cfg.smtp_user and cfg.smtp_password:
                await smtp.login(cfg.smtp_user, cfg.smtp_password)

            for therapist in therapists:
                try:
                    message = MIMEText(cfg.body, _charset="utf-8")
                    message["Subject"] = Header(cfg.subject, "utf-8")
                    message["From"] = cfg.from_address
                    message["To"] = therapist.email

                    await smtp.sendmail(
                        cfg.from_address,
                        [therapist.email],
                        message.as_string(),
                    )
                    successfully_contacted.append(therapist)
                    logger.info("Contacted: %s <%s>", therapist.name, therapist.email)
                except Exception:
                    logger.exception(
                        "Failed to contact %s <%s>", therapist.name, therapist.email
                    )

            await smtp.quit()

        except Exception:
            logger.exception("SMTP connection failed to %s:%d", cfg.smtp_host, cfg.smtp_port)

        logger.info(
            "Successfully contacted %d / %d therapist(s)",
            len(successfully_contacted),
            len(therapists),
        )
        return successfully_contacted
