# webcompanion

A local always-on companion server: addressable items, comment threads, and
an event queue.

`webcompanion` is a standalone, standard-library-only Python package and
CLI. It runs as a local HTTP daemon that Claude Code plugins and IDE
integrations talk to over a small versioned HTTP contract — see
[`docs/contract.md`](https://github.com/petmakris/webcompanion/blob/main/docs/contract.md) for the full route table and SSE
frame vocabulary, written for anyone implementing a client against it.

## Requirements

- Python 3.9+
- **macOS or Linux only.** The daemon's thread-store locking
  (`threads.py`) uses `fcntl`, which does not exist on Windows. There is
  no Windows build and none is planned.
- **Zero runtime dependencies.** Everything the daemon needs ships in the
  standard library. This is not a minimalism preference — it is what lets
  the installed service run as a single-file zipapp instead of a
  virtualenv (see "Why a zipapp, not a virtualenv" below).

## Install

```bash
pipx install webcompanion
webcompanion install-service
```

The second command builds the zipapp, mints a write token, writes a
launchd job (macOS, `~/Library/LaunchAgents/dev.webcompanion.plist`) or a
systemd user unit (Linux, `~/.config/systemd/user/webcompanion.service`),
and starts it. Re-run it after every upgrade — it is idempotent: it never
re-mints the token (which would invalidate an IDE plugin's saved
credential), and each run always installs and restarts against the
version you just upgraded to.

## Why a zipapp, not a virtualenv

`pipx` normally gives you a virtualenv bound to the interpreter that
created it. That is fine for a foreground CLI: if that Python is ever
removed, you find out the next time you try to run the command. It is not
fine for an always-on background service: when the interpreter is retired
(a Homebrew upgrade, an OS update), the venv's entry point fails to exec,
and `launchd`'s `KeepAlive` respawns the job forever at its throttle
interval — the service shows as present in `launchctl list`, every client
sees connection refused, and nothing in that failure says why.

Since the package has no runtime dependencies, a virtualenv buys nothing
here — there is nothing to isolate. So `install-service` instead builds a
`webcompanion.pyz` zipapp and points the service definition at
`/usr/bin/env python3 <path>/webcompanion.pyz serve`. `python3` is
resolved fresh from `PATH` on every single launch, so replacing the system
Python never leaves a dangling absolute interpreter path baked into the
service file. If you were using a plain pip install with an absolute
interpreter path before, `webcompanion doctor` will tell you.

## A caveat about *which* Python 3 the service actually gets

The shell you run `webcompanion` from and the service `launchd`/`systemd`
starts do **not** necessarily see the same `python3`. A launchd/systemd
job gets a minimal `PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`) — it does not
inherit your shell's `PATH`, so a Homebrew or pyenv Python that your shell
finds first is often invisible to the service, which then falls back to
whatever `/usr/bin/env python3` resolves to under that minimal `PATH`.

This is not a hypothetical: on the machine this package was built on, the
interactive shell resolves `python3` to Homebrew 6.0's Python 3.14.7, but
the *service's* `/usr/bin/env python3`, under launchd's minimal `PATH`,
resolves to Apple's Command Line Tools Python at
`/Library/Developer/CommandLineTools/usr/bin/python3` — version **3.9.6**.
That gap is exactly why this package's `requires-python = ">=3.9"` is
load-bearing today, not future-proofing for some hypothetical old system —
3.9 is the real minimum this service runs on in practice, on the machine
where it was written.

`webcompanion doctor` reports both interpreters **separately**, so this
drift is visible instead of silently working until it doesn't:

```
python3 (running doctor): /Users/you/.venv/bin/python (3.14.7)
python3 (via /usr/bin/env, the service's minimal PATH): /Library/Developer/CommandLineTools/usr/bin/python3 (3.9.6)
```

If the second line reports a version below 3.9, or reports `NOT FOUND`,
the service will fail to exec — `doctor` says so and exits non-zero.

## Configuration

Every setting lives in one file, `~/.claude/webcompanion/config.json`,
written mode `0600` because it carries the write token.

`install-service` creates it and mints a token if there is none. Nothing
else writes it — edit it by hand, then restart the service. **There are no
environment variables.** The five per-skill servers this package replaces
were launched by a Claude session and inherited its shell environment, so
`WEBCOMPANION_BIND` and friends worked; a launchd or systemd job gets a
fixed environment those never reach, and a setting that quietly stops
applying is worse than one that never existed.

A field that is missing, or has the wrong type, falls back to its default —
the file is never rejected, because a daemon that refuses to start over a
typo in an optional field is worse than one running on defaults. A file
that will not parse at all is also treated as absent by the daemon, but
`install-service` refuses to mint a token over it (that would discard the
token an IDE plugin has already saved).

| Field | Type | Default | Effect |
|---|---|---|---|
| `port` | integer | `3080` | The TCP port the daemon listens on. If something else already holds it, `serve` refuses to bind and names the holder rather than silently splitting requests between two servers — change this field and restart. |
| `bind` | string | `"127.0.0.1"` | The address the daemon listens on. **Loopback is not a default to change casually:** one daemon holds sessions from *every project on the machine*, and any address beyond loopback exposes all of them, plus `/api/open` (which launches an editor) and every session's registered renderer files, to anything that can reach that address. `"::1"` is supported. |
| `token` | string | minted on first `install-service` | The write token. A caller presenting it in `X-WebCompanion-Token` may write from anywhere; loopback callers do not need it. It is **never re-minted** on upgrade or restart — re-minting would invalidate the credential an IDE plugin has saved mid-session. Rotate it by editing this field and restarting; every saved client credential stops working at that moment, on purpose. |
| `retention_days` | integer or `null` | `null` — **infinite** | Delete a workspace idle this many days. `null` means workspaces are never deleted by age, and that is the default deliberately: `resume <slug>` is a shipped feature, workspaces go back to install day, and there is no backup. Setting this starts deleting real data on the next daemon start. |
| `idle_expiry_hours` | integer or `null` | `12` | Auto-mark a live session `finished` (never deleted — the same marker file `webcompanion end` writes) once it has been idle this many hours, as a safety net for skills that never call `end` themselves. Unlike `retention_days`, this ships **on**: 12 hours was picked against real production data, where every genuinely abandoned session was 1–3 days idle and none were under 6 hours, leaving a full same-day working session comfortable room before this could act on it. `null` turns the safety net off entirely. |
| `workspace_root` | string or `null` | `null` → `~/.claude/webcompanion/workspaces` | Where session directories live. Must be an **absolute** path; a relative one is ignored in favour of the default, because it would resolve against the daemon's own working directory (`/` under launchd) rather than anywhere anyone meant. |

A complete file, with every field at its default except the token:

```json
{
  "port": 3080,
  "bind": "127.0.0.1",
  "token": "…",
  "retention_days": null,
  "idle_expiry_hours": 12,
  "workspace_root": null
}
```

`webcompanion doctor` prints this file's path and mode, warns if the mode is
not `0600`, and reports whether `sessions.json` beside it still parses.

## Commands

| Command | What it does |
|---|---|
| `webcompanion install-service` | Build the zipapp, write the launchd plist or systemd unit, start the service. Idempotent; re-run after every upgrade. |
| `webcompanion uninstall` | Stop and unload the service and remove the plist/unit and the zipapp. **Leaves the config file and every workspace alone** and prints where they are. |
| `webcompanion status` | Is it running, on which port, with how many sessions. |
| `webcompanion doctor` | The fuller diagnosis: both interpreters, the config, whether `sessions.json` parses, the zipapp, whether the service is installed *and* whether launchd/systemd knows the job, the port holder, restart count, health, and the tail of the service log. |
| `webcompanion push` | Create a session and load its items from a JSON file. |
| `webcompanion update` | Replace one item's body by anchor. |
| `webcompanion end` | Mark a session finished (or `--cancel`). |
| `webcompanion watch` | Follow a session's event queue, printing one banner per event. Fails if the sid is not a registered session; it never creates one. |
| `webcompanion ack` | Acknowledge an event by id. **Required after answering one** — see below. |
| `webcompanion migrate` | Move workspaces out of the five older per-skill roots. |
| `webcompanion serve` | Run the daemon in the foreground. This is what the service execs; you do not normally run it yourself. |

### Answering an event means acknowledging it

`watch` prints `WEBCOMPANION_EVENT skill=<kind> sid=<sid> event_id=<id>`
and then waits for that event to be acknowledged. Whoever answers must run:

```bash
webcompanion ack --sid <sid> --event-id <id>
```

Without it the watcher re-emits the same event up to three times, 30
minutes apart, and then gives up with `WEBCOMPANION_DROPPED` — so a
question that *was* answered is reported to the user as dropped, an hour and
a half later.

## When things go wrong

Every command talks to the daemon over HTTP and reports exactly one of
three problems — never anything vaguer:

**The service is not installed** (no config file has ever been written):

```
webcompanion: the companion service is not installed.

  pipx install webcompanion && webcompanion install-service
```

**The service is installed but not answering** (config exists, but no
daemon is listening on the configured port):

```
webcompanion: the service is installed but not answering on http://127.0.0.1:3080.

  webcompanion status
  launchctl kickstart -k gui/$UID/dev.webcompanion   # macOS
  systemctl --user restart webcompanion              # Linux

Log: /Users/you/.claude/webcompanion/webcompanion.log
```

Run `webcompanion status` for a quick read on whether it's running at
all, and `webcompanion doctor` for the fuller diagnosis (both
interpreters, the zipapp, the service's own interpreter, restart count,
and the health check).

**Contract mismatch** — the client and the daemon disagree about the HTTP
contract version, answered as HTTP `426`:

```
webcompanion: the client speaks contract 2, this daemon speaks 1; update the daemon with
pipx upgrade webcompanion && webcompanion install-service
```

(or the reverse message, telling you to update the *client*, if the
daemon is the newer side). See
[`docs/contract.md`](https://github.com/petmakris/webcompanion/blob/main/docs/contract.md#versioning-the-x-webcompanion-contract-header)
for exactly when this fires. This package's CLI never starts the daemon
on your behalf on any of these three paths — installing and starting it
is yours to do; ours is only to say clearly what's wrong.

## Migrating from the older per-skill servers

If you have used the five older per-skill servers this package replaces
(`annotate`, `deck`, `dataflow`, `walkthrough`, `interactive_review`, each
with its own state root under `~/.claude/`), migrate them into this
daemon's single workspace root:

```bash
webcompanion migrate            # dry run (the default) -- prints what would move, moves nothing
webcompanion migrate --into DIR # rehearsal -- copies into DIR, leaves every source untouched
webcompanion migrate --apply    # the real move
```

`--apply` refuses to run while the service is answering: a migration moves
whole workspaces while the daemon's own startup sweep deletes any it has no
registry row for. Stop the service, migrate, start it again. `--into`
rehearses without stopping anything.

**Copying an old root's directory tree does not isolate a test run.** The
old registry (`sessions.json`) stores each session's directories as
*absolute paths* pointing at wherever they actually live, and copying the
tree does not rewrite them. `plan()` now records which root each row came
from and refuses any row that resolves outside it, so this is caught rather
than obeyed — but `--into DIR` remains the way to rehearse, because it
copies each workspace into `DIR` and leaves every original untouched.

## Development

```bash
pip install -e .
python3 -m pytest -q
```

`project.dependencies` is `[]` and stays that way — CI asserts it
directly. The `dev` extra (`pytest`, `playwright`) is dev-only and never
imported by anything under `src/`.

## Further reading

- [`docs/contract.md`](https://github.com/petmakris/webcompanion/blob/main/docs/contract.md) — the full HTTP route table, the
  SSE frame vocabulary, and the versioning rule, written to stand alone
  for anyone implementing a client.
