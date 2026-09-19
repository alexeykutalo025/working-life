"""Periods of the user's own life, marked on the timeline.

Spec screen 4 asks for user-defined eras shaded behind the bars. The dates are
typed by hand by someone who is not going to type 1984-01-01, so the rule is
that a rough date is accepted and made precise, and a date that cannot be read
is refused out loud rather than stored as NULL and quietly forgotten.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from recall.api.app import create_app
from recall.api.timeline import normalize_era_date


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


# --- reading a date a person typed ----------------------------------------


def test_a_year_on_its_own_means_the_whole_year():
    """"1984 to 1997" has to cover the end of 1997, not just its first second."""
    assert normalize_era_date("1984", end=False, label="x") == "1984-01-01"
    assert normalize_era_date("1997", end=True, label="x") == "1997-12-31"


def test_a_year_and_month_means_the_whole_month():
    assert normalize_era_date("1984-06", end=False, label="x") == "1984-06-01"
    assert normalize_era_date("1984-06", end=True, label="x") == "1984-06-30"


def test_february_knows_about_leap_years():
    assert normalize_era_date("2000-02", end=True, label="x") == "2000-02-29"
    assert normalize_era_date("1900-02", end=True, label="x") == "1900-02-28"


def test_a_full_date_is_kept_as_it_is():
    assert normalize_era_date("1984-06-15", end=False, label="x") == "1984-06-15"
    assert normalize_era_date("1984-06-15", end=True, label="x") == "1984-06-15"


def test_a_timestamp_is_accepted_and_trimmed():
    assert normalize_era_date("1984-06-15T09:30:00", end=False, label="x") == "1984-06-15"


def test_empty_means_open_ended_not_an_error():
    assert normalize_era_date(None, end=False, label="x") is None
    assert normalize_era_date("   ", end=True, label="x") is None


@pytest.mark.parametrize(
    "bad",
    ["last summer", "1984/06", "84", "1984-13", "1984-02-30", "1984-06-15-01", ""],
    ids=["prose", "slashes", "two-digit-year", "month-13", "no-such-day",
         "too-many-parts", "blank"],
)
def test_a_date_that_cannot_be_read_is_refused(bad: str):
    if bad == "":
        assert normalize_era_date(bad, end=False, label="x") is None
        return
    with pytest.raises(HTTPException) as caught:
        normalize_era_date(bad, end=False, label="The start date")
    assert caught.value.status_code == 400


def test_a_two_digit_year_is_refused_rather_than_guessed():
    """"84" could be 1984 or 2084, and this program does not guess dates."""
    with pytest.raises(HTTPException) as caught:
        normalize_era_date("84", end=False, label="The start date")
    assert "in full" in caught.value.detail


def test_the_year_zero_is_not_a_date():
    with pytest.raises(HTTPException):
        normalize_era_date("0000", end=False, label="The start date")


def test_the_refusal_says_what_to_type_instead():
    with pytest.raises(HTTPException) as caught:
        normalize_era_date("last summer", end=False, label="The start date")
    detail = caught.value.detail
    assert "The start date" in detail
    assert "1984" in detail, "the message should show the shape of a date that works"


def test_the_refusal_names_the_actual_problem():
    with pytest.raises(HTTPException) as caught:
        normalize_era_date("1984-02-30", end=False, label="The start date")
    assert "29 days" in caught.value.detail


# --- through the API ------------------------------------------------------


def test_a_period_can_be_created_and_comes_back(client):
    created = client.post("/api/eras", json={
        "name": "Harrow & Sons", "start_utc": "1984", "end_utc": "1997",
        "color": "#3d6ea8", "notes": "The good years",
    })
    assert created.status_code == 200
    assert created.json()["saved"] is True

    eras = client.get("/api/eras").json()
    assert len(eras) == 1
    assert eras[0]["name"] == "Harrow & Sons"
    assert eras[0]["start_utc"] == "1984-01-01"
    assert eras[0]["end_utc"] == "1997-12-31"


def test_a_period_with_no_name_is_refused(client):
    response = client.post("/api/eras", json={"name": "   ", "start_utc": "1984"})
    assert response.status_code == 400
    assert "name" in response.json()["detail"]


def test_a_period_that_ends_before_it_starts_is_refused(client):
    """Backwards dates would shade a negative width, or nothing at all."""
    response = client.post("/api/eras", json={
        "name": "Backwards", "start_utc": "1997", "end_utc": "1984",
    })
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "1984-12-31" in detail and "1997-01-01" in detail

    assert client.get("/api/eras").json() == [], "nothing should have been stored"


def test_one_year_is_a_period_not_a_backwards_one(client):
    """Start and end in the same year must not trip the ordering check."""
    response = client.post("/api/eras", json={
        "name": "The year off", "start_utc": "1998", "end_utc": "1998",
    })
    assert response.status_code == 200


def test_a_period_with_no_end_is_allowed(client):
    response = client.post("/api/eras", json={"name": "Still going", "start_utc": "2010"})
    assert response.status_code == 200
    assert client.get("/api/eras").json()[0]["end_utc"] is None


def test_a_bad_date_is_refused_before_anything_is_stored(client):
    response = client.post("/api/eras", json={
        "name": "Sometime", "start_utc": "last summer",
    })
    assert response.status_code == 400
    assert client.get("/api/eras").json() == []


def test_a_period_can_be_removed(client):
    era_id = client.post("/api/eras", json={"name": "Brief", "start_utc": "2001"}).json()["id"]
    assert client.delete(f"/api/eras/{era_id}").status_code == 200
    assert client.get("/api/eras").json() == []


def test_removing_a_period_that_is_not_there_says_so(client):
    response = client.delete("/api/eras/999")
    assert response.status_code == 404
    assert "999" in response.json()["detail"]


def test_periods_reach_the_timeline(client):
    """The chart reads eras off the timeline payload, so they have to be in it."""
    client.post("/api/eras", json={
        "name": "Harrow & Sons", "start_utc": "1984", "end_utc": "1997",
    })
    payload = client.get("/api/timeline?level=year").json()
    assert [e["name"] for e in payload["eras"]] == ["Harrow & Sons"]
