/* Siriously debug console. Plain JS, no dependencies, no network except this server. */
"use strict";

// ---- config -------------------------------------------------------------------------------------
const CONFIG = {
  // Energy per token. PrismML's published figure for 1-bit Bonsai 8B on an M4 Pro. Single place to change it.
  MWH_PER_TOKEN: 0.074,
  ENERGY_ASSUMPTION:
    "~0.074 mWh/token, PrismML's published figure for 1-bit Bonsai 8B on M4 Pro; rough estimate. " +
    "Tokens = prompt + completion from model usage; fast-path commands use no model.",
  HISTORY: 200, // events replayed on (re)connect (server max)
  MAX_CARDS: 40,
  BACKOFF_MS: [500, 1000, 2000, 4000, 8000],
  LIVE_MS_LIMIT: 15000, // stop the live counter after this (replayed or stuck utterances)
  EVAL_REFRESH_MS: 60000,
  GOOD: 0.85,
  WARN: 0.7,
};
const FAST_ROUTES = new Set(["control", "flow", "fastpath"]);
const MODEL_ROUTES = new Set(["planner", "dictation"]);

// ---- helpers ------------------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);

/** Build a DOM node. Text is always set as text, never HTML (heard text is untrusted). */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const fmtMs = (ms) => (ms == null ? "—" : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`);
const median = (xs) => {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const pct = (r) => (r == null ? "—" : `${Math.round(r * 100)}%`);
const tone = (r) => (r == null ? "" : r >= CONFIG.GOOD ? "t-good" : r >= CONFIG.WARN ? "t-warn" : "t-bad");

// Optional shared secret: open the page as /console#token=... and it is sent on every call.
const TOKEN = (() => {
  const m = /[#&]token=([^&]+)/.exec(location.hash);
  if (m) sessionStorage.setItem("siriously-token", decodeURIComponent(m[1]));
  return sessionStorage.getItem("siriously-token") || "";
})();
const authHeaders = () => (TOKEN ? { "X-Siriously-Token": TOKEN } : {});

// ---- human-readable actions ---------------------------------------------------------------------
const KEY_NAMES = {
  cmd: "⌘", command: "⌘", shift: "⇧", opt: "⌥", option: "⌥", alt: "⌥", ctrl: "⌃", control: "⌃",
  return: "↵", enter: "↵", esc: "⎋", escape: "⎋", tab: "⇥", delete: "⌫", backspace: "⌫", space: "Space",
  up: "↑", down: "↓", left: "←", right: "→",
};
function fmtKeys(keys) {
  const parts = String(keys).split("+").map((p) => p.trim().toLowerCase());
  const mods = parts.filter((p) => ["cmd", "command", "shift", "opt", "option", "alt", "ctrl", "control"].includes(p));
  const rest = parts.filter((p) => !mods.includes(p));
  const order = ["⌃", "⌥", "⇧", "⌘"];
  const glyphs = mods.map((m) => KEY_NAMES[m]).sort((a, b) => order.indexOf(a) - order.indexOf(b));
  return glyphs.join("") + rest.map((k) => KEY_NAMES[k] || (k.length === 1 ? k.toUpperCase() : k)).join("");
}

/** Label for a target id: from the plan's ask/labels (and an optional data.targets map) when known. */
function targetLabel(a, labels) {
  const direct = a.display || a.label || (Array.isArray(a.path) && a.path.length ? a.path.join(" › ") : "");
  if (direct) return direct;
  return a.target != null && labels.has(a.target) ? labels.get(a.target) : null;
}

/** Returns {op, body} where body is a string or a node. */
function describeAction(a, labels) {
  const t = targetLabel(a, labels);
  const tgt = t ? `“${t}”` : a.target != null ? `#${a.target}` : "";
  switch (a.op) {
    case "menu": return { op: "menu", body: tgt };
    case "press": return { op: "press", body: tgt };
    case "type": return { op: "type", body: [`“${a.text}”`, a.target != null ? ` in ${tgt}` : ""].join("") };
    case "keys": return { op: "keys", body: h("kbd", {}, fmtKeys(a.keys)) };
    case "open_app": return { op: "open", body: a.app };
    case "scroll": return { op: "scroll", body: `${a.direction}${a.amount ? ` ${a.amount}` : ""}` };
    case "wait": return { op: "wait", body: `${a.ms} ms` };
    default: return { op: String(a.op), body: JSON.stringify(a) };
  }
}

// ---- state --------------------------------------------------------------------------------------
const state = { utts: new Map(), order: [], banner: null };

function getUtt(id) {
  let u = state.utts.get(id);
  if (!u) {
    u = {
      id, text: null, speaker: null, rejected: null, route: null, planned: [], labels: new Map(),
      actions: [], confirm: null, ask: null, done: null, error: null,
      base: null, // performance.now() - ms_since_speech_end at first live event
      firstMs: null, usage: null, el: null, fresh: true,
    };
    state.utts.set(id, u);
    state.order.unshift(id);
  }
  return u;
}

function clearBanner() { state.banner = null; renderBanner(); }

function applyEvent(ev, live) {
  const d = ev.data || {};
  const u = getUtt(ev.utterance_id);
  if (u.base == null && live && ev.ms_since_speech_end > 0) u.base = performance.now() - ev.ms_since_speech_end;
  switch (ev.type) {
    case "heard":
      clearBanner(); // any new utterance consumes a pending confirm / question
      u.text = d.text ?? ""; u.speaker = d.speaker || null;
      break;
    case "rejected":
      clearBanner();
      u.rejected = { text: d.text ?? "", score: d.score };
      break;
    case "route": u.route = d; break;
    case "plan": {
      u.planned = (d.plan && d.plan.actions) || [];
      if (d.targets && typeof d.targets === "object") {
        for (const [k, v] of Object.entries(d.targets)) u.labels.set(Number(k), String(v));
      }
      if (Array.isArray(d.ask)) {
        d.ask.forEach((id, i) => u.labels.set(id, (d.labels || [])[i] || `#${id}`));
        u.ask = { ids: d.ask, labels: d.labels || [] };
        state.banner = { kind: "ask", ask: u.ask };
        renderBanner();
      }
      break;
    }
    case "confirm":
      u.confirm = { reason: d.reason || "", plan: d.plan };
      state.banner = { kind: "confirm", reason: d.reason || "this action", plan: d.plan };
      renderBanner();
      break;
    case "action":
      u.actions.push({ i: d.i, action: d.action || {}, ok: !!d.ok, error: d.error });
      if (u.firstMs == null) u.firstMs = ev.ms_since_speech_end;
      u.confirm = null;
      break;
    case "done":
      u.done = d; u.ms = ev.ms_since_speech_end; u.usage = d.usage || null;
      clearBanner();
      break;
    case "error":
      u.error = d.message || "error";
      clearBanner();
      break;
    default: return;
  }
  schedule(u);
}

// ---- rendering ----------------------------------------------------------------------------------
const pending = new Set();
let raf = 0;
function schedule(u) {
  pending.add(u);
  if (!raf) raf = requestAnimationFrame(flush);
}
function flush() {
  raf = 0;
  for (const u of pending) renderCard(u);
  pending.clear();
  trimCards();
  renderStats();
}

function status(u) {
  if (u.rejected) return "rejected";
  if (u.error) return "error";
  if (u.done) return "done";
  if (u.confirm || u.ask) return "pending";
  return "run";
}

function renderCard(u) {
  const st = status(u);
  const first = !u.el;
  if (first) {
    u.el = h("li", { class: "card" });
    $("tl-empty")?.remove();
    const tl = $("timeline");
    const idx = state.order.indexOf(u.id);
    tl.insertBefore(u.el, tl.children[idx] || null);
  }
  u.el.className = `card s-${st}${first && u.fresh ? " fresh" : ""}`;
  const kids = [];

  // top row: id, speaker, route chip
  const top = [h("span", {}, u.id)];
  if (u.rejected) {
    top.push(h("span", { class: "chip r-rejected" }, "Rejected"));
  } else {
    if (u.speaker) {
      top.push(u.speaker.verified
        ? h("span", { class: "verdict ok" }, `✓ verified ${u.speaker.score != null ? Number(u.speaker.score).toFixed(2) : ""}`)
        : h("span", { class: "verdict bad" }, "✗ not verified"));
    }
    if (u.route) {
      const kind = u.route.kind || "none";
      const ms = typeof u.route.ms === "number" ? fmtMs(u.route.ms < 10 ? Math.round(u.route.ms * 10) / 10 : u.route.ms) : "";
      top.push(h("span", { class: `chip r-${kind}` }, kind, ms ? h("i", {}, `decided in ${ms}`) : null));
      if (u.route.control) top.push(h("span", {}, `control: ${u.route.control}`));
      if (u.route.flow) top.push(h("span", {}, `flow: ${u.route.flow}`));
    }
  }
  kids.push(h("div", { class: "card-top" }, top));

  // what was heard
  if (u.rejected) {
    kids.push(h("p", { class: "heard dim" }, `“${u.rejected.text}”`));
    kids.push(h("p", { class: "verdict bad" },
      `✗ REJECTED: voice not enrolled — ignored${u.rejected.score != null ? ` (score ${Number(u.rejected.score).toFixed(2)})` : ""}`));
  } else {
    kids.push(h("p", { class: u.text ? "heard" : "heard dim" }, u.text != null ? `“${u.text}”` : "(no transcript for this event)"));
  }

  // actions
  const rows = u.actions.map((r) => {
    const { op, body } = describeAction(r.action, u.labels);
    return h("li", { class: `act ${r.ok ? "ok" : "fail"}` },
      h("span", { class: "mark" }, r.ok ? "✓ OK" : "✗ FAIL"),
      h("span", { class: "op" }, op), h("span", {}, body),
      r.ok ? null : h("span", { class: "err" }, r.error || ""));
  });
  if (!u.done && !u.error && !u.rejected && !u.ask) {
    for (const a of u.planned.slice(u.actions.length)) {
      const { op, body } = describeAction(a, u.labels);
      rows.push(h("li", { class: "act todo" }, h("span", { class: "mark" }, "…"), h("span", { class: "op" }, op), h("span", {}, body)));
    }
  }
  if (rows.length) kids.push(h("ul", { class: "actions", "aria-label": "Actions" }, rows));
  if (u.ask) {
    kids.push(h("p", { class: "note" }, `Asked: ${u.ask.labels.map((l, i) => `${i + 1}. ${l}`).join("   ")}`));
  }

  // footer: live ms, outcome
  const foot = [];
  if (u.firstMs != null) {
    foot.push(h("span", { class: "live-ms", title: "End of speech to first action" }, `first action ${fmtMs(u.firstMs)}`));
  } else if (st === "run" && u.base != null) {
    foot.push(h("span", { class: "live-ms", "data-live": u.id, title: "Since end of speech" }, "0 ms"));
  }
  if (u.done && u.done.saved) {
    // teach ("call that standup"): say what was learned, not "0 actions"
    const steps = u.done.n_steps != null ? ` · ${u.done.n_steps} step(s)` : "";
    foot.push(h("span", { class: "outcome done" }, `SAVED flow “${u.done.saved}”${steps} · ${fmtMs(u.ms)}`));
  } else if (u.done) {
    foot.push(h("span", { class: "outcome done" }, `DONE · ${u.done.n_actions ?? u.actions.length} action(s) · ${fmtMs(u.ms)} total`));
  }
  if (u.error) foot.push(h("span", { class: "outcome err" }, `✗ ERROR: ${u.error}`));
  if (st === "pending") foot.push(h("span", { class: "outcome wait" }, "waiting for your answer"));
  if (u.usage) foot.push(h("span", { class: "note" }, `model: ${u.usage.calls ?? "?"} call(s), ${(u.usage.prompt_tokens ?? 0) + (u.usage.completion_tokens ?? 0)} tokens`));
  if (foot.length) kids.push(h("div", { class: "foot" }, foot));
  u.el.replaceChildren(...kids);
}

function trimCards() {
  while (state.order.length > CONFIG.MAX_CARDS) {
    const id = state.order.pop();
    state.utts.get(id)?.el?.remove();
    state.utts.delete(id);
  }
}

function renderBanner() {
  const b = $("banner");
  const s = state.banner;
  if (!s) { b.hidden = true; b.replaceChildren(); return; }
  b.hidden = false;
  if (s.kind === "confirm") {
    b.replaceChildren(`Say “yes” to confirm: ${s.reason}`, h("span", { class: "sub" }, "or say “no” / “cancel” to drop it"));
    return;
  }
  const items = s.ask.labels.map((l, i) => h("li", {}, h("b", {}, i + 1), l || `#${s.ask.ids[i]}`));
  b.replaceChildren("Which one?", h("span", { class: "sub" }, "Say a number or “the second one”"), h("ol", {}, items));
}

// ---- stats --------------------------------------------------------------------------------------
function renderStats() {
  const all = state.order.map((id) => state.utts.get(id)).filter(Boolean);
  const handled = all.filter((u) => u.done);
  $("st-handled").textContent = String(handled.length);

  let fast = 0, model = 0;
  const byRoute = new Map();
  for (const u of handled) {
    const kind = (u.route && u.route.kind) || (u.done && u.done.route);
    if (FAST_ROUTES.has(kind)) fast++; else if (MODEL_ROUTES.has(kind)) model++;
  }
  for (const u of all) {
    const kind = u.route && u.route.kind;
    if (kind && u.firstMs != null) (byRoute.get(kind) || byRoute.set(kind, []).get(kind)).push(u.firstMs);
  }
  $("st-fast").textContent = fast + model ? pct(fast / (fast + model)) : "—";
  $("st-fast-sub").textContent = fast + model ? `${fast} fast · ${model} model` : "no data yet";

  const lat = $("st-latency");
  if (!byRoute.size) lat.replaceChildren(h("li", { class: "muted" }, "no actions yet"));
  else lat.replaceChildren(...[...byRoute].map(([k, xs]) => h("li", { class: `r-${k}` }, fmtMs(median(xs)), h("span", {}, `${k} (n=${xs.length})`))));

  let tokens = 0, cached = 0, calls = 0, seen = 0;
  for (const u of all) {
    if (!u.usage) continue;
    seen++;
    tokens += (u.usage.prompt_tokens || 0) + (u.usage.completion_tokens || 0);
    cached += u.usage.cached_tokens || 0;
    calls += u.usage.calls || 0;
  }
  if (seen) {
    const mwh = tokens * CONFIG.MWH_PER_TOKEN;
    $("st-energy").textContent = mwh >= 1000 ? `${(mwh / 1000).toFixed(2)} Wh` : `${mwh.toFixed(mwh < 10 ? 2 : 0)} mWh`;
    $("st-energy-sub").textContent = `${tokens} tokens · ${cached} cached · ${calls} call(s)`;
  } else {
    $("st-energy").textContent = "—";
    $("st-energy-sub").textContent = handled.length ? "no model usage reported" : "no model usage yet";
  }
}

function tickLive() {
  const now = performance.now();
  for (const el of document.querySelectorAll("[data-live]")) {
    const u = state.utts.get(el.getAttribute("data-live"));
    const ms = u && u.base != null ? now - u.base : null;
    el.textContent = ms == null || ms > CONFIG.LIVE_MS_LIMIT ? "—" : fmtMs(ms);
  }
}

// ---- websocket ----------------------------------------------------------------------------------
let attempt = 0;
let ws = null;
function setConn(kind, text) {
  $("conn").className = `conn conn-${kind}`;
  $("conn-text").textContent = text;
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = `history=${CONFIG.HISTORY}${TOKEN ? `&token=${encodeURIComponent(TOKEN)}` : ""}`;
  ws = new WebSocket(`${proto}://${location.host}/events?${q}`);
  let replaying = true;
  ws.onopen = () => {
    attempt = 0;
    setConn("up", "LIVE");
    resetTimeline(); // the server replays history; start clean so nothing doubles
    setTimeout(() => { replaying = false; }, 400);
  };
  ws.onmessage = (m) => {
    try { applyEvent(JSON.parse(m.data), !replaying); } catch (e) { console.error("bad event", e); }
  };
  ws.onclose = () => {
    const delay = CONFIG.BACKOFF_MS[Math.min(attempt, CONFIG.BACKOFF_MS.length - 1)];
    attempt++;
    setConn("wait", `Reconnecting in ${Math.ceil(delay / 1000)} s…`);
    setTimeout(connect, delay);
  };
  ws.onerror = () => ws.close();
}

function resetTimeline() {
  state.utts.clear(); state.order = []; state.banner = null;
  const tl = $("timeline");
  tl.replaceChildren(h("li", { id: "tl-empty", class: "empty" }, "Waiting for a command. Speak, or type one above."));
  renderBanner(); renderStats();
}

// ---- command box --------------------------------------------------------------------------------
async function send(text, verified) {
  const msg = $("cmd-msg");
  msg.className = "cmd-msg";
  try {
    const r = await fetch("/text", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ text, verified }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${r.status}`);
    }
    msg.textContent = `Sent “${text}”${verified ? "" : " as an unverified voice"}.`;
    return true;
  } catch (e) {
    msg.className = "cmd-msg bad";
    msg.textContent = `Could not send: ${e.message}`;
    return false;
  }
}

function initCommandBox() {
  const input = $("cmd-input");
  $("cmd-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    const btn = e.submitter;
    if (btn) btn.disabled = true;
    if (await send(text, $("verified").checked)) input.value = "";
    if (btn) btn.disabled = false;
    input.focus();
  });
  $("stop-btn").addEventListener("click", () => send("stop", true));
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) {
      e.preventDefault(); input.focus();
    }
  });
}

// ---- eval scoreboard ----------------------------------------------------------------------------
function modeBlock(name, m) {
  const splits = Object.entries(m.split || {}).map(([k, s]) => h("span", { class: tone(s.rate) }, `${k} `, h("b", {}, pct(s.rate))));
  const lat = Object.entries(m.latency || {}).map(([r, s]) => `${r} ${fmtMs(s.median_ms)}`).join(" · ");
  return h("div", { class: "ev-mode" },
    h("div", { class: "ev-name" }, name === "e2e" ? "End to end" : name === "router" ? "Router only" : name),
    h("div", { class: `ev-rate ${tone(m.rate)}` }, pct(m.rate), h("small", {}, `${m.pass}/${m.total} passed${m.skip ? `, ${m.skip} skipped` : ""}`)),
    splits.length ? h("div", { class: "ev-splits" }, splits) : null,
    lat ? h("div", { class: "ev-meta" }, `${m.latency_label || "latency"}: ${lat}`) : null);
}

async function loadEval() {
  const box = $("eval");
  try {
    const r = await fetch("/eval/latest", { headers: authHeaders() });
    const body = await r.json();
    if (!r.ok) { box.replaceChildren(h("p", { class: "muted" }, "No eval results yet.")); return; }
    const modes = body.modes || {};
    const names = ["e2e", "router", ...Object.keys(modes).filter((k) => k !== "e2e" && k !== "router")].filter((k) => modes[k]);
    box.replaceChildren(...names.map((n) => modeBlock(n, modes[n])),
      h("p", { class: "ev-meta" }, `Run ${body.timestamp || "?"} · green ≥ ${CONFIG.GOOD * 100}%, amber ≥ ${CONFIG.WARN * 100}%`));
  } catch (e) {
    box.replaceChildren(h("p", { class: "muted" }, "Eval scoreboard unavailable."));
  }
}

// ---- boot ---------------------------------------------------------------------------------------
$("energy-tip").textContent = CONFIG.ENERGY_ASSUMPTION;
initCommandBox();
$("ev-refresh").addEventListener("click", loadEval);
loadEval();
setInterval(loadEval, CONFIG.EVAL_REFRESH_MS);
setInterval(tickLive, 100);
connect();
