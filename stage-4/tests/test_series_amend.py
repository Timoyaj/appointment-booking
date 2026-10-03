"""Stage 4: amending a recurring agreement.

One request moves the clock time of every remaining occurrence of an agreement,
each on the date it was already scheduled for. Each occurrence that really moves
meets the same rules an individual amendment would have held it to — its own
accepted cutoff first, then the policy that applies to the date it lands on — and
the whole operation is one write: either every occurrence that can move does, or
nothing at all does.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid

import pytest

from tablekeeper import clock
from tablekeeper.tztime import WEEKDAYS

from .conftest import (
    THURSDAY,
    adopt,
    amend_series,
    apply_plan,
    assert_ok,
    closure_body,
    error_of,
    export,
    headers_for,
    plan_fixture,
    publish,
    replan,
    reset,
    seat,
    series_of,
    signup,
    three_table_policy_body,
    token_for,
)

AT_1900 = f"{THURSDAY}T19:00"
WEEK_LATER = "2026-10-01"
FORTNIGHT_LATER = "2026-10-08"
THREE_WEEKS = "2026-10-15"


@pytest.fixture
def room(client) -> dict:
    """The three-table managed room, loaded, with Ada's token."""
    fixture = plan_fixture()
    reset(client, fixture)
    return {"fixture": fixture, "token": token_for(client)}


def agreement(client, token, *, at=AT_1900, tables="t_2", party_size=4,
              count=3, interval_weeks=1) -> dict:
    """A booking, adopted as the anchor of a weekly agreement."""
    anchor = assert_ok(seat(client, token, tables, at=at, party_size=party_size), 201)
    return assert_ok(adopt(client, token, anchor["reference"], count=count,
                           interval_weeks=interval_weeks), 201)


def amend(client, token, series_id, *, revision, from_index, local_time, key=None,
          **extra):
    body = {"expected_revision": revision, "from_index": from_index,
            "local_time": local_time, **extra}
    return amend_series(client, token, series_id, body, key=key)


def amended(client, token, series_id, **kwargs) -> dict:
    return assert_ok(amend(client, token, series_id, **kwargs), 201)


def times_of(series: dict) -> list[str]:
    return [occurrence["reservation"]["starts_at_local"]
            for occurrence in series["occurrences"]]


def references_of(series: dict) -> list[str]:
    return [occurrence["reference"] for occurrence in series["occurrences"]]


def ledger_of(client, token, reference) -> list[dict]:
    return assert_ok(client.get(f"/reservations/{reference}/history",
                                headers=headers_for(token)), 200)["entries"]


def reservation_of(client, token, reference) -> dict:
    return assert_ok(client.get(f"/reservations/{reference}",
                                headers=headers_for(token)), 200)


def restaurant_revision(client) -> int:
    return export(client)["state"]["restaurants"][0]["revision"]


# --------------------------------------------------------------------------- #
# what one amendment does
# --------------------------------------------------------------------------- #
def test_the_clock_time_moves_on_the_dates_already_scheduled(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    after = amended(client, ada, made["series_id"], revision=1, from_index=1,
                    local_time="20:00")
    assert times_of(after) == [AT_1900, f"{WEEK_LATER}T20:00",
                               f"{FORTNIGHT_LATER}T20:00"]


def test_the_answer_is_the_agreement_as_it_now_reads(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    after = amended(client, ada, made["series_id"], revision=1, from_index=0,
                    local_time="21:30")
    assert set(after) == {"series_id", "revision", "interval_weeks", "occurrences"}
    assert after == series_of(client, ada, made["series_id"])
    assert times_of(after) == [f"{THURSDAY}T21:30", f"{WEEK_LATER}T21:30",
                               f"{FORTNIGHT_LATER}T21:30"]


def test_from_index_names_the_first_occurrence_it_reaches(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    after = amended(client, ada, made["series_id"], revision=1, from_index=2,
                    local_time="18:00")
    assert times_of(after) == [AT_1900, f"{WEEK_LATER}T19:00",
                               f"{FORTNIGHT_LATER}T18:00"]


def test_everything_but_the_clock_time_is_retained(client, room):
    ada = room["token"]
    made = agreement(client, ada, tables=["t_1", "t_2"], party_size=6)
    after = amended(client, ada, made["series_id"], revision=1, from_index=0,
                    local_time="20:00")
    assert references_of(after) == references_of(made)
    assert [occurrence["index"] for occurrence in after["occurrences"]] == [0, 1, 2]
    for occurrence in after["occurrences"]:
        reservation = occurrence["reservation"]
        assert reservation["table_ids"] == ["t_1", "t_2"]
        assert reservation["party_size"] == 6
        assert reservation["restaurant_id"] == "r_anker"
        assert reservation["status"] == "confirmed"
        assert reservation["created_at"] == made["occurrences"][0][
            "reservation"]["created_at"]


def test_each_real_change_gains_one_revision_and_one_entry(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    after = amended(client, ada, made["series_id"], revision=1, from_index=1,
                    local_time="20:00")

    untouched = after["occurrences"][0]["reservation"]
    assert untouched["revision"] == 1
    assert [entry["event"] for entry in
            ledger_of(client, ada, untouched["reference"])] == ["created"]

    for occurrence in after["occurrences"][1:]:
        reservation = occurrence["reservation"]
        assert reservation["revision"] == 2
        entries = ledger_of(client, ada, reservation["reference"])
        assert [entry["event"] for entry in entries] == ["created", "changed"]
        assert entries[1]["changes"] == [{
            "field": "starts_at_local",
            "from": reservation["starts_at_local"].replace("T20:00", "T19:00"),
            "to": reservation["starts_at_local"],
        }]
        assert entries[1]["revision"] == 2
        assert "plan_id" not in entries[1]


def test_the_agreement_and_the_restaurant_each_count_the_whole_operation_once(
        client, room):
    ada = room["token"]
    made = agreement(client, ada)
    before = restaurant_revision(client)
    after = amended(client, ada, made["series_id"], revision=1, from_index=0,
                    local_time="20:00")
    assert after["revision"] == made["revision"] + 1
    assert restaurant_revision(client) == before + 1


def test_an_amendment_marks_no_occurrence_as_an_exception(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    after = amended(client, ada, made["series_id"], revision=1, from_index=0,
                    local_time="20:00")
    assert [occurrence["exception"] for occurrence in after["occurrences"]] == \
        [False, False, False]
    # And the pattern still holds, so a second amendment moves all of them again.
    again = amended(client, ada, made["series_id"], revision=after["revision"],
                    from_index=0, local_time="21:00")
    assert times_of(again) == [f"{THURSDAY}T21:00", f"{WEEK_LATER}T21:00",
                               f"{FORTNIGHT_LATER}T21:00"]


def test_a_real_change_adopts_the_policy_for_the_date_it_lands_on(client, room):
    ada = room["token"]
    made = agreement(client, ada, tables="t_3", party_size=6)
    assert_ok(publish(client, ada, three_table_policy_body(
        WEEK_LATER, reservation_duration_minutes=60)), 201)

    after = amended(client, ada, made["series_id"], revision=1, from_index=0,
                    local_time="20:00")
    anchor = after["occurrences"][0]["reservation"]
    moved = after["occurrences"][1]["reservation"]
    # The anchor lands before the policy takes effect, so it keeps what it accepted.
    assert anchor["accepted_terms"]["policy_version"] == 0
    assert anchor["accepted_terms"]["reservation_duration_minutes"] == 90
    assert anchor["ends_at"] == f"{THURSDAY}T21:30:00+02:00"
    # The later occurrences land on and after that date, and take the new sitting
    # length with the terms that go with it.
    assert moved["accepted_terms"]["policy_version"] == 1
    assert moved["accepted_terms"]["reservation_duration_minutes"] == 60
    assert moved["starts_at_local"] == f"{WEEK_LATER}T20:00"
    assert moved["ends_at"] == f"{WEEK_LATER}T21:00:00+02:00"


def test_an_agreement_keeps_its_clock_time_across_a_clock_change(client, room):
    """Berlin goes back to +01:00 on 2026-10-25: the wall clock the diner asked for
    is what every occurrence keeps, and the offset follows the calendar."""
    ada = room["token"]
    made = agreement(client, ada, at=f"{WEEK_LATER}T19:00", count=5)
    after = amended(client, ada, made["series_id"], revision=1, from_index=0,
                    local_time="20:00")
    before_change = after["occurrences"][3]["reservation"]
    assert before_change["starts_at_local"] == "2026-10-22T20:00"
    assert before_change["starts_at"].endswith("+02:00")
    after_change = after["occurrences"][4]["reservation"]
    assert after_change["starts_at_local"] == "2026-10-29T20:00"
    assert after_change["starts_at"].endswith("+01:00")
    assert after_change["starts_at"] == "2026-10-29T20:00:00+01:00"


# --------------------------------------------------------------------------- #
# occurrences an amendment leaves alone
# --------------------------------------------------------------------------- #
def test_a_cancelled_occurrence_is_not_rescheduled(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    cancelled = made["occurrences"][1]["reference"]
    assert_ok(client.post(f"/reservations/{cancelled}/cancel", json={},
                          headers=headers_for(ada)), 200)

    after = amended(client, ada, made["series_id"], revision=2, from_index=0,
                    local_time="20:00")
    assert times_of(after) == [f"{THURSDAY}T20:00", f"{WEEK_LATER}T19:00",
                               f"{FORTNIGHT_LATER}T20:00"]
    assert after["occurrences"][1]["reservation"]["status"] == "cancelled"
    assert after["occurrences"][1]["reservation"]["revision"] == 2  # cancellation
    assert [entry["event"] for entry in ledger_of(client, ada, cancelled)] == \
        ["created", "cancelled"]


def test_an_occurrence_taken_out_of_the_pattern_is_left_where_the_diner_put_it(
        client, room):
    ada = room["token"]
    made = agreement(client, ada)
    second = made["occurrences"][1]["reference"]
    assert_ok(client.patch(f"/reservations/{second}",
                           json={"starts_at_local": f"{WEEK_LATER}T21:30",
                                 "expected_revision": 1},
                           headers=headers_for(ada)), 200)

    after = amended(client, ada, made["series_id"], revision=2, from_index=0,
                    local_time="20:00")
    assert times_of(after) == [f"{THURSDAY}T20:00", f"{WEEK_LATER}T21:30",
                               f"{FORTNIGHT_LATER}T20:00"]
    assert after["occurrences"][1]["exception"] is True


def test_an_amendment_that_would_change_nothing_changes_nothing(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    before = export(client)["state"]
    after = amended(client, ada, made["series_id"], revision=1, from_index=0,
                    local_time="19:00")

    assert after["revision"] == made["revision"]
    assert times_of(after) == times_of(made)
    assert restaurant_revision(client) == before["restaurants"][0]["revision"]
    for occurrence in after["occurrences"]:
        assert occurrence["reservation"]["revision"] == 1
        assert [entry["event"] for entry in
                ledger_of(client, ada, occurrence["reference"])] == ["created"]


def test_a_no_op_keeps_the_terms_it_already_had(client, room):
    """A sitting that does not move is not re-decided, so a policy published since
    it was booked does not reach it — and neither does its cutoff."""
    ada = room["token"]
    made = agreement(client, ada)
    assert_ok(publish(client, ada, three_table_policy_body(
        THURSDAY, reservation_duration_minutes=60,
        cancellation_cutoff_minutes=0)), 201)
    after = amended(client, ada, made["series_id"], revision=1, from_index=0,
                    local_time="19:00")
    assert after["revision"] == 1
    for occurrence in after["occurrences"]:
        assert occurrence["reservation"]["accepted_terms"]["policy_version"] == 0
        assert occurrence["reservation"]["accepted_terms"][
            "reservation_duration_minutes"] == 90


def test_a_no_op_is_not_refused_by_a_cutoff_that_has_passed(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    # Inside the second occurrence's cutoff: it can no longer be changed.
    clock.freeze(dt.datetime(2026, 10, 1, 15, 30, tzinfo=dt.timezone.utc))
    after = amended(client, ada, made["series_id"], revision=1, from_index=1,
                    local_time="19:00")
    assert after["revision"] == 1
    assert times_of(after) == times_of(made)


def test_an_agreement_with_nothing_left_to_amend_succeeds(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    last = made["occurrences"][2]["reference"]
    assert_ok(client.post(f"/reservations/{last}/cancel", json={},
                          headers=headers_for(ada)), 200)
    before = restaurant_revision(client)

    after = amended(client, ada, made["series_id"], revision=2, from_index=2,
                    local_time="20:00")
    assert after["revision"] == 2
    assert restaurant_revision(client) == before
    assert after["occurrences"][2]["reservation"]["status"] == "cancelled"


def test_one_amendment_can_be_a_no_op_and_another_a_real_change(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    first = amended(client, ada, made["series_id"], revision=1, from_index=2,
                    local_time="20:00")
    assert times_of(first) == [AT_1900, f"{WEEK_LATER}T19:00",
                               f"{FORTNIGHT_LATER}T20:00"]

    # The last occurrence is already at 20:00, so it is a no-op inside an operation
    # that really moves the one before it: the operation counts once, and the
    # occurrence that did not move gains neither a revision nor an entry.
    second = amended(client, ada, made["series_id"], revision=first["revision"],
                     from_index=1, local_time="20:00")
    assert second["revision"] == first["revision"] + 1
    assert times_of(second) == [AT_1900, f"{WEEK_LATER}T20:00",
                                f"{FORTNIGHT_LATER}T20:00"]
    moved, already = second["occurrences"][1], second["occurrences"][2]
    assert moved["reservation"]["revision"] == 2
    assert [entry["event"] for entry in
            ledger_of(client, ada, moved["reference"])] == ["created", "changed"]
    assert already["reservation"]["revision"] == 2
    assert [entry["event"] for entry in
            ledger_of(client, ada, already["reference"])] == ["created", "changed"]
    assert second["occurrences"][0]["reservation"]["revision"] == 1


# --------------------------------------------------------------------------- #
# who may amend, and which agreement
# --------------------------------------------------------------------------- #
def test_another_diner_is_told_the_agreement_does_not_exist(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    other = signup(client, email="bob@example.com", display_name="Bob")["token"]
    response = amend(client, other, made["series_id"], revision=1, from_index=0,
                     local_time="20:00")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_amending_needs_a_token(client, room):
    made = agreement(client, room["token"])
    response = client.post(f"/series/{made['series_id']}/amend",
                           json={"expected_revision": 1, "from_index": 0,
                                 "local_time": "20:00"},
                           headers={"Idempotency-Key": "k_1"})
    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthenticated"


def test_amending_needs_a_key(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    response = client.post(f"/series/{made['series_id']}/amend",
                           json={"expected_revision": 1, "from_index": 0,
                                 "local_time": "20:00"},
                           headers=headers_for(ada))
    assert response.status_code == 400
    assert error_of(response)["code"] == "missing_idempotency_key"


def test_an_unknown_agreement_is_not_found(client, room):
    response = amend(client, room["token"], "ser_nonesuch", revision=1,
                     from_index=0, local_time="20:00")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


# --------------------------------------------------------------------------- #
# the body is validated before anything is judged
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [
    {"expected_revision": 1, "from_index": 0},
    {"expected_revision": 1, "local_time": "20:00"},
    {"from_index": 0, "local_time": "20:00"},
    {},
    {"expected_revision": 0, "from_index": 0, "local_time": "20:00"},
    {"expected_revision": -1, "from_index": 0, "local_time": "20:00"},
    {"expected_revision": True, "from_index": 0, "local_time": "20:00"},
    {"expected_revision": "1", "from_index": 0, "local_time": "20:00"},
    {"expected_revision": 1.0, "from_index": 0, "local_time": "20:00"},
    {"expected_revision": None, "from_index": 0, "local_time": "20:00"},
    {"expected_revision": 1, "from_index": 3, "local_time": "20:00"},
    {"expected_revision": 1, "from_index": -1, "local_time": "20:00"},
    {"expected_revision": 1, "from_index": True, "local_time": "20:00"},
    {"expected_revision": 1, "from_index": "0", "local_time": "20:00"},
    {"expected_revision": 1, "from_index": None, "local_time": "20:00"},
    {"expected_revision": 1, "from_index": 0, "local_time": "24:00"},
    {"expected_revision": 1, "from_index": 0, "local_time": "7:00"},
    {"expected_revision": 1, "from_index": 0, "local_time": "20:00:00"},
    {"expected_revision": 1, "from_index": 0, "local_time": "2000"},
    {"expected_revision": 1, "from_index": 0, "local_time": ""},
    {"expected_revision": 1, "from_index": 0, "local_time": "8pm"},
    {"expected_revision": 1, "from_index": 0, "local_time": "20:60"},
    {"expected_revision": 1, "from_index": 0, "local_time": f"{THURSDAY}T20:00"},
    {"expected_revision": 1, "from_index": 0, "local_time": "20:00+02:00"},
])
def test_an_invalid_amendment_is_refused(client, room, body):
    ada = room["token"]
    made = agreement(client, ada)
    before = export(client)["state"]
    response = amend_series(client, ada, made["series_id"], body)
    assert response.status_code == 422, body
    assert error_of(response)["code"] == "validation_failed"
    # Nothing was judged, so nothing changed and no receipt was taken.
    assert export(client)["state"] == before


def test_a_local_time_of_the_wrong_json_type_is_malformed(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    response = amend_series(client, ada, made["series_id"], {
        "expected_revision": 1, "from_index": 0, "local_time": 20})
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"


@pytest.mark.parametrize("extra", [{"note": "later is better"}, {"from_index ": 2}])
def test_unknown_fields_are_ignored(client, room, extra):
    ada = room["token"]
    made = agreement(client, ada)
    after = amended(client, ada, made["series_id"], revision=1, from_index=0,
                    local_time="20:00", **extra)
    assert times_of(after) == [f"{THURSDAY}T20:00", f"{WEEK_LATER}T20:00",
                               f"{FORTNIGHT_LATER}T20:00"]


def test_from_index_is_bounded_by_this_agreement(client, room):
    ada = room["token"]
    made = agreement(client, ada, count=2)
    assert amend(client, ada, made["series_id"], revision=1, from_index=2,
                 local_time="20:00").status_code == 422
    assert amend(client, ada, made["series_id"], revision=1, from_index=1,
                 local_time="20:00").status_code == 201


# --------------------------------------------------------------------------- #
# the revision the diner expected
# --------------------------------------------------------------------------- #
def test_an_amendment_from_a_revision_the_agreement_has_left_is_refused(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    amended(client, ada, made["series_id"], revision=1, from_index=0, local_time="20:00")
    response = amend(client, ada, made["series_id"], revision=1, from_index=0,
                     local_time="21:00")
    assert response.status_code == 409
    assert error_of(response)["code"] == "stale_revision"


def test_the_revision_is_judged_before_any_occurrence_is(client, room):
    """A stale request is refused as stale, even when every occurrence it names is
    also past its cutoff: the diner is not told about a change they did not make."""
    ada = room["token"]
    made = agreement(client, ada)
    clock.freeze(dt.datetime(2026, 10, 1, 15, 30, tzinfo=dt.timezone.utc))
    response = amend(client, ada, made["series_id"], revision=99, from_index=1,
                     local_time="20:00")
    assert response.status_code == 409
    assert error_of(response)["code"] == "stale_revision"


def test_a_stale_amendment_changes_nothing(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    amended(client, ada, made["series_id"], revision=1, from_index=0, local_time="20:00")
    before = export(client)["state"]
    assert amend(client, ada, made["series_id"], revision=1, from_index=0,
                 local_time="21:00").status_code == 409
    assert export(client)["state"] == before


def test_a_repair_leaves_the_agreement_at_a_new_revision(client, room):
    """A plan that moved one occurrence counted once there, so the next amendment
    has to expect that revision."""
    ada = room["token"]
    made = agreement(client, ada)
    plan = assert_ok(replan(client, ada, closure_body("t_2", THURSDAY)), 201)
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)

    stale = amend(client, ada, made["series_id"], revision=1, from_index=0,
                  local_time="20:00")
    assert stale.status_code == 409
    after = amended(client, ada, made["series_id"], revision=2, from_index=0,
                    local_time="20:00")
    assert after["revision"] == 3
    # The repaired occurrence kept the table the plan gave it.
    assert after["occurrences"][0]["reservation"]["table_ids"] == ["t_3"]
    assert after["occurrences"][1]["reservation"]["table_ids"] == ["t_2"]


def test_concurrent_amendments_from_one_revision_change_the_agreement_once(
        client, room):
    ada = room["token"]
    made = agreement(client, ada)
    outcomes: list = []
    barrier = threading.Barrier(20)

    def attempt(index: int) -> None:
        barrier.wait(timeout=30)
        outcomes.append(amend(client, ada, made["series_id"], revision=1,
                              from_index=0, local_time="20:00", key=f"race-{index}"))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    statuses = sorted(response.status_code for response in outcomes)
    assert all(status < 500 for status in statuses), statuses
    assert statuses.count(201) == 1, statuses
    assert set(statuses) - {201} == {409}, statuses
    after = series_of(client, ada, made["series_id"])
    assert after["revision"] == 2
    assert times_of(after) == [f"{THURSDAY}T20:00", f"{WEEK_LATER}T20:00",
                               f"{FORTNIGHT_LATER}T20:00"]
    for occurrence in after["occurrences"]:
        assert occurrence["reservation"]["revision"] == 2
        assert len(ledger_of(client, ada, occurrence["reference"])) == 2


# --------------------------------------------------------------------------- #
# the cutoff each occurrence accepted
# --------------------------------------------------------------------------- #
def test_a_real_change_meets_the_cutoff_of_the_occurrence_it_moves(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    clock.freeze(dt.datetime(2026, 10, 1, 15, 30, tzinfo=dt.timezone.utc))
    response = amend(client, ada, made["series_id"], revision=1, from_index=1,
                     local_time="20:00")
    assert response.status_code == 409
    assert error_of(response)["code"] == "cutoff_passed"
    # Nothing moved: not the occurrence that was refused, not the one after it.
    assert times_of(series_of(client, ada, made["series_id"])) == times_of(made)
    assert all(occurrence["reservation"]["revision"] == 1
               for occurrence in series_of(client, ada, made["series_id"])["occurrences"])


def test_an_occurrence_past_its_cutoff_does_not_stop_an_earlier_one(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    # Late on the second occurrence's evening, but well before the third's.
    clock.freeze(dt.datetime(2026, 10, 1, 20, 0, tzinfo=dt.timezone.utc))
    response = amend(client, ada, made["series_id"], revision=1, from_index=1,
                     local_time="20:00")
    assert response.status_code == 409
    # Amending only the third, though, is still in time.
    after = amended(client, ada, made["series_id"], revision=1, from_index=2,
                    local_time="20:00")
    assert times_of(after) == [AT_1900, f"{WEEK_LATER}T19:00",
                               f"{FORTNIGHT_LATER}T20:00"]


def test_the_cutoff_is_the_one_the_occurrence_accepted(client, room):
    """A policy published afterwards does not shorten an agreement already made,
    and one that loosens the cutoff is what the occurrence is judged by."""
    ada = room["token"]
    made = agreement(client, ada, tables="t_3", party_size=6)
    assert_ok(publish(client, ada, three_table_policy_body(
        WEEK_LATER, cancellation_cutoff_minutes=0)), 201)
    # The second occurrence accepted policy 1's zero cutoff when it was made... it
    # did not: it was made before the policy existed, so it keeps its two hours.
    clock.freeze(dt.datetime(2026, 10, 1, 16, 30, tzinfo=dt.timezone.utc))
    response = amend(client, ada, made["series_id"], revision=1, from_index=1,
                     local_time="20:00")
    assert response.status_code == 409
    assert error_of(response)["code"] == "cutoff_passed"

    # An occurrence booked *under* the looser policy is judged by it.
    later = agreement(client, ada, at=f"{THREE_WEEKS}T19:00", tables="t_3",
                      party_size=6, count=2)
    assert later["occurrences"][0]["reservation"]["accepted_terms"][
        "cancellation_cutoff_minutes"] == 0
    after = amended(client, ada, later["series_id"], revision=1, from_index=0,
                    local_time="20:00")
    assert times_of(after) == [f"{THREE_WEEKS}T20:00", "2026-10-22T20:00"]


# --------------------------------------------------------------------------- #
# the resulting sittings have to fit the room
# --------------------------------------------------------------------------- #
def test_a_time_outside_service_is_refused(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    response = amend(client, ada, made["series_id"], revision=1, from_index=0,
                     local_time="17:00")
    assert response.status_code == 422
    assert error_of(response)["code"] == "outside_opening_hours"


def test_a_time_off_the_slot_grid_is_refused(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    response = amend(client, ada, made["series_id"], revision=1, from_index=0,
                     local_time="19:15")
    assert response.status_code == 422
    assert error_of(response)["code"] == "not_on_slot_grid"


def test_a_time_that_does_not_exist_on_that_date_is_refused(client):
    """Berlin springs forward on 2026-03-29, so 02:30 never happens that day."""
    clock.freeze(dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.timezone.utc))
    reset(client, plan_fixture(opening_hours=[
        {"weekday": day, "opens": "01:00", "closes": "05:00"} for day in WEEKDAYS]))
    ada = token_for(client)
    anchor = assert_ok(seat(client, ada, "t_3", at="2026-03-22T03:30",
                            party_size=4), 201)
    made = assert_ok(adopt(client, ada, anchor["reference"], count=2), 201)
    assert times_of(series_of(client, ada, made["series_id"])) == [
        "2026-03-22T03:30", "2026-03-29T03:30"]

    response = amend(client, ada, made["series_id"], revision=1, from_index=1,
                     local_time="02:30")
    assert response.status_code == 422
    assert error_of(response)["code"] == "invalid_local_time"
    assert times_of(series_of(client, ada, made["series_id"])) == [
        "2026-03-22T03:30", "2026-03-29T03:30"]

    # An hour that does exist that day is accepted, new offset and all.
    after = amended(client, ada, made["series_id"], revision=1, from_index=1,
                    local_time="03:00")
    moved = after["occurrences"][1]["reservation"]
    assert moved["starts_at_local"] == "2026-03-29T03:00"
    assert moved["starts_at"] == "2026-03-29T03:00:00+02:00"


def test_a_party_that_no_longer_fits_the_table_it_holds_is_refused(client, room):
    """The resulting sitting is judged by the policy for its date, capacities and
    all — and the tables an amendment retains are the ones it measures."""
    ada = room["token"]
    made = agreement(client, ada)
    assert_ok(publish(client, ada, three_table_policy_body(
        WEEK_LATER, capacities={"t_1": 2, "t_2": 2, "t_3": 6})), 201)
    response = amend(client, ada, made["series_id"], revision=1, from_index=1,
                     local_time="20:00")
    assert response.status_code == 422
    assert error_of(response)["code"] == "party_exceeds_capacity"


def test_a_non_occupancy_problem_wins_over_a_clash_whatever_its_index(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    # Somebody else takes the second occurrence's new table and time...
    other = signup(client, email="bob@example.com", display_name="Bob")["token"]
    assert_ok(seat(client, other, "t_2", at=f"{WEEK_LATER}T20:30", party_size=4), 201)
    # ...and a policy makes 20:00 off the grid for the third occurrence's date.
    assert_ok(publish(client, ada, three_table_policy_body(
        FORTNIGHT_LATER, slot_minutes=45)), 201)

    response = amend(client, ada, made["series_id"], revision=1, from_index=1,
                     local_time="20:00")
    assert response.status_code == 422, response.text
    assert error_of(response)["code"] == "not_on_slot_grid"


def test_occupancy_problems_are_reported_in_occurrence_order(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    other = signup(client, email="bob@example.com", display_name="Bob")["token"]
    assert_ok(seat(client, other, "t_2", at=f"{FORTNIGHT_LATER}T20:30",
                   party_size=4), 201)
    assert_ok(seat(client, other, "t_2", at=f"{WEEK_LATER}T20:30", party_size=4), 201)
    response = amend(client, ada, made["series_id"], revision=1, from_index=1,
                     local_time="20:00")
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"
    assert times_of(series_of(client, ada, made["series_id"])) == times_of(made)


def test_an_amendment_may_not_walk_into_an_untouched_occurrence(client, room):
    """An occurrence a diner took out of the pattern stays where they put it, and
    the rest of the agreement may not be moved on top of it."""
    ada = room["token"]
    made = agreement(client, ada, tables="t_3", party_size=6)
    second = made["occurrences"][1]["reference"]
    assert_ok(client.patch(f"/reservations/{second}",
                           json={"starts_at_local": f"{FORTNIGHT_LATER}T21:00",
                                 "expected_revision": 1},
                           headers=headers_for(ada)), 200)
    response = amend(client, ada, made["series_id"], revision=2, from_index=2,
                     local_time="21:00")
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"


def test_an_amendment_may_not_walk_into_a_closed_table(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    # The closure starts exactly when the second occurrence's sitting ends, so the
    # plan has nothing to move — but the table is out of service from 20:30.
    plan = assert_ok(replan(client, ada, closure_body(
        "t_2", WEEK_LATER, opens="20:30", closes="22:00")), 201)
    assert plan["assignments"] == []
    assert_ok(apply_plan(client, ada, plan["plan_id"]), 201)
    assert series_of(client, ada, made["series_id"])["revision"] == made["revision"]

    response = amend(client, ada, made["series_id"], revision=1, from_index=1,
                     local_time="20:00")
    assert response.status_code == 409
    assert error_of(response)["code"] == "table_unavailable"
    assert times_of(series_of(client, ada, made["series_id"])) == times_of(made)


def test_a_refused_amendment_leaves_no_trace(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    other = signup(client, email="bob@example.com", display_name="Bob")["token"]
    assert_ok(seat(client, other, "t_2", at=f"{WEEK_LATER}T20:30", party_size=4), 201)
    before = export(client)["state"]

    response = amend(client, ada, made["series_id"], revision=1, from_index=1,
                     local_time="20:00")
    assert response.status_code == 409
    # No history, no receipt, no revision — anywhere in the agreement.
    assert export(client)["state"] == before


# --------------------------------------------------------------------------- #
# one request, one receipt
# --------------------------------------------------------------------------- #
def test_an_amendment_is_idempotent_even_after_the_agreement_moves_on(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    key = f"k_{uuid.uuid4().hex}"
    first = assert_ok(amend(client, ada, made["series_id"], revision=1,
                            from_index=0, local_time="20:00", key=key), 201)
    amended(client, ada, made["series_id"], revision=2, from_index=0,
            local_time="21:00")
    replay = assert_ok(amend(client, ada, made["series_id"], revision=1,
                             from_index=0, local_time="20:00", key=key), 200)
    assert replay == first
    assert times_of(series_of(client, ada, made["series_id"])) == [
        f"{THURSDAY}T21:00", f"{WEEK_LATER}T21:00", f"{FORTNIGHT_LATER}T21:00"]


def test_an_amendment_key_may_not_be_spent_on_a_different_time(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    key = f"k_{uuid.uuid4().hex}"
    assert_ok(amend(client, ada, made["series_id"], revision=1, from_index=0,
                    local_time="20:00", key=key), 201)
    response = amend(client, ada, made["series_id"], revision=1, from_index=0,
                     local_time="21:00", key=key)
    assert response.status_code == 409
    assert error_of(response)["code"] == "idempotency_key_reuse"


def test_a_refused_amendment_leaves_its_key_unspent(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    key = f"k_{uuid.uuid4().hex}"
    assert amend(client, ada, made["series_id"], revision=99, from_index=0,
                 local_time="20:00", key=key).status_code == 409
    assert_ok(amend(client, ada, made["series_id"], revision=1, from_index=0,
                    local_time="20:00", key=key), 201)


def test_an_imported_agreement_can_still_be_amended(client, room):
    ada = room["token"]
    made = agreement(client, ada)
    snapshot = export(client)
    state = snapshot["state"]
    for table in ("replans", "replan_assignments", "table_closures"):
        state.pop(table, None)
    for entry in state["reservation_history"]:
        entry.pop("plan_id", None)

    reset(client, plan_fixture())
    assert_ok(client.post("/_test/import", json=snapshot), 204)
    after = amended(client, ada, made["series_id"], revision=1, from_index=1,
                    local_time="20:00")
    assert times_of(after) == [AT_1900, f"{WEEK_LATER}T20:00",
                               f"{FORTNIGHT_LATER}T20:00"]
    assert after["revision"] == 2
