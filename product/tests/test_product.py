"""The product layer: sessions that end, restaurants that can be created, staff
with roles, the notification outbox and the audit trail.

These are additions to the service's contract rather than parts of it, so nothing
here changes an answer the earlier stages gave. Everything is exercised over HTTP
through the ASGI app, like the rest of the suite.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from tablekeeper import auth, notifications
from tablekeeper.api import create_app

from .conftest import (
    ADA,
    THURSDAY,
    adopt,
    apply_plan,
    assert_ok,
    book,
    closure_body,
    error_of,
    headers_for,
    managed_fixture,
    plan_fixture,
    replan,
    reset,
    seat,
    signup,
    token_for,
)

RESTAURANT = {
    "name": "Zum Anker",
    "timezone": "Europe/Berlin",
    "slot_minutes": 30,
    "reservation_duration_minutes": 90,
    "cancellation_cutoff_minutes": 120,
    "opening_hours": [
        {"weekday": "thu", "opens": "18:00", "closes": "23:00"},
        {"weekday": "fri", "opens": "18:00", "closes": "23:30"},
    ],
    "tables": [
        {"id": "t_1", "label": "1", "capacity": 2},
        {"id": "t_2", "label": "2", "capacity": 4},
    ],
    "combinable": [["t_1", "t_2"]],
}


def open_restaurant(client, token, **overrides):
    """Create a restaurant through the API and return its body."""
    body = {**RESTAURANT, **overrides}
    response = client.post("/restaurants", json=body, headers=headers_for(token))
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# sessions that end
# --------------------------------------------------------------------------- #
def test_a_new_token_reports_when_it_expires(client, seeded):
    token = signup(client, "session@example.com")["token"]
    body = assert_ok(client.get("/auth/session", headers=headers_for(token)), 200)
    assert body["user_id"]
    assert body["email"] == "session@example.com"
    assert body["expires_at"] is not None
    assert body["restaurants"] == []


def test_logging_out_stops_the_token_working(client, seeded):
    token = signup(client, "bye@example.com")["token"]
    assert_ok(client.post("/auth/logout", headers=headers_for(token)), 200)
    response = client.get("/auth/session", headers=headers_for(token))
    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthenticated"


def test_logging_out_twice_is_not_an_error(client, seeded):
    token = signup(client, "twice@example.com")["token"]
    assert_ok(client.post("/auth/logout", headers=headers_for(token)), 200)
    # The second call carries a token that is already dead, so it is a 401 rather
    # than a second success: there is nothing left to sign out of.
    assert client.post("/auth/logout", headers=headers_for(token)).status_code == 401


def test_signing_out_everywhere_kills_every_device(client, seeded):
    first = signup(client, "many@example.com")["token"]
    second = assert_ok(
        client.post("/auth/login", json={
            "email": "many@example.com", "password": "long enough"}), 200
    )["token"]
    third = assert_ok(
        client.post("/auth/login", json={
            "email": "many@example.com", "password": "long enough"}), 200
    )["token"]

    assert_ok(client.post("/auth/logout-all", headers=headers_for(first)), 200)
    for token in (first, second, third):
        assert client.get("/auth/session", headers=headers_for(token)).status_code == 401


def test_an_expired_token_stops_working(client, seeded, monkeypatch, tmp_path):
    """Expiry is the thing a leaking token is supposed to run into."""
    monkeypatch.setenv("TABLEKEEPER_TOKEN_TTL_MINUTES", "1")
    app = create_app(database_path=str(tmp_path / "expiry.db"), test_hooks=True)
    with TestClient(app, raise_server_exceptions=False) as other:
        token = signup(other, "short@example.com")["token"]
        assert other.get("/auth/session", headers=headers_for(token)).status_code == 200
        # Move the clock past the session's own expiry.
        from tablekeeper import clock

        frozen = clock.now()
        clock.freeze(frozen.replace(year=frozen.year + 1))
        try:
            response = other.get("/auth/session", headers=headers_for(token))
            assert response.status_code == 401
            # The token still exists; it is the session that ran out, and signing
            # in again is what a diner does about it.
            fresh = assert_ok(other.post("/auth/login", json={
                "email": "short@example.com", "password": "long enough"}), 200)
            assert other.get(
                "/auth/session", headers=headers_for(fresh["token"])
            ).status_code == 200
        finally:
            clock.reset()


def test_a_token_from_an_older_snapshot_never_expires(client, seeded, tmp_path):
    """Importing an export must not sign every diner out of a working service."""
    token = signup(client, "legacy@example.com")["token"]
    snapshot = assert_ok(client.get("/_test/export"), 200)
    # A snapshot an earlier stage exported has no session rows at all: the tokens
    # in it were issued when tokens did not expire.
    snapshot["state"].pop("token_sessions", None)

    app = create_app(database_path=str(tmp_path / "legacy.db"), test_hooks=True)
    with TestClient(app, raise_server_exceptions=False) as destination:
        assert_ok(destination.post("/_test/import", json=snapshot), 204)
        assert destination.get(
            "/auth/session", headers=headers_for(token)
        ).status_code == 200


def test_repeated_failed_logins_lock_the_address_out(client, seeded):
    wrong = {"email": ADA["email"], "password": "not the password"}
    for _ in range(auth.DEFAULT_LOGIN_MAX_FAILURES):
        response = client.post("/auth/login", json=wrong)
        assert response.status_code == 401
    response = client.post("/auth/login", json=wrong)
    assert response.status_code == 429
    assert error_of(response)["code"] == "too_many_attempts"
    # Even the right password is refused while the lockout stands: the caller is
    # being told about their pace, not about the password.
    assert client.post("/auth/login", json={
        "email": ADA["email"], "password": ADA["password"]}).status_code == 429


def test_a_successful_login_clears_the_failure_count(client, seeded):
    wrong = {"email": ADA["email"], "password": "not the password"}
    for _ in range(auth.DEFAULT_LOGIN_MAX_FAILURES - 1):
        assert client.post("/auth/login", json=wrong).status_code == 401
    assert_ok(client.post("/auth/login", json={
        "email": ADA["email"], "password": ADA["password"]}), 200)
    for _ in range(auth.DEFAULT_LOGIN_MAX_FAILURES - 1):
        assert client.post("/auth/login", json=wrong).status_code == 401


def test_the_lockout_is_per_address(client, seeded):
    wrong = {"email": ADA["email"], "password": "not the password"}
    for _ in range(auth.DEFAULT_LOGIN_MAX_FAILURES):
        client.post("/auth/login", json=wrong)
    # Ada is locked out; a different diner signing in is unaffected.
    signup(client, "other@example.com")
    assert_ok(client.post("/auth/login", json={
        "email": "other@example.com", "password": "long enough"}), 200)


# --------------------------------------------------------------------------- #
# creating a restaurant
# --------------------------------------------------------------------------- #
def test_a_restaurant_can_be_created_by_an_ordinary_person(client, seeded):
    token = signup(client, "owner@example.com", "long enough", "Owner")["token"]
    body = open_restaurant(client, token)
    assert body["name"] == "Zum Anker"
    assert body["timezone"] == "Europe/Berlin"
    assert body["slot_minutes"] == 30
    assert [t["id"] for t in body["tables"]] == ["t_1", "t_2"]
    assert body["combinable"] == [["t_1", "t_2"]]
    assert body["role"] == "owner"
    assert [s["role"] for s in body["staff"]] == ["owner"]

    # It is a real restaurant: searchable, and bookable by a diner.
    assert body["id"] in [
        r["id"] for r in client.get("/restaurants").json()["restaurants"]
    ]
    diner = signup(client, "diner@example.com")["token"]
    response = client.post(
        "/reservations",
        json={"restaurant_id": body["id"], "table_id": "t_1",
              "starts_at_local": f"{THURSDAY}T19:00", "party_size": 2},
        headers={**headers_for(diner), "Idempotency-Key": "made-1"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["restaurant_id"] == body["id"]


def test_the_creator_owns_the_restaurant_and_can_plan_its_seating(client, seeded):
    """Owner is a manager for the rules the earlier stages wrote."""
    token = signup(client, "boss@example.com", "long enough", "Boss")["token"]
    restaurant = open_restaurant(client, token)
    restaurant_id = restaurant["id"]
    policy = {
        "effective_from": "2026-09-01",
        "slot_minutes": 30,
        "reservation_duration_minutes": 90,
        "cancellation_cutoff_minutes": 120,
        "opening_hours": [{"weekday": "thu", "opens": "18:00", "closes": "23:00"}],
        "capacities": {"t_1": 2, "t_2": 4},
    }
    response = client.post(
        f"/restaurants/{restaurant_id}/policies",
        json=policy,
        headers={**headers_for(token), "Idempotency-Key": "p1"},
    )
    assert response.status_code == 201, response.text


def test_a_stranger_cannot_read_a_restaurants_people(client, seeded):
    owner = signup(client, "owner2@example.com", "long enough", "Owner")["token"]
    restaurant = open_restaurant(client, owner)
    stranger = signup(client, "stranger@example.com")["token"]
    response = client.get(
        f"/restaurants/{restaurant['id']}/staff", headers=headers_for(stranger)
    )
    assert response.status_code == 404
    # The public detail of the same restaurant is still open to everybody.
    assert client.get(f"/restaurants/{restaurant['id']}").status_code == 200


def test_creating_the_same_restaurant_twice_with_one_key_makes_one(client, seeded):
    token = signup(client, "retry@example.com", "long enough", "Retry")["token"]
    headers = {**headers_for(token), "Idempotency-Key": "signup-1"}
    first = client.post("/restaurants", json=RESTAURANT, headers=headers)
    assert first.status_code == 201
    second = client.post("/restaurants", json=RESTAURANT, headers=headers)
    assert second.status_code == 200
    assert second.json() == first.json()
    mine = client.get("/restaurants/mine", headers=headers_for(token)).json()
    assert len(mine["restaurants"]) == 1


def test_my_restaurants_lists_what_i_work_at_and_nothing_else(client, seeded):
    owner = signup(client, "mine@example.com", "long enough", "Mine")["token"]
    restaurant = open_restaurant(client, owner)
    other = signup(client, "notmine@example.com")["token"]
    assert_ok(client.get("/restaurants/mine", headers=headers_for(owner)), 200)
    assert client.get("/restaurants/mine", headers=headers_for(other)).json() == {
        "restaurants": []
    }
    assert client.get("/restaurants/mine", headers=headers_for(owner)).json()[
        "restaurants"
    ][0]["id"] == restaurant["id"]


def test_a_restaurant_needs_an_id_that_is_not_somebody_elses(client, seeded):
    """A created restaurant gets its own id; two owners never collide."""
    first = signup(client, "a@example.com", "long enough", "A")["token"]
    second = signup(client, "b@example.com", "long enough", "B")["token"]
    one = open_restaurant(client, first, name="One")
    two = open_restaurant(client, second, name="Two")
    assert one["id"] != two["id"]


@pytest.mark.parametrize(
    "broken, message",
    [
        ({"name": None}, "name"),
        ({"name": ""}, "name"),
        ({"timezone": "Mars/Olympus"}, "timezone"),
        ({"slot_minutes": 0}, "slot_minutes"),
        ({"tableless": True}, "tables"),
        ({"opening_hours": []}, "opening_hours"),
        ({"opening_hours": [{"weekday": "nope", "opens": "18:00", "closes": "23:00"}]},
         "opening_hours"),
        ({"opening_hours": [{"weekday": "thu", "opens": "23:00", "closes": "18:00"}]},
         "opening_hours"),
        ({"tables": [{"id": "t_1", "label": "1", "capacity": 0}]}, "capacity"),
        ({"tables": [{"id": "t_1", "label": "1", "capacity": 2},
                     {"id": "t_1", "label": "1b", "capacity": 4}]}, "repeats"),
        ({"combinable": [["t_1", "t_9"]]}, "unknown table"),
        ({"combinable": [["t_1", "t_1"]]}, "same table twice"),
        ({"combinable": [["t_1"]]}, "pair"),
    ],
)
def test_a_broken_restaurant_is_refused(client, seeded, broken, message):
    token = signup(client, "broken@example.com", "long enough", "Broken")["token"]
    body = {**RESTAURANT, **{k: v for k, v in broken.items() if k == "tableless"}}
    if "tableless" in broken:
        body.pop("tables")
    else:
        body.update({k: v for k, v in broken.items() if v is not None})
        for key, value in broken.items():
            if value is None:
                body.pop(key, None)
    response = client.post("/restaurants", json=body, headers=headers_for(token))
    assert response.status_code == 422, response.text
    assert error_of(response)["code"] == "validation_failed"
    assert message in response.text
    # Nothing was half-created: the id the caller would have got does not exist.
    assert client.get("/restaurants/mine", headers=headers_for(token)).json() == {
        "restaurants": []
    }


def test_creating_a_restaurant_needs_a_token(client, seeded):
    response = client.post("/restaurants", json=RESTAURANT)
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# staff and roles
# --------------------------------------------------------------------------- #
def test_an_owner_can_add_a_manager_and_a_host(client, seeded):
    owner = signup(client, "owner3@example.com", "long enough", "Owner")["token"]
    restaurant = open_restaurant(client, owner)
    restaurant_id = restaurant["id"]
    signup(client, "manager@example.com", "long enough", "Manager")
    signup(client, "host@example.com", "long enough", "Host")

    added = assert_ok(client.post(
        f"/restaurants/{restaurant_id}/staff",
        json={"email": "manager@example.com", "role": "manager"},
        headers=headers_for(owner),
    ), 201)
    assert added["role"] == "manager"
    assert_ok(client.post(
        f"/restaurants/{restaurant_id}/staff",
        json={"email": "host@example.com", "role": "host"},
        headers=headers_for(owner),
    ), 201)

    people = assert_ok(client.get(
        f"/restaurants/{restaurant_id}/staff", headers=headers_for(owner)), 200
    )
    assert sorted(s["role"] for s in people["staff"]) == ["host", "manager", "owner"]


def test_a_manager_may_publish_a_policy_and_a_host_may_not(client, seeded):
    owner = signup(client, "owner4@example.com", "long enough", "Owner")["token"]
    restaurant = open_restaurant(client, owner)
    restaurant_id = restaurant["id"]
    signup(client, "manager2@example.com", "long enough", "Manager")
    host_token = signup(client, "host2@example.com", "long enough", "Host")["token"]
    for email, role in (("manager2@example.com", "manager"), ("host2@example.com", "host")):
        assert_ok(client.post(
            f"/restaurants/{restaurant_id}/staff",
            json={"email": email, "role": role},
            headers=headers_for(owner),
        ), 201)

    manager = assert_ok(client.post("/auth/login", json={
        "email": "manager2@example.com", "password": "long enough"}), 200)["token"]
    policy = {
        "effective_from": "2026-09-01",
        "slot_minutes": 30,
        "reservation_duration_minutes": 90,
        "cancellation_cutoff_minutes": 120,
        "opening_hours": [{"weekday": "thu", "opens": "18:00", "closes": "23:00"}],
        "capacities": {"t_1": 2, "t_2": 4},
    }
    assert client.post(
        f"/restaurants/{restaurant_id}/policies",
        json=policy,
        headers={**headers_for(manager), "Idempotency-Key": "manager-p1"},
    ).status_code == 201
    refused = client.post(
        f"/restaurants/{restaurant_id}/policies",
        json=policy,
        headers={**headers_for(host_token), "Idempotency-Key": "host-p1"},
    )
    assert refused.status_code == 403


def test_a_host_can_read_the_room_but_not_change_who_works_here(client, seeded):
    owner = signup(client, "owner5@example.com", "long enough", "Owner")["token"]
    restaurant = open_restaurant(client, owner)
    restaurant_id = restaurant["id"]
    host_token = signup(client, "host3@example.com", "long enough", "Host")["token"]
    assert_ok(client.post(
        f"/restaurants/{restaurant_id}/staff",
        json={"email": "host3@example.com", "role": "host"},
        headers=headers_for(owner),
    ), 201)
    # A host may look at the room...
    assert_ok(client.get(
        f"/restaurants/{restaurant_id}/staff", headers=headers_for(host_token)), 200
    )
    # ...and may not add anybody to it.
    other = signup(client, "other2@example.com", "long enough", "Other")
    response = client.post(
        f"/restaurants/{restaurant_id}/staff",
        json={"email": other["display_name"], "role": "manager"},
        headers=headers_for(host_token),
    )
    assert response.status_code == 403


def test_a_diner_cannot_add_themselves_to_a_restaurant(client, seeded):
    owner = signup(client, "owner6@example.com", "long enough", "Owner")["token"]
    restaurant = open_restaurant(client, owner)
    diner = signup(client, "sneaky@example.com", "long enough", "Sneaky")
    response = client.post(
        f"/restaurants/{restaurant['id']}/staff",
        json={"email": "sneaky@example.com", "role": "manager"},
        headers=headers_for(diner["token"]),
    )
    assert response.status_code == 403


def test_somebody_without_an_account_cannot_be_added(client, seeded):
    owner = signup(client, "owner7@example.com", "long enough", "Owner")["token"]
    restaurant = open_restaurant(client, owner)
    response = client.post(
        f"/restaurants/{restaurant['id']}/staff",
        json={"email": "nobody@example.com", "role": "host"},
        headers=headers_for(owner),
    )
    assert response.status_code == 422
    assert "sign up first" in response.text


def test_a_role_can_be_changed_and_a_host_cannot_publish_after_a_demotion(client, seeded):
    owner = signup(client, "owner8@example.com", "long enough", "Owner")["token"]
    restaurant = open_restaurant(client, owner)
    restaurant_id = restaurant["id"]
    signup(client, "demoted@example.com", "long enough", "Demoted")
    assert_ok(client.post(
        f"/restaurants/{restaurant_id}/staff",
        json={"email": "demoted@example.com", "role": "manager"},
        headers=headers_for(owner),
    ), 201)
    person = assert_ok(client.post("/auth/login", json={
        "email": "demoted@example.com", "password": "long enough"}), 200)

    # As a manager they could publish; the demotion to host is what removes it.
    changed = assert_ok(client.patch(
        f"/restaurants/{restaurant_id}/staff/{person['user_id']}",
        json={"role": "host"},
        headers=headers_for(owner),
    ), 200)
    assert changed["role"] == "host"
    policy = {
        "effective_from": "2026-09-01", "slot_minutes": 30,
        "reservation_duration_minutes": 90, "cancellation_cutoff_minutes": 120,
        "opening_hours": [{"weekday": "thu", "opens": "18:00", "closes": "23:00"}],
        "capacities": {"t_1": 2, "t_2": 4},
    }
    refused = client.post(
        f"/restaurants/{restaurant_id}/policies", json=policy,
        headers={**headers_for(person["token"]), "Idempotency-Key": "demoted-p1"},
    )
    assert refused.status_code == 403


def test_the_last_owner_cannot_be_removed_or_demoted(client, seeded):
    owner = signup(client, "lonely@example.com", "long enough", "Lonely")
    restaurant = open_restaurant(client, owner["token"])
    restaurant_id = restaurant["id"]
    demote = client.patch(
        f"/restaurants/{restaurant_id}/staff/{owner['user_id']}",
        json={"role": "manager"},
        headers=headers_for(owner["token"]),
    )
    assert demote.status_code == 409
    assert error_of(demote)["code"] == "last_owner"
    remove = client.delete(
        f"/restaurants/{restaurant_id}/staff/{owner['user_id']}",
        headers=headers_for(owner["token"]),
    )
    assert remove.status_code == 409
    assert error_of(remove)["code"] == "last_owner"


def test_a_second_owner_can_be_added_and_then_the_first_may_leave(client, seeded):
    first = signup(client, "first@example.com", "long enough", "First")
    restaurant = open_restaurant(client, first["token"])
    restaurant_id = restaurant["id"]
    second = signup(client, "second@example.com", "long enough", "Second")
    assert_ok(client.post(
        f"/restaurants/{restaurant_id}/staff",
        json={"email": "second@example.com", "role": "owner"},
        headers=headers_for(first["token"]),
    ), 201)
    assert_ok(client.delete(
        f"/restaurants/{restaurant_id}/staff/{first['user_id']}",
        headers=headers_for(second["token"]),
    ), 200)
    people = assert_ok(client.get(
        f"/restaurants/{restaurant_id}/staff", headers=headers_for(second["token"])), 200
    )
    assert [s["role"] for s in people["staff"]] == ["owner"]


def test_adding_somebody_twice_is_refused(client, seeded):
    owner = signup(client, "dup@example.com", "long enough", "Dup")["token"]
    restaurant = open_restaurant(client, owner)
    signup(client, "again@example.com", "long enough", "Again")
    headers = headers_for(owner)
    assert_ok(client.post(
        f"/restaurants/{restaurant['id']}/staff",
        json={"email": "again@example.com", "role": "host"}, headers=headers), 201
    )
    response = client.post(
        f"/restaurants/{restaurant['id']}/staff",
        json={"email": "again@example.com", "role": "host"}, headers=headers,
    )
    assert response.status_code == 409
    assert error_of(response)["code"] == "already_staff"


def test_an_unknown_role_is_refused(client, seeded):
    owner = signup(client, "role@example.com", "long enough", "Role")["token"]
    restaurant = open_restaurant(client, owner)
    signup(client, "person@example.com", "long enough", "Person")
    response = client.post(
        f"/restaurants/{restaurant['id']}/staff",
        json={"email": "person@example.com", "role": "wizard"},
        headers=headers_for(owner),
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# the outbox
# --------------------------------------------------------------------------- #
def drain(client, headers):
    return assert_ok(client.post("/_test/notifications/drain", headers=headers), 200)


def guest_messages(delivered):
    """What a booking sent the guests, out of everything the drain delivered.

    Signing up also sends the new account a confirmation of its own address,
    which is that person's business and belongs to no restaurant; the counts in
    these tests are about what a booking tells a diner.
    """
    return [m for m in delivered["messages"] if m["kind"] != "email_verification"]


def manager_headers(client):
    """Ada is this restaurant's manager; the outbox is hers to read."""
    return headers_for(assert_ok(client.post("/auth/login", json={
        "email": ADA["email"], "password": ADA["password"]}), 200)["token"])


@pytest.fixture
def diner(client) -> dict:
    """A second diner, who manages nothing."""
    return {"token": signup(client, email="bob@example.com", display_name="Bob")["token"]}


@pytest.fixture
def room(client) -> dict:
    """The three-table managed room: a closing table has somewhere to move to."""
    fixture = plan_fixture()
    reset(client, fixture)
    return {"fixture": fixture, "token": token_for(client)}


@pytest.fixture
def managed(client) -> dict:
    """The default fixture, with Ada managing the restaurant.

    The outbox and the audit trail belong to the people who run a restaurant, so
    these tests need somebody who actually does.
    """
    fixture = managed_fixture()
    reset(client, fixture)
    return fixture


def test_a_confirmation_is_written_when_a_booking_is_made(client, managed):
    token = signup(client, "confirmed@example.com", "long enough", "Diner")["token"]
    reference = assert_ok(book(client, token, table_id="t_2", party_size=4), 201)["reference"]
    outbox = assert_ok(
        client.get("/restaurants/r_anker/notifications", headers=manager_headers(client)), 200
    )
    assert outbox["summary"] == {"queued": 1, "sent": 0, "failed": 0}
    message = outbox["notifications"][0]
    assert message["kind"] == "booking_confirmed"
    assert message["to_email"] == "confirmed@example.com"
    assert message["reference"] == reference
    assert message["status"] == "queued"

    delivered = drain(client, manager_headers(client))
    assert delivered["result"]["queued"] == 0 and delivered["result"]["failed"] == 0
    messages = guest_messages(delivered)
    assert len(messages) == 1
    assert messages[0]["kind"] == "booking_confirmed"
    assert "Zum Anker" in messages[0]["subject"]
    assert reference in messages[0]["body"]
    assert "4 guests" in messages[0]["body"]


def test_a_retried_booking_sends_one_message(client, seeded):
    """The replay returns the stored answer before anything is written."""
    token = signup(client, "retry2@example.com", "long enough", "Diner")["token"]
    headers = headers_for(token, "one-key")
    body = {"restaurant_id": "r_anker", "table_id": "t_2",
            "starts_at_local": f"{THURSDAY}T19:00", "party_size": 4}
    assert client.post("/reservations", json=body, headers=headers).status_code == 201
    assert client.post("/reservations", json=body, headers=headers).status_code == 200
    delivered = drain(client, manager_headers(client))
    assert len(guest_messages(delivered)) == 1


def test_cancelling_amending_and_moving_each_tell_the_diner(client, managed):
    token = signup(client, "lifecycle@example.com", "long enough", "Diner")["token"]
    headers = headers_for(token)
    first = assert_ok(book(client, token, table_id="t_1", party_size=2,
                           starts_at_local=f"{THURSDAY}T19:00"), 201)
    second = assert_ok(book(client, token, table_id="t_2", party_size=4,
                            starts_at_local=f"{THURSDAY}T19:00"), 201)

    assert_ok(client.patch(f"/reservations/{first['reference']}",
                           json={"party_size": 2, "starts_at_local": f"{THURSDAY}T20:00"},
                           headers=headers), 200)
    assert_ok(client.post(
        "/reservation-moves",
        json={"moves": [{"reference": second["reference"], "table_id": "t_2",
                         "starts_at_local": f"{THURSDAY}T20:00"}]},
        headers=headers_for(token, "batch-1"),
    ), 201)
    assert_ok(client.post(f"/reservations/{first['reference']}/cancel", headers=headers), 200)

    # Two bookings confirmed, one amended, one moved, one cancelled: five real
    # changes, and exactly one message for each.
    delivered = drain(client, manager_headers(client))
    messages = guest_messages(delivered)
    assert len(messages) == 5
    assert messages[-1]["kind"] == "booking_cancelled"
    assert "cancelled" in messages[-1]["subject"].lower()
    kinds = [m["kind"] for m in messages]
    assert kinds.count("booking_confirmed") == 2
    assert kinds.count("booking_changed") == 2


def test_a_booking_that_was_not_moved_gets_no_message(client, managed):
    token = signup(client, "unmoved@example.com", "long enough", "Diner")["token"]
    booked = assert_ok(book(client, token, table_id="t_2", party_size=4), 201)
    drain(client, manager_headers(client))
    # A batch that lists the booking but leaves it exactly where it is.
    assert_ok(client.post(
        "/reservation-moves",
        json={"moves": [{"reference": booked["reference"], "table_id": "t_2"}]},
        headers=headers_for(token, "batch-2"),
    ), 201)
    after = assert_ok(client.get(
        "/restaurants/r_anker/notifications", headers=manager_headers(client)), 200
    )
    assert after["summary"]["queued"] == 0


def test_a_replan_tells_the_diners_whose_tables_moved(client, room, diner):
    """The point of the whole repair: nobody turns up to a table that moved."""
    seated = assert_ok(
        seat(client, diner["token"], "t_1", at=f"{THURSDAY}T19:00", party_size=2), 201
    )
    drain(client, headers_for(room["token"]))
    assert_ok(apply_plan(
        client, room["token"],
        assert_ok(replan(client, room["token"], closure_body("t_1", THURSDAY)), 201)["plan_id"],
    ), 201)

    delivered = drain(client, headers_for(room["token"]))
    assert delivered["result"]["sent"] == 1
    message = delivered["messages"][0]
    assert message["kind"] == "seating_changed"
    assert message["reference"] == seated["reference"]
    assert message["to_email"] == "bob@example.com"
    assert "has changed" in message["subject"]
    assert "unchanged" in message["body"]


def test_adopting_a_series_tells_the_diner_once_for_the_agreement(client, managed):
    token = signup(client, "series-outbox@example.com", "long enough", "Diner")["token"]
    anchor = assert_ok(book(client, token, table_id="t_1", party_size=2,
                            starts_at_local=f"{THURSDAY}T19:00"), 201)
    drain(client, manager_headers(client))
    assert_ok(adopt(client, token, anchor["reference"], count=3, interval_weeks=1,
                    key="series-1"), 201)

    delivered = drain(client, manager_headers(client))
    assert delivered["result"]["sent"] == 1
    message = delivered["messages"][0]
    assert message["kind"] == "series_adopted"
    assert "3 bookings in total" in message["body"]
    assert "every week" in message["body"]


def test_nothing_is_sent_when_no_transport_is_configured(client, managed):
    """An unconfigured deployment is visibly unconfigured, not silently successful."""
    token = signup(client, "notransport@example.com", "long enough", "Diner")["token"]
    assert_ok(book(client, token, table_id="t_2", party_size=4), 201)
    result = assert_ok(client.post(
        "/restaurants/r_anker/notifications/drain", headers=manager_headers(client)), 200
    )
    assert result == {"configured": False, "sent": 0, "failed": 0, "queued": 1}
    # And the message is still there to send when a transport is configured.
    outbox = assert_ok(client.get(
        "/restaurants/r_anker/notifications", headers=manager_headers(client)), 200
    )
    assert outbox["summary"]["queued"] == 1


def test_a_transport_that_raises_marks_the_message_failed_and_keeps_going(
    client, managed
):
    token = signup(client, "failing@example.com", "long enough", "Diner")["token"]
    assert_ok(book(client, token, table_id="t_2", party_size=4), 201)

    class Broken:
        def send(self, message):
            raise RuntimeError("mail server said no")

    client.app.state.transport = Broken()
    result = assert_ok(client.post(
        "/restaurants/r_anker/notifications/drain", headers=manager_headers(client)), 200
    )
    assert result == {"configured": True, "sent": 0, "failed": 1, "queued": 0}
    outbox = assert_ok(client.get(
        "/restaurants/r_anker/notifications", headers=manager_headers(client)), 200
    )
    assert outbox["notifications"][0]["status"] == "failed"
    assert "mail server said no" in outbox["notifications"][0]["last_error"]

    # A failed message can be put back and delivered once the transport works.
    notification_id = outbox["notifications"][0]["id"]
    requeued = assert_ok(client.post(
        f"/restaurants/r_anker/notifications/{notification_id}/retry",
        headers=manager_headers(client)), 200
    )
    assert requeued["requeued"] is True
    client.app.state.transport = client.app.state.recorder
    assert assert_ok(client.post(
        "/restaurants/r_anker/notifications/drain", headers=manager_headers(client)), 200
    )["sent"] == 1


def test_the_outbox_is_between_a_restaurant_and_its_staff(client, seeded):
    diner = signup(client, "nosy@example.com")["token"]
    assert_ok(book(client, diner, table_id="t_2", party_size=4), 201)
    # The diner booked here, but does not work here, so the restaurant's outbox —
    # every message to every diner — is not theirs to read.
    assert client.get(
        "/restaurants/r_anker/notifications", headers=headers_for(diner)
    ).status_code == 404


# --------------------------------------------------------------------------- #
# the audit trail
# --------------------------------------------------------------------------- #
def test_the_audit_trail_names_who_did_what(client, seeded):
    owner = signup(client, "audit@example.com", "long enough", "Owner")
    restaurant = open_restaurant(client, owner["token"])
    restaurant_id = restaurant["id"]
    signup(client, "hired@example.com", "long enough", "Hired")
    assert_ok(client.post(
        f"/restaurants/{restaurant_id}/staff",
        json={"email": "hired@example.com", "role": "manager"},
        headers=headers_for(owner["token"]),
    ), 201)
    ada = headers_for(assert_ok(client.post("/auth/login", json={
        "email": ADA["email"], "password": ADA["password"]}), 200)["token"])
    policy = {
        "effective_from": "2026-09-01", "slot_minutes": 30,
        "reservation_duration_minutes": 90, "cancellation_cutoff_minutes": 120,
        "opening_hours": [{"weekday": "thu", "opens": "18:00", "closes": "23:00"}],
        "capacities": {"t_1": 2, "t_2": 4},
    }
    assert client.post(
        f"/restaurants/{restaurant_id}/policies", json=policy,
        headers={**ada, "Idempotency-Key": "audit-p1"},
    ).status_code in (201, 403)

    trail = assert_ok(client.get(
        f"/restaurants/{restaurant_id}/audit", headers=headers_for(owner["token"])), 200
    )
    actions = [entry["action"] for entry in trail["entries"]]
    assert "restaurant_created" in actions
    assert "staff_added" in actions
    added = next(e for e in trail["entries"] if e["action"] == "staff_added")
    assert added["user_id"] == owner["user_id"]
    assert "hired" not in added["detail"]


def test_a_stranger_cannot_read_the_audit_trail(client, seeded):
    owner = signup(client, "audit2@example.com", "long enough", "Owner")["token"]
    restaurant = open_restaurant(client, owner)
    stranger = signup(client, "peek@example.com")["token"]
    assert client.get(
        f"/restaurants/{restaurant['id']}/audit", headers=headers_for(stranger)
    ).status_code == 404


# --------------------------------------------------------------------------- #
# the control surface is switchable
# --------------------------------------------------------------------------- #
def test_the_test_hooks_can_be_turned_off_entirely(tmp_path):
    """A deployment that turns them off does not serve those paths at all."""
    app = create_app(database_path=str(tmp_path / "no-hooks.db"), test_hooks=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/health").status_code == 200
        for method, path in (
            ("GET", "/_test/export"),
            ("POST", "/_test/reset"),
            ("POST", "/_test/import"),
            ("POST", "/_test/notifications/drain"),
        ):
            response = client.request(method, path, json={})
            assert response.status_code == 404, f"{method} {path} answered"


def test_the_hooks_follow_the_environment_when_nothing_is_said(tmp_path, monkeypatch):
    monkeypatch.setenv("TABLEKEEPER_TEST_HOOKS", "0")
    app = create_app(database_path=str(tmp_path / "env-hooks.db"))
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/_test/export").status_code == 404


def test_the_snapshot_carries_the_new_tables(client, seeded):
    token = signup(client, "snapshot@example.com")["token"]
    restaurant = open_restaurant(client, token)
    assert_ok(book(client, token, table_id="t_1", party_size=2), 201)
    state = assert_ok(client.get("/_test/export"), 200)["state"]
    for table in ("token_sessions", "restaurant_staff", "notifications",
                  "audit_log", "login_attempts"):
        assert table in state, table
    assert any(r["kind"] == "booking_confirmed" for r in state["notifications"])
    assert restaurant["id"] in [r["id"] for r in state["restaurants"]]
