"""The screens a restaurant's own people use: open a restaurant, then run it.

The four screens in :mod:`tablekeeper.webui` are the diner's. These two are the
restaurant's, and the difference is the point: everything a manager has been able
to do since stage 3 — publish a policy, plan around a closed table, apply the plan
— has been reachable only by composing JSON against the API. A product cannot ask
a restaurant manager to do that, so this module gives them a page.

Both screens are server-rendered as far as the server can see and driven by
``static/console.js``, which talks to the same API a diner's browser does. That
is deliberate: the console is not a privileged back door, it is an ordinary
client of the API, so every rule about who may do what is enforced in one place —
the service — rather than being duplicated in a template.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .webui import _escape, _shell

# A short list of zones with a datalist rather than a select: the IANA database
# has hundreds, and a restaurant that is not on the list must still be able to
# open. The service is the thing that decides whether a zone is real.
COMMON_ZONES = (
    "Africa/Lagos",
    "Africa/Nairobi",
    "Africa/Johannesburg",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Sydney",
    "Europe/London",
    "Europe/Dublin",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Amsterdam",
)

WEEKDAY_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def start_screen() -> str:
    """The one screen that turns a person with an account into a restaurant."""
    zones = "".join(f'<option value="{_escape(z)}"></option>' for z in COMMON_ZONES)
    days = "".join(
        f'<option value="{day}">{day.capitalize()}</option>' for day in WEEKDAY_ORDER
    )
    return f"""
<section class="screen screen-console">
  <header class="screen-head">
    <h1 class="title">Open your restaurant</h1>
    <p class="lede">Describe your room and your service once. You become its owner,
      you can add your staff straight after, and diners can book from the moment
      you save.</p>
  </header>

  <div class="live" id="start-status" role="status" aria-live="polite"></div>
  <div id="start-result"></div>

  <form class="panel console-panel" id="start-form" novalidate data-testid="start-form">
    <fieldset class="console-fieldset">
      <legend class="label">Your restaurant</legend>
      <div class="field">
        <label class="label" for="start-name">Name</label>
        <input class="control" id="start-name" name="name" type="text"
               autocomplete="organization" data-testid="start-name" required>
      </div>
      <div class="field">
        <label class="label" for="start-timezone">Timezone</label>
        <input class="control" id="start-timezone" name="timezone" type="text"
               list="zone-list" value="Europe/Berlin" data-testid="start-timezone">
        <datalist id="zone-list">{zones}</datalist>
        <p class="hint">An IANA zone name, such as <code>Europe/Berlin</code>. Your
          service hours are local to this zone.</p>
      </div>
    </fieldset>

    <fieldset class="console-fieldset">
      <legend class="label">Your service</legend>
      <div class="console-grid">
        <div class="field">
          <label class="label" for="start-slot">Slot minutes</label>
          <input class="control" id="start-slot" name="slot_minutes" type="number"
                 min="1" max="1440" value="30" data-testid="start-slot">
          <p class="hint">Sittings start on this grid.</p>
        </div>
        <div class="field">
          <label class="label" for="start-duration">Sitting minutes</label>
          <input class="control" id="start-duration" name="reservation_duration_minutes"
                 type="number" min="1" max="1440" value="90"
                 data-testid="start-duration">
          <p class="hint">How long a table is held.</p>
        </div>
        <div class="field">
          <label class="label" for="start-cutoff">Cancellation cutoff</label>
          <input class="control" id="start-cutoff" name="cancellation_cutoff_minutes"
                 type="number" min="0" max="10080" value="120"
                 data-testid="start-cutoff">
          <p class="hint">Minutes before a sitting that a diner may still change it.</p>
        </div>
      </div>
    </fieldset>

    <fieldset class="console-fieldset">
      <legend class="label">Opening hours</legend>
      <p class="hint">One row per service. Delete the ones you do not need.</p>
      <div id="hours-rows" class="console-rows"></div>
      <button class="button button-quiet button-small" type="button" id="add-hours"
              data-testid="add-hours">Add a service</button>
    </fieldset>

    <fieldset class="console-fieldset">
      <legend class="label">Tables</legend>
      <p class="hint">Give each table the name your staff say out loud, and how many
        it seats.</p>
      <div id="table-rows" class="console-rows"></div>
      <button class="button button-quiet button-small" type="button" id="add-table"
              data-testid="add-table">Add a table</button>
    </fieldset>

    <fieldset class="console-fieldset">
      <legend class="label">Tables that join</legend>
      <p class="hint">A pair can be booked as one seating for a larger party.
        Combining is not transitive: declaring A&ndash;B and B&ndash;C does not
        make A&ndash;C bookable.</p>
      <div id="pair-rows" class="console-rows"></div>
      <button class="button button-quiet button-small" type="button" id="add-pair"
              data-testid="add-pair">Add a pair</button>
    </fieldset>

    <button class="button button-primary" id="start-submit" type="submit"
            data-testid="start-submit">Open my restaurant</button>
  </form>
</section>

<template id="hours-row-template">
  <div class="console-row hours-row">
    <label class="label visually-hidden" for="hours-weekday">Weekday</label>
    <select class="control hours-weekday" id="hours-weekday">{days}</select>
    <input class="control hours-opens" type="time" value="18:00" aria-label="Opens">
    <input class="control hours-closes" type="time" value="23:00" aria-label="Closes">
    <button class="button button-danger button-small remove-row" type="button"
            aria-label="Remove this service">Remove</button>
  </div>
</template>

<template id="table-row-template">
  <div class="console-row table-row">
    <input class="control table-label" type="text" placeholder="Label, e.g. 12"
           aria-label="Table label">
    <input class="control table-capacity" type="number" min="1" max="100" value="2"
           aria-label="Seats">
    <button class="button button-danger button-small remove-row" type="button"
            aria-label="Remove this table">Remove</button>
  </div>
</template>

<template id="pair-row-template">
  <div class="console-row pair-row">
    <select class="control pair-first" aria-label="First table"></select>
    <select class="control pair-second" aria-label="Second table"></select>
    <button class="button button-danger button-small remove-row" type="button"
            aria-label="Remove this pair">Remove</button>
  </div>
</template>
"""


def console_screen() -> str:
    """The manager's dashboard: the room, the people, the messages, the record."""
    return """
<section class="screen screen-console">
  <header class="screen-head">
    <h1 class="title">Your restaurant</h1>
    <p class="lede">The room as your diners see it, the people who run it, what your
      diners have been told, and what has been done here.</p>
  </header>

  <div class="live" id="console-status" role="status" aria-live="polite"></div>

  <div class="field" id="restaurant-picker-field">
    <label class="label" for="restaurant-picker">Restaurant</label>
    <select class="control" id="restaurant-picker" data-testid="restaurant-picker">
      <option value="">Choose one&hellip;</option>
    </select>
  </div>

  <div id="console-body" data-testid="console-body"></div>

  <p class="alt" id="console-start-link">Not on the list yet?
    <a href="/start">Open a restaurant</a>.</p>
</section>
"""


def register(app: FastAPI) -> None:
    """Add the restaurant screens and their script to an existing API app."""

    @app.get("/start", response_class=HTMLResponse, include_in_schema=False)
    def screen_start(request: Request) -> HTMLResponse:
        return HTMLResponse(
            _shell(title="Open your restaurant", route="/start", body=start_screen())
        )

    @app.get("/console", response_class=HTMLResponse, include_in_schema=False)
    def screen_console(request: Request) -> HTMLResponse:
        return HTMLResponse(
            _shell(title="Your restaurant", route="/console", body=console_screen())
        )

    # `console.js` needs no route of its own: it sits in the same static
    # directory as the diner's script, and `/assets` is mounted over that
    # directory by `webui.register`, so it is served from the image like every
    # other asset.
