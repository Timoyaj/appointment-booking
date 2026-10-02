# Dispatch — stage 4

@coordinator — this is the whole task. Nothing outside this brief is context for you or for the
seats you dispatch to.

## Scope

Deliver `stage-4/`: a complete, buildable service that answers every Stage 4 requirement in the
specification pasted at the end of this brief, and that still answers every Stage 1, 2 and 3
requirement.

`stage-4/` is `stage-3/` carried forward plus Stage 4. Two things are added: when a table is closed,
a manager is shown a proposed seating arrangement and reviews it before it is applied; and a
recurring reservation can be amended.

## What to deliver

- `stage-4/Dockerfile` — builds the service from a clean context.
- `stage-4/RUN.md` — the exact commands a reviewer follows to build it, run it, and see it work.
- `stage-4/requirements.txt` — runtime dependencies, pinned.
- the source, the browser UI, and the stage's own tests.
- `stage-4/README.md` — the design, plus every judgment call the specification did not settle:
  the question, the reading chosen, and why.

One process, one port, API and UI together. The browser reaches the API through the origin it was
loaded from.

## What not to do

- Do not touch `stage-1/`, `stage-2/` or `stage-3/`. They are delivered and frozen.
- This is the last stage: there is no later suite to keep clear of, so build every remaining
  requirement the specification states. Nothing beyond it.
- Do not write code to the shipped checks. They are not in this repository and must not be added to
  it. Build to the specification.
- Do not put a `.git` directory inside the stage folder.
- Do not add a feature the specification does not ask for. No new screens are required by this
  stage; the existing availability, confirmation and lookup screens must reflect an applied plan.

## Evidence to report back

1. The stage's own suite, run in full, with the count.
2. The shipped Stage 1, 2, 3 and 4 checks, run against the live service, with all four counts.
3. A manual pass over a closure: propose one, read the plan, apply it, and show that every
   considered booking kept its reference, owner, party size, times and accepted terms, and that the
   availability, confirmation and lookup screens reflect the new seating.
4. A recurring reservation amended, and the same one where the amendment cannot be met.
5. The list of judgment calls added to `stage-4/README.md`.

## Where the specification leaves room

When the specification does not settle a question, choose the reading a careful reader could defend,
write it into `stage-4/README.md`, and keep going. Do not stop the room to ask.

## Requirements — Stage 4 specification, verbatim

# Tablekeeper — Stage 4: seating changes and recurring amendments

Extends all earlier stages, including stage-1 atomic reservation moves, stage-2 table
combinations and stage-3 policies and recurring agreements. All earlier requirements apply.

## Seating changes after a table closure

When a table becomes unavailable, a manager can review a proposed seating arrangement
before applying it. Customers must keep their booking times, party sizes and accepted terms.
No new screens are required. Existing availability, confirmation and lookup screens must
reflect an applied plan.

`POST /restaurants/{id}/replans` requires a manager and an idempotency key. Body:

```json
{"table_id": "t_2", "from": "2026-09-28T18:00:00+02:00",
 "to": "2026-09-28T23:00:00+02:00"}
```

The instants have explicit offsets and `from < to`; invalid interval is 422
`validation_failed`, unknown table 404. The proposed closure is the half-open interval
`[from,to)`. Consider every confirmed booking at this restaurant overlapping that interval.
Other bookings retain their assignments.
Planning must support up to 6 tables, 4 declared pairs and 6 considered bookings; larger
inputs may return 422 `planning_limit`. Each considered booking must retain its reference,
owner, party size, start, end and accepted terms. Assign it a single or a declared pair with
enough capacity under **its own accepted terms**, without conflicts with fixed bookings,
other assignments, previously applied closures or the proposed closure. Diners' cancellation
cutoffs do not prevent an operator repair. No booking may disappear or be cancelled.

Among feasible plans minimize, in order:

1. Number of bookings whose table set changes.
2. Total unused seats across all considered bookings (capacity minus party size).
3. The vector of option ranks in ascending reservation-reference order. Singles are ranked
   first in fixture order, then pairs in declared order, starting at 0.

Returns 201:

```json
{"plan_id": "opaque", "restaurant_revision": 4,
 "closure": {"table_id": "t_2", "from": "...", "to": "..."},
 "assignments": [{"reference": "ABC12345", "table_ids": ["t_1"], "changed": true}],
 "moved_count": 1, "unused_seats": 0}
```

Assignments include every considered booking in reference order. A restaurant revision starts
at 0 after reset and increments once for each successful new booking, real amendment,
cancellation, policy publication or plan application. No-op writes, failures, previews and
replays do not increment it. Preview stores only a plan: no closure, occupancy, reservation
revision or history changes. No feasible plan gives 409 `no_feasible_plan`, changing nothing.

`POST /restaurants/{id}/replans/{plan_id}/apply`, body `{}`, requires a manager and an
idempotency key. Return 201 with `{"plan_id": "...", "restaurant_revision": 5,
"reservations": [...]}`; reservations include every considered booking in reference order.
Unknown plan or one from another restaurant is 404. Any intervening restaurant revision
invalidates the plan: 409 `stale_plan`, changing nothing. A plan already applied under a
different key gives 409 `plan_already_applied`; replay of the successful key returns the
original response with 200, even after later changes. Application is atomic.

Application records the closure and all assignments together. Each moved booking increments
its revision once and gains one `reassigned` history entry with a `table_ids` change and
`plan_id`; accepted terms and times remain identical. Unmoved bookings gain nothing. The
restaurant revision increments once for the **whole plan**. Closures thereafter exclude
singles and pairs from availability and reject creates/amendments with 409 `table_unavailable`.
In explanations, `no_overlap` is false for a closure as for a conflicting booking.

Concurrent applications must not leave partially moved bookings. A closure at another
restaurant does not invalidate this plan.

## Amend recurring reservations

`POST /series/{series_id}/amend` is an owner-only idempotent write. Unknown or another owner's
series is 404; no token is 401. Body:

```json
{"expected_revision": 3, "from_index": 2, "local_time": "20:00"}
```

Revision must be a positive integer; from_index an integer in 0..count-1; local_time exactly
HH:MM in 00:00..23:59. Booleans are invalid integers. Invalid input gives 422
`validation_failed`; a mismatched series revision gives 409 `stale_revision` before any
occurrence's cutoff or booking validation. Unknown fields are ignored.

Consider indices at or after from_index, excluding cancelled occurrences and those marked
exception. Change their clock time on their original scheduled local dates, retaining each
reference, owner, party size and current table selection. A change with identical
resulting fields is a no-op and retains its terms. Each real change checks its old accepted
cutoff, then adopts the policy for its resulting start date, just like an individual PATCH.

The resulting occurrences must not conflict with unchanged occurrences, other bookings
or applied closures. On failure, histories, idempotency records and all revisions remain
unchanged. Non-occupancy errors take precedence in occurrence-index order; otherwise an
occupancy conflict returns `table_unavailable`.

On success return 201 with the current series response. Each changed occurrence gains one
ordinary changed history entry and one reservation revision. The series and restaurant
revisions each increase once for the entire operation if anything changed. Series amendments
do not mark exceptions. All-no-op or empty eligible sets succeed without changing revisions.
Replay returns the original response with 200 even after further edits or cancellations.

Seating repairs may move series occurrences. They preserve their exception flags, scheduled
dates, identities and accepted terms. Each affected series revision increases once per plan
application if at least one member moved.
Concurrent amendments from the same expected revision may not both make a real change.

A stage-4 service must accept exports produced by the same team's stages 1–3. These
operations must support imported series, including moved and cancelled occurrences.
Earlier booking and series receipts, histories and retries remain valid.
