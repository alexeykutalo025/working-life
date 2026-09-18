"""Encoding repair, HTML and RTF conversion, subject normalisation."""

from __future__ import annotations

import pytest

from recall.normalize.text import (
    clean_text,
    decode_bytes,
    decompress_rtf,
    html_to_text,
    is_compressed_rtf,
    looks_like_mojibake,
    normalize_subject,
    repair_mojibake,
    rtf_to_text,
    snippet,
    strip_signature,
)

# --- decoding -------------------------------------------------------------


def test_ascii_is_certain():
    result = decode_bytes(b"plain ascii text")
    assert result.text == "plain ascii text"
    assert result.confidence == 1.0
    assert not result.was_guessed


def test_declared_cp1252_decodes_cleanly():
    """A 1997 Windows mailer: curly quotes and a pound sign in cp1252."""
    raw = "The quote is £4,500 – that’s final.".encode("cp1252")
    result = decode_bytes(raw, "windows-1252")
    assert "£4,500" in result.text
    assert "’" in result.text
    assert result.confidence == 1.0


def test_iso_8859_1_is_treated_as_cp1252():
    """Mail labelled Latin-1 is nearly always cp1252, and the difference shows.

    Byte 0x92 is a right single quote in cp1252 and an unprintable control
    character in true Latin-1. Trusting the label would mangle every apostrophe
    in twenty years of mail.
    """
    raw = b"it\x92s here"
    result = decode_bytes(raw, "iso-8859-1")
    assert result.text == "it’s here"


def test_undeclared_utf8_is_recognised():
    raw = "Réunion avec Aoife".encode("utf-8")
    result = decode_bytes(raw)
    assert result.text == "Réunion avec Aoife"
    assert result.confidence < 1.0, "no declaration means we are not certain"


def test_undecodable_bytes_keep_the_message():
    """Never lose a message because some bytes are unreadable.

    What the bytes in the middle turn out to be is a guess whichever encoding
    wins; what matters is that the readable text survives and the result is
    not claimed to be certain.
    """
    result = decode_bytes(b"good text \xff\xfe\xff more good text")
    assert "good text" in result.text
    assert "more good text" in result.text
    assert result.confidence < 1.0


def test_empty_bytes():
    assert decode_bytes(b"").text == ""


# --- mojibake -------------------------------------------------------------


def test_classic_apostrophe_mojibake_is_detected_and_repaired():
    """UTF-8 right-quote read as cp1252. The single most common case."""
    broken = "Fitzgeraldâ€™s invoice"
    assert looks_like_mojibake(broken)
    fixed, was_repaired = repair_mojibake(broken)
    assert fixed == "Fitzgerald’s invoice"
    assert was_repaired


def test_pound_sign_mojibake():
    broken = "The quote is Â£4,500"
    fixed, _ = repair_mojibake(broken)
    assert fixed == "The quote is £4,500"


def test_dash_mojibake():
    broken = "4,500 â€“ final"
    fixed, _ = repair_mojibake(broken)
    assert fixed == "4,500 – final"


def test_correct_accented_text_is_left_alone():
    """Genuine French must not be "repaired" into nonsense."""
    good = "Réunion avec Aoife Ní Bhraonáin à Paris"
    assert not looks_like_mojibake(good)
    fixed, was_repaired = repair_mojibake(good)
    assert fixed == good
    assert not was_repaired


def test_plain_english_is_left_alone():
    good = "The quarterly figures are attached."
    fixed, was_repaired = repair_mojibake(good)
    assert fixed == good and not was_repaired


def test_repair_lowers_confidence():
    """Repair is an improvement, but it is still an inference."""
    result = clean_text("Fitzgeraldâ€™s invoice")
    assert result.repaired_mojibake
    assert result.confidence < 1.0


# --- clean_text -----------------------------------------------------------


def test_clean_text_normalizes_line_endings():
    assert clean_text(b"a\r\nb\rc\n").text == "a\nb\nc\n"


def test_clean_text_strips_nulls():
    assert "\x00" not in clean_text(b"before\x00after").text


def test_clean_text_normalizes_unicode():
    """Composed and decomposed accents must compare equal, for search and dedup."""
    decomposed = "été"     # e + combining acute
    composed = "été"
    assert clean_text(decomposed).text == clean_text(composed).text


# --- HTML -----------------------------------------------------------------


def test_html_to_text_extracts_readable_text():
    text = html_to_text("<html><body><p>The proof is <b>ready</b>.</p></body></html>")
    assert "The proof is ready." in text


def test_html_to_text_drops_style_and_script():
    text = html_to_text(
        "<html><head><style>p{color:red}</style></head>"
        "<body><p>Visible</p><script>alert(1)</script></body></html>"
    )
    assert "Visible" in text
    assert "color" not in text
    assert "alert" not in text


def test_html_to_text_keeps_link_addresses():
    """In an archive, where a link pointed may be the only record of a supplier."""
    text = html_to_text("<p>See <a href='http://studiolibre.fr/proof'>the proof</a>.</p>")
    assert "the proof" in text
    assert "studiolibre.fr/proof" in text


def test_html_to_text_keeps_image_alt_text():
    assert "[image: Company logo]" in html_to_text("<img src='x.gif' alt='Company logo'>")


def test_html_to_text_gives_blocks_line_breaks():
    text = html_to_text("<p>One</p><p>Two</p>")
    assert "One" in text and "Two" in text
    assert "\n" in text


def test_html_to_text_survives_malformed_markup():
    text = html_to_text("<p>Unclosed <b>bold <i>and italic</p>")
    assert "Unclosed" in text and "bold" in text


def test_html_to_text_decodes_entities():
    assert "Fish & Chips" in html_to_text("<p>Fish &amp; Chips</p>")


def test_html_to_text_of_nothing():
    assert html_to_text("") == ""


# --- RTF ------------------------------------------------------------------


def test_rtf_to_text_basic():
    rtf = r"{\rtf1\ansi\deff0{\fonttbl{\f0 Times;}}\f0\fs24 Hello world\par}"
    assert "Hello world" in rtf_to_text(rtf)


def test_rtf_to_text_drops_font_and_colour_tables():
    rtf = (
        r"{\rtf1\ansi{\fonttbl{\f0\froman Times New Roman;}}"
        r"{\colortbl;\red255\green0\blue0;}\f0 Visible text\par}"
    )
    text = rtf_to_text(rtf)
    assert "Visible text" in text
    assert "Times New Roman" not in text
    assert "red255" not in text


def test_rtf_hex_escapes_become_characters():
    rtf = r"{\rtf1\ansi Caf\'e9 du Nord\par}"
    assert "Café du Nord" in rtf_to_text(rtf)


def test_rtf_unicode_escapes():
    rtf = r"{\rtf1\ansi R\u233?union\par}"
    assert "Réunion" in rtf_to_text(rtf)


def test_rtf_paragraph_becomes_newline():
    assert "\n" in rtf_to_text(r"{\rtf1\ansi One\par Two\par}")


def test_rtf_smart_quotes():
    text = rtf_to_text(r"{\rtf1\ansi \lquote quoted\rquote\par}")
    assert "‘" in text and "’" in text


def test_rtf_of_nothing():
    assert rtf_to_text("") == ""


def test_uncompressed_rtf_marker_is_recognised():
    import struct

    payload = rb"{\rtf1\ansi Stored uncompressed\par}"
    data = struct.pack("<II4sI", len(payload), len(payload), b"MELA", 0) + payload
    assert is_compressed_rtf(data)
    assert "Stored uncompressed" in decompress_rtf(data)


def test_unknown_compression_marker_is_refused_with_a_reason():
    import struct

    data = struct.pack("<II4sI", 10, 10, b"XXXX", 0) + b"0123456789"
    with pytest.raises(ValueError, match="unknown marker"):
        decompress_rtf(data)


def test_too_short_compressed_rtf_is_refused():
    with pytest.raises(ValueError, match="too short"):
        decompress_rtf(b"short")


# --- subjects -------------------------------------------------------------


@pytest.mark.parametrize("subject,expected", [
    ("Quarterly figures", "quarterly figures"),
    ("RE: Quarterly figures", "quarterly figures"),
    ("Re: Quarterly figures", "quarterly figures"),
    ("FW: Quarterly figures", "quarterly figures"),
    ("Fwd: Quarterly figures", "quarterly figures"),
    ("RE: FW: Re: Quarterly figures", "quarterly figures"),
    ("AW: Quarterly figures", "quarterly figures"),      # German
    ("SV: Quarterly figures", "quarterly figures"),      # Scandinavian
    ("TR: Quarterly figures", "quarterly figures"),      # French forward
    ("[clients] Quarterly figures", "quarterly figures"),
    ("RE: [clients] Quarterly figures", "quarterly figures"),
    ("RE[2]: Quarterly figures", "quarterly figures"),
    ("  RE:   Quarterly    figures  ", "quarterly figures"),
])
def test_normalize_subject(subject, expected):
    assert normalize_subject(subject) == expected


def test_normalize_subject_of_nothing():
    assert normalize_subject(None) == ""
    assert normalize_subject("") == ""


def test_normalize_subject_keeps_a_subject_that_starts_with_re_as_a_word():
    """"Review" must not lose its first two letters."""
    assert normalize_subject("Review of the Kelly account") == "review of the kelly account"


# --- signatures -----------------------------------------------------------


def test_signature_is_split_off_not_discarded():
    """A 1997 signature block may be the only record of somebody's job title."""
    body = (
        "Margaret,\n\nThe figures are attached.\n\nTim\n"
        "-- \nTim McCarthy\nManaging Director\nContract Marketing Ltd\n+353 1 555 0100\n"
    )
    kept, removed = strip_signature(body)
    assert "The figures are attached." in kept
    assert "Managing Director" not in kept
    assert "Managing Director" in removed


def test_quoted_reply_is_split_off():
    body = (
        "Read them. Print costs are up 18%.\n\n"
        "On 14 April 2003, Tim McCarthy wrote:\n> The Q1 figures are attached.\n"
    )
    kept, removed = strip_signature(body)
    assert "Print costs are up" in kept
    assert "Q1 figures are attached" in removed


def test_original_message_marker_is_split_off():
    body = "See below.\n\n-----Original Message-----\nFrom: Bob\nSubject: Quote\n"
    kept, removed = strip_signature(body)
    assert kept == "See below."
    assert "Original Message" in removed


def test_body_with_no_signature_is_untouched():
    body = "Just a short note about Thursday."
    kept, removed = strip_signature(body)
    assert kept == body
    assert removed is None


def test_a_body_that_is_only_a_signature_is_kept():
    """Cutting at position 2 would leave an empty message."""
    body = "-- \nTim\n"
    kept, _ = strip_signature(body)
    assert "Tim" in kept


# --- snippet --------------------------------------------------------------


def test_snippet_cuts_at_a_word_boundary():
    text = " ".join(f"word{i}" for i in range(100))
    out = snippet(text, 50)
    assert len(out) <= 51
    assert out.endswith("…")
    assert not out.rstrip("…").endswith("wor")


def test_short_text_is_not_truncated():
    assert snippet("short", 100) == "short"
