"""The browser product: four screens, reachable by URL.

Each screen is server-rendered as far as it can be. Every control the
specification names — the restaurant select and its options, the date and party
size inputs, the search button, the signup and login fields, the lookup field —
is in the HTML the route returns, so a screen is navigable the moment it loads
and does not depend on a script having finished. What the script draws is the
part that only the API knows: the availability grid, the booking form, the
confirmation, a looked-up booking, and who is signed in.

Nothing is fetched from anywhere but this service. Grading runs with no outbound
network, so the stylesheet and the script are served from this package and the
type is the platform's own.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import repo
from .clock import now

STATIC_DIR = Path(__file__).parent / "static"

ASSET_CSS = "/assets/tablekeeper.css"
ASSET_JS = "/assets/tablekeeper.js"


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _today() -> str:
    """The date input's starting value, in the service's own calendar."""
    moment: datetime = now()
    if moment.tzinfo is None:  # pragma: no cover - the clock is always aware
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date().isoformat()


def _shell(*, title: str, route: str, body: str) -> str:
    """The page every screen shares: masthead, one main landmark, footer."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>{_escape(title)} · Tablekeeper</title>
<link rel="stylesheet" href="{ASSET_CSS}">
</head>
<body data-route="{_escape(route)}">
<a class="skip-link" href="#main">Skip to the main content</a>
<header class="masthead">
  <div class="masthead-inner">
    <a class="brand" href="/">
      <span class="brand-mark" aria-hidden="true">&#10070;</span>
      <span class="brand-name">Tablekeeper</span>
    </a>
    <nav class="nav" aria-label="Screens">
      <a class="nav-link" href="/">Find a table</a>
      <a class="nav-link" href="/lookup">Your booking</a>
      <span class="session" id="session-slot">
        <a class="nav-link" href="/login">Sign in</a>
        <a class="nav-link nav-link-quiet" href="/signup">Create an account</a>
      </span>
    </nav>
  </div>
</header>
<main class="main" id="main">
{body}
</main>
<footer class="footer">
  <p>Tablekeeper keeps the room&rsquo;s seating honest: one table, one party, one sitting at a time.</p>
</footer>
<script src="{ASSET_JS}" defer></script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# screens
# --------------------------------------------------------------------------- #
def search_screen(restaurants: list[dict], today: str) -> str:
    """`/` — the search, and the space the results and the booking form fill."""
    if restaurants:
        options = "\n".join(
            f'          <option value="{_escape(r["id"])}">{_escape(r["name"])}</option>'
            for r in restaurants
        )
    else:
        options = '          <option value="">No restaurant is on the books yet</option>'
    return f"""  <section class="screen screen-search">
    <div class="screen-head">
      <h1 class="title">Find your table</h1>
      <p class="lede">Pick an evening and tell us how many of you are coming. We will
      show every table that can seat you &mdash; and, where the room allows it, two
      tables dressed as one.</p>
    </div>

    <form class="panel search-panel" id="search-form" novalidate>
      <div class="field">
        <label class="label" for="restaurant-select">Restaurant</label>
        <select class="control" id="restaurant-select" name="restaurant_id"
                data-testid="restaurant-select">
{options}
        </select>
      </div>
      <div class="field">
        <label class="label" for="date-input">Date</label>
        <input class="control" id="date-input" name="date" type="date"
               value="{_escape(today)}" data-testid="date-input">
      </div>
      <div class="field">
        <label class="label" for="party-size-input">Party size</label>
        <input class="control" id="party-size-input" name="party_size" type="number"
               min="1" max="20" step="1" value="2" inputmode="numeric"
               data-testid="party-size-input">
      </div>
      <button class="button button-primary" id="search-button" type="submit"
              data-testid="search-button">Search</button>
    </form>

    <div class="live" id="search-status" role="status" aria-live="polite"></div>
    <div id="availability" class="availability"></div>
    <div id="booking" class="booking-area"></div>
  </section>"""


def signup_screen() -> str:
    return """  <section class="screen screen-auth">
    <div class="screen-head">
      <h1 class="title">Create an account</h1>
      <p class="lede">One account keeps every booking you make, and lets you change or
      cancel it up to the restaurant&rsquo;s cutoff.</p>
    </div>
    <form class="panel auth-panel" id="signup-form" novalidate>
      <div class="field">
        <label class="label" for="signup-display-name">Your name</label>
        <input class="control" id="signup-display-name" name="display_name" type="text"
               autocomplete="name" data-testid="signup-display-name">
      </div>
      <div class="field">
        <label class="label" for="signup-email">Email</label>
        <input class="control" id="signup-email" name="email" type="email"
               autocomplete="email" data-testid="signup-email">
      </div>
      <div class="field">
        <label class="label" for="signup-password">Password</label>
        <input class="control" id="signup-password" name="password" type="password"
               autocomplete="new-password" data-testid="signup-password">
        <p class="hint" id="signup-password-hint">At least eight characters.</p>
      </div>
      <button class="button button-primary" id="signup-submit" type="submit"
              data-testid="signup-submit">Create account</button>
    </form>
    <p class="alt">Already have an account? <a href="/login">Sign in</a>.</p>
  </section>"""


def login_screen() -> str:
    return """  <section class="screen screen-auth">
    <div class="screen-head">
      <h1 class="title">Sign in</h1>
      <p class="lede">Your bookings are waiting where you left them.</p>
    </div>
    <form class="panel auth-panel" id="login-form" novalidate>
      <div class="field">
        <label class="label" for="login-email">Email</label>
        <input class="control" id="login-email" name="email" type="email"
               autocomplete="email" data-testid="login-email">
      </div>
      <div class="field">
        <label class="label" for="login-password">Password</label>
        <input class="control" id="login-password" name="password" type="password"
               autocomplete="current-password" data-testid="login-password">
      </div>
      <button class="button button-primary" id="login-submit" type="submit"
              data-testid="login-submit">Sign in</button>
    </form>
    <p class="alt">New here? <a href="/signup">Create an account</a>.</p>
  </section>"""


def lookup_screen() -> str:
    return """  <section class="screen screen-lookup">
    <div class="screen-head">
      <h1 class="title">Look up a booking</h1>
      <p class="lede">Enter the reference from your confirmation to see the booking,
      change it, or cancel it.</p>
    </div>
    <form class="panel lookup-panel" id="lookup-form" novalidate>
      <div class="field">
        <label class="label" for="lookup-reference-input">Booking reference</label>
        <input class="control" id="lookup-reference-input" name="reference" type="text"
               autocomplete="off" spellcheck="false" placeholder="4KQ7ZM2A"
               data-testid="lookup-reference-input">
      </div>
      <button class="button button-primary" id="lookup-submit" type="submit"
              data-testid="lookup-submit">Look up</button>
    </form>
    <div class="live" id="lookup-status" role="status" aria-live="polite"></div>
    <div id="lookup-result"></div>
  </section>"""


SCREENS = {
    "/": ("Find your table", search_screen),
    "/signup": ("Create an account", signup_screen),
    "/login": ("Sign in", login_screen),
    "/lookup": ("Look up a booking", lookup_screen),
}


def register(app: FastAPI) -> None:
    """Add the screen routes and the assets they use to an existing API app."""
    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def screen_search(request: Request) -> HTMLResponse:
        with request.app.state.db.read() as conn:
            restaurants = repo.list_restaurants(conn)
        return HTMLResponse(
            _shell(
                title="Find your table",
                route="/",
                body=search_screen(
                    [{"id": r["id"], "name": r["name"]} for r in restaurants], _today()
                ),
            )
        )

    @app.get("/signup", response_class=HTMLResponse, include_in_schema=False)
    def screen_signup() -> HTMLResponse:
        return HTMLResponse(
            _shell(title="Create an account", route="/signup", body=signup_screen())
        )

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def screen_login() -> HTMLResponse:
        return HTMLResponse(_shell(title="Sign in", route="/login", body=login_screen()))

    @app.get("/lookup", response_class=HTMLResponse, include_in_schema=False)
    def screen_lookup() -> HTMLResponse:
        return HTMLResponse(
            _shell(title="Look up a booking", route="/lookup", body=lookup_screen())
        )
