"""The mail readers: .eml, .mbox, .msg, .olm, and the two written from scratch."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from recall.models import Kind, Role
from recall.parsers.base import parser_for
from recall.parsers.dbx import DbxParser
from recall.parsers.eml import (
    EmlParser,
    MboxParser,
    decode_header_value,
    raw_header_values,
)
from recall.parsers.mbx import MbxParser
from recall.parsers.olm import OlmParser
from tests.fixtures.generate import generate_eml, generate_mbox, generate_olm


@pytest.fixture
def mail(tmp_path: Path) -> Path:
    generate_eml(tmp_path)
    generate_mbox(tmp_path)
    return tmp_path


def parse(path: Path, parser_cls=EmlParser, **kwargs) -> list:
    with parser_cls(path, **kwargs) as parser:
        return list(parser.parse())


# --- .eml -----------------------------------------------------------------


def test_a_plain_message_reads(mail: Path):
    item = parse(mail / "001-plain.eml")[0]
    assert item.kind == Kind.MESSAGE
    assert item.subject == "Quarterly figures"
    assert "Q1 figures are attached" in item.body_text
    assert item.occurred.utc == "2003-04-14T09:30:00Z"
    assert item.parse_confidence == 1.0


def test_sender_and_recipients_get_their_roles(mail: Path):
    item = parse(mail / "001-plain.eml")[0]
    roles = {(p.role, p.address) for p in item.participants}
    assert (Role.FROM, "tmccarthy@contractmktg.com") in roles
    assert (Role.TO, "mobrien@contractmktg.com") in roles


def test_display_names_are_kept(mail: Path):
    item = parse(mail / "001-plain.eml")[0]
    sender = next(p for p in item.participants if p.role == Role.FROM)
    assert sender.display_name == "Tim McCarthy"


def test_cp1252_declared_as_latin1_decodes_correctly(mail: Path):
    """Byte 0x92 is an apostrophe in cp1252 and a control character in Latin-1.

    Trusting the declared charset would mangle every apostrophe in twenty years
    of Windows mail.
    """
    item = parse(mail / "003-cp1252.eml")[0]
    assert "£4,500" in item.body_text
    assert "Fitzgerald’s" in item.body_text
    assert item.parse_confidence == 1.0, "a correctly declared charset is certain"


def test_mojibake_is_repaired_and_marked_as_inferred(mail: Path):
    item = parse(mail / "004-mojibake.eml")[0]
    assert "£4,500" in item.body_text
    assert "Fitzgerald’s" in item.body_text
    assert "â€" not in item.body_text
    assert item.parse_confidence < 1.0, "repair is an improvement, not a certainty"


def test_the_repaired_copy_matches_the_original(mail: Path):
    """The whole point: the same message stored two ways reads the same."""
    original = parse(mail / "003-cp1252.eml")[0].body_text.strip()
    migrated = parse(mail / "004-mojibake.eml")[0].body_text.strip()
    assert original == migrated


def test_a_message_with_no_id_still_reads(mail: Path):
    item = parse(mail / "005-no-message-id.eml")[0]
    assert item.internet_message_id is None
    assert item.subject == "Delivery Tuesday"


def test_two_hundred_recipients(mail: Path):
    item = parse(mail / "006-200-recipients.eml")[0]
    recipients = [p for p in item.participants if p.role == Role.TO]
    assert len(recipients) == 200


def test_html_body_is_converted_and_the_html_kept(mail: Path):
    item = parse(mail / "007-html.eml")[0]
    assert item.body_html and "<b>" in item.body_html
    assert item.body_text and "<b>" not in item.body_text
    assert "ready" in item.body_text


def test_attachments_are_read(mail: Path):
    item = parse(mail / "008-attachment-a.eml")[0]
    assert len(item.attachments) == 1
    attachment = item.attachments[0]
    assert attachment.filename == "invoice-4471.pdf"
    assert attachment.data and attachment.data.startswith(b"%PDF")
    assert attachment.read_error is None


def test_a_unicode_filename_survives(mail: Path):
    item = parse(mail / "010-unicode-attachment.eml")[0]
    assert item.attachments[0].filename == "devis-été-2012-Édition.pdf"


def test_a_message_with_no_date_goes_to_undated(mail: Path):
    """Never assign epoch zero, never assign the parse date."""
    item = parse(mail / "011-no-date.eml")[0]
    assert item.occurred.utc is None
    assert any(code == "no_date" for code, _ in item.notes)


def test_reply_headers_are_kept_for_threading(mail: Path):
    item = parse(mail / "002-reply.eml")[0]
    assert item.in_reply_to
    assert item.references


def test_raw_headers_are_kept(mail: Path):
    item = parse(mail / "001-plain.eml")[0]
    assert "Subject: Quarterly figures" in item.raw_headers
    assert "From:" in item.raw_headers


def test_an_empty_file_is_reported_not_crashed(tmp_path: Path):
    p = tmp_path / "empty.eml"
    p.write_bytes(b"")
    with EmlParser(p) as parser:
        assert list(parser.parse()) == []
        assert parser.outcome.error


def test_the_source_file_is_never_modified(mail: Path):
    path = mail / "001-plain.eml"
    before = path.read_bytes()
    parse(path)
    assert path.read_bytes() == before


# --- headers --------------------------------------------------------------


def test_rfc2047_encoded_header_decodes():
    text, confidence = decode_header_value("=?utf-8?B?UsOpdW5pb24=?=")
    assert text == "Réunion"
    assert confidence == 1.0


def test_a_raw_8bit_header_decodes():
    """A 1997 mailer wrote the bytes with no encoding marker at all."""
    text, confidence = decode_header_value("Café du Nord".encode("cp1252"))
    assert text == "Café du Nord"
    assert confidence < 1.0


def test_a_plain_ascii_header_is_certain():
    text, confidence = decode_header_value("Quarterly figures")
    assert text == "Quarterly figures"
    assert confidence == 1.0


def test_a_missing_header_is_none():
    assert decode_header_value(None) == (None, 1.0)


# --- .mbox ----------------------------------------------------------------


def test_mbox_reads_every_message(mail: Path):
    items = parse(mail / "archive-2006.mbox", MboxParser)
    assert len(items) == 3


def test_mbox_counts_what_it_claims(mail: Path):
    with MboxParser(mail / "archive-2006.mbox") as parser:
        items = list(parser.parse())
        assert parser.outcome.claimed_count == 3
        assert parser.outcome.yielded_count == len(items)
        assert parser.outcome.estimated_loss == 0


def test_mbox_sample_limit(mail: Path):
    assert len(parse(mail / "archive-2006.mbox", MboxParser, sample_limit=2)) == 2


# --- .olm -----------------------------------------------------------------


def test_olm_reads_the_zip_of_xml(tmp_path: Path):
    generate_olm(tmp_path)
    items = parse(tmp_path / "mac-outlook-2011.olm", OlmParser)
    assert len(items) == 1
    item = items[0]
    assert item.subject == "Mac side of the Kelly account"
    assert item.internet_message_id == "mac-2011-05-17@contractmktg.com"
    assert item.occurred.utc == "2011-05-17T10:22:00Z"


def test_olm_keeps_the_folder_path(tmp_path: Path):
    """A message filed under Clients/Fitzgerald says something the body does not."""
    generate_olm(tmp_path)
    item = parse(tmp_path / "mac-outlook-2011.olm", OlmParser)[0]
    assert "Inbox" in item.folder_path


def test_olm_participants(tmp_path: Path):
    generate_olm(tmp_path)
    item = parse(tmp_path / "mac-outlook-2011.olm", OlmParser)[0]
    addresses = {(p.role, p.address) for p in item.participants}
    assert (Role.FROM, "tmccarthy@contractmktg.com") in addresses
    assert (Role.TO, "mobrien@contractmktg.com") in addresses


def test_a_file_that_is_not_a_zip_is_reported(tmp_path: Path):
    p = tmp_path / "broken.olm"
    p.write_bytes(b"this is not a zip file at all")
    with OlmParser(p) as parser:
        assert list(parser.parse()) == []
        assert parser.outcome.error
        assert any(f[0] == "read_failure" for f in parser.outcome.findings)


# --- .dbx: found and listed, never read ------------------------------------
#
# Recall had a from-scratch DBX reader and it was withdrawn. It mistook a
# message's sender-name attribute for the pointer to the message body, so every
# message carrying a sender name - every real message - was dropped, and the
# loss was reported as damage to the user's file. Its tests passed because the
# fixtures were built from the reader's own wrong assumptions: every info block
# they wrote had exactly one attribute, which no real message has.
#
# These tests pin the behaviour that replaced it: the file is still found and
# still accounted for, and nothing is taken out of it.


def write_dbx(path: Path) -> Path:
    """Bytes that start like a real Outlook Express message database."""
    path.write_bytes(b"\xcf\xad\x12\xfe\xc5\xfdto" + b"\x00" * 4096)
    return path


def test_dbx_is_still_recognised_as_a_file_recall_knows(tmp_path: Path):
    """Removing the reader must not make .dbx an unknown extension.

    A file with no parser at all is recorded as 'skipped' with a generic note.
    This format gets a parser that explains itself, so the user is told what
    the file is and what they can do about it.
    """
    assert parser_for(write_dbx(tmp_path / "Inbox.dbx")) is DbxParser


def test_dbx_yields_nothing(tmp_path: Path):
    assert parse(write_dbx(tmp_path / "Inbox.dbx"), DbxParser) == []


def test_dbx_says_it_will_not_read_the_file(tmp_path: Path):
    p = write_dbx(tmp_path / "Inbox.dbx")
    with DbxParser(p) as parser:
        assert list(parser.parse()) == []
        assert parser.outcome.error
        assert "not read" in parser.outcome.error

        codes = [f[0] for f in parser.outcome.findings]
        assert "read_failure" in codes

        finding = next(f for f in parser.outcome.findings if f[0] == "read_failure")
        assert "Inbox.dbx" in finding[2]
        # The detail has to tell the user how to get their mail out, because
        # Recall is not going to do it for them.
        assert ".pst" in finding[3]


def test_dbx_does_not_invent_a_loss_it_cannot_measure(tmp_path: Path):
    """claimed_count stays unknown rather than being guessed from the header.

    The old reader's header arithmetic was never validated against a real file.
    An estimated_loss computed from it would be a made-up number wearing the
    costume of a measurement.
    """
    with DbxParser(write_dbx(tmp_path / "Inbox.dbx")) as parser:
        list(parser.parse())
        assert parser.outcome.claimed_count is None
        assert parser.outcome.estimated_loss is None


def test_dbx_is_quiet_on_a_calendar_only_run(tmp_path: Path):
    """A phase-1 calendar run should not raise a problem about a mail file."""
    with DbxParser(write_dbx(tmp_path / "Inbox.dbx")) as parser:
        assert list(parser.parse(frozenset({Kind.EVENT}))) == []
        assert parser.outcome.findings == []


def test_dbx_is_never_written_to(tmp_path: Path):
    p = write_dbx(tmp_path / "Inbox.dbx")
    before = p.read_bytes()
    parse(p, DbxParser)
    assert p.read_bytes() == before


# --- .mbx -----------------------------------------------------------------


SAMPLE = (
    b"From: bob@northstarprint.co.uk\r\n"
    b"To: tim@contractmktg.com\r\n"
    b"Subject: Outlook Express days\r\n"
    b"Date: Tue, 3 Nov 1998 14:22:00 +0000\r\n"
    b"Message-ID: <oe-1998@northstarprint.co.uk>\r\n"
    b"\r\n"
    b"Sent from Outlook Express in 1998.\r\n"
)


def build_oe4_mbx(messages: list[bytes]) -> bytes:
    out = bytearray(0x54)
    out[0:4] = b"JMF9"
    struct.pack_into("<I", out, 0x08, len(messages))
    for i, raw in enumerate(messages):
        record = bytearray(16)
        record[0:4] = b"JMF6"
        struct.pack_into("<I", record, 0x04, 16 + len(raw))
        struct.pack_into("<I", record, 0x08, i)
        out += record + raw
    struct.pack_into("<I", out, 0x04, len(out))
    return bytes(out)


def test_mbx_reads_an_outlook_express_4_mailbox(tmp_path: Path):
    p = tmp_path / "Inbox.mbx"
    p.write_bytes(build_oe4_mbx([SAMPLE, SAMPLE.replace(b"1998@", b"1999@")]))
    items = parse(p, MbxParser)
    assert len(items) == 2
    assert items[0].subject == "Outlook Express days"


def test_mbx_claimed_count_comes_from_the_header(tmp_path: Path):
    p = tmp_path / "Inbox.mbx"
    p.write_bytes(build_oe4_mbx([SAMPLE] * 3))
    with MbxParser(p) as parser:
        list(parser.parse())
        assert parser.outcome.claimed_count == 3


def test_mbx_resynchronises_past_a_damaged_record(tmp_path: Path):
    """The messages after a damaged record are usually fine, and worth having."""
    good = build_oe4_mbx([SAMPLE, SAMPLE.replace(b"1998@", b"1999@")])
    damaged = bytearray(good)
    damaged[0x54:0x58] = b"XXXX"            # break the first record marker
    p = tmp_path / "damaged.mbx"
    p.write_bytes(bytes(damaged))

    with MbxParser(p) as parser:
        items = list(parser.parse())
        assert len(items) == 1, "the second message is recovered"
        assert any(f[0] == "read_failure" for f in parser.outcome.findings)


def test_mbx_reads_a_eudora_mailbox(tmp_path: Path):
    p = tmp_path / "In.mbx"
    p.write_bytes(
        b"From ???@??? Tue Nov 03 14:22:00 1998\r\n" + SAMPLE
        + b"From ???@??? Wed Nov 04 09:00:00 1998\r\n"
        + SAMPLE.replace(b"1998@", b"1999@")
    )
    items = parse(p, MbxParser)
    assert len(items) == 2
    assert items[0].backend == "eudora"


def test_mbx_that_is_neither_format_is_reported(tmp_path: Path):
    p = tmp_path / "mystery.mbx"
    p.write_bytes(b"\x01\x02\x03\x04" + b"\x00" * 500)
    with MbxParser(p) as parser:
        assert list(parser.parse()) == []
        assert any(f[0] == "magic_mismatch" for f in parser.outcome.findings)


# --- .eml: headers written as bare 8-bit bytes -----------------------------
#
# decode_header_value has always handled bytes correctly when handed them
# directly - test_a_raw_8bit_header_decodes proves it and always did. The bug
# was that it never was handed them: the stdlib decodes a header on the way
# out, replacing undecodable bytes with U+FFFD, and by the time the old code
# looked at the value the characters were already gone. Worse, the result came
# back at confidence 1.0, so nothing on the Problems screen said so.
#
# These tests go through the parser, not the function, because that is where
# the bug lived.

CP1252_SUBJECT = (
    b"From: bob@north.co.uk\r\n"
    b"To: tim@cm.com\r\n"
    b"Subject: Caf\xe9 meeting \x93quoted\x94\r\n"
    b"Date: Tue, 3 Nov 1998 14:22:00 +0000\r\n"
    b"\r\n"
    b"Body.\r\n"
)


def test_an_undeclared_8bit_subject_is_decoded_from_the_source_bytes(tmp_path: Path):
    p = tmp_path / "cp1252.eml"
    p.write_bytes(CP1252_SUBJECT)
    item = parse(p)[0]
    assert item.subject == "Café meeting “quoted”"
    assert "�" not in item.subject


def test_a_guessed_subject_is_not_reported_as_certain(tmp_path: Path):
    """The characters were guessed from the bytes, and the user is told so."""
    p = tmp_path / "cp1252.eml"
    p.write_bytes(CP1252_SUBJECT)
    item = parse(p)[0]
    assert item.parse_confidence < 1.0
    assert any(note[0] == "low_confidence_text" for note in item.notes)


def test_an_undeclared_8bit_display_name_is_decoded(tmp_path: Path):
    """A display name is a header too, and used to be lost the same way."""
    p = tmp_path / "name.eml"
    p.write_bytes(
        b"From: Bj\xf6rn Nordstr\xf6m <bjorn@north.co.uk>\r\n"
        b"To: tim@cm.com\r\n"
        b"Subject: Plain ascii subject\r\n"
        b"Date: Tue, 3 Nov 1998 14:22:00 +0000\r\n"
        b"\r\n"
        b"Body.\r\n"
    )
    item = parse(p)[0]
    sender = next(i for i in item.participants if i.role == Role.FROM)
    assert sender.display_name == "Björn Nordström"
    # And the uncertainty counts, rather than being dropped on the floor.
    assert item.parse_confidence < 1.0


def test_a_declared_charset_still_wins_and_stays_certain(mail: Path):
    """The fallback must only fire when the characters were actually lost."""
    item = parse(mail / "001-plain.eml")[0]
    assert item.parse_confidence == 1.0


def test_folded_headers_are_unfolded_in_the_source_bytes():
    values = raw_header_values(
        b"Subject: a subject that\r\n continues on the next line\r\n"
        b"To: a@b.com\r\n\r\nbody\r\n"
    )
    assert values["subject"] == [b"a subject that continues on the next line"]
    assert values["to"] == [b"a@b.com"]


def test_repeated_headers_keep_every_value_in_order():
    values = raw_header_values(b"Received: one\r\nReceived: two\r\n\r\nbody\r\n")
    assert values["received"] == [b"one", b"two"]


# --- .eml: an attached email belongs to whoever sent it --------------------
#
# message/rfc822 reports is_multipart() as true, so the old code's
# `if part.is_multipart(): continue` skipped the container - but Message.walk
# had already descended into it and was yielding the attached message's parts
# as though they were this message's own. First text/plain won, so whether the
# body was right depended on the order the sending mailer wrote the parts in.
# The attached mail was also never recorded as an attachment at all.

INNER = (
    b"From: old@sender.com\r\n"
    b"To: bob@north.co.uk\r\n"
    b"Subject: The original message\r\n"
    b"Date: Mon, 2 Nov 1998 09:00:00 +0000\r\n"
    b"\r\n"
    b"The forwarded body, which belongs to old@sender.com.\r\n"
)

COVERING_NOTE = (
    b"--B\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"Tim - my own covering note.\r\n"
)

ATTACHED_EMAIL = (
    b"--B\r\n"
    b"Content-Type: message/rfc822\r\n"
    b'Content-Disposition: attachment; filename="original.eml"\r\n'
    b"\r\n" + INNER
)

FORWARD_HEAD = (
    b"From: bob@north.co.uk\r\n"
    b"To: tim@cm.com\r\n"
    b"Subject: FW: please see below\r\n"
    b"Date: Tue, 3 Nov 1998 14:22:00 +0000\r\n"
    b'Content-Type: multipart/mixed; boundary="B"\r\n'
    b"\r\n"
)


def write_forward(path: Path, attachment_first: bool) -> Path:
    parts = (
        ATTACHED_EMAIL + COVERING_NOTE
        if attachment_first
        else COVERING_NOTE + ATTACHED_EMAIL
    )
    path.write_bytes(FORWARD_HEAD + parts + b"--B--\r\n")
    return path


@pytest.mark.parametrize("attachment_first", [True, False])
def test_a_forwarded_email_does_not_replace_the_covering_note(
    tmp_path: Path, attachment_first: bool
):
    """Whichever order the parts arrive in, the body is the sender's own."""
    p = write_forward(tmp_path / "fw.eml", attachment_first)
    item = parse(p)[0]
    assert item.body_text == "Tim - my own covering note."
    assert "forwarded body" not in (item.body_text or "")


@pytest.mark.parametrize("attachment_first", [True, False])
def test_an_attached_email_is_recorded_as_an_attachment(
    tmp_path: Path, attachment_first: bool
):
    p = write_forward(tmp_path / "fw.eml", attachment_first)
    item = parse(p)[0]
    assert len(item.attachments) == 1

    attachment = item.attachments[0]
    assert attachment.filename == "original.eml"
    assert attachment.mime_type == "message/rfc822"
    assert attachment.read_error is None
    # The attached message is kept whole, so it can be read on its own later.
    assert b"The original message" in attachment.data


def test_an_attached_email_does_not_donate_its_participants(tmp_path: Path):
    """The inner sender must not end up as a participant of the outer message.

    This is the part that made the old bug more than a body mix-up: the
    forwarded text was filed under the forwarding sender's name and date.
    """
    p = write_forward(tmp_path / "fw.eml", attachment_first=True)
    item = parse(p)[0]
    addresses = {i.address for i in item.participants}
    assert "old@sender.com" not in addresses
    assert "bob@north.co.uk" in addresses


def test_a_normal_attachment_still_reads_beside_an_attached_email(tmp_path: Path):
    """The narrower walk must not lose ordinary attachments."""
    p = tmp_path / "both.eml"
    p.write_bytes(
        FORWARD_HEAD
        + COVERING_NOTE
        + ATTACHED_EMAIL
        + b"--B\r\n"
        b'Content-Type: application/pdf; name="report.pdf"\r\n'
        b'Content-Disposition: attachment; filename="report.pdf"\r\n'
        b"\r\n"
        b"%PDF-1.4 not really a pdf\r\n"
        b"--B--\r\n"
    )
    item = parse(p)[0]
    kinds = sorted(a.mime_type for a in item.attachments)
    assert kinds == ["application/pdf", "message/rfc822"]
    assert item.body_text == "Tim - my own covering note."
