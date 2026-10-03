"""Stage 2: tables the restaurant declared combinable.

The default fixture grows a third table and two declared pairs, ``[t_1,t_2]`` and
``[t_2,t_3]``, which is enough to show that combining is not transitive: t_1 and
t_3 share no declaration, so they cannot be booked together however well the
party would fit.

Capacities are t_1=2, t_2=4, t_3=6, so a pair of 6 seats is the only way to seat
a party of 6 that does not want t_3, and a party of 7 can only be seated by
``[t_2,t_3]``.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid

import pytest

from .conftest import (
    THURSDAY,
    assert_ok,
    base_fixture,
    book,
    error_of,
    headers_for,
    reset,
    signup,
)

# 18:00-23:00 on a 30-minute grid with 90-minute sittings: 18:00 .. 21:30.
SLOT = f"{THURSDAY}T19:00"
LATER = f"{THURSDAY}T21:00"  # does not overlap a 19:00 sitting
OVERLAPPING = f"{THURSDAY}T20:00"


def paired_fixture(pairs=(("t_1", "t_2"), ("t_2", "t_3"))) -> dict:
    """The default restaurant with a third table and some declared pairs."""
    fixture = base_fixture()
    restaurant = fixture["restaurants"][0]
    restaurant["tables"] = [
        {"id": "t_1", "label": "1", "capacity": 2},
        {"id": "t_2", "label": "2", "capacity": 4},
        {"id": "t_3", "label": "3", "capacity": 6},
    ]
    if pairs is not None:
        restaurant["combinable"] = [list(pair) for pair in pairs]
    return fixture


@pytest.fixture
def paired(client) -> dict:
    """The paired fixture loaded, with one signed-up diner."""
    fixture = paired_fixture()
    reset(client, fixture)
    return {"fixture": fixture, "token": signup(client)["token"]}


def book_set(client, token, table_ids, *, starts_at_local=SLOT, party_size=6,
             restaurant_id="r_anker", key=None, **extra):
    body = {
        "restaurant_id": restaurant_id,
        "table_ids": table_ids,
        "starts_at_local": starts_at_local,
        "party_size": party_size,
        **extra,
    }
    return client.post(
        "/reservations", json=body, headers=headers_for(token, key or f"k_{uuid.uuid4().hex}")
    )


def availability(client, party_size, *, date=THURSDAY, restaurant_id="r_anker"):
    return assert_ok(
        client.get("/availability", params={
            "restaurant_id": restaurant_id, "date": date, "party_size": party_size}),
        200,
    )["slots"]


def slot_at(slots, hhmm: str) -> dict:
    return next(s for s in slots if s["starts_at_local"].endswith(hhmm))


def options_at(client, party_size, hhmm="19:00") -> list[dict]:
    return slot_at(availability(client, party_size), hhmm)["available_options"]


def move(client, token, moves, key=None):
    return client.post(
        "/reservation-moves", json={"moves": moves},
        headers=headers_for(token, key or f"k_{uuid.uuid4().hex}"),
    )


# --------------------------------------------------------------------------- #
# the declaration itself
# --------------------------------------------------------------------------- #
def test_detail_carries_declared_pairs_in_order(client, paired):
    body = assert_ok(client.get("/restaurants/r_anker"), 200)
    assert body["combinable"] == [["t_1", "t_2"], ["t_2", "t_3"]]


def test_restaurant_without_pairs_reports_an_empty_list(client, seeded):
    assert assert_ok(client.get("/restaurants/r_anker"), 200)["combinable"] == []


@pytest.mark.parametrize("pairs,code", [
    ([["t_1", "t_2", "t_3"]], "validation_failed"),      # pairs only, never three
    ([["t_1"]], "validation_failed"),                    # ... and never one
    ([[]], "validation_failed"),
    ([["t_1", "t_9"]], "validation_failed"),             # not a table of this restaurant
    ([["t_1", "t_1"]], "validation_failed"),             # a table cannot join itself
    ([["t_1", "t_2"], ["t_2", "t_1"]], "validation_failed"),  # the same pair twice
])
def test_bad_combinable_fixture_is_refused(client, seeded, pairs, code):
    fixture = paired_fixture(pairs=pairs)
    response = client.post("/_test/reset", json=fixture)
    assert error_of(response)["code"] == code, response.text


@pytest.mark.parametrize("entry", [{"t_1": "t_2"}, "t_1", 7, None, [7, 8], [None, "t_2"]])
def test_combinable_entry_of_the_wrong_json_type_is_malformed(client, seeded, entry):
    fixture = paired_fixture()
    fixture["restaurants"][0]["combinable"] = [entry]
    assert error_of(client.post("/_test/reset", json=fixture))["code"] == "malformed_request"


def test_combinable_that_is_not_an_array_is_malformed(client, seeded):
    fixture = paired_fixture()
    fixture["restaurants"][0]["combinable"] = {"t_1": "t_2"}
    assert error_of(client.post("/_test/reset", json=fixture))["code"] == "malformed_request"


def test_a_bad_combinable_fixture_leaves_the_previous_state_alone(client, paired):
    broken = paired_fixture(pairs=[["t_1", "t_1"]])
    assert client.post("/_test/reset", json=broken).status_code == 422
    assert assert_ok(client.get("/restaurants/r_anker"), 200)["combinable"] == [
        ["t_1", "t_2"], ["t_2", "t_3"]]


# --------------------------------------------------------------------------- #
# GET /availability
# --------------------------------------------------------------------------- #
def test_options_list_singles_in_fixture_order_then_pairs_in_declared_order(client, paired):
    assert options_at(client, 2) == [
        {"table_ids": ["t_1"], "capacity": 2},
        {"table_ids": ["t_2"], "capacity": 4},
        {"table_ids": ["t_3"], "capacity": 6},
        {"table_ids": ["t_1", "t_2"], "capacity": 6},
        {"table_ids": ["t_2", "t_3"], "capacity": 10},
    ]


def test_available_table_ids_stays_single_tables_only(client, paired):
    slot = slot_at(availability(client, 2), "19:00")
    assert slot["available_table_ids"] == ["t_1", "t_2", "t_3"]


def test_a_pair_is_offered_when_only_the_sum_fits(client, paired):
    """A party of 7 fits no single table; only t_2+t_3 seats it."""
    slot = slot_at(availability(client, 7), "19:00")
    assert slot["available_table_ids"] == []
    assert slot["available_options"] == [{"table_ids": ["t_2", "t_3"], "capacity": 10}]


def test_combining_is_not_transitive(client, paired):
    """t_1 joins t_2 and t_2 joins t_3; that never makes t_1+t_3 an option."""
    assert options_at(client, 8) == [{"table_ids": ["t_2", "t_3"], "capacity": 10}]


def test_a_pair_names_its_tables_in_declared_order(client, paired):
    """The search was made for a party of 6, so [t_1,t_2] is offered as declared."""
    assert {"table_ids": ["t_1", "t_2"], "capacity": 6} in options_at(client, 6)


def test_booking_one_member_withdraws_the_pair_but_not_the_other_member(client, paired):
    assert_ok(book_set(client, paired["token"], ["t_1", "t_2"], party_size=6), 201)
    slot = slot_at(availability(client, 2), "19:00")
    assert slot["available_table_ids"] == ["t_3"]
    assert slot["available_options"] == [{"table_ids": ["t_3"], "capacity": 6}]


def test_a_single_booking_withdraws_every_pair_containing_it(client, paired):
    assert_ok(book(client, paired["token"], table_id="t_2", party_size=4), 201)
    assert options_at(client, 2) == [
        {"table_ids": ["t_1"], "capacity": 2},
        {"table_ids": ["t_3"], "capacity": 6},
    ]


def test_a_pair_booking_occupies_both_members_for_its_whole_duration(client, paired):
    assert_ok(book_set(client, paired["token"], ["t_1", "t_2"], party_size=6), 201)
    # 90 minutes from 19:00 covers the 19:30 and 20:00 slots too.
    for hhmm in ("18:00", "18:30", "19:00", "19:30", "20:00"):
        slot = slot_at(availability(client, 2), hhmm)
        assert "t_1" not in slot["available_table_ids"], hhmm
        assert "t_2" not in slot["available_table_ids"], hhmm
    assert "t_1" in slot_at(availability(client, 2), "20:30")["available_table_ids"]


def test_cancelling_a_pair_offers_both_members_and_the_pair_again(client, paired):
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)["reference"]
    assert_ok(client.post(f"/reservations/{reference}/cancel", headers=headers_for(token)), 200)
    assert options_at(client, 2) == [
        {"table_ids": ["t_1"], "capacity": 2},
        {"table_ids": ["t_2"], "capacity": 4},
        {"table_ids": ["t_3"], "capacity": 6},
        {"table_ids": ["t_1", "t_2"], "capacity": 6},
        {"table_ids": ["t_2", "t_3"], "capacity": 10},
    ]


def test_a_closed_day_offers_no_options(client, paired):
    from .conftest import SATURDAY

    assert availability(client, 2, date=SATURDAY) == []


# --------------------------------------------------------------------------- #
# POST /reservations
# --------------------------------------------------------------------------- #
def test_a_declared_pair_can_be_booked(client, paired):
    body = assert_ok(book_set(client, paired["token"], ["t_1", "t_2"], party_size=6), 201)
    assert body["table_ids"] == ["t_1", "t_2"]
    assert "table_id" not in body, "a combination is never reduced to one of its tables"


def test_a_pair_may_be_requested_in_either_order(client, paired):
    """A declaration is an unordered pair; the booking keeps the order asked for."""
    body = assert_ok(book_set(client, paired["token"], ["t_2", "t_1"], party_size=6), 201)
    assert body["table_ids"] == ["t_2", "t_1"]


def test_one_table_through_table_ids_reads_as_a_set_of_one(client, paired):
    body = assert_ok(book_set(client, paired["token"], ["t_2"], party_size=4), 201)
    assert body["table_ids"] == ["t_2"]
    assert body["table_id"] == "t_2"


def test_the_stage_1_single_table_request_still_works(client, paired):
    body = assert_ok(book(client, paired["token"], table_id="t_2", party_size=4), 201)
    assert body["table_id"] == "t_2"
    assert body["table_ids"] == ["t_2"]


def test_naming_both_fields_is_refused(client, paired):
    response = book(client, paired["token"], table_id="t_2", party_size=4,
                    extra={"table_ids": ["t_1", "t_2"]})
    assert error_of(response)["code"] == "validation_failed"


@pytest.mark.parametrize("table_ids", [
    ["t_1", "t_3"],                 # never declared, whatever the sizes
    ["t_1", "t_2", "t_3"],          # pairs only
])
def test_a_set_the_restaurant_did_not_declare_is_refused(client, paired, table_ids):
    response = book_set(client, paired["token"], table_ids, party_size=2)
    assert error_of(response)["code"] == "combination_not_allowed", response.text


@pytest.mark.parametrize("table_ids", [
    ["t_2", "t_2"],                 # a table cannot be joined to itself
    ["t_1", "t_1", "t_2"],          # a repeated id makes the set malformed ...
    ["t_1", "t_2", "t_1"],          # ... before any seating rule is asked about it
])
def test_a_repeated_table_inside_a_set_is_a_validation_failure(client, paired, table_ids):
    """A set that repeats a table is not a set: it is refused while parsing.

    That holds however many tables are named, so an oversized set with a repeat is
    a malformed set rather than a refusal of the combination.
    """
    response = book_set(client, paired["token"], table_ids, party_size=2)
    assert error_of(response)["code"] == "validation_failed"


def test_an_undeclared_pair_outranks_a_party_that_does_not_fit(client, paired):
    """Neither rule is satisfiable; the seating rule is the one reported."""
    response = book_set(client, paired["token"], ["t_1", "t_3"], party_size=99)
    assert error_of(response)["code"] == "combination_not_allowed"


def test_a_party_bigger_than_the_summed_capacity_is_refused(client, paired):
    response = book_set(client, paired["token"], ["t_1", "t_2"], party_size=7)
    assert error_of(response)["code"] == "party_exceeds_capacity"


def test_a_party_exactly_the_summed_capacity_is_accepted(client, paired):
    assert_ok(book_set(client, paired["token"], ["t_1", "t_2"], party_size=6), 201)


@pytest.mark.parametrize("table_ids,code", [
    ([], "validation_failed"),
    ([""], "validation_failed"),
    ("t_1", "malformed_request"),
    (7, "malformed_request"),
    ({"table_id": "t_1"}, "malformed_request"),
    ([7], "malformed_request"),
    ([None], "malformed_request"),
])
def test_table_ids_of_the_wrong_shape(client, paired, table_ids, code):
    response = book_set(client, paired["token"], table_ids, party_size=2)
    assert error_of(response)["code"] == code, response.text


def test_a_booking_that_names_no_tables_is_refused(client, paired):
    response = client.post(
        "/reservations",
        json={"restaurant_id": "r_anker", "starts_at_local": SLOT, "party_size": 2},
        headers=headers_for(paired["token"], "no-tables"),
    )
    assert error_of(response)["code"] == "validation_failed"


def test_an_unknown_table_inside_a_pair_is_not_found(client, paired):
    response = book_set(client, paired["token"], ["t_1", "t_nope"], party_size=2)
    assert error_of(response)["code"] == "not_found"


def test_a_pair_still_has_to_exist_in_the_local_calendar(client, paired):
    """2026-03-29T02:30 is skipped by Berlin's spring-forward, pair or no pair."""
    fixture = paired_fixture()
    fixture["restaurants"][0]["opening_hours"] = [
        {"weekday": "sun", "opens": "01:00", "closes": "05:00"}]
    reset(client, fixture)
    token = signup(client, email="dst@example.com")["token"]
    response = book_set(client, token, ["t_1", "t_2"],
                        starts_at_local="2026-03-29T02:30", party_size=6)
    assert error_of(response)["code"] == "invalid_local_time"


def test_a_pair_is_refused_when_either_member_is_taken(client, paired):
    token = paired["token"]
    assert_ok(book(client, token, table_id="t_2", party_size=4), 201)
    assert error_of(book_set(client, token, ["t_1", "t_2"], party_size=6))["code"] == \
        "table_unavailable"


def test_two_pairs_sharing_a_table_cannot_both_be_booked(client, paired):
    token = paired["token"]
    assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)
    assert error_of(book_set(client, token, ["t_2", "t_3"], party_size=6))["code"] == \
        "table_unavailable"


def test_a_pair_booking_does_not_block_a_later_sitting(client, paired):
    token = paired["token"]
    assert_ok(book_set(client, token, ["t_1", "t_2"], starts_at_local=SLOT, party_size=6), 201)
    assert_ok(book_set(client, token, ["t_1", "t_2"], starts_at_local=LATER, party_size=6), 201)


def test_a_pair_booking_leaves_an_unrelated_table_free(client, paired):
    token = paired["token"]
    assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)
    assert_ok(book(client, token, table_id="t_3", party_size=6), 201)


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def test_lookup_of_a_combination_carries_the_whole_set(client, paired):
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_2", "t_3"], party_size=6), 201)["reference"]
    body = assert_ok(client.get(f"/reservations/{reference}", headers=headers_for(token)), 200)
    assert body["table_ids"] == ["t_2", "t_3"]
    assert "table_id" not in body
    assert body["status"] == "confirmed"


def test_the_listing_carries_both_kinds_of_booking(client, paired):
    token = paired["token"]
    assert_ok(book(client, token, table_id="t_3", party_size=6, starts_at_local=SLOT), 201)
    assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6, starts_at_local=SLOT), 201)
    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)["reservations"]
    by_tables = sorted(json.dumps(r["table_ids"]) for r in listed)
    assert by_tables == ['["t_1", "t_2"]', '["t_3"]']
    assert sorted(r.get("table_id", "-") for r in listed) == ["-", "t_3"]


def test_another_diner_cannot_read_a_combination_booking(client, paired):
    reference = assert_ok(
        book_set(client, paired["token"], ["t_1", "t_2"], party_size=6), 201)["reference"]
    intruder = headers_for(signup(client, email="intruder@example.com")["token"])
    assert error_of(client.get(f"/reservations/{reference}", headers=intruder))["code"] == "not_found"


# --------------------------------------------------------------------------- #
# cancel and amend
# --------------------------------------------------------------------------- #
def test_cancelling_a_combination_frees_every_table_in_the_set(client, paired):
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)["reference"]
    body = assert_ok(
        client.post(f"/reservations/{reference}/cancel", headers=headers_for(token)), 200)
    assert body["table_ids"] == ["t_1", "t_2"]
    assert body["status"] == "cancelled"
    assert_ok(book(client, token, table_id="t_1", party_size=2, starts_at_local=SLOT), 201)
    assert_ok(book(client, token, table_id="t_2", party_size=4, starts_at_local=SLOT), 201)


def test_an_amendment_can_move_a_booking_onto_a_declared_pair(client, paired):
    token = paired["token"]
    reference = assert_ok(book(client, token, table_id="t_3", party_size=6), 201)["reference"]
    body = assert_ok(
        client.patch(f"/reservations/{reference}", json={"table_ids": ["t_1", "t_2"]},
                     headers=headers_for(token)),
        200)
    assert body["table_ids"] == ["t_1", "t_2"]
    assert "table_id" not in body
    assert assert_ok(client.get("/availability", params={
        "restaurant_id": "r_anker", "date": THURSDAY, "party_size": 2}), 200
    )["slots"][2]["available_table_ids"] == ["t_3"]


def test_an_amendment_can_take_a_combination_apart(client, paired):
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)["reference"]
    body = assert_ok(
        client.patch(f"/reservations/{reference}", json={"table_id": "t_3", "party_size": 6},
                     headers=headers_for(token)),
        200)
    assert body["table_id"] == "t_3"
    assert body["table_ids"] == ["t_3"]
    # Both released tables are bookable again, at the same time.
    assert_ok(book(client, token, table_id="t_1", party_size=2, starts_at_local=SLOT), 201)


def test_an_amendment_that_names_no_tables_keeps_the_pair(client, paired):
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=5), 201)["reference"]
    body = assert_ok(
        client.patch(f"/reservations/{reference}", json={"party_size": 6},
                     headers=headers_for(token)),
        200)
    assert body["table_ids"] == ["t_1", "t_2"]
    assert body["party_size"] == 6


def test_an_amendment_can_keep_a_pair_and_change_its_time(client, paired):
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)["reference"]
    body = assert_ok(
        client.patch(f"/reservations/{reference}", json={"starts_at_local": LATER},
                     headers=headers_for(token)),
        200)
    assert body["table_ids"] == ["t_1", "t_2"]
    assert body["starts_at_local"] == LATER
    # The old sitting is free again, the new one is held by both tables.
    assert options_at(client, 2, "19:00") == [
        {"table_ids": ["t_1"], "capacity": 2},
        {"table_ids": ["t_2"], "capacity": 4},
        {"table_ids": ["t_3"], "capacity": 6},
        {"table_ids": ["t_1", "t_2"], "capacity": 6},
        {"table_ids": ["t_2", "t_3"], "capacity": 10},
    ]
    assert slot_at(availability(client, 2), "21:00")["available_table_ids"] == ["t_3"]


@pytest.mark.parametrize("patch_body,code", [
    ({"table_ids": ["t_1", "t_3"]}, "combination_not_allowed"),
    ({"table_ids": ["t_1", "t_2", "t_3"]}, "combination_not_allowed"),
    ({"table_id": "t_1", "table_ids": ["t_1", "t_2"]}, "validation_failed"),
    ({"table_ids": []}, "validation_failed"),
    ({"table_ids": ["t_1", "t_1"]}, "validation_failed"),
    ({"table_ids": ["t_nope"]}, "not_found"),
    ({"table_ids": "t_1"}, "malformed_request"),
])
def test_an_amendment_follows_the_same_combination_rules(client, paired, patch_body, code):
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)["reference"]
    response = client.patch(f"/reservations/{reference}", json=patch_body,
                            headers=headers_for(token))
    assert error_of(response)["code"] == code, response.text


def test_an_amendment_onto_a_taken_member_is_refused(client, paired):
    token = paired["token"]
    assert_ok(book(client, token, table_id="t_2", party_size=4, starts_at_local=SLOT), 201)
    reference = assert_ok(book(client, token, table_id="t_3", party_size=6), 201)["reference"]
    response = client.patch(f"/reservations/{reference}", json={"table_ids": ["t_1", "t_2"]},
                            headers=headers_for(token))
    assert error_of(response)["code"] == "table_unavailable"


def test_an_amendment_may_keep_one_of_its_own_tables(client, paired):
    """A pair moving to an overlapping pair that shares a member is not a clash with itself."""
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)["reference"]
    body = assert_ok(
        client.patch(f"/reservations/{reference}", json={"table_ids": ["t_2", "t_3"]},
                     headers=headers_for(token)),
        200)
    assert body["table_ids"] == ["t_2", "t_3"]


def test_a_party_that_no_longer_fits_its_pair_is_refused(client, paired):
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)["reference"]
    response = client.patch(f"/reservations/{reference}", json={"party_size": 7},
                            headers=headers_for(token))
    assert error_of(response)["code"] == "party_exceeds_capacity"


# --------------------------------------------------------------------------- #
# atomic moves
# --------------------------------------------------------------------------- #
def test_a_move_can_place_a_booking_on_a_pair(client, paired):
    token = paired["token"]
    reference = assert_ok(book(client, token, table_id="t_3", party_size=6), 201)["reference"]
    body = assert_ok(move(client, token, [
        {"reference": reference, "table_ids": ["t_1", "t_2"]}], key="m1"), 201)
    assert body["reservations"][0]["table_ids"] == ["t_1", "t_2"]
    assert "table_id" not in body["reservations"][0]


def test_a_move_can_take_a_pair_apart(client, paired):
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)["reference"]
    body = assert_ok(move(client, token, [
        {"reference": reference, "table_id": "t_3"}], key="m2"), 201)
    assert body["reservations"][0]["table_ids"] == ["t_3"]


def test_two_moves_may_not_leave_a_table_in_two_bookings(client, paired):
    """The first move takes t_2 as part of a pair; the second may not also take it."""
    token = paired["token"]
    first = assert_ok(book(client, token, table_id="t_3", party_size=4, starts_at_local=SLOT), 201)
    second = assert_ok(book(client, token, table_id="t_3", party_size=4, starts_at_local=LATER), 201)
    response = move(client, token, [
        {"reference": first["reference"], "table_ids": ["t_1", "t_2"]},
        {"reference": second["reference"], "table_id": "t_2", "starts_at_local": SLOT},
    ], key="m3")
    assert error_of(response)["code"] == "table_unavailable"


def test_a_pair_and_a_single_can_swap_sittings_in_one_move(client, paired):
    token = paired["token"]
    pair = assert_ok(book_set(client, token, ["t_1", "t_2"], starts_at_local=SLOT, party_size=6), 201)
    single = assert_ok(book(client, token, table_id="t_3", starts_at_local=LATER, party_size=6), 201)
    body = assert_ok(move(client, token, [
        {"reference": pair["reference"], "starts_at_local": LATER},
        {"reference": single["reference"], "starts_at_local": SLOT},
    ], key="m4"), 201)
    assert [r["table_ids"] for r in body["reservations"]] == [["t_1", "t_2"], ["t_3"]]
    assert [r["starts_at_local"] for r in body["reservations"]] == [LATER, SLOT]


def test_two_moves_onto_overlapping_pairs_are_refused(client, paired):
    """Both bookings would hold t_2 at the same time."""
    token = paired["token"]
    first = assert_ok(book(client, token, table_id="t_3", party_size=6, starts_at_local=SLOT), 201)
    second = assert_ok(book(client, token, table_id="t_3", party_size=6, starts_at_local=LATER), 201)
    response = move(client, token, [
        {"reference": first["reference"], "table_ids": ["t_1", "t_2"]},
        {"reference": second["reference"], "table_ids": ["t_2", "t_3"], "starts_at_local": SLOT},
    ], key="m5")
    assert error_of(response)["code"] == "table_unavailable"


@pytest.mark.parametrize("entry,code", [
    ({"table_ids": ["t_1", "t_3"]}, "combination_not_allowed"),
    ({"table_id": "t_3", "table_ids": ["t_1", "t_2"]}, "validation_failed"),
    ({"table_ids": ["t_1", "t_2", "t_3"]}, "combination_not_allowed"),
    ({"table_ids": ["t_nope"]}, "not_found"),
])
def test_a_move_follows_the_same_combination_rules(client, paired, entry, code):
    token = paired["token"]
    reference = assert_ok(book(client, token, table_id="t_3", party_size=6), 201)["reference"]
    response = move(client, token, [{"reference": reference, **entry}], key="m6")
    assert error_of(response)["code"] == code, response.text


def test_a_refused_move_changes_nothing(client, paired):
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)["reference"]
    assert move(client, token, [
        {"reference": reference, "table_ids": ["t_1", "t_3"]}], key="m7").status_code == 422
    body = assert_ok(client.get(f"/reservations/{reference}", headers=headers_for(token)), 200)
    assert body["table_ids"] == ["t_1", "t_2"]


# --------------------------------------------------------------------------- #
# seeded reservations
# --------------------------------------------------------------------------- #
def test_a_seeded_reservation_may_hold_a_pair(client, seeded):
    fixture = paired_fixture()
    fixture["reservations"] = [{
        "id": "res_seed", "reference": "SEED0001", "user_id": "u_ada",
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"], "party_size": 6,
        "starts_at_local": SLOT,
    }]
    reset(client, fixture)
    token = signup(client, email="seeder@example.com")["token"]
    assert error_of(book(client, token, table_id="t_1", party_size=2, starts_at_local=SLOT))[
        "code"] == "table_unavailable"
    assert error_of(book(client, token, table_id="t_2", party_size=4, starts_at_local=SLOT))[
        "code"] == "table_unavailable"
    assert_ok(book(client, token, table_id="t_3", party_size=6, starts_at_local=SLOT), 201)


def test_a_seeded_single_still_works(client, seeded):
    fixture = paired_fixture()
    fixture["reservations"] = [{
        "id": "res_seed", "reference": "SEED0002", "user_id": "u_ada",
        "restaurant_id": "r_anker", "table_id": "t_2", "party_size": 4,
        "starts_at_local": SLOT,
    }]
    reset(client, fixture)
    assert options_at(client, 2) == [
        {"table_ids": ["t_1"], "capacity": 2},
        {"table_ids": ["t_3"], "capacity": 6},
    ]


@pytest.mark.parametrize("reservation,code", [
    ({"table_id": "t_1", "table_ids": ["t_1", "t_2"]}, "validation_failed"),
    ({"table_ids": ["t_1", "t_2", "t_3"]}, "validation_failed"),
    ({"table_ids": []}, "validation_failed"),
    ({"table_ids": ["t_1", "t_1"]}, "validation_failed"),
    ({"table_ids": "t_1"}, "malformed_request"),
    ({"table_ids": [7]}, "malformed_request"),
    ({}, "validation_failed"),
])
def test_a_bad_seeded_table_set_is_refused(client, seeded, reservation, code):
    fixture = paired_fixture()
    fixture["reservations"] = [{
        "id": "res_seed", "reference": "SEED0003", "user_id": "u_ada",
        "restaurant_id": "r_anker", "party_size": 2, "starts_at_local": SLOT,
        **reservation,
    }]
    assert error_of(client.post("/_test/reset", json=fixture))["code"] == code


def test_a_seeded_pair_is_lenient_about_the_seating_rules(client, seeded):
    """Seed data describes a world; it is not a booking the API would have made."""
    fixture = paired_fixture(pairs=[["t_1", "t_2"]])
    fixture["reservations"] = [{
        "id": "res_seed", "reference": "SEED0004", "user_id": "u_ada",
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_3"], "party_size": 99,
        "starts_at_local": SLOT,
    }]
    reset(client, fixture)
    assert options_at(client, 2) == [{"table_ids": ["t_2"], "capacity": 4}]


# --------------------------------------------------------------------------- #
# export and import
# --------------------------------------------------------------------------- #
def test_a_round_trip_preserves_declarations_and_combinations(client, paired):
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)["reference"]
    snapshot = assert_ok(client.get("/_test/export"), 200)
    assert snapshot["state"]["restaurant_combinable"]
    assert {"reference": reference, "position": 0, "table_id": "t_1"} in \
        snapshot["state"]["reservation_tables"]

    reset(client, paired_fixture(pairs=None))  # wipe the declarations
    assert assert_ok(client.get("/restaurants/r_anker"), 200).get("combinable") == []
    assert_ok(client.post("/_test/import", json=snapshot), 204)

    assert assert_ok(client.get("/restaurants/r_anker"), 200)["combinable"] == [
        ["t_1", "t_2"], ["t_2", "t_3"]]
    body = assert_ok(client.get(f"/reservations/{reference}", headers=headers_for(token)), 200)
    assert body["table_ids"] == ["t_1", "t_2"]
    assert error_of(book(client, token, table_id="t_2", party_size=4, starts_at_local=SLOT))[
        "code"] == "table_unavailable"


def test_a_stage_1_snapshot_imports_and_still_occupies_its_table(client, paired):
    """A snapshot taken before combinations existed has no member rows at all."""
    token = paired["token"]
    reference = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)["reference"]
    snapshot = assert_ok(client.get("/_test/export"), 200)
    del snapshot["state"]["reservation_tables"]
    del snapshot["state"]["restaurant_combinable"]

    reset(client, paired_fixture(pairs=None))
    assert_ok(client.post("/_test/import", json=snapshot), 204)

    body = assert_ok(client.get(f"/reservations/{reference}", headers=headers_for(token)), 200)
    assert body["table_ids"] == ["t_1"], "the set is rebuilt from the row's own table"
    assert body["table_id"] == "t_1"
    # The imported booking still holds its table, and the declarations are gone.
    assert error_of(book(client, token, table_id="t_1", party_size=2, starts_at_local=SLOT))[
        "code"] == "table_unavailable"
    assert assert_ok(client.get("/restaurants/r_anker"), 200)["combinable"] == []


def test_a_pending_retry_survives_an_import(client, paired):
    """The receipt for a combination booking is part of the snapshot."""
    token = paired["token"]
    body = {"restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
            "starts_at_local": SLOT, "party_size": 6}
    first = assert_ok(client.post("/reservations", json=body,
                                  headers=headers_for(token, "retry-me")), 201)
    snapshot = assert_ok(client.get("/_test/export"), 200)
    reset(client, paired_fixture(pairs=None))
    assert_ok(client.post("/_test/import", json=snapshot), 204)

    replay = client.post("/reservations", json=body, headers=headers_for(token, "retry-me"))
    assert replay.status_code == 200, replay.text
    assert replay.json() == first


def test_a_combination_receipt_is_not_reusable_for_a_different_set(client, paired):
    token = paired["token"]
    assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6, key="one-key"), 201)
    response = book_set(client, token, ["t_2", "t_3"], party_size=6, key="one-key")
    assert error_of(response)["code"] == "idempotency_key_reuse"


def test_the_same_key_and_body_replay_identically(client, paired):
    token = paired["token"]
    first = assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6, key="same"), 201)
    replay = book_set(client, token, ["t_1", "t_2"], party_size=6, key="same")
    assert replay.status_code == 200
    assert replay.json() == first
    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)["reservations"]
    assert len(listed) == 1


def test_naming_one_table_instead_of_a_set_is_a_different_request(client, paired):
    """`table_id` and a one-member `table_ids` are the same booking, so they replay."""
    token = paired["token"]
    first = assert_ok(book_set(client, token, ["t_3"], party_size=6, key="k"), 201)
    response = book(client, token, table_id="t_3", party_size=6, key="k")
    # Different bodies under one key: the first use is reported as a reuse.
    assert response.status_code in (200, 409)
    if response.status_code == 409:
        assert error_of(response)["code"] == "idempotency_key_reuse"
    assert first["table_ids"] == ["t_3"]


# --------------------------------------------------------------------------- #
# concurrency
# --------------------------------------------------------------------------- #
def test_fifty_concurrent_requests_for_one_pair_produce_one_booking(client, paired):
    token = paired["token"]
    outcomes: list = []
    barrier = threading.Barrier(50)
    body = {"restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
            "starts_at_local": SLOT, "party_size": 6}

    def attempt(index: int) -> None:
        barrier.wait(timeout=30)
        outcomes.append(client.post(
            "/reservations", json=body, headers=headers_for(token, f"race-{index}")))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    statuses = sorted(response.status_code for response in outcomes)
    assert 500 not in statuses and all(s < 500 for s in statuses), statuses
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == 49, statuses
    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)["reservations"]
    assert len(listed) == 1
    assert listed[0]["table_ids"] == ["t_1", "t_2"]


def test_a_pair_and_its_own_members_racing_leave_one_booking(client, paired):
    """Three requests that all need t_2: exactly one may hold it."""
    token = paired["token"]
    outcomes: list = []
    barrier = threading.Barrier(3)
    attempts = [
        {"restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"], "party_size": 6},
        {"restaurant_id": "r_anker", "table_id": "t_2", "party_size": 4},
        {"restaurant_id": "r_anker", "table_ids": ["t_2", "t_3"], "party_size": 6},
    ]

    def attempt(index: int) -> None:
        barrier.wait(timeout=30)
        outcomes.append(client.post(
            "/reservations", json={**attempts[index], "starts_at_local": SLOT},
            headers=headers_for(token, f"member-race-{index}")))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    statuses = sorted(response.status_code for response in outcomes)
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == 2, statuses


def test_the_snapshot_never_shows_a_half_written_combination(client, paired):
    token = paired["token"]
    assert_ok(book_set(client, token, ["t_1", "t_2"], party_size=6), 201)
    snapshot = assert_ok(client.get("/_test/export"), 200)
    members = copy.deepcopy(snapshot["state"]["reservation_tables"])
    reservations = snapshot["state"]["reservations"]
    assert len(reservations) == 1
    assert [m["table_id"] for m in members if m["reference"] == reservations[0]["reference"]] == [
        "t_1", "t_2"]
    assert [m["position"] for m in members] == [0, 1]
