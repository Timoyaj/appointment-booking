"""The restaurant's own two screens, exactly as the routes serve them.

Same questions the diner's screens are asked, for the same reasons: the route
returns HTML, the controls are in the markup before any script runs, every input
has a visible label, and nothing on the page comes from anywhere but this
service. A console that needed a CDN would be a console that never renders in the
environment this runs in.
"""

from __future__ import annotations

import re

import pytest

from .test_screens import Page, by_testid, labelled, parse, screen


@pytest.mark.parametrize("path,anchor", [
    ("/start", "start-submit"),
    ("/console", "console-body"),
])
def test_each_console_route_is_reachable_and_returns_html(client, seeded, path, anchor):
    page = screen(client, path)
    assert by_testid(page, anchor) is not None, f"{path} is missing {anchor}"


@pytest.mark.parametrize("path", ["/start", "/console"])
def test_nothing_on_a_console_screen_comes_from_another_host(client, seeded, path):
    page = screen(client, path)
    external = [
        link for link in page.links
        if re.match(r"^[a-z][a-z0-9+.-]*:", link) or link.startswith("//")
    ]
    assert external == [], f"{path} reaches outside the service: {external}"


@pytest.mark.parametrize("path", ["/start", "/console"])
def test_every_console_screen_has_a_viewport_meta_and_a_title(client, seeded, path):
    page = screen(client, path)
    viewports = [el for el in page.elements if el.get("name") == "viewport"]
    assert viewports, f"{path} has no viewport meta"
    assert "width=device-width" in viewports[0].get("content", "")
    assert any(el["tag"] == "title" for el in page.elements)


@pytest.mark.parametrize("testid", [
    "start-name", "start-timezone", "start-slot", "start-duration", "start-cutoff",
])
def test_every_named_console_input_has_a_visible_label(client, seeded, testid):
    assert labelled(screen(client, "/start"), testid), f"{testid} has no label"


def test_the_start_screen_can_describe_a_whole_restaurant(client, seeded):
    """Every part of a restaurant has a control before any script runs."""
    page = screen(client, "/start")
    for testid in ("start-form", "start-name", "start-timezone", "start-slot",
                   "start-duration", "start-cutoff", "add-hours", "add-table",
                   "add-pair", "start-submit"):
        assert by_testid(page, testid) is not None, testid
    # The zone is a text field with a datalist rather than a closed select: a
    # restaurant outside the shortlist must still be able to open.
    zone = by_testid(page, "start-timezone")
    assert zone["tag"] == "input"
    assert zone.get("list") == "zone-list"


def test_the_console_script_is_served_from_the_image(client, seeded):
    response = client.get("/assets/console.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    # The product's own script, served from the package: it drives the console
    # against the API rather than shipping a second copy of the rules.
    assert "/restaurants/mine" in response.text
    assert "/notifications" in response.text


def test_the_console_script_drives_the_managers_new_screens(client, seeded):
    """Deposits, tonight's list and the month's numbers are on the console.

    The panels are built in the browser from the API, so what the route can prove
    is that the served script is the one that talks to those endpoints — the DOM
    checks behind `tools/console-check.mjs` prove the rest against a live service.
    """
    script = client.get("/assets/console.js").text
    for endpoint in ("/payment-settings", "/no-show", "/complete",
                     "/reports/summary", "/reservations"):
        assert endpoint in script, endpoint


def test_the_console_does_not_shadow_the_api(client, seeded):
    """Screens are served after the API, so no endpoint can be hidden by a page."""
    assert client.get("/restaurants").headers["content-type"].startswith(
        "application/json"
    )
    assert client.get("/console").headers["content-type"].startswith("text/html")


def test_a_signed_out_visitor_still_gets_the_screens(client, seeded):
    """The screens are pages, not data: they render for anybody, and the API is
    what refuses an anonymous caller."""
    for path in ("/start", "/console"):
        assert client.get(path).status_code == 200
