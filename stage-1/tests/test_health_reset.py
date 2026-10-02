"""Health, reset and the general HTTP conventions."""

from __future__ import annotations

import pytest

from tests.conftest import ADA, assert_ok, base_fixture, error_of, reset, signup


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_needs_no_authentication_and_no_state(client):
    """Healthy before any fixture is loaded."""
    assert client.get("/health").json() == {"status": "ok"}


def test_json_content_type(client, seeded):
    response = client.get("/restaurants")
    assert response.headers["content-type"] == "application/json; charset=utf-8"


def test_reset_returns_204_with_no_body(client):
    response = client.post("/_test/reset", json=base_fixture())
    assert response.status_code == 204
    assert response.content == b""


def test_reset_replaces_all_state(client, seeded):
    token = signup(client)["token"]
    created = client.post(
        "/reservations",
        json={"restaurant_id": "r_anker", "table_id": "t_2",
              "starts_at_local": "2026-09-24T19:00", "party_size": 4},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"},
    )
    assert created.status_code == 201

    # A different fixture: the old restaurant, booking and token are all gone.
    other = base_fixture()
    other["restaurants"][0]["id"] = "r_other"
    other["restaurants"][0]["name"] = "Other Place"
    reset(client, other)

    assert client.get("/restaurants").json() == {
        "restaurants": [{"id": "r_other", "name": "Other Place",
                         "timezone": "Europe/Berlin"}]
    }
    assert client.get("/restaurants/r_anker").status_code == 404
    assert client.get("/reservations",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert client.get(f"/reservations/{created.json()['reference']}",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_repeated_resets_are_supported(client, seeded):
    fixture = base_fixture()
    for _ in range(3):
        reset(client, fixture)
    assert len(client.get("/restaurants").json()["restaurants"]) == 1
    assert client.get("/restaurants/r_anker").status_code == 200


def test_reset_clears_previous_users_and_tokens(client, seeded):
    token = signup(client)["token"]
    assert client.get("/reservations",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 200

    reset(client, base_fixture())
    response = client.get("/reservations", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthenticated"


def test_seeded_users_can_log_in_immediately(client, seeded):
    response = client.post(
        "/auth/login", json={"email": ADA["email"], "password": ADA["password"]}
    )
    body = assert_ok(response, 200)
    assert body["user_id"] == "u_ada"
    assert body["display_name"] == "Ada"
    assert body["token"]


def test_reset_fixture_shape_is_preserved(client, seeded):
    body = assert_ok(client.get("/restaurants/r_anker"), 200)
    assert body == {
        "id": "r_anker",
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
    }


def test_unknown_fields_in_a_fixture_are_ignored(client):
    fixture = base_fixture()
    fixture["unexpected"] = True
    fixture["restaurants"][0]["notes"] = "whatever"
    fixture["restaurants"][0]["tables"][0]["shape"] = "round"
    fixture["users"][0]["nickname"] = "Ad"
    reset(client, fixture)
    assert client.get("/restaurants/r_anker").status_code == 200


def test_reset_rejects_unparseable_and_non_object_bodies(client):
    response = client.post(
        "/_test/reset", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"

    response = client.post("/_test/reset", json=[1, 2, 3])
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"


def test_reset_rejects_an_invalid_fixture_without_half_applying(client, seeded):
    broken = base_fixture()
    broken["restaurants"][0]["slot_minutes"] = "thirty"  # wrong JSON type
    response = client.post("/_test/reset", json=broken)
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"
    # The previous state is untouched.
    assert client.get("/restaurants/r_anker").status_code == 200

    missing = base_fixture()
    del missing["restaurants"][0]["cancellation_cutoff_minutes"]
    response = client.post("/_test/reset", json=missing)
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"
    assert client.get("/restaurants/r_anker").status_code == 200


def test_reset_rejects_an_unknown_timezone_or_bad_weekday(client, seeded):
    fixture = base_fixture()
    fixture["restaurants"][0]["timezone"] = "Mars/Olympus"
    response = client.post("/_test/reset", json=fixture)
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"

    fixture = base_fixture()
    fixture["restaurants"][0]["opening_hours"][0]["weekday"] = "thursday"
    response = client.post("/_test/reset", json=fixture)
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"


def test_reset_can_seed_reservations(client):
    fixture = base_fixture()
    fixture["reservations"] = [
        {"id": "res_seed_1", "reference": "SEED0001", "user_id": "u_ada",
         "restaurant_id": "r_anker", "table_id": "t_2",
         "starts_at_local": "2026-09-24T19:00", "party_size": 4}
    ]
    reset(client, fixture)

    headers = {"Authorization": "Bearer " + client.post(
        "/auth/login", json={"email": ADA["email"], "password": ADA["password"]}
    ).json()["token"]}
    listed = client.get("/reservations", headers=headers).json()["reservations"]
    assert len(listed) == 1
    assert listed[0]["reference"] == "SEED0001"
    assert listed[0]["status"] == "confirmed"
    assert listed[0]["starts_at"] == "2026-09-24T19:00:00+02:00"
    assert listed[0]["ends_at"] == "2026-09-24T20:30:00+02:00"

    # A seeded booking really occupies its table.
    taken = client.get("/availability", params={
        "restaurant_id": "r_anker", "date": "2026-09-24", "party_size": 4}).json()
    slot = next(s for s in taken["slots"] if s["starts_at_local"] == "2026-09-24T19:00")
    assert slot["available_table_ids"] == []


def test_unknown_route_uses_the_error_envelope(client, seeded):
    response = client.get("/nope")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


@pytest.mark.parametrize("reference", [
    "x", "ABCDE", "lower01", "TOO-LONG-WITH-DASH", "ABC-12", "WITH SPACE", "",
])
def test_reset_rejects_seeded_references_that_break_the_format(client, seeded, reference):
    """A reference is 6-12 characters of A-Z0-9 wherever it comes from."""
    fixture = base_fixture()
    fixture["reservations"] = [
        {"id": "res_seed_1", "reference": reference, "user_id": "u_ada",
         "restaurant_id": "r_anker", "table_id": "t_2",
         "starts_at_local": "2026-09-24T19:00", "party_size": 4}
    ]
    response = client.post("/_test/reset", json=fixture)
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"
    # ...and the previous state survives the rejection.
    assert client.get("/restaurants/r_anker").status_code == 200


@pytest.mark.parametrize("reference", ["SEED01", "ABCDEFGH12", "A1B2C3"])
def test_reset_accepts_well_formed_seeded_references(client, reference):
    fixture = base_fixture()
    fixture["reservations"] = [
        {"id": "res_seed_1", "reference": reference, "user_id": "u_ada",
         "restaurant_id": "r_anker", "table_id": "t_2",
         "starts_at_local": "2026-09-24T19:00", "party_size": 4}
    ]
    reset(client, fixture)
    assert client.get("/restaurants/r_anker").status_code == 200


def test_reset_rejects_duplicate_seeded_references(client, seeded):
    fixture = base_fixture()
    booking = {"id": "res_seed_1", "reference": "SEED01", "user_id": "u_ada",
               "restaurant_id": "r_anker", "table_id": "t_2",
               "starts_at_local": "2026-09-24T19:00", "party_size": 4}
    fixture["reservations"] = [dict(booking), {**booking, "id": "res_seed_2"}]
    response = client.post("/_test/reset", json=fixture)
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"
