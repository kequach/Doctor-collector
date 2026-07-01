"""Localhost web UI for Doctor Collector."""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import logging
import re
import secrets
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from doctor_collector.config import (
    AppConfig,
    config_to_public_data,
    load_config_from_data,
    load_config_public_data,
    save_config_data,
    save_config_text,
)
from doctor_collector.models.therapist import TherapistProfile
from doctor_collector.services.collector import get_default_csv_path, load_therapists_csv
from doctor_collector.workflow import (
    WorkflowError,
    collect_therapists,
    contact_collected_therapists,
)

logger = logging.getLogger(__name__)

JobProgress = Callable[[str], None]
JobAction = Callable[[JobProgress], dict[str, Any]]
_MAX_JOB_EVENTS = 60
_URL_RE = re.compile(r"https?://\S+")
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PROFILE_EXTRACTED_PREFIX = "Profil ausgelesen:"
_ASSET_VERSION = "20260701-2"
_FAVICON_PATH = Path(__file__).with_name("assets") / "favicon.png"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_event(message: str, level: str = "info") -> dict[str, str]:
    return {
        "message": message,
        "level": level,
        "created_at": _utc_now(),
    }


class _JobProgressLogHandler(logging.Handler):
    def __init__(
        self,
        thread_id: int,
        progress: Callable[[str, str, int | None], None],
    ) -> None:
        super().__init__(logging.INFO)
        self._thread_id = thread_id
        self._progress = progress

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self._thread_id:
            return

        message = _progress_message_from_log(record)
        if message:
            level = "error" if record.levelno >= logging.WARNING else "info"
            self._progress(message, level, _profiles_collected_from_log(record))


def _progress_message_from_log(record: logging.LogRecord) -> str | None:
    if record.levelno < logging.INFO:
        return None

    args = _record_args(record)
    if record.name == "doctor_collector.clients.therapie":
        if record.msg == "Crawling listing page %d: %s" and len(args) >= 1:
            return f"Suchergebnisseite {args[0]} wird geladen."
        if record.msg == "Found %d profiles on page %d" and len(args) >= 2:
            return f"{args[0]} Profil(e) auf Seite {args[1]} gefunden."
        if record.msg == "Extracted: %s" and len(args) >= 1:
            return f"Profil ausgelesen: {_sanitize_progress_message(str(args[0]))}"
        if record.msg == "Crawling complete — %d profiles collected" and len(args) >= 1:
            return f"Suche abgeschlossen: {args[0]} Profil(e) ausgelesen."
        if record.msg == "Skipping profile %s after HTTP %d" and len(args) >= 2:
            try:
                status_code = int(args[1])
            except (TypeError, ValueError):
                status_code = 0
            if status_code == 403:
                return (
                    "Profil konnte nicht geladen werden: therapie.de hat die "
                    "Anfrage abgelehnt (HTTP 403). Bitte später erneut versuchen "
                    "oder die Wartezeit erhöhen."
                )
            return f"Profil konnte nicht geladen werden (HTTP {status_code})."

    if record.name == "doctor_collector.services.collector":
        if record.msg == "Scraped %d total profiles" and len(args) >= 1:
            return f"{args[0]} Profil(e) insgesamt ausgelesen."
        if record.msg == "%d profiles passed filters" and len(args) >= 1:
            return f"{args[0]} Profil(e) passen zu den Filtern."
        if record.msg == "Saved %d therapists to %s" and len(args) >= 1:
            return f"CSV gespeichert: {args[0]} Einträge."
        if record.msg == "Collection did not complete; leaving existing CSV unchanged":
            return "Sammlung unvollständig; vorhandene CSV bleibt unverändert."

    if record.levelno >= logging.WARNING:
        return _sanitize_progress_message(record.getMessage())

    return None


def _profiles_collected_from_log(record: logging.LogRecord) -> int | None:
    if record.name not in {
        "doctor_collector.clients.therapie",
        "doctor_collector.services.collector",
    }:
        return None

    args = _record_args(record)
    if (
        record.msg in {
            "Crawling complete — %d profiles collected",
            "Scraped %d total profiles",
        }
        and len(args) >= 1
    ):
        try:
            return int(args[0])
        except (TypeError, ValueError):
            return None

    return None


def _record_args(record: logging.LogRecord) -> tuple[Any, ...]:
    if isinstance(record.args, tuple):
        return record.args
    if record.args:
        return (record.args,)
    return ()


def _sanitize_progress_message(message: str) -> str:
    without_urls = _URL_RE.sub("[URL]", message)
    return _EMAIL_RE.sub("[E-Mail]", without_urls)


@dataclass
class JobState:
    name: str = "idle"
    status: str = "idle"
    message: str = "Bereit"
    details: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, str]] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": self.details,
            "events": [event.copy() for event in self.events],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobRunner:
    """Runs one collect/contact job at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._state = JobState()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._state.as_dict()

    def start(self, name: str, action: JobAction) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            if self._state.status == "running":
                return False, self._state.as_dict()
            self._stop_event.clear()
            self._state = JobState(
                name=name,
                status="running",
                message=f"{name} gestartet",
                details={"profiles_collected": 0} if name == "Sammeln" else {},
                events=[_job_event(f"{name} gestartet")],
                started_at=_utc_now(),
            )

        thread = threading.Thread(
            target=self._run,
            args=(name, action),
        )
        thread.start()
        return True, self.snapshot()

    def _run(self, name: str, action: JobAction) -> None:
        progress_logger = logging.getLogger("doctor_collector")
        progress_handler = _JobProgressLogHandler(threading.get_ident(), self.progress)
        previous_level = progress_logger.level
        changed_level = progress_logger.getEffectiveLevel() > logging.INFO
        if changed_level:
            progress_logger.setLevel(logging.INFO)
        progress_logger.addHandler(progress_handler)

        try:
            details = action(self.progress)
            message = str(details.pop("message", f"{name} abgeschlossen"))
            status = str(details.pop("status", "succeeded"))
            if status not in {"succeeded", "stopped"}:
                status = "succeeded"
        except WorkflowError as exc:
            details = {}
            message = str(exc)
            status = "failed"
        except Exception as exc:
            logger.exception("Web job failed: %s", name)
            details = {}
            message = f"{type(exc).__name__}: {exc}"
            status = "failed"
        finally:
            progress_logger.removeHandler(progress_handler)
            if changed_level:
                progress_logger.setLevel(previous_level)

        with self._lock:
            self._state.status = status
            self._state.message = message
            self._state.details = details
            self._append_event_locked(message, "error" if status == "failed" else "info")
            self._state.finished_at = _utc_now()

    def request_stop(self, name: str) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            if self._state.status != "running" or self._state.name != name:
                return False, self._state.as_dict()

            self._state.details["stop_requested"] = True
            self._state.message = "Stopp wird angefordert."
            self._stop_event.set()
            self._append_event_locked("Stopp wird angefordert.", "info")
            return True, self._state.as_dict()

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def wait_for_stop(self, timeout: float) -> bool:
        return self._stop_event.wait(max(0.0, timeout))

    def progress(
        self,
        message: str,
        level: str = "info",
        profiles_collected: int | None = None,
    ) -> None:
        if not message:
            return

        with self._lock:
            if self._state.status != "running":
                return
            self._state.message = message
            if profiles_collected is not None:
                self._state.details["profiles_collected"] = profiles_collected
            elif message.startswith(_PROFILE_EXTRACTED_PREFIX):
                current = int(self._state.details.get("profiles_collected", 0))
                self._state.details["profiles_collected"] = current + 1
            self._append_event_locked(message, level)

    def _append_event_locked(self, message: str, level: str) -> None:
        if self._state.events and self._state.events[-1]["message"] == message:
            return
        self._state.events.append(_job_event(message, level))
        if len(self._state.events) > _MAX_JOB_EVENTS:
            del self._state.events[:-_MAX_JOB_EVENTS]


class DoctorCollectorWebApp:
    """Small stdlib HTTP app for local use."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path) if config_path else Path.cwd() / "config.yaml"
        self.jobs = JobRunner()
        self.csrf_token = secrets.token_urlsafe(24)

    def save_config(self, text: str) -> None:
        save_config_text(text, self.config_path)

    def save_config_data(self, data: dict[str, Any]) -> dict[str, Any]:
        save_config_data(data, self.config_path)
        return load_config_public_data(self.config_path)

    def start_collect(
        self,
        *,
        config_data: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        try:
            runtime_config = (
                load_config_from_data(
                    config_data,
                    self.config_path,
                    apply_env_overrides=False,
                )
                if config_data is not None
                else None
            )
        except ValueError as exc:
            raise WorkflowError(f"Konfiguration ist ungueltig: {exc}") from exc

        def _action(progress: JobProgress) -> dict[str, Any]:
            progress("Konfiguration wird geprüft.")
            result = asyncio.run(
                collect_therapists(
                    self.config_path,
                    config=runtime_config,
                    notify=False,
                    apply_env_overrides=False,
                    stop_requested=self.jobs.stop_requested,
                    stop_wait=self.jobs.wait_for_stop,
                )
            )
            stopped = self.jobs.stop_requested()
            if stopped:
                message = "Suche gestoppt; vorhandene CSV blieb unveraendert."
            elif result.csv_saved:
                message = (
                    f"{result.total_matching} passende Einträge aus "
                    f"{result.total_profiles_scraped} Profilen gesammelt."
                )
            else:
                message = (
                    "Sammlung unvollständig; vorhandene CSV blieb unverändert."
                )
            return {
                "status": "stopped" if stopped else "succeeded",
                "message": message,
                "saved_to": str(result.saved_to),
                "csv_saved": result.csv_saved,
                "total_matching": result.total_matching,
                "total_profiles_scraped": result.total_profiles_scraped,
                "profiles_collected": result.total_profiles_scraped,
            }

        return self.jobs.start("Sammeln", _action)

    def stop_collect(self) -> tuple[bool, dict[str, Any]]:
        return self.jobs.request_stop("Sammeln")

    def start_contact(
        self,
        *,
        confirmed: bool,
        reviewed_csv_signature: str | None,
    ) -> tuple[bool, dict[str, Any]]:
        current_signature = _csv_signature(get_default_csv_path())
        if not confirmed or reviewed_csv_signature != current_signature:
            raise WorkflowError("Bitte die aktuelle CSV prüfen, bevor E-Mails gesendet werden.")

        def _action(progress: JobProgress) -> dict[str, Any]:
            progress("E-Mail-Versand wird vorbereitet.")
            result = asyncio.run(
                contact_collected_therapists(
                    self.config_path,
                    apply_env_overrides=False,
                    expected_csv_signature=reviewed_csv_signature,
                )
            )
            if result.to_contact == 0:
                message = "Alle Einträge mit E-Mail wurden bereits kontaktiert."
            else:
                message = f"{result.contacted} E-Mail(s) erfolgreich gesendet."
            return {
                "message": message,
                "to_contact": result.to_contact,
                "already_contacted": result.already_contacted,
                "contacted": result.contacted,
            }

        return self.jobs.start("E-Mails senden", _action)

    def status_payload(self) -> dict[str, Any]:
        return {
            "job": self.jobs.snapshot(),
            "therapists": self._therapists_payload(),
        }

    def therapists_payload(self) -> dict[str, Any]:
        return self._therapists_payload()

    def _therapists_payload(self) -> dict[str, Any]:
        csv_path = get_default_csv_path()
        therapists = load_therapists_csv(csv_path)
        return {
            "csv_path": str(csv_path),
            "csv_signature": _csv_signature(csv_path),
            "count": len(therapists),
            "contactable": sum(1 for t in therapists if t.email),
            "rows": [_therapist_payload(t) for t in therapists],
        }

    def render_index(self) -> str:
        try:
            config_data = load_config_public_data(self.config_path)
            config_error = ""
        except Exception as exc:
            config_data = config_to_public_data(AppConfig())
            config_error = f"{type(exc).__name__}: {exc}"

        payload = self.status_payload()
        therapists = payload["therapists"]
        job = payload["job"]
        return _render_index(
            config_data=config_data,
            config_path=str(self.config_path),
            config_error=config_error,
            csv_path=str(therapists["csv_path"]),
            csv_signature=str(therapists["csv_signature"]),
            therapists=therapists["rows"],
            therapist_count=int(therapists["count"]),
            contactable_count=int(therapists["contactable"]),
            job=job,
            csrf_token=self.csrf_token,
        )

    def handler_class(self) -> type[BaseHTTPRequestHandler]:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if not self._ensure_allowed_host(path):
                    return

                try:
                    if path in {"/", "/index.html"}:
                        self._send_html(app.render_index())
                    elif path in {"/favicon.ico", "/assets/favicon.png"}:
                        self._send_binary(
                            _FAVICON_PATH.read_bytes(),
                            "image/png",
                        )
                    elif path == "/assets/styles.css":
                        self._send_text(_CSS, "text/css; charset=utf-8")
                    elif path == "/assets/app.js":
                        self._send_text(_JS, "application/javascript; charset=utf-8")
                    elif path == "/api/status":
                        self._send_json(app.status_payload())
                    elif path == "/api/therapists":
                        self._send_json(app.therapists_payload())
                    else:
                        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
                except Exception as exc:
                    logger.exception("Web request failed")
                    if path.startswith("/api/"):
                        self._send_json(
                            {"error": f"{type(exc).__name__}: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                        )
                    else:
                        self._send_text(
                            f"Error: {type(exc).__name__}: {exc}",
                            "text/plain; charset=utf-8",
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                        )

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if not self._ensure_allowed_host(path):
                    return

                try:
                    data = self._read_body()
                    header_token_ok = (
                        self.headers.get("X-Doctor-Collector-Token") == app.csrf_token
                    )
                    body_token_ok = data.get("doctor_collector_token") == app.csrf_token
                    if not header_token_ok and not body_token_ok:
                        self._send_json({"error": "Invalid request token"}, HTTPStatus.FORBIDDEN)
                        return

                    if path == "/api/config":
                        if "config_data" in data:
                            config_data = data.get("config_data")
                            if isinstance(config_data, str):
                                config_data = json.loads(config_data)
                            if not isinstance(config_data, dict):
                                raise WorkflowError("Config data must be an object.")
                            saved = app.save_config_data(config_data)
                            self._send_json({
                                "ok": True,
                                "message": "Config saved.",
                                "config_data": saved,
                            })
                        else:
                            config_text = str(data.get("config", ""))
                            app.save_config(config_text)
                            self._send_json({"ok": True, "message": "Config saved."})
                    elif path == "/api/collect":
                        config_data = data.get("config_data")
                        if isinstance(config_data, str):
                            config_data = json.loads(config_data)
                        if config_data is not None and not isinstance(config_data, dict):
                            raise WorkflowError("Config data must be an object.")
                        started, state = app.start_collect(config_data=config_data)
                        status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
                        self._send_json({"ok": started, "job": state}, status)
                    elif path == "/api/collect/stop":
                        stopped, state = app.stop_collect()
                        status = HTTPStatus.ACCEPTED if stopped else HTTPStatus.CONFLICT
                        self._send_json({"ok": stopped, "job": state}, status)
                    elif path == "/api/contact":
                        confirmed = str(data.get("confirm", "")).lower() in {
                            "1",
                            "true",
                            "on",
                            "yes",
                        }
                        reviewed_csv_signature = data.get("csv_signature")
                        started, state = app.start_contact(
                            confirmed=confirmed,
                            reviewed_csv_signature=(
                                str(reviewed_csv_signature)
                                if reviewed_csv_signature is not None
                                else None
                            ),
                        )
                        status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
                        self._send_json({"ok": started, "job": state}, status)
                    else:
                        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
                except WorkflowError as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                except Exception as exc:
                    logger.exception("Web request failed")
                    self._send_json(
                        {"error": f"{type(exc).__name__}: {exc}"},
                        HTTPStatus.BAD_REQUEST,
                    )

            def _read_body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                content_type = self.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    parsed = json.loads(raw or "{}")
                    if isinstance(parsed, dict):
                        return parsed
                    return {}

                parsed_form = parse_qs(raw, keep_blank_values=True)
                return {key: values[-1] for key, values in parsed_form.items()}

            def _ensure_allowed_host(self, path: str) -> bool:
                if _is_allowed_host_header(self.headers.get("Host")):
                    return True

                logger.warning("Rejected web request with invalid Host header")
                self._discard_body()
                if path.startswith("/api/"):
                    self._send_json({"error": "Invalid Host header"}, HTTPStatus.FORBIDDEN)
                else:
                    self._send_text(
                        "Invalid Host header",
                        "text/plain; charset=utf-8",
                        HTTPStatus.FORBIDDEN,
                    )
                return False

            def _discard_body(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length > 0:
                    self.rfile.read(length)

            def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
                self._send_text(body, "text/html; charset=utf-8", status)

            def _send_text(
                self,
                body: str,
                content_type: str,
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self._send_local_response_headers()
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def _send_binary(
                self,
                data: bytes,
                content_type: str,
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self._send_local_response_headers()
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def _send_json(
                self,
                payload: dict[str, Any],
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self._send_local_response_headers()
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def _send_local_response_headers(self) -> None:
                self.send_header("Cache-Control", "no-store")
                self.send_header("Pragma", "no-cache")
                self.send_header("Connection", "close")

            def log_message(self, format: str, *args: Any) -> None:
                logger.info("web: " + format, *args)

        return Handler


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class _ReusableIPv6ThreadingHTTPServer(_ReusableThreadingHTTPServer):
    address_family = socket.AF_INET6


def run_web(
    config_path: str | Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Start the local web UI and block until interrupted."""
    if not _is_loopback_host(host):
        raise WorkflowError("The web UI may only bind to localhost or a loopback address.")

    app = DoctorCollectorWebApp(config_path)
    server_class = _server_class_for_host(host)
    server = server_class((host, port), app.handler_class())
    url = f"http://{_url_host(host)}:{port}/"
    print(f"Doctor Collector web UI running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Doctor Collector web UI.")
    finally:
        server.server_close()


def _therapist_payload(therapist: TherapistProfile) -> dict[str, str]:
    return {
        "name": therapist.name,
        "email": therapist.email or "",
        "therapist_type": therapist.therapist_type,
        "website": therapist.website or "",
        "profile_url": therapist.profile_url,
    }


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_allowed_host_header(host_header: str | None) -> bool:
    host = _host_from_header(host_header)
    return bool(host and _is_loopback_host(host))


def _host_from_header(host_header: str | None) -> str:
    if not host_header:
        return ""

    value = host_header.strip().lower()
    if not value or "," in value:
        return ""

    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return ""
        host = value[1:end]
        port = value[end + 1:]
        if port and not _is_valid_port_suffix(port):
            return ""
        return host.rstrip(".")

    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if not port.isdigit():
            return ""
        value = host

    return value.rstrip(".")


def _is_valid_port_suffix(value: str) -> bool:
    return (
        value.startswith(":")
        and len(value) > 1
        and value[1:].isdigit()
    )


def _server_class_for_host(host: str) -> type[_ReusableThreadingHTTPServer]:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return _ReusableThreadingHTTPServer

    if address.version == 6:
        return _ReusableIPv6ThreadingHTTPServer
    return _ReusableThreadingHTTPServer


def _url_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _csv_signature(path: Path) -> str:
    if not path.exists():
        return "missing"

    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _render_index(
    *,
    config_data: dict[str, Any],
    config_path: str,
    config_error: str,
    csv_path: str,
    csv_signature: str,
    therapists: list[dict[str, str]],
    therapist_count: int,
    contactable_count: int,
    job: dict[str, Any],
    csrf_token: str,
) -> str:
    rows = "\n".join(_render_row(row) for row in therapists)
    if not rows:
        rows = (
            '<tr class="empty-row"><td colspan="5">'
            "Noch keine gesammelten Einträge."
            "</td></tr>"
        )

    config_status = config_error or "Geladen"
    copy_disabled = " disabled" if contactable_count == 0 else ""
    config_json = json.dumps(config_data, ensure_ascii=False).replace("</", "<\\/")
    job_json = json.dumps(job, ensure_ascii=False).replace("</", "<\\/")
    job_events = _render_job_events(job)
    job_profile_count = _job_profile_count(job)
    progress_hidden = "" if job.get("status") == "running" else " hidden"
    progress_aria_hidden = "false" if job.get("status") == "running" else "true"
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="doctor-collector-token" content="{_escape(csrf_token)}">
  <title>Doctor Collector</title>
  <link rel="icon" type="image/png" href="/assets/favicon.png?v={_ASSET_VERSION}">
  <link rel="stylesheet" href="/assets/styles.css?v={_ASSET_VERSION}">
</head>
<body>
  <header class="topbar">
    <div>
      <h1>Doctor Collector</h1>
      <p>Lokale Steuerung auf diesem Computer</p>
    </div>
    <span class="badge">nur lokal</span>
  </header>
  <main class="shell">
    <section class="panel config-panel">
      <form id="config-form" class="settings-form" autocomplete="off">
        <div class="section-heading">
          <div>
            <h2>Einstellungen</h2>
            <p>{_escape(config_path)}</p>
          </div>
          <span id="config-status">{_escape(config_status)}</span>
        </div>

        <div class="settings-section">
          <div class="settings-header">Suche</div>
          <div class="settings-body">
            <div class="field-grid">
              <label class="field">
                <span>Postleitzahl</span>
                <input id="therapie-post-code" type="text" inputmode="numeric">
              </label>
              <label class="field">
                <span>Suchradius</span>
                <select id="therapie-search-radius-km">
                  <option value="10">10 km</option>
                  <option value="25">25 km</option>
                  <option value="50">50 km</option>
                  <option value="100">100 km</option>
                </select>
              </label>
              <label class="field">
                <span>Therapieform</span>
                <select id="therapie-therapy-form">
                  <option value="1">Einzeltherapie</option>
                  <option value="2">Gruppentherapie</option>
                  <option value="3">Paar-/Familientherapie</option>
                </select>
              </label>
              <label class="field">
                <span>Therapieverfahren</span>
                <select id="therapie-therapy-type">
                  <option value="1">Analytische Psychotherapie</option>
                  <option value="2">Verhaltenstherapie</option>
                  <option value="3">Tiefenpsychologisch fundierte Psychotherapie</option>
                  <option value="4">Systemische Therapie</option>
                </select>
              </label>
            </div>
            <details class="advanced-settings">
              <summary>Erweiterte Einstellungen</summary>
              <div class="field-grid">
                <label class="field">
                  <span>Startseite</span>
                  <input id="therapie-start-page" type="number" min="1" step="1">
                </label>
                <label class="field">
                  <span>Maximale Seiten</span>
                  <input id="therapie-max-pages" type="number" min="1" step="1">
                </label>
                <label class="field">
                  <span>Maximale Therapeut:innen</span>
                  <input id="therapie-max-therapists" type="number" min="0" step="1">
                  <small>0 = kein Limit.</small>
                </label>
                <label class="field">
                  <span>Wartezeit pro Anfrage (Sekunden)</span>
                  <input id="therapie-request-delay-seconds" type="number" min="0.1" step="0.1">
                </label>
              </div>
            </details>
          </div>
        </div>

        <div class="settings-section">
          <div class="settings-header">Filter</div>
          <div class="settings-body">
            <label class="field">
              <span>Ausschlusswort für Typ</span>
              <div class="add-row">
                <input id="filter-exclude-add" type="text">
                <button id="filter-exclude-add-button" type="button">Hinzufügen</button>
              </div>
            </label>
            <div id="filter-exclude-chips" class="chip-wrap"></div>
          </div>
        </div>

        <div class="form-actions">
          <button id="save-config" class="primary" type="submit">Konfiguration speichern</button>
        </div>
      </form>
    </section>
    <aside class="side">
      <section class="panel">
        <h2>Schritte</h2>
        <ol class="steps">
          <li>Einstellungen ausfüllen.</li>
          <li>Optional: Konfiguration speichern.</li>
          <li>Sammeln starten.</li>
          <li>Tabelle oder CSV-Datei prüfen.</li>
          <li>Optional: E-Mail-Adressen kopieren oder Web-Versand nutzen.</li>
        </ol>
      </section>
      <section class="panel">
        <h2>Lokal & privat</h2>
        <p class="note">
          Keine Daten oder Zugangsdaten werden hochgeladen. Alles läuft lokal
          auf diesem Computer unter localhost.
        </p>
      </section>
      <section class="panel">
        <h2>Ausführen</h2>
        <div class="stack">
          <button id="collect-button" class="primary" type="button">
            Sammeln
          </button>
          <button id="stop-collect-button" class="danger" type="button" hidden>
            Suche stoppen
          </button>
        </div>
      </section>
      <section class="panel">
        <h2>Aktivität</h2>
        <p id="job-status">{_escape(job["message"])}</p>
        <div
          id="job-progress"
          class="job-progress"
          aria-hidden="{progress_aria_hidden}"{progress_hidden}
        >
          <div class="progress-track">
            <div id="job-progress-bar" class="progress-bar"></div>
          </div>
        </div>
        <ol id="job-events" class="job-events" aria-live="polite">
          {job_events}
        </ol>
        <dl>
          <div>
            <dt>Status</dt>
            <dd id="job-state">{_escape(job["status"])}</dd>
          </div>
          <div>
            <dt>Profile ausgelesen</dt>
            <dd id="job-profile-count">{job_profile_count}</dd>
          </div>
          <div>
            <dt>Gespeicherte CSV-Datei</dt>
            <dd id="csv-path">{_escape(csv_path)}</dd>
          </div>
        </dl>
      </section>
    </aside>
    <section class="panel table-panel">
      <div class="section-heading">
        <div>
          <h2>Gesammelte Einträge</h2>
          <p id="table-count" data-csv-signature="{_escape(csv_signature)}">
            {therapist_count} Einträge, {contactable_count} mit E-Mail
          </p>
        </div>
        <div class="section-actions">
          <button id="copy-emails-button" type="button"{copy_disabled}>
            E-Mail-Adressen kopieren
          </button>
          <button id="refresh-button" type="button">Aktualisieren</button>
        </div>
      </div>
      <p class="note table-note">
        Die gesammelten Daten werden in der oben angezeigten CSV-Datei gespeichert.
        Kopieren fügt alle E-Mail-Adressen kommagetrennt in die Zwischenablage.
      </p>
      <p id="copy-emails-status" class="copy-status" aria-live="polite"></p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>E-Mail</th>
              <th>Typ</th>
              <th>Website</th>
              <th>Profil</th>
            </tr>
          </thead>
          <tbody id="therapist-rows">
            {rows}
          </tbody>
        </table>
      </div>
      <div class="email-settings">
        <div class="settings-section">
          <div class="settings-header">E-Mail-Text (optional)</div>
          <div class="settings-body">
            <label class="field">
              <span>Betreff</span>
              <input id="contact-subject" type="text">
            </label>
            <label class="field">
              <span>Nachricht</span>
              <textarea id="contact-body" rows="12"></textarea>
            </label>
          </div>
        </div>

        <div class="settings-section">
          <div class="settings-header">SMTP für Web-Versand (optional)</div>
          <div class="settings-body">
            <div class="field-grid">
              <label class="field">
                <span>SMTP-Server</span>
                <input id="contact-smtp-host" type="text">
              </label>
              <label class="field">
                <span>SMTP-Port</span>
                <input id="contact-smtp-port" type="number" min="1" max="65535" step="1">
              </label>
              <label class="field">
                <span>SMTP-Benutzer</span>
                <input id="contact-smtp-user" type="text">
              </label>
              <label class="field">
                <span>SMTP-Passwort</span>
                <input id="contact-smtp-password" type="password">
              </label>
              <label class="field">
                <span>Absenderadresse</span>
                <input id="contact-from-address" type="email">
              </label>
              <label class="toggle-field">
                <span>
                  <strong>TLS verwenden</strong>
                </span>
                <input id="contact-use-tls" type="checkbox">
              </label>
            </div>
          </div>
        </div>

        <div class="settings-section">
          <div class="settings-header">E-Mails senden</div>
          <div class="settings-body">
            <div class="stack">
              <label class="checkline">
                <input id="confirm-contact" type="checkbox">
                CSV geprüft?
              </label>
              <button id="contact-button" class="danger" type="button" disabled>
                E-Mails senden (optional)
              </button>
              <p class="note">
                Der Web-Versand ist optional. Alternativ kannst du die
                E-Mail-Adressen kopieren und mit deinem eigenen Mailprogramm senden.
              </p>
            </div>
          </div>
        </div>
      </div>
    </section>
  </main>
  <div id="toast-region" class="toast-region" aria-live="polite" aria-atomic="false"></div>
  <script type="application/json" id="config-data">{config_json}</script>
  <script type="application/json" id="job-data">{job_json}</script>
  <script src="/assets/app.js?v={_ASSET_VERSION}"></script>
</body>
</html>
"""


def _job_profile_count(job: dict[str, Any]) -> int:
    details = job.get("details")
    if not isinstance(details, dict):
        return 0

    count = details.get("profiles_collected", 0)
    try:
        return max(0, int(count))
    except (TypeError, ValueError):
        return 0


def _render_job_events(job: dict[str, Any]) -> str:
    events = job.get("events")
    if not isinstance(events, list) or not events:
        return '<li class="job-event empty">Noch keine Aktivität.</li>'

    rendered: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if not message:
            continue
        level = "error" if event.get("level") == "error" else "info"
        rendered.append(f'<li class="job-event is-{level}">{_escape(message)}</li>')

    if not rendered:
        return '<li class="job-event empty">Noch keine Aktivität.</li>'
    return "\n          ".join(rendered)


def _render_row(row: dict[str, str]) -> str:
    return f"""<tr>
  <td>{_escape(row["name"])}</td>
  <td>{_escape(row["email"])}</td>
  <td>{_escape(row["therapist_type"])}</td>
  <td>{_link_cell(row["website"])}</td>
  <td>{_link_cell(row["profile_url"])}</td>
</tr>"""


def _link_cell(value: str) -> str:
    if not value:
        return ""
    if not _is_safe_http_url(value):
        return _escape(value)

    escaped = _escape(value)
    return f'<a href="{escaped}" target="_blank" rel="noreferrer">Öffnen</a>'


def _is_safe_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


_CSS = """
:root {
  color-scheme: light;
  --bg: #f5f7f8;
  --panel: #ffffff;
  --ink: #202528;
  --muted: #5f6b73;
  --line: #d7dee2;
  --primary: #166b5f;
  --primary-strong: #0f4f47;
  --danger: #a13d33;
  --danger-strong: #7d2e26;
}

* {
  box-sizing: border-box;
}

html {
  overflow-x: hidden;
}

body {
  margin: 0;
  min-height: 100vh;
  overflow-x: hidden;
  background: var(--bg);
  color: var(--ink);
  font-family: Arial, Helvetica, sans-serif;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 24px;
  border-bottom: 1px solid var(--line);
  background: #ffffff;
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  font-size: 24px;
  font-weight: 700;
}

h2 {
  font-size: 16px;
  font-weight: 700;
}

p,
dd,
dt,
label,
button,
input,
select,
textarea,
td,
th {
  font-size: 14px;
}

.topbar p,
.section-heading p,
.note,
.copy-status,
dt {
  color: var(--muted);
}

.badge {
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 6px 10px;
  color: var(--muted);
  background: #f9fbfb;
}

.shell {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  padding: 16px;
}

.panel {
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}

.config-panel,
.table-panel,
.side .panel {
  padding: 16px;
}

.side {
  display: grid;
  gap: 16px;
  align-content: start;
}

.steps {
  display: grid;
  gap: 7px;
  margin: 12px 0 0;
  padding-left: 20px;
  color: var(--muted);
}

.steps li {
  padding-left: 2px;
}

.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.section-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
}

.note {
  line-height: 1.45;
}

.table-note {
  margin: -4px 0 12px;
}

.copy-status {
  min-height: 20px;
  margin: -4px 0 8px;
}

.toast-region {
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 20;
  display: grid;
  gap: 8px;
  width: min(360px, calc(100vw - 32px));
  pointer-events: none;
}

.toast {
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  box-shadow: 0 10px 24px rgba(32, 37, 40, 0.16);
  line-height: 1.4;
  background: #ffffff;
  opacity: 0;
  transform: translateY(10px);
  animation: toast-in 180ms ease-out forwards;
  pointer-events: auto;
}

.toast-success {
  border-color: #9ac7bd;
  color: #0f4f47;
  background: #f0faf7;
}

.toast-error {
  border-color: #d9a098;
  color: #7d2e26;
  background: #fff5f3;
}

.toast.is-hiding {
  animation: toast-out 160ms ease-in forwards;
}

.job-progress {
  margin: 12px 0 0;
}

.job-progress[hidden] {
  display: none;
}

.progress-track {
  height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: #e7ecef;
}

.progress-bar {
  width: 38%;
  height: 100%;
  border-radius: inherit;
  background: var(--primary);
  animation: progress-slide 1.1s linear infinite;
}

.job-events {
  display: grid;
  gap: 6px;
  max-height: 220px;
  margin: 12px 0 0;
  padding: 0;
  overflow-y: auto;
  list-style: none;
}

.job-event {
  min-width: 0;
  padding: 8px 10px;
  border-left: 3px solid var(--line);
  border-radius: 4px;
  background: #f9fbfb;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.job-event.empty {
  color: var(--muted);
}

.job-event.is-error {
  border-left-color: var(--danger);
}

@keyframes progress-slide {
  from {
    transform: translateX(-120%);
  }

  to {
    transform: translateX(320%);
  }
}

@keyframes toast-in {
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes toast-out {
  to {
    opacity: 0;
    transform: translateY(8px);
  }
}

#config-status,
#job-state {
  color: var(--muted);
}

input,
select,
textarea {
  display: block;
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 9px 10px;
  font-family: inherit;
  line-height: 1.45;
  color: var(--ink);
  background: #fcfdfd;
}

textarea {
  min-height: 160px;
  resize: vertical;
}

input:focus,
select:focus,
textarea:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 2px rgba(22, 107, 95, 0.14);
  outline: none;
}

.settings-form {
  display: grid;
  gap: 14px;
}

.settings-section {
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 6px;
}

.settings-header {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  color: var(--primary);
  background: #f9fbfb;
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
}

.settings-body {
  display: grid;
  gap: 14px;
  padding: 14px;
}

.advanced-settings {
  display: grid;
  gap: 12px;
  padding-top: 2px;
}

.advanced-settings summary {
  width: fit-content;
  color: var(--primary);
  font-weight: 700;
  cursor: pointer;
}

.advanced-settings .field-grid {
  margin-top: 12px;
}

.field-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.field {
  display: grid;
  gap: 5px;
  min-width: 0;
  font-weight: 600;
}

.field span,
.toggle-field span {
  color: var(--ink);
}

.field small {
  color: var(--muted);
  font-weight: 400;
  line-height: 1.35;
}

.toggle-field {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 38px;
  font-weight: 600;
}

.toggle-field input {
  width: auto;
  flex: 0 0 auto;
}

.stack {
  display: grid;
  gap: 10px;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
}

button {
  min-height: 38px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0 14px;
  color: var(--ink);
  background: #ffffff;
  cursor: pointer;
}

button:hover:not(:disabled) {
  border-color: #9eacb3;
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

button[hidden] {
  display: none;
}

button.primary {
  color: #ffffff;
  border-color: var(--primary);
  background: var(--primary);
}

button.primary:hover:not(:disabled) {
  background: var(--primary-strong);
}

button.danger {
  color: #ffffff;
  border-color: var(--danger);
  background: var(--danger);
}

button.danger:hover:not(:disabled) {
  background: var(--danger-strong);
}

.checkline {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 34px;
}

.checkline input {
  width: auto;
  flex: 0 0 auto;
  margin: 0;
}

.add-row {
  display: flex;
  gap: 8px;
}

.add-row input {
  min-width: 0;
}

.add-row button {
  flex: 0 0 auto;
}

.chip-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-height: 28px;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 28px;
  padding: 3px 9px;
  border: 1px solid var(--primary);
  border-radius: 4px;
  color: var(--primary);
  background: #f3faf8;
  font-weight: 600;
}

.chip button {
  min-height: 0;
  border: 0;
  padding: 0 2px;
  color: inherit;
  background: transparent;
}

dl {
  display: grid;
  gap: 10px;
  margin: 14px 0 0;
}

dl div {
  min-width: 0;
}

dt {
  margin-bottom: 2px;
}

dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.table-panel {
  grid-column: 1 / -1;
  overflow: hidden;
}

.table-wrap {
  max-width: 100%;
  min-width: 0;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 6px;
}

.email-settings {
  display: grid;
  gap: 14px;
  margin-top: 16px;
}

table {
  width: 100%;
  min-width: 860px;
  border-collapse: collapse;
}

th,
td {
  padding: 10px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}

th {
  color: var(--muted);
  background: #f9fbfb;
  font-weight: 700;
}

td {
  overflow-wrap: anywhere;
}

.empty-row td {
  color: var(--muted);
}

a {
  color: #0f5f91;
}

@media (max-width: 900px) {
  .shell {
    grid-template-columns: 1fr;
  }

  .field-grid {
    grid-template-columns: 1fr;
  }

  .topbar {
    align-items: flex-start;
    flex-direction: column;
  }
}

@media (prefers-reduced-motion: reduce) {
  .toast,
  .toast.is-hiding {
    animation: none;
    opacity: 1;
    transform: none;
  }
}
"""


_JS = """
const canonicalLocalUrl = canonicalLocalhostUrl();
if (canonicalLocalUrl) {
  window.location.replace(canonicalLocalUrl);
}

const configForm = document.querySelector("#config-form");
const saveButton = document.querySelector("#save-config");
const collectButton = document.querySelector("#collect-button");
const stopCollectButton = document.querySelector("#stop-collect-button");
const contactButton = document.querySelector("#contact-button");
const confirmContact = document.querySelector("#confirm-contact");
const refreshButton = document.querySelector("#refresh-button");
const copyEmailsButton = document.querySelector("#copy-emails-button");
const copyEmailsStatus = document.querySelector("#copy-emails-status");
const configStatus = document.querySelector("#config-status");
const jobStatus = document.querySelector("#job-status");
const jobState = document.querySelector("#job-state");
const jobProgress = document.querySelector("#job-progress");
const jobProgressBar = document.querySelector("#job-progress-bar");
const jobEvents = document.querySelector("#job-events");
const jobProfileCount = document.querySelector("#job-profile-count");
const tableCount = document.querySelector("#table-count");
const rowBody = document.querySelector("#therapist-rows");
const csvPath = document.querySelector("#csv-path");
const toastRegion = document.querySelector("#toast-region");
const csrfToken = document.querySelector('meta[name="doctor-collector-token"]').content;

let configData = JSON.parse(document.querySelector("#config-data").textContent);
let initialJob = JSON.parse(document.querySelector("#job-data").textContent);
let latestJob = initialJob;
let excludeTypes = [];
let pollTimer = null;
let currentCsvSignature = tableCount.dataset.csvSignature || null;
let reviewedCsvSignature = null;
let frameRequestCounter = 0;
let followJobEvents = true;
const JOB_EVENTS_BOTTOM_TOLERANCE = 8;

function canonicalLocalhostUrl() {
  const host = window.location.hostname;
  if (host !== "localhost" && host !== "::1" && host !== "[::1]") {
    return null;
  }

  const port = window.location.port ? `:${window.location.port}` : "";
  return `http://127.0.0.1${port}${window.location.pathname}${window.location.search}`;
}

function byId(id) {
  return document.getElementById(id);
}

function value(id) {
  return byId(id).value;
}

function numberValue(id, fallback) {
  const parsed = Number(value(id));
  return Number.isFinite(parsed) ? parsed : fallback;
}

function integerValue(id, fallback) {
  const parsed = parseInt(value(id), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function setValue(id, val) {
  byId(id).value = val ?? "";
}

function setChecked(id, checked) {
  byId(id).checked = Boolean(checked);
}

function showToast(message, variant = "success") {
  if (!message) {
    return;
  }

  const tone = variant === "error" ? "error" : "success";
  const toast = document.createElement("div");
  toast.className = `toast toast-${tone}`;
  toast.setAttribute("role", tone === "error" ? "alert" : "status");
  toast.textContent = message;
  toastRegion.append(toast);

  window.setTimeout(() => {
    toast.classList.add("is-hiding");
    window.setTimeout(() => toast.remove(), 180);
  }, 4200);
}

function showErrorToast(prefix, error) {
  const reason = error?.message || "Unbekannter Fehler";
  showToast(`${prefix}: ${reason}`, "error");
}

function setBusy(isBusy) {
  const runningCollect = isBusy && latestJob?.name === "Sammeln";
  const stopRequested = Boolean(latestJob?.details?.stop_requested);
  saveButton.disabled = isBusy;
  collectButton.disabled = isBusy;
  stopCollectButton.hidden = !runningCollect;
  stopCollectButton.disabled = !runningCollect || stopRequested;
  contactButton.disabled = isBusy || !confirmContact.checked;
}

function clearCsvReview() {
  confirmContact.checked = false;
  reviewedCsvSignature = null;
  contactButton.disabled = true;
}

function populateConfig(cfg) {
  const therapie = cfg.therapie || {};
  const filters = cfg.filters || {};
  const contact = cfg.contact || {};

  setValue("therapie-post-code", therapie.post_code);
  setValue("therapie-search-radius-km", therapie.search_radius_km ?? 10);
  setValue("therapie-therapy-form", therapie.therapy_form ?? 1);
  setValue("therapie-therapy-type", therapie.therapy_type ?? 2);
  setValue("therapie-start-page", therapie.start_page ?? 1);
  setValue("therapie-max-pages", therapie.max_pages ?? 100);
  setValue("therapie-max-therapists", therapie.max_therapists ?? 0);
  setValue("therapie-request-delay-seconds", therapie.request_delay_seconds ?? 1.5);

  excludeTypes = Array.isArray(filters.exclude_types)
    ? filters.exclude_types.slice()
    : [];
  renderExcludeChips();

  setValue("contact-subject", contact.subject);
  setValue("contact-body", contact.body);
  setValue("contact-smtp-host", contact.smtp_host);
  setValue("contact-smtp-port", contact.smtp_port ?? 465);
  setValue("contact-smtp-user", contact.smtp_user);
  setValue("contact-smtp-password", contact.smtp_password);
  setValue("contact-from-address", contact.from_address);
  setChecked("contact-use-tls", contact.use_tls !== false);
}

function collectConfig() {
  return {
    therapie: {
      post_code: value("therapie-post-code").trim(),
      search_radius_km: integerValue("therapie-search-radius-km", 10),
      therapy_form: integerValue("therapie-therapy-form", 1),
      therapy_type: integerValue("therapie-therapy-type", 2),
      start_page: integerValue("therapie-start-page", 1),
      max_pages: integerValue("therapie-max-pages", 100),
      max_therapists: integerValue("therapie-max-therapists", 0),
      request_delay_seconds: numberValue("therapie-request-delay-seconds", 1.5),
    },
    filters: {
      exclude_types: excludeTypes.slice(),
    },
    contact: {
      subject: value("contact-subject"),
      body: value("contact-body"),
      smtp_host: value("contact-smtp-host"),
      smtp_port: integerValue("contact-smtp-port", 465),
      use_tls: byId("contact-use-tls").checked,
      smtp_user: value("contact-smtp-user"),
      smtp_password: value("contact-smtp-password"),
      from_address: value("contact-from-address"),
    },
  };
}

function renderExcludeChips() {
  const wrap = byId("filter-exclude-chips");
  wrap.replaceChildren();

  for (const [index, keyword] of excludeTypes.entries()) {
    const chip = document.createElement("span");
    chip.className = "chip";
    const text = document.createElement("span");
    text.textContent = keyword;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "x";
    remove.setAttribute("aria-label", `${keyword} entfernen`);
    remove.addEventListener("click", () => {
      excludeTypes.splice(index, 1);
      renderExcludeChips();
    });
    chip.append(text, remove);
    wrap.append(chip);
  }
}

function addExcludeType(notify = false) {
  const input = byId("filter-exclude-add");
  const keyword = input.value.trim();
  if (!keyword) {
    if (notify) {
      showToast("Ausschlusswort fehlt: Bitte Text eingeben", "error");
    }
    return;
  }
  if (!excludeTypes.some((existing) => existing.toLowerCase() === keyword.toLowerCase())) {
    excludeTypes.push(keyword);
    renderExcludeChips();
    if (notify) {
      showToast("Ausschlusswort hinzugefügt");
    }
  } else if (notify) {
    showToast("Ausschlusswort ist bereits vorhanden", "error");
  }
  input.value = "";
}

function profilesCollected(job) {
  const count = Number(job?.details?.profiles_collected ?? 0);
  return Number.isFinite(count) && count > 0 ? count : 0;
}

function setJob(job) {
  const previousStatus = latestJob?.status;
  latestJob = job || {};
  jobStatus.textContent = job.message || "";
  jobState.textContent = statusLabel(job.status || "");
  jobProfileCount.textContent = profilesCollected(job);
  const running = job.status === "running";
  jobProgress.hidden = !running;
  jobProgress.setAttribute("aria-hidden", running ? "false" : "true");
  jobProgressBar.classList.toggle("is-running", running);
  setJobEvents(job.events || []);
  setBusy(running);
  notifyJobTransition(previousStatus, latestJob);
}

function setJobEvents(events) {
  const shouldFollow = followJobEvents || isJobEventsAtBottom();
  const previousScrollTop = jobEvents.scrollTop;
  jobEvents.replaceChildren();
  if (!Array.isArray(events) || !events.length) {
    const item = document.createElement("li");
    item.className = "job-event empty";
    item.textContent = "Noch keine Aktivität.";
    jobEvents.append(item);
    settleJobEventsScroll(shouldFollow, previousScrollTop);
    return;
  }

  for (const event of events) {
    const message = event?.message || "";
    if (!message) {
      continue;
    }
    const item = document.createElement("li");
    item.className = event.level === "error"
      ? "job-event is-error"
      : "job-event is-info";
    item.textContent = message;
    jobEvents.append(item);
  }
  settleJobEventsScroll(shouldFollow, previousScrollTop);
}

function settleJobEventsScroll(shouldFollow, previousScrollTop) {
  if (shouldFollow) {
    scrollJobEventsToBottom();
  } else {
    jobEvents.scrollTop = previousScrollTop;
    updateJobEventsFollowMode();
  }
}

function isJobEventsAtBottom() {
  const distance = jobEvents.scrollHeight - jobEvents.scrollTop - jobEvents.clientHeight;
  return distance <= JOB_EVENTS_BOTTOM_TOLERANCE;
}

function scrollJobEventsToBottom() {
  jobEvents.scrollTop = jobEvents.scrollHeight;
  followJobEvents = true;
}

function updateJobEventsFollowMode() {
  followJobEvents = isJobEventsAtBottom();
}

function statusLabel(status) {
  const labels = {
    idle: "bereit",
    running: "läuft",
    succeeded: "erfolgreich",
    failed: "fehlgeschlagen",
    stopped: "gestoppt",
  };
  return labels[status] || status;
}

function notifyJobTransition(previousStatus, job) {
  if (previousStatus !== "running" || job.status === "running") {
    return;
  }

  const name = job.name || "Aktion";
  if (job.status === "succeeded") {
    showToast(`${name} erfolgreich abgeschlossen`);
  } else if (job.status === "failed") {
    showToast(`${name} fehlgeschlagen: ${job.message || "Unbekannter Fehler"}`, "error");
  } else if (job.status === "stopped") {
    showToast(`${name} gestoppt`);
  }
}

function collectedEmails() {
  const emails = [];
  for (const row of rowBody.querySelectorAll("tr")) {
    const cells = row.querySelectorAll("td");
    if (cells.length < 2) {
      continue;
    }
    const email = cells[1].textContent.trim();
    if (email) {
      emails.push(email);
    }
  }
  return Array.from(new Set(emails));
}

function updateCopyButton() {
  copyEmailsButton.disabled = collectedEmails().length === 0;
}

function setRows(payload) {
  if (currentCsvSignature && currentCsvSignature !== payload.csv_signature) {
    clearCsvReview();
  }
  currentCsvSignature = payload.csv_signature;

  tableCount.textContent = `${payload.count} Einträge, ${payload.contactable} mit E-Mail`;
  csvPath.textContent = payload.csv_path;
  rowBody.replaceChildren();
  copyEmailsStatus.textContent = "";

  if (!payload.rows.length) {
    const tr = document.createElement("tr");
    tr.className = "empty-row";
    const td = document.createElement("td");
    td.colSpan = 5;
    td.textContent = "Noch keine gesammelten Einträge.";
    tr.append(td);
    rowBody.append(tr);
    updateCopyButton();
    return;
  }

  for (const row of payload.rows) {
    const tr = document.createElement("tr");
    addCell(tr, row.name);
    addCell(tr, row.email);
    addCell(tr, row.therapist_type);
    addLinkCell(tr, row.website);
    addLinkCell(tr, row.profile_url);
    rowBody.append(tr);
  }
  updateCopyButton();
}

function addCell(tr, val) {
  const td = document.createElement("td");
  td.textContent = val || "";
  tr.append(td);
}

function addLinkCell(tr, val) {
  const td = document.createElement("td");
  if (isSafeHttpUrl(val)) {
    const link = document.createElement("a");
    link.href = val;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = "Öffnen";
    td.append(link);
  } else if (val) {
    td.textContent = val;
  }
  tr.append(td);
}

function isSafeHttpUrl(val) {
  if (!val) {
    return false;
  }
  try {
    const url = new URL(val);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

async function postJson(url, payload = {}) {
  const response = await safeFetch(url, {
    method: "POST",
    body: formBody(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || data.job?.message || "Anfrage fehlgeschlagen");
  }
  return data;
}

function formBody(payload = {}) {
  const form = new URLSearchParams();
  form.set("doctor_collector_token", csrfToken);
  for (const [key, value] of Object.entries(payload)) {
    if (value === undefined || value === null) {
      continue;
    }
    form.set(key, typeof value === "object" ? JSON.stringify(value) : String(value));
  }
  return form.toString();
}

async function safeFetch(url, options = {}) {
  const targetUrl = absoluteUrl(url);
  try {
    if (typeof fetch === "function") {
      return await fetch(url, { cache: "no-store", ...options });
    }
    return await requestWithFrame(url, options);
  } catch (error) {
    try {
      return await requestWithFrame(url, options);
    } catch (frameError) {
      throw new Error(localRequestErrorMessage(frameError, targetUrl));
    }
  }
}

function absoluteUrl(url) {
  try {
    return new URL(url, window.location.href).href;
  } catch {
    return String(url);
  }
}

function requestWithFrame(url, options = {}) {
  return new Promise((resolve, reject) => {
    const method = (options.method || "GET").toUpperCase();
    const iframe = document.createElement("iframe");
    const frameName = `doctor-collector-frame-${++frameRequestCounter}`;
    let form = null;
    let settled = false;

    iframe.name = frameName;
    iframe.hidden = true;
    iframe.style.display = "none";

    const cleanup = () => {
      window.clearTimeout(timer);
      form?.remove();
      iframe.remove();
    };

    const finish = (callback) => {
      if (settled) {
        return;
      }
      settled = true;
      try {
        callback();
      } finally {
        cleanup();
      }
    };

    const timer = window.setTimeout(() => {
      finish(() => reject(new Error(`NetworkError: ${method} ${absoluteUrl(url)} timed out`)));
    }, 30000);

    iframe.addEventListener("load", () => {
      const doc = iframe.contentDocument;
      if (!doc || doc.location.href === "about:blank") {
        return;
      }

      finish(() => {
        const text = doc.body?.textContent?.trim() || "{}";
        let data;
        try {
          data = JSON.parse(text);
        } catch (error) {
          reject(new Error(`NetworkError: ${method} ${absoluteUrl(url)} invalid response`));
          return;
        }
        resolve({
          ok: !data.error && data.ok !== false,
          status: data.error ? 400 : 200,
          json: async () => data,
          text: async () => text,
        });
      });
    });

    iframe.addEventListener("error", () => {
      finish(() => reject(new Error(`NetworkError: ${method} ${absoluteUrl(url)} frame error`)));
    });

    document.body.append(iframe);

    if (method === "GET") {
      iframe.src = url;
      return;
    }

    form = document.createElement("form");
    form.method = method;
    form.action = url;
    form.target = frameName;
    form.hidden = true;
    form.style.display = "none";

    const params = new URLSearchParams(options.body || "");
    for (const [key, value] of params.entries()) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = key;
      input.value = value;
      form.append(input);
    }

    document.body.append(form);
    form.submit();
  });
}

function localRequestErrorMessage(error, targetUrl = "") {
  const message = error?.message || "Anfrage fehlgeschlagen";
  if (
    message.includes("NetworkError")
    || message.includes("Failed to fetch")
    || message.includes("Load failed")
  ) {
    const target = targetUrl ? ` Ziel: ${targetUrl}.` : "";
    return `Verbindung zur lokalen Web-UI fehlgeschlagen.${target} Laeuft der Web-Server noch?`;
  }
  return message;
}

async function writeClipboard(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.left = "-9999px";
  document.body.append(area);
  area.select();
  const copied = document.execCommand("copy");
  area.remove();
  if (!copied) {
    throw new Error("Kopieren nicht möglich");
  }
}

async function refreshStatus(options = {}) {
  const notify = Boolean(options.notify);
  try {
    const response = await safeFetch("/api/status");
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Status konnte nicht geladen werden");
    }
    setJob(data.job);
    setRows(data.therapists);

    const running = data.job.status === "running";
    setBusy(running);
    if (!running && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (notify) {
      showToast("Daten erfolgreich aktualisiert");
    }
  } catch (error) {
    jobStatus.textContent = error.message;
    setBusy(false);
    clearInterval(pollTimer);
    pollTimer = null;
    if (notify) {
      showErrorToast("Daten konnten nicht aktualisiert werden", error);
    }
  }
}

function startPolling() {
  if (!pollTimer) {
    pollTimer = setInterval(refreshStatus, 1200);
  }
}

async function startJob(url, payload = {}) {
  setBusy(true);
  const data = await postJson(url, payload);
  setJob(data.job);
  startPolling();
  return data;
}

configForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  configStatus.textContent = "Speichern...";
  saveButton.disabled = true;
  try {
    const data = await postJson("/api/config", { config_data: collectConfig() });
    if (data.config_data) {
      configData = data.config_data;
      populateConfig(configData);
    }
    configStatus.textContent = "Gespeichert";
    showToast("Konfiguration erfolgreich gespeichert");
  } catch (error) {
    configStatus.textContent = error.message;
    showErrorToast("Konfiguration konnte nicht gespeichert werden", error);
  } finally {
    saveButton.disabled = false;
  }
});

copyEmailsButton.addEventListener("click", async () => {
  const emails = collectedEmails();
  if (!emails.length) {
    copyEmailsStatus.textContent = "Keine E-Mail-Adressen vorhanden.";
    updateCopyButton();
    showToast("Keine E-Mail-Adressen vorhanden", "error");
    return;
  }

  try {
    await writeClipboard(emails.join(", "));
    copyEmailsStatus.textContent = `${emails.length} E-Mail-Adresse(n) kopiert.`;
    showToast(`${emails.length} E-Mail-Adresse(n) kopiert`);
  } catch (error) {
    copyEmailsStatus.textContent = error.message;
    showErrorToast("E-Mail-Adressen konnten nicht kopiert werden", error);
  }
});

byId("filter-exclude-add-button").addEventListener("click", () => addExcludeType(true));
byId("filter-exclude-add").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    addExcludeType(true);
  }
});

collectButton.addEventListener("click", async () => {
  try {
    clearCsvReview();
    await startJob("/api/collect", { config_data: collectConfig() });
    showToast("Sammeln gestartet");
  } catch (error) {
    jobStatus.textContent = error.message;
    setBusy(false);
    showErrorToast("Sammeln konnte nicht gestartet werden", error);
  }
});

stopCollectButton.addEventListener("click", async () => {
  try {
    stopCollectButton.disabled = true;
    const data = await postJson("/api/collect/stop");
    setJob(data.job);
    startPolling();
    showToast("Suche wird gestoppt");
  } catch (error) {
    jobStatus.textContent = error.message;
    setBusy(latestJob.status === "running");
    showErrorToast("Suche konnte nicht gestoppt werden", error);
  }
});

confirmContact.addEventListener("change", () => {
  reviewedCsvSignature = confirmContact.checked ? currentCsvSignature : null;
  setBusy(latestJob.status === "running");
});

jobEvents.addEventListener("scroll", updateJobEventsFollowMode);

contactButton.addEventListener("click", async () => {
  try {
    await startJob("/api/contact", {
      confirm: confirmContact.checked,
      csv_signature: reviewedCsvSignature,
    });
    showToast("E-Mail-Versand gestartet");
  } catch (error) {
    jobStatus.textContent = error.message;
    setBusy(false);
    showErrorToast("E-Mail-Versand konnte nicht gestartet werden", error);
  }
});

refreshButton.addEventListener("click", () => refreshStatus({ notify: true }));
populateConfig(configData);
setJob(initialJob);
setBusy(initialJob.status === "running");
if (initialJob.status === "running") {
  startPolling();
  refreshStatus();
}
updateCopyButton();
"""
