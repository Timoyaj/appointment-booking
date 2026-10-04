// Drives the restaurant's console in a DOM, against a live service.
//
// The same trick `ui-check.mjs` uses for the diner's screens, for the same
// reason: there is no browser here, so the page is loaded into jsdom and the
// product's own script is evaluated against the real API. It proves the console
// is wired to the service — that the picker finds your restaurants, that the
// dashboard draws the room and its staff, that the outbox reports honestly and
// that opening a restaurant from `/start` really opens one.
//
//   node console-check.mjs http://127.0.0.1:8080
//
// Needs `npm install jsdom` in this directory.
import {JSDOM, VirtualConsole} from "jsdom";
import assert from "node:assert/strict";

const BASE = (process.argv[2] || "http://127.0.0.1:8080").replace(/\/$/, "");
const SESSION_KEY = "tablekeeper.session.v1";

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

async function api(path, {method = "GET", body, token, key} = {}) {
  const headers = {"Accept": "application/json"};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  if (key) headers["Idempotency-Key"] = key;
  const response = await fetch(BASE + path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload = null;
  try { payload = await response.json(); } catch (error) { payload = null; }
  return {status: response.status, ok: response.ok, payload};
}

/** A fresh account, so runs do not collide with each other. */
async function newAccount(label) {
  const email = `${label}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@console.test`;
  const password = "long enough to be allowed";
  const created = await api("/auth/signup", {
    method: "POST", body: {email, password, display_name: label},
  });
  assert.equal(created.status, 201, `signup failed: ${JSON.stringify(created.payload)}`);
  return {email, password, token: created.payload.token, userId: created.payload.user_id};
}

/**
 * Load a page and its script the way a browser would, with a session already in
 * storage, then let the script's promises settle.
 */
async function loadPage(path, {token, script}) {
  const html = await (await fetch(BASE + path)).text();
  const virtualConsole = new VirtualConsole();
  const dom = new JSDOM(html, {
    url: BASE + path,
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole,
  });
  const {window} = dom;
  // jsdom implements no fetch of its own, so the page's script is given one that
  // really talks to the service — the same requests a browser would make.
  window.fetch = async (input, init = {}) => {
    const url = new URL(String(input), BASE);
    return fetch(url, init);
  };
  if (token) window.localStorage.setItem(SESSION_KEY, JSON.stringify({token}));
  // The page's own scripts are not fetched by jsdom without `resources: usable`,
  // so the product's script is fetched and evaluated here — the same file the
  // service serves.
  const source = await (await fetch(BASE + script)).text();
  window.eval(source);
  await settle(dom);
  return dom;
}

async function settle(dom, rounds = 25) {
  // Give the script's fetch chain room to finish; each round yields to the event
  // loop and lets pending promises run.
  for (let index = 0; index < rounds; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

function text(dom, selector) {
  const node = dom.window.document.querySelector(selector);
  return node ? node.textContent : null;
}

const RESTAURANT = {
  name: "Console Bakery",
  timezone: "Europe/Berlin",
  slot_minutes: 30,
  reservation_duration_minutes: 90,
  cancellation_cutoff_minutes: 120,
  opening_hours: [{weekday: "thu", opens: "18:00", closes: "23:00"}],
  tables: [
    {id: "t_1", label: "1", capacity: 2},
    {id: "t_2", label: "2", capacity: 4},
  ],
  combinable: [["t_1", "t_2"]],
};

console.log(`Tablekeeper console — browser checks against ${BASE}\n`);

// The rest of the file is a top-level await script, which is why the imports are
// at the top and the work is not wrapped in an async main.

// ---------------------------------------------------------------------------
// the console screen
// ---------------------------------------------------------------------------
const owner = await newAccount("Owner");
const opened = await api("/restaurants", {method: "POST", body: RESTAURANT, token: owner.token});
assert.equal(opened.status, 201, `could not open a restaurant: ${JSON.stringify(opened.payload)}`);
const restaurantId = opened.payload.id;
const manager = await newAccount("Manager");
await api(`/restaurants/${restaurantId}/staff`, {
  method: "POST", body: {email: manager.email, role: "manager"}, token: owner.token,
});
// A booking, so the outbox has something in it to report.
const diner = await newAccount("Diner");
await api("/reservations", {
  method: "POST", token: diner.token, key: `console-check-${Date.now()}`,
  body: {
    restaurant_id: restaurantId, table_id: "t_1",
    starts_at_local: "2027-01-07T19:00", party_size: 2,
  },
});

// A booking this week, so the console's guest list — which shows the coming week
// — has somebody in it. The restaurant serves on Thursdays, so this is the next
// Thursday in its own zone.
function nextThursday() {
  const now = new Date();
  const here = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Berlin", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(now);
  const daysAhead = (4 - new Date(`${here}T12:00:00Z`).getUTCDay() + 7) % 7;
  const target = new Date(`${here}T12:00:00Z`);
  target.setUTCDate(target.getUTCDate() + daysAhead);
  return target.toISOString().slice(0, 10);
}

const thisWeek = await api("/reservations", {
  method: "POST", token: diner.token, key: `console-week-${Date.now()}`,
  body: {
    restaurant_id: restaurantId, table_id: "t_2",
    starts_at_local: `${nextThursday()}T19:00`, party_size: 4,
  },
});
assert.equal(thisWeek.status, 201, `could not book this week: ${JSON.stringify(thisWeek.payload)}`);
const weekReference = thisWeek.payload.reference;

/** The first button in a panel whose text is exactly `label`. */
function button(dom, label) {
  return Array.from(dom.window.document.querySelectorAll("#console-body button"))
    .find((node) => node.textContent.trim() === label);
}

await scenario("the console loads your restaurants into the picker", async () => {
  const dom = await loadPage("/console", {token: owner.token, script: "/assets/console.js"});
  const picker = dom.window.document.getElementById("restaurant-picker");
  const values = Array.from(picker.options).map((option) => option.value);
  assert.ok(values.includes(restaurantId), `picker had ${JSON.stringify(values)}`);
  dom.window.close();
});

await scenario("the dashboard draws the room, its tables and its staff", async () => {
  const dom = await loadPage(`/console?restaurant_id=${restaurantId}`, {
    token: owner.token, script: "/assets/console.js",
  });
  const body = text(dom, "#console-body");
  assert.ok(body.includes("Console Bakery"), "the restaurant's name is on the page");
  assert.ok(body.includes("seats 4"), "its tables are listed with their capacity");
  assert.ok(body.includes("manager"), "its staff are listed with their roles");
  assert.ok(body.includes("Tables that join"), "the declared pair is shown");
  dom.window.close();
});

await scenario("the outbox says what is waiting rather than claiming it was sent", async () => {
  const dom = await loadPage(`/console?restaurant_id=${restaurantId}`, {
    token: owner.token, script: "/assets/console.js",
  });
  const body = text(dom, "#console-body");
  assert.ok(body.includes("Messages to diners"), "the outbox panel is drawn");
  // What the page says is waiting is what the API says is waiting: two bookings
  // were made above, so two messages are queued.
  const outbox = await api(`/restaurants/${restaurantId}/notifications`, {token: owner.token});
  const queued = outbox.payload.summary.queued;
  assert.ok(body.includes(`${queued} waiting`),
    `the page disagrees with the API about ${queued} waiting: ${body.slice(0, 400)}`);
  assert.ok(body.includes("Booking confirmed"), "the message's subject is shown");
  dom.window.close();
});

await scenario("a stranger sees no restaurant to pick", async () => {
  const stranger = await newAccount("Stranger");
  const dom = await loadPage("/console", {token: stranger.token, script: "/assets/console.js"});
  const status = text(dom, "#console-status");
  assert.ok(/do not work at a restaurant/.test(status), `status was: ${status}`);
  dom.window.close();
});

await scenario("a signed-out visitor is asked to sign in", async () => {
  const dom = await loadPage("/console", {script: "/assets/console.js"});
  const status = text(dom, "#console-status");
  assert.ok(/sign in/i.test(status), `status was: ${status}`);
  dom.window.close();
});

await scenario("the console lists who is coming and records a no-show", async () => {
  const dom = await loadPage(`/console?restaurant_id=${restaurantId}`, {
    token: owner.token, script: "/assets/console.js",
  });
  const body = text(dom, "#console-body");
  assert.ok(body.includes("Who is coming"), "the guest list is drawn");
  assert.ok(body.includes("Diner"), `the diner's name is on it: ${body.slice(0, 600)}`);
  assert.ok(body.includes("4 at 2"), "the party and its table are shown");

  const mark = button(dom, "No-show");
  assert.ok(mark, "a confirmed booking offers a no-show button");
  mark.click();
  await settle(dom);
  const status = text(dom, "#console-status");
  assert.ok(/no-show/i.test(status), `status was: ${status}`);

  const after = await api(`/restaurants/${restaurantId}/reservations`, {token: owner.token});
  const entry = after.payload.reservations.find((r) => r.reference === weekReference);
  assert.ok(entry, "the booking is still on the restaurant's list");
  assert.equal(entry.status, "no_show", "the service recorded it");
  dom.window.close();
});

await scenario("a manager publishes a deposit and stops it again", async () => {
  const dom = await loadPage(`/console?restaurant_id=${restaurantId}`, {
    token: owner.token, script: "/assets/console.js",
  });
  const document = dom.window.document;
  assert.ok(text(dom, "#console-body").includes("No deposit is taken"),
    "a restaurant taking no deposit says so");

  document.getElementById("deposit-currency").value = "eur";
  document.getElementById("deposit-per-seat-cents").value = "1500";
  document.getElementById("deposit-from-party-size").value = "4";
  document.querySelector('[data-testid="deposits-form"]').dispatchEvent(
    new dom.window.Event("submit", {bubbles: true, cancelable: true}),
  );
  await settle(dom);
  const published = await api(`/restaurants/${restaurantId}/payment-settings`, {
    token: owner.token,
  });
  assert.equal(published.payload.deposits, true, "the deposit was published");
  assert.equal(published.payload.deposit_per_seat_cents, 1500);
  assert.ok(text(dom, "#console-body").includes("per seat"),
    "the console now shows the terms");
  assert.ok(text(dom, "#console-body").includes("15.00 EUR"),
    "and the amount as money, not as cents");

  button(dom, "Stop taking deposits").click();
  await settle(dom);
  const stopped = await api(`/restaurants/${restaurantId}/payment-settings`, {
    token: owner.token,
  });
  assert.equal(stopped.payload.deposits, false, "the deposit was withdrawn");
  dom.window.close();
});

await scenario("the console shows this month's numbers", async () => {
  const dom = await loadPage(`/console?restaurant_id=${restaurantId}`, {
    token: owner.token, script: "/assets/console.js",
  });
  const body = text(dom, "#console-body");
  assert.ok(body.includes("This month"), "the report panel is drawn");
  assert.ok(/covers served/.test(body), `the covers line is missing: ${body.slice(0, 800)}`);
  assert.ok(/% of/.test(body), "utilisation is reported as a percentage");
  dom.window.close();
});

// ---------------------------------------------------------------------------
// the onboarding screen
// ---------------------------------------------------------------------------
await scenario("the start screen arrives ready to describe a restaurant", async () => {
  const dom = await loadPage("/start", {token: owner.token, script: "/assets/console.js"});
  const hours = dom.window.document.querySelectorAll("#hours-rows .hours-row");
  const tables = dom.window.document.querySelectorAll("#table-rows .table-row");
  assert.equal(hours.length, 1, "a restaurant starts with one service window");
  assert.equal(tables.length, 2, "and two tables");
  assert.equal(dom.window.document.getElementById("start-name").value, "");
  dom.window.close();
});

await scenario("adding a table offers it as a pair, and submitting opens a restaurant", async () => {
  const dom = await loadPage("/start", {token: owner.token, script: "/assets/console.js"});
  const document = dom.window.document;
  document.getElementById("start-name").value = `Opened By Script ${Date.now()}`;
  document.getElementById("add-table").click();
  const tables = document.querySelectorAll("#table-rows .table-row");
  tables[2].querySelector(".table-label").value = "3";
  tables[2].querySelector(".table-capacity").value = "6";

  document.getElementById("add-pair").click();
  const pairs = document.querySelectorAll("#pair-rows .pair-row");
  const first = pairs[0].querySelector(".pair-first");
  const second = pairs[0].querySelector(".pair-second");
  const options = Array.from(first.options).map((option) => option.value);
  assert.ok(options.includes("3"), `the new table is offered in a pair: ${options}`);

  // The rows the script adds are wired to the row template, so a pair actually
  // selects two different tables.
  first.value = "1";
  second.value = "2";

  document.getElementById("start-form").dispatchEvent(
    new dom.window.Event("submit", {bubbles: true, cancelable: true}),
  );
  await settle(dom);
  const result = text(dom, "#start-result");
  assert.ok(/is live/.test(result), `the form reported: ${result}`);
  const status = text(dom, "#start-status");
  assert.ok(!/error/i.test(status), `status was: ${status}`);
  dom.window.close();
});

await scenario("a signed-out visitor cannot open a restaurant from the screen", async () => {
  const dom = await loadPage("/start", {script: "/assets/console.js"});
  const document = dom.window.document;
  document.getElementById("start-name").value = "Should Not Exist";
  document.getElementById("start-form").dispatchEvent(
    new dom.window.Event("submit", {bubbles: true, cancelable: true}),
  );
  await settle(dom, 8);
  const status = text(dom, "#start-status");
  assert.ok(/sign in/i.test(status), `status was: ${status}`);
  const mine = await api("/restaurants/mine", {token: owner.token});
  const names = mine.payload.restaurants.map((restaurant) => restaurant.name);
  assert.ok(!names.includes("Should Not Exist"), "nothing was created");
  dom.window.close();
});

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  process.exitCode = 1;
}
