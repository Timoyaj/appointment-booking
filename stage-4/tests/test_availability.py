"""GET /availability — the slot grid."""

from __future__ import annotations

import pytest

from tests.conftest import (
    SATURDAY,
    THURSDAY,
    assert_ok,
    book,
    error_of,
    headers_for,
    signup,
)


def avail(client, **params):
    return client.get("/availability", params=params)


def test_thursday_grid_is_the_slot_grid(client, seeded):
    body = assert_ok(avail(client, restaurant_id="r_anker", date=THURSDAY, party_size=2), 200)
    assert body["restaurant_id"] == "r_anker"
    assert body["date"] == THURSDAY
    assert body["timezone"] == "Europe/Berlin"
    # 18:00-23:00, 90-minute reservations, 30-minute grid: last start is 21:30.
    assert [slot["starts_at_local"] for slot in body["slots"]] == [
        f"{THURSDAY}T18:00", f"{THURSDAY}T18:30", f"{THURSDAY}T19:00",
        f"{THURSDAY}T19:30", f"{THURSDAY}T20:00", f"{THURSDAY}T20:30",
        f"{THURSDAY}T21:00", f"{THURSDAY}T21:30",
    ]
    assert body["slots"][0]["starts_at"] == "2026-09-24T18:00:00+02:00"
    assert body["slots"][-1]["starts_at"] == "2026-09-24T21:30:00+02:00"


def test_friday_runs_later(client, seeded):
    body = assert_ok(avail(client, restaurant_id="r_anker", date="2026-09-25", party_size=2), 200)
    assert body["slots"][-1]["starts_at_local"] == "2026-09-25T22:00"


def test_a_closed_day_has_no_slots(client, seeded):
    body = assert_ok(avail(client, restaurant_id="r_anker", date=SATURDAY, party_size=2), 200)
    assert body["slots"] == []
    assert body["date"] == SATURDAY


@pytest.mark.parametrize("params,missing", [
    ({"date": THURSDAY, "party_size": 2}, "restaurant_id"),
    ({"restaurant_id": "r_anker", "party_size": 2}, "date"),
    ({"restaurant_id": "r_anker", "date": THURSDAY}, "party_size"),
])
def test_all_three_parameters_are_required(client, seeded, params, missing):
    response = avail(client, **params)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "validation_failed"
    assert missing in error["message"]


def test_unknown_restaurant_is_404(client, seeded):
    response = avail(client, restaurant_id="r_nope", date=THURSDAY, party_size=2)
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


@pytest.mark.parametrize("value", ["4.0", "1e9", "+4", "-4", "four", "", "4 ", "04x"])
def test_integer_query_parameters_must_be_plain_digits(client, seeded, value):
    response = avail(client, restaurant_id="r_anker", date=THURSDAY, party_size=value)
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"


def test_party_size_zero_is_validation_failed(client, seeded):
    response = avail(client, restaurant_id="r_anker", date=THURSDAY, party_size="0")
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"


@pytest.mark.parametrize("date", ["24-09-2026", "2026/09/24", "2026-09-24T18:00",
                                  "2026-13-01", "2026-09-31", "thursday", ""])
def test_bad_dates_are_validation_failed(client, seeded, date):
    response = avail(client, restaurant_id="r_anker", date=date, party_size=2)
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"


def test_unknown_query_parameters_are_ignored(client, seeded):
    body = assert_ok(avail(client, restaurant_id="r_anker", date=THURSDAY,
                           party_size=2, surprise="hi", page=3), 200)
    assert body["slots"]


def test_capacity_filters_tables_in_fixture_order(client, seeded):
    small = assert_ok(avail(client, restaurant_id="r_anker", date=THURSDAY, party_size=2), 200)
    assert small["slots"][0]["available_table_ids"] == ["t_1", "t_2"]

    four = assert_ok(avail(client, restaurant_id="r_anker", date=THURSDAY, party_size=4), 200)
    assert four["slots"][0]["available_table_ids"] == ["t_2"]

    six = assert_ok(avail(client, restaurant_id="r_anker", date=THURSDAY, party_size=6), 200)
    # No table seats six: every slot still appears, with an empty list.
    assert all(slot["available_table_ids"] == [] for slot in six["slots"])
    assert len(six["slots"]) == 8


def test_a_booked_table_disappears_and_returns_after_cancellation(client, seeded):
    token = signup(client)["token"]
    before = assert_ok(avail(client, restaurant_id="r_anker", date=THURSDAY, party_size=4), 200)
    slot = next(s for s in before["slots"] if s["starts_at_local"] == f"{THURSDAY}T19:00")
    assert slot["available_table_ids"] == ["t_2"]

    created = assert_ok(book(client, token, table_id="t_2",
                             starts_at_local=f"{THURSDAY}T19:00", party_size=4), 201)

    during = assert_ok(avail(client, restaurant_id="r_anker", date=THURSDAY, party_size=4), 200)
    slot = next(s for s in during["slots"] if s["starts_at_local"] == f"{THURSDAY}T19:00")
    assert slot["available_table_ids"] == []

    # The next slot over is unaffected: occupancy is half-open.
    slot = next(s for s in during["slots"] if s["starts_at_local"] == f"{THURSDAY}T20:30")
    assert slot["available_table_ids"] == ["t_2"]

    assert_ok(client.post(f"/reservations/{created['reference']}/cancel",
                          headers=headers_for(token)), 200)
    after = assert_ok(avail(client, restaurant_id="r_anker", date=THURSDAY, party_size=4), 200)
    slot = next(s for s in after["slots"] if s["starts_at_local"] == f"{THURSDAY}T19:00")
    assert slot["available_table_ids"] == ["t_2"]


def test_availability_is_shared_across_users(client, seeded):
    """One diner's booking is visible in another's search — and to the public."""
    first = signup(client, "one@example.com")["token"]
    second = signup(client, "two@example.com")["token"]
    assert_ok(book(client, first, table_id="t_2", starts_at_local=f"{THURSDAY}T19:00"), 201)

    body = assert_ok(avail(client, restaurant_id="r_anker", date=THURSDAY, party_size=4), 200)
    slot = next(s for s in body["slots"] if s["starts_at_local"] == f"{THURSDAY}T19:00")
    assert slot["available_table_ids"] == []

    mine = assert_ok(client.get("/reservations", headers=headers_for(second)), 200)
    assert mine["reservations"] == []


def test_cancelled_bookings_do_not_block_a_slot(client):
    """A seeded cancelled booking must not occupy its table."""
    from tests.conftest import base_fixture, reset

    with_cancelled = base_fixture()
    with_cancelled["reservations"] = [
        {"id": "res_c", "reference": "SEEDCANC", "user_id": "u_ada",
         "restaurant_id": "r_anker", "table_id": "t_2",
         "starts_at_local": f"{THURSDAY}T19:00", "party_size": 4,
         "status": "cancelled"}
    ]
    reset(client, with_cancelled)

    body = assert_ok(avail(client, restaurant_id="r_anker", date=THURSDAY, party_size=4), 200)
    slot = next(s for s in body["slots"] if s["starts_at_local"] == f"{THURSDAY}T19:00")
    assert slot["available_table_ids"] == ["t_2"]


def test_two_service_windows_on_one_day(client):
    from tests.conftest import base_fixture, reset

    fixture = base_fixture()
    fixture["restaurants"][0]["opening_hours"] = [
        {"weekday": "thu", "opens": "12:00", "closes": "14:00"},
        {"weekday": "thu", "opens": "18:00", "closes": "20:00"},
    ]
    reset(client, fixture)
    body = assert_ok(avail(client, restaurant_id="r_anker", date=THURSDAY, party_size=2), 200)
    # Lunch 12:00, 12:30 (12:30+90 = 14:00 <= 14:00); dinner 18:00, 18:30.
    assert [slot["starts_at_local"] for slot in body["slots"]] == [
        f"{THURSDAY}T12:00", f"{THURSDAY}T12:30",
        f"{THURSDAY}T18:00", f"{THURSDAY}T18:30",
    ]
