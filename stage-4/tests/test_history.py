"""Stage 3: a reservation's own record, its revision, and the terms it accepted.

Three ideas are tested together because they are one mechanism. A booking is
decided under a policy and keeps a snapshot of it; every real change adds one
revision, replaces the terms with the policy that applies to the resulting date,
and writes one entry to the record; and a change that changes nothing does none of
those three things.
"""

from __future__ import annotations

import json
import threading
import uuid

import pytest

from .conftest import (
    FRIDAY,
    THURSDAY,
    assert_ok,
    base_fixture,
    book,
    error_of,
    headers_for,
    login_headers,
    managed_fixture,
    policy_body,
    publish,
    reset,
    signup,
)

AT = f"{THURSDAY}T19:00"
LATER = f"{THURSDAY}T21:00"


@pytest.fixture
def diner(client, seeded) -> dict:
    """The default fixture, loaded, with one diner signed up."""
    return {"token": signup(client)["token"], "fixture": seeded}


def headers(diner: dict, key: str | None = None) -> dict:
    return headers_for(diner["token"], key)


def amend(client, diner, reference: str, body: dict):
    return client.patch(f"/reservations/{reference}", json=body, headers=headers(diner))


def history_of(client, token, reference) -> list[dict]:
    return assert_ok(
        client.get(f"/reservations/{reference}/history", headers=headers_for(token)), 200
    )["entries"]


def decision_of(client, token, reference) -> dict:
    return assert_ok(
        client.get(f"/reservations/{reference}/decision", headers=headers_for(token)), 200)


def moves(client, diner, moves_list, key=None):
    return client.post("/reservation-moves", json={"moves": moves_list},
                       headers=headers(diner, key or f"k_{uuid.uuid4().hex}"))


def export(client) -> dict:
    return assert_ok(client.get("/_test/export"), 200)["state"]


def restaurant_revision(client) -> int:
    return int(export(client)["restaurants"][0]["revision"])


# --------------------------------------------------------------------------- #
# the record of a booking
# --------------------------------------------------------------------------- #
def test_creation_is_the_first_entry(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    entries = history_of(client, diner["token"], created["reference"])
    assert [entry["event"] for entry in entries] == ["created"]
    assert entries[0]["seq"] == 1
    assert {change["field"] for change in entries[0]["changes"]} == {
        "table_id", "starts_at_local", "party_size"}
    assert all(change["from"] is None for change in entries[0]["changes"])
    assert entries[0]["revision"] == 1
    assert entries[0]["accepted_terms"] == created["accepted_terms"]


def test_a_created_entry_names_its_fields_in_order(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    changes = history_of(client, diner["token"], created["reference"])[0]["changes"]
    assert [change["field"] for change in changes] == [
        "table_id", "starts_at_local", "party_size"]
    assert changes[0]["to"] == "t_2"
    assert changes[1]["to"] == AT
    assert changes[2]["to"] == 4


def test_a_change_records_the_old_value(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    assert_ok(amend(client, diner, reference, {"table_id": "t_1", "party_size": 2}), 200)
    entries = history_of(client, diner["token"], reference)
    assert [entry["seq"] for entry in entries] == [1, 2]
    assert entries[1]["event"] == "changed"
    assert entries[1]["changes"] == [
        {"field": "table_id", "from": "t_2", "to": "t_1"},
        {"field": "party_size", "from": 4, "to": 2},
    ]


def test_a_change_names_its_fields_in_the_documented_order(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    assert_ok(amend(client, diner, reference,
                    {"table_id": "t_1", "starts_at_local": LATER, "party_size": 2}), 200)
    changes = history_of(client, diner["token"], reference)[1]["changes"]
    assert [change["field"] for change in changes] == [
        "table_id", "starts_at_local", "party_size"]


def test_only_the_fields_that_changed_are_named(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    assert_ok(amend(client, diner, reference, {"party_size": 3}), 200)
    assert history_of(client, diner["token"], reference)[1]["changes"] == [
        {"field": "party_size", "from": 4, "to": 3}]


def test_seq_increases_by_exactly_one(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    for party in (3, 2, 4, 3):
        assert_ok(amend(client, diner, reference, {"party_size": party}), 200)
    entries = history_of(client, diner["token"], reference)
    assert [entry["seq"] for entry in entries] == [1, 2, 3, 4, 5]
    assert [entry["revision"] for entry in entries] == [1, 2, 3, 4, 5]


def test_entries_are_returned_in_seq_order_which_is_also_at_order(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    assert_ok(amend(client, diner, reference, {"party_size": 3}), 200)
    entries = history_of(client, diner["token"], reference)
    assert [entry["at"] for entry in entries] == sorted(entry["at"] for entry in entries)


def test_a_no_op_amendment_records_nothing(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    body = assert_ok(amend(client, diner, reference,
                           {"table_id": "t_2", "starts_at_local": AT, "party_size": 4}), 200)
    assert body == created, "a no-op returns the booking as it is"
    assert [entry["event"] for entry in history_of(client, diner["token"], reference)] == ["created"]
    assert body["revision"] == 1


def test_a_no_op_still_needs_a_confirmed_editable_booking(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    assert_ok(client.post(f"/reservations/{reference}/cancel", headers=headers(diner)), 200)
    response = amend(client, diner, reference, {"party_size": 4})
    assert error_of(response)["code"] == "reservation_cancelled"


def test_cancellation_is_the_last_entry_and_carries_no_changes(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    assert_ok(amend(client, diner, reference, {"party_size": 3}), 200)
    assert_ok(client.post(f"/reservations/{reference}/cancel", headers=headers(diner)), 200)
    entries = history_of(client, diner["token"], reference)
    assert [entry["event"] for entry in entries] == ["created", "changed", "cancelled"]
    assert entries[-1]["changes"] == []
    assert entries[-1]["revision"] == 3
    assert entries[-1]["accepted_terms"] == entries[-2]["accepted_terms"]


def test_a_cancelled_booking_still_has_its_history(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    assert_ok(client.post(f"/reservations/{reference}/cancel", headers=headers(diner)), 200)
    assert len(history_of(client, diner["token"], reference)) == 2


def test_replaying_an_idempotent_booking_records_nothing(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4,
                             key="one-key"), 201)
    replay = book(client, diner["token"], table_id="t_2", party_size=4, key="one-key")
    assert replay.status_code == 200
    assert replay.json() == created
    assert len(history_of(client, diner["token"], created["reference"])) == 1


def test_a_failed_amendment_records_nothing(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    assert error_of(amend(client, diner, reference, {"table_id": "t_nope"}))["code"] == "not_found"
    assert error_of(amend(client, diner, reference, {"starts_at_local": f"{THURSDAY}T19:07"}))[
        "code"] == "not_on_slot_grid"
    entries = history_of(client, diner["token"], reference)
    assert [entry["event"] for entry in entries] == ["created"]
    assert assert_ok(client.get(f"/reservations/{reference}", headers=headers(diner)), 200) == created


# --------------------------------------------------------------------------- #
# who may read it
# --------------------------------------------------------------------------- #
def test_another_diner_gets_the_same_404_as_a_reference_that_does_not_exist(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    intruder = signup(client, email="intruder@example.com")["token"]
    for path in ("history", "decision"):
        response = client.get(f"/reservations/{created['reference']}/{path}",
                              headers=headers_for(intruder))
        assert error_of(response)["code"] == "not_found", path
        missing = client.get(f"/reservations/ZZZZZZZZ/{path}", headers=headers_for(intruder))
        assert error_of(missing)["code"] == "not_found", path


@pytest.mark.parametrize("path", ["history", "decision"])
def test_no_token_at_all_is_also_a_404_not_a_401(client, diner, path):
    """Neither endpoint can be used to find out whether a reference exists."""
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    for reference in (created["reference"], "ZZZZZZZZ"):
        response = client.get(f"/reservations/{reference}/{path}")
        assert response.status_code == 404, response.text
        assert error_of(response)["code"] == "not_found"


def test_a_token_that_is_offered_and_wrong_is_still_a_401(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    response = client.get(f"/reservations/{created['reference']}/history",
                          headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthenticated"


# --------------------------------------------------------------------------- #
# the decision endpoint
# --------------------------------------------------------------------------- #
def test_the_decision_is_the_booking_current_terms(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    decision = decision_of(client, diner["token"], created["reference"])
    assert set(decision) == {"reference", "revision", "accepted_terms"}
    assert decision["reference"] == created["reference"]
    assert decision["revision"] == created["revision"] == 1
    assert decision["accepted_terms"] == created["accepted_terms"]


def test_the_decision_follows_a_real_change(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    changed = assert_ok(amend(client, diner, reference, {"party_size": 3}), 200)
    decision = decision_of(client, diner["token"], reference)
    assert decision["revision"] == changed["revision"] == 2
    assert decision["accepted_terms"] == changed["accepted_terms"]


def test_the_decision_survives_cancellation(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    cancelled = assert_ok(
        client.post(f"/reservations/{reference}/cancel", headers=headers(diner)), 200)
    decision = decision_of(client, diner["token"], reference)
    assert decision["revision"] == cancelled["revision"] == 2
    assert decision["accepted_terms"] == created["accepted_terms"]


# --------------------------------------------------------------------------- #
# accepted terms and revisions
# --------------------------------------------------------------------------- #
def test_the_accepted_terms_are_the_whole_policy_without_its_date(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    assert set(created["accepted_terms"]) == {
        "policy_version", "slot_minutes", "reservation_duration_minutes",
        "cancellation_cutoff_minutes", "opening_hours", "capacities"}
    assert "effective_from" not in created["accepted_terms"]
    assert created["accepted_terms"] == {
        "policy_version": 0, "slot_minutes": 30, "reservation_duration_minutes": 90,
        "cancellation_cutoff_minutes": 120,
        "opening_hours": [
            {"weekday": "thu", "opens": "18:00", "closes": "23:00"},
            {"weekday": "fri", "opens": "18:00", "closes": "23:30"}],
        "capacities": {"t_1": 2, "t_2": 4}}


def test_a_seeded_booking_starts_at_revision_one_under_policy_zero(client, seeded):
    fixture = base_fixture()
    fixture["reservations"] = [{
        "id": "res_seed", "reference": "SEED0001", "user_id": "u_ada",
        "restaurant_id": "r_anker", "table_id": "t_2", "party_size": 4,
        "starts_at_local": AT}]
    reset(client, fixture)
    token = login_headers(client)["Authorization"].split(" ", 1)[1]
    body = assert_ok(client.get("/reservations/SEED0001", headers=headers_for(token)), 200)
    assert body["revision"] == 1
    assert body["accepted_terms"]["policy_version"] == 0
    entries = history_of(client, token, "SEED0001")
    assert [entry["event"] for entry in entries] == ["created"]


def test_cancellation_adds_exactly_one_revision(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    cancelled = assert_ok(
        client.post(f"/reservations/{reference}/cancel", headers=headers(diner)), 200)
    assert cancelled["revision"] == 2
    again = assert_ok(
        client.post(f"/reservations/{reference}/cancel", headers=headers(diner)), 200)
    assert again["revision"] == 2, "cancelling twice is not a second change"


def test_a_real_amendment_replaces_the_terms_and_the_end_time_together(client, diner):
    fixture = managed_fixture()
    reset(client, fixture)
    manager = login_headers(client)["Authorization"].split(" ", 1)[1]
    guest = signup(client, email="guest@example.com")["token"]
    created = assert_ok(book(client, guest, table_id="t_2", party_size=4,
                             starts_at_local=f"{THURSDAY}T19:00"), 201)
    assert created["accepted_terms"]["policy_version"] == 0
    assert_ok(publish(client, manager, policy_body(effective_from=FRIDAY,
                                                   reservation_duration_minutes=45)), 201)
    moved = assert_ok(amend(client, {"token": guest}, created["reference"],
                            {"starts_at_local": f"{FRIDAY}T19:00"}), 200)
    assert moved["revision"] == 2
    assert moved["accepted_terms"]["policy_version"] == 1
    assert moved["accepted_terms"]["reservation_duration_minutes"] == 45
    assert moved["ends_at"].endswith("19:45:00+02:00"), moved["ends_at"]
    # The old entry keeps the terms it was written with.
    entries = history_of(client, guest, created["reference"])
    assert entries[0]["accepted_terms"]["policy_version"] == 0
    assert entries[1]["accepted_terms"]["policy_version"] == 1


def test_a_no_op_amendment_keeps_its_terms_even_under_a_new_policy(client, diner):
    fixture = managed_fixture()
    reset(client, fixture)
    manager = login_headers(client)["Authorization"].split(" ", 1)[1]
    guest = signup(client, email="guest@example.com")["token"]
    created = assert_ok(book(client, guest, table_id="t_2", party_size=4), 201)
    assert_ok(publish(client, manager, policy_body(reservation_duration_minutes=45)), 201)
    same = assert_ok(amend(client, {"token": guest}, created["reference"], {"party_size": 4}), 200)
    assert same == created, "nothing about the booking moved, so nothing about it changed"


def test_a_replay_returns_the_original_revision_and_terms(client, diner):
    fixture = managed_fixture()
    reset(client, fixture)
    manager = login_headers(client)["Authorization"].split(" ", 1)[1]
    guest = signup(client, email="guest@example.com")["token"]
    created = assert_ok(book(client, guest, table_id="t_2", party_size=4, key="one-key"), 201)
    assert_ok(publish(client, manager, policy_body(reservation_duration_minutes=45)), 201)
    assert_ok(amend(client, {"token": guest}, created["reference"], {"party_size": 3}), 200)
    replay = book(client, guest, table_id="t_2", party_size=4, key="one-key")
    assert replay.status_code == 200
    assert replay.json() == created, "the receipt is the answer, however stale it is"


def test_cancellation_is_judged_by_the_cutoff_the_diner_accepted(client, diner):
    """A tighter policy published afterwards does not reach back into a booking."""
    fixture = managed_fixture()
    reset(client, fixture)
    manager = login_headers(client)["Authorization"].split(" ", 1)[1]
    guest = signup(client, email="guest@example.com")["token"]
    created = assert_ok(book(client, guest, table_id="t_2", party_size=4), 201)
    # A week's notice would put the deadline before today; the accepted two hours
    # do not, so this booking is still cancellable.
    assert_ok(publish(client, manager,
                      policy_body(cancellation_cutoff_minutes=10080)), 201)
    cancelled = assert_ok(
        client.post(f"/reservations/{created['reference']}/cancel",
                    headers=headers_for(guest)), 200)
    assert cancelled["status"] == "cancelled"


def test_a_looser_policy_does_not_rescue_a_booking_that_accepted_a_tight_one(
        client, diner):
    fixture = managed_fixture()
    reset(client, fixture)
    manager = login_headers(client)["Authorization"].split(" ", 1)[1]
    guest = signup(client, email="guest@example.com")["token"]
    assert_ok(publish(client, manager,
                      policy_body(cancellation_cutoff_minutes=10080)), 201)
    created = assert_ok(book(client, guest, table_id="t_2", party_size=4), 201)
    assert created["accepted_terms"]["cancellation_cutoff_minutes"] == 10080
    # The deadline was a week before the sitting, which has already passed.
    assert error_of(client.post(f"/reservations/{created['reference']}/cancel",
                                headers=headers_for(guest)))["code"] == "cutoff_passed"
    assert_ok(publish(client, manager, policy_body(cancellation_cutoff_minutes=0)), 201)
    assert error_of(client.post(f"/reservations/{created['reference']}/cancel",
                                headers=headers_for(guest)))["code"] == "cutoff_passed"


# --------------------------------------------------------------------------- #
# expected_revision
# --------------------------------------------------------------------------- #
def test_an_amendment_may_state_the_revision_it_expects(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    body = assert_ok(amend(client, diner, created["reference"],
                           {"party_size": 3, "expected_revision": 1}), 200)
    assert body["revision"] == 2


def test_a_revision_that_is_not_the_current_one_is_stale(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    assert_ok(amend(client, diner, reference, {"party_size": 3}), 200)
    response = amend(client, diner, reference, {"party_size": 2, "expected_revision": 1})
    assert response.status_code == 409
    assert error_of(response)["code"] == "stale_revision"
    assert assert_ok(client.get(f"/reservations/{reference}", headers=headers(diner)), 200)[
        "party_size"] == 3, "a stale amendment changed nothing"


def test_stale_revision_is_refused_before_the_booking_state_is_judged(client, diner):
    """The client is told its view of the booking is old before anything else."""
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    assert_ok(amend(client, diner, reference, {"party_size": 3}), 200)
    assert_ok(client.post(f"/reservations/{reference}/cancel", headers=headers(diner)), 200)
    response = amend(client, diner, reference, {"party_size": 2, "expected_revision": 1})
    assert error_of(response)["code"] == "stale_revision"


def test_a_revision_that_matches_still_meets_the_cutoff(client, diner):
    """The two refusals are ordered, not interchangeable: this one is the cutoff's."""
    fixture = managed_fixture()
    reset(client, fixture)
    manager = login_headers(client)["Authorization"].split(" ", 1)[1]
    guest = signup(client, email="guest@example.com")["token"]
    assert_ok(publish(client, manager,
                      policy_body(cancellation_cutoff_minutes=10080)), 201)
    created = assert_ok(book(client, guest, table_id="t_2", party_size=4), 201)
    response = amend(client, {"token": guest}, created["reference"],
                     {"party_size": 3, "expected_revision": 1})
    assert error_of(response)["code"] == "cutoff_passed"


def test_stale_revision_is_refused_before_any_field_is_validated(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    assert_ok(amend(client, diner, reference, {"party_size": 3}), 200)
    response = amend(client, diner, reference,
                     {"table_id": "t_nope", "expected_revision": 1})
    assert error_of(response)["code"] == "stale_revision"


@pytest.mark.parametrize("value", [0, -1, "1", True, False, 1.5, [], {}])
def test_an_expected_revision_must_be_a_positive_integer(client, diner, value):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    response = amend(client, diner, created["reference"],
                     {"party_size": 3, "expected_revision": value})
    assert response.status_code == 422, repr(value)
    assert error_of(response)["code"] == "validation_failed"


def test_an_expected_revision_of_null_is_the_same_as_omitting_it(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    body = assert_ok(amend(client, diner, created["reference"],
                           {"party_size": 3, "expected_revision": None}), 200)
    assert body["revision"] == 2


def test_only_one_of_two_concurrent_amendments_of_one_revision_succeeds(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    outcomes: list = []
    barrier = threading.Barrier(2)
    attempts = [{"party_size": 3, "expected_revision": 1},
                {"starts_at_local": LATER, "expected_revision": 1}]

    def attempt(index: int) -> None:
        barrier.wait(timeout=30)
        outcomes.append(amend(client, diner, reference, attempts[index]))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    statuses = sorted(response.status_code for response in outcomes)
    assert statuses == [200, 409], statuses
    refused = next(r for r in outcomes if r.status_code == 409)
    assert error_of(refused)["code"] == "stale_revision"
    final = assert_ok(client.get(f"/reservations/{reference}", headers=headers(diner)), 200)
    assert final["revision"] == 2, "one real change, one revision"
    assert len(history_of(client, diner["token"], reference)) == 2


def test_a_move_may_state_the_revision_it_expects(client, diner):
    first = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    response = moves(client, diner, [
        {"reference": first["reference"], "party_size": 3, "expected_revision": 1}])
    assert_ok(response, 201)
    stale = moves(client, diner, [
        {"reference": first["reference"], "party_size": 2, "expected_revision": 1}])
    assert error_of(stale)["code"] == "stale_revision"


def test_a_move_with_a_bad_expected_revision_is_refused(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    response = moves(client, diner, [
        {"reference": created["reference"], "expected_revision": 0}])
    assert error_of(response)["code"] == "validation_failed"


# --------------------------------------------------------------------------- #
# combinations in the record
# --------------------------------------------------------------------------- #
def combined_fixture() -> dict:
    fixture = base_fixture()
    fixture["restaurants"][0]["combinable"] = [["t_1", "t_2"]]
    return fixture


def test_creating_a_pair_records_table_ids(client, seeded):
    reset(client, combined_fixture())
    token = signup(client)["token"]
    created = assert_ok(client.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": AT, "party_size": 6}, headers=headers_for(token, "pair")), 201)
    changes = history_of(client, token, created["reference"])[0]["changes"]
    assert changes[0] == {"field": "table_ids", "from": None, "to": ["t_1", "t_2"]}
    assert created["accepted_terms"]["capacities"] == {"t_1": 2, "t_2": 4}


def test_a_pair_is_recorded_in_the_declared_order(client, seeded):
    reset(client, combined_fixture())
    token = signup(client)["token"]
    created = assert_ok(client.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_2", "t_1"],
        "starts_at_local": AT, "party_size": 6}, headers=headers_for(token, "reversed")), 201)
    changes = history_of(client, token, created["reference"])[0]["changes"]
    assert changes[0]["to"] == ["t_1", "t_2"], "the ledger names the declared pair"
    assert created["table_ids"] == ["t_2", "t_1"], "the booking keeps the order asked for"


def test_a_reversed_pair_is_not_an_amendment(client, seeded):
    reset(client, combined_fixture())
    token = signup(client)["token"]
    created = assert_ok(client.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_2", "t_1"],
        "starts_at_local": AT, "party_size": 6}, headers=headers_for(token, "pair")), 201)
    same = assert_ok(client.patch(f"/reservations/{created['reference']}",
                                  json={"table_ids": ["t_1", "t_2"]},
                                  headers=headers_for(token)), 200)
    assert same["revision"] == 1
    assert same["table_ids"] == ["t_2", "t_1"], "the stored order was not rewritten"
    assert [entry["event"] for entry in history_of(client, token, created["reference"])] == [
        "created"]


def test_moving_onto_a_pair_records_both_lists(client, seeded):
    reset(client, combined_fixture())
    token = signup(client)["token"]
    created = assert_ok(book(client, token, table_id="t_1", party_size=2), 201)
    changed = assert_ok(client.patch(
        f"/reservations/{created['reference']}",
        json={"table_ids": ["t_1", "t_2"], "party_size": 6}, headers=headers_for(token)), 200)
    assert changed["table_ids"] == ["t_1", "t_2"]
    entry = history_of(client, token, created["reference"])[1]
    assert entry["changes"] == [
        {"field": "table_ids", "from": ["t_1"], "to": ["t_1", "t_2"]},
        {"field": "party_size", "from": 2, "to": 6},
    ]


def test_taking_a_pair_apart_records_both_lists(client, seeded):
    reset(client, combined_fixture())
    token = signup(client)["token"]
    created = assert_ok(client.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": AT, "party_size": 6}, headers=headers_for(token, "pair")), 201)
    assert_ok(client.patch(f"/reservations/{created['reference']}",
                           json={"table_id": "t_2", "party_size": 4},
                           headers=headers_for(token)), 200)
    entry = history_of(client, token, created["reference"])[1]
    assert entry["changes"][0] == {
        "field": "table_ids", "from": ["t_1", "t_2"], "to": ["t_2"]}


def test_a_single_to_single_change_keeps_using_table_id(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    assert_ok(amend(client, diner, created["reference"],
                    {"table_id": "t_1", "party_size": 2}), 200)
    entry = history_of(client, diner["token"], created["reference"])[1]
    assert entry["changes"][0] == {"field": "table_id", "from": "t_2", "to": "t_1"}


def test_a_pair_accepted_the_policy_that_decided_it(client, seeded):
    fixture = managed_fixture()
    fixture["restaurants"][0]["combinable"] = [["t_1", "t_2"]]
    reset(client, fixture)
    manager = login_headers(client)["Authorization"].split(" ", 1)[1]
    guest = signup(client, email="guest@example.com")["token"]
    assert_ok(publish(client, manager, policy_body(capacities={"t_1": 4, "t_2": 4})), 201)
    created = assert_ok(client.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": AT, "party_size": 8}, headers=headers_for(guest, "big-pair")), 201)
    assert created["accepted_terms"]["capacities"] == {"t_1": 4, "t_2": 4}
    assert created["accepted_terms"]["policy_version"] == 1


# --------------------------------------------------------------------------- #
# moves
# --------------------------------------------------------------------------- #
def test_a_move_records_one_entry_per_real_change(client, diner):
    first = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    second = assert_ok(book(client, diner["token"], table_id="t_1", party_size=2,
                            starts_at_local=LATER), 201)
    assert_ok(moves(client, diner, [
        {"reference": first["reference"], "starts_at_local": LATER},
        {"reference": second["reference"], "party_size": 2},      # unchanged
    ]), 201)
    assert [e["event"] for e in history_of(client, diner["token"], first["reference"])] == [
        "created", "changed"]
    assert [e["event"] for e in history_of(client, diner["token"], second["reference"])] == [
        "created"], "a move that changed nothing recorded nothing"


def test_a_move_gives_each_changed_booking_one_revision(client, diner):
    first = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    second = assert_ok(book(client, diner["token"], table_id="t_1", party_size=2,
                            starts_at_local=LATER), 201)
    body = assert_ok(moves(client, diner, [
        {"reference": first["reference"], "party_size": 3},
        {"reference": second["reference"], "party_size": 2},
    ]), 201)
    assert [r["revision"] for r in body["reservations"]] == [2, 1]


def test_a_failed_batch_records_nothing(client, diner):
    first = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    second = assert_ok(book(client, diner["token"], table_id="t_1", party_size=2,
                            starts_at_local=LATER), 201)
    response = moves(client, diner, [
        {"reference": first["reference"], "party_size": 3},
        {"reference": second["reference"], "table_id": "t_nope"},
    ])
    assert response.status_code == 404
    for reference in (first["reference"], second["reference"]):
        assert [e["event"] for e in history_of(client, diner["token"], reference)] == ["created"]
        assert assert_ok(client.get(f"/reservations/{reference}",
                                    headers=headers(diner)), 200)["revision"] == 1


# --------------------------------------------------------------------------- #
# the restaurant's own revision
# --------------------------------------------------------------------------- #
def test_the_restaurant_revision_counts_successful_writes(client, diner):
    assert restaurant_revision(client) == 0, "a fresh fixture has been changed by nobody"
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    assert restaurant_revision(client) == 1
    assert_ok(amend(client, diner, created["reference"], {"party_size": 3}), 200)
    assert restaurant_revision(client) == 2
    assert_ok(amend(client, diner, created["reference"], {"party_size": 3}), 200)
    assert restaurant_revision(client) == 2, "a no-op is not a write"
    assert_ok(client.post(f"/reservations/{created['reference']}/cancel",
                          headers=headers(diner)), 200)
    assert restaurant_revision(client) == 3
    assert_ok(client.post(f"/reservations/{created['reference']}/cancel",
                          headers=headers(diner)), 200)
    assert restaurant_revision(client) == 3, "and neither is cancelling twice"


def test_a_refused_write_does_not_count(client, diner):
    assert error_of(book(client, diner["token"], table_id="t_nope"))["code"] == "not_found"
    assert restaurant_revision(client) == 0


def test_a_policy_publication_counts_once(client, seeded):
    reset(client, managed_fixture())
    token = login_headers(client)["Authorization"].split(" ", 1)[1]
    assert_ok(publish(client, token, policy_body()), 201)
    assert restaurant_revision(client) == 1
    assert publish(client, token, policy_body(slot_minutes=0)).status_code == 422
    assert restaurant_revision(client) == 1


def test_a_batch_of_moves_counts_once(client, diner):
    first = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    second = assert_ok(book(client, diner["token"], table_id="t_1", party_size=2,
                            starts_at_local=LATER), 201)
    assert restaurant_revision(client) == 2
    assert_ok(moves(client, diner, [
        {"reference": first["reference"], "party_size": 3},
        {"reference": second["reference"], "party_size": 1},
    ]), 201)
    assert restaurant_revision(client) == 3, "one increment for the whole batch"


def test_a_batch_that_changes_nothing_does_not_count(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    assert_ok(moves(client, diner, [{"reference": created["reference"], "party_size": 4}]), 201)
    assert restaurant_revision(client) == 1


# --------------------------------------------------------------------------- #
# export and import
# --------------------------------------------------------------------------- #
def test_a_round_trip_preserves_terms_revisions_and_records(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    assert_ok(amend(client, diner, reference, {"party_size": 3}), 200)
    entries = history_of(client, diner["token"], reference)
    snapshot = assert_ok(client.get("/_test/export"), 200)

    reset(client, base_fixture())
    assert_ok(client.post("/_test/import", json=snapshot), 204)

    restored = assert_ok(client.get(f"/reservations/{reference}", headers=headers(diner)), 200)
    assert restored["revision"] == 2
    assert restored["accepted_terms"] == created["accepted_terms"]
    assert history_of(client, diner["token"], reference) == entries


def test_policies_survive_a_round_trip_and_still_decide(client, seeded):
    reset(client, managed_fixture())
    manager = login_headers(client)["Authorization"].split(" ", 1)[1]
    guest = signup(client, email="guest@example.com")["token"]
    assert_ok(publish(client, manager, policy_body(reservation_duration_minutes=60)), 201)
    created = assert_ok(book(client, guest, table_id="t_2", party_size=4), 201)
    snapshot = assert_ok(client.get("/_test/export"), 200)

    reset(client, managed_fixture())
    assert policies_of_versions(client) == []
    assert_ok(client.post("/_test/import", json=snapshot), 204)
    assert policies_of_versions(client) == [1]

    again = assert_ok(book(client, guest, table_id="t_1", party_size=2,
                           starts_at_local=LATER), 201)
    assert again["accepted_terms"]["policy_version"] == 1
    assert again["ends_at"].endswith("22:00:00+02:00")
    assert assert_ok(client.get(f"/reservations/{created['reference']}",
                                headers=headers_for(guest)), 200) == created


def policies_of_versions(client) -> list[int]:
    return [p["policy_version"] for p in
            assert_ok(client.get("/restaurants/r_anker/policies"), 200)["policies"]]


def test_a_stage_2_snapshot_imports_with_terms_and_a_record(client, diner):
    """A snapshot from before policies existed has neither, and both are rebuilt."""
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    reference = created["reference"]
    snapshot = assert_ok(client.get("/_test/export"), 200)
    # Everything this stage added is taken out, so what is left is what an earlier
    # stage would actually have exported.
    for table in ("restaurant_policies", "restaurant_managers", "reservation_history",
                  "restaurant_tables"):
        snapshot["state"].pop(table, None)
    for record in snapshot["state"]["restaurants"]:
        record.pop("revision", None)
    for record in snapshot["state"]["reservations"]:
        record.pop("revision", None)
        record.pop("accepted_terms", None)

    reset(client, base_fixture())
    assert_ok(client.post("/_test/import", json=snapshot), 204)

    restored = assert_ok(client.get(f"/reservations/{reference}", headers=headers(diner)), 200)
    assert restored["revision"] == 1
    assert restored["accepted_terms"]["policy_version"] == 0
    assert restored["accepted_terms"]["reservation_duration_minutes"] == 90
    assert restored["accepted_terms"]["capacities"] == {"t_1": 2, "t_2": 4}
    entries = history_of(client, diner["token"], reference)
    assert [entry["event"] for entry in entries] == ["created"]
    assert entries[0]["at"] == restored["created_at"]
    # And the imported booking can still be amended and cancelled.
    assert_ok(amend(client, diner, reference, {"party_size": 3}), 200)
    assert history_of(client, diner["token"], reference)[-1]["changes"] == [
        {"field": "party_size", "from": 4, "to": 3}]


def test_an_earlier_snapshot_with_no_bookings_at_all_still_imports(client, seeded):
    """The shape the shipped upgrade check imports: accounts, a restaurant, nothing booked."""
    snapshot = assert_ok(client.get("/_test/export"), 200)
    for table in ("restaurant_policies", "restaurant_managers", "reservation_history"):
        snapshot["state"].pop(table, None)
    snapshot["state"].pop("restaurant_combinable", None)
    snapshot["state"].pop("reservation_tables", None)
    for record in snapshot["state"]["restaurants"]:
        record.pop("revision", None)
    assert snapshot["state"]["reservations"] == []

    reset(client, base_fixture())
    assert_ok(client.post("/_test/import", json=snapshot), 204)
    token = login_headers(client)["Authorization"].split(" ", 1)[1]
    assert assert_ok(client.get("/reservations", headers=headers_for(token)), 200) == {
        "reservations": []}
    assert restaurant_revision(client) == 0
    assert_ok(book(client, token, table_id="t_2", party_size=4), 201)


def test_a_stage_2_snapshot_keeps_a_live_session_and_a_pending_retry(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4,
                             key="retry-me"), 201)
    snapshot = assert_ok(client.get("/_test/export"), 200)
    snapshot["state"].pop("reservation_history", None)
    for record in snapshot["state"]["restaurants"]:
        record.pop("revision", None)
    for record in snapshot["state"]["reservations"]:
        record.pop("revision", None)
        record.pop("accepted_terms", None)
    reset(client, base_fixture())
    assert_ok(client.post("/_test/import", json=snapshot), 204)
    replay = book(client, diner["token"], table_id="t_2", party_size=4, key="retry-me")
    assert replay.status_code == 200
    assert replay.json() == created


def test_the_history_of_a_pair_survives_a_round_trip(client, seeded):
    reset(client, combined_fixture())
    token = signup(client)["token"]
    created = assert_ok(client.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": AT, "party_size": 6}, headers=headers_for(token, "pair")), 201)
    entries = history_of(client, token, created["reference"])
    snapshot = assert_ok(client.get("/_test/export"), 200)
    reset(client, combined_fixture())
    assert_ok(client.post("/_test/import", json=snapshot), 204)
    assert history_of(client, token, created["reference"]) == entries


def test_exported_terms_are_text_and_import_back_identically(client, diner):
    created = assert_ok(book(client, diner["token"], table_id="t_2", party_size=4), 201)
    state = export(client)
    stored = state["reservations"][0]["accepted_terms"]
    assert isinstance(stored, str), "terms are stored as the text they export as"
    assert json.loads(stored) == created["accepted_terms"]
    assert state["reservation_history"][0]["changes"]
    assert isinstance(state["reservation_history"][0]["changes"], str)
