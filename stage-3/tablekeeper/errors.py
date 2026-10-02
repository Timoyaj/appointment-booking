"""Errors.

Every 4xx/5xx response carries ``{"error": {"code": ..., "message": ...}}`` with
the status and code fixed by the spec; only the wording is ours.
"""

from __future__ import annotations


class ApiError(Exception):
    """An error that maps straight onto an HTTP response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


# --- 400 ------------------------------------------------------------------ #
def malformed_request(detail: str = "Request body could not be parsed") -> ApiError:
    return ApiError(400, "malformed_request", detail)


def missing_idempotency_key() -> ApiError:
    return ApiError(
        400, "missing_idempotency_key", "Idempotency-Key header is required"
    )


# --- 401 / 403 ------------------------------------------------------------ #
def unauthenticated(detail: str = "A valid bearer token is required") -> ApiError:
    return ApiError(401, "unauthenticated", detail)


def forbidden(detail: str = "Not permitted to access this resource") -> ApiError:
    return ApiError(403, "forbidden", detail)


# --- 404 ------------------------------------------------------------------ #
def not_found(detail: str = "No such resource") -> ApiError:
    return ApiError(404, "not_found", detail)


# --- 409 ------------------------------------------------------------------ #
def idempotency_key_reuse() -> ApiError:
    return ApiError(
        409,
        "idempotency_key_reuse",
        "This Idempotency-Key was already used with a different request body",
    )


def email_taken() -> ApiError:
    return ApiError(409, "email_taken", "That email address is already registered")


def table_unavailable(detail: str = "That table is taken for the requested interval") -> ApiError:
    return ApiError(409, "table_unavailable", detail)


def stale_revision(detail: str = "That reservation has changed since it was read") -> ApiError:
    return ApiError(409, "stale_revision", detail)


def cutoff_passed() -> ApiError:
    return ApiError(
        409,
        "cutoff_passed",
        "This booking can no longer be cancelled or changed: the cancellation cutoff has passed",
    )


def reservation_cancelled() -> ApiError:
    return ApiError(409, "reservation_cancelled", "That reservation is cancelled")


# --- 422 ------------------------------------------------------------------ #
def validation_failed(detail: str) -> ApiError:
    return ApiError(422, "validation_failed", detail)


def not_on_slot_grid() -> ApiError:
    return ApiError(
        422, "not_on_slot_grid", "That start time is not on the restaurant's slot grid"
    )


def outside_opening_hours() -> ApiError:
    return ApiError(
        422,
        "outside_opening_hours",
        "That slot is outside the restaurant's opening hours",
    )


def party_exceeds_capacity() -> ApiError:
    return ApiError(422, "party_exceeds_capacity", "That party does not fit at this table")


def combination_not_allowed(
    detail: str = "Those tables cannot be booked together"
) -> ApiError:
    return ApiError(422, "combination_not_allowed", detail)


def invalid_local_time(detail: str | None = None) -> ApiError:
    return ApiError(
        422,
        "invalid_local_time",
        detail or "That local time does not exist in the restaurant's timezone",
    )
