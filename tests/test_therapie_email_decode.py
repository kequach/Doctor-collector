from __future__ import annotations

from bs4 import BeautifulSoup

from doctor_collector.clients.therapie import TherapieClient, _decode_email


def test_decode_email_handles_wrapped_z_from_therapie_profile():
    assert (
        _decode_email("h/cvoa.tdimpfttfsAcmvfnbjm/di")
        == "g.bunz-schloesser@bluemail.ch"
    )


def test_decode_email_handles_wrapped_digits_plus_and_uppercase_z():
    assert _decode_email("a0,AAfybnqmf/ef") == "z9+Z@example.de"


def test_extract_email_decodes_contact_button_attribute_with_wrapped_chars():
    soup = BeautifulSoup(
        """
        <html>
          <body>
            <button
              id="contact-button"
              data-contact-email="h/cvoa.tdimpfttfsAcmvfnbjm/di"
            >
              schreiben
            </button>
          </body>
        </html>
        """,
        "html.parser",
    )

    assert TherapieClient._extract_email(soup) == "g.bunz-schloesser@bluemail.ch"
