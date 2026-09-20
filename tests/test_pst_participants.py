"""Who a .pst message was addressed to.

These exist because the code that turns a Recipients record set into people
called a function that was never written. Every message in every .pst and .ost
whose recipients could be read raised NameError, the per-message handler caught
it, and the message was dropped and counted as an unreadable record. An archive
built from Outlook files came out missing most of its mail, and the Problems
screen blamed the files.

Nothing in the suite noticed for one reason: there is no valid .pst fixture -
tests/fixtures/generated/damaged holds only deliberately corrupt ones, and
building a real .pst is a job of its own. So the recipient path was never once
executed. These tests execute it without a .pst, by handing the backend the
record sets pypff would have given it.
"""

from __future__ import annotations

import pytest

from recall.models import Kind, ParsedItem, Role
from recall.parsers import mapi
from recall.parsers.pst_backend import PyPffBackend, _recipient_identity


def backend(rows: list[dict]) -> PyPffBackend:
    """A backend that reads nothing but returns these recipient rows.

    __new__ rather than __init__ because opening a store needs a store. The
    two attributes below are all _participants touches.
    """
    b = PyPffBackend.__new__(PyPffBackend)
    b._codepage = None
    b._recipient_rows = lambda message: rows
    return b


def participants(rows: list[dict], kind: str = Kind.MESSAGE, props: dict | None = None):
    item = ParsedItem(kind=kind)
    backend(rows)._participants(object(), props or {}, item, kind)
    return item.participants


# --- the row reader -------------------------------------------------------


@pytest.mark.parametrize(
    "recipient_type, expected",
    [(1, Role.TO), (2, Role.CC), (3, Role.BCC)],
)
def test_recipient_type_becomes_the_role(recipient_type, expected):
    identity = _recipient_identity(
        {mapi.PR_EMAIL_ADDRESS: "aoife@example.com",
         mapi.PR_RECIPIENT_TYPE: recipient_type},
        Kind.MESSAGE,
    )
    assert identity.role == expected
    assert identity.address == "aoife@example.com"
    assert identity.address_type == "smtp"


def test_the_smtp_address_is_preferred_over_the_exchange_one():
    identity = _recipient_identity(
        {
            mapi.PR_EMAIL_ADDRESS: "/o=Org/cn=Recipients/cn=tim",
            mapi.PR_SMTP_ADDRESS: "tim@example.com",
            mapi.PR_RECIPIENT_TYPE: 1,
        },
        Kind.MESSAGE,
    )
    assert identity.address == "tim@example.com"


def test_an_exchange_only_address_is_kept_and_marked():
    identity = _recipient_identity(
        {mapi.PR_EMAIL_ADDRESS: "/o=Org/cn=Recipients/cn=tim",
         mapi.PR_ADDRTYPE: "EX", mapi.PR_RECIPIENT_TYPE: 1},
        Kind.MESSAGE,
    )
    assert identity.address_type == "ex"


def test_a_name_with_no_address_is_still_a_person():
    identity = _recipient_identity(
        {mapi.PR_RECIPIENT_DISPLAY_NAME: "Aoife Fitzgerald",
         mapi.PR_RECIPIENT_TYPE: 1},
        Kind.MESSAGE,
    )
    assert identity.display_name == "Aoife Fitzgerald"
    assert identity.address is None
    assert identity.address_type == "none"


def test_a_row_saying_nothing_is_not_a_person():
    """An empty row must not become a participant with no name and no address.

    ParsedItem refuses to hold one - see ParsedIdentity.__post_init__ - so a
    row like this has to be dropped here rather than raised over.
    """
    assert _recipient_identity({}, Kind.MESSAGE) is None
    assert _recipient_identity({mapi.PR_RECIPIENT_TYPE: 1}, Kind.MESSAGE) is None


def test_an_event_recipient_is_an_attendee_with_a_response():
    identity = _recipient_identity(
        {mapi.PR_EMAIL_ADDRESS: "aoife@example.com",
         mapi.PR_RECIPIENT_TYPE: 1,
         mapi.PR_RECIPIENT_TRACKSTATUS: 4},
        Kind.EVENT,
    )
    assert identity.role == Role.ATTENDEE
    assert identity.response_status == "declined"


# --- the call site --------------------------------------------------------


def test_recipients_reach_the_item():
    """The regression test proper: this is the line that raised NameError."""
    people = participants([
        {mapi.PR_EMAIL_ADDRESS: "aoife@example.com", mapi.PR_RECIPIENT_TYPE: 1},
        {mapi.PR_EMAIL_ADDRESS: "sean@example.com", mapi.PR_RECIPIENT_TYPE: 2},
    ])
    assert [(p.role, p.address) for p in people] == [
        (Role.TO, "aoife@example.com"),
        (Role.CC, "sean@example.com"),
    ]


def test_an_empty_row_among_good_ones_does_not_lose_the_others():
    people = participants([
        {},
        {mapi.PR_EMAIL_ADDRESS: "aoife@example.com", mapi.PR_RECIPIENT_TYPE: 1},
    ])
    assert [p.address for p in people] == ["aoife@example.com"]


def test_the_sender_and_the_recipients_are_both_there():
    people = participants(
        [{mapi.PR_EMAIL_ADDRESS: "aoife@example.com", mapi.PR_RECIPIENT_TYPE: 1}],
        props={mapi.PR_SENDER_SMTP_ADDRESS: "tim@example.com",
               mapi.PR_SENDER_NAME: "Tim"},
    )
    assert [(p.role, p.address) for p in people] == [
        (Role.FROM, "tim@example.com"),
        (Role.TO, "aoife@example.com"),
    ]


def test_the_display_string_fallback_is_not_used_when_rows_were_read():
    """PR_DISPLAY_TO is only for stores that dropped the Recipients sub-item.

    When the rows are there it must not run as well, or everyone is listed
    twice - once with an address and once as a bare name.
    """
    people = participants(
        [{mapi.PR_EMAIL_ADDRESS: "aoife@example.com", mapi.PR_RECIPIENT_TYPE: 1}],
        props={mapi.PR_DISPLAY_TO: "Aoife Fitzgerald"},
    )
    assert len(people) == 1
