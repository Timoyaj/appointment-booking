"""POST /reservations and the reservation reads."""

from __future__ import annotations

import re

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

REFERENCE_RE = re.compile(r"^[A-Z0-9]{6,12}$")


def test_booking_returns_the_documented_shape(client, seeded):
    token = signup(client)["token"]
    body = assert_ok(book(client, token, table_id="t_2",
                          starts_at_local=f"{THURSDAY}T19:00", party_size=4), 201)
    assert set(body) == {
        "reservation_id", "reference", "restaurant_id", "table_id", "table_ids",
        "party_size", "status", "starts_at_local", "starts_at", "ends_at", "created_at",
    }
    assert body["table_ids"] == ["t_2"], "a single-table booking is a set of one"
    assert body["restaurant_id"] == "r_anker"
    assert body["table_id"] == "t_2"
    assert body["party_size"] == 4
    assert body["status"] == "confirmed"
    assert body["starts_at_local"] == f"{THURSDAY}T19:00"
    assert body["starts_at"] == "2026-09-24T19:00:00+02:00"
    assert body["ends_at"] == "2026-09-24T20:30:00+02:00"
    assert body["created_at"] == "2026-09-21T11:04:03+00:00"
    assert REFERENCE_RE.match(body["reference"])
    assert len(body["reservation_id"]) <= 64


def test_references_are_unique_and_never_change(client, seeded):
    """Every booking gets its own A-Z0-9 reference, and it never changes."""
    token = signup(client)["token"]
    # Back-to-back non-overlapping sittings (90 minutes each) on both open days.
    slots = [f"{THURSDAY}T18:00", f"{THURSDAY}T19:30", f"{THURSDAY}T21:00",
             "2026-09-25T18:00", "2026-09-25T19:30", "2026-09-25T21:00"]
    references = set()
    for starts_at_local in slots:
        for table_id, party_size in (("t_1", 2), ("t_2", 4)):
            created = assert_ok(book(client, token, table_id=table_id,
                                     starts_at_local=starts_at_local,
                                     party_size=party_size), 201)
            assert REFERENCE_RE.match(created["reference"]), created["reference"]
            references.add(created["reference"])

            fetched = assert_ok(client.get(f"/reservations/{created['reference']}",
                                           headers=headers_for(token)), 200)
            assert fetched["reference"] == created["reference"]
    assert len(references) == len(slots) * 2


def test_a_start_in_the_past_is_allowed(client, seeded):
    """Fixtures may use any date; being in the past is not a reason to refuse."""
    token = signup(client)["token"]
    body = assert_ok(book(client, token, starts_at_local="2026-09-17T19:00"), 201)
    assert body["starts_at"] == "2026-09-17T19:00:00+02:00"


def test_overlapping_booking_is_a_conflict(client, seeded):
    token = signup(client)["token"]
    assert_ok(book(client, token, table_id="t_2", starts_at_local=f"{THURSDAY}T19:00"), 201)

    for starts_at_local in (f"{THURSDAY}T18:00", f"{THURSDAY}T18:30", f"{THURSDAY}T19:00",
                            f"{THURSDAY}T19:30", f"{THURSDAY}T20:00"):
        response = book(client, token, table_id="t_2", starts_at_local=starts_at_local)
        assert response.status_code == 409, starts_at_local
        assert error_of(response)["code"] == "table_unavailable"


def test_occupancy_is_half_open(client, seeded):
    """A 90-minute booking at 19:00 does not overlap one starting at 20:30."""
    token = signup(client)["token"]
    assert_ok(book(client, token, table_id="t_2", starts_at_local=f"{THURSDAY}T19:00"), 201)
    assert_ok(book(client, token, table_id="t_2", starts_at_local=f"{THURSDAY}T20:30"), 201)

    # Back-to-back on the other side: 18:00 + 90 ends exactly when 19:30 starts.
    early = assert_ok(book(client, token, table_id="t_1", party_size=2,
                           starts_at_local=f"{THURSDAY}T18:00"), 201)
    assert early["ends_at"] == "2026-09-24T19:30:00+02:00"
    touching = assert_ok(book(client, token, table_id="t_1", party_size=2,
                              starts_at_local=f"{THURSDAY}T19:30"), 201)
    assert touching["starts_at"] == "2026-09-24T19:30:00+02:00"


def test_another_table_at_the_same_time_is_free(client, seeded):
    token = signup(client)["token"]
    assert_ok(book(client, token, table_id="t_2", starts_at_local=f"{THURSDAY}T19:00"), 201)
    assert_ok(book(client, token, table_id="t_1", starts_at_local=f"{THURSDAY}T19:00",
                  party_size=2), 201)


def test_another_user_cannot_take_a_booked_table(client, seeded):
    first = signup(client, "first@example.com")["token"]
    second = signup(client, "second@example.com")["token"]
    assert_ok(book(client, first, table_id="t_2", starts_at_local=f"{THURSDAY}T19:00"), 201)
    response = book(client, second, table_id="t_2", starts_at_local=f"{THURSDAY}T19:00")
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"


@pytest.mark.parametrize("starts_at_local", [
    f"{THURSDAY}T18:15", f"{THURSDAY}T18:45", f"{THURSDAY}T19:10", f"{THURSDAY}T20:45",
])
def test_off_grid_times_are_rejected(client, seeded, starts_at_local):
    token = signup(client)["token"]
    response = book(client, token, starts_at_local=starts_at_local)
    assert response.status_code == 422
    assert error_of(response)["code"] == "not_on_slot_grid"


@pytest.mark.parametrize("starts_at_local", [
    f"{THURSDAY}T17:00",   # before opening
    f"{THURSDAY}T17:30",   # would start before opening
    f"{THURSDAY}T22:00",   # 22:00 + 90 ends at 23:30, after the 23:00 close
    f"{THURSDAY}T23:00",   # at closing time
    f"{SATURDAY}T19:00",   # closed day
])
def test_outside_opening_hours(client, seeded, starts_at_local):
    token = signup(client)["token"]
    response = book(client, token, starts_at_local=starts_at_local)
    assert response.status_code == 422, response.text
    assert error_of(response)["code"] == "outside_opening_hours"


def test_the_last_bookable_slot_is_allowed(client, seeded):
    token = signup(client)["token"]
    # 21:30 + 90 = 23:00, exactly closing time on a Thursday.
    body = assert_ok(book(client, token, starts_at_local=f"{THURSDAY}T21:30"), 201)
    assert body["ends_at"] == "2026-09-24T23:00:00+02:00"


@pytest.mark.parametrize("table_id,party_size", [("t_1", 3), ("t_1", 4), ("t_2", 5),
                                                 ("t_2", 100)])
def test_party_exceeding_capacity(client, seeded, table_id, party_size):
    token = signup(client)["token"]
    response = book(client, token, table_id=table_id, party_size=party_size)
    assert response.status_code == 422
    assert error_of(response)["code"] == "party_exceeds_capacity"


@pytest.mark.parametrize("party_size", [0, -1, -100])
def test_party_size_below_one(client, seeded, party_size):
    token = signup(client)["token"]
    response = book(client, token, party_size=party_size)
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"


@pytest.mark.parametrize("party_size", ["4", "four", True, False, 4.0, 4.5, None, [4], {"n": 4}])
def test_party_size_of_the_wrong_type_is_validation_failed(client, seeded, party_size):
    """The spec calls out strings and booleans by name: 422, not 400."""
    token = signup(client)["token"]
    response = book(client, token, party_size=party_size)
    assert response.status_code == 422, response.text
    assert error_of(response)["code"] == "validation_failed"


@pytest.mark.parametrize("restaurant_id,table_id", [
    ("r_nope", "t_2"),
    ("r_anker", "t_nope"),
])
def test_unknown_restaurant_or_table_is_404(client, seeded, restaurant_id, table_id):
    token = signup(client)["token"]
    response = book(client, token, restaurant_id=restaurant_id, table_id=table_id)
    assert response.status_code == 404, response.text
    assert error_of(response)["code"] == "not_found"


def test_a_table_of_another_restaurant_is_404(client):
    from tests.conftest import base_fixture, reset

    fixture = base_fixture()
    fixture["restaurants"].append({
        "id": "r_zweit", "name": "Zweit", "timezone": "Europe/Berlin",
        "slot_minutes": 30, "reservation_duration_minutes": 90,
        "cancellation_cutoff_minutes": 120,
        "opening_hours": [{"weekday": "thu", "opens": "18:00", "closes": "23:00"}],
        "tables": [{"id": "z_1", "label": "1", "capacity": 4}],
    })
    reset(client, fixture)
    token = signup(client)["token"]

    response = book(client, token, restaurant_id="r_anker", table_id="z_1")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"

    assert_ok(book(client, token, restaurant_id="r_zweit", table_id="z_1"), 201)


@pytest.mark.parametrize("starts_at_local", [
    "2026-09-24T19:00+02:00", "2026-09-24T19:00Z", "2026-09-24T19:00:00",
    "2026-09-24 19:00", "2026-09-24", "19:00", "24/09/2026 19:00",
    "2026-09-24T19:00 ", "", "2026-13-24T19:00", "2026-09-24T25:00",
])
def test_starts_at_local_must_be_bare_local(client, seeded, starts_at_local):
    token = signup(client)["token"]
    response = book(client, token, starts_at_local=starts_at_local)
    assert response.status_code == 422, response.text
    assert error_of(response)["code"] == "validation_failed"


@pytest.mark.parametrize("field,value", [
    ("restaurant_id", 5), ("restaurant_id", True), ("restaurant_id", None),
    ("restaurant_id", ["r_anker"]), ("table_id", 5), ("starts_at_local", 20260924),
    ("starts_at_local", None), ("starts_at_local", ["x"]),
])
def test_wrong_json_types_are_malformed(client, seeded, field, value):
    token = signup(client)["token"]
    response = book(client, token, extra={field: value})
    assert response.status_code == 400, response.text
    assert error_of(response)["code"] == "malformed_request"


@pytest.mark.parametrize("field", ["restaurant_id", "table_id", "starts_at_local",
                                   "party_size"])
def test_missing_required_fields_are_validation_failed(client, seeded, field):
    token = signup(client)["token"]
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": f"{THURSDAY}T19:00", "party_size": 4}
    del body[field]
    response = client.post("/reservations", json=body, headers=headers_for(token, "k"))
    assert response.status_code == 422, response.text
    assert error_of(response)["code"] == "validation_failed"


def test_an_empty_id_is_validation_failed(client, seeded):
    token = signup(client)["token"]
    response = book(client, token, extra={"table_id": ""})
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"


def test_overlong_ids_are_validation_failed(client, seeded):
    token = signup(client)["token"]
    response = book(client, token, extra={"restaurant_id": "r" * 65})
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"

    response = book(client, token, extra={"table_id": "t" * 65})
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"


def test_unknown_body_fields_are_ignored(client, seeded):
    token = signup(client)["token"]
    body = assert_ok(book(client, token, extra={
        "notes": "window please", "party_name": "Ada", "status": "cancelled",
        "reservation_id": "hijack", "reference": "HIJACK", "user_id": "someone",
        "created_at": "1999-01-01T00:00:00+00:00", "nested": {"a": [1, 2]}}), 201)
    assert body["status"] == "confirmed"
    assert body["reference"] != "HIJACK"
    assert body["reservation_id"] != "hijack"
    assert body["created_at"] == "2026-09-21T11:04:03+00:00"


def test_body_must_be_a_json_object(client, seeded):
    token = signup(client)["token"]
    for raw in (b"", b"[]", b"3", b'"x"', b"{broken"):
        response = client.post("/reservations", content=raw,
                               headers={**headers_for(token, "k"),
                                        "Content-Type": "application/json"})
        assert response.status_code == 400, raw
        assert error_of(response)["code"] == "malformed_request"


def test_missing_idempotency_key(client, seeded):
    token = signup(client)["token"]
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": f"{THURSDAY}T19:00", "party_size": 4}

    response = client.post("/reservations", json=body, headers=headers_for(token))
    assert response.status_code == 400
    assert error_of(response)["code"] == "missing_idempotency_key"

    for empty in ("", "   "):
        response = client.post("/reservations", json=body,
                               headers=headers_for(token, empty))
        assert response.status_code == 400
        assert error_of(response)["code"] == "missing_idempotency_key"


def test_idempotency_key_length_limits(client, seeded):
    token = signup(client)["token"]
    assert_ok(book(client, token, key="k" * 255), 201)
    response = book(client, token, key="k" * 256, starts_at_local=f"{THURSDAY}T20:30")
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"


def test_listing_is_mine_newest_first_and_includes_cancelled(client, seeded):
    token = signup(client)["token"]
    other = signup(client, "other@example.com")["token"]

    early = assert_ok(book(client, token, starts_at_local=f"{THURSDAY}T18:00", table_id="t_1",
                           party_size=2), 201)
    late = assert_ok(book(client, token, starts_at_local=f"{THURSDAY}T21:00", table_id="t_1",
                          party_size=2), 201)
    assert_ok(book(client, other, starts_at_local=f"{THURSDAY}T19:30", table_id="t_2"), 201)

    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)
    assert [r["reference"] for r in listed["reservations"]] == [
        late["reference"], early["reference"]]

    assert_ok(client.post(f"/reservations/{late['reference']}/cancel",
                          headers=headers_for(token)), 200)
    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)
    assert [r["status"] for r in listed["reservations"]] == ["cancelled", "confirmed"]
    assert listed["reservations"][0]["reference"] == late["reference"]


def test_an_empty_list_is_returned_as_an_empty_list(client, seeded):
    token = signup(client)["token"]
    assert assert_ok(client.get("/reservations", headers=headers_for(token)), 200) == {
        "reservations": []}


def test_reading_a_reservation_by_reference(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token), 201)
    fetched = assert_ok(client.get(f"/reservations/{created['reference']}",
                                   headers=headers_for(token)), 200)
    assert fetched == created


def test_another_users_reservation_is_404(client, seeded):
    mine = signup(client, "mine@example.com")["token"]
    theirs = signup(client, "theirs@example.com")["token"]
    created = assert_ok(book(client, mine), 201)

    for endpoint in (f"/reservations/{created['reference']}",
                     f"/reservations/{created['reference']}/cancel"):
        if endpoint.endswith("/cancel"):
            response = client.post(endpoint, headers=headers_for(theirs))
        else:
            response = client.get(endpoint, headers=headers_for(theirs))
        assert response.status_code == 404, endpoint
        assert error_of(response)["code"] == "not_found"

    response = client.patch(f"/reservations/{created['reference']}",
                            json={"party_size": 2}, headers=headers_for(theirs))
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"

    response = client.post("/reservation-moves",
                           json={"moves": [{"reference": created["reference"]}]},
                           headers=headers_for(theirs, "k"))
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_unknown_reference_is_404(client, seeded):
    token = signup(client)["token"]
    assert client.get("/reservations/NOPE123", headers=headers_for(token)).status_code == 404
    assert client.post("/reservations/NOPE123/cancel",
                       headers=headers_for(token)).status_code == 404
    assert client.patch("/reservations/NOPE123", json={"party_size": 2},
                        headers=headers_for(token)).status_code == 404
