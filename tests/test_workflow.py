from __future__ import annotations

import hashlib
import json

import pytest

from doctor_collector import workflow


def _csv_signature(path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


@pytest.mark.asyncio
async def test_collect_requires_post_code(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
therapie:
  post_code: ""
""",
        encoding="utf-8",
    )

    with pytest.raises(workflow.WorkflowError, match="post_code"):
        await workflow.collect_therapists(config_path, notify=False)


@pytest.mark.asyncio
async def test_contact_skips_already_contacted_and_updates_state(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    csv_path = tmp_path / "therapists.csv"
    state_path = tmp_path / ".contacted_therapists.json"

    config_path.write_text(
        """
contact:
  smtp_user: "sender@example.com"
  smtp_password: "secret"
  from_address: "sender@example.com"
""",
        encoding="utf-8",
    )
    csv_path.write_text(
        "\n".join([
            "name,email,therapist_type,website,profile_url,excluded",
            "Already,old@example.com,Type,,https://example.test/old,",
            "New,new@example.com,Type,,https://example.test/new,",
            "Excluded,excluded@example.com,Type,,https://example.test/excluded,yes",
            "No Mail,,Type,,https://example.test/no-mail,",
        ])
        + "\n",
        encoding="utf-8",
    )
    state_path.write_text(
        json.dumps({"contacted_emails": ["old@example.com"]}),
        encoding="utf-8",
    )

    class FakeContactor:
        sent_to: list[str] = []

        def __init__(self, _config):
            pass

        def is_enabled(self) -> bool:
            return True

        async def contact(self, therapists):
            self.__class__.sent_to = [t.email for t in therapists if t.email]
            return therapists

    monkeypatch.setattr(workflow, "TherapistContactor", FakeContactor)

    result = await workflow.contact_collected_therapists(
        config_path,
        csv_file=csv_path,
        state_file=state_path,
    )

    assert result.to_contact == 1
    assert result.already_contacted == 1
    assert result.contacted == 1
    assert FakeContactor.sent_to == ["new@example.com"]

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["contacted_emails"] == ["new@example.com", "old@example.com"]


@pytest.mark.asyncio
async def test_contact_raises_when_no_emails_send_successfully(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    csv_path = tmp_path / "therapists.csv"
    state_path = tmp_path / ".contacted_therapists.json"

    config_path.write_text(
        """
contact:
  smtp_user: "sender@example.com"
  smtp_password: "secret"
  from_address: "sender@example.com"
""",
        encoding="utf-8",
    )
    csv_path.write_text(
        "name,email,therapist_type,website,profile_url\n"
        "New,new@example.com,Type,,https://example.test/new\n",
        encoding="utf-8",
    )

    class FailingContactor:
        def __init__(self, _config):
            pass

        def is_enabled(self) -> bool:
            return True

        async def contact(self, _therapists):
            return []

    monkeypatch.setattr(workflow, "TherapistContactor", FailingContactor)

    with pytest.raises(workflow.WorkflowError, match="No emails were sent successfully"):
        await workflow.contact_collected_therapists(
            config_path,
            csv_file=csv_path,
            state_file=state_path,
        )

    assert not state_path.exists()


@pytest.mark.asyncio
async def test_contact_rejects_csv_changed_after_review(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    csv_path = tmp_path / "therapists.csv"
    state_path = tmp_path / ".contacted_therapists.json"

    config_path.write_text(
        """
contact:
  smtp_user: "sender@example.com"
  smtp_password: "secret"
  from_address: "sender@example.com"
""",
        encoding="utf-8",
    )
    csv_path.write_text(
        "name,email,therapist_type,website,profile_url\n"
        "Reviewed,reviewed@example.com,Type,,https://example.test/reviewed\n",
        encoding="utf-8",
    )
    reviewed_signature = _csv_signature(csv_path)
    csv_path.write_text(
        "name,email,therapist_type,website,profile_url\n"
        "Unreviewed,unreviewed@example.com,Type,,https://example.test/unreviewed\n",
        encoding="utf-8",
    )

    class FakeContactor:
        def __init__(self, _config):
            pass

        def is_enabled(self) -> bool:
            return True

        async def contact(self, _therapists):
            raise AssertionError("contact should not be called")

    monkeypatch.setattr(workflow, "TherapistContactor", FakeContactor)

    with pytest.raises(workflow.WorkflowError, match="CSV changed after review"):
        await workflow.contact_collected_therapists(
            config_path,
            csv_file=csv_path,
            state_file=state_path,
            expected_csv_signature=reviewed_signature,
        )

    assert not state_path.exists()


@pytest.mark.asyncio
async def test_collect_can_ignore_direct_env_overrides_for_web(tmp_path, monkeypatch):
    monkeypatch.setenv("THERAPIE_POST_CODE", "10115")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
therapie:
  post_code: ""
""",
        encoding="utf-8",
    )

    with pytest.raises(workflow.WorkflowError, match="post_code"):
        await workflow.collect_therapists(
            config_path,
            notify=False,
            csv_file=tmp_path / "therapists.csv",
            state_file=tmp_path / ".contacted_therapists.json",
            apply_env_overrides=False,
        )
