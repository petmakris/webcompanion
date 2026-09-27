# Changelog

## Unreleased

- Mounts: `POST /s/{sid}/api/mounts` registers a named directory inside the
  session's `cwd`, and `GET /s/{sid}/mounts/<name>/<relpath>` serves it from
  disk with `Cache-Control: no-store`. Additive; the contract stays 1.

## 1.0.0

First release. `webcompanion` replaces five separate per-skill local HTTP
servers (`annotate`, `deck`, `dataflow`, `walkthrough`,
`interactive_review`) with one standalone, standard-library-only package
and always-on daemon.

- Daemon: one threaded HTTP server on loopback, `kind`-namespaced sessions,
  addressable items with derived versions, comment threads, an event
  queue, and SSE streaming (`item-changed`, `thread-changed`,
  `thread-deleted`, `session-ended`, `heartbeat`) — see
  [`docs/contract.md`](docs/contract.md) for the full route table.
- Contract versioning via `X-WebCompanion-Contract`, answered `426` on a
  mismatch naming which side is old; a missing header is tolerated.
- Ownership model: loopback-by-construction plus a durable write token,
  with `Sec-Fetch-Site`/`Origin` checks against a malicious same-machine
  page.
- Code anchors: an item's `code` references are resolved fresh on every
  read against the client's own working tree, never cached stale.
- CLI: `serve`, `push`, `update`, `end`, `watch`, `install-service`,
  `status`, `doctor`, `migrate`. Every command reports one of three
  diagnoses on failure — not installed, installed but not answering, or a
  contract mismatch — and never starts the daemon itself.
- `install-service` ships the daemon as a zipapp run via
  `/usr/bin/env python3`, specifically so the service never binds to a
  virtualenv interpreter that a later Homebrew/OS upgrade can retire.
- `doctor` reports the interpreter running `doctor` and the interpreter
  the service's minimal `PATH` actually resolves separately, since they
  can (and, on the machine this was built on, do) differ.
- `migrate` moves workspaces out of the five old per-skill roots into the
  new single workspace root: `--dry-run` (default) only prints, `--into
  DIR` rehearses by copying with every source left untouched, `--apply`
  performs the real move. Crash-safe: a kill between moving a workspace's
  files and registering it is recovered on the next run rather than left
  orphaned or silently marked done.
- Zero runtime dependencies (`project.dependencies = []`, enforced by CI).
  Requires Python 3.9+, macOS or Linux only (`threads.py` uses `fcntl`).
