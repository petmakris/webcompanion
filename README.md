# webcompanion

A local always-on companion server: addressable items, comment threads, and
an event queue.

`webcompanion` is a standalone, standard-library-only Python package and
CLI. It runs as a local HTTP daemon that Claude Code plugins and IDE
integrations talk to over a small versioned HTTP contract — see
[`docs/contract.md`](docs/contract.md) for the full route table and SSE
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
[`docs/contract.md`](docs/contract.md#versioning-the-x-webcompanion-contract-header)
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

**`--into DIR` copies; it does not isolate.** The old registry
(`sessions.json`) stores each session's directories as *absolute paths*
pointing at wherever they actually live. Copying a workspace's directory
tree to a scratch location does **not** rewrite those paths — a `plan()`
or `apply()` pointed at the copy still resolves back to the *original,
live* directories. The only safe way to rehearse a migration without
touching real data is `--into DIR`, which copies each workspace's files
into `DIR` and leaves every original untouched; do not try to build your
own isolated test run by copying an old root's directory tree yourself
and running `--apply` against the copy — it will still move the real
data.

## Development

```bash
pip install -e .
python3 -m pytest -q
```

`project.dependencies` is `[]` and stays that way — CI asserts it
directly. The `dev` extra (`pytest`, `playwright`) is dev-only and never
imported by anything under `src/`.

## Further reading

- [`docs/contract.md`](docs/contract.md) — the full HTTP route table, the
  SSE frame vocabulary, and the versioning rule, written to stand alone
  for anyone implementing a client.
