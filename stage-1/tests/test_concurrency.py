"""Concurrency: up to 50 requests in flight, no 5xx, no double bookings."""

from __future__ import annotations

import threading

from tests.conftest import (
    THURSDAY,
    assert_ok,
    base_fixture,
    book,
    headers_for,
    reset,
    signup,
)


def race(client, workers, target):
    """Run ``workers`` copies of ``target(index)`` released at the same instant."""
    barrier = threading.Barrier(workers)
    outcomes: list = []
    lock = threading.Lock()

    def run(index):
        barrier.wait()
        result = target(index)
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a worker thread hung"
    return outcomes


def assert_no_5xx(responses):
    for response in responses:
        assert response.status_code < 500, response.text


def test_fifty_racing_bookings_produce_exactly_one_winner(client, seeded):
    token = signup(client)["token"]
    workers = 50

    def attempt(index):
        return client.post("/reservations", json={
            "restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": f"{THURSDAY}T19:00", "party_size": 4,
        }, headers=headers_for(token, f"race-{index}"))

    responses = race(client, workers, attempt)
    assert_no_5xx(responses)

    statuses = sorted(response.status_code for response in responses)
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == workers - 1, statuses

    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)
    assert len(listed["reservations"]) == 1


def test_fifty_concurrent_retries_of_one_key_create_one_booking(client, seeded):
    token = signup(client)["token"]

    def attempt(_index):
        return client.post("/reservations", json={
            "restaurant_id": "r_anker", "table_id": "t_1",
            "starts_at_local": f"{THURSDAY}T19:00", "party_size": 2,
        }, headers=headers_for(token, "one-key"))

    responses = race(client, 50, attempt)
    assert_no_5xx(responses)
    assert all(response.status_code in (200, 201) for response in responses)
    assert sum(1 for r in responses if r.status_code == 201) == 1
    assert len({tuple(sorted(r.json().items())) for r in responses}) == 1

    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)
    assert len(listed["reservations"]) == 1


def test_fifty_bookings_for_distinct_slots_all_succeed(client):
    """Serialisation must not turn into false rejections."""
    fixture = base_fixture()
    fixture["restaurants"][0]["tables"] = [
        {"id": f"t_{index}", "label": str(index), "capacity": 4} for index in range(10)
    ]
    fixture["restaurants"][0]["opening_hours"] = [
        {"weekday": "thu", "opens": "08:00", "closes": "23:00"}
    ]
    reset(client, fixture)
    token = signup(client)["token"]

    # 10 tables x 5 back-to-back 90-minute sittings = 50 distinct slots.
    slots = [
        (f"t_{table}", f"{THURSDAY}T{hour:02d}:{minute:02d}")
        for table in range(10)
        for hour, minute in ((8, 0), (9, 30), (11, 0), (12, 30), (14, 0))
    ]
    assert len(slots) == 50

    def attempt(index):
        table_id, starts_at_local = slots[index]
        return client.post("/reservations", json={
            "restaurant_id": "r_anker", "table_id": table_id,
            "starts_at_local": starts_at_local, "party_size": 4,
        }, headers=headers_for(token, f"slot-{index}"))

    responses = race(client, 50, attempt)
    assert_no_5xx(responses)
    assert all(response.status_code == 201 for response in responses), [
        (r.status_code, r.text) for r in responses if r.status_code != 201]
    assert len({r.json()["reference"] for r in responses}) == 50


def test_concurrent_moves_onto_one_slot_leave_one_winner(client):
    """Eight managers race to move eight different bookings onto one free slot."""
    fixture = base_fixture()
    fixture["restaurants"][0]["opening_hours"] = [
        {"weekday": "thu", "opens": "08:00", "closes": "23:00"}
    ]
    fixture["restaurants"][0]["tables"] = [
        {"id": f"t_{index}", "label": str(index), "capacity": 4} for index in range(10)
    ]
    reset(client, fixture)
    token = signup(client)["token"]

    movers = [
        assert_ok(book(client, token, table_id=f"t_{index}", party_size=4,
                       starts_at_local=f"{THURSDAY}T18:00"), 201)
        for index in range(8)
    ]

    def attempt(index):
        return client.post("/reservation-moves", json={"moves": [
            {"reference": movers[index]["reference"], "table_id": "t_9",
             "starts_at_local": f"{THURSDAY}T19:00"}]},
            headers=headers_for(token, f"move-{index}"))

    responses = race(client, len(movers), attempt)
    assert_no_5xx(responses)

    winners = [response for response in responses if response.status_code == 201]
    losers = [response for response in responses if response.status_code == 409]
    assert len(winners) == 1, [response.status_code for response in responses]
    assert len(losers) == len(movers) - 1

    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)[
        "reservations"]
    on_target = [r for r in listed if r["table_id"] == "t_9"]
    assert len(on_target) == 1
    assert on_target[0]["starts_at_local"] == f"{THURSDAY}T19:00"
    # Every loser is still where it started.
    assert len([r for r in listed if r["starts_at_local"] == f"{THURSDAY}T18:00"]) == 7


def test_reads_and_writes_together_never_produce_a_5xx(client, seeded):
    token = signup(client)["token"]
    assert_ok(book(client, token, table_id="t_1", party_size=2,
                   starts_at_local=f"{THURSDAY}T18:00"), 201)

    def attempt(index):
        kind = index % 6
        if kind == 0:
            return client.get("/health")
        if kind == 1:
            return client.get("/restaurants")
        if kind == 2:
            return client.get("/availability", params={
                "restaurant_id": "r_anker", "date": THURSDAY, "party_size": 2})
        if kind == 3:
            return client.get("/reservations", headers=headers_for(token))
        if kind == 4:
            return client.get("/_test/export")
        return client.post("/reservations", json={
            "restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": f"{THURSDAY}T{18 + (index // 6) % 4}:00",
            "party_size": 4}, headers=headers_for(token, f"mixed-{index}"))

    responses = race(client, 48, attempt)
    assert_no_5xx(responses)
    for response in responses:
        assert response.headers["content-type"].startswith("application/json")
        assert isinstance(response.json(), (dict, list))


def test_garbage_under_load_never_produces_a_5xx(client, seeded):
    """Malformed input is a client error, not a server error."""
    token = signup(client)["token"]
    garbage = [
        ("post", "/reservations", b"{", {"Content-Type": "application/json"}),
        ("post", "/reservations", b"[1]", {"Content-Type": "application/json"}),
        ("post", "/reservations", b"", {"Content-Type": "application/json"}),
        ("post", "/auth/signup", b"null", {"Content-Type": "application/json"}),
        ("post", "/_test/reset", b"{\"users\": 5}", {"Content-Type": "application/json"}),
        ("post", "/_test/import", b"[]", {"Content-Type": "application/json"}),
        ("get", "/availability?restaurant_id=r_anker&date=x&party_size=1", None, {}),
        ("get", "/availability?party_size=1e9&date=2026-09-24&restaurant_id=r_anker", None, {}),
        ("get", "/reservations/" + "R" * 200, None, {"Authorization": f"Bearer {token}"}),
        ("patch", "/reservations/NOPE", b"{}", {"Authorization": f"Bearer {token}"}),
        ("post", "/reservation-moves", b"{\"moves\": 5}",
         {"Authorization": f"Bearer {token}", "Idempotency-Key": "g"}),
        ("get", "/nope", None, {}),
        ("delete", "/reservations", None, {"Authorization": f"Bearer {token}"}),
    ]

    def attempt(index):
        method, url, content, headers = garbage[index % len(garbage)]
        request = client.request(method.upper(), url, headers=headers)
        if content is not None:
            request = client.request(method.upper(), url, content=content, headers=headers)
        return request

    responses = race(client, len(garbage) * 3, attempt)
    assert_no_5xx(responses)
    for response in responses:
        assert 400 <= response.status_code < 500, (response.status_code, response.text)
        assert set(response.json()) == {"error"}
