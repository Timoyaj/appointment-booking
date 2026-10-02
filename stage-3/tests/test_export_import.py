"""Export and import (§10).

An export is an atomic read-only snapshot; an import replaces the destination
atomically and must accept an unchanged export from this service. Accounts,
hashed passwords, live tokens, reservations, references, timestamps and completed
idempotent receipts all survive the round trip.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from tablekeeper.api import create_app
from tests.conftest import (
    ADA,
    THURSDAY,
    assert_ok,
    base_fixture,
    book,
    error_of,
    headers_for,
    reset,
    signup,
)


@contextmanager
def other_client(tmp_path, name="destination"):
    app = create_app(database_path=str(tmp_path / f"{name}.db"))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def populate(client):
    """Load a fixture, sign someone up, and make a couple of bookings."""
    reset(client, base_fixture())
    token = signup(client, "mover@example.com", "long enough", "Mover")["token"]
    first = assert_ok(book(client, token, table_id="t_1", party_size=2,
                           starts_at_local=f"{THURSDAY}T19:00", key="export-1"), 201)
    second = assert_ok(book(client, token, table_id="t_2", party_size=4,
                            starts_at_local=f"{THURSDAY}T19:00", key="export-2"), 201)
    assert_ok(client.patch(f"/reservations/{second['reference']}",
                           json={"party_size": 3}, headers=headers_for(token)), 200)
    assert_ok(client.post(f"/reservations/{first['reference']}/cancel",
                          headers=headers_for(token)), 200)
    return token


def test_export_shape(client, seeded):
    body = assert_ok(client.get("/_test/export"), 200)
    assert body["track"] == "tablekeeper"
    assert body["format_version"] == 1
    assert isinstance(body["state"], dict)


def test_import_accepts_an_unchanged_export(client, seeded, tmp_path):
    populate(client)
    snapshot = assert_ok(client.get("/_test/export"), 200)

    with other_client(tmp_path) as destination:
        assert_ok(destination.post("/_test/import", json=snapshot), 204)
        assert destination.get("/_test/export").json()["state"] == snapshot["state"]

        # Fixtures, restaurants and tables came across.
        assert assert_ok(destination.get("/restaurants"), 200) == {
            "restaurants": [{"id": "r_anker", "name": "Zum Anker",
                             "timezone": "Europe/Berlin"}]}
        assert destination.get("/restaurants/r_anker").json()["tables"] == [
            {"id": "t_1", "label": "1", "capacity": 2},
            {"id": "t_2", "label": "2", "capacity": 4}]


def test_accounts_tokens_and_passwords_survive(client, seeded, tmp_path):
    token = populate(client)
    snapshot = assert_ok(client.get("/_test/export"), 200)

    with other_client(tmp_path) as destination:
        assert_ok(destination.post("/_test/import", json=snapshot), 204)

        # The imported bearer token still works.
        listed = assert_ok(destination.get("/reservations", headers=headers_for(token)), 200)
        assert len(listed["reservations"]) == 2

        # ...and the hashed password still logs in, producing a new valid token.
        body = assert_ok(destination.post("/auth/login", json={
            "email": "mover@example.com", "password": "long enough"}), 200)
        assert_ok(destination.get("/reservations",
                                  headers=headers_for(body["token"])), 200)

        # Passwords are not carried across in plaintext.
        stored = next(u for u in destination.get("/_test/export").json()["state"]["users"]
                      if u["email"] == "mover@example.com")
        assert "long enough" not in stored["password"]


def test_reservations_references_and_timestamps_are_not_regenerated(client, seeded, tmp_path):
    populate(client)
    snapshot = assert_ok(client.get("/_test/export"), 200)
    before = snapshot["state"]["reservations"]

    with other_client(tmp_path) as destination:
        assert_ok(destination.post("/_test/import", json=snapshot), 204)
        after = destination.get("/_test/export").json()["state"]["reservations"]
        assert after == before
        assert {r["reference"] for r in after} == {r["reference"] for r in before}
        assert {r["created_at"] for r in after} == {r["created_at"] for r in before}
        assert {r["status"] for r in after} == {"confirmed", "cancelled"}


def test_completed_idempotent_receipts_survive(client, seeded, tmp_path):
    token = populate(client)
    original = assert_ok(book(client, token, table_id="t_2", party_size=2,
                              starts_at_local=f"{THURSDAY}T21:00", key="receipt"), 201)
    snapshot = assert_ok(client.get("/_test/export"), 200)

    with other_client(tmp_path) as destination:
        assert_ok(destination.post("/_test/import", json=snapshot), 204)
        replay = destination.post(
            "/reservations",
            json={"restaurant_id": "r_anker", "table_id": "t_2",
                  "starts_at_local": f"{THURSDAY}T21:00", "party_size": 2},
            headers=headers_for(token, "receipt"))
        assert replay.status_code == 200
        assert replay.json() == original


def test_failed_request_keys_remain_reusable(client, seeded, tmp_path):
    token = populate(client)
    failed = book(client, token, table_id="t_nope", key="burnt", starts_at_local=f"{THURSDAY}T21:00")
    assert failed.status_code == 404
    snapshot = assert_ok(client.get("/_test/export"), 200)

    with other_client(tmp_path) as destination:
        assert_ok(destination.post("/_test/import", json=snapshot), 204)
        created = assert_ok(book(destination, token, table_id="t_2", party_size=2,
                                 starts_at_local=f"{THURSDAY}T21:00", key="burnt"), 201)
        assert created["table_id"] == "t_2"


def test_import_is_replacement_not_merge(client, seeded, tmp_path):
    source_token = populate(client)
    snapshot = assert_ok(client.get("/_test/export"), 200)

    with other_client(tmp_path) as destination:
        # The destination has its own, different state.
        reset(destination, base_fixture())
        destination_user = signup(destination, "dest@example.com")["token"]
        destination_booking = assert_ok(book(destination, destination_user), 201)

        assert_ok(destination.post("/_test/import", json=snapshot), 204)

        # Destination credentials and data are gone.
        assert destination.get("/reservations",
                               headers=headers_for(destination_user)).status_code == 401
        assert destination.get(f"/reservations/{destination_booking['reference']}",
                               headers=headers_for(source_token)).status_code == 404
        response = destination.post("/auth/login", json={
            "email": "dest@example.com", "password": "long enough"})
        assert response.status_code == 401
        assert error_of(response)["code"] == "unauthenticated"

        # Source state is present exactly once.
        listed = assert_ok(destination.get("/reservations",
                                           headers=headers_for(source_token)), 200)
        assert len(listed["reservations"]) == 2

        # Importing the same snapshot again duplicates nothing.
        assert_ok(destination.post("/_test/import", json=snapshot), 204)
        listed = assert_ok(destination.get("/reservations",
                                           headers=headers_for(source_token)), 200)
        assert len(listed["reservations"]) == 2
        assert destination.get("/_test/export").json()["state"] == snapshot["state"]


def test_export_is_a_snapshot_that_later_writes_do_not_change(client, seeded):
    token = populate(client)
    snapshot = assert_ok(client.get("/_test/export"), 200)

    assert_ok(book(client, token, table_id="t_2", party_size=2,
                   starts_at_local=f"{THURSDAY}T21:00"), 201)
    assert len(assert_ok(client.get("/reservations", headers=headers_for(token)), 200)[
        "reservations"]) == 3

    # Restoring the snapshot rewinds that write.
    assert_ok(client.post("/_test/import", json=snapshot), 204)
    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)
    assert len(listed["reservations"]) == 2


def test_reset_clears_imported_state(client, seeded, tmp_path):
    token = populate(client)
    snapshot = assert_ok(client.get("/_test/export"), 200)

    with other_client(tmp_path) as destination:
        assert_ok(destination.post("/_test/import", json=snapshot), 204)
        assert_ok(destination.post("/_test/reset", json=base_fixture()), 204)
        assert destination.get("/reservations", headers=headers_for(token)).status_code == 401
        assert destination.get("/_test/export").json()["state"]["reservations"] == []


@pytest.mark.parametrize("payload", [
    {},
    {"track": "tablekeeper"},
    {"track": "tablekeeper", "format_version": 1},
    {"format_version": 1, "state": {}},
    {"track": "something-else", "format_version": 1, "state": {}},
    {"track": "tablekeeper", "format_version": 2, "state": {}},
    {"track": "tablekeeper", "format_version": "1", "state": {}},
    {"track": "tablekeeper", "format_version": 1, "state": []},
    {"track": "tablekeeper", "format_version": 1,
     "state": {"users": [{"id": "u", "email": "e", "nope": 1}]}},
    {"track": "tablekeeper", "format_version": 1, "state": {"users": "nope"}},
    {"track": "tablekeeper", "format_version": 1, "state": {"users": [1, 2]}},
])
def test_invalid_imports_are_rejected_without_touching_the_destination(
    client, seeded, payload
):
    token = populate(client)
    before = client.get("/_test/export").json()["state"]

    response = client.post("/_test/import", json=payload)
    assert response.status_code == 422, payload
    assert error_of(response)["code"] == "validation_failed"

    assert client.get("/_test/export").json()["state"] == before
    assert_ok(client.get("/reservations", headers=headers_for(token)), 200)


def test_import_of_unparseable_json_is_malformed(client, seeded):
    before = client.get("/_test/export").json()["state"]
    response = client.post("/_test/import", content=b"{oops",
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"

    response = client.post("/_test/import", json=[1, 2])
    assert response.status_code == 400
    assert error_of(response)["code"] == "malformed_request"
    assert client.get("/_test/export").json()["state"] == before


def test_import_with_an_empty_state_gives_an_empty_service(client, seeded):
    populate(client)
    empty = {"track": "tablekeeper", "format_version": 1,
             "state": {table: [] for table in
                       ("users", "tokens", "restaurants", "opening_hours",
                        "dining_tables", "reservations", "idempotency")}}
    assert_ok(client.post("/_test/import", json=empty), 204)
    assert client.get("/restaurants").json() == {"restaurants": []}
    assert client.post("/auth/login", json={
        "email": ADA["email"], "password": ADA["password"]}).status_code == 401


def test_a_partial_state_is_accepted(client, seeded):
    """Tables absent from the snapshot are simply empty."""
    snapshot = {"track": "tablekeeper", "format_version": 1, "state": {}}
    assert_ok(client.post("/_test/import", json=snapshot), 204)
    assert client.get("/restaurants").json() == {"restaurants": []}


def test_batch_move_receipts_survive_an_import(client, seeded, tmp_path):
    token = signup(client)["token"]
    first = assert_ok(book(client, token, table_id="t_1", party_size=2), 201)
    second = assert_ok(book(client, token, table_id="t_2", party_size=2), 201)
    payload = {"moves": [{"reference": first["reference"], "table_id": "t_2"},
                         {"reference": second["reference"], "table_id": "t_1"}]}
    applied = assert_ok(client.post("/reservation-moves", json=payload,
                                    headers=headers_for(token, "batch")), 201)
    snapshot = assert_ok(client.get("/_test/export"), 200)

    with other_client(tmp_path) as destination:
        assert_ok(destination.post("/_test/import", json=snapshot), 204)
        replay = destination.post("/reservation-moves", json=payload,
                                  headers=headers_for(token, "batch"))
        assert replay.status_code == 200
        assert replay.json() == applied
        assert replay.json()["reservations"][0]["table_id"] == "t_2"


def test_completed_request_bodies_are_part_of_the_export(client, seeded):
    """§10 asks that completed idempotent request bodies and responses survive."""
    token = populate(client)
    state = assert_ok(client.get("/_test/export"), 200)["state"]
    receipts = [r for r in state["idempotency"] if r["key"] == "export-1"]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["method"] == "POST"
    assert receipt["path"] == "/reservations"
    assert receipt["status_code"] == 201
    assert json.loads(receipt["request_body"]) == {
        "restaurant_id": "r_anker", "table_id": "t_1",
        "starts_at_local": f"{THURSDAY}T19:00", "party_size": 2}
    assert json.loads(receipt["response_body"])["reference"]
    assert receipt["user_id"]
    assert token
