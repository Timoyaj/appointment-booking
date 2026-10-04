"""Getting back into an account, and proving an address is real.

Password reset is the one place where a bug is an account takeover, so the tests
below are about the properties that make it safe rather than about the wording of
a response: the same answer whether or not the address exists, a link that works
once and expires, sessions that do not survive a reset, and tokens that are stored
in a form that is useless to whoever reads the database.
"""

from __future__ import annotations

import hashlib

import pytest

from tablekeeper import clock

from .conftest import (
    ADA,
    NOW,
    assert_ok,
    book,
    error_of,
    export,
    headers_for,
    signup,
)


def drain_all(client) -> list[dict]:
    """Every message this call delivered, as the recorder saw them."""
    return assert_ok(client.post("/_test/notifications/drain"), 200)["messages"]


def message_of(client, kind: str, to_email: str | None = None) -> dict:
    delivered = [m for m in drain_all(client) if m["kind"] == kind]
    if to_email is not None:
        delivered = [m for m in delivered if m["to_email"] == to_email]
    assert delivered, f"no {kind} message was sent"
    return delivered[-1]


def token_from(body: str) -> str:
    """The code the message carries: the only indented line in it."""
    coded = [line.strip() for line in body.splitlines() if line.startswith("    ") and line.strip()]
    assert coded, body
    return coded[0]


# --------------------------------------------------------------------------- #
# confirming an address
# --------------------------------------------------------------------------- #
def test_signing_up_asks_the_address_to_confirm_itself(client, seeded):
    body = signup(client, "newcomer@example.com")
    session = assert_ok(client.get("/auth/session", headers=headers_for(body["token"])), 200)
    assert session["email_verified"] is False

    message = message_of(client, "email_verification", "newcomer@example.com")
    assert "Confirm your Tablekeeper email address" == message["subject"]
    token = token_from(message["body"])

    confirmed = assert_ok(client.post("/auth/verify-email", json={"token": token}), 200)
    assert confirmed["email_verified"] is True
    assert assert_ok(
        client.get("/auth/session", headers=headers_for(body["token"])), 200
    )["email_verified"] is True


def test_a_confirmation_link_works_once(client, seeded):
    signup(client, "once@example.com")
    token = token_from(message_of(client, "email_verification", "once@example.com")["body"])
    assert_ok(client.post("/auth/verify-email", json={"token": token}), 200)
    response = client.post("/auth/verify-email", json={"token": token})
    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthenticated"


def test_asking_again_replaces_the_old_link(client, seeded):
    body = signup(client, "again@example.com")
    first = token_from(message_of(client, "email_verification", "again@example.com")["body"])
    headers = headers_for(body["token"])
    assert assert_ok(client.post("/auth/verify-email/resend", headers=headers), 200) == {
        "sent": True, "email_verified": False,
    }
    second = token_from(message_of(client, "email_verification", "again@example.com")["body"])
    assert second != first
    assert client.post("/auth/verify-email", json={"token": first}).status_code == 401
    assert_ok(client.post("/auth/verify-email", json={"token": second}), 200)


def test_a_confirmed_address_is_not_asked_again(client, seeded):
    body = signup(client, "done@example.com")
    token = token_from(message_of(client, "email_verification", "done@example.com")["body"])
    assert_ok(client.post("/auth/verify-email", json={"token": token}), 200)
    assert assert_ok(
        client.post("/auth/verify-email/resend", headers=headers_for(body["token"])), 200
    ) == {"sent": False, "email_verified": True}


def test_a_link_nobody_could_have_guessed_is_not_accepted(client, seeded):
    response = client.post("/auth/verify-email", json={"token": "not-a-real-token"})
    assert response.status_code == 401
    assert client.post("/auth/verify-email", json={}).status_code == 422


def test_a_restaurant_can_insist_on_a_confirmed_address(client, seeded, monkeypatch):
    """Off by default; a shop that turns it on will not seat an unconfirmed diner."""
    body = signup(client, "unconfirmed@example.com")
    token = body["token"]
    assert assert_ok(book(client, token), 201)

    monkeypatch.setenv("TABLEKEEPER_REQUIRE_VERIFIED_EMAIL", "1")
    booking = {
        "restaurant_id": "r_anker", "table_id": "t_2",
        "starts_at_local": "2026-09-24T21:00", "party_size": 4,
    }
    refused = client.post(
        "/reservations", json=booking, headers=headers_for(token, "strict-key")
    )
    assert refused.status_code == 403
    assert error_of(refused)["code"] == "email_not_verified"

    confirmation = token_from(
        message_of(client, "email_verification", "unconfirmed@example.com")["body"]
    )
    assert_ok(client.post("/auth/verify-email", json={"token": confirmation}), 200)
    # The refusal spent its idempotency key, exactly like an accepted request
    # would have: a retry has to be a new attempt.
    assert_ok(
        client.post(
            "/reservations", json=booking, headers=headers_for(token, "strict-key-2")
        ),
        201,
    )


# --------------------------------------------------------------------------- #
# getting back in
# --------------------------------------------------------------------------- #
def test_an_unknown_address_is_answered_exactly_like_a_known_one(client, seeded):
    """Otherwise the endpoint tells anybody who asks which guests have accounts."""
    known = client.post("/auth/password-reset", json={"email": ADA["email"]})
    unknown = client.post("/auth/password-reset", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    # And the unknown one wrote nothing at all.
    kinds = [m["kind"] for m in drain_all(client)]
    assert kinds.count("password_reset") == 1


def test_a_reset_link_changes_the_password_and_signs_every_device_out(client, seeded):
    body = signup(client, "lost@example.com", "old password here", "Lost")
    phone = assert_ok(
        client.post("/auth/login", json={"email": "lost@example.com", "password": "old password here"}),
        200,
    )["token"]
    laptop = body["token"]
    assert_ok(client.post("/auth/password-reset", json={"email": "lost@example.com"}), 202)
    token = token_from(message_of(client, "password_reset", "lost@example.com")["body"])

    changed = assert_ok(
        client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "new_password": "a brand new password"},
        ),
        200,
    )
    assert changed["password_changed"] is True
    assert changed["sessions_revoked"] == 2  # both devices were signed in

    # Both old sessions are refused, the old password is refused, the new one works.
    for revoked in (phone, laptop):
        assert client.get("/auth/session", headers=headers_for(revoked)).status_code == 401
    assert client.post("/auth/login", json={
        "email": "lost@example.com", "password": "old password here",
    }).status_code == 401
    assert_ok(client.post("/auth/login", json={
        "email": "lost@example.com", "password": "a brand new password",
    }), 200)


def test_a_reset_link_is_spent_by_the_first_use(client, seeded):
    signup(client, "spent@example.com")
    assert_ok(client.post("/auth/password-reset", json={"email": "spent@example.com"}), 202)
    token = token_from(message_of(client, "password_reset", "spent@example.com")["body"])
    assert_ok(client.post("/auth/password-reset/confirm", json={
        "token": token, "new_password": "the first new one"}), 200)
    response = client.post("/auth/password-reset/confirm", json={
        "token": token, "new_password": "the second new one"})
    assert response.status_code == 401
    assert "already been used" in response.text
    # The first new password still stands.
    assert_ok(client.post("/auth/login", json={
        "email": "spent@example.com", "password": "the first new one"}), 200)


def test_asking_for_another_link_retires_the_last_one(client, seeded):
    signup(client, "twice@example.com")
    for _ in range(2):
        assert_ok(client.post("/auth/password-reset", json={"email": "twice@example.com"}), 202)
    delivered = [m for m in drain_all(client) if m["kind"] == "password_reset"]
    assert len(delivered) == 2, "the second request must still send an email"
    old, new = (token_from(m["body"]) for m in delivered)
    assert client.post("/auth/password-reset/confirm", json={
        "token": old, "new_password": "must not work"}).status_code == 401
    assert_ok(client.post("/auth/password-reset/confirm", json={
        "token": new, "new_password": "this one does"}), 200)


def test_a_link_stops_working_when_its_time_is_up(client, seeded):
    signup(client, "slow@example.com")
    assert_ok(client.post("/auth/password-reset", json={"email": "slow@example.com"}), 202)
    token = token_from(message_of(client, "password_reset", "slow@example.com")["body"])
    # The default life is an hour; step the clock past it.
    clock.freeze(NOW.replace(hour=NOW.hour + 2))
    response = client.post("/auth/password-reset/confirm", json={
        "token": token, "new_password": "too late now"})
    assert response.status_code == 401
    assert "expired" in response.text


def test_a_password_that_breaks_the_rules_does_not_spend_the_link(client, seeded):
    signup(client, "picky@example.com")
    assert_ok(client.post("/auth/password-reset", json={"email": "picky@example.com"}), 202)
    token = token_from(message_of(client, "password_reset", "picky@example.com")["body"])
    response = client.post("/auth/password-reset/confirm", json={
        "token": token, "new_password": "short"})
    assert response.status_code == 422
    # The link was not burned by the attempt, so the diner can try again.
    assert_ok(client.post("/auth/password-reset/confirm", json={
        "token": token, "new_password": "a password that passes"}), 200)


def test_the_database_holds_no_usable_link(client, seeded):
    """A snapshot that leaks must not hand anybody a working reset.

    A message that is still waiting to be delivered necessarily holds the code —
    that is what is about to be sent. The credential table never does, and once
    the transport has taken the message the outbox does not either.
    """
    signup(client, "leaky@example.com")
    assert_ok(client.post("/auth/password-reset", json={"email": "leaky@example.com"}), 202)
    token = token_from(message_of(client, "password_reset", "leaky@example.com")["body"])

    snapshot = export(client)
    stored = snapshot["state"]["password_resets"]
    assert len(stored) == 1
    assert stored[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(stored), "the token itself is in the credential table"

    after = export(client)
    assert after["state"]["notifications"][0]["status"] == "sent"
    assert token not in str(after["state"]), "a sent message still carries the code"


def test_a_missing_field_is_a_client_error_not_a_crash(client, seeded):
    assert client.post("/auth/password-reset", json={}).status_code == 422
    # A number is the wrong shape, and this service answers 400 for those.
    assert client.post("/auth/password-reset", json={"email": 12}).status_code == 400
    assert client.post("/auth/password-reset/confirm", json={"token": "x"}).status_code == 422
    assert client.post("/auth/verify-email", json={}).status_code == 422
