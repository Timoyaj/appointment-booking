"""Stage 3: recurring agreements.

Adopting a booking makes it occurrence zero of a series and generates the rest.
The generated occurrences are ordinary bookings in every respect — their own
reference, their own record, their own policy for their own date, and they occupy
their tables — which is why an adoption is all-or-nothing: the first occurrence
that cannot be placed refuses the whole thing and nothing survives it.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tablekeeper import clock

from .conftest import (
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

AT = f"{THURSDAY}T19:00"          # 2026-09-24, a Thursday
WEEK_LATER = "2026-10-01T19:00"
FORTNIGHT_LATER = "2026-10-08T19:00"


@pytest.fixture
def diner(client, seeded) -> dict:
    return {"token": signup(client)["token"]}


def adopt(client, token, anchor_reference, *, count=3, interval_weeks=1, key=None, **extra):
    body = {"anchor_reference": anchor_reference, "count": count,
            "interval_weeks": interval_weeks, **extra}
    return client.post("/series", json=body,
                       headers=headers_for(token, key or f"k_{uuid.uuid4().hex}"))


def anchor_booking(client, token, **kwargs) -> dict:
    return assert_ok(book(client, token, starts_at_local=kwargs.pop("at", AT), **kwargs), 201)


def series_of(client, token, series_id) -> dict:
    return assert_ok(client.get(f"/series/{series_id}", headers=headers_for(token)), 200)


def reservations_of(client, token) -> list[dict]:
    return assert_ok(client.get("/reservations", headers=headers_for(token)), 200)["reservations"]


def restaurant_revision(client) -> int:
    return int(assert_ok(client.get("/_test/export"), 200)["state"]["restaurants"][0]["revision"])


def sunday_fixture(opens="01:00", closes="05:00") -> dict:
    """A restaurant that serves the small hours, for the daylight-saving cases."""
    fixture = base_fixture()
    restaurant = fixture["restaurants"][0]
    restaurant["opening_hours"] = [{"weekday": "sun", "opens": opens, "closes": closes}]
    return fixture


# --------------------------------------------------------------------------- #
# adoption
# --------------------------------------------------------------------------- #
def test_adopting_returns_the_whole_agreement(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    body = assert_ok(adopt(client, diner["token"], anchor["reference"], count=3), 201)
    assert set(body) == {"series_id", "revision", "interval_weeks", "occurrences"}
    assert body["revision"] == 1
    assert body["interval_weeks"] == 1
    assert [o["index"] for o in body["occurrences"]] == [0, 1, 2]
    assert all(o["exception"] is False for o in body["occurrences"])


def test_occurrence_zero_is_the_anchor_itself(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    body = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    assert body["occurrences"][0]["reservation"] == anchor
    assert body["occurrences"][0]["reference"] == anchor["reference"]


def test_each_occurrence_has_its_own_reference(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    body = assert_ok(adopt(client, diner["token"], anchor["reference"], count=4), 201)
    references = [o["reference"] for o in body["occurrences"]]
    assert len(set(references)) == 4
    assert references[0] == anchor["reference"]


def test_occurrences_are_weeks_apart_at_the_same_local_time(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    body = assert_ok(adopt(client, diner["token"], anchor["reference"],
                           count=3, interval_weeks=1), 201)
    assert [o["reservation"]["starts_at_local"] for o in body["occurrences"]] == [
        AT, WEEK_LATER, FORTNIGHT_LATER]


def test_the_interval_is_honoured(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    body = assert_ok(adopt(client, diner["token"], anchor["reference"],
                           count=3, interval_weeks=2), 201)
    assert body["interval_weeks"] == 2
    assert [o["reservation"]["starts_at_local"] for o in body["occurrences"]] == [
        AT, FORTNIGHT_LATER, "2026-10-22T19:00"]


def test_generated_occurrences_inherit_the_party_and_the_tables(client, diner):
    fixture = base_fixture()
    fixture["restaurants"][0]["combinable"] = [["t_1", "t_2"]]
    fixture["restaurants"][0]["tables"].append({"id": "t_3", "label": "3", "capacity": 6})
    reset(client, fixture)
    token = signup(client, email="pair@example.com")["token"]
    anchor = assert_ok(client.post("/reservations", json={
        "restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
        "starts_at_local": AT, "party_size": 6}, headers=headers_for(token, "pair")), 201)
    body = assert_ok(adopt(client, token, anchor["reference"], count=2), 201)
    generated = body["occurrences"][1]["reservation"]
    assert generated["table_ids"] == ["t_1", "t_2"]
    assert generated["party_size"] == 6
    assert generated["restaurant_id"] == "r_anker"
    assert generated["status"] == "confirmed"


def test_occurrences_occupy_their_tables(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    other = signup(client, email="other@example.com")["token"]
    refused = book(client, other, table_id="t_2", party_size=4, starts_at_local=WEEK_LATER)
    assert error_of(refused)["code"] == "table_unavailable"
    # A different table on the same evening is untouched.
    assert_ok(book(client, other, table_id="t_1", party_size=2,
                   starts_at_local=WEEK_LATER), 201)


def test_occurrences_appear_in_the_ordinary_listing(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    assert_ok(adopt(client, diner["token"], anchor["reference"], count=3), 201)
    listed = reservations_of(client, diner["token"])
    assert len(listed) == 3
    assert {r["starts_at_local"] for r in listed} == {AT, WEEK_LATER, FORTNIGHT_LATER}


def test_each_occurrence_has_its_own_record(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    body = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    for occurrence in body["occurrences"]:
        entries = assert_ok(client.get(
            f"/reservations/{occurrence['reference']}/history",
            headers=headers_for(diner["token"])), 200)["entries"]
        assert [entry["event"] for entry in entries] == ["created"]
        assert entries[0]["revision"] == 1


def test_the_anchor_keeps_its_own_record_unchanged(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    before = assert_ok(client.get(f"/reservations/{anchor['reference']}/history",
                                  headers=headers_for(diner["token"])), 200)
    assert_ok(adopt(client, diner["token"], anchor["reference"], count=3), 201)
    after = assert_ok(client.get(f"/reservations/{anchor['reference']}/history",
                                 headers=headers_for(diner["token"])), 200)
    assert after == before


def test_adoption_counts_once_against_the_restaurant(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    assert restaurant_revision(client) == 1
    assert_ok(adopt(client, diner["token"], anchor["reference"], count=4), 201)
    assert restaurant_revision(client) == 2, "one increment for the whole adoption"


def test_unknown_fields_are_ignored(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    body = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2,
                           souvenir="yes"), 201)
    assert body["interval_weeks"] == 1


# --------------------------------------------------------------------------- #
# each occurrence selects its own policy
# --------------------------------------------------------------------------- #
def test_a_later_occurrence_meets_the_policy_of_its_own_date(client, seeded):
    reset(client, managed_fixture())
    manager = login_headers(client)["Authorization"].split(" ", 1)[1]
    guest = signup(client, email="guest@example.com")["token"]
    anchor = anchor_booking(client, guest, table_id="t_2", party_size=4)
    # A policy that only takes effect from the second occurrence's date onward.
    assert_ok(publish(client, manager, policy_body(
        effective_from="2026-10-01", reservation_duration_minutes=60)), 201)
    assert_ok(adopt(client, guest, anchor["reference"], count=3), 201)

    listed = {r["starts_at_local"]: r for r in reservations_of(client, guest)}
    assert listed[AT]["accepted_terms"]["policy_version"] == 0
    assert listed[AT]["ends_at"].endswith("20:30:00+02:00")
    assert listed[WEEK_LATER]["accepted_terms"]["policy_version"] == 1
    assert listed[WEEK_LATER]["accepted_terms"]["reservation_duration_minutes"] == 60
    assert listed[WEEK_LATER]["ends_at"].endswith("20:00:00+02:00")
    assert listed[FORTNIGHT_LATER]["accepted_terms"]["policy_version"] == 1


def test_a_generated_occurrence_is_refused_by_the_policy_of_its_own_date(client, seeded):
    reset(client, managed_fixture())
    manager = login_headers(client)["Authorization"].split(" ", 1)[1]
    guest = signup(client, email="guest@example.com")["token"]
    anchor = anchor_booking(client, guest, table_id="t_2", party_size=4)
    # From the second week the room is closed on Thursdays, so there is nothing to
    # place the second occurrence on.
    assert_ok(publish(client, manager, policy_body(
        effective_from="2026-10-01",
        opening_hours=[{"weekday": "fri", "opens": "18:00", "closes": "23:30"}])), 201)
    response = adopt(client, guest, anchor["reference"], count=2)
    assert error_of(response)["code"] == "outside_opening_hours"
    assert len(reservations_of(client, guest)) == 1, "nothing partial survived"


def test_a_policy_capacity_can_refuse_a_later_occurrence(client, seeded):
    reset(client, managed_fixture())
    manager = login_headers(client)["Authorization"].split(" ", 1)[1]
    guest = signup(client, email="guest@example.com")["token"]
    anchor = anchor_booking(client, guest, table_id="t_2", party_size=4)
    assert_ok(publish(client, manager, policy_body(
        effective_from="2026-10-01", capacities={"t_1": 2, "t_2": 2})), 201)
    response = adopt(client, guest, anchor["reference"], count=2)
    assert error_of(response)["code"] == "party_exceeds_capacity"
    assert len(reservations_of(client, guest)) == 1


# --------------------------------------------------------------------------- #
# daylight saving
# --------------------------------------------------------------------------- #
def test_a_time_that_does_not_exist_refuses_the_whole_adoption(client, seeded):
    """Berlin springs forward on 2026-03-29, so 02:30 does not exist that day."""
    clock.freeze(dt.datetime(2026, 3, 1, 12, 0, 0, tzinfo=dt.timezone.utc))
    reset(client, sunday_fixture())
    token = signup(client, email="dst@example.com")["token"]
    anchor = anchor_booking(client, token, table_id="t_2", party_size=4,
                            at="2026-03-22T02:30")
    response = adopt(client, token, anchor["reference"], count=2, interval_weeks=1)
    assert response.status_code == 422
    assert error_of(response)["code"] == "invalid_local_time"
    assert len(reservations_of(client, token)) == 1, "no partial series survived"
    assert restaurant_revision(client) == 1, "and no counter moved"


def test_a_repeated_local_time_uses_the_first_occurrence(client, seeded):
    """Berlin falls back on 2026-10-25, so 02:30 happens twice that day."""
    reset(client, sunday_fixture())
    token = signup(client, email="fold@example.com")["token"]
    anchor = anchor_booking(client, token, table_id="t_2", party_size=4,
                            at="2026-10-18T02:30")
    body = assert_ok(adopt(client, token, anchor["reference"], count=2), 201)
    generated = body["occurrences"][1]["reservation"]
    assert generated["starts_at_local"] == "2026-10-25T02:30"
    assert generated["starts_at"].endswith("+02:00"), \
        "the first of the two occurrences, as everywhere else in this service"


def test_a_series_can_cross_a_daylight_saving_change(client, seeded):
    """The clock time is kept; the offset is not."""
    reset(client, sunday_fixture(opens="01:00", closes="06:00"))
    token = signup(client, email="cross@example.com")["token"]
    anchor = anchor_booking(client, token, table_id="t_2", party_size=4,
                            at="2026-10-18T04:30")
    body = assert_ok(adopt(client, token, anchor["reference"], count=2), 201)
    first, second = (o["reservation"] for o in body["occurrences"])
    assert first["starts_at"].endswith("+02:00")
    assert second["starts_at"].endswith("+01:00")
    assert first["starts_at_local"][-5:] == second["starts_at_local"][-5:] == "04:30"


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #
def test_adoption_needs_a_token(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    response = client.post("/series", json={
        "anchor_reference": anchor["reference"], "count": 2, "interval_weeks": 1})
    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthenticated"


def test_adoption_needs_an_idempotency_key(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    response = client.post("/series", json={
        "anchor_reference": anchor["reference"], "count": 2, "interval_weeks": 1},
        headers=headers_for(diner["token"]))
    assert response.status_code == 400
    assert error_of(response)["code"] == "missing_idempotency_key"


@pytest.mark.parametrize("count", [1, 0, 13, -1, "3", True, False, 2.0, None, []])
def test_count_is_an_integer_between_two_and_twelve(client, diner, count):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    body = {"anchor_reference": anchor["reference"], "count": count, "interval_weeks": 1}
    if count is None:
        del body["count"]
    response = client.post("/series", json=body, headers=headers_for(diner["token"], "c"))
    assert response.status_code == 422, repr(count)
    assert error_of(response)["code"] == "validation_failed"


@pytest.mark.parametrize("weeks", [0, 5, -1, "1", True, 1.5, None])
def test_interval_weeks_is_an_integer_between_one_and_four(client, diner, weeks):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    body = {"anchor_reference": anchor["reference"], "count": 2, "interval_weeks": weeks}
    if weeks is None:
        del body["interval_weeks"]
    response = client.post("/series", json=body, headers=headers_for(diner["token"], "w"))
    assert response.status_code == 422, repr(weeks)
    assert error_of(response)["code"] == "validation_failed"


def test_the_bounds_are_inclusive(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_1", party_size=2)
    body = assert_ok(adopt(client, diner["token"], anchor["reference"],
                           count=12, interval_weeks=4), 201)
    assert len(body["occurrences"]) == 12
    # Twelve occurrences four weeks apart: eleven intervals of twenty-eight days.
    last = dt.date.fromisoformat(THURSDAY) + dt.timedelta(days=11 * 28)
    assert body["occurrences"][-1]["reservation"]["starts_at_local"] == \
        f"{last.isoformat()}T19:00"


def test_an_unknown_anchor_is_not_found(client, diner):
    response = adopt(client, diner["token"], "ZZZZZZZZ", count=2)
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_an_empty_anchor_reference_is_required(client, diner):
    response = adopt(client, diner["token"], "", count=2)
    assert error_of(response)["code"] == "validation_failed"


def test_another_diner_anchor_is_not_found(client, diner):
    mine = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    intruder = signup(client, email="intruder@example.com")["token"]
    response = adopt(client, intruder, mine["reference"], count=2)
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_a_missing_anchor_reference_is_required(client, diner):
    response = client.post("/series", json={"count": 2, "interval_weeks": 1},
                           headers=headers_for(diner["token"], "no-anchor"))
    assert error_of(response)["code"] == "validation_failed"


def test_an_anchor_reference_of_the_wrong_type_is_malformed(client, diner):
    response = client.post("/series", json={"anchor_reference": 7, "count": 2,
                                            "interval_weeks": 1},
                           headers=headers_for(diner["token"], "bad-anchor"))
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"


def test_a_cancelled_anchor_cannot_be_adopted(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    assert_ok(client.post(f"/reservations/{anchor['reference']}/cancel",
                          headers=headers_for(diner["token"])), 200)
    response = adopt(client, diner["token"], anchor["reference"], count=2)
    assert response.status_code == 409
    assert error_of(response)["code"] == "reservation_cancelled"


def test_an_anchor_past_its_accepted_cutoff_cannot_be_adopted(client, seeded):
    reset(client, managed_fixture())
    manager = login_headers(client)["Authorization"].split(" ", 1)[1]
    guest = signup(client, email="guest@example.com")["token"]
    assert_ok(publish(client, manager,
                      policy_body(cancellation_cutoff_minutes=10080)), 201)
    anchor = anchor_booking(client, guest, table_id="t_2", party_size=4)
    response = adopt(client, guest, anchor["reference"], count=2)
    assert error_of(response)["code"] == "cutoff_passed"


def test_a_booking_already_in_an_agreement_cannot_be_adopted_again(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    response = adopt(client, diner["token"], anchor["reference"], count=2)
    assert response.status_code == 409
    assert error_of(response)["code"] == "already_in_series"


def test_a_generated_occurrence_cannot_be_adopted_either(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    body = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    generated = body["occurrences"][1]["reference"]
    response = adopt(client, diner["token"], generated, count=2)
    assert error_of(response)["code"] == "already_in_series"


def test_an_ordinary_refusal_leaves_nothing_behind(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    other = signup(client, email="other@example.com")["token"]
    # Somebody else holds the table a fortnight out, so the third occurrence fails.
    blocker = assert_ok(book(client, other, table_id="t_2", party_size=4,
                             starts_at_local=FORTNIGHT_LATER), 201)
    counted = restaurant_revision(client)      # the anchor and the blocker
    response = adopt(client, diner["token"], anchor["reference"], count=3, key="once")
    assert error_of(response)["code"] == "table_unavailable"
    assert len(reservations_of(client, diner["token"])) == 1, "no partial series"
    assert restaurant_revision(client) == counted, "no counter moved"

    # A failed adoption claimed nothing, so the same key is still free — and with
    # the blocker gone the very same request now succeeds.
    assert_ok(client.post(f"/reservations/{blocker['reference']}/cancel",
                          headers=headers_for(other)), 200)
    retry = adopt(client, diner["token"], anchor["reference"], count=3, key="once")
    assert retry.status_code == 201, retry.text
    assert len(reservations_of(client, diner["token"])) == 3


# --------------------------------------------------------------------------- #
# reading an agreement
# --------------------------------------------------------------------------- #
def test_reading_an_agreement_reports_the_current_state(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    reference = created["occurrences"][1]["reference"]
    assert_ok(client.patch(f"/reservations/{reference}", json={"party_size": 3},
                           headers=headers_for(diner["token"])), 200)

    read = series_of(client, diner["token"], created["series_id"])
    assert read["series_id"] == created["series_id"]
    assert read["interval_weeks"] == created["interval_weeks"]
    assert [o["index"] for o in read["occurrences"]] == [0, 1]
    assert [o["reference"] for o in read["occurrences"]] == \
        [o["reference"] for o in created["occurrences"]], "indices and references never move"
    assert read["occurrences"][1]["reservation"]["party_size"] == 3


def test_another_diner_cannot_read_an_agreement(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    intruder = signup(client, email="intruder@example.com")["token"]
    response = client.get(f"/series/{created['series_id']}", headers=headers_for(intruder))
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_an_agreement_is_not_readable_without_a_token(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    for path in (f"/series/{created['series_id']}", "/series/ser_nope"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert error_of(response)["code"] == "not_found"


def test_an_unknown_agreement_is_not_found(client, diner):
    response = client.get("/series/ser_does-not-exist", headers=headers_for(diner["token"]))
    assert error_of(response)["code"] == "not_found"


# --------------------------------------------------------------------------- #
# changing one occurrence
# --------------------------------------------------------------------------- #
def test_a_real_change_makes_an_occurrence_an_exception(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    reference = created["occurrences"][1]["reference"]
    assert_ok(client.patch(f"/reservations/{reference}", json={"party_size": 3},
                           headers=headers_for(diner["token"])), 200)

    read = series_of(client, diner["token"], created["series_id"])
    assert read["revision"] == 2, "one real change, one increment"
    assert [o["exception"] for o in read["occurrences"]] == [False, True]


def test_the_anchor_can_be_an_exception_too(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    assert_ok(client.patch(f"/reservations/{anchor['reference']}", json={"party_size": 3},
                           headers=headers_for(diner["token"])), 200)
    read = series_of(client, diner["token"], created["series_id"])
    assert [o["exception"] for o in read["occurrences"]] == [True, False]
    assert read["revision"] == 2


def test_an_exception_is_permanent(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    reference = created["occurrences"][1]["reference"]
    token = diner["token"]
    assert_ok(client.patch(f"/reservations/{reference}", json={"party_size": 3},
                           headers=headers_for(token)), 200)
    # Putting the value back does not put the occurrence back into the pattern.
    assert_ok(client.patch(f"/reservations/{reference}", json={"party_size": 4},
                           headers=headers_for(token)), 200)
    read = series_of(client, token, created["series_id"])
    assert read["occurrences"][1]["exception"] is True
    assert read["revision"] == 3


def test_a_no_op_change_is_not_an_exception(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    reference = created["occurrences"][1]["reference"]
    assert_ok(client.patch(f"/reservations/{reference}", json={"party_size": 4},
                           headers=headers_for(diner["token"])), 200)
    read = series_of(client, diner["token"], created["series_id"])
    assert read["revision"] == 1
    assert read["occurrences"][1]["exception"] is False


def test_a_failed_change_is_not_an_exception(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    reference = created["occurrences"][1]["reference"]
    assert error_of(client.patch(f"/reservations/{reference}", json={"table_id": "t_nope"},
                                 headers=headers_for(diner["token"])))["code"] == "not_found"
    read = series_of(client, diner["token"], created["series_id"])
    assert read["revision"] == 1
    assert read["occurrences"][1]["exception"] is False


def test_cancelling_an_occurrence_counts_but_is_not_an_exception(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=3), 201)
    reference = created["occurrences"][1]["reference"]
    cancelled = assert_ok(client.post(f"/reservations/{reference}/cancel",
                                      headers=headers_for(diner["token"])), 200)
    assert cancelled["status"] == "cancelled"

    read = series_of(client, diner["token"], created["series_id"])
    assert read["revision"] == 2
    assert len(read["occurrences"]) == 3, "the cancelled occurrence is retained"
    assert [o["exception"] for o in read["occurrences"]] == [False, False, False]
    assert read["occurrences"][1]["reservation"]["status"] == "cancelled"


def test_cancelling_twice_counts_once(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    reference = created["occurrences"][1]["reference"]
    for _ in range(2):
        assert_ok(client.post(f"/reservations/{reference}/cancel",
                              headers=headers_for(diner["token"])), 200)
    assert series_of(client, diner["token"], created["series_id"])["revision"] == 2


def test_cancelling_the_anchor_leaves_its_siblings(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=3), 201)
    assert_ok(client.post(f"/reservations/{anchor['reference']}/cancel",
                          headers=headers_for(diner["token"])), 200)
    read = series_of(client, diner["token"], created["series_id"])
    assert [o["reservation"]["status"] for o in read["occurrences"]] == [
        "cancelled", "confirmed", "confirmed"]
    assert read["revision"] == 2
    # A sibling still holds its table.
    other = signup(client, email="other@example.com")["token"]
    assert error_of(book(client, other, table_id="t_2", party_size=4,
                         starts_at_local=WEEK_LATER))["code"] == "table_unavailable"
    # The anchor's table is free again on its own evening.
    assert_ok(book(client, other, table_id="t_2", party_size=4, starts_at_local=AT), 201)


def test_ordinary_revision_rules_still_apply_to_an_occurrence(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    reference = created["occurrences"][1]["reference"]
    response = client.patch(f"/reservations/{reference}",
                            json={"party_size": 3, "expected_revision": 99},
                            headers=headers_for(diner["token"]))
    assert error_of(response)["code"] == "stale_revision"
    assert series_of(client, diner["token"], created["series_id"])["revision"] == 1


def test_a_batch_of_moves_counts_once_per_agreement(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=3), 201)
    first = created["occurrences"][1]["reference"]
    second = created["occurrences"][2]["reference"]
    assert_ok(client.post("/reservation-moves", json={"moves": [
        {"reference": first, "party_size": 3},
        {"reference": second, "party_size": 2},
    ]}, headers=headers_for(diner["token"], "batch")), 201)

    read = series_of(client, diner["token"], created["series_id"])
    assert read["revision"] == 2, "one increment for the whole batch"
    assert [o["exception"] for o in read["occurrences"]] == [False, True, True]


def test_a_batch_that_changes_nothing_leaves_the_agreement_alone(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    reference = created["occurrences"][1]["reference"]
    assert_ok(client.post("/reservation-moves", json={"moves": [
        {"reference": reference, "party_size": 4}]},
        headers=headers_for(diner["token"], "no-op")), 201)
    read = series_of(client, diner["token"], created["series_id"])
    assert read["revision"] == 1
    assert read["occurrences"][1]["exception"] is False


def test_a_failed_batch_leaves_the_agreement_alone(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    reference = created["occurrences"][1]["reference"]
    response = client.post("/reservation-moves", json={"moves": [
        {"reference": reference, "party_size": 3},
        {"reference": reference, "table_id": "t_nope"},
    ]}, headers=headers_for(diner["token"], "bad-batch"))
    assert response.status_code >= 400
    read = series_of(client, diner["token"], created["series_id"])
    assert read["revision"] == 1
    assert read["occurrences"][1]["exception"] is False


# --------------------------------------------------------------------------- #
# replays, and agreements that arrive in a snapshot
# --------------------------------------------------------------------------- #
def test_a_replay_returns_the_original_agreement(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2,
                              key="one-key"), 201)
    reference = created["occurrences"][1]["reference"]
    assert_ok(client.patch(f"/reservations/{reference}", json={"party_size": 3},
                           headers=headers_for(diner["token"])), 200)

    replay = adopt(client, diner["token"], anchor["reference"], count=2, key="one-key")
    assert replay.status_code == 200
    assert replay.json() == created, "the receipt is the answer, however stale"
    read = series_of(client, diner["token"], created["series_id"])
    assert read["revision"] == 2, "a replay changed no counter"
    assert len(reservations_of(client, diner["token"])) == 2


def test_one_key_cannot_adopt_twice(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    assert_ok(adopt(client, diner["token"], anchor["reference"], count=2, key="one-key"), 201)
    other = anchor_booking(client, diner["token"], table_id="t_1", party_size=2,
                           at=WEEK_LATER)
    response = adopt(client, diner["token"], other["reference"], count=2, key="one-key")
    assert error_of(response)["code"] == "idempotency_key_reuse"


def test_an_agreement_survives_a_round_trip(client, diner):
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=3), 201)
    reference = created["occurrences"][1]["reference"]
    assert_ok(client.patch(f"/reservations/{reference}", json={"party_size": 3},
                           headers=headers_for(diner["token"])), 200)
    snapshot = assert_ok(client.get("/_test/export"), 200)
    assert snapshot["state"]["series"] and snapshot["state"]["series_occurrences"]

    reset(client, base_fixture())
    assert_ok(client.post("/_test/import", json=snapshot), 204)
    read = series_of(client, diner["token"], created["series_id"])
    assert read["series_id"] == created["series_id"]
    assert read["interval_weeks"] == created["interval_weeks"]
    assert read["revision"] == 2
    assert [o["reference"] for o in read["occurrences"]] == \
        [o["reference"] for o in created["occurrences"]]
    assert [o["exception"] for o in read["occurrences"]] == [False, True, False]
    assert read["occurrences"][1]["reservation"]["party_size"] == 3
    # And the agreement still works: one occurrence can still be changed.
    assert_ok(client.patch(
        f"/reservations/{read['occurrences'][2]['reference']}",
        json={"party_size": 2}, headers=headers_for(diner["token"])), 200)
    assert series_of(client, diner["token"], created["series_id"])["revision"] == 3


def test_a_booking_imported_from_an_earlier_stage_can_be_adopted(client, diner):
    """An agreement needs nothing an earlier snapshot could not have carried."""
    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    snapshot = assert_ok(client.get("/_test/export"), 200)
    for table in ("series", "series_occurrences", "reservation_history",
                  "restaurant_policies", "restaurant_managers"):
        snapshot["state"].pop(table, None)
    for record in snapshot["state"]["restaurants"]:
        record.pop("revision", None)
    for record in snapshot["state"]["reservations"]:
        record.pop("revision", None)
        record.pop("accepted_terms", None)

    reset(client, base_fixture())
    assert_ok(client.post("/_test/import", json=snapshot), 204)

    created = assert_ok(adopt(client, diner["token"], anchor["reference"], count=2), 201)
    assert created["occurrences"][0]["reservation"]["reference"] == anchor["reference"]
    assert created["occurrences"][1]["reservation"]["starts_at_local"] == WEEK_LATER
    assert len(reservations_of(client, diner["token"])) == 2


def test_concurrent_adoptions_of_one_anchor_produce_one_agreement(client, diner):
    import threading

    anchor = anchor_booking(client, diner["token"], table_id="t_2", party_size=4)
    outcomes: list = []
    barrier = threading.Barrier(20)

    def attempt(index: int) -> None:
        barrier.wait(timeout=30)
        outcomes.append(adopt(client, diner["token"], anchor["reference"], count=2,
                              key=f"race-{index}"))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    statuses = sorted(response.status_code for response in outcomes)
    assert all(status < 500 for status in statuses), statuses
    assert statuses.count(201) == 1, statuses
    assert set(statuses) - {201} == {409}, statuses
    agreed = [response.json()["series_id"] for response in outcomes
              if response.status_code == 201]
    assert len(agreed) == 1
