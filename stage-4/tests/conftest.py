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


# --------------------------------------------------------------------------- #
# stage 4: a room that can be re-planned, and the writes that re-plan it
# --------------------------------------------------------------------------- #
THREE_TABLES = [
    {"id": "t_1", "label": "1", "capacity": 2},
    {"id": "t_2", "label": "2", "capacity": 4},
    {"id": "t_3", "label": "3", "capacity": 6},
]
TWO_PAIRS = [["t_1", "t_2"], ["t_2", "t_3"]]
BERLIN_SUMMER = "+02:00"  # Europe/Berlin before the clocks go back on 2026-10-25


def plan_fixture(*, tables: list[dict[str, Any]] | None = None,
                 combinable: list[list[str]] | None = None,
                 managers: list[str] | None = None,
                 opening_hours: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """A managed restaurant with three tables and two declared pairs.

    Planning needs a room with somewhere to go: the default fixture's two tables
    cannot seat a party of four anywhere else once one of them closes.
    """
    fixture = managed_fixture(managers)
    restaurant = fixture["restaurants"][0]
    restaurant["tables"] = copy.deepcopy(
        THREE_TABLES if tables is None else tables)
    if combinable is not None:
        restaurant["combinable"] = copy.deepcopy(combinable)
    else:
        restaurant["combinable"] = copy.deepcopy(TWO_PAIRS)
    if opening_hours is not None:
        restaurant["opening_hours"] = copy.deepcopy(opening_hours)
    return fixture


def three_table_policy_body(effective_from: str = THURSDAY, **overrides: Any) -> dict[str, Any]:
    """A complete, valid policy for `plan_fixture`'s restaurant."""
    body = policy_body(effective_from)
    body["capacities"] = {"t_1": 2, "t_2": 4, "t_3": 6}
    body.update(overrides)
    return body


def instant(date_str: str, hhmm: str = "18:00", offset: str = BERLIN_SUMMER) -> str:
    """An RFC 3339 instant with an explicit offset, as a closure names its ends."""
    return f"{date_str}T{hhmm}:00{offset}"


def closure_body(table_id: str = "t_2", date_str: str = THURSDAY, *,
                 opens: str = "18:00", closes: str = "23:00",
                 offset: str = BERLIN_SUMMER) -> dict[str, Any]:
    return {"table_id": table_id,
            "from": instant(date_str, opens, offset),
            "to": instant(date_str, closes, offset)}


def seat(client: TestClient, token: str, tables: str | list[str], *,
         at: str = f"{THURSDAY}T19:00", party_size: int = 4,
         restaurant_id: str = "r_anker", key: str | None = None):
    """Book one table or a declared pair, and return the raw response."""
    body: dict[str, Any] = {"restaurant_id": restaurant_id,
                            "starts_at_local": at, "party_size": party_size}
    if isinstance(tables, str):
        body["table_id"] = tables
    else:
        body["table_ids"] = list(tables)
    return client.post("/reservations", json=body,
                       headers=headers_for(token, key or f"k_{uuid.uuid4().hex}"))


def replan(client: TestClient, token: str, body: dict[str, Any],
           restaurant_id: str = "r_anker", key: str | None = None):
    """Preview a seating plan the way a manager's client would."""
    return client.post(f"/restaurants/{restaurant_id}/replans", json=body,
                       headers=headers_for(token, key or f"k_{uuid.uuid4().hex}"))


def apply_plan(client: TestClient, token: str, plan_id: str,
               restaurant_id: str = "r_anker", key: str | None = None,
               body: dict[str, Any] | None = None):
    """Apply a proposed plan."""
    return client.post(f"/restaurants/{restaurant_id}/replans/{plan_id}/apply",
                       json={} if body is None else body,
                       headers=headers_for(token, key or f"k_{uuid.uuid4().hex}"))


def adopt(client: TestClient, token: str, anchor_reference: str, *,
          count: int = 3, interval_weeks: int = 1, key: str | None = None):
    """POST /series."""
    return client.post("/series", json={"anchor_reference": anchor_reference,
                                        "count": count,
                                        "interval_weeks": interval_weeks},
                       headers=headers_for(token, key or f"k_{uuid.uuid4().hex}"))


def amend_series(client: TestClient, token: str, series_id: str, body: dict[str, Any],
                 key: str | None = None):
    """POST /series/{id}/amend."""
    return client.post(f"/series/{series_id}/amend", json=body,
                       headers=headers_for(token, key or f"k_{uuid.uuid4().hex}"))


def series_of(client: TestClient, token: str, series_id: str) -> dict[str, Any]:
    return client.get(f"/series/{series_id}", headers=headers_for(token)).json()


def availability_of(client: TestClient, date_str: str, party_size: int, *,
                    restaurant_id: str = "r_anker", explain: bool = False) -> dict[str, Any]:
    params: dict[str, Any] = {"restaurant_id": restaurant_id, "date": date_str,
                              "party_size": party_size}
    if explain:
        params["explain"] = "true"
    response = client.get("/availability", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def slot_at(day: dict[str, Any], hhmm: str) -> dict[str, Any]:
    return next(slot for slot in day["slots"]
                if slot["starts_at_local"].endswith(f"T{hhmm}"))


def export(client: TestClient) -> dict[str, Any]:
    response = client.get("/_test/export")
    assert response.status_code == 200, response.text
    return response.json()


def token_for(client: TestClient, email: str = ADA["email"],
              password: str = ADA["password"]) -> str:
    """A bearer token for a seeded or signed-up user."""
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["token"]
