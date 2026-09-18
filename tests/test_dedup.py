"""Dedup keys: the same record collapses, different records do not."""

from __future__ import annotations

import pytest

from recall.models import Kind, ParsedIdentity, ParsedItem, Role, TimePoint
from recall.normalize.dedup import (
    contact_dedup_key,
    dedup_key_for,
    event_dedup_key,
    message_dedup_key,
    minute_stamp,
    normalize_address,
)

# --- address normalisation ------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Tim@Example.COM", "tim@example.com"),
    ("  tim@example.com  ", "tim@example.com"),
    ("Tim McCarthy <tim@example.com>", "tim@example.com"),
    ("<tim@example.com>", "tim@example.com"),
])
def test_normalize_address(raw, expected):
    assert normalize_address(raw) == expected


def test_normalize_address_of_nothing():
    assert normalize_address(None) == ""
    assert normalize_address("") == ""


def test_minute_stamp_drops_seconds():
    """Re-saving through a different Outlook can shift the stored second."""
    assert minute_stamp("2003-04-14T09:30:45Z") == "2003-04-14T09:30"
    assert minute_stamp(None) == ""


# --- events ---------------------------------------------------------------


def test_event_key_uses_uid_recurrence_and_start():
    key = event_dedup_key("uid-1", None, "2003-04-17T14:00:00Z")
    assert key == event_dedup_key("uid-1", None, "2003-04-17T14:00:00Z")


def test_same_uid_different_start_is_a_different_instance():
    a = event_dedup_key("uid-1", None, "2003-04-17T14:00:00Z")
    b = event_dedup_key("uid-1", None, "2003-04-24T14:00:00Z")
    assert a != b


def test_recurrence_id_separates_instances():
    a = event_dedup_key("uid-1", "2003-04-17T14:00:00Z", "2003-04-17T14:00:00Z")
    b = event_dedup_key("uid-1", "2003-04-24T14:00:00Z", "2003-04-17T14:00:00Z")
    assert a != b


def test_event_without_uid_falls_back_to_subject_start_organizer():
    key = event_dedup_key(
        None, None, "1998-06-12T19:30:00Z",
        subject="Dinner with the Fitzgeralds", organizer="tim@example.com",
    )
    same = event_dedup_key(
        None, None, "1998-06-12T19:30:00Z",
        subject="RE: Dinner with the Fitzgeralds", organizer="Tim <TIM@EXAMPLE.COM>",
    )
    assert key == same, "the subject prefix and the address case must not matter"


def test_event_without_uid_different_organizer_is_different():
    a = event_dedup_key(None, None, "1998-06-12T19:30:00Z",
                        subject="Dinner", organizer="tim@example.com")
    b = event_dedup_key(None, None, "1998-06-12T19:30:00Z",
                        subject="Dinner", organizer="bob@example.com")
    assert a != b


# --- messages -------------------------------------------------------------


def test_message_key_uses_message_id_when_present():
    a = message_dedup_key("<abc@example.com>")
    b = message_dedup_key("abc@example.com")
    c = message_dedup_key("  <ABC@EXAMPLE.COM>  ")
    assert a == b == c, "brackets, spaces and case must not matter"


def test_different_message_ids_are_different():
    assert message_dedup_key("<a@x.com>") != message_dedup_key("<b@x.com>")


def test_message_without_id_uses_the_composite():
    key = message_dedup_key(
        None,
        sender="tim@example.com",
        recipients=["margaret@example.com", "declan@example.com"],
        occurred_utc="2003-04-14T09:30:00Z",
        subject="Quarterly figures",
        body_text="The Q1 figures are attached.",
    )
    assert len(key) == 64


def test_recipient_order_does_not_matter():
    """One client's To/Cc order must not make a second copy of the message."""
    a = message_dedup_key(
        None, sender="tim@x.com",
        recipients=["a@x.com", "b@x.com"],
        occurred_utc="2003-04-14T09:30:00Z", subject="s", body_text="b",
    )
    b = message_dedup_key(
        None, sender="tim@x.com",
        recipients=["b@x.com", "a@x.com"],
        occurred_utc="2003-04-14T09:30:00Z", subject="s", body_text="b",
    )
    assert a == b


def test_seconds_do_not_matter():
    a = message_dedup_key(None, sender="t@x.com", recipients=["m@x.com"],
                          occurred_utc="2003-04-14T09:30:00Z", subject="s", body_text="b")
    b = message_dedup_key(None, sender="t@x.com", recipients=["m@x.com"],
                          occurred_utc="2003-04-14T09:30:59Z", subject="s", body_text="b")
    assert a == b


def test_a_different_minute_is_a_different_message():
    a = message_dedup_key(None, sender="t@x.com", recipients=["m@x.com"],
                          occurred_utc="2003-04-14T09:30:00Z", subject="s", body_text="b")
    b = message_dedup_key(None, sender="t@x.com", recipients=["m@x.com"],
                          occurred_utc="2003-04-14T09:31:00Z", subject="s", body_text="b")
    assert a != b


def test_reply_prefix_does_not_matter_for_the_composite():
    a = message_dedup_key(None, sender="t@x.com", recipients=["m@x.com"],
                          occurred_utc="2003-04-14T09:30:00Z",
                          subject="Quarterly figures", body_text="b")
    b = message_dedup_key(None, sender="t@x.com", recipients=["m@x.com"],
                          occurred_utc="2003-04-14T09:30:00Z",
                          subject="RE: Quarterly figures", body_text="b")
    assert a == b


def test_only_the_first_2000_characters_of_the_body_count():
    base = "x" * 2000
    a = message_dedup_key(None, sender="t@x.com", recipients=["m@x.com"],
                          occurred_utc="2003-04-14T09:30:00Z", subject="s",
                          body_text=base + "tail one")
    b = message_dedup_key(None, sender="t@x.com", recipients=["m@x.com"],
                          occurred_utc="2003-04-14T09:30:00Z", subject="s",
                          body_text=base + "tail two")
    assert a == b


def test_different_bodies_are_different_messages():
    a = message_dedup_key(None, sender="t@x.com", recipients=["m@x.com"],
                          occurred_utc="2003-04-14T09:30:00Z", subject="s",
                          body_text="Yes, go ahead.")
    b = message_dedup_key(None, sender="t@x.com", recipients=["m@x.com"],
                          occurred_utc="2003-04-14T09:30:00Z", subject="s",
                          body_text="No, hold off.")
    assert a != b


# --- contacts -------------------------------------------------------------


def test_contact_key_uses_addresses():
    a = contact_dedup_key(emails=["m@x.com", "margaret@y.com"], display_name="Margaret")
    b = contact_dedup_key(emails=["MARGARET@Y.COM", "M@X.COM"], display_name="M. O'Brien")
    assert a == b, "the same addresses are the same person, whatever the card says"


def test_contact_without_address_uses_name_and_company():
    a = contact_dedup_key(display_name="Bob Jenkins", organization="Northstar Print")
    b = contact_dedup_key(display_name="bob jenkins", organization="northstar print")
    assert a == b


def test_same_name_different_company_is_a_different_person():
    a = contact_dedup_key(display_name="John Murphy", organization="Northstar Print")
    b = contact_dedup_key(display_name="John Murphy", organization="Fitzgerald Partners")
    assert a != b


def test_a_contact_with_nothing_identifying_is_refused():
    """Inventing an identity for an empty card would be a guess."""
    with pytest.raises(ValueError, match="cannot be identified"):
        contact_dedup_key()


# --- dispatch -------------------------------------------------------------


def _event_item() -> ParsedItem:
    return ParsedItem(
        kind=Kind.EVENT,
        subject="Review Q1",
        ical_uid="uid-1",
        occurred=TimePoint(utc="2003-04-17T14:00:00Z"),
        participants=[ParsedIdentity(address="tim@x.com", role=Role.ORGANIZER)],
    )


def _message_item() -> ParsedItem:
    return ParsedItem(
        kind=Kind.MESSAGE,
        subject="Quarterly figures",
        internet_message_id="<abc@x.com>",
        occurred=TimePoint(utc="2003-04-14T09:30:00Z"),
        participants=[
            ParsedIdentity(address="tim@x.com", role=Role.FROM),
            ParsedIdentity(address="margaret@x.com", role=Role.TO),
        ],
    )


def test_dispatch_picks_the_event_key():
    assert dedup_key_for(_event_item()) == event_dedup_key(
        "uid-1", None, "2003-04-17T14:00:00Z"
    )


def test_dispatch_picks_the_message_key():
    assert dedup_key_for(_message_item()) == message_dedup_key("<abc@x.com>")


def test_dispatch_refuses_an_unknown_kind():
    item = _message_item()
    item.kind = "invention"
    with pytest.raises(ValueError, match="not a kind"):
        dedup_key_for(item)


def test_keys_are_stable_across_runs():
    """The whole resume and re-run design rests on this."""
    first = dedup_key_for(_message_item())
    second = dedup_key_for(_message_item())
    assert first == second
