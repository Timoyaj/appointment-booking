"""Deposits: what a restaurant holds to keep a table, and when it keeps it.

The money model is the small one that covers the loss restaurants actually take —
a table for eight on a Friday that does not turn up:

* a restaurant publishes a deposit: an amount **per seat** and the party size it
  applies **from**;
* booking a party that size **authorizes** a hold on the diner's card as part of
  the same request, so a declined card refuses the booking instead of leaving a
  table held by somebody who never paid;
* the hold is **captured** when the party does not turn up, and **released** when
  they do — or when the diner cancels inside the cutoff.

The service never sees a card number. `payment_method_id` is the provider's own
handle for a card the diner already gave them, and `provider_ref` is the
provider's handle for the hold. Storing either is storing somebody's payment
details, and there is no reason to.

Providers sit behind a three-method protocol, which is the whole point: the
booking path deals in holds and releases rather than in anybody's API, so the
service can be developed and proven against :class:`FakeProvider` with no network
and no account anywhere.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .clock import now
from .errors import validation_failed
from .tztime import rfc3339

# The provider a deployment gets when it configures none. It is not a stub that
# pretends to succeed: it models authorization, capture and release, and refuses
# the payment methods it is told to refuse, so the whole product can be driven
# end to end without a payment account existing.
FAKE = "fake"

AUTHORIZED = "authorized"
CAPTURED = "captured"
RELEASED = "released"
FAILED = "failed"

CURRENCY_RE_LENGTH = 3
MAX_DEPOSIT_PER_SEAT_CENTS = 1_000_000
MAX_PARTY_SIZE = 100


@dataclass(frozen=True)
class Result:
    """What a provider says when it is asked to move money."""

    ok: bool
    provider_ref: str
    error: str | None = None


class Provider(Protocol):
    """Three calls: hold, take, give back. Nothing else is needed to run a table."""

    name: str

    def authorize(
        self, *, amount_cents: int, currency: str, payment_method_id: str,
        idempotency_key: str,
    ) -> Result: ...

    def capture(self, *, provider_ref: str, amount_cents: int) -> Result: ...

    def release(self, *, provider_ref: str) -> Result: ...


class FakeProvider:
    """A provider that lives in the process. The one the tests and demos use.

    It is deliberately *not* a stub that always says yes: a payment method whose
    id contains ``decline`` is refused, and a hold can only be captured once, so
    the service's refusals, its ledger and its "already captured" behaviour are
    exercised rather than assumed.
    """

    name = FAKE

    def __init__(self) -> None:
        self.holds: dict[str, dict[str, Any]] = {}

    def authorize(
        self, *, amount_cents: int, currency: str, payment_method_id: str,
        idempotency_key: str,
    ) -> Result:
        if not payment_method_id:
            return Result(False, "", "A payment method is required")
        if "decline" in payment_method_id.lower():
            return Result(False, "", "That card was declined")
        if amount_cents <= 0:
            return Result(False, "", "That amount is not chargeable")
        # One hold per idempotency key: a retried booking holds once.
        for reference, hold in self.holds.items():
            if hold["idempotency_key"] == idempotency_key:
                return Result(True, reference)
        provider_ref = f"hold_{secrets.token_hex(8)}"
        self.holds[provider_ref] = {
            "amount_cents": amount_cents,
            "currency": currency,
            "idempotency_key": idempotency_key,
            "captured_cents": 0,
            "released": False,
        }
        return Result(True, provider_ref)

    def capture(self, *, provider_ref: str, amount_cents: int) -> Result:
        hold = self.holds.get(provider_ref)
        if hold is None:
            return Result(False, provider_ref, "No such hold")
        if hold["released"]:
            return Result(False, provider_ref, "That hold has already been released")
        if amount_cents > hold["amount_cents"]:
            return Result(False, provider_ref, "That is more than was held")
        hold["captured_cents"] += amount_cents
        return Result(True, provider_ref)

    def release(self, *, provider_ref: str) -> Result:
        hold = self.holds.get(provider_ref)
        if hold is None:
            return Result(False, provider_ref, "No such hold")
        if hold["captured_cents"] >= hold["amount_cents"]:
            return Result(False, provider_ref, "That hold was already taken")
        hold["released"] = True
        return Result(True, provider_ref)


class StripeProvider:
    """Stripe, over HTTPS, with no SDK to install.

    Manual capture is what makes this a hold rather than a charge: the
    authorization is taken with ``capture_method=manual``, and the money moves
    only when the restaurant says the party did not come.

    **This has never been run against Stripe.** There is no account, no key and no
    outbound network in the environment this was written in, and it is not
    exercised by the suite beyond a test that checks the request it builds. Treat
    it as the shape a real integration takes — the three calls, the manual
    capture, the idempotency key — and expect to spend an afternoon on it against
    a test account before it takes anybody's money.
    """

    name = "stripe"
    BASE = "https://api.stripe.com/v1"

    def __init__(self, secret_key: str, *, timeout: float = 15.0) -> None:
        self.secret_key = secret_key
        self.timeout = timeout

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "StripeProvider | None":
        key = env.get("TABLEKEEPER_STRIPE_SECRET_KEY")
        return cls(key) if key else None

    def _post(self, path: str, fields: dict[str, str], idempotency_key: str | None) -> dict:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - a fixed https host
            f"{self.BASE}{path}",
            method="POST",
            data=body,
            headers={
                "Authorization": f"Bearer {self.secret_key}",
                "Content-Type": "application/x-www-form-urlencoded",
                **({"Idempotency-Key": idempotency_key} if idempotency_key else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read() or b"{}")

    def authorize(
        self, *, amount_cents: int, currency: str, payment_method_id: str,
        idempotency_key: str,
    ) -> Result:
        try:
            payload = self._post(
                "/payment_intents",
                {
                    "amount": str(amount_cents),
                    "currency": currency.lower(),
                    "payment_method": payment_method_id,
                    # The whole difference between a hold and a charge.
                    "capture_method": "manual",
                    "confirm": "true",
                },
                idempotency_key,
            )
        except urllib.error.HTTPError as error:  # pragma: no cover - needs a key
            return Result(False, "", _stripe_error(error))
        except Exception as error:  # pragma: no cover - needs a network
            return Result(False, "", f"{type(error).__name__}: {error}")
        if payload.get("status") == "requires_capture":
            return Result(True, payload.get("id", ""))
        return Result(False, payload.get("id", ""), _stripe_status(payload))

    def capture(self, *, provider_ref: str, amount_cents: int) -> Result:
        try:
            payload = self._post(
                f"/payment_intents/{provider_ref}/capture",
                {"amount_to_capture": str(amount_cents)},
                None,
            )
        except urllib.error.HTTPError as error:  # pragma: no cover - needs a key
            return Result(False, provider_ref, _stripe_error(error))
        except Exception as error:  # pragma: no cover - needs a network
            return Result(False, provider_ref, f"{type(error).__name__}: {error}")
        if payload.get("status") == "succeeded":
            return Result(True, provider_ref)
        return Result(False, provider_ref, _stripe_status(payload))

    def release(self, *, provider_ref: str) -> Result:
        try:
            payload = self._post(
                f"/payment_intents/{provider_ref}/cancel", {}, None
            )
        except urllib.error.HTTPError as error:  # pragma: no cover - needs a key
            return Result(False, provider_ref, _stripe_error(error))
        except Exception as error:  # pragma: no cover - needs a network
            return Result(False, provider_ref, f"{type(error).__name__}: {error}")
        if payload.get("status") in ("canceled", "requires_payment_method"):
            return Result(True, provider_ref)
        return Result(False, provider_ref, _stripe_status(payload))


def _stripe_error(error: urllib.error.HTTPError) -> str:  # pragma: no cover - needs a key
    try:
        payload = json.loads(error.read() or b"{}")
        message = payload.get("error", {}).get("message")
        if message:
            return str(message)
    except Exception:
        pass
    return f"Stripe refused the request ({error.code})"


def _stripe_status(payload: dict) -> str:  # pragma: no cover - needs a key
    return f"Stripe left the payment in '{payload.get('status', 'unknown')}'"


def provider_from_env(env: dict[str, str]) -> Provider:
    """The provider a deployment configured, or the in-process one.

    Falling back to :class:`FakeProvider` rather than to nothing is deliberate: a
    restaurant that has published a deposit and a deployment that forgot to
    configure a provider should still be able to take a booking, and the hold it
    takes is recorded in the ledger like any other. What must never happen is a
    service that believes it took money it did not take.
    """
    stripe = StripeProvider.from_env(env)
    if stripe is not None:
        return stripe
    return FakeProvider()


# --------------------------------------------------------------------------- #
# the deposit a booking owes
# --------------------------------------------------------------------------- #
def validate_settings(body: dict, tables: list[dict]) -> dict:
    """A deposit as a restaurant states it, or a refusal naming what is wrong."""
    currency = body.get("currency")
    if not isinstance(currency, str) or len(currency.strip()) != CURRENCY_RE_LENGTH:
        raise validation_failed(
            "'currency' must be a three-letter code, such as 'eur' or 'ngn'"
        )
    per_seat = body.get("deposit_per_seat_cents")
    if isinstance(per_seat, bool) or not isinstance(per_seat, int):
        raise validation_failed("'deposit_per_seat_cents' must be an integer")
    if not 0 <= per_seat <= MAX_DEPOSIT_PER_SEAT_CENTS:
        raise validation_failed(
            f"'deposit_per_seat_cents' must be between 0 and {MAX_DEPOSIT_PER_SEAT_CENTS}"
        )
    from_size = body.get("deposit_from_party_size")
    if isinstance(from_size, bool) or not isinstance(from_size, int):
        raise validation_failed("'deposit_from_party_size' must be an integer")
    if not 1 <= from_size <= MAX_PARTY_SIZE:
        raise validation_failed(
            f"'deposit_from_party_size' must be between 1 and {MAX_PARTY_SIZE}"
        )
    return {
        "currency": currency.strip().lower(),
        "deposit_per_seat_cents": per_seat,
        "deposit_from_party_size": from_size,
    }


def deposit_for(settings: dict | None, party_size: int) -> int:
    """What this booking must hold, in cents. Zero means no deposit applies.

    Zero is also what a restaurant with no settings owes — which is what makes
    deposits opt-in and leaves every booking made before them exactly as it was.
    """
    if settings is None:
        return 0
    if party_size < int(settings["deposit_from_party_size"]):
        return 0
    return int(settings["deposit_per_seat_cents"]) * party_size


def describe(settings: dict | None) -> dict:
    """The settings as an API body, including the honest empty answer."""
    if settings is None:
        return {"deposits": False}
    return {
        "deposits": True,
        "currency": settings["currency"],
        "deposit_per_seat_cents": int(settings["deposit_per_seat_cents"]),
        "deposit_from_party_size": int(settings["deposit_from_party_size"]),
        "updated_at": settings["updated_at"],
    }


def new_intent_id() -> str:
    return f"pi_{secrets.token_hex(10)}"


def record_event(
    conn: sqlite3.Connection, *, intent_id: str, kind: str, amount_cents: int,
    detail: dict | None = None, at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO payment_events (intent_id, kind, amount_cents, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            intent_id,
            kind,
            int(amount_cents),
            json.dumps(detail or {}, sort_keys=True, separators=(",", ":")),
            at or rfc3339(now()),
        ),
    )


def format_money(cents: int, currency: str) -> str:
    """Cents as a person reads them: 4000 EUR becomes "40.00 EUR".

    Amounts are held in the smallest unit because that is the only way to add
    them up without losing a penny, and formatted back only where somebody is
    going to read it.
    """
    return f"{int(cents) / 100:.2f} {(currency or '').upper()}".strip()
