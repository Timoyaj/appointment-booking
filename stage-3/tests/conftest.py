"""Shared fixtures.

The clock is frozen at Monday 2026-09-21T11:04:03Z so cutoff rules are
deterministic, and the default fixture is the spec's example restaurant: Zum
Anker in Europe/Berlin, 30-minute slots, 90-minute reservations, a 120-minute
cancellation cutoff, Thursday and Friday service, and a 2-seat and a 4-seat table.
"""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tablekeeper import clock
from tablekeeper.api import create_app

NOW = dt.datetime(2026, 9, 21, 11, 4, 3, tzinfo=dt.timezone.utc)  # a Monday

THURSDAY = "2026-09-24"
FRIDAY = "2026-09-25"
SATURDAY = "2026-09-26"  # closed in the default fixture
SUNDAY = "2026-09-27"

ADA = {"id": "u_ada", "email": "ada@example.com", "password": "correct horse",
       "display_name": "Ada"}


def base_fixture() -> dict[str, Any]:
    """A fresh copy of the default fixture."""
    return copy.deepcopy({
        "users": [dict(ADA)],
        "restaurants": [
            {
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
        ],
        "reservations": [],
    })


@pytest.fixture(autouse=True)
def frozen_clock():
    clock.freeze(NOW)
    yield
    clock.reset()


@pytest.fixture
def client(tmp_path) -> TestClient:
    app = create_app(database_path=str(tmp_path / "tablekeeper.db"))
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def seeded(client: TestClient) -> dict[str, Any]:
    """The default fixture, loaded."""
    fixture = base_fixture()
    response = client.post("/_test/reset", json=fixture)
    assert response.status_code == 204, response.text
    return fixture


def reset(client: TestClient, fixture: dict[str, Any]) -> None:
    response = client.post("/_test/reset", json=fixture)
    assert response.status_code == 204, response.text


def signup(client: TestClient, email: str = "diner@example.com",
           password: str = "long enough", display_name: str = "Diner") -> dict:
    response = client.post(
        "/auth/signup",
        json={"email": email, "password": password, "display_name": display_name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def headers_for(token: str, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def login_headers(client: TestClient, email: str = ADA["email"],
                  password: str = ADA["password"]) -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return headers_for(response.json()["token"])


def book(client: TestClient, token: str, *, restaurant_id: str = "r_anker",
         table_id: str = "t_2", starts_at_local: str = f"{THURSDAY}T19:00",
         party_size: int = 4, key: str | None = None, extra: dict | None = None):
    """POST /reservations with sensible defaults; returns the raw response."""
    body: dict[str, Any] = {
        "restaurant_id": restaurant_id,
        "table_id": table_id,
        "starts_at_local": starts_at_local,
        "party_size": party_size,
    }
    if extra:
        body.update(extra)
    if key is None:
        # Unique by default, so a test that books twice really books twice.
        key = f"k_{uuid.uuid4().hex}"
    return client.post("/reservations", json=body, headers=headers_for(token, key))


def assert_ok(response, status: int) -> Any:
    """Assert a status and that the service never produced a 5xx."""
    assert response.status_code < 500, f"5xx from the service: {response.text}"
    assert response.status_code == status, response.text
    if response.content:
        return response.json()
    return None


def error_of(response) -> dict:
    """The error envelope of a 4xx/5xx response."""
    assert response.status_code >= 400, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == {"code", "message"}, body
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    return body["error"]


# --------------------------------------------------------------------------- #
# stage 3: managed restaurants and published policies
# --------------------------------------------------------------------------- #
def managed_fixture(managers: list[str] | None = None) -> dict[str, Any]:
    """The default fixture with Ada (by default) managing the restaurant."""
    fixture = base_fixture()
    fixture["restaurants"][0]["manager_user_ids"] = (
        [ADA["id"]] if managers is None else list(managers)
    )
    return fixture


def policy_body(effective_from: str = THURSDAY, **overrides: Any) -> dict[str, Any]:
    """A complete, valid policy for the default fixture's restaurant."""
    body: dict[str, Any] = {
        "effective_from": effective_from,
        "slot_minutes": 30,
        "reservation_duration_minutes": 90,
        "cancellation_cutoff_minutes": 120,
        "opening_hours": [
            {"weekday": "thu", "opens": "18:00", "closes": "23:00"},
            {"weekday": "fri", "opens": "18:00", "closes": "23:30"},
        ],
        "capacities": {"t_1": 2, "t_2": 4},
    }
    body.update(overrides)
    return body


def publish(client: TestClient, token: str, body: dict[str, Any],
            restaurant_id: str = "r_anker", key: str | None = None):
    """POST a policy the way a manager's client would."""
    return client.post(
        f"/restaurants/{restaurant_id}/policies",
        json=body,
        headers=headers_for(token, key or f"k_{uuid.uuid4().hex}"),
    )


def manager_headers(client: TestClient, fixture: dict[str, Any] | None = None) -> dict[str, str]:
    """Load a managed fixture and return the manager's headers."""
    reset(client, fixture if fixture is not None else managed_fixture())
    return login_headers(client)
