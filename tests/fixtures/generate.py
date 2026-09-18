"""Synthetic source files, including the deliberately awful ones.

Real PST files cannot be shipped with a test suite, and the archive this program
is for does not exist yet, so the corpus is generated. Spec section 13 lists the
nasty cases it must contain, and every one of them is here for a reason drawn
from real mail:

  cp1252 bytes            a 1997 message from a Windows mailer
  mojibake                the same message after a bad migration
  no timezone             a floating iCalendar time
  no Message-ID           an old client that did not emit one
  recurring events        a weekly meeting held for years
  200 recipients          an all-staff mail
  the same message thrice the overlapping-backups problem, the whole point
  identical attachments   the same PDF sent under four names
  Unicode filenames       an accented supplier name
  a zero-byte file        a failed copy

and, separately, the deliberate-damage set for the integrity engine: a store
that lies about its type, a mailbox with February 2003 removed, folders that
claim a year they do not contain, one address wearing twelve names, one mailbox
that is a strict subset of another, undated and 1961-dated messages.

``generate_all`` writes the clean set. ``generate_damaged`` writes the broken
set separately, so a test can assert that clean input produces zero findings.
"""

from __future__ import annotations

import hashlib
import zipfile
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

UTC = timezone.utc

# A cast of correspondents with enough history to be worth searching.
PEOPLE = [
    ("Tim McCarthy", "tmccarthy@contractmktg.com"),
    ("Margaret O'Brien", "mobrien@contractmktg.com"),
    ("Declan Walsh", "declan.walsh@fitzgerald-partners.ie"),
    ("Aoife Ní Bhraonáin", "aoife@studiolibre.fr"),
    ("Bob Jenkins", "bob.jenkins@northstarprint.co.uk"),
    ("accounts", "accounts@northstarprint.co.uk"),
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write(path: Path, data: bytes | str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_bytes(data.encode(encoding))
    else:
        path.write_bytes(data)
    return path


def _eml(
    *,
    subject: str,
    sender: tuple[str, str],
    to: list[tuple[str, str]],
    date: datetime | None,
    body: str,
    message_id: str | None = "auto",
    cc: list[tuple[str, str]] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    charset: str = "utf-8",
    attachments: list[tuple[str, bytes, str]] | None = None,
    html: str | None = None,
) -> bytes:
    """One RFC 5322 message, built with the stdlib so it is genuinely valid."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{sender[0]} <{sender[1]}>"
    msg["To"] = ", ".join(f"{n} <{a}>" for n, a in to)
    if cc:
        msg["Cc"] = ", ".join(f"{n} <{a}>" for n, a in cc)
    if date is not None:
        msg["Date"] = format_datetime(date)
    if message_id == "auto":
        digest = hashlib.sha1(
            f"{subject}{sender[1]}{date}".encode("utf-8")
        ).hexdigest()[:20]
        msg["Message-ID"] = f"<{digest}@contractmktg.com>"
    elif message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)

    msg.set_content(body, charset=charset)
    if html:
        msg.add_alternative(html, subtype="html")

    for filename, data, mime in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    return msg.as_bytes()


def _ics_event(
    *,
    uid: str | None,
    summary: str,
    start: str,
    end: str | None = None,
    location: str | None = None,
    organizer: tuple[str, str] | None = None,
    attendees: list[tuple[str, str, str]] | None = None,
    rrule: str | None = None,
    description: str | None = None,
    all_day: bool = False,
) -> str:
    lines = ["BEGIN:VEVENT"]
    if uid:
        lines.append(f"UID:{uid}")
    lines.append(f"SUMMARY:{summary}")
    if all_day:
        lines.append(f"DTSTART;VALUE=DATE:{start}")
        if end:
            lines.append(f"DTEND;VALUE=DATE:{end}")
    else:
        lines.append(f"DTSTART:{start}")
        if end:
            lines.append(f"DTEND:{end}")
    if location:
        lines.append(f"LOCATION:{location}")
    if description:
        lines.append(f"DESCRIPTION:{description}")
    if organizer:
        lines.append(f"ORGANIZER;CN={organizer[0]}:mailto:{organizer[1]}")
    for name, address, status in attendees or []:
        lines.append(
            f"ATTENDEE;CN={name};PARTSTAT={status}:mailto:{address}"
        )
    if rrule:
        lines.append(f"RRULE:{rrule}")
    lines.append("END:VEVENT")
    return "\n".join(lines)


def _calendar(events: list[str], prodid: str = "-//Recall Test//EN") -> str:
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:" + prodid + "\n"
        + "\n".join(events)
        + "\nEND:VCALENDAR\n"
    )


# ---------------------------------------------------------------------------
# The clean corpus
# ---------------------------------------------------------------------------


def generate_all(out: Path) -> Path:
    """The full corpus: everything valid, including the awkward-but-legitimate.

    "Awkward but legitimate" means a floating iCalendar time, a message with no
    Message-ID, cp1252 bytes, a zero-byte file. These are not damage - they are
    what real archives contain - and several of them correctly raise findings.
    Use ``generate_spotless`` when a test needs input that must produce none.
    """
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)

    generate_eml(out / "mail")
    generate_mbox(out / "mail")
    generate_ics(out / "calendar")
    generate_vcs(out / "calendar")
    generate_vcf(out / "contacts")
    generate_olm(out / "mac")
    generate_edge_cases(out / "edge")
    return out


def generate_spotless(out: Path) -> Path:
    """Files with nothing whatever wrong with them.

    Spec section 13 requires proof that clean input produces zero findings - a
    system that cries wolf is as useless as one that stays silent. Every file
    here has a Message-ID or a UID, a fully specified timezone, a plausible
    size, and a date inside the believable range.
    """
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    tim, margaret, declan, aoife, bob, _ = PEOPLE

    _write(out / "correspondence.mbox", b"".join(
        b"From tim@contractmktg.com Mon Jan  1 00:00:00 2001\n" + _eml(
            subject=f"Kelly account, week {week}",
            sender=tim, to=[margaret], cc=[declan],
            date=datetime(2005, 1 + (week % 12), 1 + (week % 27), 9, 0, tzinfo=UTC),
            body=f"Week {week} correspondence about the Kelly account.\n",
            message_id=f"<spotless-{week:03d}@contractmktg.com>",
        ) + b"\n"
        for week in range(1, 25)
    ))

    _write(out / "meetings.ics", _calendar([
        _ics_event(
            uid=f"spotless-mtg-{i}@contractmktg.com",
            summary=f"Planning meeting {i}",
            start=f"2005{1 + (i % 12):02d}{1 + (i % 27):02d}T140000Z",
            end=f"2005{1 + (i % 12):02d}{1 + (i % 27):02d}T150000Z",
            location="Boardroom",
            organizer=tim,
            attendees=[(margaret[0], margaret[1], "ACCEPTED")],
        )
        for i in range(1, 13)
    ]))

    _write(out / "contacts.vcf", (
        "BEGIN:VCARD\r\nVERSION:3.0\r\n"
        "FN:Declan Walsh\r\nN:Walsh;Declan;;;\r\n"
        "ORG:Fitzgerald Partners\r\n"
        "EMAIL:declan.walsh@fitzgerald-partners.ie\r\n"
        "END:VCARD\r\n"
    ))

    return out


def generate_eml(out: Path) -> list[Path]:
    """Individual messages, including the awkward encodings."""
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    tim, margaret, declan, aoife, bob, accounts = PEOPLE

    # A plain, unremarkable message - the control case.
    written.append(_write(out / "001-plain.eml", _eml(
        subject="Quarterly figures",
        sender=tim, to=[margaret],
        date=datetime(2003, 4, 14, 9, 30, tzinfo=UTC),
        body="Margaret,\n\nThe Q1 figures are attached. Call me when you have "
             "read them.\n\nTim\n",
    )))

    # A reply, so threading has something to reconstruct.
    written.append(_write(out / "002-reply.eml", _eml(
        subject="RE: Quarterly figures",
        sender=margaret, to=[tim],
        date=datetime(2003, 4, 14, 11, 5, tzinfo=UTC),
        body="Tim,\n\nRead them. The print costs are up 18%. Let us talk "
             "Thursday.\n\nMargaret\n",
        in_reply_to=_message_id("Quarterly figures", tim[1],
                                datetime(2003, 4, 14, 9, 30, tzinfo=UTC)),
        references=[_message_id("Quarterly figures", tim[1],
                                datetime(2003, 4, 14, 9, 30, tzinfo=UTC))],
    )))

    # cp1252 bytes, correctly declared. A 1997 Windows mailer.
    cp1252_body = (
        "Tim,\r\n\r\nThe quote is £4,500 – that includes the "
        "Fitzgerald’s job.\r\n\r\nBob\r\n"
    )
    raw = _eml(
        subject="Quote for the Fitzgerald job",
        sender=bob, to=[tim],
        date=datetime(1997, 11, 3, 14, 22, tzinfo=UTC),
        body="placeholder",
        charset="utf-8",
    )
    raw = _rewrite_body_as_cp1252(raw, cp1252_body)
    written.append(_write(out / "003-cp1252.eml", raw))

    # The same message after a bad migration: UTF-8 read as cp1252.
    mojibake_body = (
        "Tim,\r\n\r\nThe quote is Â£4,500 â€“ that "
        "includes the Fitzgeraldâ€™s job.\r\n\r\nBob\r\n"
    )
    written.append(_write(out / "004-mojibake.eml", _eml(
        subject="Quote for the Fitzgerald job (migrated copy)",
        sender=bob, to=[tim],
        date=datetime(1997, 11, 3, 14, 22, tzinfo=UTC),
        body=mojibake_body,
        message_id="<mojibake-1997@northstarprint.co.uk>",
    )))

    # No Message-ID at all. The composite dedup key has to carry this one.
    written.append(_write(out / "005-no-message-id.eml", _eml(
        subject="Delivery Tuesday",
        sender=declan, to=[tim],
        date=datetime(1999, 6, 8, 8, 0, tzinfo=UTC),
        body="Tim - the pallets arrive Tuesday morning. Declan\n",
        message_id=None,
    )))

    # 200 recipients: an all-staff mail.
    many = [(f"Staff Member {i:03d}", f"staff{i:03d}@contractmktg.com") for i in range(200)]
    written.append(_write(out / "006-200-recipients.eml", _eml(
        subject="Office closed Friday",
        sender=margaret, to=many,
        date=datetime(2005, 12, 20, 16, 45, tzinfo=UTC),
        body="The office is closed this Friday. Back on the 3rd.\n",
    )))

    # HTML body, so the HTML-to-text conversion has real input.
    written.append(_write(out / "007-html.eml", _eml(
        subject="New brochure proof",
        sender=aoife, to=[tim, margaret],
        date=datetime(2011, 3, 2, 10, 15, tzinfo=UTC),
        body="The proof is ready.",
        html="<html><body><p>The proof is <b>ready</b>.</p>"
             "<p>See <a href='http://studiolibre.fr/proof'>the proof</a>.</p>"
             "<style>p{color:red}</style></body></html>",
    )))

    # Identical attachment contents under two different names.
    pdf = _fake_pdf("Invoice 4471")
    written.append(_write(out / "008-attachment-a.eml", _eml(
        subject="Invoice 4471",
        sender=accounts, to=[tim],
        date=datetime(2008, 2, 11, 9, 0, tzinfo=UTC),
        body="Invoice attached.\n",
        attachments=[("invoice-4471.pdf", pdf, "application/pdf")],
    )))
    written.append(_write(out / "009-attachment-b.eml", _eml(
        subject="FW: Invoice 4471",
        sender=tim, to=[margaret],
        date=datetime(2008, 2, 11, 9, 30, tzinfo=UTC),
        body="For the file.\n",
        attachments=[("Invoice 4471 (copy).pdf", pdf, "application/pdf")],
    )))

    # A Unicode filename, which is how a 1990s supplier name survives.
    written.append(_write(out / "010-unicode-attachment.eml", _eml(
        subject="Devis Studio Libre",
        sender=aoife, to=[tim],
        date=datetime(2012, 9, 4, 13, 20, tzinfo=UTC),
        body="Le devis est joint.\n",
        attachments=[
            ("devis-été-2012-Édition.pdf", _fake_pdf("Devis"), "application/pdf")
        ],
    )))

    # A message with no Date header at all - the Undated bucket.
    written.append(_write(out / "011-no-date.eml", _eml(
        subject="Undated note about the Kelly account",
        sender=tim, to=[margaret],
        date=None,
        body="No date on this one.\n",
        message_id="<undated-1@contractmktg.com>",
    )))

    return written


def _message_id(subject: str, sender: str, date: datetime) -> str:
    digest = hashlib.sha1(f"{subject}{sender}{date}".encode("utf-8")).hexdigest()[:20]
    return f"<{digest}@contractmktg.com>"


def _rewrite_body_as_cp1252(raw: bytes, body: str) -> bytes:
    """Replace a message's body with genuine cp1252 bytes and say so.

    EmailMessage.as_bytes() separates headers from body with a bare newline,
    not CRLF, so the split has to try both - getting this wrong appends the new
    body to the old one instead of replacing it.
    """
    for separator in (b"\r\n\r\n", b"\n\n"):
        head, found, _ = raw.partition(separator)
        if found:
            break
    else:  # pragma: no cover - a message always has a header/body separator
        raise ValueError("the generated message has no header/body separator")

    head = head.replace(b'charset="utf-8"', b'charset="iso-8859-1"')
    head = head.replace(
        b"Content-Transfer-Encoding: base64", b"Content-Transfer-Encoding: 8bit"
    )
    head = head.replace(
        b"Content-Transfer-Encoding: quoted-printable",
        b"Content-Transfer-Encoding: 8bit",
    )
    return head + b"\r\n\r\n" + body.encode("cp1252", errors="replace")


def _fake_pdf(title: str) -> bytes:
    """A small but structurally real PDF, so pypdf can open it."""
    content = f"BT /F1 24 Tf 72 720 Td ({title}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n"
        f"{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def generate_mbox(out: Path) -> Path:
    """A Unix mbox holding the same three messages three times over.

    This is the overlapping-backups case the whole dedup design exists for: the
    same message in a .eml, in this mbox, and in the "second backup" mbox.
    """
    out.mkdir(parents=True, exist_ok=True)
    tim, margaret, declan, _, bob, _ = PEOPLE

    messages = [
        _eml(subject="Quarterly figures", sender=tim, to=[margaret],
             date=datetime(2003, 4, 14, 9, 30, tzinfo=UTC),
             body="Margaret,\n\nThe Q1 figures are attached. Call me when you have "
                  "read them.\n\nTim\n"),
        _eml(subject="Site visit notes", sender=declan, to=[tim],
             date=datetime(2004, 7, 19, 15, 0, tzinfo=UTC),
             body="Notes from the site visit are below.\n"),
        _eml(subject="Printer contract", sender=bob, to=[tim, margaret],
             date=datetime(2006, 1, 30, 11, 45, tzinfo=UTC),
             body="Contract renewal for the year.\n"),
    ]

    body = b""
    for msg in messages:
        body += b"From tim@contractmktg.com Mon Jan  1 00:00:00 2001\n" + msg + b"\n"

    path = _write(out / "archive-2006.mbox", body)
    _write(out / "second-backup.mbox", body)   # the very same messages again
    return path


def generate_ics(out: Path) -> list[Path]:
    """Calendars, including a floating time and a recurring meeting."""
    out.mkdir(parents=True, exist_ok=True)
    tim, margaret, declan, aoife, bob, _ = PEOPLE
    written: list[Path] = []

    # Fully specified: UTC times, UID, organizer, attendees with responses.
    written.append(_write(out / "meetings-2003.ics", _calendar([
        _ics_event(
            uid="mtg-2003-04-17@contractmktg.com",
            summary="Review Q1 figures with Margaret",
            start="20030417T140000Z", end="20030417T153000Z",
            location="Boardroom, Pearse Street",
            organizer=tim,
            attendees=[
                (margaret[0], margaret[1], "ACCEPTED"),
                (declan[0], declan[1], "DECLINED"),
            ],
            description="Print costs up 18 per cent. Bring the Northstar quote.",
        ),
        _ics_event(
            uid="mtg-2003-05-02@contractmktg.com",
            summary="Northstar Print site visit",
            start="20030502T090000Z", end="20030502T170000Z",
            location="Northstar Print, Leeds",
            organizer=bob,
            attendees=[(tim[0], tim[1], "ACCEPTED")],
        ),
    ])))

    # A floating time: no Z, no TZID. The instant is genuinely unknown.
    written.append(_write(out / "floating-time.ics", _calendar([
        _ics_event(
            uid="floating-1998@contractmktg.com",
            summary="Dinner with the Fitzgeralds",
            start="19980612T193000",        # no zone, deliberately
            end="19980612T223000",
            location="Restaurant Patrick Guilbaud",
            organizer=tim,
        ),
    ])))

    # A weekly meeting held for eleven years: the master is stored, the
    # instances are not.
    written.append(_write(out / "recurring.ics", _calendar([
        _ics_event(
            uid="weekly-standup@contractmktg.com",
            summary="Monday morning meeting",
            start="20010108T090000Z", end="20010108T093000Z",
            location="Pearse Street",
            organizer=tim,
            attendees=[(margaret[0], margaret[1], "ACCEPTED")],
            rrule="FREQ=WEEKLY;BYDAY=MO;UNTIL=20120101T000000Z",
        ),
    ])))

    # An all-day event, and one with no UID at all.
    written.append(_write(out / "all-day-and-no-uid.ics", _calendar([
        _ics_event(
            uid="holiday-2007@contractmktg.com",
            summary="Office closed - Christmas",
            start="20071224", end="20071227", all_day=True,
        ),
        _ics_event(
            uid=None,
            summary="Dentist",
            start="20090914T103000Z", end="20090914T110000Z",
        ),
    ])))

    return written


def generate_vcs(out: Path) -> Path:
    """vCalendar 1.0 - quoted-printable, positional RRULE, no timezone."""
    out.mkdir(parents=True, exist_ok=True)
    content = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:1.0\r\n"
        "PRODID:-//Microsoft Corporation//Schedule+ 7.0 MIMEDIR//EN\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:schedplus-1996-0042\r\n"
        "SUMMARY;ENCODING=QUOTED-PRINTABLE;CHARSET=ISO-8859-1:"
        "R=E9union avec Aoife\r\n"
        "DTSTART:19960923T140000\r\n"
        "DTEND:19960923T153000\r\n"
        "LOCATION;ENCODING=QUOTED-PRINTABLE:Bureau, 12 rue de l=27=C9glise\r\n"
        "DESCRIPTION;ENCODING=QUOTED-PRINTABLE:Premi=E8re r=E9union.\r\n"
        "ORGANIZER:MAILTO:tmccarthy@contractmktg.com\r\n"
        "ATTENDEE;STATUS=ACCEPTED:MAILTO:aoife@studiolibre.fr\r\n"
        "PRIORITY:1\r\n"
        "CLASS:PRIVATE\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:schedplus-1996-0043\r\n"
        "SUMMARY:Weekly sales meeting\r\n"
        "DTSTART:19960902T100000\r\n"
        "DTEND:19960902T110000\r\n"
        "RRULE:W1 MO #52\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    return _write(out / "schedule-plus-1996.vcs", content.encode("cp1252"))


def generate_vcf(out: Path) -> Path:
    """vCards, including one with several addresses and accented characters."""
    out.mkdir(parents=True, exist_ok=True)
    cards = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\n"
        "FN:Margaret O'Brien\r\nN:O'Brien;Margaret;;;\r\n"
        "ORG:Contract Marketing Ltd.;Accounts\r\n"
        "TITLE:Finance Director\r\n"
        "EMAIL;TYPE=WORK:mobrien@contractmktg.com\r\n"
        "EMAIL;TYPE=HOME:margaret.obrien@eircom.net\r\n"
        "TEL;TYPE=WORK,VOICE:+353 1 555 0101\r\n"
        "TEL;TYPE=CELL:+353 87 555 0199\r\n"
        "ADR;TYPE=WORK:;;14 Pearse Street;Dublin;;D02 XY45;Ireland\r\n"
        "END:VCARD\r\n"
        "BEGIN:VCARD\r\nVERSION:3.0\r\n"
        "FN:Aoife Ní Bhraonáin\r\nN:Ní Bhraonáin;Aoife;;;\r\n"
        "ORG:Studio Libre\r\n"
        "EMAIL:aoife@studiolibre.fr\r\n"
        "TEL;TYPE=WORK:+33 1 42 00 00 00\r\n"
        "END:VCARD\r\n"
        "BEGIN:VCARD\r\nVERSION:2.1\r\n"
        "FN:Bob Jenkins\r\nN:Jenkins;Bob;;;\r\n"
        "ORG:Northstar Print\r\n"
        "EMAIL;INTERNET:bob.jenkins@northstarprint.co.uk\r\n"
        "END:VCARD\r\n"
    )
    return _write(out / "address-book.vcf", cards)


def generate_olm(out: Path) -> Path:
    """A Mac Outlook archive: a zip of XML, which is the easy win in the spec."""
    out.mkdir(parents=True, exist_ok=True)
    path = out / "mac-outlook-2011.olm"
    path.parent.mkdir(parents=True, exist_ok=True)

    message_xml = """<?xml version="1.0" encoding="UTF-8"?>
<emails>
  <email>
    <OPFMessageCopySubject>Mac side of the Kelly account</OPFMessageCopySubject>
    <OPFMessageCopySentTime>2011-05-17T10:22:00Z</OPFMessageCopySentTime>
    <OPFMessageCopyBody>Sending from the Mac. Files are on the server.</OPFMessageCopyBody>
    <OPFMessageCopyMessageID>&lt;mac-2011-05-17@contractmktg.com&gt;</OPFMessageCopyMessageID>
    <OPFMessageCopyFromAddresses>
      <emailAddress OPFContactEmailAddressAddress="tmccarthy@contractmktg.com"
                    OPFContactEmailAddressName="Tim McCarthy"/>
    </OPFMessageCopyFromAddresses>
    <OPFMessageCopyToAddresses>
      <emailAddress OPFContactEmailAddressAddress="mobrien@contractmktg.com"
                    OPFContactEmailAddressName="Margaret O'Brien"/>
    </OPFMessageCopyToAddresses>
  </email>
</emails>
"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Accounts/Tim/Mail/Inbox/Messages_001.xml", message_xml)
        zf.writestr("Accounts/Tim/Mail/Inbox/hierarchy.plist", "<plist/>")
    return path


def generate_edge_cases(out: Path) -> list[Path]:
    """The awkward ones that are not damage: empty, huge subject, odd names."""
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    tim, margaret, *_ = PEOPLE

    # A zero-byte file. Not damage exactly - a failed copy, which happens.
    written.append(_write(out / "empty.eml", b""))

    # A subject longer than any sane column.
    written.append(_write(out / "long-subject.eml", _eml(
        subject="Re: " * 40 + "the Kelly account and everything attached to it",
        sender=tim, to=[margaret],
        date=datetime(2010, 8, 3, 12, 0, tzinfo=UTC),
        body="See subject.\n",
    )))

    # A filename with characters Windows dislikes but the archive must survive.
    written.append(_write(out / "résumé - draft (2).eml", _eml(
        subject="Résumé draft",
        sender=margaret, to=[tim],
        date=datetime(2013, 2, 14, 9, 0, tzinfo=UTC),
        body="Draft attached.\n",
    )))

    return written


# ---------------------------------------------------------------------------
# The deliberate-damage corpus (spec section 13)
# ---------------------------------------------------------------------------


def generate_damaged(out: Path) -> Path:
    """Files that are broken on purpose, each targeting one integrity check."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)

    # magic_mismatch: the name says .pst, the contents are a zip.
    _write(out / "not-really.pst", b"PK\x03\x04" + b"\x00" * 3000)

    # zero_or_tiny: a PST far too small to hold anything.
    _write(out / "truncated.pst", b"!BDN" + b"\x00" * 200)

    # zero_or_tiny: nothing at all.
    _write(out / "nothing.dbx", b"")

    # read_failure: a PST header followed by rubbish.
    _write(out / "corrupt.pst", b"!BDN" + bytes(range(256)) * 2000)

    # implausible_date and no_date, in one mailbox.
    tim, margaret, *_ = PEOPLE
    body = b""
    body += b"From tim@contractmktg.com Mon Jan  1 00:00:00 2001\n" + _eml(
        subject="A message dated 1961",
        sender=tim, to=[margaret],
        date=datetime(1961, 4, 12, 6, 7, tzinfo=UTC),
        body="Before email existed.\n",
        message_id="<impossible-1961@contractmktg.com>",
    ) + b"\n"
    body += b"From tim@contractmktg.com Mon Jan  1 00:00:00 2001\n" + _eml(
        subject="A message with no date at all",
        sender=tim, to=[margaret],
        date=None,
        body="No Date header.\n",
        message_id="<nodate-2@contractmktg.com>",
    ) + b"\n"
    _write(out / "bad-dates.mbox", body)

    generate_gap_mailbox(out)
    generate_contradiction_mailbox(out)
    generate_over_merged(out)
    generate_subset_pair(out)
    return out


def generate_gap_mailbox(out: Path) -> Path:
    """Every month of 2002-2004 except February 2003, which must be a hard_gap."""
    out.mkdir(parents=True, exist_ok=True)
    tim, margaret, *_ = PEOPLE
    body = b""
    n = 0

    for year in (2002, 2003, 2004):
        for month in range(1, 13):
            if year == 2003 and month == 2:
                continue            # the hole, on purpose
            for day in (5, 12, 19, 26):
                n += 1
                body += b"From tim@contractmktg.com Mon Jan  1 00:00:00 2001\n" + _eml(
                    subject=f"Weekly report {year}-{month:02d}-{day:02d}",
                    sender=tim, to=[margaret],
                    date=datetime(year, month, day, 9, 0, tzinfo=UTC),
                    body=f"Report for the week of {day}/{month}/{year}.\n",
                    message_id=f"<gap-{year}{month:02d}{day:02d}@contractmktg.com>",
                ) + b"\n"

    return _write(out / "gap-february-2003.mbox", body)


def generate_contradiction_mailbox(out: Path) -> Path:
    """A file whose name claims 1996-2004 but holds nothing from 2001.

    This is the source_contradiction case: a parse failure wearing the costume
    of a quiet year, and the one the spec singles out as most important.
    """
    out.mkdir(parents=True, exist_ok=True)
    tim, margaret, *_ = PEOPLE
    body = b""

    for year in (1996, 1997, 1998, 1999, 2000, 2002, 2003, 2004):
        for month in (3, 6, 9, 12):
            body += b"From tim@contractmktg.com Mon Jan  1 00:00:00 2001\n" + _eml(
                subject=f"Kelly account {year} Q{month // 3}",
                sender=tim, to=[margaret],
                date=datetime(year, month, 15, 10, 0, tzinfo=UTC),
                body=f"Kelly account correspondence, {year}.\n",
                message_id=f"<kelly-{year}{month:02d}@contractmktg.com>",
            ) + b"\n"

    return _write(out / "archive1996-2004.mbox", body)


def generate_over_merged(out: Path) -> Path:
    """One address wearing twelve different display names.

    info@ at a print shop, used by everyone in the office. It must be flagged
    as over_merged_risk and never appear as a single "top correspondent".
    """
    out.mkdir(parents=True, exist_ok=True)
    names = [
        "Reception", "Bob Jenkins", "Sales Desk", "Accounts", "Bob J",
        "Northstar Print", "Karen at Northstar", "Print Room", "Bob (mobile)",
        "Despatch", "Northstar Accounts", "Enquiries",
    ]
    tim, *_ = PEOPLE
    body = b""
    for i, name in enumerate(names):
        body += b"From info@northstarprint.co.uk Mon Jan  1 00:00:00 2001\n" + _eml(
            subject=f"Job {4400 + i}",
            sender=(name, "info@northstarprint.co.uk"),
            to=[tim],
            date=datetime(2009, 1 + (i % 12), 10, 9, 0, tzinfo=UTC),
            body=f"About job {4400 + i}.\n",
            message_id=f"<role-{i}@northstarprint.co.uk>",
        ) + b"\n"
    return _write(out / "role-mailbox.mbox", body)


def generate_subset_pair(out: Path) -> tuple[Path, Path]:
    """Two mailboxes where one strictly contains the other.

    The pair must produce duplicate_account_store naming the bigger file as the
    superset, with the overlap percentage.
    """
    out.mkdir(parents=True, exist_ok=True)
    tim, margaret, *_ = PEOPLE

    messages = []
    for i in range(20):
        messages.append(_eml(
            subject=f"Contract Marketing note {i:02d}",
            sender=tim, to=[margaret],
            date=datetime(2007, 1 + (i % 12), 3 + i, 14, 0, tzinfo=UTC),
            body=f"Note number {i}.\n",
            message_id=f"<subset-{i:02d}@contractmktg.com>",
        ))

    def mbox(msgs):
        out_bytes = b""
        for m in msgs:
            out_bytes += b"From tim@contractmktg.com Mon Jan  1 00:00:00 2001\n" + m + b"\n"
        return out_bytes

    big = _write(out / "mailbox-full.mbox", mbox(messages))
    small = _write(out / "mailbox-partial.mbox", mbox(messages[:12]))
    return big, small


if __name__ == "__main__":  # pragma: no cover
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else "generated")
    generate_all(target / "clean")
    generate_damaged(target / "damaged")
    print(f"Wrote fixtures to {target.resolve()}")
