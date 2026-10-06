// Drives the browser half of stage 2 without a browser.
//
// No Chromium exists in this sandbox, so the screens are loaded into jsdom and
// the product's own script is evaluated against the live service: real HTML from
// the server, real fetches to the API, real DOM assertions on the data-testid
// attributes the specification names. It is not a rendering check -- it cannot
// see layout, paint or a 375-pixel viewport -- but every behaviour the spec asks
// for by name is exercised here: out-of-order responses, a lost response and its
// retry, a refusal that refreshes the grid and keeps the form, combined tables,
// lookup and cancel.
//
//   node ui-check.mjs http://127.0.0.1:8095
//
// Needs `npm install jsdom` in this directory.
import {JSDOM, VirtualConsole} from "jsdom";
import assert from "node:assert/strict";

const BASE = (process.argv[2] || "http://127.0.0.1:8095").replace(/\/$/, "");
const SESSION_KEY = "tablekeeper.session.v1";
const ADA = {email: "ada@example.com", password: "correct horse"};

let passed = 0;
const failures = [];

async function scenario(name, body) {
  try {
    await body();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (error) {
    failures.push({name, error});
    console.log(`  FAIL ${name}\n         ${String(error.message).split("\n").slice(0, 6).join("\n         ")}`);
  }
}

// --------------------------------------------------------------------------- //
// the world
// --------------------------------------------------------------------------- //
function fixture({combinable = [["t_1", "t_2"], ["t_2", "t_3"]]} = {}) {
  return {
    users: [{id: "u_ada", email: ADA.email, password: ADA.password, display_name: "Ada"}],
    restaurants: [{
      id: "r_anker", name: "Zum Anker", timezone: "Europe/Berlin",
      slot_minutes: 30, reservation_duration_minutes: 90, cancellation_cutoff_minutes: 120,
      opening_hours: [{weekday: "thu", opens: "18:00", closes: "23:00"}],
      tables: [
        {id: "t_1", label: "1", capacity: 2},
        {id: "t_2", label: "2", capacity: 4},
        {id: "t_3", label: "3", capacity: 6},
      ],
      combinable,
    }],
    reservations: [],
  };
}

const BOOKING_DATE = (() => {
  const day = new Date();
  day.setUTCDate(day.getUTCDate() + 7);
  // The restaurant serves Thursday and Friday; walk forward to the next Thursday.
  while (day.getUTCDay() !== 4) day.setUTCDate(day.getUTCDate() + 1);
  return day.toISOString().slice(0, 10);
})();
const AT = `${BOOKING_DATE}T19:00`;
const LATER = `${BOOKING_DATE}T21:00`;

async function reset(world = fixture()) {
  const response = await fetch(`${BASE}/_test/reset`, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(world),
  });
  assert.equal(response.status, 204, `reset failed: ${await response.text()}`);
}

async function tokenFor(email = ADA.email, password = ADA.password) {
  const response = await fetch(`${BASE}/auth/login`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({email, password}),
  });
  const payload = await response.json().catch(() => null);
  assert.equal(response.status, 200, JSON.stringify(payload));
  return payload;
}

async function bookDirectly(token, body, key = `direct-${Math.random().toString(16).slice(2)}`) {
  const response = await fetch(`${BASE}/reservations`, {
    method: "POST",
    headers: {"Content-Type": "application/json", Authorization: `Bearer ${token}`,
      "Idempotency-Key": key},
    body: JSON.stringify(body),
  });
  return {status: response.status, payload: await response.json().catch(() => null)};
}

async function reservationsOf(token) {
  const response = await fetch(`${BASE}/reservations`, {headers: {Authorization: `Bearer ${token}`}});
  return (await response.json()).reservations;
}

// --------------------------------------------------------------------------- //
// the browser
// --------------------------------------------------------------------------- //
const instrument = {
  delay: null,      // (url, method) -> milliseconds to hold a response for
  drop: null,       // (url, method) -> true to swallow the response entirely
  log: [],
};

function makeFetch(window) {
  return async (input, init = {}) => {
    const url = new URL(String(input), BASE);
    const method = (init.method || "GET").toUpperCase();
    const relative = `${method} ${url.pathname}${url.search}`;
    const delay = instrument.delay && instrument.delay(url, method);
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    const response = await fetch(url, init);
    const drop = instrument.drop && instrument.drop(url, method);
    instrument.log.push({relative, status: response.status, dropped: Boolean(drop)});
    if (drop) throw new TypeError("network response was lost");
    return response;
  };
}

// jsdom's Location is unforgeable, so `location.assign` cannot be replaced. It
// reports an unimplemented navigation through the virtual console instead, which
// is enough to know that a screen asked the browser to move on.
function consoleFor(navigated) {
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => {
    if (/Not implemented: navigation/.test(error.message)) navigated.push("(navigated)");
    else console.error(`  [jsdom] ${error.message}`);
  });
  return virtualConsole;
}

async function page(path, {session = null, query = ""} = {}) {
  const html = await (await fetch(`${BASE}${path}`)).text();
  const navigated = [];
  const dom = new JSDOM(html, {
    url: `${BASE}${path}${query}`, runScripts: "outside-only", pretendToBeVisual: true,
    virtualConsole: consoleFor(navigated),
  });
  const {window} = dom;
  window.fetch = makeFetch(window);
  if (session) window.localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  const script = await (await fetch(`${BASE}/assets/tablekeeper.js`)).text();
  window.eval(script);
  return {window, document: window.document, navigated, dom};
}

const tid = (document, name) => document.querySelector(`[data-testid='${name}']`);
const tidAll = (document, name) => Array.from(document.querySelectorAll(`[data-testid='${name}']`));

async function waitFor(document, name, {attached = true, timeout = 5000} = {}) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const node = tid(document, name);
    if (attached ? Boolean(node) : !node) return node;
    if (Date.now() > deadline) {
      throw new Error(`timed out waiting for [data-testid='${name}'] to be ${attached ? "attached" : "detached"}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

const settle = (ms = 60) => new Promise((resolve) => setTimeout(resolve, ms));

function fill(window, node, value) {
  assert.ok(node, "no such input");
  node.value = String(value);
  node.dispatchEvent(new window.Event("input", {bubbles: true}));
  node.dispatchEvent(new window.Event("change", {bubbles: true}));
}

function press(window, node) {
  assert.ok(node, "no such control to press");
  node.dispatchEvent(new window.MouseEvent("click", {bubbles: true, cancelable: true}));
}

async function search(page_, {restaurantId = "r_anker", date = BOOKING_DATE, partySize = 4} = {}) {
  // Every search names its date: the input starts on today's date, and today is
  // not necessarily a day this restaurant serves.
  const {window, document} = page_;
  fill(window, tid(document, "restaurant-select"), restaurantId);
  fill(window, tid(document, "date-input"), date);
  fill(window, tid(document, "party-size-input"), partySize);
  press(window, tid(document, "search-button"));
  return waitFor(document, "availability-grid");
}

async function signedInPage(path = "/") {
  const session = await tokenFor();
  return page(path, {session: {token: session.token, displayName: session.display_name, email: ADA.email}});
}

// --------------------------------------------------------------------------- //
// screens
// --------------------------------------------------------------------------- //
console.log(`\nTablekeeper stage 2 — browser checks against ${BASE}`);
console.log(`Booking date used: ${BOOKING_DATE} (a Thursday, seven days out)\n`);

await scenario("every required screen is reachable by URL, with its controls", async () => {
  for (const [route, anchor] of [["/", "search-button"], ["/signup", "signup-submit"],
    ["/login", "login-submit"], ["/lookup", "lookup-submit"]]) {
    const response = await fetch(`${BASE}${route}`);
    assert.equal(response.status, 200, route);
    assert.match(response.headers.get("content-type"), /text\/html/, `${route} must return HTML`);
    const {document} = await page(route);
    assert.ok(tid(document, anchor), `${route} is missing ${anchor}`);
  }
});

await scenario("the restaurant select is server-rendered with restaurant ids", async () => {
  await reset();
  const {document} = await page("/");
  const select = tid(document, "restaurant-select");
  const values = Array.from(select.querySelectorAll("option")).map((option) => option.value);
  assert.deepEqual(values, ["r_anker"]);
  assert.equal(select.querySelector("option").textContent, "Zum Anker");
  assert.equal(tid(document, "date-input").type, "date");
  assert.equal(tid(document, "party-size-input").type, "number");
});

await scenario("signing in shows the diner on the screen, signing out removes them", async () => {
  await reset();
  const anonymous = await page("/");
  assert.equal(tid(anonymous.document, "current-user"), null, "signed out: no current-user");

  const login = await page("/login");
  fill(login.window, tid(login.document, "login-email"), ADA.email);
  fill(login.window, tid(login.document, "login-password"), ADA.password);
  press(login.window, tid(login.document, "login-submit"));
  const signedInThere = await waitFor(login.document, "current-user");
  assert.match(signedInThere.textContent, /Ada/, "signed in on the login screen itself");
  await settle(200);
  assert.equal(login.navigated.length, 1, "and then taken to the search screen");
  assert.deepEqual(anonymous.navigated, [], "the anonymous page did not navigate");

  const signedIn = await signedInPage("/");
  const shown = await waitFor(signedIn.document, "current-user");
  assert.match(shown.textContent, /Ada/, `current-user must carry the display name: ${shown.textContent}`);
  press(signedIn.window, tid(signedIn.document, "logout-button"));
  await waitFor(signedIn.document, "current-user", {attached: false});
  assert.equal(signedIn.window.localStorage.getItem(SESSION_KEY), null, "the session is gone");
});

await scenario("a wrong password is refused on the screen", async () => {
  await reset();
  const {window, document} = await page("/login");
  fill(window, tid(document, "login-email"), ADA.email);
  fill(window, tid(document, "login-password"), "not the password");
  press(window, tid(document, "login-submit"));
  const error = await waitFor(document, "auth-error");
  assert.ok(error.textContent.trim().length > 0, "auth-error must carry a message");
  assert.equal(tid(document, "current-user"), null);
});

await scenario("signing up creates the account and signs the diner in", async () => {
  await reset();
  const {window, document, navigated} = await page("/signup");
  fill(window, tid(document, "signup-display-name"), "Grace");
  fill(window, tid(document, "signup-email"), "grace@example.com");
  fill(window, tid(document, "signup-password"), "sufficiently long");
  press(window, tid(document, "signup-submit"));
  // The shipped checks wait for the diner on the signup screen itself, before any
  // navigation: signing in must be visible where it happened.
  const shown = await waitFor(document, "current-user");
  assert.match(shown.textContent, /Grace/, `current-user carries the name: ${shown.textContent}`);
  await settle(200);
  assert.equal(navigated.length, 1, "a successful signup goes to the search screen");
  const stored = JSON.parse(window.localStorage.getItem(SESSION_KEY));
  assert.equal(stored.displayName, "Grace");
  assert.ok(stored.token, "the session holds a token");
});

await scenario("searching draws one cell per table per slot, with availability", async () => {
  await reset();
  const page_ = await signedInPage("/");
  await search(page_, {partySize: 4});
  const {document} = page_;
  // t_1 seats 2, so for a party of 4 it is present but not available.
  assert.equal(tid(document, `slot-t_1-19:00`).getAttribute("data-available"), "false");
  assert.equal(tid(document, `slot-t_2-19:00`).getAttribute("data-available"), "true");
  assert.equal(tid(document, `slot-t_3-19:00`).getAttribute("data-available"), "true");
  // 18:00 to 21:30 on a 30-minute grid.
  for (const hhmm of ["18:00", "18:30", "19:00", "19:30", "20:00", "20:30", "21:00", "21:30"]) {
    assert.ok(tid(document, `slot-t_2-${hhmm}`), `no cell for t_2 at ${hhmm}`);
  }
  assert.equal(tid(document, "slot-t_2-22:00"), null, "a sitting cannot start after 21:30");
});

await scenario("combination cells appear in declared order for a party they can seat", async () => {
  await reset();
  const page_ = await signedInPage("/");
  await search(page_, {partySize: 4});
  const {document} = page_;
  assert.ok(tid(document, "slot-t_1+t_2-19:00"), "the [t_1,t_2] pair has a cell");
  assert.ok(tid(document, "slot-t_2+t_3-19:00"), "the [t_2,t_3] pair has a cell");
  assert.equal(tid(document, "slot-t_2+t_1-19:00"), null, "ids follow combinable order");
  assert.equal(tid(document, "slot-t_1+t_3-19:00"), null, "combining is not transitive");
  assert.equal(tid(document, "slot-t_1+t_2-19:00").getAttribute("data-available"), "true");
});

await scenario("a pair too small for the party is not offered at all", async () => {
  await reset();
  const page_ = await signedInPage("/");
  await search(page_, {partySize: 7});
  const {document} = page_;
  assert.equal(tid(document, "slot-t_1+t_2-19:00"), null, "six seats cannot take seven");
  assert.equal(tid(document, "slot-t_2+t_3-19:00").getAttribute("data-available"), "true");
  for (const table of ["t_1", "t_2", "t_3"]) {
    assert.equal(tid(document, `slot-${table}-19:00`).getAttribute("data-available"), "false");
  }
});

await scenario("a closed day shows no-slots instead of the grid", async () => {
  await reset();
  const page_ = await signedInPage("/");
  const saturday = (() => {
    const day = new Date(`${BOOKING_DATE}T12:00:00Z`);
    day.setUTCDate(day.getUTCDate() + 2);   // Thursday -> Saturday, closed
    return day.toISOString().slice(0, 10);
  })();
  await search(page_, {date: saturday, partySize: 4}).catch(() => null);
  await waitFor(page_.document, "no-slots");
  assert.equal(tid(page_.document, "availability-grid"), null, "the grid is not shown as well");
});

await scenario("booking a single table through the grid reaches a confirmation", async () => {
  await reset();
  const page_ = await signedInPage("/");
  await search(page_, {partySize: 4});
  const {window, document} = page_;
  press(window, tid(document, "slot-t_2-19:00"));
  await waitFor(document, "booking-form");
  const summary = tid(document, "booking-summary").textContent;
  assert.match(summary, /19:00/, `the summary carries the local start time: ${summary}`);
  assert.match(summary, /Table 2/, `the summary carries the table label: ${summary}`);
  assert.equal(tid(document, "booking-party-size").value, "4", "pre-filled from the search");

  press(window, tid(document, "booking-submit"));
  await waitFor(document, "confirmation");
  const reference = tid(document, "confirmation-reference").textContent.trim();
  assert.match(reference, /^[A-Z0-9]{6,12}$/, `confirmation-reference is exactly the reference: ${reference}`);
  const details = tid(document, "confirmation-details").textContent;
  for (const expected of ["Zum Anker", "Table 2", "19:00"]) {
    assert.match(details, new RegExp(expected.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
      `${expected} missing from ${details}`);
  }
  assert.ok(tid(document, "booking-form"), "the form stays on screen after success");
  assert.equal(tid(document, "booking-error"), null);
});

await scenario("submitting the unchanged form again returns the same reference", async () => {
  await reset();
  const page_ = await signedInPage("/");
  await search(page_, {partySize: 4});
  const {window, document} = page_;
  press(window, tid(document, "slot-t_2-19:00"));
  await waitFor(document, "booking-form");
  press(window, tid(document, "booking-submit"));
  await waitFor(document, "confirmation");
  const first = tid(document, "confirmation-reference").textContent.trim();

  instrument.log.length = 0;
  press(window, tid(document, "booking-submit"));
  await settle(300);
  assert.equal(tid(document, "confirmation-reference").textContent.trim(), first);
  assert.equal(tid(document, "booking-error"), null);
  const session = JSON.parse(window.localStorage.getItem(SESSION_KEY));
  const listed = await reservationsOf(session.token);
  assert.equal(listed.length, 1, "one booking, not two");
  const posts = instrument.log.filter((entry) => entry.relative.startsWith("POST /reservations"));
  assert.equal(posts.length, 1, "the retry went out once");
  assert.equal(posts[0].status, 200, "a replay of the same key and body is a 200");
});

await scenario("changing a field makes the next submission a new booking", async () => {
  await reset();
  const page_ = await signedInPage("/");
  await search(page_, {partySize: 4});
  const {window, document} = page_;
  press(window, tid(document, "slot-t_3-19:00"));
  await waitFor(document, "booking-form");
  press(window, tid(document, "booking-submit"));
  await waitFor(document, "confirmation");
  const first = tid(document, "confirmation-reference").textContent.trim();

  press(window, tid(document, "slot-t_3-21:00"));   // a different sitting
  await waitFor(document, "booking-form");
  press(window, tid(document, "booking-submit"));
  await settle(400);
  const second = tid(document, "confirmation-reference").textContent.trim();
  assert.notEqual(second, first, "a new intent books again");
  const session = JSON.parse(window.localStorage.getItem(SESSION_KEY));
  assert.equal((await reservationsOf(session.token)).length, 2);
});

await scenario("editing a field makes the next submission a new, refused booking", async () => {
  // The slot is already held by the first booking, so a genuinely new request for
  // it must be refused -- a form that replayed the old key would not be.
  await reset();
  const page_ = await signedInPage("/");
  await search(page_, {partySize: 4});
  const {window, document} = page_;
  press(window, tid(document, "slot-t_2-19:00"));
  await waitFor(document, "booking-form");
  press(window, tid(document, "booking-submit"));
  await waitFor(document, "confirmation");

  fill(window, tid(document, "booking-party-size"), 2);
  press(window, tid(document, "booking-submit"));
  await waitFor(document, "booking-error");
  assert.ok(tid(document, "booking-error").textContent.trim().length > 0);
  assert.equal(tid(document, "booking-party-size").value, "2", "the diner's edit survived");

  const session = JSON.parse(window.localStorage.getItem(SESSION_KEY));
  const listed = await reservationsOf(session.token);
  assert.equal(listed.filter((r) => r.status === "confirmed").length, 1);
  assert.equal(listed[0].party_size, 4, "the refused second request changed nothing");
});

await scenario("a booked slot is unavailable after a fresh visit to the screen", async () => {
  await reset();
  const page_ = await signedInPage("/");
  await search(page_, {partySize: 4});
  press(page_.window, tid(page_.document, "slot-t_2-19:00"));
  await waitFor(page_.document, "booking-form");
  press(page_.window, tid(page_.document, "booking-submit"));
  await waitFor(page_.document, "confirmation");

  const again = await signedInPage("/");          // a full reload of the screen
  await search(again, {partySize: 4});
  assert.equal(tid(again.document, "slot-t_2-19:00").getAttribute("data-available"), "false");
  assert.equal(tid(again.document, "slot-t_3-19:00").getAttribute("data-available"), "true");
});

await scenario("booking a declared pair holds both tables and names both", async () => {
  await reset();
  const page_ = await signedInPage("/");
  await search(page_, {partySize: 6});
  const {window, document} = page_;
  press(window, tid(document, "slot-t_1+t_2-19:00"));
  await waitFor(document, "booking-form");
  assert.match(tid(document, "booking-summary").textContent, /Tables 1 & 2/,
    "the summary names every table in the selection");
  assert.equal(tid(document, "booking-party-size").value, "6");
  press(window, tid(document, "booking-submit"));
  await waitFor(document, "confirmation");
  const tables = tid(document, "confirmation-tables").textContent;
  assert.match(tables, /1/, `confirmation-tables names the first table: ${tables}`);
  assert.match(tables, /2/, `confirmation-tables names the second table: ${tables}`);

  const session = JSON.parse(window.localStorage.getItem(SESSION_KEY));
  const listed = await reservationsOf(session.token);
  assert.equal(listed.length, 1);
  assert.deepEqual(listed[0].table_ids, ["t_1", "t_2"]);
  assert.equal(listed[0].table_id, undefined, "a combination is not reduced to one table");

  // Both tables are now taken, and so is the pair.
  press(window, tid(document, "search-button"));
  await settle(400);
  assert.equal(tid(document, "slot-t_1-19:00").getAttribute("data-available"), "false");
  assert.equal(tid(document, "slot-t_2-19:00").getAttribute("data-available"), "false");
  assert.equal(tid(document, "slot-t_1+t_2-19:00").getAttribute("data-available"), "false");
  assert.equal(tid(document, "slot-t_3-19:00").getAttribute("data-available"), "true");
});

await scenario("a refusal shows booking-error, refreshes the grid and keeps the form", async () => {
  await reset();
  const page_ = await signedInPage("/");
  await search(page_, {partySize: 4});
  const {window, document} = page_;
  press(window, tid(document, "slot-t_2-19:00"));
  await waitFor(document, "booking-form");
  fill(window, tid(document, "booking-party-size"), 3);   // the diner's own edit

  // Another client takes the table while the form is open.
  const other = await tokenFor();
  const taken = await bookDirectly(other.token, {
    restaurant_id: "r_anker", table_id: "t_2", starts_at_local: AT, party_size: 4});
  assert.equal(taken.status, 201, JSON.stringify(taken));

  press(window, tid(document, "booking-submit"));
  const error = await waitFor(document, "booking-error");
  assert.ok(error.textContent.trim().length > 0);
  assert.equal(tid(document, "confirmation"), null, "no confirmation for a refused attempt");
  await waitFor(document, "availability-grid");
  assert.equal(tid(document, "slot-t_2-19:00").getAttribute("data-available"), "false",
    "availability was refreshed");
  assert.ok(tid(document, "booking-form"), "the form is preserved");
  assert.equal(tid(document, "booking-party-size").value, "3", "and so are its inputs");
});

await scenario("a lost response says the outcome is unknown, and the retry recovers it", async () => {
  await reset();
  const page_ = await signedInPage("/");
  await search(page_, {partySize: 4});
  const {window, document} = page_;
  press(window, tid(document, "slot-t_2-19:00"));
  await waitFor(document, "booking-form");

  // The booking reaches the service and commits; only the response is lost.
  instrument.drop = (url, method) => method === "POST" && url.pathname === "/reservations";
  press(window, tid(document, "booking-submit"));
  const uncertain = await waitFor(document, "booking-uncertain");
  assert.ok(uncertain.textContent.trim().length > 0, "booking-uncertain must not be empty");
  assert.equal(tid(document, "booking-error"), null, "a lost response is not a refusal");
  assert.equal(tid(document, "confirmation"), null, "and never a confirmation the server did not send");
  assert.ok(tid(document, "booking-form"), "the form is still there to retry from");
  instrument.drop = null;

  press(window, tid(document, "booking-submit"));
  await waitFor(document, "confirmation");
  assert.equal(tid(document, "booking-uncertain"), null, "a successful retry clears the uncertainty");
  assert.equal(tid(document, "booking-error"), null);
  const reference = tid(document, "confirmation-reference").textContent.trim();
  assert.match(reference, /^[A-Z0-9]{6,12}$/);

  const session = JSON.parse(window.localStorage.getItem(SESSION_KEY));
  const listed = await reservationsOf(session.token);
  assert.equal(listed.length, 1, "the retry replayed the original booking instead of making another");
  assert.equal(listed[0].reference, reference, "and it is the original reference");
});

await scenario("a late search response never restores the earlier search", async () => {
  await reset();
  const page_ = await signedInPage("/");
  const {window, document} = page_;
  // Search A (party of 2) is held up; search B (party of 7) answers at once.
  fill(window, tid(document, "date-input"), BOOKING_DATE);
  let held = false;
  instrument.delay = (url, method) => {
    if (method === "GET" && url.pathname === "/availability" && url.search.includes("party_size=2") && !held) {
      held = true;
      return 400;
    }
    return 0;
  };
  fill(window, tid(document, "party-size-input"), 2);
  press(window, tid(document, "search-button"));
  await settle(40);
  fill(window, tid(document, "party-size-input"), 7);
  press(window, tid(document, "search-button"));
  await settle(700);
  instrument.delay = null;

  await waitFor(document, "availability-grid");
  // Party of 7: no single table fits, and only [t_2,t_3] can seat them.
  assert.equal(tid(document, "slot-t_1+t_2-19:00"), null, "the late party-of-2 grid did not come back");
  assert.equal(tid(document, "slot-t_2-19:00").getAttribute("data-available"), "false");
  assert.equal(tid(document, "slot-t_2+t_3-19:00").getAttribute("data-available"), "true");
  assert.match(tid(document, "availability-grid").textContent, /party of 7/);
});

await scenario("clicking an available cell while signed out asks for a sign-in", async () => {
  await reset();
  const page_ = await page("/");
  const {window, document} = page_;
  fill(window, tid(document, "date-input"), BOOKING_DATE);
  fill(window, tid(document, "party-size-input"), 4);
  press(window, tid(document, "search-button"));
  await waitFor(document, "availability-grid");
  press(window, tid(document, "slot-t_2-19:00"));
  await waitFor(document, "auth-error");
  assert.equal(tid(document, "booking-form"), null, "no booking form for a signed-out diner");
});

await scenario("clicking a taken cell does nothing", async () => {
  await reset();
  const page_ = await signedInPage("/");
  const {window, document} = page_;
  fill(window, tid(document, "date-input"), BOOKING_DATE);
  fill(window, tid(document, "party-size-input"), 9);   // nothing seats nine
  press(window, tid(document, "search-button"));
  await waitFor(document, "availability-grid");
  const cell = tid(document, "slot-t_1-19:00");
  assert.equal(cell.getAttribute("data-available"), "false");
  press(window, cell);
  await settle(150);
  assert.equal(tid(document, "booking-form"), null);
  assert.equal(tid(document, "booking-error"), null);
});

await scenario("lookup shows a booking, then cancels it", async () => {
  await reset();
  const session = await tokenFor();
  const booked = await bookDirectly(session.token, {
    restaurant_id: "r_anker", table_ids: ["t_1", "t_2"], starts_at_local: AT, party_size: 6});
  assert.equal(booked.status, 201, JSON.stringify(booked));
  const reference = booked.payload.reference;

  const page_ = await page("/lookup", {
    session: {token: session.token, displayName: session.display_name, email: ADA.email}});
  const {window, document} = page_;
  fill(window, tid(document, "lookup-reference-input"), reference);
  press(window, tid(document, "lookup-submit"));
  await waitFor(document, "reservation-detail");
  assert.equal(tid(document, "reservation-status").textContent, "confirmed",
    "reservation-status is exactly the status");
  const tables = tid(document, "reservation-tables").textContent;
  assert.match(tables, /1/);
  assert.match(tables, /2/);
  assert.ok(tid(document, "reservation-cancel-button"));

  press(window, tid(document, "reservation-cancel-button"));
  await settle(400);
  assert.equal(tid(document, "reservation-status").textContent, "cancelled");
  assert.equal(tid(document, "reservation-cancel-button"), null, "absent once cancelled");
  assert.equal(tid(document, "reservation-error"), null);

  // Both tables are free again.
  const after = await bookDirectly(session.token, {
    restaurant_id: "r_anker", table_id: "t_2", starts_at_local: AT, party_size: 4});
  assert.equal(after.status, 201, JSON.stringify(after));
});

await scenario("lookup of an unknown reference is refused on the screen", async () => {
  await reset();
  const page_ = await signedInPage("/lookup");
  const {window, document} = page_;
  fill(window, tid(document, "lookup-reference-input"), "ZZZZZZZZ");
  press(window, tid(document, "lookup-submit"));
  await waitFor(document, "reservation-error");
  assert.equal(tid(document, "reservation-detail"), null);
});

await scenario("a cancelled lookup keeps the reference readable", async () => {
  await reset();
  const session = await tokenFor();
  const booked = await bookDirectly(session.token, {
    restaurant_id: "r_anker", table_id: "t_3", starts_at_local: LATER, party_size: 6});
  await fetch(`${BASE}/reservations/${booked.payload.reference}/cancel`, {
    method: "POST", headers: {Authorization: `Bearer ${session.token}`}});
  const page_ = await page("/lookup", {session: {token: session.token, displayName: "Ada"}});
  const {window, document} = page_;
  fill(window, tid(document, "lookup-reference-input"), booked.payload.reference);
  press(window, tid(document, "lookup-submit"));
  await waitFor(document, "reservation-detail");
  assert.equal(tid(document, "reservation-status").textContent, "cancelled");
  assert.equal(tid(document, "reservation-cancel-button"), null);
});

await scenario("a reference in the query string is looked up on arrival", async () => {
  await reset();
  const session = await tokenFor();
  const booked = await bookDirectly(session.token, {
    restaurant_id: "r_anker", table_id: "t_2", starts_at_local: AT, party_size: 4});
  const page_ = await page("/lookup", {
    session: {token: session.token, displayName: "Ada"},
    query: `?reference=${booked.payload.reference}`});
  await waitFor(page_.document, "reservation-detail");
  assert.equal(tid(page_.document, "reservation-status").textContent, "confirmed");
});

await scenario("a session signed in before an upgrade survives it", async () => {
  await reset();
  const page_ = await signedInPage("/");
  await search(page_, {partySize: 4});
  const {window, document} = page_;
  press(window, tid(document, "slot-t_2-19:00"));
  await waitFor(document, "booking-form");

  // The service is replaced by its own snapshot between browser requests.
  const snapshot = await (await fetch(`${BASE}/_test/export`)).json();
  delete snapshot.state.reservation_tables;
  delete snapshot.state.restaurant_combinable;
  const imported = await fetch(`${BASE}/_test/import`, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(snapshot)});
  assert.equal(imported.status, 204, await imported.text());

  assert.ok(tid(document, "current-user"), "still signed in, without a reload");
  press(window, tid(document, "booking-submit"));
  await waitFor(document, "confirmation");
  assert.match(tid(document, "confirmation-reference").textContent.trim(), /^[A-Z0-9]{6,12}$/);
});

await scenario("the bookings screen asks a signed-out visitor to sign in", async () => {
  await reset();
  const {document} = await page("/bookings");
  const prompt = await waitFor(document, "bookings-signin-prompt");
  assert.ok(prompt.querySelector("a[href='/login']"), "the prompt offers a way to sign in");
  assert.equal(tid(document, "bookings-list"), null, "no list is drawn for a stranger");
});

await scenario("the bookings screen shows an empty room before the first booking", async () => {
  await reset();
  const {document} = await signedInPage("/bookings");
  const empty = await waitFor(document, "bookings-empty");
  assert.ok(empty.querySelector("a[href='/']"), "the empty room points at the search");
});

await scenario("the bookings screen lists the diner's own bookings, soonest first", async () => {
  await reset();
  const session = await tokenFor();
  const early = await bookDirectly(session.token, {
    restaurant_id: "r_anker", table_id: "t_1", starts_at_local: `${BOOKING_DATE}T18:00`, party_size: 2});
  const late = await bookDirectly(session.token, {
    restaurant_id: "r_anker", table_id: "t_1", starts_at_local: LATER, party_size: 2});
  assert.equal(early.status, 201, JSON.stringify(early));
  assert.equal(late.status, 201, JSON.stringify(late));

  // A stranger's booking must never appear on this list.
  const stranger = await fetch(`${BASE}/auth/signup`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({email: "stranger@example.com", password: "long enough", display_name: "Stranger"})});
  const strangerToken = (await stranger.json()).token;
  const other = await bookDirectly(strangerToken, {
    restaurant_id: "r_anker", table_id: "t_3", starts_at_local: AT, party_size: 5});
  assert.equal(other.status, 201, JSON.stringify(other));

  const {document} = await page("/bookings", {
    session: {token: session.token, displayName: session.display_name, email: ADA.email}});
  await waitFor(document, "bookings-list");
  const upcoming = tid(document, "bookings-upcoming");
  assert.ok(upcoming, "the confirmed future sittings form the upcoming group");
  const cards = Array.from(upcoming.querySelectorAll("[data-testid^='booking-card-']"))
    .map((card) => card.getAttribute("data-testid"));
  assert.deepEqual(cards,
    [`booking-card-${early.payload.reference}`, `booking-card-${late.payload.reference}`],
    "soonest sitting first");
  assert.ok(tid(document, `bookings-reference-${early.payload.reference}`), "the reference is on the card");
  assert.equal(tid(document, `bookings-open-${early.payload.reference}`).getAttribute("href"),
    `/lookup?reference=${early.payload.reference}`, "a card opens its own booking");
  assert.equal(tid(document, `booking-card-${other.payload.reference}`), null,
    "another account's booking never appears");
  // The restaurant's name lands with the card, fetched from the service itself.
  // Re-query each pass: a redraw replaces the nodes an earlier query held.
  const deadline = Date.now() + 5000;
  for (;;) {
    const group = tid(document, "bookings-upcoming");
    if (group && /Zum Anker/.test(group.textContent)) break;
    assert.ok(Date.now() < deadline, "the restaurant name never arrived on the card");
    await settle(25);
  }
});

await scenario("a past sitting sits apart from the upcoming ones", async () => {
  await reset();
  const session = await tokenFor();
  const upcomingBooking = await bookDirectly(session.token, {
    restaurant_id: "r_anker", table_id: "t_2", starts_at_local: AT, party_size: 4});
  const cancelled = await bookDirectly(session.token, {
    restaurant_id: "r_anker", table_id: "t_1", starts_at_local: LATER, party_size: 2});
  assert.equal(upcomingBooking.status, 201);
  assert.equal(cancelled.status, 201);
  const response = await fetch(`${BASE}/reservations/${cancelled.payload.reference}/cancel`, {
    method: "POST", headers: {Authorization: `Bearer ${session.token}`}});
  assert.equal(response.status, 200);

  const {document} = await page("/bookings", {
    session: {token: session.token, displayName: "Ada", email: ADA.email}});
  await waitFor(document, "bookings-list");
  const past = tid(document, "bookings-past");
  assert.ok(past, "cancelled bookings form the past group");
  assert.ok(past.querySelector(`[data-testid='booking-card-${cancelled.payload.reference}']`));
  assert.equal(past.querySelector(`[data-testid='bookings-cancel-${cancelled.payload.reference}']`),
    null, "a cancelled booking offers no cancel button");
  const upcomingGroup = tid(document, "bookings-upcoming");
  assert.equal(
    upcomingGroup.querySelector(`[data-testid='booking-card-${cancelled.payload.reference}']`),
    null, "a cancelled booking leaves the upcoming group");
});

await scenario("a booking can be cancelled from the list", async () => {
  await reset();
  const session = await tokenFor();
  const booked = await bookDirectly(session.token, {
    restaurant_id: "r_anker", table_id: "t_2", starts_at_local: AT, party_size: 4});
  assert.equal(booked.status, 201);
  const reference = booked.payload.reference;

  const {window, document} = await page("/bookings", {
    session: {token: session.token, displayName: "Ada", email: ADA.email}});
  await waitFor(document, "bookings-list");
  assert.ok(tid(document, `bookings-cancel-${reference}`), "an upcoming booking offers its cancel");
  press(window, tid(document, `bookings-cancel-${reference}`));

  // The card moves from upcoming into the past group, wearing its new status.
  const deadline = Date.now() + 5000;
  for (;;) {
    const past = tid(document, "bookings-past");
    const card = past && past.querySelector(`[data-testid='booking-card-${reference}']`);
    if (card) {
      assert.match(card.textContent, /cancelled/);
      break;
    }
    assert.ok(Date.now() < deadline, "the cancelled booking never moved to the past group");
    await settle(25);
  }
  assert.equal(tid(document, `bookings-cancel-${reference}`), null, "the cancel button is gone");
  const after = await reservationsOf(session.token);
  assert.equal(after.find((r) => r.reference === reference).status, "cancelled",
    "the service really cancelled it");
  // And the table is free again for somebody else.
  const rebook = await bookDirectly(session.token, {
    restaurant_id: "r_anker", table_id: "t_2", starts_at_local: AT, party_size: 4});
  assert.equal(rebook.status, 201, JSON.stringify(rebook));
});

// --------------------------------------------------------------------------- //
console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  for (const failure of failures) console.log(`\n--- ${failure.name}\n${failure.error.stack}`);
  process.exit(1);
}
