/* Tablekeeper — the browser half of the product.
 *
 * The server renders each screen's controls; this script draws what only the API
 * knows: who is signed in, the availability matrix, the booking form, the
 * confirmation and a looked-up booking.
 *
 * Three rules shape the code below, because the specification asks for them by
 * name:
 *
 *   1. Responses can arrive out of order. Every request that can change the
 *      screen takes a sequence number, and a response that is not the newest is
 *      dropped — a slow search never overwrites a faster one that came after it.
 *   2. A booking is one intent, and an intent has one idempotency key. The key
 *      is bound to the exact body it was made for: submit the same form again
 *      and the same key goes with it, so a lost response can be retried and the
 *      original reference comes back. Change any field and it is a new intent
 *      with a new key.
 *   3. A lost response is not a refusal. When the network fails, the diner is
 *      told the outcome is unknown — never shown an error, and never shown a
 *      confirmation the server did not send. The server stays authoritative: this
 *      script never invents a result from anything it remembered.
 */
(() => {
  "use strict";

  const SESSION_KEY = "tablekeeper.session.v1";
  const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December"];

  // --------------------------------------------------------------------- //
  // small helpers
  // --------------------------------------------------------------------- //
  const byId = (id) => document.getElementById(id);

  function el(tag, attributes, children) {
    const node = document.createElement(tag);
    Object.entries(attributes || {}).forEach(([name, value]) => {
      if (value === null || value === undefined || value === false) return;
      if (name === "class") node.className = value;
      else if (name === "text") node.textContent = value;
      else if (name.startsWith("on") && typeof value === "function") {
        node.addEventListener(name.slice(2), value);
      } else node.setAttribute(name, value === true ? "" : String(value));
    });
    (children || []).forEach((child) => {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function replace(container, children) {
    if (!container) return;
    container.textContent = "";
    (Array.isArray(children) ? children : [children]).forEach((child) => {
      if (child) container.appendChild(child);
    });
  }

  function newKey() {
    const bytes = new Uint8Array(16);
    if (window.crypto && typeof window.crypto.getRandomValues === "function") {
      window.crypto.getRandomValues(bytes);
    } else {
      for (let index = 0; index < bytes.length; index += 1) {
        bytes[index] = Math.floor(Math.random() * 256);
      }
    }
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  function readSession() {
    try {
      const raw = window.localStorage.getItem(SESSION_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return parsed && parsed.token ? parsed : null;
    } catch (error) {
      return null;
    }
  }

  function writeSession(session) {
    try { window.localStorage.setItem(SESSION_KEY, JSON.stringify(session)); } catch (error) { /* private mode */ }
  }

  function clearSession() {
    try { window.localStorage.removeItem(SESSION_KEY); } catch (error) { /* private mode */ }
  }

  function localParts(stamp) {
    // "2026-09-24T19:00" — read by hand, so no timezone can shift the day.
    const [datePart, timePart] = String(stamp || "").split("T");
    const [year, month, day] = (datePart || "").split("-").map(Number);
    return {
      date: new Date(year, (month || 1) - 1, day || 1),
      hhmm: (timePart || "").slice(0, 5),
    };
  }

  function humanDay(stamp) {
    const parts = localParts(stamp);
    return `${WEEKDAYS[parts.date.getDay()]} ${parts.date.getDate()} ${MONTHS[parts.date.getMonth()]}`;
  }

  function humanWhen(stamp) {
    const parts = localParts(stamp);
    return `${humanDay(stamp)} at ${parts.hhmm}`;
  }

  function clockOf(stamp) {
    return localParts(stamp).hhmm;
  }

  function tableLabel(table) {
    const label = table && (table.label || table.id);
    return label === undefined || label === null ? "your table" : `Table ${label}`;
  }

  function selectionLabel(tableIds, restaurant) {
    const tables = (restaurant && restaurant.tables) || [];
    const labels = tableIds.map((id) => {
      const found = tables.find((table) => table.id === id);
      return found ? tableLabel(found) : `Table ${id}`;
    });
    if (labels.length === 1) return labels[0];
    if (labels.length === 2) return `Tables ${labels[0].replace(/^Table /, "")} & ${labels[1].replace(/^Table /, "")}`;
    return `Tables ${labels.map((label) => label.replace(/^Table /, "")).join(", ")}`;
  }

  // One wording for every refusal, so a diner is never shown a raw error code.
  const WORDING = {
    table_unavailable: "That table was taken while you were choosing. The grid has been refreshed — pick another sitting.",
    party_exceeds_capacity: "That party does not fit the seating you chose. Try a smaller party or a larger table.",
    combination_not_allowed: "The restaurant does not seat those tables together. Choose one table, or a pair it offers.",
    cutoff_passed: "This booking can no longer be changed: the restaurant's cancellation cutoff has passed.",
    reservation_cancelled: "That booking is cancelled, so it cannot be changed.",
    unauthenticated: "Your session has ended. Sign in again to continue.",
    email_taken: "That email already has an account. Try signing in instead.",
    idempotency_key_reuse: "That request was already sent with different details. Check the form and try again.",
    not_found: "Nothing matches that reference on your account.",
  };

  function wording(payload, fallback) {
    const code = payload && payload.error && payload.error.code;
    return WORDING[code] || fallback || "That did not work. Please try again.";
  }

  // --------------------------------------------------------------------- //
  // talking to the API
  // --------------------------------------------------------------------- //
  async function api(path, options) {
    const settings = options || {};
    const headers = {"Accept": "application/json"};
    if (settings.body !== undefined) headers["Content-Type"] = "application/json";
    if (settings.key) headers["Idempotency-Key"] = settings.key;
    if (state.session && state.session.token) headers.Authorization = `Bearer ${state.session.token}`;

    let response;
    try {
      response = await fetch(path, {
        method: settings.method || "GET",
        headers,
        body: settings.body === undefined ? undefined : JSON.stringify(settings.body),
      });
    } catch (networkError) {
      // No response at all: the outcome is unknown, not a refusal.
      return {ok: false, status: 0, payload: null, lost: true};
    }
    let payload = null;
    try { payload = await response.json(); } catch (parseError) { payload = null; }
    return {ok: response.ok, status: response.status, payload, lost: false};
  }

  const restaurantCache = new Map();
  async function restaurantDetail(restaurantId) {
    if (restaurantCache.has(restaurantId)) return restaurantCache.get(restaurantId);
    const result = await api(`/restaurants/${encodeURIComponent(restaurantId)}`);
    if (result.ok && result.payload) restaurantCache.set(restaurantId, result.payload);
    return result.ok ? result.payload : null;
  }

  // --------------------------------------------------------------------- //
  // state
  // --------------------------------------------------------------------- //
  const state = {
    route: window.location.pathname.replace(/\/+$/, "") || "/",
    session: readSession(),
    sequence: 0,            // newest request wins; older responses are dropped
    search: null,           // {restaurantId, date, partySize}
    restaurant: null,       // detail: name, tables, combinable
    slots: [],
    searching: false,
    searchError: null,
    selection: null,        // {tableIds, startsAtLocal, capacity, combined}
    bookingPartySize: "",   // what the diner typed in the booking form
    booking: {submitting: false, error: null, uncertain: null, confirmation: null, pending: null},
    lookup: {loading: false, error: null, reservation: null, busy: false},
    // The diner's own list: null until the first fetch answers, so an empty
    // room is never shown where a loading room is the truth.
    bookings: {loading: false, error: null, list: null, cancelling: null},
    authError: null,
    cellError: null,        // an available cell clicked while signed out
  };

  function notice(kind, testId, message) {
    return el("p", {class: `notice notice-${kind}`, role: "alert", "data-testid": testId, text: message});
  }

  // --------------------------------------------------------------------- //
  // the header: who is signed in, on every screen
  // --------------------------------------------------------------------- //
  function renderSession() {
    const slot = byId("session-slot");
    if (!slot) return;
    if (!state.session) {
      replace(slot, [
        el("a", {class: "nav-link", href: "/login", text: "Sign in"}),
        el("a", {class: "nav-link nav-link-quiet", href: "/signup", text: "Create an account"}),
      ]);
      return;
    }
    const name = state.session.displayName || state.session.email || "Signed in";
    replace(slot, [
      el("span", {class: "current-user", "data-testid": "current-user", text: name}),
      el("button", {
        class: "button button-quiet button-small",
        type: "button",
        "data-testid": "logout-button",
        text: "Sign out",
        onclick: signOut,
      }),
    ]);
  }

  function signOut() {
    clearSession();
    state.session = null;
    state.booking = {submitting: false, error: null, uncertain: null, confirmation: null, pending: null};
    state.lookup = {loading: false, error: null, reservation: null, busy: false};
    state.bookings = {loading: false, error: null, list: null, cancelling: null};
    render();
  }

  // --------------------------------------------------------------------- //
  // search and availability
  // --------------------------------------------------------------------- //
  function readSearchForm() {
    const restaurantSelect = byId("restaurant-select");
    const dateInput = byId("date-input");
    const partyInput = byId("party-size-input");
    return {
      restaurantId: restaurantSelect ? restaurantSelect.value : "",
      date: dateInput ? dateInput.value : "",
      partySize: partyInput ? String(partyInput.value || "").trim() : "",
    };
  }

  function availabilityQuery(search) {
    const params = new URLSearchParams({
      restaurant_id: search.restaurantId,
      date: search.date,
      party_size: String(search.partySize),
    });
    return `/availability?${params.toString()}`;
  }

  async function loadAvailability(search, sequence, options) {
    const keep = options || {};
    const [detailResult, availabilityResult] = await Promise.all([
      restaurantDetail(search.restaurantId),
      api(availabilityQuery(search)),
    ]);
    // A newer request owns the screen now; this one is dropped whole.
    if (sequence !== state.sequence) return;

    state.searching = false;
    if (!availabilityResult.ok) {
      if (availabilityResult.lost) {
        state.searchError = "We could not reach the restaurant. Check your connection and search again.";
      } else {
        state.searchError = wording(availabilityResult.payload,
          "That search could not be run. Check the date and party size.");
      }
      if (!keep.selection) resetBookingArea();
      render();
      return;
    }
    state.searchError = null;
    state.restaurant = detailResult;
    state.slots = (availabilityResult.payload && availabilityResult.payload.slots) || [];
    if (!keep.selection) resetBookingArea();
    render();
  }

  function resetBookingArea() {
    state.selection = null;
    state.booking = {submitting: false, error: null, uncertain: null, confirmation: null, pending: null};
    state.cellError = null;
  }

  async function runSearch(event) {
    if (event) event.preventDefault();
    const form = readSearchForm();
    const partySize = Number(form.partySize);
    if (!form.restaurantId) { state.searchError = "Choose a restaurant."; render(); return; }
    if (!form.date) { state.searchError = "Choose a date."; render(); return; }
    if (!Number.isInteger(partySize) || partySize < 1) {
      state.searchError = "Enter a party size of at least one."; render(); return;
    }

    state.search = {restaurantId: form.restaurantId, date: form.date, partySize};
    // A new search invalidates whatever the previous one offered.
    resetBookingArea();
    state.searching = true;
    state.searchError = null;
    state.sequence += 1;
    render();
    await loadAvailability(state.search, state.sequence, {});
  }

  // After a refusal the grid is refreshed, but the diner's form is left alone:
  // the spec asks for the selection and its inputs to survive so they can be changed.
  async function refreshAvailability() {
    if (!state.search) return;
    state.searching = true;
    state.sequence += 1;
    render();
    await loadAvailability(state.search, state.sequence, {selection: true});
  }

  function optionKey(tableIds) {
    return [...tableIds].sort().join("+");
  }

  function slotOptions(slot) {
    const byKey = new Map();
    ((slot && slot.available_options) || []).forEach((option) => {
      byKey.set(optionKey(option.table_ids || []), option);
    });
    return byKey;
  }

  function renderAvailability() {
    const container = byId("availability");
    if (!container) return;
    if (state.searching) {
      replace(container, el("div", {class: "panel loading"}, [
        el("span", {class: "spinner", "aria-hidden": "true"}),
        el("span", {text: "Looking for tables…"}),
      ]));
      return;
    }
    if (!state.search) { replace(container, []); return; }
    if (!state.slots.length) {
      replace(container, el("div", {class: "panel empty", "data-testid": "no-slots"}, [
        el("h2", {text: "No sittings on that day"}),
        el("p", {text: `${restaurantName()} is not seating guests on ${humanDayOfSearch()} for a party of ${state.search.partySize}. Try another evening.`}),
      ]));
      return;
    }
    replace(container, renderGrid());
  }

  function restaurantName() {
    return (state.restaurant && state.restaurant.name) || "The restaurant";
  }

  function humanDayOfSearch() {
    const [year, month, day] = String(state.search.date).split("-").map(Number);
    const parsed = new Date(year, (month || 1) - 1, day || 1);
    return `${WEEKDAYS[parsed.getDay()]} ${parsed.getDate()} ${MONTHS[parsed.getMonth()]}`;
  }

  function renderGrid() {
    const restaurant = state.restaurant || {tables: [], combinable: []};
    const tables = restaurant.tables || [];
    const pairs = restaurant.combinable || [];
    const partySize = state.search.partySize;
    const capacityOf = (id) => {
      const found = tables.find((table) => table.id === id);
      return found ? Number(found.capacity) : 0;
    };
    // A pair is offered to this party only when the two tables together seat it.
    const offeredPairs = pairs.filter((pair) => Array.isArray(pair) && pair.length === 2
      && capacityOf(pair[0]) + capacityOf(pair[1]) >= partySize);

    const headRow = el("tr", {}, [el("th", {class: "grid-corner", scope: "col", text: "Seating"})].concat(
      state.slots.map((slot) => el("th", {scope: "col", text: clockOf(slot.starts_at_local)}))
    ));

    function cell(slot, tableIds, available, combined) {
      const testId = `slot-${tableIds.join("+")}-${clockOf(slot.starts_at_local)}`;
      const selected = state.selection
        && state.selection.startsAtLocal === slot.starts_at_local
        && optionKey(state.selection.tableIds) === optionKey(tableIds);
      const classes = ["cell"];
      if (combined) classes.push("cell-pair");
      if (selected) classes.push("cell-selected");
      return el("td", {}, [el("button", {
        type: "button",
        class: classes.join(" "),
        "data-testid": testId,
        "data-available": available ? "true" : "false",
        "aria-disabled": available ? null : "true",
        "aria-pressed": selected ? "true" : null,
        onclick: () => chooseCell(slot, tableIds, available),
      }, [
        el("span", {class: "cell-state", text: available ? (combined ? "Seats you" : "Free") : "Taken"}),
        el("span", {
          class: "cell-note",
          text: combined
            ? `Table ${tableIds.map((id) => labelOf(id)).join(" + ")}`
            : `${capacityOf(tableIds[0])} seats`,
        }),
      ])]);
    }

    const labelOf = (id) => {
      const found = tables.find((table) => table.id === id);
      return found ? (found.label || found.id) : id;
    };

    function chooseCell(slot, tableIds, available) {
      if (!available) return;              // an unavailable cell does nothing
      if (!state.session) {
        state.cellError = "Sign in to book a table.";
        render();
        return;
      }
      state.cellError = null;
      const capacity = tableIds.reduce((total, id) => total + capacityOf(id), 0);
      const changed = !state.selection
        || optionKey(state.selection.tableIds) !== optionKey(tableIds)
        || state.selection.startsAtLocal !== slot.starts_at_local;
      state.selection = {
        tableIds: [...tableIds],
        startsAtLocal: slot.starts_at_local,
        capacity,
        combined: tableIds.length > 1,
      };
      if (changed) {
        // A different sitting is a different intent, so its key goes with it, and
        // the party size starts from the search again.
        state.bookingPartySize = String(state.search.partySize);
        state.booking = {submitting: false, error: null, uncertain: null,
          confirmation: null, pending: null};
      }
      render();
      const form = document.querySelector("[data-testid='booking-form']");
      if (form && typeof form.scrollIntoView === "function") {
        form.scrollIntoView({behavior: "smooth", block: "nearest"});
      }
    }

    const bodyRows = tables.map((table) => {
      const cells = state.slots.map((slot) => {
        const free = (slot.available_table_ids || []).indexOf(table.id) !== -1;
        return cell(slot, [table.id], free, false);
      });
      return el("tr", {}, [
        el("th", {class: "grid-rowhead", scope: "row"}, [
          el("span", {class: "row-name", text: tableLabel(table)}),
          el("span", {class: "row-meta", text: `Seats ${table.capacity}`}),
        ]),
      ].concat(cells));
    }).concat(offeredPairs.map((pair) => el("tr", {class: "row-combination"}, [
      el("th", {class: "grid-rowhead", scope: "row"}, [
        el("span", {class: "row-name", text: selectionLabel(pair, restaurant)}),
        el("span", {class: "row-meta", text: `Two tables · seats ${capacityOf(pair[0]) + capacityOf(pair[1])}`}),
      ]),
    ].concat(state.slots.map((slot) => {
      const options = slotOptions(slot);
      const offered = options.get(optionKey(pair));
      return cell(slot, pair, Boolean(offered), true);
    })))));

    return el("div", {class: "panel grid-panel", "data-testid": "availability-grid"}, [
      el("p", {class: "grid-caption"}, [
        el("strong", {text: restaurantName()}),
        ` · ${humanDayOfSearch()} · party of ${partySize} · sittings are ${durationNote()}`,
      ]),
      el("div", {class: "grid-scroll"}, [
        el("table", {class: "grid"}, [
          el("thead", {}, [headRow]),
          el("tbody", {}, bodyRows),
        ]),
      ]),
      el("ul", {class: "grid-legend"}, [
        el("li", {}, [el("span", {class: "swatch swatch-free"}), "Free"]),
        el("li", {}, [el("span", {class: "swatch swatch-pair"}), "Two tables dressed as one"]),
        el("li", {}, [el("span", {class: "swatch swatch-taken"}), "Taken"]),
        el("li", {}, [el("span", {class: "swatch swatch-selected"}), "Your choice"]),
      ]),
    ]);
  }

  function durationNote() {
    const minutes = state.restaurant && state.restaurant.reservation_duration_minutes;
    if (!minutes) return "each sitting keeps the table for its length";
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    const length = rest ? `${hours} h ${rest} min` : `${hours} hour${hours === 1 ? "" : "s"}`;
    return `each sitting keeps the table for ${length}`;
  }

  // --------------------------------------------------------------------- //
  // booking
  // --------------------------------------------------------------------- //
  function bookingBody() {
    const partySize = Number(String(state.bookingPartySize).trim());
    return {
      restaurant_id: state.search.restaurantId,
      table_ids: state.selection.tableIds,
      starts_at_local: state.selection.startsAtLocal,
      // A party size that is not a whole number is sent as searched, and the
      // server's own answer is what the diner is shown.
      party_size: Number.isInteger(partySize) && partySize >= 1 ? partySize : state.search.partySize,
    };
  }

  function renderBooking() {
    const container = byId("booking");
    if (!container) return;
    if (!state.selection || !state.search) {
      replace(container, state.cellError ? [notice("error", "auth-error", state.cellError)] : []);
      return;
    }
    const nodes = [];
    if (state.cellError) nodes.push(notice("error", "auth-error", state.cellError));

    const label = selectionLabel(state.selection.tableIds, state.restaurant);
    nodes.push(el("form", {class: "panel booking", "data-testid": "booking-form", novalidate: true, onsubmit: submitBooking}, [
      el("h2", {text: state.selection.combined ? "Reserve two tables as one" : "Reserve your table"}),
      el("p", {class: "booking-summary", "data-testid": "booking-summary"}, [
        el("strong", {text: label}),
        ` · ${restaurantName()} · ${humanWhen(state.selection.startsAtLocal)}`,
      ]),
      el("div", {class: "field"}, [
        el("label", {class: "label", for: "booking-party-size", text: "Party size"}),
        el("input", {
          class: "control",
          id: "booking-party-size",
          type: "number",
          min: "1",
          max: String(Math.max(state.selection.capacity, state.search.partySize)),
          step: "1",
          inputmode: "numeric",
          value: String(state.bookingPartySize),
          "data-testid": "booking-party-size",
          "aria-describedby": "booking-capacity-hint",
          // Written to state, not re-rendered: re-rendering on every keystroke
          // would take the caret away from the diner.
          oninput: (event) => { state.bookingPartySize = event.target.value; },
        }),
        el("p", {
          class: "hint",
          id: "booking-capacity-hint",
          text: state.selection.combined
            ? `This seating is two tables together and holds ${state.selection.capacity}.`
            : `This table holds ${state.selection.capacity}.`,
        }),
      ]),
      el("div", {class: "actions"}, [
        el("button", {
          class: "button button-primary",
          type: "submit",
          "data-testid": "booking-submit",
          disabled: state.booking.submitting,
          text: state.booking.submitting ? "Booking…" : "Confirm reservation",
        }),
      ]),
    ]));

    if (state.booking.uncertain) nodes.push(notice("uncertain", "booking-uncertain", state.booking.uncertain));
    if (state.booking.error) nodes.push(notice("error", "booking-error", state.booking.error));
    if (state.booking.confirmation) nodes.push(renderConfirmation(state.booking.confirmation));
    replace(container, nodes);
  }

  function renderConfirmation(reservation) {
    const tableIds = reservation.table_ids || (reservation.table_id ? [reservation.table_id] : []);
    const label = selectionLabel(tableIds, state.restaurant);
    return el("section", {class: "panel confirmation", "data-testid": "confirmation"}, [
      el("p", {class: "confirmation-eyebrow", text: "Your table is booked"}),
      el("p", {class: "reference", "data-testid": "confirmation-reference", text: reservation.reference}),
      el("p", {
        class: "confirmation-details",
        "data-testid": "confirmation-details",
        text: `${restaurantName()} · ${label} · ${humanWhen(reservation.starts_at_local)}`
          + ` · party of ${reservation.party_size}`,
      }),
      el("p", {
        class: "confirmation-tables",
        "data-testid": "confirmation-tables",
        text: `Seated at ${label}${tableIds.length > 1 ? " — the two tables are held together for your whole sitting." : ""}`,
      }),
      el("p", {
        class: "hint",
        text: "Keep this reference. You will need it to change or cancel the booking.",
      }),
      el("div", {class: "actions"}, [
        el("a", {class: "button button-quiet", href: `/lookup?reference=${encodeURIComponent(reservation.reference)}`, text: "Look up this booking"}),
      ]),
    ]);
  }

  async function submitBooking(event) {
    if (event) event.preventDefault();
    if (!state.selection || !state.search) return;
    if (!state.session) {
      state.cellError = "Sign in to book a table.";
      render();
      return;
    }

    const body = bookingBody();
    const signature = JSON.stringify(body);
    const pending = state.booking.pending;
    // One intent, one key: the same body keeps the key it was first sent with, so
    // a retry after a lost response replays the original booking instead of
    // making a second one. Any change of field is a new intent.
    state.booking.pending = pending && pending.signature === signature
      ? pending
      : {key: newKey(), signature};
    state.booking.submitting = true;
    state.booking.error = null;
    state.booking.uncertain = null;
    render();

    const result = await api("/reservations", {
      method: "POST", body, key: state.booking.pending.key,
    });
    state.booking.submitting = false;

    if (result.lost) {
      // The booking may or may not have been made. Say so, and keep the key so
      // the retry asks the same question.
      state.booking.uncertain = "We could not reach the restaurant, so we do not know whether this"
        + " booking went through. Nothing has been charged or lost: press “Confirm reservation”"
        + " again and we will ask with the same reference request, so you can never be booked twice.";
      state.booking.error = null;
      state.booking.confirmation = null;
      render();
      return;
    }
    if (result.ok && result.payload) {
      state.booking.confirmation = result.payload;
      state.booking.error = null;
      state.booking.uncertain = null;
      render();
      return;
    }
    state.booking.confirmation = null;
    state.booking.uncertain = null;
    if (result.status === 409 && result.payload && result.payload.error
      && result.payload.error.code === "table_unavailable") {
      state.booking.error = wording(result.payload);
      render();
      await refreshAvailability();   // the form and its inputs stay as they were
      return;
    }
    state.booking.error = wording(result.payload, "That booking could not be made.");
    render();
  }

  // --------------------------------------------------------------------- //
  // lookup
  // --------------------------------------------------------------------- //
  function renderLookup() {
    const container = byId("lookup-result");
    const status = byId("lookup-status");
    if (!container) return;
    const notices = [];
    if (status) replace(status, state.lookup.loading
      ? [el("div", {class: "panel loading"}, [
        el("span", {class: "spinner", "aria-hidden": "true"}),
        el("span", {text: "Looking up your booking…"})])]
      : []);
    if (state.lookup.error) notices.push(notice("error", "reservation-error", state.lookup.error));
    if (state.lookup.reservation) notices.push(renderReservation(state.lookup.reservation));
    replace(container, notices);
  }

  function renderReservation(reservation) {
    const tableIds = reservation.table_ids || (reservation.table_id ? [reservation.table_id] : []);
    const cancelled = reservation.status === "cancelled";
    const facts = el("dl", {class: "reservation-facts"}, [
      el("div", {}, [el("dt", {text: "Status"}),
        el("dd", {}, [el("span", {
          class: `status-pill ${cancelled ? "status-cancelled" : "status-confirmed"}`,
          "data-testid": "reservation-status",
          text: reservation.status,
        })])]),
      el("div", {}, [el("dt", {text: "When"}),
        el("dd", {text: humanWhen(reservation.starts_at_local)})]),
      el("div", {}, [el("dt", {text: "Party"}),
        el("dd", {text: `${reservation.party_size} guest${reservation.party_size === 1 ? "" : "s"}`})]),
      el("div", {}, [el("dt", {text: "Seating"}),
        el("dd", {"data-testid": "reservation-tables", text: reservationTableLabel(reservation)})]),
    ]);

    const actions = [];
    if (!cancelled) {
      actions.push(el("button", {
        class: "button button-danger",
        type: "button",
        "data-testid": "reservation-cancel-button",
        disabled: state.lookup.busy,
        text: state.lookup.busy ? "Cancelling…" : "Cancel this booking",
        onclick: cancelReservation,
      }));
    }
    actions.push(el("a", {class: "button button-quiet", href: "/", text: "Book another table"}));

    return el("section", {class: "panel reservation", "data-testid": "reservation-detail"}, [
      el("p", {
        class: "confirmation-eyebrow",
        text: (lookupRestaurant && lookupRestaurant.name) || "Your booking",
      }),
      el("p", {class: "reservation-reference", text: reservation.reference}),
      facts,
      el("p", {
        class: "hint",
        text: cancelled
          ? "This booking is cancelled. The tables it held are free again."
          : "You can cancel up to the restaurant's cutoff before the sitting begins.",
      }),
      el("div", {class: "actions"}, actions),
    ]);
  }

  // The lookup screen renders a reservation the search screen never loaded, so it
  // asks for the restaurant itself and falls back to the raw ids meanwhile.
  let lookupRestaurant = null;
  function reservationTableLabel(reservation) {
    const tableIds = reservation.table_ids || (reservation.table_id ? [reservation.table_id] : []);
    return selectionLabel(tableIds, lookupRestaurant);
  }

  async function submitLookup(event) {
    if (event) event.preventDefault();
    const input = byId("lookup-reference-input");
    const reference = input ? String(input.value || "").trim() : "";
    state.lookup.error = null;
    state.lookup.reservation = null;
    if (!reference) { state.lookup.error = "Enter the reference from your confirmation."; render(); return; }
    if (!state.session) { state.lookup.error = "Sign in to look up a booking."; render(); return; }

    state.lookup.loading = true;
    render();
    const result = await api(`/reservations/${encodeURIComponent(reference)}`);
    state.lookup.loading = false;
    if (result.lost) {
      state.lookup.error = "We could not reach the restaurant. Try again in a moment.";
    } else if (result.ok && result.payload) {
      state.lookup.reservation = result.payload;
      lookupRestaurant = await restaurantDetail(result.payload.restaurant_id);
      // A cancelled booking has no cancel button at all, so nothing is left over.
    } else {
      state.lookup.error = result.status === 404
        ? `No booking with reference ${reference.toUpperCase()} belongs to your account.`
        : wording(result.payload, "That booking could not be found.");
    }
    render();
  }

  async function cancelReservation() {
    const reservation = state.lookup.reservation;
    if (!reservation) return;
    state.lookup.busy = true;
    state.lookup.error = null;
    render();
    const result = await api(`/reservations/${encodeURIComponent(reservation.reference)}/cancel`, {method: "POST"});
    state.lookup.busy = false;
    if (result.lost) {
      state.lookup.error = "We could not reach the restaurant, so the booking was not cancelled. Try again.";
    } else if (result.ok && result.payload) {
      state.lookup.reservation = result.payload;
    } else {
      state.lookup.error = wording(result.payload, "That booking could not be cancelled.");
    }
    render();
  }

  // --------------------------------------------------------------------- //
  // the diner's own list: /bookings
  // --------------------------------------------------------------------- //
  // The list is everything GET /reservations returns, split by what still
  // matters: a confirmed sitting in the future is upcoming (soonest first,
  // because that is how a diner reads it), everything else — past sittings and
  // cancellations — keeps the service's own order, newest first.
  function nowLocalStamp() {
    const moment = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    return `${moment.getFullYear()}-${pad(moment.getMonth() + 1)}-${pad(moment.getDate())}`
      + `T${pad(moment.getHours())}:${pad(moment.getMinutes())}`;
  }

  function isUpcoming(reservation) {
    return reservation.status === "confirmed"
      && String(reservation.starts_at_local || "") > nowLocalStamp();
  }

  function restaurantNameOf(restaurantId) {
    const detail = restaurantCache.get(restaurantId);
    return (detail && detail.name) || "The restaurant";
  }

  async function loadBookings() {
    if (!state.session) { render(); return; }   // the signed-out prompt is drawn from state
    state.bookings.loading = true;
    state.bookings.error = null;
    render();
    const result = await api("/reservations");
    state.bookings.loading = false;
    if (result.lost) {
      state.bookings.error = "We could not reach the restaurant. Check your connection and try again.";
    } else if (result.status === 401) {
      // The session ended elsewhere: fall back to the signed-out view rather
      // than ask for a list the service will not give.
      clearSession();
      state.session = null;
    } else if (result.ok && result.payload) {
      state.bookings.list = result.payload.reservations || [];
      state.bookings.error = null;
      // Restaurant names travel separately; each one redraws the list as it lands.
      [...new Set(state.bookings.list.map((reservation) => reservation.restaurant_id))]
        .forEach((restaurantId) => { restaurantDetail(restaurantId).then(() => render()); });
    } else {
      state.bookings.error = wording(result.payload, "Your bookings could not be loaded.");
    }
    render();
  }

  function copyText(text, button) {
    const mark = () => {
      if (!button) return;
      button.textContent = "Copied";
      window.setTimeout(() => { button.textContent = "Copy"; }, 2000);
    };
    try {
      if (window.navigator.clipboard && typeof window.navigator.clipboard.writeText === "function") {
        window.navigator.clipboard.writeText(text).then(mark, () => {});
        return;
      }
    } catch (error) { /* the reference stays selectable text */ }
  }

  function bookingCard(reservation, upcoming) {
    const reference = reservation.reference;
    const cancelled = reservation.status === "cancelled";
    const tableIds = reservation.table_ids || (reservation.table_id ? [reservation.table_id] : []);
    const actions = [
      el("a", {
        class: "button button-quiet button-small",
        href: `/lookup?reference=${encodeURIComponent(reference)}`,
        "data-testid": `bookings-open-${reference}`,
        text: "Open booking",
      }),
    ];
    if (upcoming) actions.push(el("button", {
      class: "button button-danger button-small",
      type: "button",
      "data-testid": `bookings-cancel-${reference}`,
      disabled: state.bookings.cancelling === reference,
      text: state.bookings.cancelling === reference ? "Cancelling…" : "Cancel",
      onclick: () => cancelFromList(reference),
    }));
    return el("article", {class: "panel booking-card", "data-testid": `booking-card-${reference}`}, [
      el("div", {class: "booking-card-head"}, [
        el("p", {class: "booking-card-when", text: humanWhen(reservation.starts_at_local)}),
        el("span", {
          class: `status-pill ${cancelled ? "status-cancelled" : "status-confirmed"}`,
          text: reservation.status,
        }),
      ]),
      el("p", {
        class: "booking-card-meta",
        text: `${restaurantNameOf(reservation.restaurant_id)} · party of ${reservation.party_size}`
          + ` · ${selectionLabel(tableIds, restaurantCache.get(reservation.restaurant_id))}`,
      }),
      el("div", {class: "booking-card-reference-row"}, [
        el("span", {class: "booking-card-reference", "data-testid": `bookings-reference-${reference}`, text: reference}),
        el("button", {
          class: "button button-quiet button-small",
          type: "button",
          "data-testid": `bookings-copy-${reference}`,
          text: "Copy",
          onclick: (event) => copyText(reference, event.currentTarget),
        }),
      ]),
      el("div", {class: "actions"}, actions),
    ]);
  }

  function renderBookings() {
    const container = byId("bookings");
    const status = byId("bookings-status");
    if (!container) return;
    if (status) replace(status, state.bookings.loading
      ? [el("div", {class: "panel loading"}, [
        el("span", {class: "spinner", "aria-hidden": "true"}),
        el("span", {text: "Fetching your reservations…"})])]
      : []);

    if (!state.session) {
      replace(container, [el("div", {class: "panel bookings-signin", "data-testid": "bookings-signin-prompt"}, [
        el("h2", {text: "Sign in to see your reservations"}),
        el("p", {text: "Your bookings live on your account. Sign in and they are all here — no reference to type in."}),
        el("div", {class: "actions"}, [
          el("a", {class: "button button-primary", href: "/login", text: "Sign in"}),
          el("a", {class: "button button-quiet", href: "/signup", text: "Create an account"}),
        ]),
      ])]);
      return;
    }
    if (state.bookings.error) {
      replace(container, [
        notice("error", "bookings-error", state.bookings.error),
        el("div", {class: "actions"}, [el("button", {
          class: "button button-quiet",
          type: "button",
          "data-testid": "bookings-retry",
          text: "Try again",
          onclick: loadBookings,
        })]),
      ]);
      return;
    }
    if (state.bookings.list === null) { replace(container, []); return; }
    if (!state.bookings.list.length) {
      replace(container, [el("div", {class: "panel empty", "data-testid": "bookings-empty"}, [
        el("h2", {text: "No reservations yet"}),
        el("p", {text: "When you book a table it will wait for you here, ready to open, change or cancel."}),
        el("div", {class: "actions"}, [
          el("a", {class: "button button-primary", href: "/", text: "Find a table"}),
        ]),
      ])]);
      return;
    }

    const upcoming = state.bookings.list.filter(isUpcoming)
      .sort((a, b) => String(a.starts_at_local).localeCompare(String(b.starts_at_local)));
    const past = state.bookings.list.filter((reservation) => !isUpcoming(reservation));
    const groups = [];
    if (upcoming.length) groups.push(el("section", {class: "bookings-group", "data-testid": "bookings-upcoming"},
      [el("h2", {class: "bookings-group-title", text: "Upcoming"})].concat(upcoming.map((reservation) => bookingCard(reservation, true)))));
    if (past.length) groups.push(el("section", {class: "bookings-group", "data-testid": "bookings-past"},
      [el("h2", {class: "bookings-group-title", text: "Past and cancelled"})].concat(past.map((reservation) => bookingCard(reservation, false)))));
    replace(container, [el("div", {class: "bookings-list", "data-testid": "bookings-list"}, groups)]);
  }

  async function cancelFromList(reference) {
    state.bookings.cancelling = reference;
    state.bookings.error = null;
    render();
    const result = await api(`/reservations/${encodeURIComponent(reference)}/cancel`, {method: "POST"});
    state.bookings.cancelling = null;
    if (result.lost) {
      state.bookings.error = "We could not reach the restaurant, so the booking was not cancelled. Try again.";
    } else if (result.ok && result.payload) {
      state.bookings.list = (state.bookings.list || [])
        .map((reservation) => (reservation.reference === reference ? result.payload : reservation));
    } else {
      state.bookings.error = wording(result.payload, "That booking could not be cancelled.");
    }
    render();
  }

  // --------------------------------------------------------------------- //
  // signup and login
  // --------------------------------------------------------------------- //
  function renderAuthError() {
    const form = byId("signup-form") || byId("login-form");
    if (!form) return;
    const existing = form.parentNode.querySelector("[data-testid='auth-error']");
    if (existing) existing.remove();
    if (!state.authError) return;
    form.insertAdjacentElement("beforebegin", notice("error", "auth-error", state.authError));
  }

  async function submitSignup(event) {
    event.preventDefault();
    state.authError = null;
    const body = {
      display_name: String(byId("signup-display-name").value || "").trim(),
      email: String(byId("signup-email").value || "").trim(),
      password: String(byId("signup-password").value || ""),
    };
    const result = await api("/auth/signup", {method: "POST", body});
    if (result.lost) {
      state.authError = "We could not reach the restaurant. Your details were not saved — try again.";
      renderAuthError();
      return;
    }
    if (result.ok && result.payload && result.payload.token) {
      signIn(result.payload, body.email, body.display_name);
      return;
    }
    state.authError = wording(result.payload, "That account could not be created.");
    renderAuthError();
  }

  async function submitLogin(event) {
    event.preventDefault();
    state.authError = null;
    const body = {
      email: String(byId("login-email").value || "").trim(),
      password: String(byId("login-password").value || ""),
    };
    const result = await api("/auth/login", {method: "POST", body});
    if (result.lost) {
      state.authError = "We could not reach the restaurant. Try again in a moment.";
      renderAuthError();
      return;
    }
    if (result.ok && result.payload && result.payload.token) {
      signIn(result.payload, body.email);
      return;
    }
    state.authError = wording(result.payload, "That sign-in did not work.");
    renderAuthError();
  }

  function signIn(payload, email, displayName) {
    state.session = {
      token: payload.token,
      displayName: payload.display_name || displayName || email || "Signed in",
      email,
    };
    writeSession(state.session);
    state.authError = null;
    // Drawn here first: the diner is signed in on the screen they signed in from,
    // and only then is the search screen asked for. Anything waiting to see them
    // signed in does not have to win a race with a navigation.
    render();
    window.location.assign("/");
  }

  // --------------------------------------------------------------------- //
  // render
  // --------------------------------------------------------------------- //
  function render() {
    renderSession();
    if (state.route === "/") { renderAvailability(); renderBooking(); }
    if (state.route === "/lookup") renderLookup();
    if (state.route === "/bookings") renderBookings();
    if (state.route === "/signup" || state.route === "/login") renderAuthError();
  }

  function bind() {
    const searchForm = byId("search-form");
    if (searchForm) searchForm.addEventListener("submit", runSearch);
    const lookupForm = byId("lookup-form");
    if (lookupForm) lookupForm.addEventListener("submit", submitLookup);
    const signupForm = byId("signup-form");
    if (signupForm) signupForm.addEventListener("submit", submitSignup);
    const loginForm = byId("login-form");
    if (loginForm) loginForm.addEventListener("submit", submitLogin);

    // A reference in the query string is looked up straight away, so a diner who
    // follows the link from a confirmation lands on their booking.
    if (state.route === "/lookup") {
      const reference = new URLSearchParams(window.location.search).get("reference");
      const input = byId("lookup-reference-input");
      if (reference && input) {
        input.value = reference;
        if (state.session) submitLookup(null);
      }
    }

    // The list is the diner's own, so it asks for itself the moment the screen
    // loads; a signed-out visitor gets the prompt instead.
    if (state.route === "/bookings") loadBookings();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => { bind(); render(); });
  } else {
    bind();
    render();
  }
})();
