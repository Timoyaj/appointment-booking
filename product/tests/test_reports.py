"""What a restaurant can see about its own room.

The point of these is that the numbers are *true*: a report a restaurant staffs a
shift against has to count what happened, including the bookings that did not,
and it must not invent a figure it cannot know.
"""

from __future__ import annotations

import pytest

from .conftest import (
    THURSDAY,
    FRIDAY,
    assert_ok,
    book,
    closure_body,
    error_of,
    headers_for,
    replan,
    seat,
    signup,
)
from .test_payments import DEPOSIT
from .test_product import open_restaurant

WINDOW = "?from=2026-09-01&to=2026-09-30"


@pytest.fixture
def room(client) -> dict:
    """A restaurant with two tables, an owner, and one diner who books in it."""
    owner = signup(client, "report-owner@example.com", "long enough", "Owner")
    restaurant = open_restaurant(client, owner["token"])
    return {
        "id": restaurant["id"],
        "token": owner["token"],
        "headers": headers_for(owner["token"]),
        "diner": signup(client, "report-diner@example.com")["token"],
    }


def summary_of(client, room, window=WINDOW) -> dict:
    return assert_ok(
        client.get(f"/restaurants/{room['id']}/reports/summary{window}", headers=room["headers"]),
        200,
    )


def test_an_empty_restaurant_reports_zeroes_rather_than_nothing(client, room):
    body = summary_of(client, room)
    assert body["bookings"] == {
        "total": 0, "confirmed": 0, "cancelled": 0, "no_show": 0,
    }
    assert body["covers"]["served"] == 0
    # The room could still have served somebody: the capacity is published hours
    # times seats, not the number of bookings that happened to exist.
    assert body["covers"]["available"] > 0
    assert body["covers"]["utilisation"] == 0.0
    assert body["no_show_rate"] == 0.0
    assert body["lead_time_days"]["average"] is None


def test_confirmed_cancelled_and_no_shows_are_counted_separately(client, room):
    first = assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_1",
        party_size=2, starts_at_local=f"{THURSDAY}T19:00",
    ), 201)
    second = assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_2",
        party_size=4, starts_at_local=f"{THURSDAY}T20:00",
    ), 201)
    third = assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_1",
        party_size=2, starts_at_local=f"{FRIDAY}T19:00",
    ), 201)
    assert_ok(client.post(
        f"/reservations/{second['reference']}/cancel", headers=headers_for(room["diner"])
    ), 200)
    assert_ok(client.post(
        f"/reservations/{third['reference']}/no-show", headers=room["headers"]
    ), 200)

    body = summary_of(client, room)
    assert body["bookings"] == {
        "total": 3, "confirmed": 1, "cancelled": 1, "no_show": 1,
    }
    assert body["covers"]["served"] == 2
    assert body["covers"]["cancelled"] == 4
    assert body["covers"]["no_show"] == 2
    assert body["no_show_rate"] == pytest.approx(1 / 3, abs=0.0001)
    assert body["cancellation_rate"] == pytest.approx(1 / 3, abs=0.0001)
    assert first["status"] == "confirmed"


def test_covers_are_reported_by_the_hour_and_the_day(client, room):
    for at, party in ((f"{THURSDAY}T19:00", 2), (f"{THURSDAY}T19:30", 4),
                      (f"{FRIDAY}T20:00", 2)):
        assert_ok(book(
            client, room["diner"], restaurant_id=room["id"],
            table_id="t_1" if party == 2 else "t_2", party_size=party,
            starts_at_local=at,
        ), 201)
    body = summary_of(client, room)
    assert body["covers_by_hour"] == {"19": 6, "20": 2}
    assert body["covers_by_day"] == {THURSDAY: 6, FRIDAY: 2}


def test_the_window_bounds_what_is_counted(client, room):
    """A booking outside the window is not in it, however real it is."""
    assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_1",
        party_size=2, starts_at_local=f"{THURSDAY}T19:00",
    ), 201)
    assert summary_of(client, room, "?from=2026-10-01&to=2026-10-31")["bookings"]["total"] == 0
    assert summary_of(client, room, f"?from={THURSDAY}&to={THURSDAY}")["bookings"]["total"] == 1
    # The window is inclusive of both ends and of the whole of the last day.
    assert summary_of(client, room, f"?from={THURSDAY}&to={FRIDAY}")["bookings"]["total"] == 1


def test_party_sizes_are_bucketed(client, room):
    for at, party, table in ((f"{THURSDAY}T19:00", 2, "t_1"),
                             (f"{THURSDAY}T21:00", 2, "t_1"),
                             (f"{THURSDAY}T19:00", 4, "t_2")):
        assert_ok(book(
            client, room["diner"], restaurant_id=room["id"], table_id=table,
            party_size=party, starts_at_local=at,
        ), 201)
    body = summary_of(client, room)
    assert body["bookings_by_party_size"] == {"1-2": 2, "3-4": 1}


def test_lead_time_says_how_far_ahead_people_book(client, room):
    """Booked on the Monday (the frozen clock), for the Thursday: three days."""
    assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_1",
        party_size=2, starts_at_local=f"{THURSDAY}T19:00",
    ), 201)
    lead = summary_of(client, room)["lead_time_days"]
    assert lead["bookings_measured"] == 1
    assert lead["average"] == 3.0
    assert lead["same_day"] == 0
    assert lead["within_a_week"] == 1


def test_table_use_says_which_tables_earn_their_space(client, room):
    assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_1",
        party_size=2, starts_at_local=f"{THURSDAY}T19:00",
    ), 201)
    use = {entry["table_id"]: entry for entry in summary_of(client, room)["table_use"]}
    assert use["t_1"]["bookings"] == 1 and use["t_1"]["covers"] == 2
    assert use["t_2"]["bookings"] == 0 and use["t_2"]["covers"] == 0
    # A pair counts for both of the tables it was seated across.
    assert_ok(seat(client, room["diner"], ["t_1", "t_2"],
                   restaurant_id=room["id"], at=f"{FRIDAY}T19:00", party_size=6), 201)
    use = {entry["table_id"]: entry for entry in summary_of(client, room)["table_use"]}
    assert use["t_1"]["bookings"] == 2 and use["t_2"]["bookings"] == 1
    assert use["t_2"]["covers"] == 6, "the whole party, on each table it used"


def test_utilisation_is_measured_against_the_hours_published(client, room):
    """A restaurant that shortens its service sees its own decision, not a benchmark."""
    assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_1",
        party_size=2, starts_at_local=f"{THURSDAY}T19:00",
    ), 201)
    one_day = summary_of(client, room, f"?from={THURSDAY}&to={THURSDAY}")
    # Thursday: 18:00-23:00 is five hours, 90-minute sittings, six seats
    # (t_1's two plus t_2's four).
    assert one_day["sittings_published"] == 3
    assert one_day["covers"]["available"] == 18
    assert one_day["covers"]["utilisation"] == pytest.approx(2 / 18, abs=0.0001)


def test_a_plan_that_moves_a_booking_shows_up_in_the_table_use(client, room):
    """The repair moved somebody; the report must agree with where they are now."""
    seated = assert_ok(seat(client, room["diner"], "t_1", restaurant_id=room["id"],
                            at=f"{THURSDAY}T19:00", party_size=2), 201)
    plan = assert_ok(replan(
        client, room["token"], closure_body("t_1", THURSDAY), restaurant_id=room["id"]
    ), 201)
    assert_ok(client.post(
        f"/restaurants/{room['id']}/replans/{plan['plan_id']}/apply",
        json={}, headers=headers_for(room["token"], "report-apply"),
    ), 201)

    use = {entry["table_id"]: entry for entry in summary_of(client, room)["table_use"]}
    assert use["t_1"]["bookings"] == 0, "the closed table is not seating anybody"
    assert use["t_2"]["bookings"] == 1, "the booking is reported where it now sits"
    assert seated["table_ids"] == ["t_1"]


def test_money_captured_is_reported_and_does_not_count_released_holds(client, room):
    assert_ok(client.put(
        f"/restaurants/{room['id']}/payment-settings", json=DEPOSIT,
        headers=room["headers"],
    ), 200)
    kept = assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_2",
        party_size=4, starts_at_local=f"{THURSDAY}T19:00",
        extra={"payment_method_id": "pm_visa"},
    ), 201)
    given_back = assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_1",
        party_size=2, starts_at_local=f"{FRIDAY}T19:00",
    ), 201)
    assert_ok(client.post(
        f"/reservations/{kept['reference']}/no-show", headers=room["headers"]
    ), 200)
    assert_ok(client.post(
        f"/reservations/{given_back['reference']}/cancel",
        headers=headers_for(room["diner"]),
    ), 200)

    money = summary_of(client, room)["money"]
    assert money["deposits_published"] is True
    assert money["currency"] == "eur"
    # Only the deposit that was kept: a released hold is not revenue.
    assert money["captured_cents"] == 4000


@pytest.mark.parametrize("window, message", [
    ("", "required"),
    ("?from=2026-09-01", "required"),
    ("?from=nonsense&to=2026-09-30", "YYYY-MM-DD"),
    ("?from=2026-09-30&to=2026-09-01", "not be before"),
    ("?from=2020-01-01&to=2026-12-31", "at most"),
])
def test_a_window_that_cannot_be_answered_is_refused(client, room, window, message):
    response = client.get(
        f"/restaurants/{room['id']}/reports/summary{window}", headers=room["headers"]
    )
    assert response.status_code == 422, response.text
    assert error_of(response)["code"] == "validation_failed"
    assert message in response.text


def test_a_report_is_between_a_restaurant_and_its_staff(client, room):
    stranger = signup(client, "curious@example.com")["token"]
    assert client.get(
        f"/restaurants/{room['id']}/reports/summary{WINDOW}",
        headers=headers_for(stranger),
    ).status_code == 404
    # The diner who books here is not staff either.
    assert client.get(
        f"/restaurants/{room['id']}/reports/summary{WINDOW}",
        headers=headers_for(room["diner"]),
    ).status_code == 404


def test_the_csv_carries_every_booking_whatever_became_of_it(client, room):
    kept = assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_2",
        party_size=4, starts_at_local=f"{THURSDAY}T19:00",
    ), 201)
    dropped = assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_1",
        party_size=2, starts_at_local=f"{FRIDAY}T20:00",
    ), 201)
    assert_ok(client.post(
        f"/reservations/{dropped['reference']}/cancel", headers=headers_for(room["diner"])
    ), 200)

    response = client.get(
        f"/restaurants/{room['id']}/reports/bookings.csv{WINDOW}", headers=room["headers"]
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    lines = response.text.strip().split("\n")
    assert lines[0] == (
        "reference,date,time,tables,party_size,status,created_at,"
        "cancellation_cutoff_minutes"
    )
    assert len(lines) == 3
    assert kept["reference"] in lines[1]
    assert ",confirmed," in lines[1]
    assert ",cancelled," in lines[2]
    # The cutoff the diner accepted is in there, which is what a dispute turns on.
    assert lines[1].endswith(",120")


def test_a_csv_cell_cannot_become_a_spreadsheet_formula(client, room, monkeypatch):
    """This file is opened in a spreadsheet, where =, +, - and @ start a formula."""
    from tablekeeper import reports

    assert reports._csv_cell("=cmd|'/c calc'!A1").startswith("'=")
    assert reports._csv_cell("+1").startswith("'+")
    assert reports._csv_cell("@SUM(A1)").startswith("'@")
    assert reports._csv_cell("-2").startswith("'-")
    # A cell with a comma or a quote is quoted, and its quotes are doubled.
    assert reports._csv_cell('a,b') == '"a,b"'
    assert reports._csv_cell('say "hi"') == '"say ""hi"""'


# --------------------------------------------------------------------------- #
# who is coming
# --------------------------------------------------------------------------- #
def test_staff_see_who_is_coming_with_the_name_on_the_booking(client, room):
    booked = assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_2",
        party_size=4, starts_at_local=f"{THURSDAY}T19:00",
    ), 201)
    listed = assert_ok(client.get(
        f"/restaurants/{room['id']}/reservations?from={THURSDAY}&to={THURSDAY}",
        headers=room["headers"],
    ), 200)
    assert listed["from"] == THURSDAY and listed["to"] == THURSDAY
    assert len(listed["reservations"]) == 1
    entry = listed["reservations"][0]
    assert entry["reference"] == booked["reference"]
    assert entry["party_size"] == 4
    assert entry["table_ids"] == ["t_2"]
    assert entry["diner_email"] == "report-diner@example.com"
    assert entry["cancellation_cutoff_minutes"] == 120
    assert entry["status"] == "confirmed"


def test_the_default_window_is_the_coming_week(client, room):
    """A host opening the console gets this week, not an empty form to fill in."""
    near = assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_2",
        party_size=4, starts_at_local=f"{THURSDAY}T19:00",
    ), 201)
    far = assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_2",
        party_size=4, starts_at_local="2026-10-22T19:00",
    ), 201)
    listed = assert_ok(client.get(
        f"/restaurants/{room['id']}/reservations", headers=room["headers"]
    ), 200)
    # The clock is frozen on Monday the 21st, so this is Monday to Sunday.
    assert listed["from"] == "2026-09-21" and listed["to"] == "2026-09-27"
    references = [entry["reference"] for entry in listed["reservations"]]
    assert near["reference"] in references
    assert far["reference"] not in references


def test_a_cancelled_booking_is_still_on_the_list(client, room):
    """A host looking at tonight needs to see what is *not* coming too."""
    booked = assert_ok(book(
        client, room["diner"], restaurant_id=room["id"], table_id="t_2",
        party_size=4, starts_at_local=f"{THURSDAY}T19:00",
    ), 201)
    assert_ok(client.post(
        f"/reservations/{booked['reference']}/cancel", headers=headers_for(room["diner"])
    ), 200)
    listed = assert_ok(client.get(
        f"/restaurants/{room['id']}/reservations?from={THURSDAY}&to={THURSDAY}",
        headers=room["headers"],
    ), 200)
    assert [entry["status"] for entry in listed["reservations"]] == ["cancelled"]


def test_the_guest_list_is_the_restaurants_own(client, room):
    stranger = signup(client, "nosy@example.com")["token"]
    for headers in (headers_for(stranger), headers_for(room["diner"])):
        assert client.get(
            f"/restaurants/{room['id']}/reservations?from={THURSDAY}&to={THURSDAY}",
            headers=headers,
        ).status_code == 404
