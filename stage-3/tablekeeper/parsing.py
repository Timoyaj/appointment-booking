"""Request parsing with the exact error semantics the spec asks for.

The rules are fiddly enough that they get their own module:

* a body that does not parse as JSON, or a field of the wrong JSON type, is
  ``400 malformed_request``;
* a missing required field or query parameter is ``422 validation_failed``;
* a field of the right type but an invalid format or out-of-range value is
  ``422 validation_failed``;
* ``party_size`` is called out explicitly — strings and booleans are 422, not 400;
* ``starts_at_local`` must be a bare local ``YYYY-MM-DDTHH:MM`` — a string with
  an offset or a ``Z`` is 422, while a non-string is 400;
* an integer **query** parameter must be written as plain decimal digits, so
  ``1e9``, ``4.0`` and ``+4`` are 422 whatever their value;
* a booking names its tables with ``table_ids``, or with ``table_id`` for a set of
  one. Sending both is 422, an array that is not an array is 400, and an empty or
  repeated entry inside it is 422;
* unknown body fields and unknown query parameters are ignored, never an error.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from .errors import malformed_request, validation_failed
from .tztime import parse_local

MAX_ID_LENGTH = 64
MAX_IDEMPOTENCY_KEY_LENGTH = 255

DIGITS_RE = re.compile(r"^[0-9]+$")


def parse_object(raw: bytes | str | None) -> dict:
    """Parse a request body into a JSON object, or 400."""
    if raw is None:
        raise malformed_request("A JSON request body is required")
    if isinstance(raw, bytes):
        if not raw.strip():
            raise malformed_request("A JSON request body is required")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise malformed_request("Body is not valid UTF-8") from exc
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise malformed_request(f"Body is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise malformed_request("Body must be a JSON object")
    return parsed


def parse_optional_object(raw: bytes | str | None) -> dict:
    """For endpoints where a body is allowed but not required (cancel)."""
    if raw is None or (isinstance(raw, bytes) and not raw.strip()) or raw == "":
        return {}
    return parse_object(raw)


def _is_wrong_type(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, list, dict))


def string_field(
    body: Mapping[str, Any],
    name: str,
    *,
    required: bool = True,
    max_length: int | None = None,
    allow_null_as_absent: bool = False,
) -> str | None:
    if name not in body:
        if required:
            raise validation_failed(f"'{name}' is required")
        return None
    value = body[name]
    if value is None and allow_null_as_absent:
        return None
    if not isinstance(value, str):
        raise malformed_request(f"'{name}' must be a string")
    limit = MAX_ID_LENGTH if max_length is None else max_length
    if len(value) > limit:
        raise validation_failed(f"'{name}' must be at most {limit} characters")
    if required and value == "":
        raise validation_failed(f"'{name}' must not be empty")
    return value


def id_field(body: Mapping[str, Any], name: str, *, required: bool = True) -> str | None:
    """An opaque identifier: at most 64 characters."""
    return string_field(
        body, name, required=required, max_length=MAX_ID_LENGTH, allow_null_as_absent=not required
    )


def table_set_field(
    body: Mapping[str, Any], *, required: bool = True
) -> list[str] | None:
    """The tables a booking holds: ``table_ids``, or ``table_id`` as a set of one.

    Sending both is refused rather than quietly preferring one — a request that
    names two different seatings does not say which one the diner meant. A
    ``null`` in either field is treated as absent, as elsewhere.
    """
    has_ids = body.get("table_ids") is not None
    has_one = body.get("table_id") is not None
    if has_ids and has_one:
        raise validation_failed("Send 'table_ids' or 'table_id', not both")

    if has_one:
        return [id_field(body, "table_id")]  # type: ignore[return-value]

    if has_ids:
        value = body["table_ids"]
        if not isinstance(value, list):
            raise malformed_request("'table_ids' must be an array of table ids")
        if not value:
            raise validation_failed("'table_ids' must not be empty")
        table_ids: list[str] = []
        for index, entry in enumerate(value):
            if not isinstance(entry, str):
                raise malformed_request(f"'table_ids[{index}]' must be a string")
            if entry == "":
                raise validation_failed(f"'table_ids[{index}]' must not be empty")
            if len(entry) > MAX_ID_LENGTH:
                raise validation_failed(
                    f"'table_ids[{index}]' must be at most {MAX_ID_LENGTH} characters"
                )
            if entry in table_ids:
                raise validation_failed(
                    f"'table_ids' names table '{entry}' more than once"
                )
            table_ids.append(entry)
        return table_ids

    if required:
        raise validation_failed("'table_id' or 'table_ids' is required")
    return None


def local_time_field(body: Mapping[str, Any], name: str = "starts_at_local") -> str:
    """A bare local ``YYYY-MM-DDTHH:MM`` wall-clock string."""
    if name not in body:
        raise validation_failed(f"'{name}' is required")
    value = body[name]
    if not isinstance(value, str):
        raise malformed_request(f"'{name}' must be a string")
    if parse_local(value) is None:
        raise validation_failed(
            f"'{name}' must be a bare local time in YYYY-MM-DDTHH:MM form, with no "
            "offset and no 'Z'"
        )
    return value


def optional_local_time_field(
    body: Mapping[str, Any], name: str = "starts_at_local"
) -> str | None:
    if name not in body or body[name] is None:
        return None
    return local_time_field(body, name)


def party_size_field(body: Mapping[str, Any], *, required: bool = True) -> int | None:
    """Party size, with the spec's explicit rule: strings and booleans are 422."""
    if "party_size" not in body or body["party_size"] is None:
        if not required:
            return None
        raise validation_failed(
            "'party_size' is required" if "party_size" not in body
            else "'party_size' must be an integer"
        )
    value = body["party_size"]
    # A JSON boolean is not an integer, however Python types it.
    if not isinstance(value, int) or isinstance(value, bool):
        raise validation_failed("'party_size' must be an integer")
    if value < 1:
        raise validation_failed("'party_size' must be at least 1")
    return int(value)


def optional_party_size_field(body: Mapping[str, Any]) -> int | None:
    """`party_size` when present, otherwise None (amendments send a subset)."""
    if "party_size" not in body or body["party_size"] is None:
        return None
    return party_size_field(body)


def expected_revision_field(body: Mapping[str, Any]) -> int | None:
    """`expected_revision`: the revision the client believes it is changing.

    Optional, and omitting it keeps the earlier semantics. A positive integer that
    differs from the stored revision is a conflict the service reports; anything
    that is not a positive integer is refused here, before any of that.
    """
    if "expected_revision" not in body or body["expected_revision"] is None:
        return None
    value = body["expected_revision"]
    if not isinstance(value, int) or isinstance(value, bool):
        raise validation_failed("'expected_revision' must be an integer")
    if value < 1:
        raise validation_failed("'expected_revision' must be at least 1")
    return int(value)


def query_string(
    params: Mapping[str, str], name: str, *, required: bool = True
) -> str | None:
    value = params.get(name)
    if value is None or value == "":
        if required:
            raise validation_failed(f"query parameter '{name}' is required")
        return None
    return value


def query_int(params: Mapping[str, str], name: str, *, required: bool = True) -> int | None:
    """An integer query parameter, written as plain decimal digits."""
    value = params.get(name)
    if value is None or value == "":
        if required:
            raise validation_failed(f"query parameter '{name}' is required")
        return None
    if not DIGITS_RE.match(value):
        raise validation_failed(
            f"query parameter '{name}' must be written as plain decimal digits"
        )
    try:
        return int(value)
    except ValueError as exc:  # pragma: no cover - regex guarantees digits
        raise validation_failed(f"query parameter '{name}' is not a valid integer") from exc


def idempotency_key(header: str | None) -> str:
    """``Idempotency-Key``: 1..255 characters, required on the two write paths."""
    if header is None or header.strip() == "":
        from .errors import missing_idempotency_key

        raise missing_idempotency_key()
    if len(header) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise validation_failed(
            f"Idempotency-Key must be at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters"
        )
    return header
