# Setting up and running the room

Everything the human does around a run. None of this is dispatched to a seat: in the submitted run
the stage brief is the only human input, and from that dispatch until the coordinator's final report
no seat may ask for clarification, approval, confirmation or another decision.

## Once, before any run

1. Prerequisites: Python 3.12+, Git, a running Docker daemon, a Band Desktop account, and your own
   model-provider access.
2. Set up the event package (from inside its directory):

   ```sh
   python3 -m venv .venv && . .venv/bin/activate
   python -m pip install -r harness/requirements.txt
   python -m playwright install chromium    # on Linux add --with-deps if libraries are missing
   python -m harness --help
   ```

3. Keep the result repository separate from the event package and the factory inputs:

   ```sh
   mkdir -p ../band-work/result ../band-work/checks
   git -C ../band-work/result init -b main
   git -C ../band-work/result config user.name "Your Name"
   git -C ../band-work/result config user.email you@example.test
   ```

   Give the seats the **absolute** path to the result checkout. A seat works in its own sandbox and
   may not resolve a relative path, and will otherwise create a repository only it can see.
4. In Band Desktop, create the three seats below, each with its own seat identity. Confirm a direct
   `@handle` message reaches each seat and that each seat can reply.
5. Configure a Git name and email for each seat. Keep credentials outside the result repository and
   its history.
6. Prepare permissions for the result checkout, Git, Docker and browser checks.

## Seats

| Handle | Model | Mandate |
| --- | --- | --- |
| `coordinator` | claude-haiku-4-5-20251001 | [mandates/coordinator.md](../mandates/coordinator.md) |
| `implementer` | claude-sonnet-4-6 | [mandates/implementer.md](../mandates/implementer.md) |
| `reviewer` | claude-sonnet-4-6 | [mandates/reviewer.md](../mandates/reviewer.md) |

Paste each mandate file in full as that seat's mandate, and select the model named in its `Model:`
line. **The handle must equal the mandate filename**: the validator reads the handles out of the
room log and the mandates out of `mandates/`, and matches them.

## Running a stage

- Practice runs are not judged. Use them to develop the factory, then do the submitted run clean.
- Resolve every question about the event or the specification **before** dispatching. During the run
  the seats resolve choices from the requirements and from each other; if they cannot proceed, the
  coordinator records the blocker and the available evidence as the stage outcome.
- Dispatch `dispatch/stage-N.md` to `@coordinator` — the whole file, unchanged — and then stay out.
  No steering, no approvals, no debugging hints, no reruns until it passes.
- Run the stages in order. A folder counts only if every earlier folder counts.

## Checking a stage

```sh
python -m harness check --track tablekeeper ../band-work/result
python -m harness run --track tablekeeper --repo ../band-work/result --stage 2 \
    --out ../band-work/checks/stage-2
```

`--stage N` builds that folder from its `Dockerfile`, runs its suite and every earlier suite against
it, and then runs the *next* stage's suite to confirm the folder does not answer later requirements
early. Keep the `--out` directories while iterating; they hold the logs and the report.

## After the last stage

1. Export the room: Band Desktop → the room's ⋮ menu → Open in Band → ⋮ → Download → Download full
   session. Save the file **unchanged** as `room.json` at the repository root.
2. Re-check and score a fresh clone:

   ```sh
   python -m harness check --track tablekeeper ../band-work/result
   git clone <your-repository> /tmp/fresh-clone
   python -m harness run --track tablekeeper --repo /tmp/fresh-clone --all --mode isolated
   ```

3. Follow each `stage-N/RUN.md` by hand in a clean container.
4. Confirm no `.git` directory exists inside any stage folder.
