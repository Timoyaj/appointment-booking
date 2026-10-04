"""Stage 3: explaining why a table is or is not offered.

Two rules decide a table for a slot — the party fits it, and nothing confirmed
overlaps it — and they are independent. An explanation reports both for every
table of the restaurant, whether or not some other rule already excluded it, so a
diner is never left guessing which of the two said no.
"""

from __future__ import annotations

import pytest

from .conftest import (
    SATURDAY,
    THURSDAY,
    assert_ok,
    base_fixture,
    book,
    error_of,
    headers_for,
    login_headers,
    managed_fixture,
    policy_body,
    publish,
    reset,
    signup,
)

RULES = ["capacity", "no_overlap"]


def slots(client, party_size: int, date: str = THURSDAY, explain: str | None = "true"):
    params = {"restaurant_id": "r_anker", "date": date, "party_size": party_size}
    if explain is not None:
        params["explain"] = explain
    return assert_ok(client.get("/availability", params=params), 200)["slots"]


def slot_at(client, party_size: int, hhmm: str = "19:00", **kwargs) -> dict:
    found = [s for s in slots(client, party_size, **kwargs) if s["starts_at_local"].endswith(hhmm)]
    assert found, f"no slot at {hhmm}"
    return found[0]


def rules_of(entry: dict) -> dict[str, bool]:
    return {rule["rule"]: rule["holds"] for rule in entry["rules"]}


# --------------------------------------------------------------------------- #
# the shape of an explanation
# --------------------------------------------------------------------------- #
def test_every_table_appears_exactly_once_in_fixture_order(client, seeded):
    slot = slot_at(client, 4)
    assert [entry["table_id"] for entry in slot["explain"]] == ["t_1", "t_2"]


def test_both_rules_are_reported_for_every_table_in_order(client, seeded):
    for entry in slot_at(client, 2)["explain"]:
        assert [rule["rule"] for rule in entry["rules"]] == RULES


def test_a_rule_that_holds_is_reported_holding(client, seeded):
    """A table another rule already excluded still gets its full account."""
    explained = {e["table_id"]: rules_of(e) for e in slot_at(client, 4)["explain"]}
    # t_1 holds two, so a party of four cannot fit it — but nothing has booked it.
    assert explained["t_1"] == {"capacity": False, "no_overlap": True}
    assert explained["t_2"] == {"capacity": True, "no_overlap": True}


def test_a_table_excluded_by_both_rules_reports_both_false(client, seeded):
    token = signup(client)["token"]
    assert_ok(book(client, token, table_id="t_2", party_size=4), 201)
    explained = {e["table_id"]: rules_of(e) for e in slot_at(client, 4)["explain"]}
    assert explained["t_2"] == {"capacity": True, "no_overlap": False}
    assert explained["t_1"] == {"capacity": False, "no_overlap": True}

    fixture = base_fixture()
    fixture["restaurants"][0]["tables"].append({"id": "t_3", "label": "3", "capacity": 6})
    reset(client, fixture)
    token = signup(client, email="second@example.com")["token"]
    assert_ok(book(client, token, table_id="t_1", party_size=2), 201)
    explained = {e["table_id"]: rules_of(e) for e in slot_at(client, 4)["explain"]}
    assert explained["t_1"] == {"capacity": False, "no_overlap": False}, \
        "too small and taken: both rules are still reported"


def test_available_is_true_exactly_when_both_rules_hold(client, seeded):
    token = signup(client)["token"]
    assert_ok(book(client, token, table_id="t_2", party_size=4), 201)
    for slot in slots(client, 2):
        for entry in slot["explain"]:
            holds = rules_of(entry)
            assert entry["available"] == (holds["capacity"] and holds["no_overlap"])


def test_the_available_entries_are_exactly_available_table_ids_in_order(client, seeded):
    token = signup(client)["token"]
    assert_ok(book(client, token, table_id="t_2", party_size=4,
                   starts_at_local=f"{THURSDAY}T19:00"), 201)
    for slot in slots(client, 2):
        assert [e["table_id"] for e in slot["explain"] if e["available"]] == \
            slot["available_table_ids"]


def test_a_slot_with_no_free_table_still_explains_every_table(client, seeded):
    token = signup(client)["token"]
    for table in ("t_1", "t_2"):
        assert_ok(book(client, token, table_id=table, party_size=2,
                       starts_at_local=f"{THURSDAY}T19:00"), 201)
    slot = slot_at(client, 2)
    assert slot["available_table_ids"] == []
    assert [e["table_id"] for e in slot["explain"]] == ["t_1", "t_2"]
    assert all(e["available"] is False for e in slot["explain"])
    assert all(rules_of(e)["no_overlap"] is False for e in slot["explain"])


def test_a_closed_day_still_returns_no_slots(client, seeded):
    assert slots(client, 2, date=SATURDAY) == []


def test_an_explanation_carries_the_policy_that_decided_it(client, seeded):
    assert all(e["policy_version"] == 0 for e in slot_at(client, 2)["explain"])


def test_an_explanation_names_the_published_policy(client, seeded):
    fixture = managed_fixture()
    reset(client, fixture)
    token = login_headers(client)["Authorization"].split(" ", 1)[1]
    assert_ok(publish(client, token, policy_body(effective_from=THURSDAY)), 201)
    assert all(e["policy_version"] == 1 for e in slot_at(client, 2)["explain"])


def test_an_explanation_follows_a_published_capacity(client, seeded):
    fixture = managed_fixture()
    reset(client, fixture)
    token = login_headers(client)["Authorization"].split(" ", 1)[1]
    assert_ok(publish(client, token, policy_body(capacities={"t_1": 6, "t_2": 1})), 201)
    explained = {e["table_id"]: rules_of(e) for e in slot_at(client, 4)["explain"]}
    assert explained["t_1"]["capacity"] is True, "the policy grew t_1"
    assert explained["t_2"]["capacity"] is False, "and shrank t_2"
    assert slot_at(client, 4)["available_table_ids"] == ["t_1"]


def test_explaining_a_combination_is_about_its_tables(client, seeded):
    """`explain` accounts for tables; the pairs a party could take stay in
    `available_options`, which is where a combination is offered."""
    fixture = base_fixture()
    fixture["restaurants"][0]["combinable"] = [["t_1", "t_2"]]
    reset(client, fixture)
    slot = slot_at(client, 6)
    assert [e["table_id"] for e in slot["explain"]] == ["t_1", "t_2"]
    assert {"table_ids": ["t_1", "t_2"], "capacity": 6} in slot["available_options"]


# --------------------------------------------------------------------------- #
# the parameter itself
# --------------------------------------------------------------------------- #
def test_without_explain_the_response_keeps_its_earlier_shape(client, seeded):
    for slot in slots(client, 2, explain=None):
        assert "explain" not in slot
        assert set(slot) == {
            "starts_at_local", "starts_at", "available_table_ids", "available_options"}


@pytest.mark.parametrize("value", ["false", "1", "0", "", "TRUE", "True", "yes", "true ", " true"])
def test_explain_accepts_only_the_value_true(client, seeded, value):
    response = client.get("/availability", params={
        "restaurant_id": "r_anker", "date": THURSDAY, "party_size": 2, "explain": value})
    assert response.status_code == 422, repr(value)
    assert error_of(response)["code"] == "validation_failed"


def test_explain_is_optional_and_off_by_default(client, seeded):
    body = assert_ok(client.get("/availability", params={
        "restaurant_id": "r_anker", "date": THURSDAY, "party_size": 2}), 200)
    assert all("explain" not in slot for slot in body["slots"])


def test_explaining_does_not_need_a_token(client, seeded):
    response = client.get("/availability", params={
        "restaurant_id": "r_anker", "date": THURSDAY, "party_size": 2, "explain": "true"})
    assert response.status_code == 200


def test_explain_is_refused_before_the_rest_of_the_query_is_used(client, seeded):
    response = client.get("/availability", params={
        "restaurant_id": "r_nope", "date": THURSDAY, "party_size": 2, "explain": "nope"})
    assert error_of(response)["code"] == "validation_failed"


def test_a_bad_explain_does_not_hide_a_missing_parameter(client, seeded):
    response = client.get("/availability", params={"restaurant_id": "r_anker",
                                                   "explain": "true"})
    assert error_of(response)["code"] == "validation_failed"
    assert "date" in response.json()["error"]["message"]


def test_every_slot_of_the_day_is_explained(client, seeded):
    token = signup(client)["token"]
    assert_ok(book(client, token, table_id="t_2", party_size=4,
                   starts_at_local=f"{THURSDAY}T19:00"), 201)
    explained = slots(client, 4)
    assert len(explained) == 8
    taken = {slot["starts_at_local"][-5:]: [e["table_id"] for e in slot["explain"]
                                            if not rules_of(e)["no_overlap"]]
             for slot in explained}
    # A 90-minute sitting from 19:00 overlaps 18:00 through 20:00, and not 20:30.
    for hhmm in ("18:00", "18:30", "19:00", "19:30", "20:00"):
        assert taken[hhmm] == ["t_2"], hhmm
    assert taken["20:30"] == []


def test_explanations_are_not_affected_by_another_diner_party_size(client, seeded):
    """Each request explains itself: the rules are reported for the party asked about."""
    small = {e["table_id"]: rules_of(e) for e in slot_at(client, 2)["explain"]}
    large = {e["table_id"]: rules_of(e) for e in slot_at(client, 4)["explain"]}
    assert small["t_1"]["capacity"] is True
    assert large["t_1"]["capacity"] is False
    assert small["t_2"]["capacity"] is True and large["t_2"]["capacity"] is True
