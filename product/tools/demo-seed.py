#!/usr/bin/env python3
"""Fill a running service with a demo restaurant, staff, policies and bookings.

For showing the console to somebody, and for a pilot that should not start from
an empty room. It talks to the service over HTTP only — the same API every other
client uses — so it cannot put the service into a state the API would refuse.

    python3 tools/demo-seed.py --base-url http://localhost:8080

It is safe to run twice: it signs in the accounts it made last time rather than
failing on a taken email, and it only opens a restaurant when the owner has none.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request

OWNER = ("owner@demo.test", "demo-password", "Ada Okonkwo")
DINERS = [
    ("ngozi@demo.test", "demo-password", "Ngozi Eze"),
    ("tunde@demo.test", "demo-password", "Tunde Bello"),
    ("chidi@demo.test", "demo-password", "Chidi Nwosu"),
    ("amaka@demo.test", "demo-password", "Amaka Obi"),
]

RESTAURANT = {
    "name": "Zum Anker",
    "timezone": "Europe/Berlin",
    "slot_minutes": 30,
    "reservation_duration_minutes": 90,
    "cancellation_cutoff_minutes": 120,
    "opening_hours": [
        {"weekday": "thu", "opens": "18:00", "closes": "23:00"},
        {"weekday": "fri", "opens": "18:00", "closes": "23:30"},
        {"weekday": "sat", "opens": "12:00", "closes": "23:30"},
        {"weekday": "sun", "opens": "12:00", "closes": "21:00"},
    ],
    "tables": [
        {"id": "t_1", "label": "1", "capacity": 2},
        {"id": "t_2", "label": "2", "capacity": 2},
        {"id": "t_3", "label": "3", "capacity": 4},
        {"id": "t_4", "label": "4", "capacity": 4},
        {"id": "t_5", "label": "5", "capacity": 6},
        {"id": "t_6", "label": "6", "capacity": 8},
    ],
    "combinable": [["t_3", "t_4"], ["t_5", "t_6"]],
}

POLICY = {
    "effective_from": "2026-01-01",
    "slot_minutes": 30,
    "reservation_duration_minutes": 90,
    "cancellation_cutoff_minutes": 120,
    "opening_hours": RESTAURANT["opening_hours"],
    "capacities": {"t_1": 2, "t_2": 2, "t_3": 4, "t_4": 4, "t_5": 6, "t_6": 8},
}

# (ISO weekday, local time, table(s), party, diner index, outcome).
#
# Keyed by weekday rather than by "day 0, day 1", so the rows keep meaning the
# same thing whichever evening the seeder is run on. Every sitting also has to
# finish inside the published window — a 90-minute sitting starting at 20:00 on a
# Sunday closes at 21:00, so it is refused — and no two rows may hold the same
# table at overlapping times.
BOOKINGS = [
    # Sunday lunch, when the room is open from 12:00 and closes at 21:00.
    (7, "12:30", "t_5", 5, 0, "confirmed"),
    (7, "13:00", "t_3", 3, 2, "confirmed"),
    (7, "18:00", ["t_5", "t_6"], 8, 1, "confirmed"),
    # Thursday dinner.
    (4, "18:00", "t_1", 2, 0, "confirmed"),
    (4, "19:00", "t_3", 4, 1, "confirmed"),
    (4, "19:30", "t_5", 6, 2, "confirmed"),
    (4, "21:00", "t_2", 2, 3, "cancelled"),
    # Friday dinner.
    (5, "18:30", "t_4", 4, 2, "confirmed"),
    (5, "19:00", "t_6", 8, 1, "confirmed"),
    (5, "20:30", "t_1", 2, 3, "confirmed"),
]

# ISO weekday numbers: Monday is 1, so Thursday to Sunday is 4..7. These must
# match `RESTAURANT["opening_hours"]` above, or every booking is refused for
# being outside opening hours.
OPEN_DAYS = {4, 5, 6, 7}


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None

    def call(self, method: str, path: str, body=None, *, key=None) -> tuple[int, dict]:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except json.JSONDecodeError:
                return error.code, {"raw": raw.decode("utf-8", "replace")}


def sign_in_or_up(client: Client, email: str, password: str, name: str) -> str:
    status, body = client.call("POST", "/auth/login", {"email": email, "password": password})
    if status == 200:
        return body["token"]
    status, body = client.call(
        "POST", "/auth/signup", {"email": email, "password": password, "display_name": name}
    )
    if status != 201:
        raise SystemExit(f"could not create {email}: {status} {body}")
    return body["token"]


def next_weekday(iso_weekday: int) -> dt.date:
    """The next date with this ISO weekday, today included.

    Today is read in the **restaurant's** timezone, not the machine's: a booking
    is placed against the restaurant's local calendar, so a seeder running at
    00:30 UTC must ask Berlin what day it is there before deciding whether it is
    a Thursday.
    """
    from zoneinfo import ZoneInfo

    today = dt.datetime.now(ZoneInfo(RESTAURANT["timezone"])).date()
    ahead = (iso_weekday - today.isoweekday()) % 7
    return today + dt.timedelta(days=ahead)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    arguments = parser.parse_args()
    client = Client(arguments.base_url)

    owner_token = sign_in_or_up(client, *OWNER)
    client.token = owner_token

    status, body = client.call("GET", "/restaurants/mine")
    if status != 200:
        raise SystemExit(f"could not read your restaurants: {status} {body}")
    mine = body["restaurants"]
    if mine:
        client.restaurant_id = mine[0]["id"]
        print(f"using the restaurant you already have: {mine[0]['name']} ({mine[0]['id']})")
    else:
        status, body = client.call("POST", "/restaurants", RESTAURANT, key="demo-restaurant")
        if status not in (200, 201):
            raise SystemExit(f"could not open the restaurant: {status} {body}")
        client.restaurant_id = body["id"]
        print(f"opened {body['name']} ({body['id']})")

    restaurant_id = client.restaurant_id

    # A published policy, so availability is decided by something a manager can see.
    status, body = client.call(
        "POST", f"/restaurants/{restaurant_id}/policies", POLICY, key="demo-policy"
    )
    if status == 201:
        print(f"published policy version {body['policy_version']}")
    elif status == 200:
        print("policy already published")
    else:
        print(f"policy refused: {status} {body}")

    # The diners first: somebody can only be added to a restaurant once they have
    # an account, and the staff below are diners who also work here.
    diner_tokens = [sign_in_or_up(client, *diner) for diner in DINERS]
    client.token = owner_token

    # Staff, so the console has people in it.
    for email, role in ((DINERS[0][0], "manager"), (DINERS[1][0], "host")):
        status, body = client.call(
            "POST", f"/restaurants/{restaurant_id}/staff", {"email": email, "role": role}
        )
        if status == 201:
            print(f"{email} is now {role}")
        elif status == 409:
            print(f"{email} was already staff")
        else:
            print(f"{email} could not be added: {status} {body.get('error', body)}")
    made = 0
    for index, (weekday, time, tables, party, diner_index, outcome) in enumerate(BOOKINGS):
        client.token = diner_tokens[diner_index]
        day = next_weekday(weekday)
        starts_at_local = f"{day.isoformat()}T{time}"
        status, body = client.call(
            "POST",
            "/reservations",
            {
                "restaurant_id": restaurant_id,
                "table_ids": tables if isinstance(tables, list) else [tables],
                "starts_at_local": starts_at_local,
                "party_size": party,
            },
            # The date is part of the key: run this again next week and it makes
            # next week's bookings rather than replaying last week's, while a
            # second run on the same day still replays instead of doubling up.
            key=f"demo-booking-{index}-{day.isoformat()}",
        )
        if status not in (200, 201):
            reason = body.get("error", body)
            print(f"  booking {starts_at_local} for {party}: {status} {reason}")
            continue
        made += 1
        if outcome == "cancelled":
            client.call("POST", f"/reservations/{body['reference']}/cancel", {})

    client.token = owner_token
    status, outbox = client.call("GET", f"/restaurants/{restaurant_id}/notifications")
    summary = outbox.get("summary", {}) if status == 200 else {}
    print(
        f"\n{made} bookings made. Outbox: {summary.get('queued', '?')} waiting, "
        f"{summary.get('sent', 0)} sent, {summary.get('failed', 0)} failed."
    )
    print(f"\nSign in at /console as {OWNER[0]} / {OWNER[1]}")
    print("The console shows the room, the staff, the outbox and the audit trail.")


if __name__ == "__main__":
    sys.exit(main())
