"""What a parser does with the records it cannot read, and how it reads a big file.

The archive's central promise is that nothing goes missing quietly. A record
the parser drops has to leave something behind saying so, or the count on the
Problems screen goes down and no screen anywhere accounts for the difference.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recall.models import Kind, ParsedItem
from recall.parsers import eml as eml_module
from recall.parsers import mapi
from recall.parsers.eml import MboxParser, _count_separators
from recall.parsers.pst_backend import PyPffBackend
from tests.fixtures.generate import generate_mbox


@pytest.fixture
def mailbox(tmp_path: Path) -> Path:
    generate_mbox(tmp_path)
    return tmp_path / "archive-2006.mbox"


# --- a message that could not be built ------------------------------------


def test_an_unbuildable_message_is_recorded_not_just_logged(mailbox, monkeypatch):
    """This used to be log.debug and a continue.

    The record left the archive and left nothing behind. claimed_count made the
    shortfall visible as a number, but nothing said which record or why, and
    the branch immediately above - for a message that could not be fetched at
    all - had been recording a finding all along.
    """
    monkeypatch.setattr(
        eml_module, "build_message",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("a header went wrong")),
    )

    with MboxParser(mailbox) as parser:
        items = list(parser.parse())
        outcome = parser.outcome

    assert items == []
    codes = [f[0] for f in outcome.findings]
    assert codes and set(codes) == {"read_failure"}
    assert len(outcome.findings) == outcome.claimed_count

    detail = outcome.findings[0][3]
    assert "a header went wrong" in detail
    assert "ValueError" in detail


def test_a_readable_mailbox_reports_nothing(mailbox):
    """The other half: no findings invented for a file that read cleanly."""
    with MboxParser(mailbox) as parser:
        items = list(parser.parse())
        assert items
        assert parser.outcome.findings == []


# --- counting the separators ----------------------------------------------


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"From a@b 2003\nhi\n\nFrom c@d 2004\nyo\n", 2),
        (b"From a@b 2003\nhi\n\nFrom c@d 2004", 2),
        (b"From a@b\r\nhi\r\nFrom c@d\r\n", 2),
        (b"From  a@b\nhi\n", 0),      # two spaces: not a separator
        (b"From \nhi\n", 0),          # nothing after the space
        (b"Fromage\nhi\n", 0),
        (b"From a@b\nFrom the desk of\n", 2),
        (b"", 0),
    ],
)
def test_the_separator_count_matches_the_regex_it_replaced(tmp_path, data, expected):
    """The old count was a regex over the whole file; this is line by line.

    The cases here are the ones where "^From \\S+" and a careless line test
    disagree - a double space, a bare "From ", a word beginning with From.
    """
    path = tmp_path / "t.mbox"
    path.write_bytes(data)
    assert _count_separators(path) == expected


def test_the_mailbox_is_never_read_whole(mailbox, monkeypatch):
    """An mbox holding a decade of mail is measured in gigabytes.

    It used to be read into memory in full purely to count separators, while
    mailbox.mbox opened the same file again beside it. Reading it whole is the
    defect, so the test forbids it outright rather than measuring memory.
    """
    def refuse(self, *a, **k):
        raise AssertionError(f"read the whole of {self} into memory")

    monkeypatch.setattr(Path, "read_bytes", refuse)

    with MboxParser(mailbox) as parser:
        items = list(parser.parse())

    assert items
    assert parser.outcome.claimed_count == len(items)


def test_a_mailbox_that_cannot_be_opened_is_reported(tmp_path):
    with MboxParser(tmp_path / "not-here.mbox") as parser:
        assert list(parser.parse()) == []
        assert parser.outcome.error


# --- an email attached to an email ----------------------------------------


class _Attachment:
    def __init__(self, props: dict) -> None:
        self.props = props

    def get_size(self) -> int:                    # pragma: no cover - guarded
        raise AssertionError("an embedded message has no buffer to read")

    def read_buffer(self, size):                  # pragma: no cover - guarded
        raise AssertionError("an embedded message has no buffer to read")


class _Message:
    def __init__(self, attachments: list[_Attachment]) -> None:
        self.attachments = attachments

    def get_number_of_attachments(self) -> int:
        return len(self.attachments)

    def get_attachment(self, index):
        return self.attachments[index]


def attachments_of(props: dict) -> list:
    backend = PyPffBackend.__new__(PyPffBackend)
    backend._codepage = None
    item = ParsedItem(kind=Kind.MESSAGE)
    backend._attachments(_Message([_Attachment(props)]), item)
    return item.attachments


def test_an_embedded_message_says_it_was_not_opened(monkeypatch):
    """PR_ATTACH_METHOD was read and then never used.

    Method 5 is a message attached to a message, which has no bytes to read, so
    it came through as an attachment with no contents and no explanation. Recall
    still does not open one - that is a feature, not a fix - but it now says so
    where the Problems screen can show it.
    """
    monkeypatch.setattr(
        mapi, "read_properties",
        lambda item, **k: {
            mapi.PR_ATTACH_METHOD: mapi.ATTACH_EMBEDDED_MSG,
            mapi.PR_ATTACH_LONG_FILENAME: "Fwd: the lease.msg",
        },
    )
    attachment = attachments_of({})[0]

    assert attachment.filename == "Fwd: the lease.msg"
    assert attachment.data is None
    assert attachment.read_error
    assert "email attached to an email" in attachment.read_error
