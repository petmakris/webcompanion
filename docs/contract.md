# webcompanion HTTP contract, version 1

This document describes contract 1 — the wire contract for the
`webcompanion` daemon: every route, every SSE frame, and the versioning
rule that keeps a daemon and a client from misunderstanding each other. It
is written to stand alone — if you are implementing a client (an IDE
plugin, a second Claude Code plugin, anything that is not this
repository), you should not need to read the daemon's source or its
design spec to get this right.

The daemon is a single-process, loopback-bound HTTP server. There is
normally one daemon per machine, holding sessions from every project and
every client kind that talks to it.

## Terms

- **Daemon** — the always-on `webcompanion serve` process this contract
  describes.
- **Session** — one unit of work the daemon tracks: a `sid` (session id),
  a `slug` (short human-readable id, unique within its kind), a `kind`
  (which client feature created it — see below), a `cwd` (the project
  directory it belongs to), and a small metadata bag.
- **Kind** — a short string naming which client feature owns a session.
  Kinds partition storage and slug uniqueness: two kinds may both use the
  slug `my-plan` without colliding. A kind must match
  `^[a-z][a-z0-9_-]{0,63}$`.

  A kind is a free-form string as far as the daemon is concerned, but the
  seven that exist have ONE canonical spelling each, and a client that pushes
  a different one gets a separate, invisible partition rather than an error.
  They are, exactly:

  ```
  annotate
  deck
  dataflow
  walkthrough
  interactive-review
  show-diff
  atlas
  ```

  Note the hyphen in `interactive-review`. `webcompanion migrate` derives a
  kind from the old per-skill directory name, and the directory that skill
  writes is `~/.claude/interactive-review`, so a migrated session arrives
  under the hyphenated kind — the same one a client should push. A session
  migrated from a directory named `interactive_review` would arrive under
  that kind instead, and the two are different partitions to the daemon; a
  client that finds nothing under one spelling should try the other before
  concluding a session is gone.

  `show-diff` is hyphenated too, and is the one kind `migrate` will never
  produce: it never had a per-skill server or a `~/.claude/show-diff`
  directory to migrate from, having been written against this contract in
  the first place.
- **Item** — one opaque JSON body addressed by a client-chosen string
  called an **anchor**. The daemon never inspects an item's shape; it
  stores the body, derives a version from its content hash, and hands both
  back unchanged.
- **Thread** — an append-only list of messages attached to an anchor
  (comments on an item, or on the special anchor `__general__`).
- **Owner** — a caller allowed to make write requests: either the loopback
  machine itself, or a caller presenting the daemon's write token. See
  "Who may write" below.
- **Contract version** — the integer in this document's title, bumped only
  on a breaking change to a route or a payload shape. The running daemon's
  own value is available at `/health` and printed by `webcompanion
  --version`.

## Versioning: the `X-WebCompanion-Contract` header

Every request may carry:

```
X-WebCompanion-Contract: 1
```

The daemon's behavior:

- **Header absent** → tolerated. A hand-run `curl` or a health probe is
  not a contract violation; the request proceeds normally.
- **Header present and equal to the daemon's contract number** → proceeds
  normally.
- **Header present and unreadable** (not an integer) → `426 Upgrade
  Required`, body: `unreadable X-WebCompanion-Contract: '<value>'; this
  daemon speaks contract <N>`.
- **Header present, a valid integer, lower than the daemon's** → `426`,
  body: `the client speaks contract <sent>, this daemon speaks <N>; update
  the client`. **The client is old.**
- **Header present, a valid integer, higher than the daemon's** → `426`,
  body: `the client speaks contract <sent>, this daemon speaks <N>; update
  the daemon with` `pipx upgrade webcompanion && webcompanion
  install-service`. **The daemon is old.**

The message always names which side needs to update — never leave a caller
to guess. This check runs before routing, so a `426` can come back for any
path, including one this document does not list (a future route, or a typo).
A client should send this header on every request; a body of `426` should
be treated as fatal for the whole request, not retried.

## Who may write

Two independent gates apply to every request, in this order:

1. **Contract check** (above). Fails closed with `426`.
2. **Ownership check**, only for routes marked **write** in the table
   below. Fails closed with `403 forbidden`.

A caller is the **owner** if either is true:

- The connection arrives from loopback (`127.0.0.1` / `::1`), **and** it is
  not a same-origin browser request forged by a page you merely opened —
  the daemon additionally checks `Sec-Fetch-Site` (must be
  `same-origin`/`same-site`/`none` if present) and, when an `Origin` header
  is present, that its hostname matches the request's `Host` hostname. A
  non-browser caller (curl, a CLI, an IDE plugin) sends neither header and
  is unaffected.
- The caller presents the write token in `X-WebCompanion-Token`, and it
  matches the daemon's configured token exactly.

There is no third way. A read-only client (a browser tab just viewing a
session) needs neither loopback-owner status nor the token for GET routes
that are not marked **write**.

## Session discovery and creation both require `kind`

- **Creating** a session (`POST /api/sessions`) requires a `kind` in the
  body matching `^[a-z][a-z0-9_-]{0,63}$`. Missing or malformed → `400`.
- **Discovering** sessions (`GET /api/sessions`) without `scope=all`
  requires `cwd` in the query string (`400` if absent); `kind` is an
  optional filter alongside it. Two different kinds may have live sessions
  in the same `cwd` at once — a client that does not filter by `kind` can
  get back another client's sessions.
- `scope=all` (every session on the daemon, across every `cwd` and every
  `kind`) is itself a **write**-gated read: it requires owner status even
  though it is a GET, because it can enumerate another project's sessions.

## Route table

Quick reference, one line per route (method and path only — see the table
below for what each one does and what it returns):

```
GET    /
GET    /health
GET    /api/whoami
GET    /api/sessions
GET    /_wc/core.js
GET    /_wc/favicon.svg
POST   /api/sessions
POST   /api/open
GET    /s/{sid}/
DELETE /s/{sid}/
GET    /s/{sid}/poll
GET    /s/{sid}/stream
GET    /s/{sid}/items
GET    /s/{sid}/items/<anchor>
PUT    /s/{sid}/items/<anchor>
PATCH  /s/{sid}/items
DELETE /s/{sid}/items/<anchor>
POST   /s/{sid}/api/assets
GET    /s/{sid}/assets/<relpath>
POST   /s/{sid}/api/mounts
GET    /s/{sid}/mounts/<name>/<relpath>
POST   /s/{sid}/api/upload
POST   /s/{sid}/api/submit
GET    /s/{sid}/threads
GET    /s/{sid}/threads/<anchor>
POST   /s/{sid}/threads/<anchor>
POST   /s/{sid}/api/threads/delete
POST   /s/{sid}/api/finish
POST   /s/{sid}/api/cancel
POST   /s/{sid}/api/unfinish
```

`{sid}` is a session id or its slug. A slug is unique **within a kind**,
not across kinds, so the same slug can name a session in `annotate`, in
`deck` and in `dataflow` at once. Add `?kind=<kind>` to any session-scoped
route to say which you mean. Without it, a slug matching exactly one live
session resolves; a slug matching more than one is `409` with a body naming
the candidate kinds (never `404`, which would read as "it is gone" for
something that exists three times over).
`<anchor>` and `<relpath>` are the last, free-form segment of their path —
an anchor may itself contain characters that look like path separators
once URL-decoded. **write** means the ownership check applies.

| Method | Path | write | Description |
|---|---|---|---|
| GET | `/` | | The landing page: the kinds on this daemon as a grid, plus any session that is currently live, whatever its kind. One click (a `#<kind>` fragment, no second route) opens that kind's own sessions. HTML only — it is a browser view of `/api/sessions?scope=all` and adds no state of its own. The listing it fetches is owner-gated, so a non-owner is told so rather than shown an empty list. |
| GET | `/health` | | Liveness and version: `{banner, contract, version, uptime, sessions}`. Never contract- or owner-gated beyond the header check every route gets. |
| GET | `/api/whoami` | | `{writable: bool}` — whether *this* caller currently passes the ownership check. |
| GET | `/api/sessions` | (scope=all only) | List sessions. `?cwd=<path>&kind=<kind>` (cwd required) for one project; `?scope=all` for every session on the daemon (owner only). Each row: `{sid, slug, kind, cwd, title, state, url}`, where `state` is `live`, `finished` or `cancelled`. (`state` was added after contract 1 shipped; it is additive, so a client written against the original six keys reads them unchanged.) |
| GET | `/_wc/core.js` | | The daemon's packaged browser runtime (shared JS every session's page loads). |
| GET | `/_wc/favicon.svg` | | The tab icon every session's page links to. |
| POST | `/api/sessions` | write | Create a session. Body: `{kind, cwd, title?, slug?, supersede?}`. `kind` and `cwd` are required (`400` otherwise). `supersede: true` marks every other live session of the same `kind` and `cwd` as finished. Returns `201 {sid, slug, kind, url, token}` — `token` is the daemon's write token, handed to whoever just created the session. |
| POST | `/api/open` | write | Open a file in the user's editor. Body: `{file, line?}`. `file` is resolved and then must fall inside some existing session's `cwd` (`403` otherwise) — the daemon's only subprocess capability, and this containment check is its entire defence. `404` if not a file; `500` if the editor could not be launched. |
| GET | `/s/{sid}/` | | The session's HTML shell page: a minimal page that loads `/_wc/core.js` and, if a renderer has registered (see `/api/assets` below), that renderer's entry script. `404 no such session` if `sid` does not resolve. |
| DELETE | `/s/{sid}/` | write | **Delete the session and its whole workspace** — registry row, items, threads, uploaded assets, event queue. Irreversible, with no second copy. A session that is not finished or cancelled is refused `409` unless `?force=1`: the one deletion nobody means to make is of something still running, while naming a terminal session is intent enough on its own. Refuses `409` too if the row's workspace is not where its kind's root would have put it, rather than deleting whatever the row points at. Any stream still open on the session is wound down first, so a page that is watching gets `session-ended` instead of finding its directory gone. Returns `200 {ok, sid, kind}`. |
| GET | `/s/{sid}/poll` | | One-shot state snapshot for clients that are not holding an SSE connection: `{finished, cancelled, watcher_seen_at, items: {anchor: version}, threads: {anchor: version}, acked: [event_id]}`. `acked` lists every answered event id, sorted, so a client on the polling fallback learns of an ack the same way the stream's `event-acked` frame tells an SSE client. |
| GET | `/s/{sid}/stream` | | Open an SSE connection. See "SSE frame vocabulary" below. `503 too many open streams` if the daemon is already holding `MAX_CONCURRENT_STREAMS` (200) connections. |
| GET | `/s/{sid}/items` | | Snapshot of every item: `{anchor: {body, version}}`. |
| GET | `/s/{sid}/items/<anchor>` | | One item: `{body, version, code?}`. `code`, when present, is the *freshly resolved* source for any code anchors the body names (resolved on every request, never cached — the client's repository can change while the session is open). `404 no such item` if the anchor is not stored. |
| PUT | `/s/{sid}/items/<anchor>` | write | Upsert one item. Body is the item's raw JSON body (any object). `400` if the anchor is invalid or the body exceeds the 2 MB per-item limit. |
| PATCH | `/s/{sid}/items` | write | Upsert (or fully replace) many items at once. Body: `{items: {anchor: body, ...}, replace?: bool}`. With `replace: true`, any anchor **not** present in `items` is deleted — the shape a full-document push wants. `400` if `items` is not an object, or any single anchor/body fails validation; the whole batch is rejected together (no partial write from a validation failure). |
| DELETE | `/s/{sid}/items/<anchor>` | write | Delete one item. `200 {ok: true}` whether or not the anchor existed. |
| POST | `/s/{sid}/api/assets` | write | Register a renderer for this session's shell page. Body: `{static_root, entry?}`. `static_root` must resolve to an existing directory; `entry`, if given, is the script tag written into the shell page. Registration is a file in the session's own workspace, not daemon memory, so it survives the daemon's own restarts. |
| GET | `/s/{sid}/assets/<relpath>` | | Serve a file from the registered `static_root`, containment-checked against symlink escapes. `404 no renderer registered for this session` if nothing has registered yet; `403 forbidden` on an escape attempt; `404 no such asset` otherwise. |
| POST | `/s/{sid}/api/mounts` | write | Register a named directory to serve from disk. Body: `{name, root}`. `name` matches `^[a-z0-9][a-z0-9_-]{0,63}$` (`400` otherwise); `root` must resolve to an existing directory (`400`) **inside the session's `cwd`** (`403`). Stored in the session's workspace (`mounts.json`), so it survives daemon restarts; re-registering a name replaces its root. Returns `200 {name, url}`, `url` relative to the session page. Additive to contract 1. |
| GET | `/s/{sid}/mounts/<name>/<relpath>` | | Serve `root/relpath` of a registered mount, symlinks resolved, `403` on an escape from the mount or the session's `cwd`, `404` for an unknown mount or a missing file. A path with a segment starting with `.` (`.env`, `.git/config`; `..` is judged by the escape rule), or one whose resolved target has such a segment below the mount root, is `404`: dotfiles are never served. Sent with `Cache-Control: no-store`, so a reload after a save always gets the new bytes. Additive to contract 1. |
| POST | `/s/{sid}/api/upload` | write | Upload a pasted image. Body is the raw image bytes; `Content-Type` must be one of `image/png`, `image/jpeg`, `image/gif`, `image/webp`. `413` past 10 MB, `415` on an unrecognized type, `411` if `Content-Length` is missing. Returns `200 {path, size}`. |
| POST | `/s/{sid}/api/submit` | write | Submit a comment/interaction event on an anchor. Body: `{anchor, text, images?}` — `images`, if given, must be paths this same session's own `/api/upload` produced. `400` on a missing/invalid anchor or empty text. Returns `202 {event_id}` — the event is queued for a separate watcher process to consume, not answered synchronously. **Whoever answers the event must acknowledge it**, or the watcher re-emits it (3 attempts, 30 minutes apart) and then drops it: run `webcompanion ack --sid <sid> --event-id <event_id>` on the daemon's own host. There is no HTTP route for the acknowledgement — the queue and the ack are files in the session's workspace, and the only consumer runs beside the daemon. |
| GET | `/s/{sid}/threads` | | Snapshot of every thread: `{anchor: {anchor, version, messages, title?, anchor_text?}}`. |
| GET | `/s/{sid}/threads/<anchor>` | | One thread. An anchor nobody has commented on is **not** a `404` — it comes back as `{anchor, version: 0, messages: []}`, because a client asks for the thread of every region it renders and a 404 per un-commented region is noise. `400` if the anchor is invalid. |
| POST | `/s/{sid}/threads/<anchor>` | write | Append one message. Body: `{text, role?, source_event_id?, title?, anchor_text?}` plus any other keys you want stored on the message. `text` is required and non-empty (`400` otherwise). `role` defaults to `agent`; `ts` (unix seconds) is stamped server-side. `source_event_id` makes the append **idempotent** — a second POST carrying an id already in the thread returns `{appended: false}` and changes nothing, which is what makes the watcher's re-emission safe. `title` is thread-level and last-write-wins; `anchor_text` is thread-level and first-write-wins. Returns `200 {appended, version}`. |
| POST | `/s/{sid}/api/threads/delete` | write | Delete one thread. Body: `{anchor}`. Returns `200 {deleted: bool}` — `false` simply means there was nothing there. A POST rather than a DELETE because the anchor travels in the body: an anchor may contain characters that look like path separators. |
| POST | `/s/{sid}/api/finish` | write | Mark the session finished. Ends its SSE streams (a `session-ended` frame, then close). The daemon can also mark a session finished on its own, with no client call at all — its `idle_expiry_hours` config field (see `README.md`) runs a periodic sweep that finishes any live session idle past that many hours, as a safety net for clients that never call this route themselves. `/api/unfinish` undoes either an explicit or an automatic finish identically. |
| POST | `/s/{sid}/api/cancel` | write | Mark the session cancelled. Same effect on streams as finish; the two states are reported separately by `/poll`, as `finished` and `cancelled`. `/health` reports neither — it counts sessions and says nothing about their state. |
| POST | `/s/{sid}/api/unfinish` | write | Undo a `finish`/`cancel`, whether it was manual or automatic: removes the `finished`/`cancelled` marker(s) and the watcher heartbeat file, whichever are present. Idempotent — calling it on a session that is already live is `200 {ok: true}`, not an error. Removing the heartbeat too (not just the markers) matters because a heartbeat that predates the un-finish reads as stale to a polling client, which would otherwise re-latch the session as ended on its very next poll; deleting it restores the same "no watcher yet" state a session that never had one is in. |

Every session-scoped route resolves `{sid}` against the daemon's registry
first; an unresolvable id or slug is `404 no such session` for every one
of them, before any route-specific logic runs.

## SSE frame vocabulary

`GET /s/{sid}/stream` opens a `text/event-stream` connection. Every frame
is `event: <name>\ndata: <json>\n\n`. The vocabulary is fixed and closed —
there is no per-client extension point; anything a specific client used to
emit as its own frame type is expressed as one of these:

| Event | Payload | Meaning |
|---|---|---|
| `connected` | `{}` | First frame on every stream, before anything else. |
| `item-changed` | `{anchor, version, initial?: true}` | An item was created, updated, or deleted. `version: 0` means the anchor was deleted (there is no separate `item-deleted` event). `initial: true` marks the opening snapshot the stream sends for every anchor the client could already see from its own first GET — a client that just fetched current state should not re-render on these; the flag is never set on a frame reporting an actual later change. |
| `thread-changed` | `{anchor, version, initial?: true}` | A thread's message list changed. Same `initial` semantics as `item-changed`. |
| `thread-deleted` | `{anchor}` | A thread was deleted. (Threads have a dedicated deletion frame; items do not — see above.) |
| `session-ended` | `{}` | The session became finished or cancelled. The daemon sends this and then closes the connection; a client should not attempt to reconnect a stream for a session it has just been told ended. |
| `heartbeat` | `{}` | Sent after 30 seconds with no other frame, so a client (and any proxy in between) can tell the connection is still alive rather than merely silent. |

A stream that cannot get a slot (200 already open) never reaches any of
this: it gets a plain `503 too many open streams` at connection time, not
inside the event stream.

## Failure modes a client should distinguish

- **`403 forbidden`** — the caller is not the owner. Only returned from
  routes marked **write** above (plus `GET /api/sessions?scope=all`).
- **`409 Conflict`** — an ambiguous slug: it names a live session in more
  than one `kind`. The body lists the kinds; retry with `?kind=<kind>`.
- **`408 Request Timeout`** — only from `/s/{sid}/api/upload`: the declared
  `Content-Length` did not arrive within 30 seconds. The daemon will not
  hold a thread for a client that stops sending.
- **`404`** — either "no such session" (an unresolvable `{sid}`/slug), "no
  such item", "no such asset", or "no such file" (from `/api/open`),
  distinguished by the response body text.
- **`426`** — contract mismatch; see "Versioning" above. This is the one
  status a client should treat as fatal for the *whole* connection to this
  daemon, not just the one request, since every other route will answer
  the same way until one side is upgraded.
- **`400`** — malformed input on an otherwise-reachable, otherwise-allowed
  request (missing required field, invalid `kind`, oversized item, and so
  on). The body names the specific problem.

Below the HTTP layer, a client cannot reach the daemon at all — connection
refused, DNS failure, timeout. That is not a status code this document can
name; see the main `README.md`'s "when things go wrong" section for how
this package's own CLI reports it (the daemon is never auto-started by a
client on this failure).
