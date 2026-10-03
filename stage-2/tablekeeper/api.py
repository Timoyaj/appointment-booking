"""HTTP surface.

Routes are deliberately thin: they parse, authenticate, resolve idempotency and
delegate to :mod:`tablekeeper.service`. The order of those steps is part of the
contract — the body is parsed first (400), then the caller is authenticated
(401), then idempotency is resolved (409 on key reuse, 200 on replay), and only
then are endpoint-specific fields and resources checked.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, parsing, service, testhooks, webui
from .db import Database
from .errors import ApiError

logger = logging.getLogger("tablekeeper")

CONTENT_TYPE = "application/json; charset=utf-8"


class JsonResponse(JSONResponse):
    media_type = CONTENT_TYPE


def create_app(database_path: str = "/tmp/tablekeeper/tablekeeper.db") -> FastAPI:
    db = Database(database_path)
    applied = db.migrate()
    if applied:
        logger.info("applied migrations: %s", ", ".join(applied))

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

    # -- plumbing --------------------------------------------------------- #
    async def raw_body(request: Request) -> bytes:
        return await request.body()

    def database(request: Request) -> Database:
        return request.app.state.db

    def authenticate(request: Request) -> dict:
        """Bearer token -> user. 401 when missing, malformed or unknown."""
        with request.app.state.db.read() as conn:
            return auth.authenticate(conn, request.headers.get("authorization"))

    # -- health ----------------------------------------------------------- #
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # -- test control (unauthenticated) ----------------------------------- #
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

    # -- public reads ----------------------------------------------------- #
    @app.get("/restaurants")
    def get_restaurants(request: Request) -> dict:
        return service.public_restaurants(database(request))

    @app.get("/restaurants/{restaurant_id}")
    def get_restaurant(request: Request, restaurant_id: str) -> dict:
        return service.public_restaurant(database(request), restaurant_id)

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
                database(request), user["user_id"], reference
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
