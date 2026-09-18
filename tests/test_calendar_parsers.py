"""The .ics and .vcs readers, and the timezone rule they exist to honour."""

from __future__ import annotations

from pathlib import Path

import pytest

from recall.models import Kind, Role
from recall.parsers.ics import IcsParser
from recall.parsers.vcs import VcsParser
from tests.fixtures.generate import generate_ics, generate_vcs


@pytest.fixture
def calendars(tmp_path: Path) -> Path:
    generate_ics(tmp_path)
    generate_vcs(tmp_path)
    return tmp_path


def parse(path: Path, parser_cls=IcsParser) -> list:
    with parser_cls(path) as parser:
        return list(parser.parse())


# --- iCalendar ------------------------------------------------------------


def test_reads_every_event(calendars: Path):
    events = parse(calendars / "meetings-2003.ics")
    assert len(events) == 2
    assert {e.subject for e in events} == {
        "Review Q1 figures with Margaret", "Northstar Print site visit"
    }


def test_utc_times_are_fully_known(calendars: Path):
    event = next(e for e in parse(calendars / "meetings-2003.ics")
                 if e.subject.startswith("Review"))
    assert event.occurred.utc == "2003-04-17T14:00:00Z"
    assert event.occurred.tz == "UTC"
    assert event.occurred.tz_known is True
    assert event.end.utc == "2003-04-17T15:30:00Z"


def test_floating_time_keeps_its_date_and_admits_the_zone_is_unknown(calendars: Path):
    """The rule the spec is most insistent about.

    A time with no Z and no TZID is 7:30pm wherever you were. The date is a
    fact; the instant is not. Recall keeps the date, records the zone as
    unknown, and raises unknown_timezone. It does not call it UTC, and it does
    not throw the date away.
    """
    event = parse(calendars / "floating-time.ics")[0]
    assert event.occurred.utc is not None, "the date is known and must be kept"
    assert event.occurred.local == "1998-06-12T19:30:00"
    assert event.occurred.tz is None, "no timezone may be invented"
    assert event.occurred.tz_known is False
    assert any(code == "unknown_timezone" for code, _ in event.notes)


def test_all_day_event_has_no_time_to_be_unsure_about(calendars: Path):
    events = parse(calendars / "all-day-and-no-uid.ics")
    christmas = next(e for e in events if "Christmas" in e.subject)
    assert christmas.all_day is True
    assert christmas.occurred.utc.startswith("2007-12-24")
    assert not any(code == "unknown_timezone" for code, _ in christmas.notes)


def test_event_without_a_uid_is_still_read(calendars: Path):
    events = parse(calendars / "all-day-and-no-uid.ics")
    dentist = next(e for e in events if e.subject == "Dentist")
    assert dentist.ical_uid is None
    assert dentist.occurred.utc == "2009-09-14T10:30:00Z"


def test_organizer_and_attendees_with_responses(calendars: Path):
    event = next(e for e in parse(calendars / "meetings-2003.ics")
                 if e.subject.startswith("Review"))
    by_role = {(p.role, p.address): p for p in event.participants}
    assert (Role.ORGANIZER, "tmccarthy@contractmktg.com") in by_role
    assert by_role[(Role.ATTENDEE, "mobrien@contractmktg.com")].response_status == "accepted"
    assert by_role[(Role.ATTENDEE, "declan.walsh@fitzgerald-partners.ie")].response_status == "declined"


def test_recurrence_rule_is_stored_not_expanded(calendars: Path):
    """Eleven years of a weekly meeting is one row, not five hundred."""
    events = parse(calendars / "recurring.ics")
    assert len(events) == 1, "instances must not be expanded into the database"
    master = events[0]
    assert master.is_recurring_master is True
    assert master.recurrence["rrule"]["FREQ"] == ["WEEKLY"]
    assert "BYDAY" in master.recurrence["rrule"]


def test_location_and_description_survive(calendars: Path):
    event = next(e for e in parse(calendars / "meetings-2003.ics")
                 if e.subject.startswith("Review"))
    assert event.location == "Boardroom, Pearse Street"
    assert "Print costs up 18 per cent" in event.body_text


def test_kind_is_event(calendars: Path):
    assert all(e.kind == Kind.EVENT for e in parse(calendars / "meetings-2003.ics"))


def test_claimed_count_is_recorded(calendars: Path):
    with IcsParser(calendars / "meetings-2003.ics") as parser:
        list(parser.parse())
        assert parser.outcome.claimed_count == 2
        assert parser.outcome.yielded_count == 2
        assert parser.outcome.estimated_loss == 0


def test_sample_limit_stops_early(calendars: Path):
    with IcsParser(calendars / "meetings-2003.ics", sample_limit=1) as parser:
        assert len(list(parser.parse())) == 1


def test_asking_for_only_mail_yields_nothing(calendars: Path):
    with IcsParser(calendars / "meetings-2003.ics") as parser:
        assert list(parser.parse(frozenset({Kind.MESSAGE}))) == []


def test_a_missing_file_reports_rather_than_raises(tmp_path: Path):
    with IcsParser(tmp_path / "nope.ics") as parser:
        assert list(parser.parse()) == []
        assert parser.outcome.error


def test_garbage_is_reported_not_crashed(tmp_path: Path):
    p = tmp_path / "junk.ics"
    p.write_text("BEGIN:VCALENDAR\nthis is not a calendar at all\n")
    with IcsParser(p) as parser:
        list(parser.parse())   # must not raise


def test_source_file_is_never_modified(calendars: Path):
    path = calendars / "meetings-2003.ics"
    before = path.read_bytes()
    parse(path)
    assert path.read_bytes() == before


# --- vCalendar 1.0 --------------------------------------------------------


def test_vcs_reads_both_events(calendars: Path):
    events = parse(calendars / "schedule-plus-1996.vcs", VcsParser)
    assert len(events) == 2


def test_vcs_quoted_printable_is_decoded(calendars: Path):
    """How accented names survived 1996."""
    events = parse(calendars / "schedule-plus-1996.vcs", VcsParser)
    meeting = events[0]
    assert meeting.subject == "Réunion avec Aoife"
    assert "Église" in meeting.location
    assert "Première" in meeting.body_text


def test_vcs_times_are_floating_and_said_to_be(calendars: Path):
    """vCalendar almost never records a zone, and that is reported, not assumed."""
    meeting = parse(calendars / "schedule-plus-1996.vcs", VcsParser)[0]
    assert meeting.occurred.local == "1996-09-23T14:00:00"
    assert meeting.occurred.tz is None
    assert meeting.occurred.tz_known is False
    assert any(code == "unknown_timezone" for code, _ in meeting.notes)


def test_vcs_positional_rrule_is_understood(calendars: Path):
    """"W1 MO #52" is weekly on Monday, 52 times - not FREQ=WEEKLY;BYDAY=MO."""
    events = parse(calendars / "schedule-plus-1996.vcs", VcsParser)
    weekly = next(e for e in events if e.subject == "Weekly sales meeting")
    assert weekly.is_recurring_master is True
    rule = weekly.recurrence["rrule"]
    assert rule["FREQ"] == "WEEKLY"
    assert rule["INTERVAL"] == 1
    assert rule["COUNT"] == 52
    assert "MO" in rule["BY"]


def test_vcs_unreadable_rrule_is_flagged_not_approximated(tmp_path: Path):
    """A wrong repeat rule invents meetings that never happened."""
    p = tmp_path / "odd.vcs"
    p.write_text(
        "BEGIN:VCALENDAR\r\nVERSION:1.0\r\nBEGIN:VEVENT\r\n"
        "SUMMARY:Odd rule\r\nDTSTART:19970101T090000\r\n"
        "RRULE:QQ9 ZZ\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    event = parse(p, VcsParser)[0]
    assert event.recurrence.get("unparsed")
    assert any(code == "unresolved_recurrence" for code, _ in event.notes)


def test_vcs_attendee_status(calendars: Path):
    meeting = parse(calendars / "schedule-plus-1996.vcs", VcsParser)[0]
    attendee = next(p for p in meeting.participants if p.role == Role.ATTENDEE)
    assert attendee.address == "aoife@studiolibre.fr"
    assert attendee.response_status == "accepted"


def test_vcs_confidence_is_never_certain(calendars: Path):
    """A 1996 file that declares no charset is never fully trusted."""
    meeting = parse(calendars / "schedule-plus-1996.vcs", VcsParser)[0]
    assert meeting.parse_confidence < 1.0


def test_vcs_event_with_no_start_goes_to_undated(tmp_path: Path):
    p = tmp_path / "nodate.vcs"
    p.write_text(
        "BEGIN:VCALENDAR\r\nVERSION:1.0\r\nBEGIN:VEVENT\r\n"
        "SUMMARY:No start at all\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    event = parse(p, VcsParser)[0]
    assert event.occurred.utc is None
    assert any(code == "no_date" for code, _ in event.notes)
