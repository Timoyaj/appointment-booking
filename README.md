# Tablekeeper — dark-factory entry

**Track:** `tablekeeper` (restaurant reservations)
**Specification:** `tablekeeper/spec/stage-1.md` … `stage-4.md` from the kickoff checkout

## How to read this repository

Each `stage-N/` folder is a **complete, buildable service on its own** and holds the
solution to that stage only — `stage-2/` is `stage-1/` carried forward and widened,
`stage-3/` is `stage-2/` carried forward, and so on. A folder is graded against every
suite up to its own number, so an earlier folder that stops working caps everything
above it.

```text
README.md         this file
FACTORY.md        the factory: seats, models, workflow, verification, submission checklist
mandates/         one generic mandate per seat — how a seat works, never what this track does
dispatch/         the self-contained task handed to the room for each stage
artifacts/        what the seats report back: runs, output, decisions, findings
room.json         the Band export of the room (pending — see below)
stage-1/          Dockerfile, RUN.md, source, tests   ← reservations API
stage-2/          Dockerfile, RUN.md, source, tests   ← browser product + combined tables
stage-3/          Dockerfile, RUN.md, source, tests   ← policies, history, recurring
stage-4/          Dockerfile, RUN.md, source, tests   ← closure replanning, series amendments
```

Nothing is shared between stage folders: each one builds and serves from its own
`RUN.md` with no manual setup. The factory files above are how the stages get built;
they are described in [FACTORY.md](FACTORY.md).

## Build and run a stage

From the repository root, for stage 1:

```bash
cd stage-1
docker build -t tablekeeper-stage-1 . && docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-stage-1
curl -s localhost:8080/health          # {"status":"ok"}
```

Each folder's `RUN.md` has the full command, a smoke test that exercises the stage's
behaviour end to end, and the configuration it accepts.

## Check a stage

The service's own suite runs against the ASGI app, so it needs no container:

```bash
cd stage-1
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

To run the track's shipped checks against a running service:

```bash
python -m pytest <checkout>/tablekeeper/test/stage_1 \
    -p harness.plugin --base-url http://127.0.0.1:8080
```

## Current status

| Stage | Folder | Own suite | Shipped checks |
| --- | --- | --- | --- |
| 1 | `stage-1/` | 310 passing | 120/120 passing |
| 2 | `stage-2/` | 442 passing, plus 25 browser checks in a DOM | stage 1: 120/120 against it; stage 2: the checks that need no browser pass, the Playwright ones cannot run here |
| 3 | `stage-3/` | 697 passing, plus the same 25 browser checks | stage 1: 120/120; stage 2: 2/2 of those needing no browser; stage 3: 7/7 |
| 4 | `stage-4/` | 887 passing, plus the same 25 browser checks | stage 1: 120/120; stage 2: 2/2 of those needing no browser; stage 3: 7/7; stage 4: 6/6 |

Stage 4 is also the only stage whose shipped checks exercise the upgrade path, and
they were run twice: once against the stage-4 service alone, and once with a live
`stage-3/` service as `--previous-base-url`, so the snapshots really were exported
by stage 3 and imported by stage 4 (2/2, 7/7 and 6/6 either way).

No browser can be installed in the workspace this was built in, so `stage-2/`'s
screens are verified two ways instead: `tests/test_screens.py` asserts what the
routes serve, and `tools/ui-check.mjs` loads those screens into a DOM and drives
the product's own script against the live service — out-of-order responses, a
lost booking response and its retry, a refusal that refreshes the grid and keeps
the form, combined tables, lookup and cancel. Layout and paint are not checked by
either and need a real browser.

A green run on the shipped checks is not evidence of a stage: only part of each suite
ships, and the rest is written in the specification. `stage-1/README.md` records the
design decisions and the places where the spec leaves room to choose.

## Validating the entry

The organizers' offline validator checks the layout, the stage folders, and that the
mandates are generic:

```bash
cd <package-root>        # holds harness/
python -m harness check --track tablekeeper <path-to-this-repository>
```

Current output: one problem, `room.json is missing`.

## Still required before submission

- **`room.json`** — the only gate this repository cannot supply itself. Configure the
  three seats in [FACTORY.md](FACTORY.md) against the mandates in `mandates/`, run the
  room, then export it: in the Band console open the room, choose ⋮ → Download →
  Download full session, and save the file unchanged as `room.json` at the repository
  root. Each seat must have both posted and been addressed by handle.
- **The room's own delivery** — all four briefs in `dispatch/` are complete and
  self-contained, and all four folders exist and are verified, but see the provenance
  note in [FACTORY.md](FACTORY.md): the stage folders as they stand were built outside
  the room, so the room's delivery replaces them.

After `room.json` lands, re-run `harness check`, then clone fresh and run
`python -m harness run --track tablekeeper --repo <clone> --all`.
