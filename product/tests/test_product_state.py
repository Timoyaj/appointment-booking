"""Snapshots taken after deposits and account recovery existed.

The engine's own round-trip tests cover the service as it was; these cover what
the product layer added on top. Two things matter to a restaurant that backs
itself up: a snapshot taken today must restore everything it had — including
money and a live reset link — and a snapshot taken *before* these tables existed
must still load, because an upgrade must not lock somebody out of their own data.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from tablekeeper.api import create_app

from .conftest import (
    FRIDAY,
    THURSDAY,
    assert_ok,
    book,
    error_of,
    headers_for,
    reset,
    signup,
)
from .test_payments import DEPOSIT
from .test_product import open_restaurant


@contextmanager
def other_client(tmp_path, name="product-destination"):
    app = create_app(database_path=str(tmp_path / f"{name}.db"))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def token_from(body: str) -> str:
    coded = [line.strip() for line in body.splitlines()
             if line.startswith("    ") and line.strip()]
    assert coded, body
    return coded[0]


def drain_all(client) -> list[dict]:
    return assert_ok(client.post("/_test/notifications/drain"), 200)["messages"]


def message_of(client, kind: str) -> dict:
    delivered = [m for m in drain_all(client) if m["kind"] == kind]
    assert delivered, f"no {kind} message was sent"
    return delivered[-1]


def load(client):
    """A restaurant taking deposits, with one no-show that kept its money, one
    cancelled booking whose hold was released, and a live password reset."""
    owner = signup(client, "keeper@example.com", "long enough", "Keeper")
    restaurant = open_restaurant(client, owner["token"])
    headers = headers_for(owner["token"])
    assert_ok(client.put(
        f"/restaurants/{restaurant['id']}/payment-settings", json=DEPOSIT, headers=headers
    ), 200)

    diner = signup(client, "guest@example.com")["token"]
    kept = assert_ok(book(
        client, diner, restaurant_id=restaurant["id"], table_id="t_2", party_size=4,
        starts_at_local=f"{THURSDAY}T19:00", extra={"payment_method_id": "pm_visa"},
    ), 201)
    given_back = assert_ok(book(
        client, diner, restaurant_id=restaurant["id"], table_id="t_2", party_size=4,
        starts_at_local=f"{FRIDAY}T19:00", extra={"payment_method_id": "pm_visa"},
    ), 201)
    assert_ok(client.post(
        f"/reservations/{kept['reference']}/no-show", headers=headers
    ), 200)
    assert_ok(client.post(
        f"/reservations/{given_back['reference']}/cancel", headers=headers_for(diner)
    ), 200)

    # A card that was refused: no booking, but the attempt is part of the record
    # a restaurant keeps about how often its deposit rule turns people away.
    declined = client.post(
        "/reservations",
        json={"restaurant_id": restaurant["id"], "table_id": "t_2",
              "starts_at_local": f"{FRIDAY}T21:00", "party_size": 4,
              "payment_method_id": "pm_decline"},
        headers=headers_for(diner, "declined-1"),
    )
    assert declined.status_code == 402

    assert_ok(client.post("/auth/password-reset", json={"email": "guest@example.com"}), 202)
    reset_link = token_from(message_of(client, "password_reset")["body"])
    return {
        "restaurant_id": restaurant["id"],
        "headers": headers,
        "diner": diner,
        "reset_link": reset_link,
    }


def test_a_snapshot_carries_the_money_and_the_ledger(client, seeded, tmp_path):
    room = load(client)
    snapshot = assert_ok(client.get("/_test/export"), 200)
    state = snapshot["state"]
    for table in ("restaurant_payment_settings", "payment_intents", "payment_events",
                  "payment_attempts"):
        assert state[table], f"{table} is missing from the snapshot"

    with other_client(tmp_path) as destination:
        assert_ok(destination.post("/_test/import", json=snapshot), 204)
        assert destination.get("/_test/export").json()["state"] == state

        settings = assert_ok(destination.get(
            f"/restaurants/{room['restaurant_id']}/payment-settings", headers=room["headers"]
        ), 200)
        assert settings["deposits"] is True
        assert settings["deposit_per_seat_cents"] == DEPOSIT["deposit_per_seat_cents"]

        summary = assert_ok(destination.get(
            f"/restaurants/{room['restaurant_id']}/reports/summary"
            f"?from=2026-09-01&to=2026-09-30",
            headers=room["headers"],
        ), 200)
        # The deposit that was kept came back, and the one that was released did
        # not become revenue in the move.
        assert summary["money"]["captured_cents"] == 4000
        assert summary["money"]["declined"] == 1, "the refusal survived the move"

        def kinds_for(reference: str) -> list[str]:
            body = assert_ok(destination.get(
                f"/reservations/{reference}/payments", headers=room["headers"]
            ), 200)
            return [event["kind"] for intent in body["intents"] for event in intent["events"]]

        kept, given_back = state["payment_intents"]
        assert state["payment_attempts"][0]["outcome"] == "declined"
        assert kept["status"] == "captured" and "captured" in kinds_for(kept["reference"])
        assert given_back["status"] == "released" and "released" in kinds_for(
            given_back["reference"]
        )


def test_a_restored_reset_link_still_works(client, seeded, tmp_path):
    """A restored account is the same account: its pending links are still its own."""
    room = load(client)
    snapshot = assert_ok(client.get("/_test/export"), 200)

    with other_client(tmp_path) as destination:
        assert_ok(destination.post("/_test/import", json=snapshot), 204)
        changed = assert_ok(destination.post(
            "/auth/password-reset/confirm",
            json={"token": room["reset_link"], "new_password": "restored and changed"},
        ), 200)
        assert changed["password_changed"] is True
        assert_ok(destination.post("/auth/login", json={
            "email": "guest@example.com", "password": "restored and changed",
        }), 200)


def test_a_snapshot_from_before_these_tables_existed_still_loads(client, seeded, tmp_path):
    """An upgrade must not refuse the backup somebody took last week."""
    load(client)
    snapshot = assert_ok(client.get("/_test/export"), 200)
    for table in ("restaurant_payment_settings", "payment_intents", "payment_events",
                  "payment_attempts", "password_resets", "email_verifications",
                  "email_verified"):
        snapshot["state"].pop(table)

    with other_client(tmp_path) as destination:
        assert_ok(destination.post("/_test/import", json=snapshot), 204)
        # And the destination simply has none of it, rather than half of it.
        assert destination.get("/_test/export").json()["state"]["payment_intents"] == []
        assert_ok(destination.post(
            "/auth/login", json={"email": "guest@example.com", "password": "long enough"}
        ), 200)


def test_a_snapshot_that_names_a_column_that_does_not_exist_is_refused(client, seeded):
    """Silently dropping a field would restore something other than the backup."""
    snapshot = assert_ok(client.get("/_test/export"), 200)
    snapshot["state"]["payment_intents"].append({
        "id": "pi_invented", "restaurant_id": "r_anker", "reference": "X",
        "amount_cents": 4000, "currency": "eur", "status": "authorized",
        "provider": "fake", "created_at": "2026-09-21T11:04:03+00:00",
        "surprise": "nobody asked for this",
    })
    response = client.post("/_test/import", json=snapshot)
    assert response.status_code == 422
    assert "unknown column" in response.text
    assert error_of(response)["code"] == "validation_failed"
