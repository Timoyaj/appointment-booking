"""Authentication: signup, login, bearer tokens and which endpoints are public."""

from __future__ import annotations

import pytest

from tests.conftest import (
    ADA,
    assert_ok,
    base_fixture,
    error_of,
    headers_for,
    reset,
    signup,
)


def test_signup_returns_a_token(client, seeded):
    body = assert_ok(
        client.post("/auth/signup", json={
            "email": "diner@example.com", "password": "long enough",
            "display_name": "Diner"}),
        201,
    )
    assert set(body) == {"user_id", "display_name", "token"}
    assert body["display_name"] == "Diner"
    assert body["user_id"]
    assert body["token"]


def test_signup_unknown_fields_are_ignored(client, seeded):
    body = assert_ok(
        client.post("/auth/signup", json={
            "email": "diner@example.com", "password": "long enough",
            "display_name": "Diner", "newsletter": True, "age": 30}),
        201,
    )
    assert body["display_name"] == "Diner"


def test_duplicate_email_is_a_conflict(client, seeded):
    signup(client, "dup@example.com")
    response = client.post("/auth/signup", json={
        "email": "dup@example.com", "password": "long enough", "display_name": "Two"})
    assert response.status_code == 409
    assert error_of(response)["code"] == "email_taken"


def test_duplicate_email_of_a_seeded_user_is_a_conflict(client, seeded):
    response = client.post("/auth/signup", json={
        "email": ADA["email"], "password": "long enough", "display_name": "Impostor"})
    assert response.status_code == 409
    assert error_of(response)["code"] == "email_taken"


@pytest.mark.parametrize("email", ["", "nope", "a@", "@b", "a b@c.com", "a@@b.com",
                                   "a@b c", 42, None])
def test_bad_email_is_validation_failed(client, seeded, email):
    response = client.post("/auth/signup", json={
        "email": email, "password": "long enough", "display_name": "D"})
    if isinstance(email, str):
        assert response.status_code == 422
        assert error_of(response)["code"] == "validation_failed"
    else:
        assert response.status_code == 400
        assert error_of(response)["code"] == "malformed_request"


@pytest.mark.parametrize("password", ["", "short", "1234567", None, 12345678])
def test_short_or_wrong_type_password(client, seeded, password):
    response = client.post("/auth/signup", json={
        "email": "p@example.com", "password": password, "display_name": "D"})
    if isinstance(password, str) or password is None:
        assert response.status_code == 422
        assert error_of(response)["code"] == "validation_failed"
    else:
        assert response.status_code == 400
        assert error_of(response)["code"] == "malformed_request"


def test_exactly_eight_characters_is_allowed(client, seeded):
    assert_ok(client.post("/auth/signup", json={
        "email": "eight@example.com", "password": "12345678", "display_name": "E"}), 201)


def test_missing_display_name_is_validation_failed(client, seeded):
    response = client.post("/auth/signup", json={
        "email": "d@example.com", "password": "long enough"})
    assert response.status_code == 422
    assert error_of(response)["code"] == "validation_failed"


def test_signup_body_must_be_a_json_object(client, seeded):
    response = client.post("/auth/signup", content=b"[]",
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"

    response = client.post("/auth/signup", content=b"not json",
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"


def test_login_returns_a_token(client, seeded):
    body = assert_ok(client.post("/auth/login", json={
        "email": ADA["email"], "password": ADA["password"]}), 200)
    assert body["user_id"] == "u_ada"
    assert body["display_name"] == "Ada"
    assert body["token"]


@pytest.mark.parametrize("email,password", [
    (ADA["email"], "wrong password"),
    ("nobody@example.com", ADA["password"]),
    ("nobody@example.com", "wrong password"),
])
def test_bad_login_is_unauthenticated(client, seeded, email, password):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthenticated"


def test_login_for_a_signed_up_user(client, seeded):
    signup(client, "new@example.com", "their password", "New Diner")
    body = assert_ok(client.post("/auth/login", json={
        "email": "new@example.com", "password": "their password"}), 200)
    assert body["display_name"] == "New Diner"


def test_passwords_are_not_stored_in_plaintext(client, seeded):
    signup(client, "hash@example.com", "correct horse battery", "Hash")
    state = assert_ok(client.get("/_test/export"), 200)["state"]
    stored = next(u for u in state["users"] if u["email"] == "hash@example.com")
    assert "correct horse battery" not in stored["password"]
    assert stored["password"].startswith("scrypt$")
    # ...but the password still works.
    assert_ok(client.post("/auth/login", json={
        "email": "hash@example.com", "password": "correct horse battery"}), 200)


def test_an_account_may_hold_several_valid_tokens(client, seeded):
    tokens = [signup(client, "multi@example.com")["token"]]
    for _ in range(2):
        tokens.append(client.post("/auth/login", json={
            "email": "multi@example.com", "password": "long enough"}).json()["token"])
    assert len(set(tokens)) == 3
    for token in tokens:
        assert_ok(client.get("/reservations", headers=headers_for(token)), 200)


@pytest.mark.parametrize("authorization", [
    None, "", "Bearer", "Bearer ", "bearer", "Token abc", "abc",
    "Bearer not-a-real-token", "Bearer " + "x" * 40,
])
def test_bad_bearer_tokens_are_unauthenticated(client, seeded, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    response = client.get("/reservations", headers=headers)
    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthenticated"


def test_bearer_scheme_is_case_insensitive(client, seeded):
    token = signup(client)["token"]
    assert_ok(client.get("/reservations", headers={"Authorization": f"bearer {token}"}), 200)
    assert_ok(client.get("/reservations", headers={"Authorization": f"BEARER {token}"}), 200)


def test_public_endpoints_need_no_token(client, seeded):
    assert_ok(client.get("/restaurants"), 200)
    assert_ok(client.get("/restaurants/r_anker"), 200)
    assert_ok(client.get("/availability", params={
        "restaurant_id": "r_anker", "date": "2026-09-24", "party_size": 2}), 200)
    assert_ok(client.get("/health"), 200)


def test_protected_endpoints_need_a_token(client, seeded):
    # The body parses before the caller is authenticated, so a well-formed body
    # with no token is 401 rather than a field error.
    assert client.get("/reservations").status_code == 401
    assert client.post("/reservations", json={}).status_code == 401
    assert client.get("/reservations/ANYREF").status_code == 401
    assert client.post("/reservations/ANYREF/cancel").status_code == 401
    assert client.patch("/reservations/ANYREF", json={}).status_code == 401
    assert client.post("/reservation-moves", json={"moves": []}).status_code == 401


def test_test_control_endpoints_need_no_token(client, seeded):
    assert_ok(client.get("/_test/export"), 200)
    assert_ok(client.post("/_test/reset", json=base_fixture()), 204)
    exported = client.get("/_test/export").json()
    assert_ok(client.post("/_test/import", json=exported), 204)


def test_tokens_survive_a_reset_of_other_data_but_not_a_reset(client, seeded):
    token = signup(client)["token"]
    assert_ok(client.get("/reservations", headers=headers_for(token)), 200)
    reset(client, base_fixture())
    assert client.get("/reservations", headers=headers_for(token)).status_code == 401
