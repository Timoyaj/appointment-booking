"""Idempotency (§7)."""

from __future__ import annotations

import json
import threading

import pytest

from tests.conftest import (
    THURSDAY,
    assert_ok,
    book,
    error_of,
    headers_for,
    signup,
)

BODY = {
    "restaurant_id": "r_anker",
    "table_id": "t_2",
    "starts_at_local": f"{THURSDAY}T19:00",
    "party_size": 4,
}


def post(client, token, key, body=None):
    return client.post("/reservations", json=body if body is not None else dict(BODY),
                       headers=headers_for(token, key))


def test_first_use_is_201_and_a_replay_is_200_with_the_same_body(client, seeded):
    token = signup(client)["token"]
    first = assert_ok(post(client, token, "key-1"), 201)
    replay = post(client, token, "key-1")

    assert replay.status_code == 200
    # Identical as a JSON value: key order and whitespace do not matter.
    assert replay.json() == first


def test_a_replay_is_not_a_second_booking(client, seeded):
    token = signup(client)["token"]
    for _ in range(4):
        post(client, token, "key-2")
    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)
    assert len(listed["reservations"]) == 1


def test_a_replay_survives_later_changes_to_the_resource(client, seeded):
    """The original response is returned even after amendment and cancellation."""
    token = signup(client)["token"]
    first = assert_ok(post(client, token, "key-3"), 201)

    assert_ok(client.patch(f"/reservations/{first['reference']}",
                           json={"starts_at_local": f"{THURSDAY}T20:30"},
                           headers=headers_for(token)), 200)
    assert post(client, token, "key-3").json() == first
    assert post(client, token, "key-3").status_code == 200

    assert_ok(client.post(f"/reservations/{first['reference']}/cancel",
                          headers=headers_for(token)), 200)
    replay = post(client, token, "key-3")
    assert replay.status_code == 200
    assert replay.json() == first
    assert replay.json()["status"] == "confirmed"  # the original receipt, not today's state


def test_a_replay_makes_no_further_state_change(client, seeded):
    token = signup(client)["token"]
    first = assert_ok(post(client, token, "key-4"), 201)
    before = client.get("/_test/export").json()["state"]

    for _ in range(3):
        assert post(client, token, "key-4").json() == first

    after = client.get("/_test/export").json()["state"]
    assert after["reservations"] == before["reservations"]
    assert after["idempotency"] == before["idempotency"]


def test_the_same_key_with_a_different_body_is_a_conflict(client, seeded):
    token = signup(client)["token"]
    assert_ok(post(client, token, "key-5"), 201)

    for variant in ({**BODY, "party_size": 2},
                    {**BODY, "starts_at_local": f"{THURSDAY}T20:30"},
                    {**BODY, "table_id": "t_1"},
                    {**BODY, "restaurant_id": "r_nope"}):
        response = post(client, token, "key-5", variant)
        assert response.status_code == 409, variant
        assert error_of(response)["code"] == "idempotency_key_reuse"


def test_key_reuse_is_checked_before_field_validation(client, seeded):
    """A used key with a different body is 409 even when that body is invalid."""
    token = signup(client)["token"]
    assert_ok(post(client, token, "key-6"), 201)

    for invalid in ({**BODY, "party_size": 0},
                    {**BODY, "party_size": "four"},
                    {**BODY, "starts_at_local": "not-a-time"},
                    {**BODY, "table_id": "t_nope"},
                    {"restaurant_id": "r_nope"}):
        response = post(client, token, "key-6", invalid)
        assert response.status_code == 409, invalid
        assert error_of(response)["code"] == "idempotency_key_reuse"


def test_key_order_and_whitespace_do_not_matter(client, seeded):
    """'Same body' means the same JSON value, so ordering is irrelevant."""
    token = signup(client)["token"]
    first = assert_ok(post(client, token, "key-7"), 201)
    reordered = {
        "party_size": 4,
        "starts_at_local": f"{THURSDAY}T19:00",
        "table_id": "t_2",
        "restaurant_id": "r_anker",
    }
    replay = post(client, token, "key-7", reordered)
    assert replay.status_code == 200
    assert replay.json() == first

    # A different JSON value is a different body, even if the extra field is one
    # the endpoint would otherwise ignore.
    changed = post(client, token, "key-7", {**reordered, "notes": "window please"})
    assert changed.status_code == 409
    assert error_of(changed)["code"] == "idempotency_key_reuse"


def test_the_same_key_on_a_different_path_is_a_different_request(client, seeded):
    token = signup(client)["token"]
    created = assert_ok(post(client, token, "shared-key", {**BODY, "party_size": 2}), 201)

    # Same key string, different path: must succeed normally, not 409.
    moved = client.post("/reservation-moves",
                        json={"moves": [{"reference": created["reference"],
                                        "table_id": "t_1"}]},
                        headers=headers_for(token, "shared-key"))
    assert moved.status_code == 201, moved.text
    assert moved.json()["reservations"][0]["table_id"] == "t_1"


def test_the_same_key_for_two_users_does_not_interact(client, seeded):
    first = signup(client, "one@example.com")["token"]
    second = signup(client, "two@example.com")["token"]

    mine = assert_ok(post(client, first, "same-key"), 201)
    # The same key, the same body, a different user: a first use for them. The
    # table is taken, so the honest answer is a conflict rather than a replay.
    theirs = post(client, second, "same-key")
    assert theirs.status_code == 409
    assert error_of(theirs)["code"] == "table_unavailable"

    theirs = assert_ok(post(client, second, "same-key",
                            {**BODY, "table_id": "t_1", "party_size": 2}), 201)
    assert theirs["reservation_id"] != mine["reservation_id"]
    assert post(client, first, "same-key").json() == mine


def test_a_key_is_reusable_after_the_original_request_failed(client, seeded):
    """Failed requests are not recorded, so the key is not burned."""
    token = signup(client)["token"]

    failed = post(client, token, "key-8", {**BODY, "table_id": "t_nope"})
    assert failed.status_code == 404

    # Same key, different (valid) body: a first use, not a reuse conflict.
    created = assert_ok(post(client, token, "key-8"), 201)
    assert created["table_id"] == "t_2"
    assert post(client, token, "key-8").json() == created


def test_a_key_is_reusable_after_a_conflict(client, seeded):
    token = signup(client)["token"]
    other = signup(client, "other@example.com")["token"]
    theirs = assert_ok(book(client, other, table_id="t_2",
                            starts_at_local=f"{THURSDAY}T19:00"), 201)

    blocked = post(client, token, "key-9")
    assert blocked.status_code == 409
    assert error_of(blocked)["code"] == "table_unavailable"

    assert_ok(client.post(f"/reservations/{theirs['reference']}/cancel",
                          headers=headers_for(other)), 200)
    created = assert_ok(post(client, token, "key-9"), 201)
    assert created["table_id"] == "t_2"
    assert post(client, token, "key-9").json() == created


def test_concurrent_identical_requests_produce_one_201(client, seeded):
    token = signup(client)["token"]
    workers = 16
    barrier = threading.Barrier(workers)
    outcomes: list = []
    lock = threading.Lock()

    def attempt(_index):
        barrier.wait()
        response = post(client, token, "race-key")
        with lock:
            outcomes.append(response)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    statuses = sorted(response.status_code for response in outcomes)
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) == workers - 1, statuses

    bodies = {json.dumps(response.json(), sort_keys=True) for response in outcomes}
    assert len(bodies) == 1, "every response must carry the same body"
    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)
    assert len(listed["reservations"]) == 1


@pytest.mark.parametrize("key", ["k", "a" * 255, "with spaces", "a/b?c=d",
                                 "9f2c1a3e-6b7d-4e8f", "key:with:colons"])
def test_any_key_string_in_range_is_accepted(client, seeded, key):
    token = signup(client)["token"]
    assert_ok(post(client, token, key), 201)


def test_moves_are_idempotent_too(client, seeded):
    token = signup(client)["token"]
    first = assert_ok(book(client, token, table_id="t_1", party_size=2,
                           starts_at_local=f"{THURSDAY}T19:00"), 201)
    second = assert_ok(book(client, token, table_id="t_2", party_size=2,
                            starts_at_local=f"{THURSDAY}T19:00"), 201)
    payload = {"moves": [{"reference": first["reference"], "table_id": "t_2"},
                         {"reference": second["reference"], "table_id": "t_1"}]}

    applied = assert_ok(client.post("/reservation-moves", json=payload,
                                    headers=headers_for(token, "move-key")), 201)
    replay = client.post("/reservation-moves", json=payload,
                         headers=headers_for(token, "move-key"))
    assert replay.status_code == 200
    assert replay.json() == applied

    # The swap happened once, not twice.
    now_first = assert_ok(client.get(f"/reservations/{first['reference']}",
                                     headers=headers_for(token)), 200)
    assert now_first["table_id"] == "t_2"

    conflicting = client.post("/reservation-moves",
                              json={"moves": [{"reference": first["reference"],
                                               "table_id": "t_1"}]},
                              headers=headers_for(token, "move-key"))
    assert conflicting.status_code == 409
    assert error_of(conflicting)["code"] == "idempotency_key_reuse"
