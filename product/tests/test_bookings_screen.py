"""The `/bookings` screen, exactly as the route serves it.

The same questions the other screens are asked, for the same reasons: the route
returns HTML, the spaces the script fills are in the markup before any script
runs, and nothing on the page comes from anywhere but this service. What the
script draws — the list itself, its groups, an empty room, a signed-out prompt —
is exercised by `tools/ui-check.mjs` against the live service.
"""

from __future__ import annotations

import re

import pytest

from .conftest import NOW, base_fixture, reset, signup
from .test_screens import Page, by_testid, parse, screen


def test_the_bookings_route_is_reachable_and_returns_html(client, seeded):
    page = screen(client, "/bookings")
    assert by_testid(page, "bookings-root") is not None, (
        "the screen must carry the space the list is drawn into"
    )


def test_the_bookings_screen_has_its_status_region_before_any_script(client, seeded):
    """The loading state announces itself; the region cannot be born with it."""
    page = screen(client, "/bookings")
    status = next(
        (el for el in page.elements
         if el["tag"] == "div" and el.get("id") == "bookings-status"),
        None,
    )
    assert status is not None, "no #bookings-status region in the markup"
    assert status.get("role") == "status"
    assert status.get("aria-live") == "polite"


def test_the_bookings_screen_has_no_controls_of_its_own(client, seeded):
    """The list is drawn by the script; the server-rendered page names none."""
    page = screen(client, "/bookings")
    inputs = [el for el in page.elements if el["tag"] in ("input", "select", "button")]
    assert inputs == [], f"unexpected controls in the server markup: {inputs}"


def test_nothing_on_the_bookings_screen_comes_from_another_host(client, seeded):
    page = screen(client, "/bookings")
    external = [
        link for link in page.links
        if re.match(r"^[a-z][a-z0-9+.-]*:", link) or link.startswith("//")
    ]
    assert external == [], f"/bookings reaches outside the service: {external}"


def test_the_bookings_screen_has_a_viewport_meta_and_a_title(client, seeded):
    page = screen(client, "/bookings")
    viewports = [el for el in page.elements if el.get("name") == "viewport"]
    assert viewports, "/bookings has no viewport meta"
    assert "width=device-width" in viewports[0].get("content", "")
    assert any(el["tag"] == "title" for el in page.elements)


@pytest.mark.parametrize("path", ["/", "/signup", "/login", "/lookup", "/bookings",
                                  "/start", "/console"])
def test_every_screen_links_to_the_bookings_screen(client, seeded, path):
    """The list is reachable from anywhere, because losing a reference is how
    diners arrive: the nav always names it, signed in or not."""
    page = screen(client, path)
    links = [el for el in page.elements
             if el["tag"] == "a" and el.get("href") == "/bookings"]
    assert links, f"{path} does not link to /bookings"


def test_the_date_input_does_not_offer_days_in_the_past(client, seeded):
    """The picker starts on the service's own today and refuses earlier days."""
    page = screen(client, "/")
    date_input = by_testid(page, "date-input")
    today = NOW.date().isoformat()
    assert date_input.get("value") == today
    assert date_input.get("min") == today, (
        "the date input must not offer days before the service's today"
    )


def test_the_screen_survives_an_empty_room(client, seeded):
    """No restaurants on the books: the screen still renders its shell."""
    fixture = base_fixture()
    fixture["restaurants"] = []
    reset(client, fixture)
    page = screen(client, "/bookings")
    assert by_testid(page, "bookings-root") is not None


def test_the_screen_does_not_depend_on_being_signed_in(client, seeded):
    """The server cannot know who is asking; the markup is the same either way,
    and the route answers without a token."""
    signup(client)
    response = client.get("/bookings")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = parse(response.text)
    assert by_testid(page, "bookings-root") is not None
