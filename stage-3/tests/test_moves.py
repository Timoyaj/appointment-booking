"""POST /reservation-moves (§11) — atomic multi-booking changes."""

from __future__ import annotations

import datetime as dt

import pytest

from tablekeeper import clock
from tests.conftest import (
    THURSDAY,
    assert_ok,
    base_fixture,
    book,
    error_of,
    headers_for,
    login_headers,
    reset,
    signup,
)

FRIDAY = "2026-09-25"


def moves(client, token, payload, key="move-key"):
    return client.post("/reservation-moves", json=payload, headers=headers_for(token, key))


def two_bookings(client, token, *, first=("t_1", 2), second=("t_2", 2),
                 starts_at_local=f"{THURSDAY}T19:00"):
    a = assert_ok(book(client, token, table_id=first[0], party_size=first[1],
                       starts_at_local=starts_at_local), 201)
    b = assert_ok(book(client, token, table_id=second[0], party_size=second[1],
                       starts_at_local=starts_at_local), 201)
    return a, b


def test_a_swap_commits_as_one_unit(client, seeded):
    """Each move alone would clash with the other's table; together they fit."""
    token = signup(client)["token"]
    a, b = two_bookings(client, token)

    # Judged one at a time against the current state, both are impossible.
    assert client.patch(f"/reservations/{a['reference']}", json={"table_id": "t_2"},
                        headers=headers_for(token)).status_code == 409
    assert client.patch(f"/reservations/{b['reference']}", json={"table_id": "t_1"},
                        headers=headers_for(token)).status_code == 409

    body = assert_ok(moves(client, token, {"moves": [
        {"reference": a["reference"], "table_id": "t_2"},
        {"reference": b["reference"], "table_id": "t_1"},
    ]}), 201)

    assert [r["reference"] for r in body["reservations"]] == [a["reference"], b["reference"]]
    assert body["reservations"][0]["table_id"] == "t_2"
    assert body["reservations"][1]["table_id"] == "t_1"


def test_success_is_201_and_includes_unchanged_items_in_input_order(client, seeded):
    token = signup(client)["token"]
    a, b = two_bookings(client, token)

    body = assert_ok(moves(client, token, {"moves": [
        {"reference": b["reference"], "starts_at_local": f"{THURSDAY}T20:30"},
        {"reference": a["reference"]},  # listed but unchanged
    ]}), 201)

    assert len(body["reservations"]) == 2
    assert body["reservations"][0]["reference"] == b["reference"]
    assert body["reservations"][0]["starts_at_local"] == f"{THURSDAY}T20:30"
    assert body["reservations"][1] == a


def test_identity_owner_and_creation_time_never_change(client, seeded):
    token = signup(client)["token"]
    a, _b = two_bookings(client, token)

    body = assert_ok(moves(client, token, {"moves": [
        {"reference": a["reference"], "table_id": "t_2", "party_size": 4,
         "starts_at_local": f"{THURSDAY}T20:30"},
    ]}), 201)
    moved = body["reservations"][0]
    assert moved["reservation_id"] == a["reservation_id"]
    assert moved["reference"] == a["reference"]
    assert moved["created_at"] == a["created_at"]
    assert moved["status"] == "confirmed"

    fetched = assert_ok(client.get(f"/reservations/{a['reference']}",
                                   headers=headers_for(token)), 200)
    assert fetched == moved
    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)
    assert len(listed["reservations"]) == 2


def test_a_no_op_move_retains_all_values(client, seeded):
    token = signup(client)["token"]
    a, _b = two_bookings(client, token)
    body = assert_ok(moves(client, token, {"moves": [
        {"reference": a["reference"], "table_id": "t_1",
         "starts_at_local": f"{THURSDAY}T19:00", "party_size": 2}]}), 201)
    assert body["reservations"][0] == a


def test_unchanged_listed_bookings_still_occupy_their_table(client, seeded):
    """Listing a booking without changing it does not release its slot."""
    token = signup(client)["token"]
    a, b = two_bookings(client, token)

    response = moves(client, token, {"moves": [
        {"reference": a["reference"], "table_id": "t_2"},  # wants b's table
        {"reference": b["reference"]},                     # unchanged, still on t_2
    ]})
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"


def test_an_overlap_between_resulting_bookings_is_a_conflict(client, seeded):
    token = signup(client)["token"]
    a, b = two_bookings(client, token, first=("t_1", 2), second=("t_2", 2),
                        starts_at_local=f"{THURSDAY}T18:00")
    c = assert_ok(book(client, token, table_id="t_1", party_size=2,
                       starts_at_local=f"{THURSDAY}T21:00"), 201)
    assert b["table_id"] == "t_2"

    response = moves(client, token, {"moves": [
        {"reference": a["reference"], "starts_at_local": f"{THURSDAY}T19:30"},
        {"reference": c["reference"], "starts_at_local": f"{THURSDAY}T20:00"},
    ]}, key="clash")
    assert response.status_code == 409
    error = error_of(response)
    assert error["code"] == "table_unavailable"
    assert a["reference"] in error["message"] and c["reference"] in error["message"]
    assert b["reference"] not in error["message"]


def test_an_overlap_with_an_unlisted_booking_is_a_conflict(client, seeded):
    mine = signup(client, "mine@example.com")["token"]
    theirs = signup(client, "theirs@example.com")["token"]
    unlisted = assert_ok(book(client, theirs, table_id="t_2", party_size=2,
                              starts_at_local=f"{THURSDAY}T20:30"), 201)
    movable = assert_ok(book(client, mine, table_id="t_1", party_size=2,
                             starts_at_local=f"{THURSDAY}T19:00"), 201)

    response = moves(client, mine, {"moves": [
        {"reference": movable["reference"], "table_id": "t_2",
         "starts_at_local": f"{THURSDAY}T20:00"}]})
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"
    assert client.get(f"/reservations/{unlisted['reference']}",
                      headers=headers_for(theirs)).json()["table_id"] == "t_2"


def test_everything_or_nothing(client, seeded):
    """A rejected set changes no occupancy, no record and burns no retry key."""
    token = signup(client)["token"]
    a, b = two_bookings(client, token)
    before = client.get("/_test/export").json()["state"]

    response = moves(client, token, {"moves": [
        {"reference": a["reference"], "starts_at_local": f"{THURSDAY}T20:30"},  # fine
        {"reference": b["reference"], "party_size": 6},                        # too big
    ]}, key="all-or-nothing")
    assert response.status_code == 409 or response.status_code == 422
    assert error_of(response)["code"] == "party_exceeds_capacity"

    after = client.get("/_test/export").json()["state"]
    assert after["reservations"] == before["reservations"]
    assert after["idempotency"] == before["idempotency"]

    # The key was not consumed by the failure, so a corrected set with the same
    # key still works.
    fixed = assert_ok(moves(client, token, {"moves": [
        {"reference": a["reference"], "starts_at_local": f"{THURSDAY}T20:30"},
        {"reference": b["reference"], "party_size": 4},
    ]}, key="all-or-nothing"), 201)
    assert fixed["reservations"][0]["starts_at_local"] == f"{THURSDAY}T20:30"
    assert fixed["reservations"][1]["party_size"] == 4


def test_unknown_or_foreign_references_are_404(client, seeded):
    token = signup(client)["token"]
    other = signup(client, "other@example.com")["token"]
    theirs = assert_ok(book(client, other), 201)
    mine = assert_ok(book(client, token, table_id="t_1", party_size=2), 201)

    for reference in ("NOPE123", theirs["reference"]):
        response = moves(client, token, {"moves": [
            {"reference": mine["reference"], "party_size": 1},
            {"reference": reference, "party_size": 1}]}, key=f"k-{reference}")
        assert response.status_code == 404, reference
        assert error_of(response)["code"] == "not_found"


def test_moves_must_stay_inside_one_restaurant(client):
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

    here = assert_ok(book(client, token, restaurant_id="r_anker", table_id="t_2"), 201)
    there = assert_ok(book(client, token, restaurant_id="r_zweit", table_id="z_1"), 201)

    response = moves(client, token, {"moves": [
        {"reference": here["reference"], "party_size": 3},
        {"reference": there["reference"], "party_size": 3}]})
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"
    assert client.get(f"/reservations/{here['reference']}",
                      headers=headers_for(token)).json()["party_size"] == 4


@pytest.mark.parametrize("payload", [
    {"moves": []},
    {"moves": "not-a-list"},
    {"moves": {}},
    {"moves": [[{"reference": "X"}]]},
    {"moves": ["reference"]},
    {"moves": [{}]},
    {"moves": [{"reference": ""}]},
    {"moves": [{"reference": 5}]},
    {"moves": [{"reference": None}]},
    {"moves": [{"reference": "R" * 65}]},
    {"moves": [{"reference": "A"}, {"reference": "A"}]},
    {},
])
def test_invalid_move_shapes_are_validation_failed(client, seeded, payload):
    token = signup(client)["token"]
    response = moves(client, token, payload)
    assert response.status_code == 422, payload
    assert error_of(response)["code"] == "validation_failed"


def test_more_than_eight_moves_is_rejected(client, seeded):
    token = signup(client)["token"]
    a, _b = two_bookings(client, token)
    response = moves(client, token, {"moves": [
        {"reference": a["reference"]}] * 8 + [{"reference": "EXTRA01"}]})
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"


def test_eight_moves_is_allowed(client):
    fixture = base_fixture()
    slots = [(THURSDAY, "18:00"), (THURSDAY, "19:30"), (THURSDAY, "21:00"),
             (FRIDAY, "18:00")]
    fixture["reservations"] = [
        {"id": f"res_seed_{index}", "reference": f"SEED{index:04d}", "user_id": "u_ada",
         "restaurant_id": "r_anker", "table_id": table_id,
         "starts_at_local": f"{day}T{hour}", "party_size": 2}
        for index, ((day, hour), table_id) in enumerate(
            [(slot, table) for slot in slots for table in ("t_1", "t_2")])
    ]
    assert len(fixture["reservations"]) == 8
    reset(client, fixture)
    headers = login_headers(client)

    body = assert_ok(moves(client, headers["Authorization"].split(" ")[1], {"moves": [
        {"reference": reservation["reference"], "party_size": 1}
        for reservation in fixture["reservations"]
    ]}), 201)
    assert len(body["reservations"]) == 8
    assert all(r["party_size"] == 1 for r in body["reservations"])
    assert [r["reference"] for r in body["reservations"]] == [
        reservation["reference"] for reservation in fixture["reservations"]]


def test_a_cancelled_booking_cannot_be_moved(client, seeded):
    token = signup(client)["token"]
    a, b = two_bookings(client, token)
    assert_ok(client.post(f"/reservations/{b['reference']}/cancel",
                          headers=headers_for(token)), 200)

    response = moves(client, token, {"moves": [
        {"reference": a["reference"], "party_size": 1},
        {"reference": b["reference"], "party_size": 1}]})
    assert response.status_code == 409
    assert error_of(response)["code"] == "reservation_cancelled"
    assert client.get(f"/reservations/{a['reference']}",
                      headers=headers_for(token)).json()["party_size"] == 2


def test_the_cutoff_applies_to_each_booking(client, seeded):
    token = signup(client)["token"]
    early = assert_ok(book(client, token, table_id="t_1", party_size=2,
                           starts_at_local=f"{THURSDAY}T19:00"), 201)
    late = assert_ok(book(client, token, table_id="t_2", party_size=2,
                          starts_at_local=f"{THURSDAY}T21:00"), 201)

    # 16:00Z is past the 15:00Z deadline of the 19:00 Berlin booking but not
    # past the 17:00Z deadline of the 21:00 Berlin one.
    clock.freeze(dt.datetime(2026, 9, 24, 16, 0, tzinfo=dt.timezone.utc))
    response = moves(client, token, {"moves": [
        {"reference": early["reference"], "party_size": 1},
        {"reference": late["reference"], "party_size": 1}]})
    assert response.status_code == 409
    assert error_of(response)["code"] == "cutoff_passed"

    ok = assert_ok(moves(client, token, {"moves": [
        {"reference": late["reference"], "party_size": 1}]}, key="second"), 201)
    assert ok["reservations"][0]["party_size"] == 1


def test_a_cutoff_error_precedes_other_problems_for_that_booking(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(book(client, token, table_id="t_1", party_size=2,
                             starts_at_local=f"{THURSDAY}T19:00"), 201)
    clock.freeze(dt.datetime(2026, 9, 24, 16, 0, tzinfo=dt.timezone.utc))

    # Off-grid *and* past the cutoff: the cutoff error wins for this booking.
    response = moves(client, token, {"moves": [
        {"reference": created["reference"], "starts_at_local": f"{THURSDAY}T18:15"}]})
    assert response.status_code == 409
    assert error_of(response)["code"] == "cutoff_passed"


def test_non_occupancy_errors_take_precedence_in_input_order(client, seeded):
    token = signup(client)["token"]
    a = assert_ok(book(client, token, table_id="t_1", party_size=2,
                       starts_at_local=f"{THURSDAY}T18:00"), 201)
    b = assert_ok(book(client, token, table_id="t_2", party_size=2,
                       starts_at_local=f"{THURSDAY}T18:00"), 201)

    # move 0 has a capacity problem, move 1 an off-grid problem.
    response = moves(client, token, {"moves": [
        {"reference": a["reference"], "party_size": 6},
        {"reference": b["reference"], "starts_at_local": f"{THURSDAY}T18:15"}]})
    assert response.status_code == 422
    assert error_of(response)["code"] == "party_exceeds_capacity"

    # Swapped order, swapped answer.
    response = moves(client, token, {"moves": [
        {"reference": b["reference"], "starts_at_local": f"{THURSDAY}T18:15"},
        {"reference": a["reference"], "party_size": 6}]}, key="other-order")
    assert response.status_code == 422
    assert error_of(response)["code"] == "not_on_slot_grid"


def test_non_occupancy_errors_beat_occupancy_errors(client, seeded):
    token = signup(client)["token"]
    a = assert_ok(book(client, token, table_id="t_1", party_size=2,
                       starts_at_local=f"{THURSDAY}T18:00"), 201)
    b = assert_ok(book(client, token, table_id="t_2", party_size=2,
                       starts_at_local=f"{THURSDAY}T18:00"), 201)

    # move 1 would clash with move 0's result, but move 0 has a capacity problem,
    # which is a non-occupancy error and therefore reported first.
    response = moves(client, token, {"moves": [
        {"reference": a["reference"], "table_id": "t_2", "party_size": 6},
        {"reference": b["reference"], "table_id": "t_2",
         "starts_at_local": f"{THURSDAY}T18:00"}]})
    assert response.status_code == 422
    assert error_of(response)["code"] == "party_exceeds_capacity"


def test_an_unknown_table_in_a_move_is_404(client, seeded):
    token = signup(client)["token"]
    a, _b = two_bookings(client, token)
    response = moves(client, token, {"moves": [
        {"reference": a["reference"], "table_id": "t_nope"}]})
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_a_move_outside_opening_hours_or_off_grid(client, seeded):
    token = signup(client)["token"]
    a, _b = two_bookings(client, token)
    for value, code in ((f"{THURSDAY}T17:00", "outside_opening_hours"),
                        (f"{THURSDAY}T18:15", "not_on_slot_grid"),
                        ("2026-09-26T19:00", "outside_opening_hours")):
        response = moves(client, token, {"moves": [
            {"reference": a["reference"], "starts_at_local": value}]}, key=value)
        assert response.status_code == 422, value
        assert error_of(response)["code"] == code


def test_wrong_types_inside_a_move_are_malformed(client, seeded):
    token = signup(client)["token"]
    a, _b = two_bookings(client, token)
    for changes in ({"table_id": 5}, {"starts_at_local": 20260924},
                    {"table_id": ["t_2"]}, {"starts_at_local": True}):
        response = moves(client, token, {"moves": [
            {"reference": a["reference"], **changes}]}, key=str(changes))
        assert response.status_code == 400, changes
        assert error_of(response)["code"] == "malformed_request"


def test_a_bad_local_time_inside_a_move_is_validation_failed(client, seeded):
    """A string that is not a bare local time is 422, not 400."""
    token = signup(client)["token"]
    a, _b = two_bookings(client, token)
    for value in (f"{THURSDAY}T19:00Z", f"{THURSDAY}T19:00+02:00", "19:00",
                  f"{THURSDAY} 19:00"):
        response = moves(client, token, {"moves": [
            {"reference": a["reference"], "starts_at_local": value}]}, key=value)
        assert response.status_code == 422, value
        assert error_of(response)["code"] == "validation_failed"


def test_an_invalid_party_size_inside_a_move_is_validation_failed(client, seeded):
    token = signup(client)["token"]
    a, _b = two_bookings(client, token)
    for value in (0, "2", True, 2.0):
        response = moves(client, token, {"moves": [
            {"reference": a["reference"], "party_size": value}]}, key=str(value))
        assert response.status_code == 422, value
        assert error_of(response)["code"] == "validation_failed"


def test_unknown_fields_inside_a_move_are_ignored(client, seeded):
    token = signup(client)["token"]
    a, _b = two_bookings(client, token)
    body = assert_ok(moves(client, token, {"moves": [
        {"reference": a["reference"], "party_size": 1, "notes": "window",
         "reservation_id": "hijack", "created_at": "1999-01-01T00:00:00+00:00",
         "status": "cancelled", "user_id": "someone"}]}), 201)
    moved = body["reservations"][0]
    assert moved["party_size"] == 1
    assert moved["reservation_id"] == a["reservation_id"]
    assert moved["created_at"] == a["created_at"]
    assert moved["status"] == "confirmed"


def test_moves_require_a_token_and_a_key(client, seeded):
    a_token = signup(client)["token"]
    a, _b = two_bookings(client, a_token)
    payload = {"moves": [{"reference": a["reference"], "party_size": 1}]}

    assert client.post("/reservation-moves", json=payload).status_code == 401
    response = client.post("/reservation-moves", json=payload,
                           headers={"Authorization": f"Bearer {a_token}"})
    assert response.status_code == 400
    assert error_of(response)["code"] == "missing_idempotency_key"


def test_a_rejected_move_set_is_not_replayed(client, seeded):
    """A failure is not recorded, so the same key can carry the corrected set."""
    token = signup(client)["token"]
    a, b = two_bookings(client, token)
    bad = moves(client, token, {"moves": [
        {"reference": a["reference"], "table_id": "t_nope"},
        {"reference": b["reference"], "party_size": 1}]}, key="retry-me")
    assert bad.status_code == 404

    good = assert_ok(moves(client, token, {"moves": [
        {"reference": a["reference"], "party_size": 1},
        {"reference": b["reference"], "party_size": 1}]}, key="retry-me"), 201)
    assert [r["party_size"] for r in good["reservations"]] == [1, 1]


def test_a_seeded_booking_can_be_moved(client):
    fixture = base_fixture()
    fixture["reservations"] = [{
        "id": "res_seed_1", "reference": "SEED0001", "user_id": "u_ada",
        "restaurant_id": "r_anker", "table_id": "t_1",
        "starts_at_local": f"{THURSDAY}T19:00", "party_size": 2,
    }]
    reset(client, fixture)
    headers = login_headers(client)
    token = headers["Authorization"].split(" ")[1]

    body = assert_ok(moves(client, token, {"moves": [
        {"reference": "SEED0001", "table_id": "t_2", "party_size": 4}]}), 201)
    assert body["reservations"][0]["table_id"] == "t_2"
    assert body["reservations"][0]["party_size"] == 4
    assert body["reservations"][0]["reference"] == "SEED0001"
    assert body["reservations"][0]["reservation_id"] == "res_seed_1"
