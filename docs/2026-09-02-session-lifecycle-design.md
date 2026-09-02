# Session lifecycle: auto-expiry, un-finish, and skill-level explicit end

## The problem, with real numbers

As of this writing, the running daemon (uptime ~25h) holds 49 session workspaces. Of those,
10 are `finished`, 8 are `cancelled`, and **33 have never been marked either way** — genuinely
"live" as far as the daemon and every armed watcher are concerned. Of those 33: one is under a
day old; **32 are between 1 and 3 days old**, dominated by `show-diff` sessions from two days
ago. None of the 49 sessions is younger than an hour and unfinished-but-abandoned; the pattern
is not "sessions are abandoned quickly" but "sessions are never explicitly closed at all, and
accumulate for days."

Root cause, confirmed by reading every migrated skill's `SKILL.md`: **no skill calls
`webcompanion end` on natural completion.** The only call site of `webcompanion end` in any of
`dataflow`, `walkthrough`, or `ask_diff`'s `SKILL.md` is a "Terminal cancellation" section
triggered by the user explicitly saying "scrap it" / "stop the review" — an abort path, not a
finish path. `deck`'s `SKILL.md` has no `webcompanion end` reference at all. `show_diff` and
`annotate` were not part of this program's own migration but are on the same daemon and show
the identical gap.

Two different failure shapes exist, confirmed by reading the actual client code:
- **Browser-facing skills** (`dataflow`, `deck`, `annotate`) already have a client-side "Done"
  button wired to `window.WebCompanion.api.finish()` in the daemon's own shared runtime
  (`static/core.js`). The gap here is that a user closing the tab instead of clicking Done never
  calls it — the mechanism exists, but nothing requires using it.
- **IDE-facing skills** (`walkthrough`, `ask_diff`, `show_diff`) have **no finish affordance at
  all** on the client side — confirmed by grepping the whole `ide-plugin` Java tree for
  `api/finish`, which returns nothing. For these three, a session only ever ends via the
  explicit "scrap it" cancel path, or never.

This is the same shape the daemon's own `webcompanion end` CLI command and `/api/finish` route
already fully support — nothing here requires a new daemon-side write path for finishing. What's
missing is (a) something that catches the "nobody ever called it" case automatically, and
(b) a way to undo an automatic (or manual) finish that turns out to have been premature, since
"finished" today is a one-way door with no route back to a live session.

## Decisions taken

**1. Daemon-level auto-expiry of idle sessions, marking them `finished` (not deleting anything).**
This is a safety net, not the primary mechanism — it exists so watchers stop leaking even when a
skill's own explicit-finish step (Decision 3) is missing or skipped. It reuses the exact same
`_mark(state_dir, _FINISHED_MARKER)` write the CLI's `end` command already performs — there is no
new terminal state, only a new caller of the existing one.

**2. A new "un-finish" capability**, since auto-expiry being a safety net means it can and will
occasionally act on a session the user actually still wants — the whole point of a *safety net*
is that it may be wrong sometimes, and being wrong must be recoverable. Confirmed safe to build:
`finished`/`cancelled` are marker *files*, and marking one never touches `items/`, `threads/`, or
any other session data (verified by reading every call site of `_mark` and `_is_terminal` in
`server.py` — none of them deletes anything). The only thing "finished" currently does is make
`_is_terminal()` return `True`, which the SSE stream (`stream.serve`) and `webcompanion watch`
both use to end the live channel. Removing the marker file is therefore sufficient to make a
session live again in every sense that matters — no data was ever at risk.

**3. Skill-level explicit "call `webcompanion end` when naturally done" steps**, added wherever
missing. **This is now a bigger surface than originally scoped.** The session-leak investigation
that kicked off this initiative named `show_diff` and `walkthrough` specifically; with `dataflow`,
`deck`, and `ask_diff` freshly migrated by this program, all three turn out to have the identical
gap. The skill-side task is therefore: add an explicit natural-completion finish step to
`dataflow`, `deck`, `walkthrough`, and `ask_diff` (the four skills this program's plans and
worktrees can touch directly), and flag `show_diff`/`annotate` for the same treatment as a
follow-up outside this program's own worktree scope (they were not part of the webcompanion
cutover and have their own SKILL.md ownership).

**4. `retention_days`/`cleanup.expire()` are never touched.** That mechanism `shutil.rmtree`s a
whole workspace and ships disabled by design, specifically because `resume <slug>` is a real
feature and workspaces need to survive indefinitely by default. Auto-expiry (Decision 1) only
ever writes a marker file — it is a fundamentally different, much smaller-blast-radius operation,
and the two must stay independent: enabling auto-expiry must never be read as "and now it's safe
to also turn on retention," and the code must not create any coupling that makes that easier to
do by accident.

## Mechanism

### Auto-expiry trigger: a periodic in-process sweep, not an opportunistic per-request check

The daemon already runs one cleanup pass, `cleanup.sweep()`, but only once, at `Daemon.start()`
(`server.py:236`) — appropriate for its actual job (removing stray directories no registry row
points at) but insufficient for idle-session detection, since the daemon runs for days between
restarts (current uptime: ~25h) and an idle session reached that state long after startup.

**Decision: a periodic background sweep**, started alongside the daemon's existing HTTP server
thread in `Daemon.start()`, running every `N` minutes (see "Open question: the threshold" below)
and calling a new `expire_idle(cfg, registry)` function — deliberately named differently from
`cleanup.expire()`, to keep the two mechanisms visually as well as behaviourally distinct in the
code. This matches the codebase's own established pattern for periodic filesystem-driven checks
(`webcompanion watch`'s own poll loop is the closest existing precedent, though it's a separate
process — the daemon's own sweep is the same idea running as a background thread inside the
already-running server process, since unlike `watch` it has no reason to be a separate OS
process).

`expire_idle` walks every LIVE (not already finished/cancelled) row in the registry and marks
`finished` any session whose **idleness** exceeds the threshold. Idleness is measured the same
way `cleanup.expire()`'s own `_last_activity` helper already measures it for a workspace
(newest mtime across the base dir and its meaningful children) — reuse that helper rather than
inventing a second idleness definition, so "how stale is this session" means one thing across
the whole module.

### The un-finish capability

A new CLI subcommand, `webcompanion unfinish --sid <sid>`, matching `end`'s own shape
(`commands/end.py` is the direct template — same `client_from_config()`/`preflight()`/error
handling pattern), backed by a new HTTP route and a new `Client` method (matching how `end`'s
`finish`/`cancel` client methods are already shaped). The route removes the `finished` or
`cancelled` marker file if present (idempotent — calling it on a session that isn't marked either
way is a no-op success, not an error) and returns confirmation. Once removed, the session is
live again in every sense: `/poll`'s `finished`/`cancelled` fields go back to `false`,
`_is_terminal()` returns `False`, and a fresh `webcompanion watch` on that `sid` resumes exactly
as it would for a session that was never finished at all — no special-case "resumed" state is
needed anywhere else in the daemon, because "finished" was never anything more than a boolean
file's presence.

**Open question for the implementing task to settle with real code, not to guess here**: should
`unfinish` also touch `_HEARTBEAT_FILE` (the watcher heartbeat), given a long-idle session's
heartbeat will read as very stale immediately after un-finishing? Read how `pollLiveness`
/`_watcher_seen_at` react to a stale-but-present heartbeat before deciding whether to clear it,
leave it, or refresh it to "now" — whichever avoids a session that was just resumed still reading
as `PAUSED`/stale for a confusing period.

### Skill-level explicit finish steps

For each of `dataflow`, `deck`, `walkthrough`, `ask_diff`: add a natural-completion section
(distinct from each skill's existing "Terminal cancellation" section) documenting when Claude
should call `webcompanion end --sid <sid>` (finish, not cancel) — e.g., the user indicates the
review/tour/diagram walkthrough is done, or (for browser-facing skills) proactively when Claude
detects the natural end of the interaction, mirroring whatever signal already exists in that
skill's own flow. This is a **documentation-only change per skill** — no new daemon route is
needed, `webcompanion end` without `--cancel` already does exactly this.

## What is explicitly NOT in scope

- `retention_days` / `cleanup.expire()` — untouched, per Decision 4.
- `show_diff` and `annotate`'s own `SKILL.md` — flagged as a follow-up, not touched by this
  initiative's own claude-annotate worktree (they live in the same repo but are not part of this
  program's migration scope).
- Any change to `webcompanion watch`'s own loop — `watch_loop` already correctly exits on
  `_is_terminal()`; nothing about auto-expiry or un-finish requires touching it.
- A "Done" button for the three IDE-facing skills (`walkthrough`, `ask_diff`, `show_diff`) —
  worth naming as a real gap (confirmed: no client-side finish affordance exists for these three
  at all), but building IDE UI is a separate, larger initiative than this one; the skill-level
  explicit-finish step (Decision 3) is the mitigation this initiative delivers for them.

## Self-review

- **Spec coverage**: auto-expiry, un-finish, and skill-level steps are all covered by a task in
  the implementation plan below.
- **Ambiguity check**: the idle threshold's exact value and the heartbeat-on-unfinish question
  are both named explicitly as open questions for the implementing task to settle with evidence,
  rather than guessed here — consistent with this whole program's established discipline of not
  inventing plan text a real code-read would settle better.
- **Scope check**: focused on one subsystem (session lifecycle); does not touch `retention_days`,
  `watch`'s loop, or any skill's core review/diagram/tour logic.
