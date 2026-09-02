# Session Lifecycle: Auto-Expiry and Un-Finish — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop the daemon's watcher-leak problem (33 of 49 live sessions never explicitly
finished, most 1-3 days old) with a daemon-level safety net that auto-finishes idle sessions,
and a matching "un-finish" capability so that safety net's mistakes are recoverable.

**Architecture:** A new `expire_idle(cfg, registry)` function in `cleanup.py`, invoked by a
periodic background sweep the `Daemon` starts alongside its HTTP server, marks idle live
sessions `finished` using the exact same `_mark()` write path the CLI's `end` command already
uses. A new `unfinish` route/CLI/client method removes the `finished`/`cancelled` marker file,
making a session live again — since those markers were never anything but a boolean file's
presence, nothing else needs to change.

**Tech Stack:** Python 3.9+, this repo's existing stdlib-only `http.server` stack, no new
dependency.

**Spec:** `docs/2026-09-02-session-lifecycle-design.md` — read it in full before starting either
task. It records two open questions Task 1/2 must settle with real evidence, not guesses: the
idle threshold's exact value, and whether `unfinish` should touch the watcher heartbeat file.

## Global Constraints

- Never touch `cleanup.expire()` or `Config.retention_days` — a completely separate, disabled-
  by-default, data-deleting mechanism. This plan's own `expire_idle()` only ever writes a marker
  file; naming and code must keep the two visually and behaviourally distinct.
- No new third-party dependency.
- Every new route follows this file's existing owner-gating pattern (`self._require_owner()`)
  and `daemon.registry.note_change(...)` convention — read `_finish`/`_cancel` in `server.py`
  (around lines 552-570) as the exact template for any new route in this plan.
- Every new CLI command follows `commands/end.py`'s exact shape (`build_parser`,
  `client_from_config()`, `preflight(client)`, the `try/except (DaemonUnreachable,
  ContractMismatch, HttpError)` pattern).
- This repo's own test suite (`python3 -m pytest -q` from the repo root, `.venv` activated) is
  the only test surface — 361 tests currently pass; every task must leave that suite green plus
  its own new tests, and must never touch a live running daemon (the actual production daemon on
  this machine has 49 real sessions other live Claude Code sessions currently depend on).
- **Do not deploy, restart, or otherwise touch the live running daemon process at any point in
  either task.** Both tasks are fully verifiable against this repo's own test suite in this
  isolated worktree. Deployment of the finished work to the actually-running daemon is a
  separate, explicit-confirmation-gated step outside this plan's own scope.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/webcompanion/cleanup.py` (modify) | Add `expire_idle(cfg, registry) -> int`, reusing `_last_activity`. Keep it visually distinct from `expire()`. |
| `src/webcompanion/config.py` (modify) | Add the idle-threshold config field (name/default TBD by Task 1's own research — see the design doc's open question). |
| `src/webcompanion/server.py` (modify) | Add the periodic background sweep inside `Daemon.start()`; add the `unfinish` route (`_SID_UNFINISH_RE`, `_unfinish` handler) matching `_finish`/`_cancel`'s shape. |
| `src/webcompanion/client.py` (modify) | Add a `Client.unfinish(sid)` method matching `finish`/`cancel`'s shape. |
| `src/webcompanion/commands/unfinish.py` (new) | `webcompanion unfinish --sid <sid>`, matching `commands/end.py`'s exact shape. |
| `src/webcompanion/commands/_common.py` or wherever the CLI's subcommand registry lives (modify) | Register the new `unfinish` subcommand — find the existing registration point for `end`/`watch`/etc. and mirror it. |
| `tests/test_cleanup.py` (modify) | Tests for `expire_idle`. |
| `tests/test_server_sessions.py` or a new `tests/test_server_lifecycle.py` (modify/new) | Tests for the `unfinish` route and the periodic sweep's wiring. |

---

### Task 1: The un-finish capability

**Files:**
- Modify: `src/webcompanion/server.py`, `src/webcompanion/client.py`
- Create: `src/webcompanion/commands/unfinish.py`
- Modify: whichever file registers CLI subcommands (find it — likely `cli.py` or `__main__.py`;
  grep for where `end`'s subcommand gets registered and mirror that exactly)
- Modify/create: a test file covering the new route and CLI command

**Interfaces:**
- Consumes: `_mark`, `_is_marked`, `_FINISHED_MARKER`, `_CANCELLED_MARKER` (existing, unchanged).
- Produces: `POST /s/{sid}/api/unfinish` (owner-gated, matching `/api/finish`'s shape exactly —
  same auth, same `_session(sid)` resolution, same `note_change` call), `Client.unfinish(sid) ->
  dict`, `webcompanion unfinish --sid <sid>` CLI command.

- [ ] **Step 1: Resolve the open questions from the design doc, with real evidence**

  Read `server.py`'s `_poll` handler and whatever consumes `_watcher_seen_at`/`watcher_seen_at`
  downstream (check the IDE plugin's `pollLiveness`-equivalent methods if easily reachable, or at
  minimum reason from this daemon's own `/poll` response shape and what a client waking up to a
  freshly-unfinished-but-stale-heartbeat session would see) to decide: does `unfinish` need to
  also clear or refresh `_HEARTBEAT_FILE`? Write your decision and reasoning into this task's
  report — this is a real design call this plan deliberately left open rather than guessing.

- [ ] **Step 2: Add the `/api/unfinish` route**

  In `server.py`: add `_SID_UNFINISH_RE = re.compile(r"^/s/([^/]+)/api/unfinish$")` near the
  other `_SID_*_RE` patterns (around line 48-58), dispatch it in `do_POST` alongside `_finish`/
  `_cancel` (around line 449-454), and add `_unfinish(self, sid: str) -> None` mirroring `_finish`
  exactly (owner check, `_session(sid)` resolution, `note_change`) except its body removes
  whichever marker file(s) are present:

  ```python
  def _unfinish(self, sid: str) -> None:
      if not self._require_owner():
          return
      resolved, dirs = self._session(sid)
      if resolved is None:
          return
      state_dir = Path(dirs["state_dir"])
      for marker in (_FINISHED_MARKER, _CANCELLED_MARKER):
          try:
              (state_dir / marker).unlink()
          except FileNotFoundError:
              pass  # idempotent -- already live, nothing to undo
      # Apply Step 1's decision about the heartbeat file here.
      daemon.registry.note_change(resolved)
      self._json(200, {"ok": True})
  ```

  (Adjust exactly per Step 1's heartbeat decision before finalizing.)

- [ ] **Step 3: Write tests for the route**

  Using this repo's existing test fixtures for session routes (read `tests/test_server_sessions.py`
  or `tests/conftest.py` for the fixture pattern already used to spin up a test daemon instance),
  write tests: unfinishing a `finished` session clears the marker and `/poll` subsequently reports
  `finished: false`; unfinishing a `cancelled` session behaves the same way for `cancelled`;
  unfinishing an already-live session is a no-op success, not an error; unfinishing without
  ownership is rejected the same way `/api/finish` already is (find and mirror that existing test).

- [ ] **Step 4: Add `Client.unfinish`**

  In `client.py`, add a method mirroring `finish`/`cancel`'s exact shape and calling the new route.

- [ ] **Step 5: Add the CLI command**

  Create `commands/unfinish.py` mirroring `commands/end.py` exactly (same `build_parser`/`run`
  shape, `--sid` required, no `--cancel`-equivalent flag needed since un-finishing is unconditional).
  Register it wherever `end`/`watch`/etc. are registered — find that registration point and mirror
  it precisely (same help text conventions, same subcommand list ordering approach).

- [ ] **Step 6: Run the test suite for this task**

  Run: `cd <this worktree> && source ../../.venv/bin/activate && python3 -m pytest -q`
  Expected: 361 existing plus this task's new tests, 0 failures.

- [ ] **Step 7: Commit**

  ```bash
  git add src/webcompanion/server.py src/webcompanion/client.py src/webcompanion/commands/unfinish.py <cli registration file> <test file>
  git commit -m "feat: add \`webcompanion unfinish\` to reopen a finished/cancelled session"
  ```

---

### Task 2: Daemon-level auto-expiry of idle sessions

**Files:**
- Modify: `src/webcompanion/cleanup.py`, `src/webcompanion/config.py`, `src/webcompanion/server.py`
- Modify: `tests/test_cleanup.py`

**Interfaces:**
- Consumes: `_last_activity` (existing, in `cleanup.py` — reuse verbatim, do not redefine
  idleness a second way). `_mark`/`_FINISHED_MARKER`/`_is_terminal` (existing, in `server.py`)
  are NOT imported into `cleanup.py` — see Step 2's circular-import fix — they're passed into
  `expire_idle` as parameters from its one call site in `server.py`, which already defines all
  three itself.
- Produces: `expire_idle(cfg: Config, registry: Registry, *, is_terminal, mark_finished) -> int`
  (returns count marked finished, mirroring `expire()`'s own return-count convention), a new
  `Config` field for the idle threshold, a periodic background sweep wired into `Daemon.start()`.

- [ ] **Step 1: Resolve the idle-threshold value with real evidence, not a guess**

  This plan's own research found: of 33 genuinely-live (never finished/cancelled) sessions on
  the currently-running daemon, 32 are between 1 and 3 days old and only 1 is under a day old;
  none are under 6 hours old. Read `cleanup.py`'s `_last_activity` docstring and implementation
  in full, and pick a default threshold that (a) would have caught the real abandoned sessions
  observed (1-3 days old) without (b) risking marking a genuinely-in-progress same-day review
  finished. State your chosen default and reasoning explicitly in this task's report — a value
  in the range of several hours to one day is a reasonable starting point given the observed
  data, but confirm against the actual `_last_activity` semantics (what exactly counts as
  "activity" — does opening the browser tab count without any further interaction?) before
  committing to a number.

  Also decide: should this be configurable (a new `Config` field, e.g. `idle_expiry_hours`,
  parsed the same way `retention_days` is in `config.py`) with a sensible non-`None` default (so
  the feature is on by default, unlike `retention_days`), or should the daemon-level auto-expiry
  and `retention_days` share the same enable/disable philosophy? The design doc's own Decision 1
  frames this as "a safety net" that should generally be on, unlike `retention_days` — implement
  it that way (a real default, not `None`-disabled-by-default) unless your own reading of the
  config module's conventions gives you a strong reason to do otherwise; if you deviate, say so
  explicitly in the report.

- [ ] **Step 2: Add `expire_idle` to `cleanup.py`**

  **Confirmed during this plan's own pre-dispatch review — a real circular import, not a
  hypothetical one: do not import from `server.py` into `cleanup.py`.** `cleanup.py`'s current
  imports are `paths`, `config.Config`, `registry.{UNREADABLE, Registry}` — it has NO dependency
  on `server.py` today. But `server.py` already does `from webcompanion import ... cleanup ...`
  (line 33) to call the existing one-shot `cleanup.sweep()`. Adding `from webcompanion.server
  import _is_terminal, _FINISHED_MARKER, _mark` (or similar) into `cleanup.py` would create
  `server → cleanup → server`, an import cycle — Python would raise `ImportError` on whichever
  module loads second, not fail silently, so this would be caught immediately by the test suite,
  but fix it correctly from the start rather than discovering it that way.

  **Resolution: dependency injection, not a shared import.** `expire_idle` takes the
  terminal-check and marking behavior as parameters, keeping `cleanup.py`'s existing
  zero-dependency-on-`server.py` property intact — `server.py` (which already owns
  `_is_terminal`/`_mark`/the marker constants) passes them in at the one call site inside
  `Daemon.start()`'s periodic sweep (Step 3), the same way it already passes `cfg`/`registry`:

  ```python
  def expire_idle(cfg: Config, registry: Registry, *,
                  is_terminal, mark_finished) -> int:
      """Mark `finished` any live session idle past `cfg.<idle_threshold_field>`.

      Unlike `expire()`, this never deletes anything -- it only ever calls
      `mark_finished` (the same marker-file write `webcompanion end` already
      performs), and only ever touches a session `is_terminal` reports as not
      already finished/cancelled. Safety net for skills that never call `end`
      themselves; see docs/2026-09-02-session-lifecycle-design.md.

      `is_terminal`/`mark_finished` are injected (not imported from `server.py`)
      to keep this module's existing independence from `server.py` -- avoids
      introducing a `server -> cleanup -> server` import cycle, since `server.py`
      already imports `cleanup` for the existing one-shot `sweep()` call.
      """
      # implementer: fill in per Step 1's resolved threshold/config field name.
      # is_terminal(state_dir: Path) -> bool; mark_finished(state_dir: Path) -> None
  ```

  At the call site in `Daemon.start()` (Step 3), pass `is_terminal=_is_terminal,
  mark_finished=lambda d: _mark(d, _FINISHED_MARKER)` (both already defined in `server.py`,
  same module as the call site, so no import issue there at all).

- [ ] **Step 3: Wire a periodic sweep into `Daemon.start()`**

  **Confirmed during this plan's own pre-dispatch review**: `Daemon` spawns exactly ONE
  background thread today — `self._thread = threading.Thread(target=self._httpd.serve_forever,
  daemon=True)` (`server.py:265`), started in `start()`, joined with a 5s timeout in `stop()`
  (`server.py:268-273`). This periodic sweep will be the second. Mirror the existing thread's
  `daemon=True` flag (so it can never block process exit even if `stop()` doesn't explicitly
  join it) and store it as a new `self._sweep_thread` attribute alongside `self._thread`, for
  symmetry and so `stop()` can join it too if you choose to extend `stop()` — join with a short
  timeout the same way, or simply rely on `daemon=True` and skip joining it if that's simpler and
  this repo's own tests don't need deterministic sweep-thread shutdown (say which you chose in
  the report). Use a `threading.Event` for the sleep (`event.wait(interval_seconds)`) rather than
  a bare `time.sleep` in a loop — this lets a test (or a future `stop()` extension) wake the
  thread immediately via `event.set()` instead of waiting out a real interval, which matters for
  Step 4's testability.

  A background thread running `while not stop_event.is_set(): expire_idle(...); stop_event.wait(N)`
  is the correct shape. Choose a sweep interval sensibly smaller than the idle threshold itself
  (e.g., checking every 30-60 minutes for an idle threshold measured in hours is more than
  sufficient resolution) — do not poll every few seconds, this is a background hygiene task, not
  a live-latency-sensitive path. Wrap the periodic call in the same "never let this stop the
  daemon" `try/except` discipline `cleanup.sweep()`'s own call site already uses (per Step 2's
  resolution, this call site is also where `_is_terminal`/`_mark`/`_FINISHED_MARKER` are passed
  into `expire_idle` as the `is_terminal`/`mark_finished` parameters — no import needed since
  `server.py` already defines all three itself).

- [ ] **Step 4: Write tests**

  In `tests/test_cleanup.py`, mirror `expire()`'s own existing tests' structure: a session idle
  past the threshold gets `finished` written (not deleted — assert the directory and its contents
  still exist); a session within the threshold is untouched; an already-finished or -cancelled
  session is untouched (not double-marked, not erroring); a session whose `_last_activity` can't
  be determined is left alone (mirroring `expire()`'s own "can't tell how old it is — leave it"
  conservative behavior). Since `expire_idle` takes `is_terminal`/`mark_finished` as injected
  parameters (per Step 2's circular-import fix), `cleanup.py`'s own tests can pass small local
  fakes or the real `server._is_terminal`/`server._mark` — either is fine, since the function's
  own contract doesn't care which; using the real ones is probably simplest and most faithful,
  same file `expire()`'s own tests likely already import from for comparable checks. Also test
  the periodic-sweep wiring itself if this repo's existing test patterns make that practical
  (check how `cleanup.sweep()`'s own startup-wiring, if tested at all, is tested — mirror that
  approach; the `threading.Event`-based sleep from Step 3 should make this practical — start a
  real `Daemon`, set the event immediately or use a near-zero interval, confirm `expire_idle` got
  called; if `Daemon.start()`'s threading isn't practically testable in this suite's existing
  style even with that, say so in the report rather than forcing an awkward test).

- [ ] **Step 5: Run the test suite for this task**

  Run: `python3 -m pytest -q` from the repo root (`.venv` activated). Expected: previous count
  plus this task's new tests, 0 failures.

- [ ] **Step 6: Commit**

  ```bash
  git add src/webcompanion/cleanup.py src/webcompanion/config.py src/webcompanion/server.py tests/test_cleanup.py
  git commit -m "feat: auto-expire idle sessions to finished (safety net for skills that never call end)"
  ```

---

## Testing strategy

Both tasks are fully verifiable against this repo's own 361-test suite plus new tests, entirely
within this isolated worktree — no live daemon interaction is needed or permitted for either
task. Real production session data (49 live sessions, 33 currently unfinished) informed the
threshold decision in Task 2 Step 1 but must never be read, modified, or tested against directly.

## Known limitations (accepted, not deferred silently)

- Browser-facing skills' existing client-side "Done" button (`api.finish()` in `core.js`)
  remains the primary finish path for `dataflow`/`deck`/`annotate`; this plan's auto-expiry is a
  safety net under it, not a replacement.
- IDE-facing skills (`walkthrough`, `ask_diff`, `show_diff`) have no client-side finish
  affordance at all — the skill-level explicit-end step (a separate plan, in the claude-annotate
  repo) is the mitigation; a real IDE "Done" button is out of scope for this initiative.
- Deploying this to the actually-running production daemon (stopping it, upgrading it, and
  confirming the 33 currently-live sessions behave correctly under the new auto-expiry) is a
  separate, explicit-confirmation-gated step this plan does not perform.
- **The first sweep runs immediately on daemon start, not after the first wait interval** —
  `_sweep_loop` checks idleness before its first `Event.wait()`. On the eventual production
  upgrade, this means essentially all of the real 33 currently-live, never-explicitly-closed
  sessions (32 of which are already 1-3 days idle, well past the 12-hour default) will be marked
  `finished` within seconds of the restart, not gradually over the following sweep cycles. This
  is the intended safety-net behavior and every one of them is immediately recoverable via
  `webcompanion unfinish`, but whoever performs that deployment should expect and be ready for
  this immediate, bulk effect rather than being surprised by it.
