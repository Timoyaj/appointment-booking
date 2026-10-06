# What to add to Tablekeeper so you can sell it

An assessment of the four stage folders against what a restaurant would actually pay
money for. Code references are to `stage-4/`, since it is the widest build.

> **Status: tier 1 is built.** See [`product/`](product/README.md) for the layer
> that now sits on top of the engine — the same service, carried forward the way
> each stage was carried forward, with **1041 tests** (887 of them unchanged).
> What is closed, in the order this document ranked it:
>
> - **§2.1 — the data-wipe endpoint.** `/_test/*` is now off in the shipped image,
>   not merely discouraged. *(done)*
> - **§2.3 + §2.5 — token expiry, revocation, log-out, login throttling.** *(done)*
> - **§3.1 — restaurant onboarding and the manager console, roles included.** *(done:
>   `POST /restaurants`, owner/manager/host, `/start`, `/console`, audit trail, and
>   now the guest list, deposits, this month's numbers and no-show/complete on the
>   console itself)*
> - **§3.2 — the notification outbox.** *(done: confirmations, changes, cancellations,
>   reseating, address confirmations and reset links are written inside the
>   transaction they belong to, with retries, a visible failure state, and an SMTP
>   transport a deployment turns on)*
> - **§2.2 — persistence.** *(partly: the image now keeps its database in a volume
>   instead of `/tmp`, so a restart is not an erasure; Postgres is still ahead)*
> - **§3.3 — deposits.** *(done: a manager publishes a per-seat deposit from a
>   threshold party size; the hold is taken inside the booking transaction, a
>   decline leaves no booking, no-show captures and complete/cancel release, and an
>   append-only ledger records all of it. Stripe behind an env var, and an
>   in-process provider without one)*
> - **§3.5 — reporting.** *(done: covers, utilisation against published hours,
>   no-show and cancellation rates, lead time, by hour/day/party size, table use,
>   money kept — plus a CSV a restaurant can open in a spreadsheet)*
> - **§2.4 — password reset and email verification.** *(done: reset always answers
>   202 and signs every device out, links are single-use and stored hashed, and
>   signup confirms the address; enforcement of confirmed addresses is one env var)*
> - On top of the ranked work: **a diner can now see their own bookings** at
>   `/bookings` — upcoming sittings soonest first, no reference needed, cancel
>   from the list — and the date picker no longer offers past days. *(done)*
>
> Still open, in the order they matter: reminders (§3.4), amend/cancel by the link
> in the email, the waitlist, the mutating manager flows (policies, plans) as
> screens rather than API calls, and hosting/backups/observability (§2.7) plus the
> licence and data-retention policy (§2.6).

---

## The verdict in one paragraph

You have a **booking engine**, not a **product**. The engine is the genuinely hard
part and it is genuinely good: correct double-booking prevention under concurrency,
idempotent writes, DST-correct local time, immutable booking history, versioned
policies, closure replanning, combined tables, 887 tests. Most competitors get
several of those wrong. But an engine is not sellable on its own, because a
restaurant cannot sign up, cannot be notified, cannot take a deposit, cannot see its
own data, and loses everything on restart. You are roughly **30% of the way to a
sellable product and 100% of the way to the hardest 30%**.

There is also one thing to fix *before* you show this to anybody else: the
`/_test/*` endpoints are unauthenticated and enabled in the shipped image.
`curl -X POST yourhost/_test/reset` wipes every reservation in the database.
That is not a feature gap, it is a liability.

---

## 1. What you already have (the defensible part)

Worth naming explicitly, because this is what you are actually selling and it is
worth more than the screens around it:

| Capability | Where | Why a buyer cares |
| --- | --- | --- |
| No double-booking under concurrency — 50 racing requests, one wins, 49 clean 409s, no 5xx | `db.py` (`BEGIN IMMEDIATE`), `test_concurrency.py` | Restaurants fire staff over double-bookings |
| Idempotent booking and multi-booking moves, receipts survive export/import | `idempotency.py` | Flaky mobile networks are the #1 source of phantom bookings |
| DST-correct wall-clock time (spring-forward gap, fall-back fold) | `tztime.py`, `test_dst.py` | Almost every hobby booking script gets this wrong twice a year |
| Immutable, versioned booking history and published policies | `history.py`, `policies.py` | Dispute resolution: "I booked for 8, you seated us at 7" |
| Atomic batch moves — a two-booking swap in one request | `service.move_reservations` | The host stand does this 20 times a night, by phone, today |
| Closure replanning that keeps times, party sizes and terms | `planning.py`, `service.apply_replan` | "The dishwasher flooded, move 14 bookings" is a real Tuesday |
| Combined tables, non-transitive declarations | `domain.check_combination` | Party of 7 at a 2+6 restaurant |
| Zero runtime network, one container, no external DB | `Dockerfile`, `RUN.md` | Sells to venues with bad/no wifi-dependent POS |

Lead with that list. It is a better engineering story than most funded reservation
startups can tell.

---

## 2. Do not put this in front of a customer until these are closed

These are not features. They are reasons a pilot would end badly.

| # | Problem | Evidence | Fix |
| --- | --- | --- | --- |
| 1 | **Anyone can delete all data.** `/_test/reset` and `/_test/import` are unauthenticated and "enabled in the delivered image" | `testhooks.py` docstring, `api.py:107` | Compile them out of the production image, or gate on a `TABLEKEEPER_TEST_HOOKS=1` env var that defaults off |
| 2 | **Everything is lost on restart.** State lives in `/tmp` and the README says it "need not survive a restart" | `api.py:45`, `stage-4/README.md` | Persistent volume now; Postgres before you sell a second tenant. SQLite is fine for the first 20 restaurants *if* it persists and you back it up |
| 3 | **Tokens never expire and cannot be revoked.** Stolen token = permanent access | `auth.py` docstring | Expiry + refresh, a token table with revocation, logout on all devices |
| 4 | **No password reset, no email verification.** A locked-out owner has no recovery path | no `reset` route anywhere | Reset by email; verification before a restaurant goes live |
| 5 | **No rate limiting** on signup, login or booking creation | — | Per-IP and per-account limits; login lockout. Someone will enumerate your diner emails otherwise |
| 6 | **No audit trail for staff actions.** Policies, replans and closures are manager-only but nothing records *which* manager did what | `service.publish_policy` checks `is_manager`, writes no actor | Actor + timestamp on every manager write. You will need this the first time a restaurant disputes a plan |
| 7 | **No observability.** No metrics, no structured logs, no error reporting, no backup/restore runbook | `serve.py` is 26 lines | Logs to stdout in JSON, a `/metrics` endpoint, and a nightly restore test — not just a backup |
| 8 | **No license, no terms, no privacy policy, no DPO story.** There is no `LICENSE` file in the repo, and you will be storing diner names, emails and dining habits | repo root | Pick a license for the code, write the DPA/privacy terms for the data. In the EU this is not optional, and diner data is exactly the kind regulators ask about |

---

## 3. The five additions that turn this into something sellable

Ranked by revenue impact, not by difficulty.

### 3.1 Restaurant onboarding and a manager console — **the single biggest gap**

Right now a restaurant can only exist by having a fixture POSTed to `/_test/reset`
(`testhooks.py:117` is the only caller of `repo.insert_restaurant`). There is no
`POST /restaurants` and no manager-facing screen at all: the entire web UI is four
diner pages — search, signup, login, lookup (`webui.py:223-254`).

Managers can publish policies and apply replans, but only by hand-crafting JSON
against the API.

You need:

- `POST /restaurants` — self-serve creation, with the creator as owner.
- Manager console screens: hours and slot grid, tables and capacities, which tables
  combine, closures, published policies, the seating-plan preview and apply.
- Staff and roles: owner / manager / host, invitations, per-location scoping.
- A "go live" checklist: hours set, tables set, a test booking passed.

Without this you are hand-onboarding every customer via curl. That does not scale
past three restaurants, and it makes support impossible.

### 3.2 Notifications — email and SMS

There is not one occurrence of `smtp`, `notif`, `twilio` or `send` anywhere in the
codebase, and no outbound network at run time by design.

A diner books and receives nothing. No confirmation, no reminder, no "your table
moved because the kitchen flooded". Restaurants judge a booking system almost
entirely on whether confirmation and reminder emails actually arrive — it is the
most visible thing you do.

Minimum viable set:

- Confirmation with the reference, plus an `.ics` attachment
- Reminder ~24h before (this alone measurably cuts no-shows)
- Change and cancellation notices, including the replan case you already compute
- Manager digest: today's covers, large parties, cancellations
- An **outbox table + retry worker**, so a mail outage never fails a booking

Adding outbound network breaks the current "no network at runtime" property. Keep
it as a separate process with its own allowlist so the booking path stays
deterministic and testable.

### 3.3 Money: deposits, prepayment, no-show fees

Nothing in the codebase touches payment. For most restaurants, "can I take a card
to hold the table" *is* the purchase decision — a no-show on a Friday table for six
is a real cash loss, and the whole point of the purchase is recovering it.

- Card-on-file hold at booking, capture on late cancellation / no-show
- Prepaid fixed menus and ticketed events
- Automatic release of the hold after the visit
- Stripe Connect so the money lands in the restaurant's account and you take a cut

This is also the cleanest way to charge: a percentage of recovered no-shows is an
easier conversation than a flat SaaS fee.

### 3.4 Real persistence, multi-tenancy and billing

- Postgres (or SQLite + a volume) with migrations you can roll forward *and* back.
  Your `migrations/0001..0005` are a good start; they need a version table and a
  tested upgrade path for a live tenant.
- Tenant isolation enforced in the data layer, not only by `restaurant_id` in
  queries. One missing `WHERE restaurant_id = ?` is a cross-tenant leak, and you
  have 764 lines of SQL in `repo.py` to audit.
- Stripe subscriptions, plan limits (locations, covers/month, staff seats),
  trial, dunning, and an internal admin view of who is paying.
- Backups per tenant and a documented restore, or you cannot honestly sign an SLA.

### 3.5 Reporting the restaurant can look at

`GET /reservations` exists but is per-user. There is no aggregate view anywhere: no
covers, no occupancy, no no-show rate, no lead time.

Restaurants renew subscriptions because of what they can *see*:

- Covers and revenue by day, service and hour
- Table utilisation — which tables earn their floor space
- No-show and late-cancellation rate, before and after deposits
- Lead time and party-size mix (do I need more 4-tops?)
- CSV export, and a weekly email digest

You already store everything needed for this. It is mostly aggregation and one
screen, and it is the highest ratio of perceived value to engineering effort on this
list.

---

## 4. Second tier — what wins deals and raises your price

| Addition | Why | Effort signal |
| --- | --- | --- |
| **Waitlist + auto-fill on cancellation** | Instantly recovers the table a cancellation just freed. Easy to demo, easy to price. Your availability + cancellation logic already supports it | Medium |
| **Diner "my bookings" screen** | `GET /reservations` and amend/cancel all exist — there is simply no screen. Cheapest real feature on this list | Small |
| **Self-service from the confirmation link** | A diner should cancel from their email without an account. Biggest single driver of freed-up tables | Small–Medium |
| **Public booking page per restaurant + embeddable widget** | The widget on the restaurant's own site is the actual product surface. Also a Google "Reserve with Google" feed | Medium |
| **Floor plan / host stand view** | Drag-and-drop seating for tonight. What hosts compare you against | Large |
| **Walk-ins and same-day seating** | Most covers in casual dining are walk-ins. Your model only has advance bookings | Medium |
| **Calendar feeds (`.ics`) and POS integrations** | Reduces "I have to enter it twice" objections. Integration partners are also a sales channel | Medium |
| **Multi-location groups** | Chains pay 10x a single site. Needs location hierarchy above `restaurant` | Medium |
| **Reviews / post-visit feedback** | Retention lever for the restaurant; marketing asset for you | Small |
| **White-label / agency mode** | If you sell through web agencies, one deal brings 30 restaurants | Medium |

---

## 5. The go-to-market wrapper

Selling software is not only the software. Before the first invoice you also need:

- **A pricing model and a page for it.** Flat monthly per location is the easiest to
  sell; per-cover aligns cost with value and suits groups; a self-hosted licence with
  an annual fee is the highest-margin option if you target venues that will not
  put guest data in someone else's cloud.
- **A hosted option.** Nobody's restaurant manager is running `docker build`. You
  must host it, or you have no product for the mass market.
- **Migration from the incumbent.** You already have export/import and a
  cross-stage upgrade path. "Import your OpenTable/SevenRooms export in an hour" is
  a real, demoable selling point — build a converter and say it out loud.
- **An SLA you can meet**, a status page, and a support channel with an answer time.
- **Docs for two audiences**: restaurant staff, and developers if you sell the API.
- **Case-study-ready proof**: one pilot restaurant, real numbers ("no-shows down
  40%, 11 covers recovered in month one").
- **Resolve the `FACTORY.md` provenance question first.** That file states the stage
  folders were built outside the room and that "the band's delivery of each stage
  replaces the folder that stands here". If this repository is an event submission,
  settle what that means for ownership before you build a company on it. Also note
  the three open items in `README.md`: `room.json` is missing and the room's own
  delivery has not happened.

---

## 6. Do **not** build these

- **A consumer marketplace to compete with OpenTable on diner discovery.** You
  cannot outspend them on demand generation, and diners do not switch booking sites.
  Sell to restaurants; be the system of record.
- **Native iOS/Android apps at this stage.** Your screens are responsive and
  server-rendered with a 891-line script — that is enough. Ship a PWA later if it is
  ever the objection.
- **Your own payment processing.** Stripe Connect exists precisely so you never have
  to touch card data or PCI scope.
- **More spec stages.** The engine is already past the point of diminishing returns.
  Every additional unit of booking correctness from here is worth far less than one
  email that actually arrives.

---

## 7. What I would do in the first month, in order

| Order | Work | Touches | Rough effort |
| --- | --- | --- | --- |
| 1 | Gate `/_test/*` behind a default-off env var; make the DB path configurable and persistent | `api.py`, `serve.py`, `testhooks.py`, `Dockerfile` | 1–2 days |
| 2 | `POST /restaurants` + onboarding wizard; owner role; "go live" checklist | `service.py`, `api.py`, new `onboarding.py`, `webui.py` | 1–2 weeks |
| 3 | Manager console: hours, tables, combinations, closures, policies, replan preview/apply | `webui.py`, `static/` | 1–2 weeks (parallel with 2) |
| 4 | Outbox + email: confirmation, `.ics`, 24h reminder, change/cancel notices | new `notifications.py`, migration, worker | 3–5 days |
| 5 | Diner "my bookings" screen + cancel/amend from the confirmation link | `webui.py`, `tablekeeper.js` | 2–3 days |
| 6 | Reporting: covers, utilisation, no-show rate, CSV export | new `reports.py`, one screen | 1 week |
| 7 | Waitlist with auto-fill on cancellation | `service.py`, migration, one screen | 1 week |
| 8 | Deposits / card-on-file via Stripe Connect | new `payments.py`, migrations | 1–2 weeks |
| 9 | Hosted deployment, backups, metrics, an SLA you can actually meet | infra | 1 week |
| 10 | Billing plans and signup flow for you | new `billing.py` | 1 week |

Items 1–4 get you to "a restaurant could use this for real". Items 1–6 get you to
"a restaurant would pay for this". Items 7–9 get you to "a restaurant would pay
*more* for this than for the incumbent".

---

## 8. If you want the shortest credible pitch

> "Most booking systems get the hard parts wrong — double-bookings, timezone bugs,
> and no way to move a room when something breaks. Tablekeeper gets those right; it
> has the concurrency, idempotency and DST behaviour that funded competitors had to
> learn in production. What it does not have yet is a way for a restaurant to sign
> itself up, a confirmation email, a deposit, or a report. Close those four and you
> have a product that a real restaurant can run its Friday night on."

That is the honest position — and a good one, because the hard, unglamorous part is
the part you have already done.
