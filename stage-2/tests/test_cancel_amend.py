"""Cancellation and amendment."""

from __future__ import annotations

import datetime as dt

import pytest

from tablekeeper import clock
from tests.conftest import (
    SATURDAY,
    THURSDAY,
    assert_ok,
    book,
    error_of,
    headers_for,
    signup,
)

# The default fixture's cutoff is 120 minutes before the start.
# 2026-09-24T19:00 Berlin == 17:00Z, so the deadline is 15:00Z that day.
DEADLINE = dt.datetime(2026, 9, 24, 15, 0, tzinfo=dt.timezone.utc)


def test_cancel_returns_the_reservation(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)

    body = assert_ok(client.post(f"/reservations/{created['reference']}/cancel",
                                 headers=headers_for(token)), 200)
    assert body["status"] == "cancelled"
    assert body["reference"] == created["reference"]
    assert body["reservation_id"] == created["reservation_id"]
    assert body["starts_at"] == created["starts_at"]


def test_cancel_frees_the_table_immediately(client, seeded):
    mine = signup(client, "mine@example.com")["token"]
    theirs = signup(client, "theirs@example.com")["token"]
    created = assert_ok(book(client, mine), 201)

    assert book(client, theirs).status_code == 409
    assert_ok(client.post(f"/reservations/{created['reference']}/cancel",
                          headers=headers_for(mine)), 200)

    # The next availability response offers the slot again...
    slot = next(s for s in assert_ok(
        client.get("/availability", params={"restaurant_id": "r_anker",
                                            "date": THURSDAY, "party_size": 4}), 200
    )["slots"] if s["starts_at_local"] == f"{THURSDAY}T19:00")
    assert slot["available_table_ids"] == ["t_2"]
    # ...and it can actually be booked.
    assert_ok(book(client, theirs), 201)


def test_cancelling_twice_is_not_an_error(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)

    first = assert_ok(client.post(f"/reservations/{created['reference']}/cancel",
                                  headers=headers_for(token)), 200)
    second = assert_ok(client.post(f"/reservations/{created['reference']}/cancel",
                                   headers=headers_for(token)), 200)
    assert second == first
    assert second["status"] == "cancelled"


def test_cancelling_after_the_cutoff_is_refused(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)

    clock.freeze(DEADLINE - dt.timedelta(minutes=1))
    assert_ok(client.post(f"/reservations/{created['reference']}/cancel",
                          headers=headers_for(token)), 200)

    other = assert_ok(book(client, token, starts_at_local=f"{THURSDAY}T20:30"), 201)
    clock.freeze(DEADLINE + dt.timedelta(minutes=90))  # inside the cutoff of 20:30
    response = client.post(f"/reservations/{other['reference']}/cancel",
                           headers=headers_for(token))
    assert response.status_code == 409
    assert error_of(response)["code"] == "cutoff_passed"


def test_the_cutoff_boundary_is_inclusive(client, seeded):
    """One second before the deadline is allowed; exactly at it is refused."""
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)

    clock.freeze(DEADLINE - dt.timedelta(seconds=1))
    assert_ok(client.post(f"/reservations/{created['reference']}/cancel",
                          headers=headers_for(token)), 200)

    second = assert_ok(book(client, token, starts_at_local=f"{THURSDAY}T20:30"), 201)
    # 20:30 Berlin == 18:30Z; minus the 120-minute cutoff gives 16:30Z.
    clock.freeze(dt.datetime(2026, 9, 24, 16, 30, tzinfo=dt.timezone.utc))
    response = client.post(f"/reservations/{second['reference']}/cancel",
                           headers=headers_for(token))
    assert response.status_code == 409
    assert error_of(response)["code"] == "cutoff_passed"


def test_a_start_in_the_past_is_always_past_the_cutoff(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token, starts_at_local="2026-09-17T19:00"), 201)
    response = client.post(f"/reservations/{created['reference']}/cancel",
                           headers=headers_for(token))
    assert response.status_code == 409
    assert error_of(response)["code"] == "cutoff_passed"


def test_cancelling_someone_elses_booking_is_404(client, seeded):
    mine = signup(client, "mine@example.com")["token"]
    theirs = signup(client, "theirs@example.com")["token"]
    created = assert_ok(book(client, mine), 201)

    response = client.post(f"/reservations/{created['reference']}/cancel",
                           headers=headers_for(theirs))
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"
    assert client.get(f"/reservations/{created['reference']}",
                      headers=headers_for(mine)).json()["status"] == "confirmed"


# --------------------------------------------------------------------------- #
# PATCH
# --------------------------------------------------------------------------- #
def test_amend_changes_the_time(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token, starts_at_local=f"{THURSDAY}T19:00"), 201)

    body = assert_ok(client.patch(f"/reservations/{created['reference']}",
                                  json={"starts_at_local": f"{THURSDAY}T20:30"},
                                  headers=headers_for(token)), 200)
    assert body["starts_at_local"] == f"{THURSDAY}T20:30"
    assert body["starts_at"] == "2026-09-24T20:30:00+02:00"
    assert body["ends_at"] == "2026-09-24T22:00:00+02:00"
    assert body["status"] == "confirmed"
    # Identity survives.
    assert body["reference"] == created["reference"]
    assert body["reservation_id"] == created["reservation_id"]
    assert body["created_at"] == created["created_at"]


def test_amend_releases_the_old_slot_and_takes_the_new_one(client, seeded):
    mine = signup(client, "mine@example.com")["token"]
    theirs = signup(client, "theirs@example.com")["token"]
    created = assert_ok(book(client, mine, starts_at_local=f"{THURSDAY}T19:00"), 201)

    assert_ok(client.patch(f"/reservations/{created['reference']}",
                           json={"starts_at_local": f"{THURSDAY}T21:00"},
                           headers=headers_for(mine)), 200)

    # The vacated slot is bookable by someone else...
    assert_ok(book(client, theirs, starts_at_local=f"{THURSDAY}T19:00"), 201)
    # ...and the slot just taken is not.
    response = book(client, theirs, starts_at_local=f"{THURSDAY}T21:00")
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"


def test_amend_changes_the_table_and_party_size(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token, table_id="t_1", party_size=2), 201)

    body = assert_ok(client.patch(f"/reservations/{created['reference']}",
                                  json={"table_id": "t_2", "party_size": 4},
                                  headers=headers_for(token)), 200)
    assert body["table_id"] == "t_2"
    assert body["party_size"] == 4
    assert body["starts_at"] == created["starts_at"]


def test_amend_accepts_any_subset_and_keeps_the_rest(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token, table_id="t_2", party_size=4,
                             starts_at_local=f"{THURSDAY}T19:00"), 201)

    body = assert_ok(client.patch(f"/reservations/{created['reference']}",
                                  json={"party_size": 3}, headers=headers_for(token)), 200)
    assert body["party_size"] == 3
    assert body["table_id"] == "t_2"
    assert body["starts_at_local"] == f"{THURSDAY}T19:00"


def test_amend_with_no_fields_is_a_no_op(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    body = assert_ok(client.patch(f"/reservations/{created['reference']}", json={},
                                  headers=headers_for(token)), 200)
    assert body == created


def test_unknown_fields_in_an_amendment_are_ignored(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    body = assert_ok(client.patch(f"/reservations/{created['reference']}",
                                  json={"party_size": 3, "reference": "HIJACK",
                                        "reservation_id": "nope", "status": "cancelled",
                                        "user_id": "someone", "created_at": "x"},
                                  headers=headers_for(token)), 200)
    assert body["party_size"] == 3
    assert body["reference"] == created["reference"]
    assert body["reservation_id"] == created["reservation_id"]
    assert body["status"] == "confirmed"
    assert body["created_at"] == created["created_at"]


@pytest.mark.parametrize("changes,code", [
    ({"starts_at_local": f"{THURSDAY}T18:15"}, "not_on_slot_grid"),
    ({"starts_at_local": f"{THURSDAY}T17:00"}, "outside_opening_hours"),
    ({"starts_at_local": f"{THURSDAY}T22:00"}, "outside_opening_hours"),
    ({"starts_at_local": f"{SATURDAY}T19:00"}, "outside_opening_hours"),
    ({"starts_at_local": "2026-09-24T19:00Z"}, "validation_failed"),
    ({"party_size": 0}, "validation_failed"),
    ({"party_size": "4"}, "validation_failed"),
    ({"party_size": True}, "validation_failed"),
    ({"table_id": "t_nope"}, "not_found"),
])
def test_amendment_validation_matches_creation(client, seeded, changes, code):
    token = signup(client)["token"]
    created = assert_ok(book(client, token, table_id="t_2", party_size=4), 201)

    response = client.patch(f"/reservations/{created['reference']}", json=changes,
                            headers=headers_for(token))
    assert response.status_code == (404 if code == "not_found" else 422), response.text
    assert error_of(response)["code"] == code

    # A failed amendment leaves the original booking and its occupancy unchanged.
    unchanged = assert_ok(client.get(f"/reservations/{created['reference']}",
                                     headers=headers_for(token)), 200)
    assert unchanged == created
    assert book(client, token, starts_at_local=f"{THURSDAY}T19:00").status_code == 409


def test_amend_into_an_occupied_slot_is_a_conflict(client, seeded):
    mine = signup(client, "mine@example.com")["token"]
    theirs = signup(client, "theirs@example.com")["token"]
    mine_booking = assert_ok(book(client, mine, table_id="t_1", party_size=2,
                                  starts_at_local=f"{THURSDAY}T19:00"), 201)
    theirs_booking = assert_ok(book(client, theirs, table_id="t_2", party_size=2,
                                    starts_at_local=f"{THURSDAY}T19:00"), 201)

    response = client.patch(f"/reservations/{mine_booking['reference']}",
                            json={"table_id": "t_2"}, headers=headers_for(mine))
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"
    assert client.get(f"/reservations/{mine_booking['reference']}",
                      headers=headers_for(mine)).json()["table_id"] == "t_1"
    assert client.get(f"/reservations/{theirs_booking['reference']}",
                      headers=headers_for(theirs)).json()["table_id"] == "t_2"


def test_amending_a_cancelled_reservation_is_a_conflict(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    assert_ok(client.post(f"/reservations/{created['reference']}/cancel",
                          headers=headers_for(token)), 200)

    response = client.patch(f"/reservations/{created['reference']}",
                            json={"party_size": 2}, headers=headers_for(token))
    assert response.status_code == 409
    assert error_of(response)["code"] == "reservation_cancelled"


def test_the_cutoff_is_measured_against_the_current_start(client, seeded):
    """A booking may be moved closer to now than its own cutoff would allow."""
    token = signup(client)["token"]
    created = assert_ok(book(client, token, starts_at_local=f"{THURSDAY}T21:00"), 201)

    # 16:30Z is inside the 120-minute cutoff of 19:00 Berlin (17:00Z) but not of
    # the current 21:00 Berlin (19:00Z), whose deadline is 17:00Z.
    clock.freeze(dt.datetime(2026, 9, 24, 16, 30, tzinfo=dt.timezone.utc))
    body = assert_ok(client.patch(f"/reservations/{created['reference']}",
                                  json={"starts_at_local": f"{THURSDAY}T19:00"},
                                  headers=headers_for(token)), 200)
    assert body["starts_at_local"] == f"{THURSDAY}T19:00"

    # Now the current start is 19:00 Berlin and its deadline has passed.
    response = client.patch(f"/reservations/{created['reference']}",
                            json={"party_size": 2}, headers=headers_for(token))
    assert response.status_code == 409
    assert error_of(response)["code"] == "cutoff_passed"


def test_amending_after_the_cutoff_is_refused(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    clock.freeze(DEADLINE + dt.timedelta(minutes=1))
    response = client.patch(f"/reservations/{created['reference']}",
                            json={"party_size": 2}, headers=headers_for(token))
    assert response.status_code == 409
    assert error_of(response)["code"] == "cutoff_passed"
    assert client.get(f"/reservations/{created['reference']}",
                      headers=headers_for(token)).json()["party_size"] == 4


@pytest.mark.parametrize("changes", [
    {"table_id": 5}, {"starts_at_local": 20260924}, {"table_id": ["t_1"]},
    {"starts_at_local": True},
])
def test_wrong_types_in_an_amendment_are_malformed(client, seeded, changes):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    response = client.patch(f"/reservations/{created['reference']}", json=changes,
                            headers=headers_for(token))
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"


def test_an_invalid_party_size_in_an_amendment_is_validation_failed(client, seeded):
    """party_size keeps its endpoint-specific rule on every path that takes it."""
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    for value in ("3", True, {"n": 3}, [3], 3.0):
        response = client.patch(f"/reservations/{created['reference']}",
                                json={"party_size": value}, headers=headers_for(token))
        assert response.status_code == 422, value
        assert error_of(response)["code"] == "validation_failed"


def test_null_optional_fields_mean_leave_it_alone(client, seeded):
    """An explicit null on an optional amendment field is treated as omitted."""
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    body = assert_ok(client.patch(
        f"/reservations/{created['reference']}",
        json={"table_id": None, "starts_at_local": None, "party_size": None},
        headers=headers_for(token)), 200)
    assert body == created


def test_amendment_body_must_be_an_object(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    response = client.patch(f"/reservations/{created['reference']}", content=b"[]",
                            headers={**headers_for(token),
                                     "Content-Type": "application/json"})
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"


def test_no_idempotency_key_is_needed_to_amend_or_cancel(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    assert_ok(client.patch(f"/reservations/{created['reference']}",
                           json={"party_size": 3},
                           headers={"Authorization": f"Bearer {token}"}), 200)
    assert_ok(client.post(f"/reservations/{created['reference']}/cancel",
                          headers={"Authorization": f"Bearer {token}"}), 200)


def test_an_amendment_does_not_change_the_reference_in_lists(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    assert_ok(client.patch(f"/reservations/{created['reference']}",
                           json={"starts_at_local": f"{THURSDAY}T18:00"},
                           headers=headers_for(token)), 200)
    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)
    assert [r["reference"] for r in listed["reservations"]] == [created["reference"]]
    assert listed["reservations"][0]["starts_at_local"] == f"{THURSDAY}T18:00"


def test_cancel_accepts_an_absent_or_empty_body(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    url = f"/reservations/{created['reference']}/cancel"

    assert_ok(client.post(url, headers=headers_for(token)), 200)

    second = assert_ok(book(client, token, starts_at_local=f"{THURSDAY}T20:30"), 201)
    url = f"/reservations/{second['reference']}/cancel"
    assert_ok(client.post(url, json={}, headers=headers_for(token)), 200)
    assert_ok(client.post(url, content=b"",
                          headers={**headers_for(token),
                                   "Content-Type": "application/json"}), 200)


@pytest.mark.parametrize("raw", [b"{oops", b"[]", b"3", b'"cancelled"'])
def test_cancel_rejects_a_malformed_body(client, seeded, raw):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    response = client.post(f"/reservations/{created['reference']}/cancel", content=raw,
                           headers={**headers_for(token),
                                    "Content-Type": "application/json"})
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"
    # The booking is untouched.
    assert client.get(f"/reservations/{created['reference']}",
                      headers=headers_for(token)).json()["status"] == "confirmed"


def test_unknown_fields_in_a_cancel_body_are_ignored(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    body = assert_ok(client.post(
        f"/reservations/{created['reference']}/cancel",
        json={"reason": "traffic", "status": "confirmed", "reference": "HIJACK"},
        headers=headers_for(token)), 200)
    assert body["status"] == "cancelled"
    assert body["reference"] == created["reference"]
