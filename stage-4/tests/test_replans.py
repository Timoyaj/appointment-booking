"""Stage 4: seating plans after a table closes.

A closure puts every booking that overlaps it in question, and the plan that
answers it is a *repair*: as few diners as possible move, the seats they are given
are as tight as their parties allow, and where two seatings cost the same the one
the room lists first wins. Previewing a plan changes nothing but the plan itself;
applying one changes the room, and is refused the moment the room has changed
since the plan was proposed.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid

import pytest

from .conftest import (
    BERLIN_SUMMER,
    FRIDAY,
    THURSDAY,
    adopt,
    apply_plan,
    assert_ok,
    availability_of,
    book,
    closure_body,
    error_of,
    export,
    headers_for,
    instant,
    plan_fixture,
    publish,
    replan,
    reset,
    seat,
    series_of,
    signup,
    slot_at,
    three_table_policy_body,
    token_for,
)

AT_1900 = f"{THURSDAY}T19:00"
AT_2100 = f"{THURSDAY}T21:00"
FULL_EVENING = closure_body("t_2", THURSDAY, opens="18:00", closes="23:00")


@pytest.fixture
def room(client) -> dict:
    """The three-table managed room, loaded, with its manager's token."""
    fixture = plan_fixture()
    reset(client, fixture)
    return {"fixture": fixture, "token": token_for(client)}


@pytest.fixture
def diner(client) -> dict:
    """A second diner, who manages nothing."""
    return {"token": signup(client, email="bob@example.com",
                            display_name="Bob")["token"]}


def propose(client, token, body=None, **kwargs):
    return replan(client, token, FULL_EVENING if body is None else body, **kwargs)


def proposal(client, token, body=None, **kwargs) -> dict:
    return assert_ok(propose(client, token, body, **kwargs), 201)


def revision_of(client, restaurant_id: str = "r_anker") -> int:
    state = export(client)["state"]
    return next(r["revision"] for r in state["restaurants"] if r["id"] == restaurant_id)


def options_at(client, date_str: str, party_size: int, hhmm: str) -> list[tuple]:
    day = availability_of(client, date_str, party_size)
    return [tuple(option["table_ids"]) for option in slot_at(day, hhmm)["available_options"]]


def two_restaurants() -> dict:
    """Ada manages two rooms; only the second has a fourth table."""
    fixture = plan_fixture()
    other = copy.deepcopy(fixture["restaurants"][0])
    other["id"] = "r_harbour"
    other["name"] = "Harbour Room"
    other["tables"].append({"id": "t_4", "label": "4", "capacity": 8})
    other["manager_user_ids"] = ["u_ada"]
    fixture["restaurants"].append(other)
    return fixture


def four_tables(combinable=None) -> dict:
    return plan_fixture(
        tables=[{"id": "t_1", "label": "1", "capacity": 2},
                {"id": "t_2", "label": "2", "capacity": 4},
                {"id": "t_3", "label": "3", "capacity": 4},
                {"id": "t_4", "label": "4", "capacity": 6}],
        combinable=[] if combinable is None else combinable)


# --------------------------------------------------------------------------- #
# who may propose a plan
# --------------------------------------------------------------------------- #
def test_a_manager_gets_a_plan_of_the_documented_shape(client, room):
    plan = proposal(client, room["token"])
    assert set(plan) == {"plan_id", "restaurant_revision", "closure", "assignments",
                         "moved_count", "unused_seats"}, plan
    assert plan["plan_id"]
    assert plan["assignments"] == []
    assert plan["moved_count"] == 0 and plan["unused_seats"] == 0


def test_the_closure_is_echoed_exactly_as_it_was_asked_for(client, room):
    body = closure_body("t_3", THURSDAY, opens="19:00", closes="22:30")
    assert proposal(client, room["token"], body)["closure"] == {
        "table_id": "t_3", "from": body["from"], "to": body["to"]}


def test_an_instant_may_be_written_in_any_offset(client, room):
    """`Z` and `+00:00` name the same instants as the Berlin ones, two hours off."""
    body = {"table_id": "t_2", "from": f"{THURSDAY}T16:00:00Z",
            "to": f"{THURSDAY}T21:00:00+00:00"}
    plan = proposal(client, room["token"], body)
    assert plan["closure"]["from"] == f"{THURSDAY}T16:00:00Z"
    assert plan["closure"]["to"] == f"{THURSDAY}T21:00:00+00:00"


def test_a_diner_may_not_plan_the_room(client, room, diner):
    response = propose(client, diner["token"])
    assert response.status_code == 403
    assert error_of(response)["code"] == "forbidden"


def test_planning_needs_a_token(client, room):
    response = client.post("/restaurants/r_anker/replans", json=FULL_EVENING)
    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthenticated"


def test_a_bad_token_is_refused(client, room):
    response = client.post("/restaurants/r_anker/replans", json=FULL_EVENING,
                           headers=headers_for("not-a-token", "k_1"))
    assert response.status_code == 401


def test_planning_needs_an_idempotency_key(client, room):
    response = client.post("/restaurants/r_anker/replans", json=FULL_EVENING,
                           headers=headers_for(room["token"]))
    assert response.status_code == 400
    assert error_of(response)["code"] == "missing_idempotency_key"


def test_an_unknown_restaurant_has_no_seating_to_plan(client, room):
    response = propose(client, room["token"], restaurant_id="r_nonesuch")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_an_unknown_table_is_not_found(client, room):
    response = propose(client, room["token"], closure_body("t_9"))
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_another_restaurants_table_is_not_found_here(client):
    reset(client, two_restaurants())
    token = token_for(client)
    assert proposal(client, token, closure_body("t_4"), restaurant_id="r_harbour")
    response = propose(client, token, closure_body("t_4"), restaurant_id="r_anker")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


# --------------------------------------------------------------------------- #
# the closure itself is validated
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [
    {"table_id": "t_2"},                                       # no interval at all
    {"from": instant(THURSDAY, "18:00"), "to": instant(THURSDAY, "23:00")},
    {"table_id": "t_2", "from": instant(THURSDAY, "18:00")},
    {"table_id": "t_2", "to": instant(THURSDAY, "23:00")},
    # An interval that does not end after it starts is not an interval.
    {"table_id": "t_2", "from": instant(THURSDAY, "23:00"),
     "to": instant(THURSDAY, "18:00")},
    {"table_id": "t_2", "from": instant(THURSDAY, "19:00"),
     "to": instant(THURSDAY, "19:00")},
    # A bare local time does not say which instant it means.
    {"table_id": "t_2", "from": f"{THURSDAY}T18:00", "to": f"{THURSDAY}T23:00"},
    {"table_id": "t_2", "from": f"{THURSDAY}T18:00:00",
     "to": instant(THURSDAY, "23:00")},
    {"table_id": "t_2", "from": "yesterday evening",
     "to": instant(THURSDAY, "23:00")},
    {"table_id": "", "from": instant(THURSDAY, "18:00"),
     "to": instant(THURSDAY, "23:00")},
])
def test_an_invalid_closure_is_refused(client, room, body):
    response = propose(client, room["token"], body)
    assert response.status_code == 422, response.text
    assert error_of(response)["code"] == "validation_failed"


@pytest.mark.parametrize("field", ["table_id", "from", "to"])
def test_a_closure_field_of_the_wrong_json_type_is_malformed(client, room, field):
    body = dict(FULL_EVENING)
    body[field] = 17
    response = propose(client, room["token"], body)
    assert response.status_code == 400, response.text
    assert error_of(response)["code"] == "malformed_request"


def test_unknown_fields_in_a_closure_are_ignored(client, room):
    body = dict(FULL_EVENING, reason="the table wobbles")
    assert set(proposal(client, room["token"], body)["closure"]) == {
        "table_id", "from", "to"}


def test_a_closure_may_cover_an_interval_outside_service_hours(client, room):
    """A room may take a table out for a delivery in the morning: nothing requires
    a closure to fall inside a sitting, and a plan for it simply has nobody to move."""
    body = {"table_id": "t_1", "from": f"{THURSDAY}T09:00:00{BERLIN_SUMMER}",
            "to": f"{THURSDAY}T12:00:00{BERLIN_SUMMER}"}
    assert proposal(client, room["token"], body)["assignments"] == []


def test_a_closure_may_cover_a_week(client, room):
    body = {"table_id": "t_3", "from": instant(THURSDAY, "18:00"),
            "to": f"{FRIDAY}T23:59:00{BERLIN_SUMMER}"}
    plan = proposal(client, room["token"], body)
    assert plan["closure"]["to"] == f"{FRIDAY}T23:59:00{BERLIN_SUMMER}"


# --------------------------------------------------------------------------- #
# what a plan considers
# --------------------------------------------------------------------------- #
def test_every_booking_overlapping_the_closure_is_considered(client, room, diner):
    ada = room["token"]
    first = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    second = assert_ok(seat(client, diner["token"], "t_3", at=AT_2100, party_size=6), 201)
    plan = proposal(client, ada)
    # A booking the closed table does not touch is still considered: the plan may
    # have to seat somebody else around it.
    assert [a["reference"] for a in plan["assignments"]] == sorted(
        [first["reference"], second["reference"]])


def test_a_booking_that_ends_when_the_closure_starts_is_not_considered(client, room):
    """Both intervals are half-open, so a sitting ending at 20:30 does not meet a
    closure starting at 20:30."""
    ada = room["token"]
    early = assert_ok(seat(client, ada, "t_3", at=f"{THURSDAY}T19:00", party_size=6), 201)
    plan = proposal(client, ada,
                    closure_body("t_3", THURSDAY, opens="20:30", closes="23:00"))
    assert plan["assignments"] == []
    assert assert_ok(client.get(f"/reservations/{early['reference']}",
                                headers=headers_for(ada)), 200)["table_id"] == "t_3"


def test_a_booking_that_starts_when_the_closure_ends_is_not_considered(client, room):
    ada = room["token"]
    late = assert_ok(seat(client, ada, "t_3", at=f"{THURSDAY}T21:30", party_size=6), 201)
    plan = proposal(client, ada,
                    closure_body("t_3", THURSDAY, opens="18:00", closes="21:30"))
    assert plan["assignments"] == []
    assert late["status"] == "confirmed"


def test_a_booking_on_another_day_is_not_considered(client, room):
    ada = room["token"]
    assert_ok(seat(client, ada, "t_2", at=f"{FRIDAY}T19:00", party_size=4), 201)
    assert proposal(client, ada)["assignments"] == []


def test_a_cancelled_booking_is_not_considered(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    assert_ok(client.post(f"/reservations/{made['reference']}/cancel", json={},
                          headers=headers_for(ada)), 200)
    assert proposal(client, ada)["assignments"] == []


def test_another_restaurants_booking_is_not_considered(client):
    reset(client, two_restaurants())
    token = token_for(client)
    assert_ok(seat(client, token, "t_2", at=AT_1900, restaurant_id="r_harbour"), 201)
    assert proposal(client, token, restaurant_id="r_anker")["assignments"] == []


# --------------------------------------------------------------------------- #
# a preview is a read that remembers itself
# --------------------------------------------------------------------------- #
def test_a_preview_changes_nothing_but_the_plan(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    before = export(client)["state"]
    plan = proposal(client, ada)
    after = export(client)["state"]

    assert len(after["replans"]) == len(before["replans"]) + 1
    assert len(after["replan_assignments"]) == len(plan["assignments"])
    assert after["table_closures"] == before["table_closures"] == []
    for table in ("restaurants", "reservations", "reservation_tables",
                  "reservation_history", "series", "series_occurrences"):
        assert after[table] == before[table], table
    assert after["restaurants"][0]["revision"] == plan["restaurant_revision"]
    assert made["revision"] == 1


def test_a_preview_does_not_close_the_table(client, room):
    ada = room["token"]
    proposal(client, ada)
    assert ("t_2",) in options_at(client, THURSDAY, 4, "19:00")
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)


def test_a_preview_does_not_count_as_a_change_to_the_restaurant(client, room):
    ada = room["token"]
    assert_ok(seat(client, ada, "t_1", at=AT_1900, party_size=2), 201)
    revision = revision_of(client)
    assert proposal(client, ada)["restaurant_revision"] == revision
    assert proposal(client, ada, closure_body("t_3"))["restaurant_revision"] == revision
    assert revision_of(client) == revision == 1


def test_a_preview_is_idempotent(client, room):
    ada = room["token"]
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    key = f"k_{uuid.uuid4().hex}"
    first = assert_ok(replan(client, ada, FULL_EVENING, key=key), 201)
    assert assert_ok(replan(client, ada, FULL_EVENING, key=key), 200) == first
    assert len(export(client)["state"]["replans"]) == 1


def test_a_preview_key_may_not_be_spent_on_a_different_closure(client, room):
    ada = room["token"]
    key = f"k_{uuid.uuid4().hex}"
    assert_ok(replan(client, ada, closure_body("t_2"), key=key), 201)
    response = replan(client, ada, closure_body("t_3"), key=key)
    assert response.status_code == 409
    assert error_of(response)["code"] == "idempotency_key_reuse"


def test_the_same_key_at_another_restaurant_is_a_different_request(client):
    reset(client, two_restaurants())
    token = token_for(client)
    key = f"k_{uuid.uuid4().hex}"
    assert_ok(replan(client, token, FULL_EVENING, restaurant_id="r_anker", key=key), 201)
    assert_ok(replan(client, token, FULL_EVENING, restaurant_id="r_harbour", key=key), 201)


def test_two_managers_may_propose_the_same_closure(client):
    fixture = plan_fixture(managers=["u_ada", "u_bob"])
    fixture["users"].append({"id": "u_bob", "email": "bob@example.com",
                             "password": "correct horse", "display_name": "Bob"})
    reset(client, fixture)
    ada = token_for(client)
    bob = token_for(client, "bob@example.com", "correct horse")
    first, second = proposal(client, ada), proposal(client, bob)
    assert first["plan_id"] != second["plan_id"]
    assert first["restaurant_revision"] == second["restaurant_revision"]


# --------------------------------------------------------------------------- #
# the plan is the smallest repair
# --------------------------------------------------------------------------- #
def test_a_booking_on_the_closed_table_moves_and_keeps_everything_else(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada)
    assert plan["assignments"] == [
        {"reference": made["reference"], "table_ids": ["t_3"], "changed": True}]
    assert plan["moved_count"] == 1
    assert plan["unused_seats"] == 2  # a party of four at the six-top


def test_a_booking_that_was_not_on_the_closed_table_stays_put(client, room, diner):
    ada, bob = room["token"], diner["token"]
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    other = assert_ok(seat(client, bob, "t_1", at=AT_1900, party_size=2), 201)
    plan = proposal(client, ada)
    by_reference = {a["reference"]: a for a in plan["assignments"]}
    assert by_reference[other["reference"]] == {
        "reference": other["reference"], "table_ids": ["t_1"], "changed": False}
    assert plan["moved_count"] == 1


def test_an_unchanged_pair_is_reported_as_the_booking_holds_it(client, room):
    """A diner who asked for `[t_2, t_1]` is still sitting at `[t_2, t_1]`."""
    ada = room["token"]
    made = assert_ok(seat(client, ada, ["t_2", "t_1"], at=AT_1900, party_size=6), 201)
    assert made["table_ids"] == ["t_2", "t_1"]
    plan = proposal(client, ada, closure_body("t_3"))
    assert plan["assignments"] == [
        {"reference": made["reference"], "table_ids": ["t_2", "t_1"], "changed": False}]


def test_fewer_moves_beats_a_tighter_room(client, room, diner):
    """Closing one table can be absorbed by moving one diner; a plan that moved
    both would be worse however it seated them."""
    reset(client, plan_fixture(
        tables=[{"id": "t_1", "label": "1", "capacity": 4},
                {"id": "t_2", "label": "2", "capacity": 4},
                {"id": "t_3", "label": "3", "capacity": 6}],
        combinable=[]))
    ada = token_for(client)
    bob = signup(client, email="bob@example.com", display_name="Bob")["token"]
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    assert_ok(seat(client, bob, "t_1", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada, closure_body("t_2"))
    assert plan["moved_count"] == 1
    assert [a["table_ids"] for a in plan["assignments"] if a["changed"]] == [["t_3"]]


def test_the_tightest_table_that_fits_wins(client):
    """Fewer empty seats beats a table the room lists earlier."""
    reset(client, plan_fixture(
        tables=[{"id": "t_1", "label": "1", "capacity": 8},
                {"id": "t_2", "label": "2", "capacity": 4},
                {"id": "t_3", "label": "3", "capacity": 6}],
        combinable=[]))
    ada = token_for(client)
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada, closure_body("t_2"))
    assert plan["assignments"][0]["table_ids"] == ["t_3"]
    assert plan["unused_seats"] == 2  # not the eight-top's four


def test_equal_cost_is_broken_by_the_order_the_room_lists_its_tables(client):
    reset(client, plan_fixture(
        tables=[{"id": "t_1", "label": "1", "capacity": 4},
                {"id": "t_2", "label": "2", "capacity": 4},
                {"id": "t_3", "label": "3", "capacity": 4}],
        combinable=[]))
    ada = token_for(client)
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada, closure_body("t_2"))
    assert plan["assignments"][0]["table_ids"] == ["t_1"]
    assert plan["unused_seats"] == 0


def test_a_single_table_outranks_a_pair_of_the_same_size(client):
    """Singles are ranked first in fixture order, then pairs in declared order."""
    reset(client, plan_fixture(
        tables=[{"id": "t_1", "label": "1", "capacity": 2},
                {"id": "t_2", "label": "2", "capacity": 4},
                {"id": "t_3", "label": "3", "capacity": 2},
                {"id": "t_4", "label": "4", "capacity": 4}],
        combinable=[["t_1", "t_3"]]))
    ada = token_for(client)
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada, closure_body("t_2"))
    assert plan["assignments"][0]["table_ids"] == ["t_4"]
    assert plan["unused_seats"] == 0


def test_a_pair_is_used_when_no_single_table_fits(client):
    reset(client, plan_fixture(
        tables=[{"id": "t_1", "label": "1", "capacity": 4},
                {"id": "t_2", "label": "2", "capacity": 4},
                {"id": "t_3", "label": "3", "capacity": 2}],
        combinable=[["t_2", "t_3"], ["t_1", "t_3"]]))
    ada = token_for(client)
    made = assert_ok(seat(client, ada, ["t_1", "t_3"], at=AT_1900, party_size=6), 201)
    plan = proposal(client, ada, closure_body("t_1"))
    assert plan["assignments"][0]["table_ids"] == ["t_2", "t_3"]
    assert plan["moved_count"] == 1 and plan["unused_seats"] == 0
    assert made["party_size"] == 6


def test_a_moved_pair_is_named_in_the_order_the_room_declared_it(client):
    reset(client, plan_fixture(
        tables=[{"id": "t_1", "label": "1", "capacity": 4},
                {"id": "t_2", "label": "2", "capacity": 2},
                {"id": "t_3", "label": "3", "capacity": 4},
                {"id": "t_4", "label": "4", "capacity": 2}],
        combinable=[["t_4", "t_3"], ["t_1", "t_2"]]))
    ada = token_for(client)
    assert_ok(seat(client, ada, ["t_1", "t_2"], at=AT_1900, party_size=6), 201)
    plan = proposal(client, ada, closure_body("t_1"))
    assert plan["assignments"][0]["table_ids"] == ["t_4", "t_3"]


def test_a_pair_holding_the_closed_table_is_not_an_option(client, room, diner):
    ada, bob = room["token"], diner["token"]
    made = assert_ok(seat(client, ada, ["t_1", "t_2"], at=AT_1900, party_size=6), 201)
    assert_ok(seat(client, bob, "t_3", at=AT_1900, party_size=6), 201)
    response = propose(client, ada)
    # Both declared pairs hold t_2, and the only single table big enough is taken:
    # there is nowhere to put a party of six.
    assert response.status_code == 409
    assert error_of(response)["code"] == "no_feasible_plan"
    assert made["status"] == "confirmed"


def test_a_booking_is_offered_the_capacities_it_accepted(client, room):
    """A policy published afterwards does not re-decide a booking already made."""
    ada = room["token"]
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    published = assert_ok(publish(client, ada, three_table_policy_body(
        THURSDAY, capacities={"t_1": 2, "t_2": 8, "t_3": 4})), 201)
    assert published["policy_version"] == 1
    plan = proposal(client, ada)
    assert plan["assignments"][0]["table_ids"] == ["t_3"]
    assert plan["unused_seats"] == 2  # six seats under its own terms, not four


def test_a_booking_made_under_a_policy_is_offered_that_policys_tables(client, room):
    ada = room["token"]
    assert_ok(publish(client, ada, three_table_policy_body(
        THURSDAY, capacities={"t_1": 6, "t_2": 6, "t_3": 6})), 201)
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=6), 201)
    assert made["accepted_terms"]["capacities"] == {"t_1": 6, "t_2": 6, "t_3": 6}
    plan = proposal(client, ada)
    # t_1 has two seats in the fixture and six under the policy it accepted.
    assert plan["assignments"][0]["table_ids"] == ["t_1"]
    assert plan["unused_seats"] == 0


def test_a_plan_for_an_empty_table_moves_nobody(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada, closure_body("t_3"))
    assert plan["assignments"] == [
        {"reference": made["reference"], "table_ids": ["t_2"], "changed": False}]
    assert plan["moved_count"] == 0
    assert plan["unused_seats"] == 0


def test_a_plan_is_refused_when_a_booking_would_have_to_disappear(client):
    """No plan may cancel, shrink or drop a booking, so an impossible repair is
    refused rather than answered with a smaller room."""
    reset(client, plan_fixture(
        tables=[{"id": "t_1", "label": "1", "capacity": 2},
                {"id": "t_2", "label": "2", "capacity": 4}],
        combinable=[]))
    ada = token_for(client)
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    response = propose(client, ada, closure_body("t_2"))
    assert response.status_code == 409
    assert error_of(response)["code"] == "no_feasible_plan"
    state = export(client)["state"]
    assert state["replans"] == [] and state["table_closures"] == []
    assert state["restaurants"][0]["revision"] == 1
    assert made["status"] == "confirmed"


def test_a_refused_plan_leaves_its_key_unspent(client):
    reset(client, plan_fixture(
        tables=[{"id": "t_1", "label": "1", "capacity": 2},
                {"id": "t_2", "label": "2", "capacity": 4}],
        combinable=[]))
    ada = token_for(client)
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    key = f"k_{uuid.uuid4().hex}"
    assert propose(client, ada, closure_body("t_2"), key=key).status_code == 409
    # A refusal is not a receipt, so the same key proposes the plan that works.
    assert proposal(client, ada, closure_body("t_1"), key=key)["moved_count"] == 0


# --------------------------------------------------------------------------- #
# the sizes a plan supports
# --------------------------------------------------------------------------- #
def test_seven_tables_are_more_than_a_plan_supports(client):
    tables = [{"id": f"t_{index}", "label": str(index), "capacity": 2 * index}
              for index in range(1, 8)]
    reset(client, plan_fixture(tables=tables, combinable=[]))
    ada = token_for(client)
    response = propose(client, ada, closure_body("t_2"))
    assert response.status_code == 422
    assert error_of(response)["code"] == "planning_limit"


def test_six_tables_are_exactly_enough(client):
    tables = [{"id": f"t_{index}", "label": str(index), "capacity": 2 * index}
              for index in range(1, 7)]
    reset(client, plan_fixture(tables=tables, combinable=[]))
    ada = token_for(client)
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    assert proposal(client, ada, closure_body("t_2"))["assignments"][0]["table_ids"] \
        == ["t_3"]


def test_five_declared_pairs_are_more_than_a_plan_supports(client):
    reset(client, plan_fixture(
        tables=[{"id": f"t_{index}", "label": str(index), "capacity": 4}
                for index in range(1, 5)],
        combinable=[["t_1", "t_2"], ["t_2", "t_3"], ["t_3", "t_4"],
                    ["t_1", "t_3"], ["t_1", "t_4"]]))
    ada = token_for(client)
    response = propose(client, ada, closure_body("t_2"))
    assert response.status_code == 422
    assert error_of(response)["code"] == "planning_limit"


def test_four_declared_pairs_are_exactly_enough(client):
    reset(client, plan_fixture(
        tables=[{"id": f"t_{index}", "label": str(index), "capacity": 4}
                for index in range(1, 5)],
        combinable=[["t_1", "t_2"], ["t_2", "t_3"], ["t_3", "t_4"], ["t_1", "t_3"]]))
    ada = token_for(client)
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    assert proposal(client, ada, closure_body("t_2"))["moved_count"] == 1


def test_seven_bookings_in_question_are_more_than_a_plan_considers(client, room):
    ada = room["token"]
    for table_id, party_size in (("t_1", 2), ("t_2", 4), ("t_3", 6)):
        for hhmm in ("18:00", "19:30", "21:00"):
            if table_id == "t_3" and hhmm != "18:00":
                continue
            assert_ok(seat(client, ada, table_id, at=f"{THURSDAY}T{hhmm}",
                           party_size=party_size), 201)
    response = propose(client, ada, closure_body("t_1"))
    assert response.status_code == 422
    assert error_of(response)["code"] == "planning_limit"


def test_six_bookings_in_question_are_exactly_enough(client, room):
    ada = room["token"]
    for table_id, party_size in (("t_1", 2), ("t_2", 4)):
        for hhmm in ("18:00", "19:30", "21:00"):
            assert_ok(seat(client, ada, table_id, at=f"{THURSDAY}T{hhmm}",
                           party_size=party_size), 201)
    plan = proposal(client, ada, closure_body("t_1"))
    assert len(plan["assignments"]) == 6
    assert plan["moved_count"] == 3


def test_a_room_at_its_limits_is_planned_inside_one_request(client, room):
    """Six tables, four pairs and six bookings: the largest plan required, and the
    search that finds it has to answer while a manager waits."""
    reset(client, plan_fixture(
        tables=[{"id": f"t_{index}", "label": str(index),
                 "capacity": 2 + 2 * (index % 3)} for index in range(1, 7)],
        combinable=[["t_1", "t_2"], ["t_2", "t_3"], ["t_3", "t_4"], ["t_4", "t_5"]]))
    ada = token_for(client)
    for hhmm in ("18:00", "19:30", "21:00"):
        assert_ok(seat(client, ada, "t_2", at=f"{THURSDAY}T{hhmm}", party_size=4), 201)
        assert_ok(seat(client, ada, "t_5", at=f"{THURSDAY}T{hhmm}", party_size=4), 201)
    started = time.monotonic()
    plan = proposal(client, ada, closure_body("t_2"))
    assert time.monotonic() - started < 5.0
    assert len(plan["assignments"]) == 6
    assert plan["moved_count"] == 3


# --------------------------------------------------------------------------- #
# applying a plan
# --------------------------------------------------------------------------- #
def test_applying_a_plan_seats_the_room_as_proposed(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada)
    applied = assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)

    assert set(applied) == {"plan_id", "restaurant_revision", "reservations"}
    assert applied["plan_id"] == plan["plan_id"]
    assert applied["restaurant_revision"] == plan["restaurant_revision"] + 1

    moved = applied["reservations"][0]
    assert moved["reference"] == made["reference"]
    assert moved["reservation_id"] == made["reservation_id"]
    assert moved["table_ids"] == ["t_3"] and moved["table_id"] == "t_3"
    assert moved["revision"] == made["revision"] + 1
    # A repair moves the tables and nothing else.
    assert moved["accepted_terms"] == made["accepted_terms"]
    assert moved["starts_at_local"] == made["starts_at_local"]
    assert moved["starts_at"] == made["starts_at"]
    assert moved["ends_at"] == made["ends_at"]
    assert moved["party_size"] == made["party_size"]
    assert moved["status"] == "confirmed" == made["status"]
    assert moved["created_at"] == made["created_at"]


def test_the_applied_booking_reads_as_moved(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada)
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    read = assert_ok(client.get(f"/reservations/{made['reference']}",
                                headers=headers_for(ada)), 200)
    assert read["table_ids"] == ["t_3"]
    assert read["revision"] == 2
    listed = assert_ok(client.get("/reservations", headers=headers_for(ada)), 200)
    assert listed["reservations"][0]["table_ids"] == ["t_3"]


def test_a_moved_booking_gains_one_reassigned_entry(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada)
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)

    ledger = assert_ok(client.get(f"/reservations/{made['reference']}/history",
                                  headers=headers_for(ada)), 200)
    entries = ledger["entries"]
    assert [entry["event"] for entry in entries] == ["created", "reassigned"]
    entry = entries[1]
    assert entry["seq"] == 2
    assert entry["changes"] == [{"field": "table_ids", "from": ["t_2"], "to": ["t_3"]}]
    assert entry["plan_id"] == plan["plan_id"]
    assert entry["revision"] == 2
    assert entry["accepted_terms"] == made["accepted_terms"]
    # An entry no plan wrote keeps the shape it has always had.
    assert "plan_id" not in entries[0]

    decision = assert_ok(client.get(f"/reservations/{made['reference']}/decision",
                                    headers=headers_for(ada)), 200)
    assert decision == {"reference": made["reference"], "revision": 2,
                        "accepted_terms": made["accepted_terms"]}


def test_a_moved_pair_is_recorded_in_declared_order(client):
    reset(client, plan_fixture(
        tables=[{"id": "t_1", "label": "1", "capacity": 4},
                {"id": "t_2", "label": "2", "capacity": 2},
                {"id": "t_3", "label": "3", "capacity": 4},
                {"id": "t_4", "label": "4", "capacity": 2}],
        combinable=[["t_4", "t_3"], ["t_1", "t_2"]]))
    ada = token_for(client)
    made = assert_ok(seat(client, ada, ["t_1", "t_2"], at=AT_1900, party_size=6), 201)
    plan = proposal(client, ada, closure_body("t_1"))
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    entry = assert_ok(client.get(f"/reservations/{made['reference']}/history",
                                 headers=headers_for(ada)), 200)["entries"][-1]
    assert entry["changes"] == [
        {"field": "table_ids", "from": ["t_1", "t_2"], "to": ["t_4", "t_3"]}]
    assert entry["event"] == "reassigned"


def test_an_unmoved_booking_gains_nothing_at_all(client, room, diner):
    ada, bob = room["token"], diner["token"]
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    other = assert_ok(seat(client, bob, "t_1", at=AT_1900, party_size=2), 201)
    plan = proposal(client, ada)
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)

    read = assert_ok(client.get(f"/reservations/{other['reference']}",
                                headers=headers_for(bob)), 200)
    assert read["revision"] == 1
    assert read["table_ids"] == ["t_1"]
    assert read["accepted_terms"] == other["accepted_terms"]
    ledger = assert_ok(client.get(f"/reservations/{other['reference']}/history",
                                  headers=headers_for(bob)), 200)
    assert [entry["event"] for entry in ledger["entries"]] == ["created"]


def test_a_plan_counts_once_for_the_whole_room(client):
    """Three diners move, and the restaurant's revision moves once."""
    reset(client, four_tables())
    ada = token_for(client)
    for hhmm in ("18:00", "19:30", "21:00"):
        assert_ok(seat(client, ada, "t_2", at=f"{THURSDAY}T{hhmm}", party_size=4), 201)
    before = revision_of(client)
    plan = proposal(client, ada, closure_body("t_2"))
    assert plan["moved_count"] == 3
    applied = assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    assert applied["restaurant_revision"] == before + 1
    assert revision_of(client) == before + 1
    assert len(applied["reservations"]) == 3
    assert all(r["table_ids"] == ["t_3"] for r in applied["reservations"])
    assert all(r["revision"] == 2 for r in applied["reservations"])


def test_the_reservations_are_listed_in_reference_order(client, room, diner):
    ada, bob = room["token"], diner["token"]
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    assert_ok(seat(client, bob, "t_3", at=AT_2100, party_size=6), 201)
    assert_ok(seat(client, ada, "t_1", at=f"{THURSDAY}T18:00", party_size=2), 201)
    plan = proposal(client, ada)
    applied = assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    references = [r["reference"] for r in applied["reservations"]]
    assert references == sorted(references)
    assert references == [a["reference"] for a in plan["assignments"]]


def test_applying_an_empty_plan_still_closes_the_table(client, room):
    ada = room["token"]
    plan = proposal(client, ada, closure_body("t_3"))
    assert plan["assignments"] == []
    applied = assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    assert applied["reservations"] == []
    assert applied["restaurant_revision"] == plan["restaurant_revision"] + 1
    assert ("t_3",) not in options_at(client, THURSDAY, 6, "19:00")


def test_a_diner_may_not_apply_a_plan(client, room, diner):
    ada = room["token"]
    plan = proposal(client, ada)
    response = apply_plan(client, diner["token"], plan["plan_id"])
    assert response.status_code == 403
    assert error_of(response)["code"] == "forbidden"


def test_applying_needs_a_token_and_a_key(client, room):
    ada = room["token"]
    plan = proposal(client, ada)
    url = f"/restaurants/r_anker/replans/{plan['plan_id']}/apply"
    response = client.post(url, json={})
    assert response.status_code == 401
    response = client.post(url, json={}, headers=headers_for(ada))
    assert response.status_code == 400
    assert error_of(response)["code"] == "missing_idempotency_key"


def test_an_unknown_plan_is_not_found(client, room):
    response = apply_plan(client, room["token"], "pln_nonesuch")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_another_restaurants_plan_is_not_found_here(client):
    reset(client, two_restaurants())
    ada = token_for(client)
    plan = proposal(client, ada, restaurant_id="r_harbour")
    response = apply_plan(client, ada, plan["plan_id"], restaurant_id="r_anker")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"
    # The plan is untouched and still applies where it was proposed.
    assert_ok(apply_plan(client, ada, plan["plan_id"], restaurant_id="r_harbour"), 201)


def test_an_unknown_restaurant_cannot_apply_a_plan(client, room):
    response = apply_plan(client, room["token"], "pln_nonesuch",
                          restaurant_id="r_nonesuch")
    assert response.status_code == 404


@pytest.mark.parametrize("body", [None, {}, {"note": "the leg is broken"}])
def test_the_apply_body_carries_nothing_the_plan_does_not_know(client, room, body):
    ada = room["token"]
    plan = proposal(client, ada)
    url = f"/restaurants/r_anker/replans/{plan['plan_id']}/apply"
    if body is None:
        response = client.post(url, headers=headers_for(ada, f"k_{uuid.uuid4().hex}"))
    else:
        response = apply_plan(client, ada, plan["plan_id"], body=body)
    assert response.status_code == 201, response.text


def test_an_apply_body_that_is_not_an_object_is_malformed(client, room):
    ada = room["token"]
    plan = proposal(client, ada)
    response = client.post(f"/restaurants/r_anker/replans/{plan['plan_id']}/apply",
                           json=[], headers=headers_for(ada, f"k_{uuid.uuid4().hex}"))
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"


def test_applying_is_idempotent_even_after_the_room_moves_on(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada)
    key = f"k_{uuid.uuid4().hex}"
    first = assert_ok(apply_plan(client, ada, plan["plan_id"], key=key), 201)
    # The room changes afterwards; the receipt is still the original answer.
    assert_ok(seat(client, ada, "t_1", at=f"{FRIDAY}T19:00", party_size=2), 201)
    replay = assert_ok(apply_plan(client, ada, plan["plan_id"], key=key), 200)
    assert replay == first
    assert assert_ok(client.get(f"/reservations/{made['reference']}",
                                headers=headers_for(ada)), 200)["revision"] == 2


def test_a_second_key_cannot_apply_the_same_plan_twice(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada)
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    response = apply_plan(client, ada, plan["plan_id"])
    assert response.status_code == 409
    assert error_of(response)["code"] == "plan_already_applied"
    # Nothing happened twice.
    assert assert_ok(client.get(f"/reservations/{made['reference']}",
                                headers=headers_for(ada)), 200)["revision"] == 2
    assert len(export(client)["state"]["table_closures"]) == 1


def test_a_key_spent_on_one_plan_is_not_spent_on_another(client, room):
    """A key is scoped to the request it was sent with, and the path names the plan:
    the second application is judged on its own, and by then the room has moved."""
    ada = room["token"]
    first = proposal(client, ada, closure_body("t_2"))
    second = proposal(client, ada, closure_body("t_3"))
    key = f"k_{uuid.uuid4().hex}"
    assert_ok(apply_plan(client, ada, first["plan_id"], key=key), 201)
    response = apply_plan(client, ada, second["plan_id"], key=key)
    assert response.status_code == 409
    assert error_of(response)["code"] == "stale_plan"


def test_an_apply_key_may_not_be_reused_with_a_different_body(client, room):
    ada = room["token"]
    plan = proposal(client, ada, closure_body("t_2"))
    key = f"k_{uuid.uuid4().hex}"
    assert_ok(apply_plan(client, ada, plan["plan_id"], key=key, body={}), 201)
    response = apply_plan(client, ada, plan["plan_id"], key=key,
                          body={"note": "the leg is broken"})
    assert response.status_code == 409
    assert error_of(response)["code"] == "idempotency_key_reuse"


def test_concurrent_applications_move_each_booking_once(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    other = assert_ok(seat(client, ada, "t_1", at=AT_2100, party_size=2), 201)
    plan = proposal(client, ada)
    outcomes: list = []
    barrier = threading.Barrier(20)

    def attempt(index: int) -> None:
        barrier.wait(timeout=30)
        outcomes.append(apply_plan(client, ada, plan["plan_id"], key=f"race-{index}"))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    statuses = sorted(response.status_code for response in outcomes)
    assert all(status < 500 for status in statuses), statuses
    assert statuses.count(201) == 1, statuses
    assert set(statuses) - {201} <= {409}, statuses
    # No booking was moved twice, and none was left half-moved.
    read = assert_ok(client.get(f"/reservations/{made['reference']}",
                                headers=headers_for(ada)), 200)
    assert read["table_ids"] == ["t_3"] and read["revision"] == 2
    ledger = assert_ok(client.get(f"/reservations/{made['reference']}/history",
                                  headers=headers_for(ada)), 200)
    assert [entry["event"] for entry in ledger["entries"]] == ["created", "reassigned"]
    untouched = assert_ok(client.get(f"/reservations/{other['reference']}",
                                     headers=headers_for(ada)), 200)
    assert untouched["revision"] == 1
    assert len(export(client)["state"]["table_closures"]) == 1


# --------------------------------------------------------------------------- #
# what an applied closure means for the room
# --------------------------------------------------------------------------- #
def test_a_closed_table_is_not_offered_while_it_is_closed(client, room):
    ada = room["token"]
    plan = proposal(client, ada, closure_body("t_2", THURSDAY, opens="19:00", closes="21:00"))
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)

    inside = availability_of(client, THURSDAY, 4, explain=True)
    slot = slot_at(inside, "19:30")
    assert "t_2" not in slot["available_table_ids"]
    assert ("t_2",) not in [tuple(o["table_ids"]) for o in slot["available_options"]]
    # A pair is unavailable when either of its tables is closed.
    assert ["t_1", "t_2"] not in [o["table_ids"] for o in slot["available_options"]]
    assert ["t_2", "t_3"] not in [o["table_ids"] for o in slot["available_options"]]
    explained = {entry["table_id"]: entry for entry in slot["explain"]}
    assert explained["t_2"]["available"] is False
    assert {"rule": "no_overlap", "holds": False} in explained["t_2"]["rules"]
    assert explained["t_3"]["available"] is True


def test_a_closed_table_comes_back_when_the_closure_ends(client, room):
    ada = room["token"]
    plan = proposal(client, ada, closure_body("t_2", THURSDAY, opens="19:30", closes="21:00"))
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)

    day = availability_of(client, THURSDAY, 4, explain=True)
    # A sitting that ends exactly when the closure starts only touches it.
    assert "t_2" in slot_at(day, "18:00")["available_table_ids"]
    # One that starts exactly when the closure ends only touches it too.
    after = slot_at(day, "21:00")
    assert "t_2" in after["available_table_ids"]
    explained = {entry["table_id"]: entry for entry in after["explain"]}
    assert {"rule": "no_overlap", "holds": True} in explained["t_2"]["rules"]
    # A sitting that runs into the closure does not get the table.
    assert "t_2" not in slot_at(day, "19:00")["available_table_ids"]


def test_another_day_is_unaffected_by_a_closure(client, room):
    ada = room["token"]
    plan = proposal(client, ada, closure_body("t_2"))
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    assert "t_2" in availability_of(client, FRIDAY, 4)["slots"][0]["available_table_ids"]


def test_a_closed_table_cannot_be_booked_into(client, room):
    ada = room["token"]
    plan = proposal(client, ada, closure_body("t_2", THURSDAY, opens="19:00", closes="21:00"))
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)

    for tables, party_size in (("t_2", 4), (["t_1", "t_2"], 4), (["t_2", "t_3"], 8)):
        response = seat(client, ada, tables, at=f"{THURSDAY}T19:30",
                        party_size=party_size)
        assert response.status_code == 409, tables
        assert error_of(response)["code"] == "table_unavailable"


def test_a_sitting_only_partly_inside_a_closure_is_refused(client, room):
    ada = room["token"]
    plan = proposal(client, ada, closure_body("t_2", THURSDAY, opens="20:00", closes="21:00"))
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    # 19:00 to 20:30 overlaps the closure's first half hour.
    response = seat(client, ada, "t_2", at=f"{THURSDAY}T19:00", party_size=4)
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"


def test_a_sitting_that_touches_a_closure_at_its_ends_is_accepted(client, room):
    ada = room["token"]
    plan = proposal(client, ada, closure_body("t_2", THURSDAY, opens="19:30", closes="20:00"))
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    # Both intervals are half-open: the sitting from 18:00 ends at 19:30, exactly
    # when the closure starts, and the one from 20:00 starts exactly when it ends.
    assert_ok(seat(client, ada, "t_2", at=f"{THURSDAY}T18:00", party_size=4), 201)
    assert_ok(seat(client, ada, "t_2", at=f"{THURSDAY}T20:00", party_size=4), 201)
    # A sitting that runs through the closure does not get the table.
    response = seat(client, ada, "t_2", at=f"{THURSDAY}T18:30", party_size=4)
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"


def test_a_booking_cannot_be_amended_into_a_closed_table(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_3", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada, closure_body("t_2"))
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    response = client.patch(f"/reservations/{made['reference']}",
                            json={"table_id": "t_2", "expected_revision": 1},
                            headers=headers_for(ada))
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"
    assert assert_ok(client.get(f"/reservations/{made['reference']}",
                                headers=headers_for(ada)), 200)["table_ids"] == ["t_3"]


def test_a_batch_of_moves_may_not_land_on_a_closed_table(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_3", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada, closure_body("t_2"))
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    response = client.post("/reservation-moves", json={"moves": [
        {"reference": made["reference"], "table_id": "t_2", "expected_revision": 1}]},
        headers=headers_for(ada, f"k_{uuid.uuid4().hex}"))
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"


def test_a_series_may_not_be_adopted_into_a_closed_table(client, room):
    ada = room["token"]
    week_later = "2026-10-01"  # the Thursday after the anchor's
    plan = proposal(client, ada,
                    closure_body("t_2", week_later, opens="19:00", closes="21:00"))
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    anchor = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    # Occurrence one lands a week later, inside that closure.
    response = adopt(client, ada, anchor["reference"], count=2)
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"
    # Nothing survived the refusal: the anchor is still alone.
    assert len(export(client)["state"]["series"]) == 0
    assert anchor["reference"] in [r["reference"] for r in
                                  export(client)["state"]["reservations"]]


def test_the_fixture_still_lists_a_closed_table(client, room):
    """A closure is not a change to the restaurant: the table is still there, and
    so is its capacity, its label and its place in the room's declared pairs."""
    ada = room["token"]
    plan = proposal(client, ada, closure_body("t_2"))
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    detail = assert_ok(client.get("/restaurants/r_anker"), 200)
    assert [t["id"] for t in detail["tables"]] == ["t_1", "t_2", "t_3"]
    assert detail["combinable"] == [["t_1", "t_2"], ["t_2", "t_3"]]


def test_a_second_closure_is_planned_around_the_first(client):
    reset(client, four_tables())
    ada = token_for(client)
    made = assert_ok(seat(client, ada, "t_4", at=AT_1900, party_size=4), 201)
    first = proposal(client, ada, closure_body("t_2"))
    assert first["assignments"][0]["changed"] is False
    assert_ok(apply_plan(client, ada, first["plan_id"]), 201)

    second = proposal(client, ada, closure_body("t_4"))
    # t_2 is already out of service, so the only four-top left is t_3.
    assert second["assignments"] == [
        {"reference": made["reference"], "table_ids": ["t_3"], "changed": True}]
    assert_ok(apply_plan(client, ada, second["plan_id"]), 201)
    assert len(export(client)["state"]["table_closures"]) == 2
    # Both closures stand at once: at an hour nothing is booked, the only table
    # left for a party of four is t_3 — t_1 seats two, t_2 and t_4 are closed.
    assert options_at(client, THURSDAY, 4, "21:00") == [("t_3",)]


def test_a_closure_at_another_restaurant_changes_nothing_here(client):
    reset(client, two_restaurants())
    ada = token_for(client)
    here = proposal(client, ada, closure_body("t_1"), restaurant_id="r_anker")
    elsewhere = proposal(client, ada, closure_body("t_2"), restaurant_id="r_harbour")
    assert_ok(apply_plan(client, ada, elsewhere["plan_id"],
                         restaurant_id="r_harbour"), 201)
    # This room's plan is not stale, because this room did not change.
    assert_ok(apply_plan(client, ada, here["plan_id"], restaurant_id="r_anker"), 201)
    assert ("t_1",) not in options_at(client, THURSDAY, 2, "19:00")
    assert ("t_2",) in options_at(client, THURSDAY, 4, "19:00")
    other_day = availability_of(client, THURSDAY, 4, restaurant_id="r_harbour")
    assert "t_2" not in slot_at(other_day, "19:00")["available_table_ids"]


# --------------------------------------------------------------------------- #
# a plan goes stale when its restaurant changes
# --------------------------------------------------------------------------- #
def test_a_new_booking_invalidates_a_plan(client, room):
    ada = room["token"]
    plan = proposal(client, ada)
    assert_ok(seat(client, ada, "t_1", at=f"{FRIDAY}T19:00", party_size=2), 201)
    response = apply_plan(client, ada, plan["plan_id"])
    assert response.status_code == 409
    assert error_of(response)["code"] == "stale_plan"
    assert export(client)["state"]["table_closures"] == []


def test_an_amendment_invalidates_a_plan(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_3", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada)
    assert_ok(client.patch(f"/reservations/{made['reference']}",
                           json={"party_size": 6, "expected_revision": 1},
                           headers=headers_for(ada)), 200)
    assert apply_plan(client, ada, plan["plan_id"]).status_code == 409


def test_a_cancellation_invalidates_a_plan(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_3", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada)
    assert_ok(client.post(f"/reservations/{made['reference']}/cancel", json={},
                          headers=headers_for(ada)), 200)
    response = apply_plan(client, ada, plan["plan_id"])
    assert response.status_code == 409
    assert error_of(response)["code"] == "stale_plan"


def test_a_published_policy_invalidates_a_plan(client, room):
    ada = room["token"]
    plan = proposal(client, ada)
    assert_ok(publish(client, ada, three_table_policy_body(THURSDAY)), 201)
    assert apply_plan(client, ada, plan["plan_id"]).status_code == 409


def test_another_applied_plan_invalidates_this_one(client, room):
    ada = room["token"]
    first = proposal(client, ada, closure_body("t_1"))
    second = proposal(client, ada, closure_body("t_3"))
    assert_ok(apply_plan(client, ada, first["plan_id"]), 201)
    response = apply_plan(client, ada, second["plan_id"])
    assert response.status_code == 409
    assert error_of(response)["code"] == "stale_plan"


def test_an_adopted_agreement_invalidates_a_plan(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_3", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada)
    assert_ok(adopt(client, ada, made["reference"], count=2), 201)
    assert apply_plan(client, ada, plan["plan_id"]).status_code == 409


def test_another_preview_does_not_invalidate_a_plan(client, room):
    ada = room["token"]
    first = proposal(client, ada, closure_body("t_1"))
    proposal(client, ada, closure_body("t_3"))
    assert_ok(apply_plan(client, ada, first["plan_id"]), 201)


def test_a_signup_elsewhere_does_not_invalidate_a_plan(client, room):
    ada = room["token"]
    plan = proposal(client, ada)
    signup(client, email="new@example.com", display_name="New")
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)


def test_a_booking_at_another_restaurant_does_not_invalidate_a_plan(client):
    reset(client, two_restaurants())
    ada = token_for(client)
    plan = proposal(client, ada, restaurant_id="r_anker")
    assert_ok(seat(client, ada, "t_2", at=AT_1900, restaurant_id="r_harbour"), 201)
    assert_ok(apply_plan(client, ada, plan["plan_id"], restaurant_id="r_anker"), 201)


def test_a_stale_application_changes_nothing_at_all(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada)
    assert_ok(seat(client, ada, "t_1", at=f"{FRIDAY}T19:00", party_size=2), 201)
    before = export(client)["state"]

    assert apply_plan(client, ada, plan["plan_id"]).status_code == 409

    # Not one row moved: no closure, no reassignment, no ledger entry, no counter —
    # and a refusal being no receipt, not even an idempotency record.
    assert export(client)["state"] == before
    assert before["table_closures"] == []
    moved = assert_ok(client.get(f"/reservations/{made['reference']}",
                                 headers=headers_for(ada)), 200)
    assert moved["table_ids"] == ["t_2"] and moved["revision"] == 1


# --------------------------------------------------------------------------- #
# a repair and a recurring agreement
# --------------------------------------------------------------------------- #
WEEK_LATER = "2026-10-01"
FORTNIGHT_LATER = "2026-10-08"
THREE_THURSDAYS = {"table_id": "t_2",
                   "from": instant(THURSDAY, "18:00"),
                   "to": instant("2026-10-09", "00:00")}


def agreed(client, token, *, at=AT_1900, tables="t_2", party_size=4, count=3):
    """A booking adopted as the anchor of a weekly agreement."""
    anchor = assert_ok(seat(client, token, tables, at=at, party_size=party_size), 201)
    made = assert_ok(adopt(client, token, anchor["reference"], count=count), 201)
    return anchor, made


def test_a_repair_moves_an_occurrence_and_keeps_it_in_its_agreement(client, room):
    ada = room["token"]
    anchor, made = agreed(client, ada)
    plan = proposal(client, ada, closure_body("t_2", THURSDAY))
    assert [a["reference"] for a in plan["assignments"]] == [anchor["reference"]]

    applied = assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    after = series_of(client, ada, made["series_id"])
    assert after["revision"] == made["revision"] + 1
    assert applied["restaurant_revision"] == plan["restaurant_revision"] + 1

    first = after["occurrences"][0]
    assert first["index"] == 0 and first["reference"] == anchor["reference"]
    assert first["exception"] is False
    assert first["reservation"]["table_ids"] == ["t_3"]
    assert first["reservation"]["starts_at_local"] == AT_1900
    assert first["reservation"]["accepted_terms"] == anchor["accepted_terms"]
    assert first["reservation"]["revision"] == anchor["revision"] + 1

    # The later occurrences were never inside the closure, so they never moved.
    second = after["occurrences"][1]
    assert second["reservation"]["table_ids"] == ["t_2"]
    assert second["reservation"]["starts_at_local"] == f"{WEEK_LATER}T19:00"
    assert second["reservation"]["revision"] == 1
    assert [entry["event"] for entry in assert_ok(
        client.get(f"/reservations/{second['reference']}/history",
                   headers=headers_for(ada)), 200)["entries"]] == ["created"]


def test_an_agreement_counts_a_repair_once_however_many_members_moved(client, room):
    ada = room["token"]
    anchor, made = agreed(client, ada)
    plan = proposal(client, ada, THREE_THURSDAYS)
    assert plan["moved_count"] == 3
    # A plan reports its bookings in reference order, which is not the order the
    # agreement counts them in.
    assert sorted(a["reference"] for a in plan["assignments"]) == sorted(
        occurrence["reference"] for occurrence in made["occurrences"])

    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    after = series_of(client, ada, made["series_id"])
    assert after["revision"] == made["revision"] + 1
    assert all(occurrence["reservation"]["table_ids"] == ["t_3"]
               for occurrence in after["occurrences"])
    assert [occurrence["reservation"]["starts_at_local"]
            for occurrence in after["occurrences"]] == [
        AT_1900, f"{WEEK_LATER}T19:00", f"{FORTNIGHT_LATER}T19:00"]


def test_a_repair_that_moves_no_member_leaves_the_agreement_alone(client, room):
    ada = room["token"]
    anchor, made = agreed(client, ada)
    plan = proposal(client, ada, closure_body("t_3"))
    assert plan["moved_count"] == 0
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    assert series_of(client, ada, made["series_id"])["revision"] == made["revision"]
    assert series_of(client, ada, made["series_id"])["occurrences"][0][
        "reservation"]["revision"] == anchor["revision"]


def test_a_repair_keeps_an_occurrence_that_was_taken_out_of_the_pattern(client, room):
    """A diner who moved one sitting by hand made it an exception; a repair moves
    the tables under it and leaves that alone."""
    ada = room["token"]
    anchor, made = agreed(client, ada)
    second = made["occurrences"][1]["reference"]
    assert_ok(client.patch(f"/reservations/{second}",
                           json={"starts_at_local": f"{WEEK_LATER}T21:00",
                                 "expected_revision": 1},
                           headers=headers_for(ada)), 200)
    plan = proposal(client, ada, THREE_THURSDAYS)
    assert plan["moved_count"] == 3
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)

    after = series_of(client, ada, made["series_id"])
    moved = next(o for o in after["occurrences"] if o["reference"] == second)
    assert moved["exception"] is True
    assert moved["reservation"]["starts_at_local"] == f"{WEEK_LATER}T21:00"
    assert moved["reservation"]["table_ids"] == ["t_3"]


def test_a_cancelled_occurrence_is_not_considered_by_a_repair(client, room):
    ada = room["token"]
    anchor, made = agreed(client, ada)
    cancelled = made["occurrences"][2]["reference"]
    assert_ok(client.post(f"/reservations/{cancelled}/cancel", json={},
                          headers=headers_for(ada)), 200)
    plan = proposal(client, ada, THREE_THURSDAYS)
    assert plan["moved_count"] == 2
    assert cancelled not in [a["reference"] for a in plan["assignments"]]
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    after = series_of(client, ada, made["series_id"])
    assert next(o for o in after["occurrences"]
                if o["reference"] == cancelled)["reservation"]["status"] == "cancelled"


def test_a_repair_moves_an_occurrence_the_diner_rescheduled(client, room):
    """An agreement whose clock time was changed is repaired at the time it now has."""
    ada = room["token"]
    anchor, made = agreed(client, ada)
    amended = assert_ok(client.post(
        f"/series/{made['series_id']}/amend",
        json={"expected_revision": made["revision"], "from_index": 1,
              "local_time": "21:00"},
        headers=headers_for(ada, f"k_{uuid.uuid4().hex}")), 201)
    plan = proposal(client, ada, closure_body("t_2", WEEK_LATER, opens="21:00",
                                              closes="22:00"))
    moved = [a for a in plan["assignments"] if a["changed"]]
    assert [a["reference"] for a in moved] == [
        amended["occurrences"][1]["reference"]]
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    after = series_of(client, ada, made["series_id"])
    assert after["occurrences"][1]["reservation"]["table_ids"] == ["t_3"]
    assert after["occurrences"][1]["reservation"]["starts_at_local"] == \
        f"{WEEK_LATER}T21:00"


# --------------------------------------------------------------------------- #
# closures and plans in a snapshot
# --------------------------------------------------------------------------- #
def stage_three_snapshot(snapshot: dict) -> dict:
    """An export shaped the way the previous stage would have written it."""
    state = copy.deepcopy(snapshot["state"])
    for table in ("replans", "replan_assignments", "table_closures"):
        state.pop(table, None)
    for entry in state["reservation_history"]:
        entry.pop("plan_id", None)
    return {"track": snapshot["track"], "format_version": snapshot["format_version"],
            "state": state}


def test_closures_and_plans_survive_a_round_trip(client, room):
    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada)
    applied = assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    snapshot = export(client)

    reset(client, plan_fixture())
    assert_ok(client.post("/_test/import", json=snapshot), 204)

    assert ("t_2",) not in options_at(client, THURSDAY, 4, "19:00")
    read = assert_ok(client.get(f"/reservations/{made['reference']}",
                                headers=headers_for(ada)), 200)
    assert read["table_ids"] == ["t_3"] and read["revision"] == 2
    ledger = assert_ok(client.get(f"/reservations/{made['reference']}/history",
                                  headers=headers_for(ada)), 200)
    assert ledger["entries"][-1]["plan_id"] == plan["plan_id"]
    # The plan is still applied, so it cannot be applied again.
    response = apply_plan(client, ada, plan["plan_id"])
    assert response.status_code == 409
    assert error_of(response)["code"] == "plan_already_applied"
    # And the room is still at the revision the application left it at.
    assert revision_of(client) == applied["restaurant_revision"]


def test_a_proposed_plan_can_still_be_applied_after_a_round_trip(client, room):
    ada = room["token"]
    assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    plan = proposal(client, ada)
    snapshot = export(client)

    reset(client, plan_fixture())
    assert_ok(client.post("/_test/import", json=snapshot), 204)
    applied = assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    assert applied["restaurant_revision"] == plan["restaurant_revision"] + 1
    assert ("t_2",) not in options_at(client, THURSDAY, 4, "19:00")


def test_a_snapshot_from_the_previous_stage_imports_and_can_be_repaired(client, room):
    ada = room["token"]
    anchor, made = agreed(client, ada)
    assert_ok(client.post("/_test/import",
                          json=stage_three_snapshot(export(client))), 204)

    # Nothing was lost: the agreement, its bookings and their records all read back.
    after = series_of(client, ada, made["series_id"])
    assert after["revision"] == made["revision"]
    assert [o["reference"] for o in after["occurrences"]] == [
        o["reference"] for o in made["occurrences"]]
    ledger = assert_ok(client.get(f"/reservations/{anchor['reference']}/history",
                                  headers=headers_for(ada)), 200)
    assert [entry["event"] for entry in ledger["entries"]] == ["created"]
    assert "plan_id" not in ledger["entries"][0]

    # And the imported room can be planned and repaired like any other.
    plan = proposal(client, ada, closure_body("t_2", THURSDAY))
    assert plan["moved_count"] == 1
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    assert ledger_of(client, ada, anchor["reference"])[-1]["event"] == "reassigned"


def ledger_of(client, token, reference) -> list[dict]:
    return assert_ok(client.get(f"/reservations/{reference}/history",
                                headers=headers_for(token)), 200)["entries"]


def test_a_rejected_snapshot_leaves_the_closures_alone(client, room):
    ada = room["token"]
    plan = proposal(client, ada)
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    before = export(client)

    broken = copy.deepcopy(before)
    broken["state"]["table_closures"][0]["reason"] = "the leg is broken"
    response = client.post("/_test/import", json=broken)
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"
    # A snapshot that is refused leaves the destination exactly as it was.
    assert export(client) == before
    assert len(export(client)["state"]["table_closures"]) == 1


def test_a_snapshot_with_unknown_plan_columns_is_refused(client, room):
    broken = export(client)
    broken["state"]["replans"].append({"id": "pln_x"})
    response = client.post("/_test/import", json=broken)
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"


# --------------------------------------------------------------------------- #
# a repair is the operator's, not the diner's
# --------------------------------------------------------------------------- #
def test_a_revision_starts_at_zero_after_a_reset(client, room):
    assert revision_of(client) == 0
    assert proposal(client, room["token"])["restaurant_revision"] == 0


def test_the_revision_counts_every_real_write_once(client, room):
    """One increment per successful booking, amendment, cancellation, publication
    and plan — and none for a no-op, a failure, a preview or a replay."""
    ada = room["token"]
    assert revision_of(client) == 0

    made = assert_ok(seat(client, ada, "t_3", at=AT_1900, party_size=4), 201)
    assert revision_of(client) == 1

    # A no-op amendment, and a refused one, count for nothing.
    assert_ok(client.patch(f"/reservations/{made['reference']}",
                           json={"party_size": 4, "expected_revision": 1},
                           headers=headers_for(ada)), 200)
    assert seat(client, ada, "t_3", at=AT_1900, party_size=4).status_code == 409
    assert revision_of(client) == 1

    assert_ok(client.patch(f"/reservations/{made['reference']}",
                           json={"party_size": 6, "expected_revision": 1},
                           headers=headers_for(ada)), 200)
    assert revision_of(client) == 2
    assert_ok(publish(client, ada, three_table_policy_body(THURSDAY)), 201)
    assert revision_of(client) == 3

    plan = proposal(client, ada)
    key = f"k_{uuid.uuid4().hex}"
    assert revision_of(client) == 3  # a preview is not a change
    assert_ok(apply_plan(client, ada, plan["plan_id"], key=key), 201)
    assert revision_of(client) == 4
    assert_ok(apply_plan(client, ada, plan["plan_id"], key=key), 200)
    assert revision_of(client) == 4  # nor is a replay of one


def test_a_cutoff_that_has_passed_does_not_stop_a_repair(client, room):
    """The diner can no longer change this booking; the room still has to seat it."""
    import datetime as dt

    from tablekeeper import clock

    ada = room["token"]
    made = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    clock.freeze(dt.datetime(2026, 9, 24, 16, 30, tzinfo=dt.timezone.utc))
    refused = client.patch(f"/reservations/{made['reference']}",
                           json={"party_size": 6, "expected_revision": 1},
                           headers=headers_for(ada))
    assert refused.status_code == 409
    assert error_of(refused)["code"] == "cutoff_passed"

    plan = proposal(client, ada)
    assert plan["moved_count"] == 1
    applied = assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    assert applied["reservations"][0]["table_ids"] == ["t_3"]
    assert applied["reservations"][0]["party_size"] == 4
    assert applied["reservations"][0]["status"] == "confirmed"


def test_no_booking_disappears_or_is_cancelled_by_a_repair(client, room, diner):
    ada, bob = room["token"], diner["token"]
    mine = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    yours = assert_ok(seat(client, bob, "t_3", at=AT_2100, party_size=6), 201)
    plan = proposal(client, ada)
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)

    assert len(export(client)["state"]["reservations"]) == 2
    for token, made in ((ada, mine), (bob, yours)):
        read = assert_ok(client.get(f"/reservations/{made['reference']}",
                                    headers=headers_for(token)), 200)
        assert read["status"] == "confirmed"
        assert read["party_size"] == made["party_size"]
        assert read["starts_at_local"] == made["starts_at_local"]
        assert read["ends_at"] == made["ends_at"]
        assert read["accepted_terms"] == made["accepted_terms"]
        assert read["restaurant_id"] == "r_anker"
    # Each diner still sees exactly their own booking.
    assert len(assert_ok(client.get("/reservations", headers=headers_for(bob)),
                         200)["reservations"]) == 1


def test_an_imported_agreement_with_moved_and_cancelled_occurrences_can_be_repaired(
        client, room):
    """A snapshot from the previous stage carries agreements that diners have
    already reshaped; both operations still work on them."""
    ada = room["token"]
    anchor = assert_ok(seat(client, ada, "t_2", at=AT_1900, party_size=4), 201)
    adoption_key = f"k_{uuid.uuid4().hex}"
    made = assert_ok(adopt(client, ada, anchor["reference"], count=3,
                           key=adoption_key), 201)
    moved = made["occurrences"][1]["reference"]
    cancelled = made["occurrences"][2]["reference"]
    assert_ok(client.patch(f"/reservations/{moved}",
                           json={"starts_at_local": f"{WEEK_LATER}T21:00",
                                 "expected_revision": 1},
                           headers=headers_for(ada)), 200)
    assert_ok(client.post(f"/reservations/{cancelled}/cancel", json={},
                          headers=headers_for(ada)), 200)
    snapshot = stage_three_snapshot(export(client))

    reset(client, plan_fixture())
    assert_ok(client.post("/_test/import", json=snapshot), 204)

    plan = proposal(client, ada, THREE_THURSDAYS)
    # The cancelled occurrence is not considered; the moved one is, at the time the
    # diner put it at.
    assert plan["moved_count"] == 2
    assert sorted(a["reference"] for a in plan["assignments"]) == sorted(
        [anchor["reference"], moved])
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)

    after = series_of(client, ada, made["series_id"])
    by_reference = {o["reference"]: o for o in after["occurrences"]}
    assert by_reference[anchor["reference"]]["reservation"]["table_ids"] == ["t_3"]
    assert by_reference[moved]["exception"] is True
    assert by_reference[moved]["reservation"]["starts_at_local"] == f"{WEEK_LATER}T21:00"
    assert by_reference[moved]["reservation"]["table_ids"] == ["t_3"]
    assert by_reference[cancelled]["reservation"]["status"] == "cancelled"
    assert by_reference[cancelled]["reservation"]["table_ids"] == ["t_2"]

    # The diner can still reschedule what is left of the pattern...
    amended_series = assert_ok(client.post(
        f"/series/{made['series_id']}/amend",
        json={"expected_revision": after["revision"], "from_index": 0,
              "local_time": "20:00"},
        headers=headers_for(ada, f"k_{uuid.uuid4().hex}")), 201)
    assert amended_series["occurrences"][0]["reservation"]["starts_at_local"] == \
        f"{THURSDAY}T20:00"
    # ...and a receipt taken before the import still replays, with the answer the
    # diner got then rather than the state the room is in now.
    replay = client.post("/series", json={"anchor_reference": anchor["reference"],
                                          "count": 3, "interval_weeks": 1},
                         headers=headers_for(ada, adoption_key))
    assert replay.status_code == 200, replay.text
    assert replay.json() == made
