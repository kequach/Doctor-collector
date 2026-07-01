from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from http import HTTPStatus
from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest

from doctor_collector.web import (
    _CSS,
    _JS,
    DoctorCollectorWebApp,
    _csv_signature,
    _is_loopback_host,
    _link_cell,
    _ReusableThreadingHTTPServer,
)
from doctor_collector.workflow import CollectSummary, ContactSummary, WorkflowError


def _wait_for_job(app: DoctorCollectorWebApp) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        snapshot = app.jobs.snapshot()
        if snapshot["status"] != "running":
            return snapshot
        time.sleep(0.01)
    raise AssertionError("web job did not finish")


def _request_app(
    app: DoctorCollectorWebApp,
    path: str,
    *,
    method: str = "GET",
    host: str | None = None,
    token: str | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, str, bytes]:
    server = _ReusableThreadingHTTPServer(("127.0.0.1", 0), app.handler_class())
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
    headers = {}
    if host is not None:
        headers["Host"] = host
    if token is not None:
        headers["X-Doctor-Collector-Token"] = token
    if content_type is not None:
        headers["Content-Type"] = content_type
    if body is not None:
        headers["Content-Length"] = str(len(body))
    if extra_headers:
        headers.update(extra_headers)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        return response.status, response.headers.get("Content-Type", ""), raw
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request_app_with_headers(
    app: DoctorCollectorWebApp,
    path: str,
    *,
    method: str = "GET",
    host: str | None = None,
    token: str | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    server = _ReusableThreadingHTTPServer(("127.0.0.1", 0), app.handler_class())
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
    headers = {}
    if host is not None:
        headers["Host"] = host
    if token is not None:
        headers["X-Doctor-Collector-Token"] = token
    if content_type is not None:
        headers["Content-Type"] = content_type
    if body is not None:
        headers["Content-Length"] = str(len(body))
    if extra_headers:
        headers.update(extra_headers)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        response_headers = {key.lower(): value for key, value in response.headers.items()}
        return response.status, response_headers, raw
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request_app_json(
    app: DoctorCollectorWebApp,
    path: str,
    *,
    method: str = "GET",
    host: str | None = None,
    token: str | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, dict[str, object]]:
    status, _content_type, raw = _request_app(
        app,
        path,
        method=method,
        host=host,
        token=token,
        body=body,
        content_type=content_type,
    )
    return status, json.loads(raw.decode("utf-8"))


def _form_body(data: dict[str, object]) -> bytes:
    return urlencode({key: str(value) for key, value in data.items()}).encode("utf-8")


def test_web_contact_requires_csv_review_confirmation(tmp_path):
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    with pytest.raises(WorkflowError, match="aktuelle CSV"):
        app.start_contact(confirmed=False, reviewed_csv_signature=None)

    assert app.jobs.snapshot()["status"] == "idle"


def test_web_api_status_reports_json_error_instead_of_dropping_connection(tmp_path):
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    def fail_therapists_payload() -> dict[str, object]:
        raise PermissionError("CSV is temporarily unavailable")

    app._therapists_payload = fail_therapists_payload  # type: ignore[method-assign]

    status, payload = _request_app_json(app, "/api/status")

    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert payload == {"error": "PermissionError: CSV is temporarily unavailable"}


def test_web_api_responses_are_not_cached_and_close_local_connection(tmp_path):
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    status, headers, raw = _request_app_with_headers(app, "/api/status")

    assert status == HTTPStatus.OK
    assert json.loads(raw.decode("utf-8"))["job"]["status"] == "idle"
    assert headers["cache-control"] == "no-store"
    assert headers["pragma"] == "no-cache"
    assert headers["connection"] == "close"
    assert "access-control-allow-origin" not in headers


def test_web_serves_favicon_asset(tmp_path):
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    status, content_type, raw = _request_app(app, "/assets/favicon.png")

    assert status == HTTPStatus.OK
    assert content_type == "image/png"
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/api/status"),
        ("POST", "/api/collect"),
        ("POST", "/api/collect/stop"),
        ("POST", "/api/therapists/exclude"),
        ("POST", "/api/therapists/remove"),
        ("POST", "/api/contact"),
    ],
)
def test_web_rejects_non_loopback_host_headers(tmp_path, method, path):
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    status, content_type, raw = _request_app(
        app,
        path,
        method=method,
        host="attacker.example",
        token=app.csrf_token,
        body=b"{}" if method == "POST" else None,
        content_type="application/json" if method == "POST" else None,
    )

    assert status == HTTPStatus.FORBIDDEN
    if path.startswith("/api/"):
        assert "application/json" in content_type
        assert json.loads(raw.decode("utf-8")) == {"error": "Invalid Host header"}
    else:
        assert raw.decode("utf-8") == "Invalid Host header"


def test_web_collect_reads_json_body_before_starting_job(tmp_path):
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    status, payload = _request_app_json(
        app,
        "/api/collect",
        method="POST",
        token=app.csrf_token,
        body=b"{",
        content_type="application/json",
    )

    assert status == HTTPStatus.BAD_REQUEST
    assert payload["error"].startswith("JSONDecodeError:")
    assert app.jobs.snapshot()["status"] == "idle"


def test_web_collect_accepts_form_body_token_without_custom_header(tmp_path):
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    status, payload = _request_app_json(
        app,
        "/api/collect",
        method="POST",
        body=_form_body({"doctor_collector_token": app.csrf_token}),
    )
    snapshot = _wait_for_job(app)

    assert status == HTTPStatus.ACCEPTED
    assert payload["ok"] is True
    assert snapshot["status"] == "failed"


def test_web_config_accepts_form_body_token_and_json_payload(tmp_path):
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    status, payload = _request_app_json(
        app,
        "/api/config",
        method="POST",
        body=_form_body({
            "doctor_collector_token": app.csrf_token,
            "config_data": json.dumps({"therapie": {"post_code": "10115"}}),
        }),
    )

    assert status == HTTPStatus.OK
    assert payload["ok"] is True
    assert payload["config_data"]["therapie"]["post_code"] == "10115"


def test_web_collect_uses_unsaved_config_payload_without_writing_it(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("therapie:\n  post_code: '10115'\n", encoding="utf-8")
    seen_config = None

    async def fake_collect(*args, config=None, **kwargs):
        nonlocal seen_config
        seen_config = config
        return CollectSummary(
            total_profiles_scraped=0,
            total_matching=0,
            saved_to=tmp_path / "therapists.csv",
            therapists=0,
            csv_saved=True,
        )

    monkeypatch.setattr("doctor_collector.web.collect_therapists", fake_collect)
    app = DoctorCollectorWebApp(config_path)

    status, payload = _request_app_json(
        app,
        "/api/collect",
        method="POST",
        body=_form_body({
            "doctor_collector_token": app.csrf_token,
            "config_data": json.dumps({
                "therapie": {
                    "post_code": "60320",
                    "max_therapists": 4,
                },
            }),
        }),
    )
    snapshot = _wait_for_job(app)

    assert status == HTTPStatus.ACCEPTED
    assert payload["ok"] is True
    assert snapshot["status"] == "succeeded"
    assert seen_config is not None
    assert seen_config.therapie.post_code == "60320"
    assert seen_config.therapie.max_therapists == 4
    assert "10115" in config_path.read_text(encoding="utf-8")
    assert "60320" not in config_path.read_text(encoding="utf-8")


def test_web_collect_stop_endpoint_marks_running_collect_job(tmp_path):
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")
    finish = threading.Event()
    progress_seen = threading.Event()

    def action(progress):
        progress("Sammeln laeuft.")
        progress_seen.set()
        finish.wait(timeout=2)
        return {"message": "Fertig"}

    started, _ = app.jobs.start("Sammeln", action)
    try:
        assert progress_seen.wait(timeout=2)
        status, payload = _request_app_json(
            app,
            "/api/collect/stop",
            method="POST",
            token=app.csrf_token,
            body=b"{}",
            content_type="application/json",
        )
    finally:
        finish.set()
        _wait_for_job(app)

    assert started
    assert status == HTTPStatus.ACCEPTED
    assert payload["ok"] is True
    assert payload["job"]["details"]["stop_requested"] is True


def test_web_page_includes_request_token(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("therapie:\n  post_code: '10115'\n", encoding="utf-8")
    app = DoctorCollectorWebApp(config_path)

    rendered = app.render_index()

    assert 'name="doctor-collector-token"' in rendered
    assert app.csrf_token in rendered
    assert 'data-csv-signature="' in rendered
    assert 'id="config-editor"' not in rendered
    assert 'id="therapie-post-code"' in rendered
    assert 'id="contact-smtp-password"' in rendered
    assert 'id="config-data"' in rendered
    assert 'id="therapie-max-therapists" type="number" min="0"' in rendered
    assert "0 = kein Limit." in rendered
    assert 'id="therapie-request-delay-seconds" type="number" min="0.1"' in rendered
    assert "Erweiterte Einstellungen" in rendered
    assert "<h2>Schritte</h2>" in rendered
    assert "Einstellungen ausfüllen." in rendered
    assert "Optional: Konfiguration speichern." in rendered
    assert "E-Mail-Text (optional)" in rendered
    assert "SMTP für Web-Versand (optional)" in rendered
    assert "E-Mails senden (optional)" in rendered
    assert "CSV geprüft?" in rendered
    entries_index = rendered.index("<h2>Gesammelte Einträge</h2>")
    assert rendered.index("E-Mail-Text (optional)") > entries_index
    assert rendered.index("SMTP für Web-Versand (optional)") > entries_index
    assert rendered.index("<div class=\"settings-header\">E-Mails senden</div>") > entries_index
    assert "Keine Daten oder Zugangsdaten werden hochgeladen." in rendered
    assert 'id="copy-emails-button"' in rendered
    assert 'id="job-progress"' in rendered
    assert 'id="stop-collect-button"' in rendered
    assert 'id="job-profile-count"' in rendered
    assert 'id="job-events"' in rendered
    assert 'id="toast-region"' in rendered
    assert 'id="job-data"' in rendered
    assert 'id="copy-emails-data"' in rendered
    assert "/assets/styles.css?v=" in rendered
    assert "/assets/app.js?v=" in rendered
    assert "/assets/favicon.png?v=" in rendered


def test_web_layout_keeps_desktop_columns_balanced():
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);" in _CSS
    assert "grid-template-columns: minmax(0, 1fr) 300px;" not in _CSS


def test_web_activity_log_follows_newest_event_until_user_scrolls_up():
    assert "let followJobEvents = true;" in _JS
    assert "const JOB_EVENTS_BOTTOM_TOLERANCE = 8;" in _JS
    assert "const shouldFollow = followJobEvents || isJobEventsAtBottom();" in _JS
    assert "const previousScrollTop = jobEvents.scrollTop;" in _JS
    assert "function settleJobEventsScroll(shouldFollow, previousScrollTop)" in _JS
    assert "function isJobEventsAtBottom()" in _JS
    assert "function scrollJobEventsToBottom()" in _JS
    assert "jobEvents.scrollTop = jobEvents.scrollHeight;" in _JS
    assert "jobEvents.scrollTop = previousScrollTop;" in _JS
    assert "function updateJobEventsFollowMode()" in _JS
    assert 'jobEvents.addEventListener("scroll", updateJobEventsFollowMode);' in _JS


def test_web_csv_review_checkbox_sits_next_to_label_text():
    assert ".checkline input" in _CSS
    assert "width: auto;" in _CSS
    assert "flex: 0 0 auto;" in _CSS


def test_web_uses_toast_notifications_for_button_actions():
    assert ".toast-region" in _CSS
    assert ".toast-success" in _CSS
    assert ".toast-error" in _CSS
    assert "@keyframes toast-in" in _CSS
    assert "@keyframes toast-out" in _CSS
    assert 'const toastRegion = document.querySelector("#toast-region");' in _JS
    assert "function showToast" in _JS
    assert "function showErrorToast" in _JS
    assert "Konfiguration erfolgreich gespeichert" in _JS
    assert "Konfiguration konnte nicht gespeichert werden" in _JS
    assert "Sammeln gestartet" in _JS
    assert "Sammeln konnte nicht gestartet werden" in _JS
    assert "Daten erfolgreich aktualisiert" in _JS
    assert "Daten konnten nicht aktualisiert werden" in _JS
    assert "E-Mail-Versand gestartet" in _JS
    assert "E-Mail-Versand konnte nicht gestartet werden" in _JS
    assert 'startJob("/api/collect", { config_data: collectConfig() })' in _JS
    assert "notifyJobTransition" in _JS
    assert 'refreshButton.addEventListener("click", () => refreshStatus({ notify: true }));' in _JS


def test_web_copy_and_row_controls_ignore_excluded_rows():
    assert 'document.querySelector("#copy-emails-data").textContent' in _JS
    assert "let copyableEmails = JSON.parse" in _JS
    assert "copyableEmails = Array.isArray(payload.emails)" in _JS
    assert 'postJson("/api/therapists/exclude"' in _JS
    assert 'postJson("/api/therapists/remove"' in _JS


def test_web_uses_hidden_form_frame_fallback_to_support_firefox():
    assert 'typeof fetch === "function"' in _JS
    assert "return await fetch(url, { cache: \"no-store\", ...options });" in _JS
    assert "requestWithFrame" in _JS
    assert "document.createElement(\"iframe\")" in _JS
    assert "document.createElement(\"form\")" in _JS
    assert "form.submit();" in _JS
    assert "URLSearchParams(options.body || \"\")" in _JS
    assert "XMLHttpRequest" not in _JS
    assert "absoluteUrl(url)" in _JS
    assert "formBody" in _JS
    assert "doctor_collector_token" in _JS
    assert "X-Doctor-Collector-Token" not in _JS
    assert "Content-Type" not in _JS
    assert "canonicalLocalhostUrl" in _JS
    assert "window.location.replace(canonicalLocalUrl);" in _JS
    assert "http://127.0.0.1${port}${window.location.pathname}" in _JS
    assert "loopbackFallbackUrl" not in _JS
    assert "simpleLoopbackFallbackOptions" not in _JS
    assert "http://127.0.0.1:${window.location.port}${url}" not in _JS


def test_web_page_bootstraps_running_job_state(tmp_path):
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")
    finish = threading.Event()
    progress_seen = threading.Event()

    def action(progress):
        progress("Sammeln laeuft.")
        progress_seen.set()
        finish.wait(timeout=2)
        return {"message": "Fertig"}

    started, _ = app.jobs.start("Sammeln", action)
    try:
        assert progress_seen.wait(timeout=2)
        rendered = app.render_index()
    finally:
        finish.set()
        _wait_for_job(app)

    assert started
    assert 'aria-hidden="false"' in rendered
    assert '"status": "running"' in rendered
    assert "Sammeln laeuft." in rendered
    assert 'id="job-data"' in rendered
    assert 'initialJob.status === "running"' in _JS
    assert "startPolling();" in _JS


def test_web_collect_exposes_sanitized_progress_events(tmp_path, monkeypatch):
    async def fake_collect(*args, **kwargs):
        logging.getLogger("doctor_collector.clients.therapie").info(
            "Crawling listing page %d: %s",
            1,
            "https://www.therapie.de/therapeutensuche/ergebnisse/?ort=10115",
        )
        logging.getLogger("doctor_collector.clients.therapie").info(
            "Found %d profiles on page %d",
            2,
            1,
        )
        logging.getLogger("doctor_collector.services.collector").info(
            "%d profiles passed filters",
            1,
        )
        logging.getLogger("doctor_collector.clients.therapie").warning(
            "Skipping profile %s after request error: %s",
            "https://example.test/profile?email=ada@example.com",
            "ada@example.com",
        )
        logging.getLogger("doctor_collector.clients.therapie").warning(
            "Skipping profile %s after HTTP %d",
            "https://example.test/profile?email=grace@example.com",
            403,
        )
        return CollectSummary(
            total_profiles_scraped=2,
            total_matching=1,
            saved_to=tmp_path / "therapists.csv",
            therapists=1,
            csv_saved=True,
        )

    monkeypatch.setattr("doctor_collector.web.collect_therapists", fake_collect)
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    started, _ = app.start_collect()
    snapshot = _wait_for_job(app)

    messages = [event["message"] for event in snapshot["events"]]
    levels = {event["message"]: event["level"] for event in snapshot["events"]}
    assert started
    assert snapshot["status"] == "succeeded"
    assert snapshot["details"]["profiles_collected"] == 2
    assert "Suchergebnisseite 1 wird geladen." in messages
    assert "2 Profil(e) auf Seite 1 gefunden." in messages
    assert "1 Profil(e) passen zu den Filtern." in messages
    assert "Skipping profile [URL] after request error: [E-Mail]" in messages
    assert levels["Skipping profile [URL] after request error: [E-Mail]"] == "error"
    forbidden_message = (
        "Profil konnte nicht geladen werden: therapie.de hat die Anfrage "
        "abgelehnt (HTTP 403). Bitte später erneut versuchen oder die Wartezeit erhöhen."
    )
    assert forbidden_message in messages
    assert levels[forbidden_message] == "error"
    assert not any("https://" in message or "10115" in message for message in messages)
    assert not any("ada@example.com" in message for message in messages)
    assert not any("grace@example.com" in message for message in messages)


def test_web_stop_collect_requests_cooperative_stop(tmp_path, monkeypatch):
    stop_seen = threading.Event()
    wait_seen = threading.Event()

    async def fake_collect(*args, stop_requested=None, stop_wait=None, **kwargs):
        assert stop_requested is not None
        assert stop_wait is not None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if stop_requested():
                stop_seen.set()
                break
            await asyncio.sleep(0.01)
        if stop_wait(0):
            wait_seen.set()
        return CollectSummary(
            total_profiles_scraped=3,
            total_matching=0,
            saved_to=tmp_path / "therapists.csv",
            therapists=0,
            csv_saved=False,
        )

    monkeypatch.setattr("doctor_collector.web.collect_therapists", fake_collect)
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    started, _ = app.start_collect()
    stopped, state = app.stop_collect()
    snapshot = _wait_for_job(app)

    assert started
    assert stopped
    assert state["details"]["stop_requested"] is True
    assert stop_seen.is_set()
    assert wait_seen.is_set()
    assert snapshot["status"] == "stopped"
    assert snapshot["details"]["profiles_collected"] == 3
    assert "Suche gestoppt" in snapshot["message"]


def test_web_page_uses_model_defaults_when_selected_config_is_invalid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "therapie:\n  post_code: '99999'\n",
        encoding="utf-8",
    )
    selected_config = tmp_path / "bad" / "config.yaml"
    selected_config.parent.mkdir()
    selected_config.write_text(
        "therapie:\n  search_radius_km: 20\n",
        encoding="utf-8",
    )
    app = DoctorCollectorWebApp(selected_config)

    rendered = app.render_index()

    assert "search_radius_km must be one of" in rendered
    assert "99999" not in rendered
    assert '"post_code": ""' in rendered


def test_web_rejects_stale_csv_review(tmp_path, monkeypatch):
    csv_path = tmp_path / "therapists.csv"
    csv_path.write_text("name,email,therapist_type,website,profile_url\n", encoding="utf-8")
    reviewed_signature = _csv_signature(csv_path)
    csv_path.write_text(
        "name,email,therapist_type,website,profile_url\nNew,new@example.com,Type,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("doctor_collector.web.get_default_csv_path", lambda: csv_path)

    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    with pytest.raises(WorkflowError, match="aktuelle CSV"):
        app.start_contact(
            confirmed=True,
            reviewed_csv_signature=reviewed_signature,
        )


def test_web_payload_marks_excluded_rows_not_contactable(tmp_path, monkeypatch):
    csv_path = tmp_path / "therapists.csv"
    csv_path.write_text(
        "name,email,therapist_type,website,profile_url,excluded\n"
        "Active,active@example.com,Type,,https://example.test/active,\n"
        "Duplicate,active@example.com,Type,,https://example.test/duplicate,\n"
        "Disabled,disabled@example.com,Type,,https://example.test/disabled,yes\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("doctor_collector.web.get_default_csv_path", lambda: csv_path)

    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    payload = app.therapists_payload()

    assert payload["count"] == 3
    assert payload["contactable"] == 2
    assert payload["excluded"] == 1
    assert payload["emails"] == ["active@example.com"]
    assert payload["rows"][0]["excluded"] is False
    assert payload["rows"][2]["excluded"] is True


def test_web_can_exclude_and_remove_csv_rows(tmp_path, monkeypatch):
    csv_path = tmp_path / "therapists.csv"
    csv_path.write_text(
        "name,email,therapist_type,website,profile_url,excluded,note\n"
        "Ada,ada@example.com,Type,,https://example.test/ada,,keep ada\n"
        "Grace,grace@example.com,Type,,https://example.test/grace,,keep grace\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("doctor_collector.web.get_default_csv_path", lambda: csv_path)
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    status, payload = _request_app_json(
        app,
        "/api/therapists/exclude",
        method="POST",
        body=_form_body({
            "doctor_collector_token": app.csrf_token,
            "row_index": 1,
            "excluded": "true",
            "csv_signature": _csv_signature(csv_path),
        }),
    )

    assert status == HTTPStatus.OK
    assert payload["therapists"]["contactable"] == 1
    assert payload["therapists"]["rows"][1]["excluded"] is True
    assert "Grace,grace@example.com,Type,,https://example.test/grace,yes,keep grace" in (
        csv_path.read_text(encoding="utf-8")
    )

    status, payload = _request_app_json(
        app,
        "/api/therapists/remove",
        method="POST",
        body=_form_body({
            "doctor_collector_token": app.csrf_token,
            "row_index": 0,
            "csv_signature": payload["therapists"]["csv_signature"],
        }),
    )

    assert status == HTTPStatus.OK
    assert payload["therapists"]["count"] == 1
    assert payload["therapists"]["rows"][0]["email"] == "grace@example.com"
    text = csv_path.read_text(encoding="utf-8")
    assert "ada@example.com" not in text
    assert "keep grace" in text


@pytest.mark.parametrize("path", ["/api/therapists/exclude", "/api/therapists/remove"])
def test_web_row_edits_require_current_csv_signature(tmp_path, monkeypatch, path):
    csv_path = tmp_path / "therapists.csv"
    csv_path.write_text(
        "name,email,therapist_type,website,profile_url\n"
        "Ada,ada@example.com,Type,,https://example.test/ada\n",
        encoding="utf-8",
    )
    stale_signature = _csv_signature(csv_path)
    csv_path.write_text(
        "name,email,therapist_type,website,profile_url\n"
        "Grace,grace@example.com,Type,,https://example.test/grace\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("doctor_collector.web.get_default_csv_path", lambda: csv_path)
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    status, payload = _request_app_json(
        app,
        path,
        method="POST",
        body=_form_body({
            "doctor_collector_token": app.csrf_token,
            "row_index": 0,
            "excluded": "true",
            "csv_signature": stale_signature,
        }),
    )

    assert status == HTTPStatus.BAD_REQUEST
    assert "CSV changed" in payload["error"]

    status, payload = _request_app_json(
        app,
        path,
        method="POST",
        body=_form_body({
            "doctor_collector_token": app.csrf_token,
            "row_index": 0,
            "excluded": "true",
        }),
    )

    assert status == HTTPStatus.BAD_REQUEST
    assert "CSV changed" in payload["error"]
    assert "grace@example.com" in csv_path.read_text(encoding="utf-8")


def test_web_contact_passes_reviewed_csv_signature_to_workflow(tmp_path, monkeypatch):
    csv_path = tmp_path / "therapists.csv"
    csv_path.write_text("name,email,therapist_type,website,profile_url\n", encoding="utf-8")
    reviewed_signature = _csv_signature(csv_path)
    monkeypatch.setattr("doctor_collector.web.get_default_csv_path", lambda: csv_path)

    seen_signature = None

    async def fake_contact(*args, expected_csv_signature=None, **kwargs):
        nonlocal seen_signature
        seen_signature = expected_csv_signature
        return ContactSummary(to_contact=0, already_contacted=0, contacted=0)

    monkeypatch.setattr("doctor_collector.web.contact_collected_therapists", fake_contact)
    app = DoctorCollectorWebApp(tmp_path / "config.yaml")

    started, _ = app.start_contact(
        confirmed=True,
        reviewed_csv_signature=reviewed_signature,
    )
    snapshot = _wait_for_job(app)

    assert started
    assert snapshot["status"] == "succeeded"
    assert seen_signature == reviewed_signature


def test_web_page_shows_csv_path_and_copy_email_option(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("therapie:\n  post_code: '10115'\n", encoding="utf-8")
    csv_path = tmp_path / "therapists.csv"
    csv_path.write_text(
        "name,email,therapist_type,website,profile_url\n"
        "Ada,ada@example.com,Type,,https://example.test/ada\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("doctor_collector.web.get_default_csv_path", lambda: csv_path)
    app = DoctorCollectorWebApp(config_path)

    rendered = app.render_index()

    assert str(csv_path) in rendered
    assert "Gespeicherte CSV-Datei" in rendered
    assert "E-Mail-Adressen kopieren" in rendered
    assert "kommagetrennt" in rendered
    assert "ada@example.com" in rendered
    assert "<th>Aktiv</th>" in rendered
    assert "<th>Entfernen</th>" in rendered
    assert 'class="row-remove-cell"' in rendered
    assert 'data-row-action="toggle-excluded"' in rendered
    assert 'data-row-action="remove"' in rendered


def test_web_host_validation_allows_only_loopback():
    assert _is_loopback_host("localhost")
    assert _is_loopback_host("127.0.0.1")
    assert _is_loopback_host("::1")
    assert not _is_loopback_host("0.0.0.0")
    assert not _is_loopback_host("192.168.1.10")


def test_web_renders_unsafe_links_as_text():
    assert 'href="javascript:' not in _link_cell("javascript:alert(1)")
    assert "javascript:alert(1)" in _link_cell("javascript:alert(1)")
    assert 'href="https://example.test"' in _link_cell("https://example.test")
