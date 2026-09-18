"""The mail readers: .eml, .mbox, .msg, .olm, and the two written from scratch."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from recall.models import Kind, Role
from recall.parsers.dbx import DBX_MAGIC, MESSAGE_DB_MARKER, DbxError, DbxFile, DbxParser
from recall.parsers.eml import EmlParser, MboxParser, decode_header_value
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


# --- .dbx: the structural reader against hand-built bytes ------------------


def build_dbx(messages: list[bytes], *, corrupt_index: bool = False) -> bytes:
    """A minimal but structurally valid Outlook Express 5/6 message database.

    Built by hand because there is no library to generate one, which is the
    same reason the reader had to be written from scratch.
    """
    header = bytearray(0x24BC)
    header[0:4] = DBX_MAGIC
    header[4:8] = MESSAGE_DB_MARKER

    out = bytearray(header)

    # Lay out the message data blocks first so their offsets are known.
    data_offsets = []
    for raw in messages:
        offset = len(out)
        data_offsets.append(offset)
        block = bytearray(0x10)
        struct.pack_into("<I", block, 0x00, offset)
        struct.pack_into("<I", block, 0x04, 0)          # no next block
        struct.pack_into("<I", block, 0x08, len(raw))
        out += block + raw

    # One info block per message, each pointing at its data.
    info_offsets = []
    for data_offset in data_offsets:
        offset = len(out)
        info_offsets.append(offset)
        info = bytearray(0x0C + 4)
        struct.pack_into("<I", info, 0x00, offset)
        info[0x0A] = 1                                   # one attribute
        info[0x0C] = 0x84                                # message text
        info[0x0D:0x10] = data_offset.to_bytes(3, "little")
        out += info

    # One index node listing them.
    node_offset = len(out)
    node = bytearray(0x20C)
    struct.pack_into("<I", node, 0x00, node_offset)
    struct.pack_into("<H", node, 0x10, len(info_offsets))
    for i, info_offset in enumerate(info_offsets):
        entry = 0x18 + i * 12
        struct.pack_into("<I", node, entry, info_offset)
    out += node

    struct.pack_into("<I", out, 0x1C, 0xDEADBEEF if corrupt_index else node_offset)
    struct.pack_into("<I", out, 0x24, len(messages))
    return bytes(out)


SAMPLE = (
    b"From: bob@northstarprint.co.uk\r\n"
    b"To: tim@contractmktg.com\r\n"
    b"Subject: Outlook Express days\r\n"
    b"Date: Tue, 3 Nov 1998 14:22:00 +0000\r\n"
    b"Message-ID: <oe-1998@northstarprint.co.uk>\r\n"
    b"\r\n"
    b"Sent from Outlook Express in 1998.\r\n"
)


def test_dbx_header_is_validated(tmp_path: Path):
    p = tmp_path / "bad.dbx"
    p.write_bytes(b"not a dbx file at all" + b"\x00" * 300)
    with DbxParser(p) as parser:
        assert list(parser.parse()) == []
        assert "does not start with the Outlook Express marker" in parser.outcome.error


def test_dbx_too_short_is_reported(tmp_path: Path):
    p = tmp_path / "tiny.dbx"
    p.write_bytes(DBX_MAGIC + b"\x00" * 10)
    with DbxParser(p) as parser:
        list(parser.parse())
        assert "too short" in parser.outcome.error


def test_dbx_reads_a_hand_built_file(tmp_path: Path):
    p = tmp_path / "folder.dbx"
    p.write_bytes(build_dbx([SAMPLE]))
    items = parse(p, DbxParser)
    assert len(items) == 1
    assert items[0].subject == "Outlook Express days"
    assert items[0].occurred.utc == "1998-11-03T14:22:00Z"


def test_dbx_reads_several_messages(tmp_path: Path):
    p = tmp_path / "folder.dbx"
    messages = [
        SAMPLE.replace(b"<oe-1998@", b"<oe-%d@" % i) for i in range(5)
    ]
    p.write_bytes(build_dbx(messages))
    assert len(parse(p, DbxParser)) == 5


def test_dbx_claimed_count_comes_from_the_header(tmp_path: Path):
    p = tmp_path / "folder.dbx"
    p.write_bytes(build_dbx([SAMPLE, SAMPLE.replace(b"1998@", b"1999@")]))
    with DbxParser(p) as parser:
        list(parser.parse())
        assert parser.outcome.claimed_count == 2


def test_dbx_a_corrupt_index_is_reported_with_the_loss(tmp_path: Path):
    """A DBX with a damaged index was notorious. The answer is to say so."""
    p = tmp_path / "corrupt.dbx"
    p.write_bytes(build_dbx([SAMPLE, SAMPLE], corrupt_index=True))
    with DbxParser(p) as parser:
        items = list(parser.parse())
        assert items == []
        assert parser.outcome.claimed_count == 2
        assert any(f[0] == "read_failure" for f in parser.outcome.findings)


def test_dbx_a_file_with_no_messages_is_not_an_error(tmp_path: Path):
    """Folders.dbx is a real DBX that holds no mail."""
    data = bytearray(build_dbx([]))
    data[4:8] = b"\x00\x00\x00\x00"       # a different file-type marker
    p = tmp_path / "Folders.dbx"
    p.write_bytes(bytes(data))
    with DbxParser(p) as parser:
        assert list(parser.parse()) == []
        assert parser.outcome.error is None


def test_dbxfile_refuses_an_implausible_record_count():
    data = bytearray(0x300)
    data[0:4] = DBX_MAGIC
    data[4:8] = MESSAGE_DB_MARKER
    struct.pack_into("<I", data, 0x24, 10_000_000)
    with pytest.raises(DbxError, match="not plausible"):
        DbxFile(bytes(data))


# --- .mbx -----------------------------------------------------------------


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
