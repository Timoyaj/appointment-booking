"""Time and DST (§9).

Berlin: spring forward 2026-03-29 02:00 -> 03:00, fall back 2026-10-25
03:00 -> 02:00. New York: spring forward 2026-03-08, fall back 2026-11-01.
"""

from __future__ import annotations

import pytest

from tests.conftest import assert_ok, base_fixture, book, error_of, reset, signup


def dst_fixture(timezone="Europe/Berlin", *, opens="00:00", closes="06:00",
                slot_minutes=30, duration=90):
    """A restaurant open all night on Sundays, so transitions are visible."""
    fixture = base_fixture()
    restaurant = fixture["restaurants"][0]
    restaurant["timezone"] = timezone
    restaurant["opening_hours"] = [{"weekday": "sun", "opens": opens, "closes": closes}]
    restaurant["slot_minutes"] = slot_minutes
    restaurant["reservation_duration_minutes"] = duration
    restaurant["cancellation_cutoff_minutes"] = 0
    return fixture


BERLIN_SPRING = "2026-03-29"
BERLIN_FALL = "2026-10-25"
NY_SPRING = "2026-03-08"
NY_FALL = "2026-11-01"


def slots_for(client, restaurant_id, date, party_size=2):
    body = assert_ok(client.get("/availability", params={
        "restaurant_id": restaurant_id, "date": date, "party_size": party_size}), 200)
    return body["slots"]


# --------------------------------------------------------------------------- #
# spring forward
# --------------------------------------------------------------------------- #
def test_skipped_local_times_never_appear_in_availability(client):
    reset(client, dst_fixture())
    labels = [slot["starts_at_local"] for slot in slots_for(client, "r_anker", BERLIN_SPRING)]
    assert labels == [
        "2026-03-29T00:00", "2026-03-29T00:30", "2026-03-29T01:00", "2026-03-29T01:30",
        "2026-03-29T03:00", "2026-03-29T03:30", "2026-03-29T04:00", "2026-03-29T04:30",
    ]
    offsets = {slot["starts_at_local"]: slot["starts_at"] for slot in
               slots_for(client, "r_anker", BERLIN_SPRING)}
    assert offsets["2026-03-29T01:30"] == "2026-03-29T01:30:00+01:00"
    assert offsets["2026-03-29T03:00"] == "2026-03-29T03:00:00+02:00"


@pytest.mark.parametrize("starts_at_local", ["2026-03-29T02:00", "2026-03-29T02:30"])
def test_booking_a_skipped_local_time_is_invalid(client, starts_at_local):
    reset(client, dst_fixture())
    token = signup(client)["token"]
    response = book(client, token, starts_at_local=starts_at_local)
    assert response.status_code == 422
    assert error_of(response)["code"] == "invalid_local_time"


def test_booking_around_the_spring_forward_gap(client):
    reset(client, dst_fixture())
    token = signup(client)["token"]

    before = assert_ok(book(client, token, starts_at_local="2026-03-29T01:30"), 201)
    assert before["starts_at"] == "2026-03-29T01:30:00+01:00"
    # 90 absolute minutes later the clocks have sprung forward.
    assert before["ends_at"] == "2026-03-29T04:00:00+02:00"

    after = assert_ok(book(client, token, starts_at_local="2026-03-29T04:00"), 201)
    assert after["starts_at"] == "2026-03-29T04:00:00+02:00"
    # The two touch but do not overlap: occupancy is half-open in absolute time.
    assert before["ends_at"] == after["starts_at"]


def test_new_york_spring_forward(client):
    reset(client, dst_fixture("America/New_York"))
    labels = [slot["starts_at_local"] for slot in slots_for(client, "r_anker", NY_SPRING)]
    assert "2026-03-08T02:00" not in labels
    assert "2026-03-08T02:30" not in labels
    assert "2026-03-08T01:30" in labels and "2026-03-08T03:00" in labels

    token = signup(client)["token"]
    response = book(client, token, starts_at_local="2026-03-08T02:30")
    assert response.status_code == 422
    assert error_of(response)["code"] == "invalid_local_time"

    ok = assert_ok(book(client, token, starts_at_local="2026-03-08T01:00"), 201)
    assert ok["starts_at"] == "2026-03-08T01:00:00-05:00"
    assert ok["ends_at"] == "2026-03-08T03:30:00-04:00"


# --------------------------------------------------------------------------- #
# fall back
# --------------------------------------------------------------------------- #
def test_a_repeated_local_hour_appears_once(client):
    reset(client, dst_fixture())
    slots = slots_for(client, "r_anker", BERLIN_FALL)
    labels = [slot["starts_at_local"] for slot in slots]
    assert labels == [
        "2026-10-25T00:00", "2026-10-25T00:30", "2026-10-25T01:00", "2026-10-25T01:30",
        "2026-10-25T02:00", "2026-10-25T02:30", "2026-10-25T03:00", "2026-10-25T03:30",
        "2026-10-25T04:00", "2026-10-25T04:30",
    ]
    by_label = {slot["starts_at_local"]: slot["starts_at"] for slot in slots}
    # The first occurrence — before the clocks change — is the one offered.
    assert by_label["2026-10-25T02:00"] == "2026-10-25T02:00:00+02:00"
    assert by_label["2026-10-25T02:30"] == "2026-10-25T02:30:00+02:00"
    assert by_label["2026-10-25T03:00"] == "2026-10-25T03:00:00+01:00"


def test_a_repeated_local_time_resolves_to_the_first_occurrence(client):
    reset(client, dst_fixture())
    token = signup(client)["token"]

    created = assert_ok(book(client, token, starts_at_local="2026-10-25T02:30"), 201)
    assert created["starts_at"] == "2026-10-25T02:30:00+02:00"
    # 00:30Z + 90 absolute minutes == 02:00Z == 03:00 local, after the change.
    assert created["ends_at"] == "2026-10-25T03:00:00+01:00"


def test_the_spec_example_ends_at_reads_02_00_not_03_00(client):
    """'A 90-minute reservation starting at 01:30 on a fall-back night ends 90
    real minutes later, and its local ends_at will read 02:00, not 03:00.'"""
    reset(client, dst_fixture())
    token = signup(client)["token"]
    created = assert_ok(book(client, token, starts_at_local="2026-10-25T01:30"), 201)
    assert created["starts_at"] == "2026-10-25T01:30:00+02:00"
    assert created["ends_at"] == "2026-10-25T02:00:00+01:00"


def test_the_second_occurrence_is_not_bookable(client):
    reset(client, dst_fixture())
    token = signup(client)["token"]
    first = assert_ok(book(client, token, starts_at_local="2026-10-25T02:30"), 201)

    # The same local label resolves to the same instant, so it is taken.
    again = book(client, token, starts_at_local="2026-10-25T02:30")
    assert again.status_code == 409
    assert error_of(again)["code"] == "table_unavailable"

    # 02:00 local (first occurrence) overlaps 02:30 local (first occurrence).
    overlapping = book(client, token, starts_at_local="2026-10-25T02:00")
    assert overlapping.status_code == 409

    # The slot grid still shows 02:30 as taken and 04:00 as free.
    slots = {s["starts_at_local"]: s["available_table_ids"]
             for s in slots_for(client, "r_anker", BERLIN_FALL)}
    assert slots["2026-10-25T02:30"] == ["t_1"]
    assert slots["2026-10-25T04:00"] == ["t_1", "t_2"]
    assert first["table_id"] == "t_2"


def test_new_york_fall_back(client):
    reset(client, dst_fixture("America/New_York"))
    token = signup(client)["token"]

    slots = {s["starts_at_local"]: s["starts_at"] for s in slots_for(client, "r_anker", NY_FALL)}
    assert slots["2026-11-01T01:00"] == "2026-11-01T01:00:00-04:00"
    assert slots["2026-11-01T01:30"] == "2026-11-01T01:30:00-04:00"
    assert slots["2026-11-01T02:00"] == "2026-11-01T02:00:00-05:00"

    created = assert_ok(book(client, token, starts_at_local="2026-11-01T01:30"), 201)
    assert created["starts_at"] == "2026-11-01T01:30:00-04:00"
    assert created["ends_at"] == "2026-11-01T02:00:00-05:00"


# --------------------------------------------------------------------------- #
# offsets follow the IANA rules for the date
# --------------------------------------------------------------------------- #
def test_offsets_follow_the_season(client):
    reset(client, dst_fixture())
    token = signup(client)["token"]

    summer = assert_ok(book(client, token, starts_at_local="2026-06-07T03:00"), 201)
    assert summer["starts_at"] == "2026-06-07T03:00:00+02:00"

    winter = assert_ok(book(client, token, starts_at_local="2026-12-06T03:00"), 201)
    assert winter["starts_at"] == "2026-12-06T03:00:00+01:00"


def test_a_utc_restaurant_has_no_transitions(client):
    reset(client, dst_fixture("UTC"))
    token = signup(client)["token"]
    labels = [s["starts_at_local"] for s in slots_for(client, "r_anker", BERLIN_SPRING)]
    assert labels == [f"2026-03-29T{hour:02d}:{minute:02d}"
                      for hour in range(5) for minute in (0, 30)]
    created = assert_ok(book(client, token, starts_at_local="2026-03-29T02:30"), 201)
    assert created["starts_at"] == "2026-03-29T02:30:00+00:00"


def test_the_slot_grid_respects_a_wider_step(client):
    reset(client, dst_fixture(slot_minutes=60, duration=60))
    labels = [s["starts_at_local"] for s in slots_for(client, "r_anker", BERLIN_FALL)]
    assert labels == ["2026-10-25T00:00", "2026-10-25T01:00", "2026-10-25T02:00",
                      "2026-10-25T03:00", "2026-10-25T04:00", "2026-10-25T05:00"]


def test_a_long_reservation_must_still_end_by_closing(client):
    reset(client, dst_fixture(opens="18:00", closes="23:00", duration=180))
    labels = [s["starts_at_local"] for s in slots_for(client, "r_anker", BERLIN_FALL)]
    # 18:00 and 18:30 fit (18:00+180 = 21:00, 20:00 <= 23:00); 20:30+180 = 23:30 does not.
    assert labels == ["2026-10-25T18:00", "2026-10-25T18:30", "2026-10-25T19:00",
                      "2026-10-25T19:30", "2026-10-25T20:00"]

    token = signup(client)["token"]
    assert_ok(book(client, token, starts_at_local="2026-10-25T20:00"), 201)
    response = book(client, token, starts_at_local="2026-10-25T20:30")
    assert response.status_code == 422
    assert error_of(response)["code"] == "outside_opening_hours"
