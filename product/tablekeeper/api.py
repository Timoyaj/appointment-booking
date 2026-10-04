"""HTTP surface.

Routes are deliberately thin: they parse, authenticate, resolve idempotency and
delegate to :mod:`tablekeeper.service`. The order of those steps is part of the
contract — the body is parsed first (400), then the caller is authenticated
(401), then idempotency is resolved (409 on key reuse, 200 on replay), and only
then are endpoint-specific fields and resources checked.

Three read endpoints depart from the 401 rule on purpose: a booking's own record,
the decision that produced it, and an agreement answer 404 to anybody who is not
the owner, including somebody who sent no credentials at all, so that none of them
can be used to find out whether a reference or an agreement exists. Writing to an
agreement does not depart from anything: it needs a token, and a missing one is a
401 like every other write.

The manager-only writes — publishing a policy, previewing a seating plan and
applying one — all take an idempotency key and all resolve the restaurant before
they resolve the caller's right to act on it, so an unknown restaurant is a 404 to
everybody and a known one is a 403 to a diner.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import (
    auth,
    console,
    notifications,
    onboarding,
    parsing,
    payments,
    service,
    testhooks,
    webui,
)
from .db import Database
from .errors import ApiError

logger = logging.getLogger("tablekeeper")

CONTENT_TYPE = "application/json; charset=utf-8"


class JsonResponse(JSONResponse):
    media_type = CONTENT_TYPE


def test_hooks_enabled(explicit: bool | None = None) -> bool:
    """Whether the ``/_test/*`` control endpoints are served at all.

    They are unauthenticated by design — a reset wipes every reservation — so they
    are a development and verification surface, never something to expose to the
    public internet. The rule is therefore:

    * an explicit argument wins (the tests pass ``True`` and mean it);
    * otherwise ``TABLEKEEPER_TEST_HOOKS`` decides, and a deployment that sets it
      to ``0`` gets a service where those paths do not exist at all — not a
      service where they exist and are refused;
    * and with nothing said either way they are **on**, because the reference
      checks for this track drive a running service through them and a silent
      change of default would break them.

    The shipped product image sets the variable to ``0``.
    """
    if explicit is not None:
        return explicit
    return os.environ.get("TABLEKEEPER_TEST_HOOKS", "1") not in ("0", "false", "no", "")


def create_app(
    database_path: str = "/tmp/tablekeeper/tablekeeper.db",
    *,
    test_hooks: bool | None = None,
) -> FastAPI:
    db = Database(database_path)
    applied = db.migrate()
    if applied:
        logger.info("applied migrations: %s", ", ".join(applied))
    serve_test_hooks = test_hooks_enabled(test_hooks)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Up to 50 requests may be in flight; endpoints are synchronous and run
        # in the worker-thread pool, so give the pool enough room for all of them.
        try:
            import anyio

            limiter = anyio.to_thread.current_default_thread_limiter()
            limiter.total_tokens = 128
        except Exception:  # pragma: no cover - depends on the anyio backend
            logger.warning("could not raise the worker-thread limit", exc_info=True)
        yield

    app = FastAPI(
        title="Tablekeeper",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        default_response_class=JsonResponse,
        lifespan=lifespan,
    )
    app.state.db = db
    # A transport when the deployment configured one, and nothing when it did not:
    # a service with no mail server must leave messages visibly queued rather than
    # report them sent. The recorder is what the test hooks read, so the product's
    # own suite can assert what a diner would have received without a mail server
    # existing anywhere.
    app.state.transport = notifications.SmtpTransport.from_env(os.environ)
    # The payments provider, configured the same way and for the same reason: a
    # deployment with a Stripe key talks to Stripe, and one without takes holds in
    # the process so the whole deposit path can be driven end to end.
    app.state.payments_provider = payments.provider_from_env(os.environ)
    app.state.recorder = notifications.RecordingTransport()
    app.state.serve_test_hooks = serve_test_hooks

    # -- plumbing --------------------------------------------------------- #
    async def raw_body(request: Request) -> bytes:
        return await request.body()

    def database(request: Request) -> Database:
        return request.app.state.db

    def authenticate(request: Request) -> dict:
        """Bearer token -> user. 401 when missing, malformed or unknown."""
        with request.app.state.db.read() as conn:
            return auth.authenticate(conn, request.headers.get("authorization"))

    def optional_user(request: Request) -> str | None:
        """The caller's identity when one is offered, and None when none is.

        For a booking's own record and its decision, having no identity is not an
        error: those answer 404 to anybody who is not the owner, and a caller who
        is nobody in particular is not the owner. Credentials that *are* offered and
        turn out to be wrong stay a 401, as everywhere else.
        """
        header = request.headers.get("authorization")
        if header is None:
            return None
        with request.app.state.db.read() as conn:
            return auth.authenticate(conn, header)["user_id"]

    # -- health ----------------------------------------------------------- #
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # -- test control (unauthenticated, and absent in the shipped image) ---- #
    if serve_test_hooks:

        @app.post("/_test/reset")
        def test_reset(
            request: Request, body: bytes = Depends(raw_body)
        ) -> Response:
            testhooks.reset(database(request), parsing.parse_object(body))
            return Response(status_code=204)

        @app.get("/_test/export")
        def test_export(request: Request) -> dict:
            return testhooks.export_state(database(request))

        @app.post("/_test/import")
        def test_import(
            request: Request, body: bytes = Depends(raw_body)
        ) -> Response:
            testhooks.import_state(database(request), parsing.parse_object(body))
            return Response(status_code=204)

        @app.post("/_test/notifications/drain")
        def test_drain(request: Request) -> dict:
            """Deliver the outbox to a recorder, and report what would be sent.

            The only way the product's own suite can see a confirmation message
            without standing up a mail server. It lives behind the same switch as
            the rest of the control surface, so it cannot be reached in a
            deployment that turned those off.
            """
            transport = request.app.state.recorder
            # Only what *this* call delivered: the recorder is a running log of
            # everything the service has ever sent, and a caller asking to drain
            # wants to know what went out now, not to re-read the whole history.
            already = len(transport.sent)
            result = notifications.drain(database(request), transport)
            return {
                "result": result,
                "messages": [
                    {
                        "kind": m["kind"],
                        "to_email": m["to_email"],
                        "subject": m["subject"],
                        "body": m["body"],
                        "reference": m["reference"],
                    }
                    for m in transport.sent[already:]
                ],
            }

    # -- auth ------------------------------------------------------------- #
    @app.post("/auth/signup")
    def auth_signup(
        request: Request, body: bytes = Depends(raw_body)
    ) -> Response:
        parsed = parsing.parse_object(body)
        return JsonResponse(status_code=201, content=service.signup(database(request), parsed))

    @app.post("/auth/login")
    def auth_login(
        request: Request, body: bytes = Depends(raw_body)
    ) -> Response:
        parsed = parsing.parse_object(body)
        return JsonResponse(status_code=200, content=service.login(database(request), parsed))

    @app.get("/auth/session")
    def get_session(request: Request) -> dict:
        """Who the caller is, when their token stops working, and where they work.

        A separate endpoint rather than extra fields on signup and login: those
        two bodies are fixed by the service's contract, and a client that only
        wants a token should not be handed a session description it did not ask
        for.
        """
        user = authenticate(request)
        return service.session_info(database(request), user["user_id"], user["token"])

    @app.post("/auth/password-reset")
    def post_password_reset(
        request: Request, body: bytes = Depends(raw_body)
    ) -> Response:
        """Ask for a reset link. Always 202, whatever was sent.

        Deliberately not 404 for an unknown address: this endpoint must not be
        usable to find out which of a restaurant's guests have accounts here.
        """
        parsed = parsing.parse_object(body)
        return JsonResponse(
            status_code=202,
            content=service.request_password_reset(database(request), body=parsed),
        )

    @app.post("/auth/password-reset/confirm")
    def post_password_reset_confirm(
        request: Request, body: bytes = Depends(raw_body)
    ) -> dict:
        parsed = parsing.parse_object(body)
        return service.confirm_password_reset(database(request), body=parsed)

    @app.post("/auth/verify-email")
    def post_verify_email(
        request: Request, body: bytes = Depends(raw_body)
    ) -> dict:
        """Confirm an address with the code from the message."""
        parsed = parsing.parse_object(body)
        return service.confirm_email_verification(database(request), body=parsed)

    @app.post("/auth/verify-email/resend")
    def post_verify_email_resend(request: Request) -> dict:
        user = authenticate(request)
        return service.request_email_verification(
            database(request), user_id=user["user_id"]
        )

    @app.post("/auth/logout")
    def post_logout(request: Request) -> dict:
        """Sign this device out. Signing out twice is not an error."""
        user = authenticate(request)
        return service.logout(database(request), token=user["token"])

    @app.post("/auth/logout-all")
    def post_logout_all(request: Request) -> dict:
        """Sign out of every device, as after a suspected password leak."""
        user = authenticate(request)
        return service.logout_everywhere(database(request), user_id=user["user_id"])

    # -- public reads ----------------------------------------------------- #
    @app.get("/restaurants")
    def get_restaurants(request: Request) -> dict:
        return service.public_restaurants(database(request))

    # Declared before `/restaurants/{restaurant_id}`, which would otherwise read
    # "mine" as the id of a restaurant called mine.
    @app.get("/restaurants/mine")
    def get_my_restaurants(request: Request) -> dict:
        """The restaurants this caller owns, manages or hosts."""
        user = authenticate(request)
        return onboarding.my_restaurants(database(request), user_id=user["user_id"])

    @app.post("/restaurants")
    def post_restaurant(
        request: Request,
        body: bytes = Depends(raw_body),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        """Open a restaurant. The caller becomes its owner.

        The key is optional here, as on the other manager writes: a client that
        sends one gets the retry safety of every other write in this service, and
        one that does not still gets a restaurant rather than a 400.
        """
        parsed = parsing.parse_object(body)
        user = authenticate(request)
        key = (
            parsing.idempotency_key(idempotency_key)
            if idempotency_key is not None
            else None
        )
        status, payload = onboarding.create_restaurant(
            database(request),
            user_id=user["user_id"],
            body=parsed,
            key=key,
            method="POST",
            path=request.url.path,
        )
        return JsonResponse(status_code=status, content=payload)

    @app.get("/restaurants/{restaurant_id}")
    def get_restaurant(request: Request, restaurant_id: str) -> dict:
        return service.public_restaurant(database(request), restaurant_id)

    # -- the people who run a restaurant ---------------------------------- #
    @app.get("/restaurants/{restaurant_id}/staff")
    def get_staff(request: Request, restaurant_id: str) -> dict:
        """The restaurant as its staff read it, and 404 to everybody else."""
        user = authenticate(request)
        return onboarding.restaurant_for_staff(
            database(request), user_id=user["user_id"], restaurant_id=restaurant_id
        )

    @app.post("/restaurants/{restaurant_id}/staff")
    def post_staff(
        request: Request, restaurant_id: str, body: bytes = Depends(raw_body)
    ) -> Response:
        """Add somebody who already has an account. Owners only."""
        parsed = parsing.parse_object(body)
        user = authenticate(request)
        status, payload = onboarding.add_staff(
            database(request),
            actor_id=user["user_id"],
            restaurant_id=restaurant_id,
            body=parsed,
        )
        return JsonResponse(status_code=status, content=payload)

    @app.patch("/restaurants/{restaurant_id}/staff/{user_id}")
    def patch_staff(
        request: Request, restaurant_id: str, user_id: str,
        body: bytes = Depends(raw_body),
    ) -> dict:
        parsed = parsing.parse_object(body)
        actor = authenticate(request)
        return onboarding.change_role(
            database(request),
            actor_id=actor["user_id"],
            restaurant_id=restaurant_id,
            target_id=user_id,
            body=parsed,
        )

    @app.delete("/restaurants/{restaurant_id}/staff/{user_id}")
    def delete_staff(request: Request, restaurant_id: str, user_id: str) -> dict:
        actor = authenticate(request)
        return onboarding.remove_staff(
            database(request),
            actor_id=actor["user_id"],
            restaurant_id=restaurant_id,
            target_id=user_id,
        )

    @app.get("/restaurants/{restaurant_id}/audit")
    def get_audit(request: Request, restaurant_id: str) -> dict:
        """What has been done here, and by whom. Anybody who works here may read it."""
        user = authenticate(request)
        return onboarding.audit_trail(
            database(request), user_id=user["user_id"], restaurant_id=restaurant_id
        )

    # -- what a restaurant charges to hold a table ------------------------ #
    @app.get("/restaurants/{restaurant_id}/payment-settings")
    def get_payment_settings(request: Request, restaurant_id: str) -> dict:
        """The deposit this restaurant publishes. Anybody who works here may read it."""
        user = authenticate(request)
        return service.get_payment_settings(
            database(request), user_id=user["user_id"], restaurant_id=restaurant_id
        )

    @app.put("/restaurants/{restaurant_id}/payment-settings")
    def put_payment_settings(
        request: Request, restaurant_id: str, body: bytes = Depends(raw_body)
    ) -> dict:
        """Publish a deposit, or change the one already published."""
        parsed = parsing.parse_object(body)
        user = authenticate(request)
        return service.put_payment_settings(
            database(request),
            user_id=user["user_id"],
            restaurant_id=restaurant_id,
            body=parsed,
        )

    @app.delete("/restaurants/{restaurant_id}/payment-settings")
    def delete_payment_settings(request: Request, restaurant_id: str) -> dict:
        """Stop asking for deposits. Holds already taken stay exactly as they are."""
        user = authenticate(request)
        return service.clear_payment_settings(
            database(request), user_id=user["user_id"], restaurant_id=restaurant_id
        )

    # -- the party came, or did not --------------------------------------- #
    @app.post("/reservations/{reference}/no-show")
    def post_no_show(request: Request, reference: str) -> dict:
        """Record a no-show and keep the deposit the diner left."""
        user = authenticate(request)
        return service.mark_no_show(
            database(request), user_id=user["user_id"], reference=reference,
            provider=request.app.state.payments_provider,
        )

    @app.post("/reservations/{reference}/complete")
    def post_complete(request: Request, reference: str) -> dict:
        """The party came: release the hold on their card."""
        user = authenticate(request)
        return service.mark_complete(
            database(request), user_id=user["user_id"], reference=reference,
            provider=request.app.state.payments_provider,
        )

    @app.get("/reservations/{reference}/payments")
    def get_payments(request: Request, reference: str) -> dict:
        """Every hold on a booking and what happened to it."""
        user = authenticate(request)
        return service.reservation_payments(
            database(request), user_id=user["user_id"], reference=reference
        )

    # -- what the restaurant has told its diners -------------------------- #
    @app.get("/restaurants/{restaurant_id}/reports/summary")
    def get_report_summary(request: Request, restaurant_id: str) -> dict:
        """Covers, cancellations, no-shows, timing, table use and money taken."""
        user = authenticate(request)
        return service.restaurant_summary(
            database(request), user_id=user["user_id"], restaurant_id=restaurant_id,
            params=dict(request.query_params),
        )

    @app.get("/restaurants/{restaurant_id}/reports/bookings.csv")
    def get_report_csv(request: Request, restaurant_id: str) -> Response:
        """The same window as a CSV, for whoever keeps the spreadsheet."""
        user = authenticate(request)
        body = service.restaurant_bookings_csv(
            database(request), user_id=user["user_id"], restaurant_id=restaurant_id,
            params=dict(request.query_params),
        )
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="bookings.csv"'
            },
        )

    @app.get("/restaurants/{restaurant_id}/reservations")
    def get_restaurant_reservations(request: Request, restaurant_id: str) -> dict:
        """Who is coming, in the restaurant's own dates. Staff only."""
        user = authenticate(request)
        return service.restaurant_reservations(
            database(request),
            user_id=user["user_id"],
            restaurant_id=restaurant_id,
            params=dict(request.query_params),
        )

    @app.get("/restaurants/{restaurant_id}/notifications")
    def get_notifications(request: Request, restaurant_id: str) -> dict:
        """The outbox: what has been written, what went out, what did not."""
        user = authenticate(request)
        return service.list_notifications(
            database(request), user_id=user["user_id"], restaurant_id=restaurant_id
        )

    @app.post("/restaurants/{restaurant_id}/notifications/{notification_id}/retry")
    def post_notification_retry(
        request: Request, restaurant_id: str, notification_id: str
    ) -> dict:
        user = authenticate(request)
        return service.retry_notification(
            database(request),
            user_id=user["user_id"],
            restaurant_id=restaurant_id,
            notification_id=notification_id,
        )

    @app.post("/restaurants/{restaurant_id}/notifications/drain")
    def post_notifications_drain(request: Request, restaurant_id: str) -> dict:
        """Hand queued messages to the configured transport.

        Without one this reports what is still waiting rather than marking
        anything sent, so an unconfigured deployment is visibly unconfigured.
        """
        user = authenticate(request)
        return service.drain_notifications(
            database(request),
            user_id=user["user_id"],
            restaurant_id=restaurant_id,
            transport=request.app.state.transport,
        )

    @app.get("/restaurants/{restaurant_id}/policies")
    def get_policies(request: Request, restaurant_id: str) -> dict:
        """Public: what a restaurant has published, in publication order."""
        return service.list_policies(database(request), restaurant_id)

    @app.post("/restaurants/{restaurant_id}/policies")
    def post_policy(
        request: Request,
        restaurant_id: str,
        body: bytes = Depends(raw_body),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        parsed = parsing.parse_object(body)
        user = authenticate(request)
        key = parsing.idempotency_key(idempotency_key)
        status, payload = service.publish_policy(
            database(request),
            user_id=user["user_id"],
            key=key,
            restaurant_id=restaurant_id,
            body=parsed,
            method="POST",
            # The literal path, so one key cannot be spent on two restaurants.
            path=request.url.path,
        )
        return JsonResponse(status_code=status, content=payload)

    # -- seating plans after a table closure -------------------------------- #
    @app.post("/restaurants/{restaurant_id}/replans")
    def post_replan(
        request: Request,
        restaurant_id: str,
        body: bytes = Depends(raw_body),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        """Preview a seating plan. Stores the plan and changes nothing else."""
        parsed = parsing.parse_object(body)
        user = authenticate(request)
        key = parsing.idempotency_key(idempotency_key)
        status, payload = service.propose_replan(
            database(request),
            user_id=user["user_id"],
            key=key,
            restaurant_id=restaurant_id,
            body=parsed,
            method="POST",
            path=request.url.path,
        )
        return JsonResponse(status_code=status, content=payload)

    @app.post("/restaurants/{restaurant_id}/replans/{plan_id}/apply")
    def apply_replan(
        request: Request,
        restaurant_id: str,
        plan_id: str,
        body: bytes = Depends(raw_body),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        """Apply a proposed plan: the closure and every reassignment, atomically."""
        # The body carries nothing the plan does not already know, but it is still
        # required to be an object when it is sent, and it is part of the receipt.
        parsed = parsing.parse_optional_object(body)
        user = authenticate(request)
        key = parsing.idempotency_key(idempotency_key)
        status, payload = service.apply_replan(
            database(request),
            user_id=user["user_id"],
            key=key,
            restaurant_id=restaurant_id,
            plan_id=plan_id,
            body=parsed,
            method="POST",
            # The literal path, so one key cannot be spent on two plans.
            path=request.url.path,
        )
        return JsonResponse(status_code=status, content=payload)

    @app.get("/availability")
    def get_availability(request: Request) -> dict:
        return service.public_availability(
            database(request), dict(request.query_params)
        )

    # -- reservations ----------------------------------------------------- #
    @app.post("/reservations")
    def post_reservation(
        request: Request,
        body: bytes = Depends(raw_body),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        parsed = parsing.parse_object(body)
        user = authenticate(request)
        key = parsing.idempotency_key(idempotency_key)
        status, payload = service.create_reservation(
            database(request),
            user_id=user["user_id"],
            key=key,
            body=parsed,
            method="POST",
            path="/reservations",
            provider=request.app.state.payments_provider,
        )
        return JsonResponse(status_code=status, content=payload)

    @app.get("/reservations")
    def list_reservations(request: Request) -> Response:
        user = authenticate(request)
        return JsonResponse(
            status_code=200,
            content=service.list_reservations(database(request), user["user_id"]),
        )

    @app.get("/reservations/{reference}")
    def get_reservation(request: Request, reference: str) -> Response:
        user = authenticate(request)
        return JsonResponse(
            status_code=200,
            content=service.get_reservation(database(request), user["user_id"], reference),
        )

    @app.get("/reservations/{reference}/history")
    def get_reservation_history(request: Request, reference: str) -> Response:
        user_id = optional_user(request)
        return JsonResponse(
            status_code=200,
            content=service.reservation_history(database(request), user_id, reference),
        )

    @app.get("/reservations/{reference}/decision")
    def get_reservation_decision(request: Request, reference: str) -> Response:
        user_id = optional_user(request)
        return JsonResponse(
            status_code=200,
            content=service.reservation_decision(database(request), user_id, reference),
        )

    @app.post("/reservations/{reference}/cancel")
    def cancel_reservation(
        request: Request, reference: str, body: bytes = Depends(raw_body)
    ) -> Response:
        # A body is optional here, but if one is sent it must still be a JSON
        # object: malformed input is a 400 rather than something we ignore.
        parsing.parse_optional_object(body)
        user = authenticate(request)
        return JsonResponse(
            status_code=200,
            content=service.cancel_reservation(
                database(request), user["user_id"], reference,
                provider=request.app.state.payments_provider,
            ),
        )

    @app.patch("/reservations/{reference}")
    def amend_reservation(
        request: Request, reference: str, body: bytes = Depends(raw_body)
    ) -> Response:
        parsed = parsing.parse_object(body)
        user = authenticate(request)
        return JsonResponse(
            status_code=200,
            content=service.amend_reservation(
                database(request), user["user_id"], reference, parsed
            ),
        )

    # -- recurring agreements --------------------------------------------- #
    @app.post("/series")
    def post_series(
        request: Request,
        body: bytes = Depends(raw_body),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        parsed = parsing.parse_object(body)
        user = authenticate(request)
        key = parsing.idempotency_key(idempotency_key)
        status, payload = service.create_series(
            database(request),
            user_id=user["user_id"],
            key=key,
            body=parsed,
            method="POST",
            path="/series",
        )
        return JsonResponse(status_code=status, content=payload)

    @app.get("/series/{series_id}")
    def get_series(request: Request, series_id: str) -> Response:
        # No token is not a 401 here: an agreement belongs to one diner, and
        # anybody else is told it does not exist.
        user_id = optional_user(request)
        return JsonResponse(
            status_code=200, content=service.series_detail(database(request), user_id, series_id)
        )

    @app.post("/series/{series_id}/amend")
    def amend_series(
        request: Request,
        series_id: str,
        body: bytes = Depends(raw_body),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        parsed = parsing.parse_object(body)
        # A write, so it needs an identity: no token is a 401 here, unlike reading
        # an agreement, which answers 404 to anybody who is not its diner.
        user = authenticate(request)
        key = parsing.idempotency_key(idempotency_key)
        status, payload = service.amend_series(
            database(request),
            user_id=user["user_id"],
            key=key,
            series_id=series_id,
            body=parsed,
            method="POST",
            path=request.url.path,
        )
        return JsonResponse(status_code=status, content=payload)

    # -- batch moves ------------------------------------------------------ #
    @app.post("/reservation-moves")
    def post_reservation_moves(
        request: Request,
        body: bytes = Depends(raw_body),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        parsed = parsing.parse_object(body)
        user = authenticate(request)
        key = parsing.idempotency_key(idempotency_key)
        status, payload = service.move_reservations(
            database(request),
            user_id=user["user_id"],
            key=key,
            body=parsed,
            method="POST",
            path="/reservation-moves",
        )
        return JsonResponse(status_code=status, content=payload)

    # -- browser screens -------------------------------------------------- #
    # Registered after the API so a screen route can never shadow an endpoint.
    webui.register(app)
    # The restaurant's own screens, for the same reason and after the API.
    console.register(app)

    # -- errors ----------------------------------------------------------- #
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> Response:
        return JsonResponse(status_code=exc.status, content=exc.to_body())

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """Every 4xx/5xx carries the error envelope, including routing failures."""
        code = "not_found" if exc.status_code in (404, 405) else "malformed_request"
        message = (
            f"No route for {request.method} {request.url.path}"
            if exc.status_code in (404, 405)
            else str(exc.detail)
        )
        return JsonResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": message}},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> Response:  # pragma: no cover - no pydantic models are used
        return JsonResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_failed",
                    "message": "Request validation failed",
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> Response:
        # The spec asks for no 5xx; if one ever happens it still gets the envelope.
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JsonResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "The service could not complete this request",
                }
            },
        )

    return app
