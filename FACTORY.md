# Factory

How this repository is produced.

## Seats

| Handle | Mandate | Model | Job |
| --- | --- | --- | --- |
| `coordinator` | [mandates/coordinator.md](mandates/coordinator.md) | claude-haiku-4-5-20251001 | Owns the whole delivery: reads the task and the specification, decides the shape of each stage, splits it into self-contained pieces, dispatches them, collects the judgments, and decides when a stage is deliverable. |
| `implementer` | [mandates/implementer.md](mandates/implementer.md) | claude-sonnet-4-6 | Builds what the handoff asks for and proves it works: smallest behaviour that satisfies the requirements, one copy of each rule, whole suite run before reporting, output attached. |
| `reviewer` | [mandates/reviewer.md](mandates/reviewer.md) | claude-sonnet-4-6 | Checks the work against the specification, not against a summary of it: drives the built service, hunts unmentioned cases, verifies a clean copy follows its own run instructions, reports one problem per message. |

Three seats, one per file in [mandates/](mandates/). Each mandate is generic: it describes how a
seat works, never what this track's service does. Track-specific detail travels in the dispatch
briefs below and in the specification they paste from.

## Workflow

1. **Dispatch.** The coordinator gets one brief per stage from [dispatch/](dispatch/). A brief is
   self-contained: the stage's scope, the specification text pasted in full or named by path, what
   must be delivered, what must not be touched, and what evidence to report back.
2. **Build.** The coordinator splits the brief and hands pieces to the implementer. The implementer
   works inside one stage folder only and never carries a later stage's behaviour into an earlier
   folder.
3. **Review.** The reviewer drives the built stage from outside it, checks a clean copy against the
   stage's run instructions, and reports findings back to the seat that owns the work.
4. **Record.** Every judgment call the specification did not settle is written into the stage's
   `README.md` (what was decided and why) and reported by the seats into
   [artifacts/](artifacts/).
5. **Deliver.** A stage is delivered when it builds, serves, passes its own suite, passes the
   shipped checks for every suite up to and including its own number, and does not answer the next
   stage's requirements early.

## Artifacts

`dispatch/` holds the task handed to the room for each stage. `artifacts/` holds what the seats
report back: run output, decisions, review findings. `room.json` is the Band export of the room —
the record of the collaboration itself, and the evidence for the reciprocity gate.

## Stages

| Folder | Delivers | Status |
| --- | --- | --- |
| `stage-1/` | Reservation service: restaurants and tables, local-time availability, reservations with client retries, amendments, cancellations, bulk moves, export and import. | Reference delivered and verified. |
| `stage-2/` | The same service in a browser, plus combined tables. | Reference delivered and verified. |
| `stage-3/` | Explanations, history, policies, recurring reservations, collective moves. | Reference delivered and verified. |
| `stage-4/` | Seating changes after a table closure, amending a recurring reservation. | Reference delivered and verified. |

Each folder is a complete service: `Dockerfile`, `RUN.md`, source, and its own tests. No stage
folder contains a `.git` directory. A later folder carries the earlier one forward and adds only its
own stage's behaviour.

## Provenance

The factory files — this document, the mandates, the dispatch briefs — are written by the human
running the room. The stage folders are not: under the event's rules a stage counts only if the code
that passes its checks came out of the collaboration in the Band room, and the room's event log is
what shows that.

All four `stage-N/` folders as they stand were built in working sessions while these mandates and
briefs were being prepared. Each is a working, verified reference for what a delivered stage looks
like, and together they are what the dispatch briefs were written against — but they are not the
band's work, and the band's delivery of each stage replaces the folder that stands here. Nothing
downstream depends on them: every brief in `dispatch/` is self-contained, so the room can build any
stage from its brief alone, in any order.

## Verification

Each stage was run from its own folder, in a clean virtualenv holding only what that folder's
`requirements.txt` and `requirements-dev.txt` name, and every check below was made against a live
service of that folder on its own port.

| Folder | Own suite | Shipped checks against it | Browser checks in a DOM |
| --- | --- | --- | --- |
| `stage-1/` | 310 passed | stage 1: 120 passed | — |
| `stage-2/` | 442 passed | stage 1: 120; stage 2: 2 of those needing no browser | 25 passed |
| `stage-3/` | 697 passed | stage 1: 120; stage 2: 2; stage 3: 7 | 25 passed |
| `stage-4/` | 887 passed | stage 1: 120; stage 2: 2; stage 3: 7; stage 4: 6 | 25 passed |

`stage-4/` was additionally run with a live `stage-3/` service as `--previous-base-url`, so the
shipped upgrade checks exported from stage 3 and imported into stage 4 for real: 2, 7 and 6 passed
again. Stage 4 also carries the earlier stages' suites unchanged — 697 of its 887 tests are stage
3's tests, run against the stage-4 service.

```bash
cd stage-1
python -m venv .venv && .venv/bin/pip install -r requirements.txt
PORT=8080 TABLEKEEPER_DB=/tmp/tablekeeper/stage-1.db \
  .venv/bin/python -m tablekeeper.serve &
curl -s http://127.0.0.1:8080/health
```

The shipped checks are run from the organizer package, not from this repository:

```bash
cd <package-root>            # holds harness/ and tablekeeper/test/
python -m pytest tablekeeper/test/stage_1 -p harness.plugin --base-url http://127.0.0.1:8080
# and, for the upgrade path a later stage has to support:
python -m pytest tablekeeper/test/stage_4 -p harness.plugin \
    --base-url http://127.0.0.1:8080 --previous-base-url http://127.0.0.1:8003
```

Three things cannot be checked in the workspace these references were built in, and are recorded as
open rather than as passed: a `docker build` (no container runtime is available), the 17 shipped
stage-2 checks that drive a real browser (no browser can be installed — which is what each folder's
`tools/ui-check.mjs` substitutes for, in a DOM against the live service), and layout or paint at
375 px. Each `stage-N/RUN.md` was walked by hand against a running service instead, command for
command.

## Before submitting

- [ ] Configure the three seats above against these mandates in a Band Desktop room.
- [ ] Run the room on `dispatch/stage-1.md`, then `dispatch/stage-2.md` and the later briefs, so the
      stage folders are the band's delivery rather than the reference implementation.
- [ ] Export the Band room to `room.json`, with each seat having both posted and been addressed by
      handle.
- [ ] Re-run `python -m harness check --track tablekeeper .` until the gates are clean.
- [ ] Clone the repository fresh and run `python -m harness run --track tablekeeper --repo <clone> --all`.
- [ ] Follow each `stage-N/RUN.md` by hand in a clean container.
- [ ] Confirm no `.git` directory exists inside any stage folder.
