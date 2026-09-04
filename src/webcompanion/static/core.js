// webcompanion browser runtime. Owns select -> comment -> submit -> refetch
// and renders nothing: a client marks its own commentable regions with
// data-wc-anchor, and on a delta this hands the anchor + version back for
// the client to re-fetch and redraw. What an anchor's content IS -- prose,
// a slide, a diff hunk, a graph node -- is never this file's business.
(function () {
  const BASE = (() => {
    const p = window.location.pathname;
    return p.endsWith("/") ? p : p + "/";
  })();

  // ── Contract negotiation ────────────────────────────────────────────
  // Sent on every fetch so a stale page is told (426) rather than left to
  // silently misbehave against a daemon that has moved on. EventSource has
  // no way to set a request header, so the stream connection cannot carry
  // it -- the server tolerates a request with the header absent.
  const CONTRACT = 1;
  const CONTRACT_HEADER = "X-WebCompanion-Contract";

  // ── Write capability ──────────────────────────────────────────────────
  // Reads need nothing. Writes need either a loopback connection (which the
  // server recognises on its own) or this token. It arrives in the URL
  // fragment, which browsers never send to the server and never write to
  // logs or Referer headers -- so the owner URL can be pasted into a
  // terminal or a note without the credential leaking through the request
  // path.
  //
  // Held in sessionStorage, not localStorage: a shared or borrowed device
  // forgets it when the tab closes.
  const TOKEN_HEADER = "X-WebCompanion-Token";
  const TOKEN_KEY = "webcompanion.token." + window.location.host;

  const token = (() => {
    const m = /(?:^|[#&])k=([^&]+)/.exec(window.location.hash || "");
    if (m) {
      const t = decodeURIComponent(m[1]);
      try { sessionStorage.setItem(TOKEN_KEY, t); } catch (_) {}
      // Strip it from the address bar so a screenshot or a shoulder-surfer
      // does not carry write access away. Same document, no reload.
      try {
        history.replaceState(null, "", window.location.pathname + window.location.search);
      } catch (_) {}
      return t;
    }
    try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (_) { return ""; }
  })();

  function headers(extra) {
    const h = Object.assign({ [CONTRACT_HEADER]: String(CONTRACT) }, extra || {});
    if (token) h[TOKEN_HEADER] = token;
    return h;
  }

  let writable = false;

  const api = {
    BASE,
    get writable() { return writable; },
    async fetchJSON(path, opts) {
      const merged = Object.assign({}, opts || {});
      merged.headers = headers(merged.headers);
      const r = await fetch(BASE + path, merged);
      if (!r.ok) throw new Error(`${path}: ${r.status}`);
      return await r.json();
    },
    async fetchText(path) {
      const r = await fetch(BASE + path, { headers: headers() });
      if (!r.ok) throw new Error(`${path}: ${r.status}`);
      return await r.text();
    },
    async submit(payload) {
      const r = await fetch(BASE + "api/submit", {
        method: "POST", headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error("submit failed: " + r.status);
      return await r.json();
    },
    async finish() {
      const r = await fetch(BASE + "api/finish", { method: "POST", headers: headers() });
      return r.ok;
    },
    async cancel() {
      const r = await fetch(BASE + "api/cancel", { method: "POST", headers: headers() });
      return r.ok;
    },
    async pasteImage(blob) {
      const r = await fetch(BASE + "api/upload", {
        method: "POST",
        headers: headers({ "Content-Type": blob.type || "image/png" }),
        body: blob,
      });
      if (!r.ok) throw new Error("upload failed: " + r.status);
      return await r.json();
    },
  };

  // Ask the server rather than inferring from the hostname: loopback grants
  // write access with no token at all, and only the server knows whether the
  // token we hold is still the configured one. The token is NOT reminted on
  // restart -- it lives in the daemon's config file and survives every
  // upgrade on purpose, because reminting it would invalidate the IDE
  // plugin's saved credential mid-session. What can go stale is a token
  // this tab kept from a machine whose config was replaced.
  async function resolveWritable() {
    try {
      const r = await fetch("/api/whoami", { headers: headers() });
      writable = r.ok ? !!(await r.json()).writable : false;
    } catch (_) {
      writable = false;
    }
    document.body.classList.toggle("read-only", !writable);
    return writable;
  }

  // ── Live updates ────────────────────────────────────────────────────
  // The daemon's frame vocabulary is fixed: connected, item-changed
  // {anchor, version}, document-changed {version}, thread-changed
  // {anchor, version}, thread-deleted {anchor}, event-acked {event_id},
  // heartbeat, session-ended.
  // document-changed is defined but not emitted by the daemon today, so it
  // is bound defensively and never depended on.
  //
  // onDelta contract: called as onDelta({kind, anchor, version, initial}).
  // `initial` is true for a delta that only echoes state a client can
  // already see from its own first fetch -- the stream's opening snapshot
  // reports every existing anchor so a client connecting LATE still gets
  // full state, but a client that just loaded the page does not need to
  // re-render on these. A client may safely skip initial deltas; it must
  // NOT skip non-initial ones, and a late-connecting client still needs the
  // initial batch to learn about anchors it has not fetched yet.
  let onDelta = () => {};
  let lastVersions = {};
  let es = null;
  let esErrorCount = 0;
  let pollTimer = null;
  let usingPoll = false;
  let reconnectTimer = null;
  let ended = false;
  // True once the first full-state read has completed, from whichever
  // transport gets there first (EventSource's "connected" always precedes
  // its own snapshot burst; a poll fallback that starts before EventSource
  // ever connects gets exactly one initial batch of its own). Once set, no
  // later delta from either transport is ever marked initial again -- a
  // change that arrives while switching transports is a real change.
  let firstSnapshotSeen = false;
  const pollIntervalMs = 1000;

  function frame(ev) {
    try { return JSON.parse(ev.data); } catch (_) { return {}; }
  }

  function startStream() {
    if (ended || usingPoll || es) return;
    es = new EventSource(BASE + "stream");
    es.addEventListener("connected", () => {
      esErrorCount = 0;
      stopPolling();
      firstSnapshotSeen = true;
    });
    es.addEventListener("heartbeat", () => { esErrorCount = 0; });
    es.addEventListener("item-changed", (ev) => {
      const d = frame(ev);
      lastVersions[d.anchor] = d.version;
      onDelta({ kind: "item", anchor: d.anchor, version: d.version, initial: !!d.initial });
    });
    es.addEventListener("document-changed", (ev) => {
      const d = frame(ev);
      onDelta({ kind: "document", anchor: null, version: d.version, initial: false });
    });
    es.addEventListener("thread-changed", (ev) => {
      const d = frame(ev);
      onDelta({ kind: "thread", anchor: d.anchor, version: d.version, initial: !!d.initial });
    });
    es.addEventListener("thread-deleted", (ev) => {
      const d = frame(ev);
      onDelta({ kind: "thread-deleted", anchor: d.anchor, version: 0, initial: false });
    });
    // The only frame that reports something OTHER than content moving: an
    // event was answered. A renderer that locks its page while a comment is
    // in flight needs this, because "answered, nothing needed changing" moves
    // no version and is otherwise indistinguishable from "still working".
    es.addEventListener("event-acked", (ev) => {
      const d = parse(ev);
      if (!d) return;
      onDelta({ kind: "event-acked", anchor: null, version: 0, initial: false,
                event_id: d.event_id });
    });
    es.addEventListener("session-ended", () => {
      ended = true;
      document.body.classList.add("session-finished");
      closeStream();
      stopPolling();
    });
    es.onerror = () => {
      if (ended) return;
      esErrorCount += 1;
      if (esErrorCount >= 2) {
        closeStream();
        startPolling();
        scheduleReconnect();
      }
    };
  }

  function closeStream() {
    if (es) { try { es.close(); } catch (_) {} es = null; }
  }

  // The connection an EventSource holds does not survive a laptop sleeping,
  // and unlike a lost network blip the browser does not always notice on
  // its own. A page that stops updating and never says so is worse than one
  // that falls back to polling and keeps retrying the stream underneath.
  function scheduleReconnect() {
    if (reconnectTimer || ended) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      esErrorCount = 0;
      startStream();
    }, 5000);
  }

  async function pollOnce() {
    try {
      const data = await api.fetchJSON("poll");
      // BOTH terminal states, not just finished. The SSE path ends on
      // session-ended, which the daemon emits for either one; this is the
      // path a client falls back to when SSE is broken, and it used to
      // ignore `cancelled` entirely -- so a cancelled session polled the
      // daemon once a second forever on exactly the transport a struggling
      // connection is already using.
      if (data.finished || data.cancelled) {
        ended = true;
        document.body.classList.add("session-finished");
        stopPolling();
        closeStream();
        return;
      }
      // Only the very first successful full-state read of the whole page
      // (from whichever transport gets there first) is a snapshot; see the
      // firstSnapshotSeen comment above init(). A poll that starts because
      // EventSource just dropped is reporting real changes, not a snapshot.
      const isInitial = !firstSnapshotSeen;
      firstSnapshotSeen = true;
      const items = data.items || {};
      const threads = data.threads || {};
      for (const anchor of Object.keys(items)) {
        if (lastVersions[anchor] !== items[anchor]) {
          lastVersions[anchor] = items[anchor];
          onDelta({ kind: "item", anchor, version: items[anchor], initial: isInitial });
        }
      }
      for (const anchor of Object.keys(threads)) {
        const key = "thread:" + anchor;
        if (lastVersions[key] !== threads[anchor]) {
          lastVersions[key] = threads[anchor];
          onDelta({ kind: "thread", anchor, version: threads[anchor], initial: isInitial });
        }
      }
    } catch (e) {
      console.warn("poll failed", e);
    }
  }

  function startPolling() {
    if (usingPoll || ended) return;
    usingPoll = true;
    pollOnce();
    pollTimer = setInterval(pollOnce, pollIntervalMs);
  }

  function stopPolling() {
    usingPoll = false;
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // ── Selection and composer ──────────────────────────────────────────
  // A client marks its commentable regions; the runtime owns what happens
  // when one is clicked. It never decides what the region CONTAINS -- on a
  // delta it hands the anchor back and the client re-fetches and redraws.
  let composerEl = null;

  function closeComposer() {
    if (composerEl) { composerEl.remove(); composerEl = null; }
  }

  function openComposer(anchor, anchorEl) {
    closeComposer();

    const panel = document.createElement("div");
    panel.className = "wc-composer";
    panel.setAttribute("data-wc-composer", anchor);

    const textarea = document.createElement("textarea");
    textarea.className = "wc-composer-text";
    textarea.placeholder = "Comment on this...";
    panel.appendChild(textarea);

    const images = [];
    textarea.addEventListener("paste", (ev) => {
      const clip = ev.clipboardData ? Array.from(ev.clipboardData.items) : [];
      for (const it of clip) {
        if (it.type && it.type.indexOf("image/") === 0) {
          ev.preventDefault();
          const blob = it.getAsFile();
          api.pasteImage(blob).then((ref) => images.push(ref))
            .catch((e) => console.warn("paste image failed", e));
        }
      }
    });

    const actions = document.createElement("div");
    actions.className = "wc-composer-actions";

    const submitBtn = document.createElement("button");
    submitBtn.type = "button";
    submitBtn.textContent = "Comment";
    submitBtn.addEventListener("click", async () => {
      const text = textarea.value.trim();
      if (!text) return;
      submitBtn.disabled = true;
      try {
        await api.submit({ anchor, text, images });
        closeComposer();
      } catch (e) {
        console.warn("submit failed", e);
        submitBtn.disabled = false;
      }
    });

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", closeComposer);

    actions.appendChild(submitBtn);
    actions.appendChild(cancelBtn);
    panel.appendChild(actions);

    (anchorEl.parentNode || document.body).insertBefore(panel, anchorEl.nextSibling);
    composerEl = panel;
    textarea.focus();
  }

  // Event delegation on a root, not a listener per element -- a client may
  // add regions after load, and this must still catch them.
  function bindSelection(root) {
    (root || document).addEventListener("click", (ev) => {
      if (composerEl && composerEl.contains(ev.target)) return;
      const el = ev.target.closest("[data-wc-anchor]");
      if (!el || !writable) return;
      openComposer(el.getAttribute("data-wc-anchor"), el);
    });
  }

  window.WebCompanion = {
    api,
    get writable() { return writable; },
    resolveWritable,
    init({ onDelta: handler, root } = {}) {
      onDelta = handler || (() => {});
      bindSelection(root);
      // Paint read-only before the first fetch so a reader never sees
      // controls appear and then vanish. Live updates do not wait on it --
      // reading is what a shared link is for.
      resolveWritable();
      startStream();
    },
  };
})();
