from __future__ import annotations

import sys

import pytest

from doctor_collector import __main__ as cli


def test_cli_rejects_combined_collect_contact(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["doctor_collector", "--collect", "--contact"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert "review therapists.csv" in capsys.readouterr().out
