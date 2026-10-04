"""Stage 3: published booking policies.

A policy is a complete set of rules with the date it takes effect from. Only a
manager of the restaurant may publish one, versions are never reused or edited,
and what a policy changes is every *future* decision — never a booking that has
already accepted the terms it was decided under.
"""

from __future__ import annotations

import uuid

import pytest

from .conftest import (
    FRIDAY,
    SATURDAY,
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


@pytest.fixture
def manager(client) -> dict:
    """A managed restaurant, loaded, with its manager signed in."""
    fixture = managed_fixture()
    reset(client, fixture)
    headers = login_headers(client)          # Ada is the seeded manager
    return {"fixture": fixture, "headers": headers, "token": token_of(headers)}


def token_of(headers: dict) -> str:
    return headers["Authorization"].split(" ", 1)[1]


def policies_of(client, restaurant_id: str = "r_anker") -> list[dict]:
    return assert_ok(client.get(f"/restaurants/{restaurant_id}/policies"), 200)["policies"]


def availability(client, party_size: int, date: str = THURSDAY, **params):
    return assert_ok(client.get("/availability", params={
        "restaurant_id": "r_anker", "date": date, "party_size": party_size, **params}), 200)


# --------------------------------------------------------------------------- #
# who may publish
# --------------------------------------------------------------------------- #
def test_publishing_needs_a_token(client, manager):
    response = client.post("/restaurants/r_anker/policies", json=policy_body())
    assert error_of(response)["code"] == "unauthenticated"
    assert response.status_code == 401


def test_a_diner_who_is_not_a_manager_may_not_publish(client, manager):
    diner = signup(client, email="diner@example.com")["token"]
    response = publish(client, diner, policy_body())
    assert response.status_code == 403
    assert error_of(response)["code"] == "forbidden"
    assert policies_of(client) == []


def test_a_manager_may_publish(client, manager):
    response = publish(client, manager["token"], policy_body())
    body = assert_ok(response, 201)
    assert body["policy_version"] == 1


def test_an_unknown_restaurant_is_not_found_even_for_a_manager(client, manager):
    response = publish(client, manager["token"], policy_body(), restaurant_id="r_nope")
    assert error_of(response)["code"] == "not_found"


def test_a_manager_of_one_restaurant_does_not_manage_another(client, manager):
    fixture = managed_fixture()
    fixture["restaurants"].append({**fixture["restaurants"][0], "id": "r_bake",
                                   "name": "Bakery Nine", "manager_user_ids": []})
    reset(client, fixture)
    headers = login_headers(client)
    assert_ok(publish(client, token_of(headers), policy_body(capacities={"t_1": 2, "t_2": 4})), 201)
    response = publish(client, token_of(headers), policy_body(capacities={"t_1": 2, "t_2": 4}),
                       restaurant_id="r_bake")
    assert response.status_code == 403


def test_a_manager_listed_by_id_is_the_one_allowed(client, manager):
    """Bob is seeded in the fixture's users only when the fixture names him."""
    fixture = base_fixture()
    fixture["users"].append({"id": "u_bob", "email": "bob@example.com",
                             "password": "correct horse", "display_name": "Bob"})
    fixture["restaurants"][0]["manager_user_ids"] = ["u_bob"]
    reset(client, fixture)
    bob = client.post("/auth/login", json={"email": "bob@example.com",
                                           "password": "correct horse"})
    assert_ok(bob, 200)
    assert_ok(publish(client, bob.json()["token"], policy_body()), 201)
    ada = login_headers(client)                       # Ada is not a manager here
    assert publish(client, token_of(ada), policy_body()).status_code == 403


def test_publishing_needs_an_idempotency_key(client, manager):
    response = client.post("/restaurants/r_anker/policies", json=policy_body(),
                           headers=headers_for(manager["token"]))
    assert response.status_code == 400
    assert error_of(response)["code"] == "missing_idempotency_key"


def test_a_manager_gains_no_access_to_another_diner_booking(client, manager):
    diner = signup(client, email="diner@example.com")["token"]
    reference = assert_ok(book(client, diner, table_id="t_2", party_size=4), 201)["reference"]
    assert error_of(client.get(f"/reservations/{reference}",
                               headers=headers_for(manager["token"])))["code"] == "not_found"
    assert error_of(client.get(f"/reservations/{reference}/history",
                               headers=headers_for(manager["token"])))["code"] == "not_found"


# --------------------------------------------------------------------------- #
# versions
# --------------------------------------------------------------------------- #
def test_versions_increase_by_one_per_restaurant(client, manager):
    token = manager["token"]
    assert_ok(publish(client, token, policy_body(effective_from=THURSDAY)), 201)["policy_version"]
    second = assert_ok(publish(client, token, policy_body(effective_from=FRIDAY)), 201)
    third = assert_ok(publish(client, token, policy_body(effective_from=FRIDAY)), 201)
    assert [p["policy_version"] for p in policies_of(client)] == [1, 2, 3]
    assert second["policy_version"] == 2 and third["policy_version"] == 3


def test_versions_are_counted_per_restaurant(client, manager):
    fixture = managed_fixture()
    fixture["restaurants"].append({**fixture["restaurants"][0], "id": "r_bake",
                                   "name": "Bakery Nine"})
    reset(client, fixture)
    token = token_of(login_headers(client))
    assert_ok(publish(client, token, policy_body(), restaurant_id="r_bake"), 201)
    assert_ok(publish(client, token, policy_body()), 201)
    assert [p["policy_version"] for p in policies_of(client, "r_bake")] == [1]
    assert [p["policy_version"] for p in policies_of(client)] == [1]


def test_a_refused_policy_allocates_no_version(client, manager):
    token = manager["token"]
    broken = policy_body(slot_minutes=0)
    assert publish(client, token, broken).status_code == 422
    assert policies_of(client) == []
    assert_ok(publish(client, token, policy_body()), 201)
    assert [p["policy_version"] for p in policies_of(client)] == [1]


def test_a_replay_returns_the_original_and_allocates_no_version(client, manager):
    token = manager["token"]
    first = assert_ok(publish(client, token, policy_body(), key="one-key"), 201)
    replay = publish(client, token, policy_body(), key="one-key")
    assert replay.status_code == 200
    assert replay.json() == first
    assert len(policies_of(client)) == 1


def test_one_key_cannot_publish_two_different_policies(client, manager):
    token = manager["token"]
    assert_ok(publish(client, token, policy_body(slot_minutes=30), key="one-key"), 201)
    response = publish(client, token, policy_body(slot_minutes=60), key="one-key")
    assert error_of(response)["code"] == "idempotency_key_reuse"
    assert len(policies_of(client)) == 1


def test_policies_are_immutable(client, manager):
    token = manager["token"]
    assert_ok(publish(client, token, policy_body()), 201)
    for method in ("PATCH", "PUT", "DELETE"):
        response = client.request(method, "/restaurants/r_anker/policies/1",
                                  json=policy_body(), headers=headers_for(token, "x"))
        assert response.status_code in (404, 405), method
        assert "error" in response.json()
    assert policies_of(client)[0]["slot_minutes"] == 30


def test_the_listing_is_public_and_in_publication_order(client, manager):
    token = manager["token"]
    assert_ok(publish(client, token, policy_body(effective_from=FRIDAY, slot_minutes=60)), 201)
    assert_ok(publish(client, token, policy_body(effective_from=THURSDAY, slot_minutes=45)), 201)
    listed = assert_ok(client.get("/restaurants/r_anker/policies"), 200)["policies"]
    assert [p["policy_version"] for p in listed] == [1, 2]
    assert [p["slot_minutes"] for p in listed] == [60, 45], "publication order, not date order"


def test_the_listing_omits_policy_zero(client, seeded):
    assert policies_of(client) == []


def test_listing_an_unknown_restaurant_is_not_found(client, seeded):
    assert error_of(client.get("/restaurants/r_nope/policies"))["code"] == "not_found"


def test_the_published_body_is_returned_with_its_version(client, manager):
    supplied = policy_body(effective_from=FRIDAY, reservation_duration_minutes=60,
                           cancellation_cutoff_minutes=30)
    body = assert_ok(publish(client, manager["token"], supplied), 201)
    assert body == {**supplied, "policy_version": 1}


def test_unknown_fields_in_a_policy_are_ignored(client, manager):
    supplied = policy_body(souvenir="yes", tables=[{"id": "t_9"}], timezone="UTC")
    body = assert_ok(publish(client, manager["token"], supplied), 201)
    assert set(body) == set(policy_body()) | {"policy_version"}
    # The restaurant's own identity is not something a policy can rewrite.
    detail = assert_ok(client.get("/restaurants/r_anker"), 200)
    assert detail["timezone"] == "Europe/Berlin"
    assert [t["id"] for t in detail["tables"]] == ["t_1", "t_2"]


def test_the_restaurant_detail_keeps_reporting_the_fixture(client, manager):
    assert_ok(publish(client, manager["token"],
                      policy_body(slot_minutes=60, capacities={"t_1": 8, "t_2": 8})), 201)
    detail = assert_ok(client.get("/restaurants/r_anker"), 200)
    assert detail["slot_minutes"] == 30, "the detail is the seeded configuration"
    assert [t["capacity"] for t in detail["tables"]] == [2, 4]


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", [
    "effective_from", "slot_minutes", "reservation_duration_minutes",
    "cancellation_cutoff_minutes", "opening_hours", "capacities",
])
def test_every_field_of_a_policy_is_required(client, manager, field):
    body = policy_body()
    del body[field]
    response = publish(client, manager["token"], body)
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"
    assert policies_of(client) == []


@pytest.mark.parametrize("value", [
    "2026-02-30", "2026-13-01", "not-a-date", "2026-9-4", "", 7, True, None, [],
    "2026-09-24T00:00",
])
def test_effective_from_must_be_an_actual_date(client, manager, value):
    response = publish(client, manager["token"], policy_body(effective_from=value))
    assert error_of(response)["code"] == "validation_failed"


def test_an_effective_date_in_the_past_is_allowed(client, manager):
    body = assert_ok(publish(client, manager["token"],
                             policy_body(effective_from="2020-01-01")), 201)
    assert body["effective_from"] == "2020-01-01"


@pytest.mark.parametrize("field,bad", [
    ("slot_minutes", 0), ("slot_minutes", 1441), ("slot_minutes", "30"),
    ("slot_minutes", True), ("slot_minutes", 1.5), ("slot_minutes", None),
    ("reservation_duration_minutes", 0), ("reservation_duration_minutes", 1441),
    ("reservation_duration_minutes", "90"), ("reservation_duration_minutes", False),
    ("cancellation_cutoff_minutes", -1), ("cancellation_cutoff_minutes", 10081),
    ("cancellation_cutoff_minutes", "120"), ("cancellation_cutoff_minutes", True),
])
def test_the_integers_of_a_policy_have_bounds_and_types(client, manager, field, bad):
    response = publish(client, manager["token"], policy_body(**{field: bad}))
    assert error_of(response)["code"] == "validation_failed", f"{field}={bad!r}"


def test_the_bounds_are_inclusive(client, manager):
    assert_ok(publish(client, manager["token"],
                      policy_body(slot_minutes=1, reservation_duration_minutes=1440)), 201)
    assert_ok(publish(client, manager["token"],
                      policy_body(cancellation_cutoff_minutes=0)), 201)
    assert_ok(publish(client, manager["token"],
                      policy_body(cancellation_cutoff_minutes=10080)), 201)


@pytest.mark.parametrize("hours", [
    "not a list", {"weekday": "thu"}, 7, None,
    [{"weekday": "thu", "opens": "18:00", "closes": "23:00"},
     {"weekday": "thu", "opens": "12:00", "closes": "14:00"}],   # a weekday twice
    [{"weekday": "thursday", "opens": "18:00", "closes": "23:00"}],
    [{"weekday": "thu", "opens": "23:00", "closes": "18:00"}],   # closes before opens
    [{"weekday": "thu", "opens": "18:00", "closes": "18:00"}],
    [{"weekday": "thu", "opens": "6pm", "closes": "23:00"}],
    [{"weekday": "thu", "opens": "18:00"}],
    [{"opens": "18:00", "closes": "23:00"}],
    ["thu"],
])
def test_opening_hours_follow_stage_one(client, manager, hours):
    response = publish(client, manager["token"], policy_body(opening_hours=hours))
    assert error_of(response)["code"] == "validation_failed", repr(hours)


def test_a_policy_may_close_the_room_entirely(client, manager):
    """An empty list of windows is not an invalid policy: a day with no window is closed."""
    assert_ok(publish(client, manager["token"], policy_body(opening_hours=[])), 201)
    assert availability(client, 2)["slots"] == []


@pytest.mark.parametrize("capacities", [
    {"t_1": 2},                       # a table missing
    {"t_1": 2, "t_2": 4, "t_3": 6},   # a table this restaurant does not have
    {}, "not an object", ["t_1"], None,
    {"t_1": 0, "t_2": 4}, {"t_1": 101, "t_2": 4},
    {"t_1": "2", "t_2": 4}, {"t_1": True, "t_2": 4}, {"t_1": 2.5, "t_2": 4},
])
def test_capacities_name_exactly_this_restaurants_tables(client, manager, capacities):
    response = publish(client, manager["token"], policy_body(capacities=capacities))
    assert error_of(response)["code"] == "validation_failed", repr(capacities)


def test_capacity_bounds_are_inclusive(client, manager):
    assert_ok(publish(client, manager["token"],
                      policy_body(capacities={"t_1": 1, "t_2": 100})), 201)


def test_a_policy_body_that_is_not_an_object_is_malformed(client, manager):
    response = client.post("/restaurants/r_anker/policies", json=[policy_body()],
                           headers=headers_for(manager["token"], "k"))
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"


# --------------------------------------------------------------------------- #
# what a published policy decides
# --------------------------------------------------------------------------- #
def test_a_booking_on_the_effective_date_uses_the_new_policy(client, manager):
    token = manager["token"]
    diner = signup(client, email="diner@example.com")["token"]
    assert_ok(publish(client, token, policy_body(effective_from=THURSDAY,
                                                 reservation_duration_minutes=60)), 201)
    body = assert_ok(book(client, diner, table_id="t_2", party_size=4), 201)
    assert body["accepted_terms"]["policy_version"] == 1
    assert body["accepted_terms"]["reservation_duration_minutes"] == 60
    assert body["ends_at"].endswith("20:00:00+02:00"), body["ends_at"]


def test_a_booking_before_the_effective_date_uses_the_seeded_rules(client, manager):
    token = manager["token"]
    diner = signup(client, email="diner@example.com")["token"]
    # The restaurant opens Thursday and Friday; a policy from Friday onward leaves
    # Thursday to policy 0.
    assert_ok(publish(client, token, policy_body(effective_from=FRIDAY,
                                                 reservation_duration_minutes=60)), 201)
    body = assert_ok(book(client, diner, table_id="t_2", party_size=4), 201)
    assert body["accepted_terms"]["policy_version"] == 0
    assert body["accepted_terms"]["reservation_duration_minutes"] == 90


def test_the_greatest_effective_date_not_later_than_the_booking_wins(client, manager):
    token = manager["token"]
    diner = signup(client, email="diner@example.com")["token"]
    assert_ok(publish(client, token, policy_body(effective_from="2026-09-25",
                                                 slot_minutes=30)), 201)   # Friday
    assert_ok(publish(client, token, policy_body(effective_from="2026-09-24",
                                                 slot_minutes=60)), 201)   # Thursday
    # Thursday the 24th: only version 2 is in time, and it wins on its date.
    thursday = assert_ok(book(client, diner, table_id="t_2", party_size=4,
                              starts_at_local=f"{THURSDAY}T19:00"), 201)
    assert thursday["accepted_terms"]["policy_version"] == 2
    # Friday the 25th: both are in time, and the later effective date wins.
    friday = assert_ok(book(client, diner, table_id="t_2", party_size=4,
                            starts_at_local=f"{FRIDAY}T19:00"), 201)
    assert friday["accepted_terms"]["policy_version"] == 1


def test_a_tie_on_the_effective_date_goes_to_the_greater_version(client, manager):
    token = manager["token"]
    diner = signup(client, email="diner@example.com")["token"]
    assert_ok(publish(client, token, policy_body(effective_from=THURSDAY,
                                                 reservation_duration_minutes=60)), 201)
    assert_ok(publish(client, token, policy_body(effective_from=THURSDAY,
                                                 reservation_duration_minutes=120)), 201)
    body = assert_ok(book(client, diner, table_id="t_2", party_size=4), 201)
    assert body["accepted_terms"]["policy_version"] == 2
    assert body["accepted_terms"]["reservation_duration_minutes"] == 120


def test_a_policy_can_change_the_grid(client, manager):
    token = manager["token"]
    before = [s["starts_at_local"][-5:] for s in availability(client, 2)["slots"]]
    assert before == ["18:00", "18:30", "19:00", "19:30", "20:00", "20:30", "21:00", "21:30"]
    assert_ok(publish(client, token, policy_body(slot_minutes=60)), 201)
    after = [s["starts_at_local"][-5:] for s in availability(client, 2)["slots"]]
    assert after == ["18:00", "19:00", "20:00", "21:00"]


def test_a_policy_can_change_the_capacity_of_a_table(client, manager):
    token = manager["token"]
    diner = signup(client, email="diner@example.com")["token"]
    assert_ok(publish(client, token, policy_body(capacities={"t_1": 2, "t_2": 2})), 201)
    slot = availability(client, 4)["slots"][2]
    assert slot["available_table_ids"] == []
    assert error_of(book(client, diner, table_id="t_2", party_size=4))["code"] == \
        "party_exceeds_capacity"
    assert_ok(book(client, diner, table_id="t_2", party_size=2), 201)


def test_a_policy_can_grow_a_table_and_a_combination_with_it(client, manager):
    fixture = managed_fixture()
    fixture["restaurants"][0]["combinable"] = [["t_1", "t_2"]]
    reset(client, fixture)
    token = token_of(login_headers(client))
    diner = signup(client, email="diner@example.com")["token"]
    pair = {"restaurant_id": "r_anker", "table_ids": ["t_1", "t_2"],
            "starts_at_local": f"{THURSDAY}T19:00", "party_size": 7}
    refused = client.post("/reservations", json=pair, headers=headers_for(diner, "too-big"))
    assert error_of(refused)["code"] == "party_exceeds_capacity"
    assert_ok(publish(client, token, policy_body(capacities={"t_1": 4, "t_2": 4})), 201)
    body = assert_ok(client.post("/reservations", json=pair,
                                 headers=headers_for(diner, "now-fits")), 201)
    assert body["accepted_terms"]["capacities"] == {"t_1": 4, "t_2": 4}


def test_a_policy_can_close_a_day(client, manager):
    token = manager["token"]
    assert availability(client, 2)["slots"], "the fixture serves Thursday"
    assert_ok(publish(client, token, policy_body(opening_hours=[
        {"weekday": "fri", "opens": "18:00", "closes": "23:30"}])), 201)
    assert availability(client, 2, date=THURSDAY)["slots"] == []
    assert availability(client, 2, date=FRIDAY)["slots"]


def test_a_policy_can_change_the_sitting_length_and_so_the_occupancy(client, manager):
    token = manager["token"]
    diner = signup(client, email="diner@example.com")["token"]
    assert_ok(publish(client, token, policy_body(reservation_duration_minutes=60)), 201)
    assert_ok(book(client, diner, table_id="t_2", party_size=4,
                   starts_at_local=f"{THURSDAY}T19:00"), 201)
    # 60 minutes from 19:00 does not overlap 20:00, though 90 minutes would.
    assert_ok(book(client, diner, table_id="t_2", party_size=4,
                   starts_at_local=f"{THURSDAY}T20:00"), 201)


def test_publishing_never_edits_a_booking_already_made(client, manager):
    token = manager["token"]
    diner = signup(client, email="diner@example.com")["token"]
    before = assert_ok(book(client, diner, table_id="t_2", party_size=4), 201)
    assert_ok(publish(client, token, policy_body(reservation_duration_minutes=30,
                                                 capacities={"t_1": 1, "t_2": 1},
                                                 slot_minutes=15)), 201)
    after = assert_ok(client.get(f"/reservations/{before['reference']}",
                                 headers=headers_for(diner)), 200)
    assert after == before, "an existing booking keeps its times, terms and revision"


def test_a_policy_is_selected_by_the_local_start_date_not_the_publication_time(
        client, manager):
    token = manager["token"]
    diner = signup(client, email="diner@example.com")["token"]
    assert_ok(publish(client, token, policy_body(effective_from=FRIDAY,
                                                 reservation_duration_minutes=45)), 201)
    thursday = assert_ok(book(client, diner, table_id="t_1", party_size=2,
                              starts_at_local=f"{THURSDAY}T19:00"), 201)
    friday = assert_ok(book(client, diner, table_id="t_1", party_size=2,
                            starts_at_local=f"{FRIDAY}T19:00"), 201)
    assert thursday["accepted_terms"]["policy_version"] == 0
    assert friday["accepted_terms"]["policy_version"] == 1
    assert friday["ends_at"].endswith("19:45:00+02:00")


def test_a_policy_applies_to_amendments_by_their_resulting_date(client, manager):
    token = manager["token"]
    diner = signup(client, email="diner@example.com")["token"]
    booking = assert_ok(book(client, diner, table_id="t_2", party_size=4,
                             starts_at_local=f"{THURSDAY}T19:00"), 201)
    assert booking["accepted_terms"]["policy_version"] == 0
    assert_ok(publish(client, token, policy_body(effective_from=FRIDAY,
                                                 reservation_duration_minutes=60)), 201)
    moved = assert_ok(client.patch(f"/reservations/{booking['reference']}",
                                   json={"starts_at_local": f"{FRIDAY}T19:00"},
                                   headers=headers_for(diner)), 200)
    assert moved["accepted_terms"]["policy_version"] == 1
    assert moved["ends_at"].endswith("20:00:00+02:00")


def test_a_closed_saturday_is_still_closed_after_a_policy_for_other_days(client, manager):
    assert_ok(publish(client, manager["token"], policy_body()), 201)
    assert availability(client, 2, date=SATURDAY)["slots"] == []
