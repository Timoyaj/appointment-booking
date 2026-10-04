"""The four screens, exactly as the routes serve them.

These check what the server is responsible for: that each screen route returns
HTML rather than JSON, that every control the specification names is in the
markup before any script has run, that each input has a visible label, and that
nothing on the page comes from anywhere but this service — the graded run has no
outbound network, so a page that reaches for a CDN is a page that never renders.

What the script draws (the grid, the booking form, the confirmation, a looked-up
booking, who is signed in) is exercised by ``tools/ui-check.mjs``, which loads
these same screens into a DOM and drives the product's own script against the
live service.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from .conftest import assert_ok, base_fixture, reset, signup


class Page(HTMLParser):
    """Just enough of a parser to ask the markup questions."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.testids: list[str] = []
        self.elements: list[dict] = []
        self.links: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        self.elements.append({"tag": tag, **attrs})
        if "data-testid" in attrs:
            self.testids.append(attrs["data-testid"])
        for name in ("href", "src"):
            if attrs.get(name):
                self.links.append(attrs[name])

    def handle_data(self, data):
        if data.strip():
            self.text.append(data.strip())


def parse(markup: str) -> Page:
    page = Page()
    page.feed(markup)
    return page


def screen(client, path: str) -> Page:
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html"), (
        f"{path} must return HTML, not {response.headers['content-type']}"
    )
    assert "<!doctype html>" in response.text.lower()
    return parse(response.text)


def by_testid(page: Page, name: str) -> dict | None:
    return next((el for el in page.elements if el.get("data-testid") == name), None)


def labelled(page: Page, testid: str) -> bool:
    """A control the specification names must have a visible label of its own."""
    control = by_testid(page, testid)
    assert control is not None, f"no {testid} on the page"
    control_id = control.get("id")
    assert control_id, f"{testid} has no id, so no label can point at it"
    return any(
        el["tag"] == "label" and el.get("for") == control_id for el in page.elements
    )


@pytest.mark.parametrize("path,anchor", [
    ("/", "search-button"),
    ("/signup", "signup-submit"),
    ("/login", "login-submit"),
    ("/lookup", "lookup-submit"),
])
def test_each_screen_route_is_reachable_and_returns_html(client, seeded, path, anchor):
    page = screen(client, path)
    assert by_testid(page, anchor) is not None, f"{path} is missing {anchor}"


@pytest.mark.parametrize("path", ["/", "/signup", "/login", "/lookup"])
def test_nothing_on_a_screen_comes_from_another_host(client, seeded, path):
    """The graded run has no outbound network: no CDN, no font, no analytics."""
    page = screen(client, path)
    external = [
        link for link in page.links
        if re.match(r"^[a-z][a-z0-9+.-]*:", link) or link.startswith("//")
    ]
    assert external == [], f"{path} reaches outside the service: {external}"


@pytest.mark.parametrize("path", ["/", "/signup", "/login", "/lookup"])
def test_every_screen_has_a_viewport_meta_and_a_title(client, seeded, path):
    page = screen(client, path)
    viewports = [el for el in page.elements if el.get("name") == "viewport"]
    assert viewports, f"{path} has no viewport meta, so 375px is a guess"
    assert "width=device-width" in viewports[0].get("content", "")
    assert any(el["tag"] == "title" for el in page.elements)


def test_the_search_controls_are_all_present_and_typed(client, seeded):
    page = screen(client, "/")
    for testid in ("restaurant-select", "date-input", "party-size-input", "search-button"):
        assert by_testid(page, testid) is not None, testid
    assert by_testid(page, "date-input")["type"] == "date"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", by_testid(page, "date-input").get("value", "")), (
        "the date input starts on a real calendar date"
    )
    assert by_testid(page, "party-size-input")["type"] == "number"
    assert by_testid(page, "search-button")["type"] == "submit"


@pytest.mark.parametrize("testid", [
    "restaurant-select", "date-input", "party-size-input",
    "signup-email", "signup-password", "signup-display-name",
    "login-email", "login-password", "lookup-reference-input",
])
def test_every_named_input_has_a_visible_label(client, seeded, testid):
    path = "/"
    if testid.startswith("signup"):
        path = "/signup"
    elif testid.startswith("login"):
        path = "/login"
    elif testid.startswith("lookup"):
        path = "/lookup"
    assert labelled(screen(client, path), testid), f"{testid} has no label"


def test_the_restaurant_select_lists_every_restaurant_by_id(client):
    fixture = base_fixture()
    fixture["restaurants"].append({
        **fixture["restaurants"][0], "id": "r_bake", "name": "Bakery Nine",
    })
    reset(client, fixture)
    page = screen(client, "/")
    select = by_testid(page, "restaurant-select")
    assert select["tag"] == "select", "restaurant-select must be a real select"
    options = [el for el in page.elements if el["tag"] == "option"]
    assert [option.get("value") for option in options] == ["r_anker", "r_bake"]


def test_the_select_is_rendered_by_the_server_not_left_to_the_script(client, seeded):
    """A screen that waits for a script to fill its select cannot be searched."""
    markup = client.get("/").text
    assert '<option value="r_anker">Zum Anker</option>' in markup


def test_a_restaurant_name_is_escaped_into_the_page(client):
    fixture = base_fixture()
    fixture["restaurants"][0]["name"] = 'Zum <script>alert("x")</script> Anker'
    reset(client, fixture)
    markup = client.get("/").text
    assert "<script>alert" not in markup, "the name was injected as markup"
    assert "&lt;script&gt;" in markup
    # A diner still reads the name the restaurant gave, entities and all.
    assert 'Zum <script>alert("x")</script> Anker' in parse(markup).text


def test_no_session_elements_are_rendered_for_a_signed_out_diner(client, seeded):
    """`current-user` and `logout-button` exist only when somebody is signed in."""
    for path in ("/", "/signup", "/login", "/lookup"):
        page = screen(client, path)
        assert by_testid(page, "current-user") is None, path
        assert by_testid(page, "logout-button") is None, path


def test_the_auth_screens_carry_no_error_before_there_is_one(client, seeded):
    for path in ("/signup", "/login", "/"):
        assert by_testid(screen(client, path), "auth-error") is None, path


def test_the_lookup_screen_carries_no_result_before_a_lookup(client, seeded):
    page = screen(client, "/lookup")
    for testid in ("reservation-detail", "reservation-status",
                   "reservation-cancel-button", "reservation-error"):
        assert by_testid(page, testid) is None, testid


def test_the_search_screen_carries_no_results_before_a_search(client, seeded):
    page = screen(client, "/")
    for testid in ("availability-grid", "no-slots", "booking-form", "booking-summary",
                   "booking-party-size", "booking-submit", "booking-error",
                   "confirmation", "confirmation-reference"):
        assert by_testid(page, testid) is None, testid


def test_the_assets_are_served_by_the_service_itself(client, seeded):
    stylesheet = client.get("/assets/tablekeeper.css")
    script = client.get("/assets/tablekeeper.js")
    assert stylesheet.status_code == 200, stylesheet.text
    assert script.status_code == 200, script.text
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert "javascript" in script.headers["content-type"]
    # The states the specification names must be visually distinguishable.
    for state in ('[data-available="true"]', '[data-available="false"]',
                  ".cell-selected", ".notice-error", ".notice-uncertain",
                  ".notice-success", ".spinner"):
        assert state in stylesheet.text, f"the stylesheet has no {state} state"
    for width in ("max-width: 44rem", "max-width: 24rem"):
        assert width in stylesheet.text, f"no narrow-viewport rule for {width}"


def test_the_script_names_every_state_it_has_to_draw(client, seeded):
    script = client.get("/assets/tablekeeper.js").text
    for testid in ("availability-grid", "no-slots", "booking-form", "booking-summary",
                   "booking-party-size", "booking-submit", "booking-error",
                   "booking-uncertain", "confirmation", "confirmation-reference",
                   "confirmation-details", "confirmation-tables", "reservation-detail",
                   "reservation-status", "reservation-tables", "reservation-cancel-button",
                   "reservation-error", "current-user", "logout-button", "auth-error"):
        assert testid in script, f"the script never draws {testid}"


def test_a_screen_route_does_not_disturb_the_api(client, seeded):
    assert_ok(client.get("/health"), 200)
    assert_ok(client.get("/restaurants"), 200)
    token = signup(client)["token"]
    from .conftest import headers_for

    listed = assert_ok(client.get("/reservations", headers=headers_for(token)), 200)
    assert listed == {"reservations": []}
    # An unknown path still gets the API's JSON envelope, not a screen.
    response = client.get("/no-such-screen")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
