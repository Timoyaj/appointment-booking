"""Deposits: holding money for a table, and giving it back.

The product decision under test is the one restaurants actually lose money on — a
party that books a table for eight on a Friday and does not turn up. Everything
here is about that: a hold taken when the table is booked, kept when the party
does not come, and released when they do.
"""

from __future__ import annotations

import urllib.parse

import pytest

from tablekeeper import payments

from .conftest import (
    THURSDAY,
    assert_ok,
    book,
    error_of,
    headers_for,
    signup,
    token_for,
)
from .test_product import managed, manager_headers, open_restaurant  # noqa: F401

DEPOSIT = {
    "currency": "eur",
    "deposit_per_seat_cents": 1000,
    "deposit_from_party_size": 4,
}


@pytest.fixture
def room(client) -> dict:
    """A restaurant that takes a deposit from parties of four, and its owner."""
    owner = signup(client, "deposit-owner@example.com", "long enough", "Owner")
    restaurant = open_restaurant(client, owner["token"])
    settings = assert_ok(client.put(
        f"/restaurants/{restaurant['id']}/payment-settings",
        json=DEPOSIT, headers=headers_for(owner["token"]),
    ), 200)
    assert settings["deposits"] is True
    return {
        "id": restaurant["id"],
        "owner": owner["token"],
        "headers": headers_for(owner["token"]),
    }


def book_with(client, token, restaurant_id, *, party_size=4, card="pm_visa",
              at=f"{THURSDAY}T19:00", table_id="t_2", key="dep-1"):
    body = {
        "restaurant_id": restaurant_id, "table_id": table_id,
        "starts_at_local": at, "party_size": party_size,
    }
    if card is not None:
        body["payment_method_id"] = card
    return client.post(
        "/reservations", json=body, headers=headers_for(token, key)
    )


# --------------------------------------------------------------------------- #
# the settings
# --------------------------------------------------------------------------- #
def test_a_restaurant_with_no_settings_asks_for_nothing(client, managed):
    """Deposits are opt-in: every booking made before them behaves as it did."""
    body = assert_ok(
        client.get(
            "/restaurants/r_anker/payment-settings", headers=manager_headers(client)
        ),
        200,
    )
    assert body == {"deposits": False}


def test_only_a_manager_may_set_the_deposit(client, room):
    diner = signup(client, "noservice@example.com")["token"]
    response = client.put(
        f"/restaurants/{room['id']}/payment-settings",
        json=DEPOSIT, headers=headers_for(diner),
    )
    assert response.status_code == 403
    assert client.get(
        f"/restaurants/{room['id']}/payment-settings", headers=headers_for(diner)
    ).status_code == 404


@pytest.mark.parametrize("broken, message", [
    ({"currency": "euros"}, "three-letter"),
    ({"currency": "eur", "deposit_per_seat_cents": -1}, "between"),
    ({"currency": "eur", "deposit_per_seat_cents": "lots"}, "integer"),
    ({"currency": "eur", "deposit_per_seat_cents": 100, "deposit_from_party_size": 0},
     "between"),
    ({"currency": "eur", "deposit_per_seat_cents": None}, "deposit_per_seat_cents"),
])
def test_a_broken_deposit_is_refused(client, room, broken, message):
    body = {**DEPOSIT, **broken}
    # A null is the same as absent here, as everywhere else in this service.
    for key, value in list(broken.items()):
        if value is None:
            body.pop(key, None)
    response = client.put(
        f"/restaurants/{room['id']}/payment-settings", json=body, headers=room["headers"]
    )
    assert response.status_code == 422, response.text
    assert message in response.text


def test_a_restaurant_can_stop_taking_deposits(client, room):
    assert_ok(client.delete(
        f"/restaurants/{room['id']}/payment-settings", headers=room["headers"]
    ), 200)
    assert client.get(
        f"/restaurants/{room['id']}/payment-settings", headers=room["headers"]
    ).json() == {"deposits": False}


# --------------------------------------------------------------------------- #
# booking with a deposit
# --------------------------------------------------------------------------- #
def test_a_party_below_the_threshold_owes_nothing(client, room):
    diner = signup(client, "small@example.com")["token"]
    booked = assert_ok(
        book_with(client, diner, room["id"], party_size=2, card=None, table_id="t_1"),
        201,
    )
    assert client.get(
        f"/reservations/{booked['reference']}/payments", headers=headers_for(diner)
    ).json()["intents"] == []


def test_a_party_over_the_threshold_must_leave_a_deposit(client, room):
    diner = signup(client, "big@example.com")["token"]
    response = book_with(client, diner, room["id"], party_size=4, card=None)
    assert response.status_code == 402
    error = error_of(response)
    assert error["code"] == "payment_required"
    # The refusal says what is owed, in money rather than in cents.
    assert "40.00 EUR" in error["message"]
    # And no booking was left behind by the refusal.
    assert client.get("/reservations", headers=headers_for(diner)).json() == {
        "reservations": []
    }


def test_a_hold_is_taken_and_readable_by_the_diner_and_the_restaurant(client, room):
    diner = signup(client, "holder@example.com")["token"]
    booked = assert_ok(book_with(client, diner, room["id"], party_size=4), 201)

    for headers in (headers_for(diner), room["headers"]):
        body = assert_ok(client.get(
            f"/reservations/{booked['reference']}/payments", headers=headers
        ), 200)
        assert body["currency"] == "eur"
        intent = body["intents"][0]
        assert intent["status"] == "authorized"
        assert intent["amount_cents"] == 4000
        assert [event["kind"] for event in intent["events"]] == ["authorized"]


def test_somebody_elses_holds_are_not_readable(client, room):
    diner = signup(client, "private@example.com")["token"]
    booked = assert_ok(book_with(client, diner, room["id"], party_size=4), 201)
    stranger = signup(client, "stranger@example.com")["token"]
    assert client.get(
        f"/reservations/{booked['reference']}/payments", headers=headers_for(stranger)
    ).status_code == 404


def test_a_declined_card_refuses_the_booking(client, room):
    diner = signup(client, "declined@example.com")["token"]
    response = book_with(client, diner, room["id"], party_size=4, card="pm_declined")
    assert response.status_code == 402
    assert error_of(response)["code"] == "card_declined"
    assert client.get("/reservations", headers=headers_for(diner)).json() == {
        "reservations": []
    }


def test_a_declined_attempt_is_remembered_outside_the_rolled_back_booking(client, room):
    """The booking is gone; the fact that a card failed is not."""
    diner = signup(client, "fails@example.com")["token"]
    book_with(client, diner, room["id"], party_size=4, card="pm_declined")
    state = assert_ok(
        client.get(
            f"/restaurants/{room['id']}/reports/summary"
            f"?from=2026-01-01&to=2026-12-31", headers=room["headers"]
        ), 200
    )
    assert state["money"]["declined"] == 1


def test_a_retried_booking_holds_once(client, room):
    diner = signup(client, "retry@example.com")["token"]
    first = book_with(client, diner, room["id"], party_size=4, key="same-key")
    assert first.status_code == 201
    second = book_with(client, diner, room["id"], party_size=4, key="same-key")
    assert second.status_code == 200
    assert second.json() == first.json()
    reference = first.json()["reference"]
    holds = assert_ok(client.get(
        f"/reservations/{reference}/payments", headers=room["headers"]
    ), 200)["intents"]
    assert len(holds) == 1


# --------------------------------------------------------------------------- #
# the party came, or did not
# --------------------------------------------------------------------------- #
def test_a_no_show_keeps_the_deposit_and_tells_the_diner(client, room):
    diner = signup(client, "didnotcome@example.com")["token"]
    booked = assert_ok(book_with(client, diner, room["id"], party_size=4), 201)
    marked = assert_ok(client.post(
        f"/reservations/{booked['reference']}/no-show", headers=room["headers"]
    ), 200)
    assert marked["status"] == "no_show"

    intent = assert_ok(client.get(
        f"/reservations/{booked['reference']}/payments", headers=room["headers"]
    ), 200)["intents"][0]
    assert intent["status"] == "captured"
    assert intent["captured_cents"] == 4000
    assert [event["kind"] for event in intent["events"]] == ["authorized", "captured"]

    delivered = assert_ok(client.post(
        "/_test/notifications/drain", headers=room["headers"]
    ), 200)
    message = [m for m in delivered["messages"] if m["kind"] == "no_show_charge"][0]
    assert "40.00 EUR" in message["body"]
    assert "no-show" in message["body"]


def test_marking_a_no_show_twice_keeps_the_money_once(client, room):
    diner = signup(client, "twice@example.com")["token"]
    booked = assert_ok(book_with(client, diner, room["id"], party_size=4), 201)
    assert_ok(client.post(
        f"/reservations/{booked['reference']}/no-show", headers=room["headers"]
    ), 200)
    again = assert_ok(client.post(
        f"/reservations/{booked['reference']}/no-show", headers=room["headers"]
    ), 200)
    assert again["status"] == "no_show"
    intent = assert_ok(client.get(
        f"/reservations/{booked['reference']}/payments", headers=room["headers"]
    ), 200)["intents"][0]
    assert intent["captured_cents"] == 4000
    assert [event["kind"] for event in intent["events"]] == ["authorized", "captured"]


def test_the_party_came_so_the_hold_goes_back(client, room):
    diner = signup(client, "came@example.com")["token"]
    booked = assert_ok(book_with(client, diner, room["id"], party_size=4), 201)
    released = assert_ok(client.post(
        f"/reservations/{booked['reference']}/complete", headers=room["headers"]
    ), 200)
    assert released["deposit_released"] is True
    # The booking is still a booking: the party is sitting at that table.
    assert released["status"] == "confirmed"
    intent = assert_ok(client.get(
        f"/reservations/{booked['reference']}/payments", headers=room["headers"]
    ), 200)["intents"][0]
    assert intent["status"] == "released"
    assert intent["captured_cents"] == 0


def test_cancelling_inside_the_cutoff_gives_the_deposit_back(client, room):
    diner = signup(client, "canceller@example.com")["token"]
    booked = assert_ok(book_with(client, diner, room["id"], party_size=4), 201)
    assert_ok(client.post(
        f"/reservations/{booked['reference']}/cancel", headers=headers_for(diner)
    ), 200)
    intent = assert_ok(client.get(
        f"/reservations/{booked['reference']}/payments", headers=headers_for(diner)
    ), 200)["intents"][0]
    assert intent["status"] == "released"
    delivered = assert_ok(client.post(
        "/_test/notifications/drain", headers=room["headers"]
    ), 200)
    message = [m for m in delivered["messages"] if m["kind"] == "booking_cancelled"][0]
    assert "released back to your card" in message["body"]


def test_a_cancelled_booking_cannot_be_marked_a_no_show(client, room):
    diner = signup(client, "cancelledtwice@example.com")["token"]
    booked = assert_ok(book_with(client, diner, room["id"], party_size=4), 201)
    assert_ok(client.post(
        f"/reservations/{booked['reference']}/cancel", headers=headers_for(diner)
    ), 200)
    response = client.post(
        f"/reservations/{booked['reference']}/no-show", headers=room["headers"]
    )
    assert response.status_code == 409
    assert error_of(response)["code"] == "reservation_cancelled"


def test_a_no_show_frees_the_table_for_somebody_else(client, room):
    """Nobody is sitting there, so the table is not theirs any more."""
    diner = signup(client, "gone@example.com")["token"]
    booked = assert_ok(book_with(client, diner, room["id"], party_size=4), 201)
    assert_ok(client.post(
        f"/reservations/{booked['reference']}/no-show", headers=room["headers"]
    ), 200)
    other = assert_ok(book_with(
        client, diner, room["id"], party_size=4, at=f"{THURSDAY}T20:30", key="after"
    ), 201)
    assert other["reference"] != booked["reference"]


def test_a_no_show_cannot_be_amended(client, room):
    diner = signup(client, "left@example.com")["token"]
    booked = assert_ok(book_with(client, diner, room["id"], party_size=4), 201)
    assert_ok(client.post(
        f"/reservations/{booked['reference']}/no-show", headers=room["headers"]
    ), 200)
    response = client.patch(
        f"/reservations/{booked['reference']}", json={"party_size": 3},
        headers=headers_for(diner),
    )
    assert response.status_code == 409
    assert error_of(response)["code"] == "reservation_not_editable"


def test_only_the_restaurants_own_staff_can_mark_a_no_show(client, room):
    diner = signup(client, "outsider@example.com")["token"]
    booked = assert_ok(book_with(client, diner, room["id"], party_size=4), 201)
    # The diner's own booking, but they do not work here.
    for path in ("no-show", "complete"):
        response = client.post(
            f"/reservations/{booked['reference']}/{path}", headers=headers_for(diner)
        )
        assert response.status_code == 404, path
    # A stranger cannot even find the booking.
    stranger = signup(client, "other2@example.com")["token"]
    assert client.post(
        f"/reservations/{booked['reference']}/no-show", headers=headers_for(stranger)
    ).status_code == 404


def test_the_no_show_is_written_into_the_bookings_own_record(client, room):
    diner = signup(client, "recorded@example.com")["token"]
    booked = assert_ok(book_with(client, diner, room["id"], party_size=4), 201)
    assert_ok(client.post(
        f"/reservations/{booked['reference']}/no-show", headers=room["headers"]
    ), 200)
    history = assert_ok(client.get(
        f"/reservations/{booked['reference']}/history", headers=headers_for(diner)
    ), 200)
    events = [entry["event"] for entry in history["entries"]]
    assert events == ["created", "no_show"]


# --------------------------------------------------------------------------- #
# the provider seam
# --------------------------------------------------------------------------- #
def test_a_deployment_with_no_provider_still_holds_the_deposit(client, room):
    """No payment account anywhere, and the whole path still runs."""
    assert isinstance(client.app.state.payments_provider, payments.FakeProvider)
    diner = signup(client, "noprovider@example.com")["token"]
    booked = assert_ok(book_with(client, diner, room["id"], party_size=4), 201)
    intent = assert_ok(client.get(
        f"/reservations/{booked['reference']}/payments", headers=room["headers"]
    ), 200)["intents"][0]
    assert intent["status"] == "authorized"
    assert booked["status"] == "confirmed"


def test_a_stripe_key_configures_the_stripe_provider(monkeypatch):
    monkeypatch.setenv("TABLEKEEPER_STRIPE_SECRET_KEY", "sk_test_123")
    provider = payments.provider_from_env(dict(__import__("os").environ))
    assert isinstance(provider, payments.StripeProvider)
    assert provider.secret_key == "sk_test_123"


def test_the_stripe_adapter_asks_for_a_hold_rather_than_a_charge(monkeypatch):
    """The one thing that must be right about it: manual capture.

    It has never been run against Stripe — there is no account, no key and no
    outbound network where this was written — so what is checked is the request it
    builds, which is where a mistake would be silent and expensive.
    """
    captured: dict = {}

    class FakeResponse:
        def read(self):
            return b'{"id": "pi_123", "status": "requires_capture"}'

        def __enter__(self):
            return self

        def __exit__(self, *arguments):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        return FakeResponse()

    monkeypatch.setattr(payments.urllib.request, "urlopen", fake_urlopen)
    provider = payments.StripeProvider("sk_test_123")
    result = provider.authorize(
        amount_cents=4000, currency="eur", payment_method_id="pm_visa",
        idempotency_key="booking:ABC123",
    )
    assert result.ok and result.provider_ref == "pi_123"
    fields = urllib.parse.parse_qs(captured["body"])
    assert fields["capture_method"] == ["manual"]
    assert fields["amount"] == ["4000"]
    assert fields["currency"] == ["eur"]
    assert fields["confirm"] == ["true"]
    assert captured["url"].endswith("/v1/payment_intents")
    assert captured["headers"]["idempotency-key"] == "booking:ABC123"
    assert captured["headers"]["authorization"] == "Bearer sk_test_123"


def test_the_stripe_adapter_reports_a_refusal_rather_than_raising(monkeypatch):
    import urllib.error

    class FakeError(urllib.error.HTTPError):
        def read(self):
            return b'{"error": {"message": "Your card was declined."}}'

    def fake_urlopen(request, timeout=None):
        raise FakeError(request.full_url, 402, "Payment Required", {}, None)

    monkeypatch.setattr(payments.urllib.request, "urlopen", fake_urlopen)
    provider = payments.StripeProvider("sk_test_123")
    result = provider.authorize(
        amount_cents=4000, currency="eur", payment_method_id="pm_visa",
        idempotency_key="booking:ABC123",
    )
    assert result.ok is False
    assert result.error == "Your card was declined."


def test_money_is_written_the_way_people_write_it():
    assert payments.format_money(4000, "eur") == "40.00 EUR"
    assert payments.format_money(0, "ngn") == "0.00 NGN"
    assert payments.format_money(125, "gbp") == "1.25 GBP"


def test_a_deposit_is_opt_in_for_every_restaurant_that_never_set_one(client, seeded):
    """A restaurant that publishes nothing takes nothing: the engine's own
    behaviour, unchanged, for every restaurant that existed before deposits did."""
    token = signup(client, "nodeposit@example.com")["token"]
    booked = assert_ok(book(client, token, table_id="t_2", party_size=4), 201)
    assert client.get(
        f"/reservations/{booked['reference']}/payments", headers=headers_for(token)
    ).json()["intents"] == []
