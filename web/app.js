(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const pct = (x, d = 0) => (x * 100).toFixed(d) + "%";
const mmss = t => { t = Math.max(0, Math.floor(t)); return Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0"); };
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let S = null;                    // last state from the server
let failures = 0;
const expanded = new Set();
let onlyCuts = false;
let draggingRate = false, rateTimer = null;

// ---------------------------------------------------------------- talking to the server
function send(action, extra = {}) {
  return fetch("/api/control", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, ...extra }) })
    .catch(() => { failures++; });
}
async function poll() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    S = await r.json();
    failures = 0;
    if (S && S.t !== undefined) render();
  } catch (e) { failures++; }
  banner();
  setTimeout(poll, 400);
}
function banner() {
  const b = $("#banner");
  let msg = "";
  if (failures >= 3) msg = "Lost contact with the simulation server. Check that python -m smartdialer.server is still running.";
  else if (S && S.error) msg = "The simulation stopped after an error and is paused. Details: " + S.error.trim().split("\n").pop();
  else if (S && S.violations && S.violations.length) msg = "Invariant broken: " + S.violations[0];
  b.hidden = !msg; b.textContent = msg;
}

// ---------------------------------------------------------------- static controls
function bindControls() {
  $("#play").addEventListener("click", () => send(S && S.running ? "pause" : "play"));
  document.addEventListener("keydown", e => {
    if (e.code === "Space" && !/INPUT|BUTTON|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); $("#play").click(); }
  });
  $("#speed").addEventListener("click", e => { const b = e.target.closest("button"); if (b) send("speed", { value: +b.dataset.v }); });
  $("#mode").addEventListener("click", e => { const b = e.target.closest("button"); if (b) send("mode", { value: b.dataset.v }); });
  $$("[data-act]").forEach(b => b.addEventListener("click", () => {
    const x = { act: b.dataset.act };
    const extra = {};
    if (b.dataset.v) extra.value = +b.dataset.v;
    if (b.dataset.point) extra.point = b.dataset.point;
    send(x.act, extra);
  }));
  $("#t-stress").addEventListener("change", e => send("stress", { on: e.target.checked }));
  $("#t-messy").addEventListener("change", e => send("messy", { on: e.target.checked }));
  $("#reset").addEventListener("click", () => {
    const a = Math.max(5, Math.min(2000, +$("#n-agents").value || 100));
    const w = Math.max(1, Math.min(6, +$("#n-workers").value || 2));
    $("#n-agents").value = a; $("#n-workers").value = w;
    send("reset", { agents: a, workers: w });
  });
  const pushRates = () => {
    clearTimeout(rateTimer);
    rateTimer = setTimeout(() => send("rates", { answer_rate: +$("#s-ans").value / 100, talk_mean_s: +$("#s-talk").value }), 120);
  };
  for (const id of ["#s-ans", "#s-talk"]) {
    const el = $(id);
    el.addEventListener("pointerdown", () => draggingRate = true);
    el.addEventListener("input", () => { draggingRate = true; syncRateLabels(); pushRates(); });
    el.addEventListener("change", () => setTimeout(() => draggingRate = false, 600));
  }
  window.addEventListener("pointerup", () => setTimeout(() => draggingRate = false, 600));
  $("#only-cut").addEventListener("change", e => { onlyCuts = e.target.checked; lastLedgerKey = ""; renderLedger(); });
}
function syncRateLabels() {
  $("#o-ans").textContent = $("#s-ans").value + "%";
  $("#o-talk").textContent = $("#s-talk").value + " s";
}

// ---------------------------------------------------------------- render
const STATES = [
  ["AVAILABLE", "Free", "--s-available"], ["RESERVED", "Picked", "--s-reserved"], ["DIALING", "Dialing", "--s-dialing"],
  ["CONNECTED", "On a call", "--s-connected"], ["WRAP_UP", "Wrap-up", "--s-wrap"], ["PAUSED", "Paused", "--s-paused"], ["OFFLINE", "Offline", "--s-offline"],
];
function render() {
  const s = S;
  $("#clock").textContent = mmss(s.t);
  $("#play").classList.toggle("is-running", s.running);
  $("#play").setAttribute("aria-label", s.running ? "Pause" : "Resume");
  for (const b of $$("#speed button")) b.setAttribute("aria-pressed", String(Math.abs(+b.dataset.v - s.speed) < 0.01));
  for (const b of $$("#mode button")) b.setAttribute("aria-pressed", String(b.dataset.v === s.campaign.mode));
  renderGate();
  renderScenarios();
  renderRates();
  $("#agent-count").textContent = s.now.online + " online";
  renderProviders();
  renderWorkers();
  renderKpis();
  renderLegend();
  drawFloor();
  renderCharts();
  renderLedger();
  renderLog();
  const e = s.events || {};
  const names = { APPLIED: "applied", DUPLICATE: "duplicates dropped", STALE: "stale ignored", IGNORED_TERMINAL: "late for a finished call", APPLIED_EARLY: "arrived before the answer", GHOST_HANGUP: "ghost calls hung up", UNKNOWN_CALL: "unknown call" };
  $("#disp").textContent = Object.keys(e).length ? "Provider events: " + Object.keys(e).sort((a, b) => e[b] - e[a]).map(k => `${e[k]} ${names[k] || k.toLowerCase()}`).join(", ") : "";
}
function renderGate() {
  const g = $("#gate"), s = S.safety, mode = S.campaign.mode;
  let cls = "ok", txt;
  if (S.campaign.status !== "RUNNING") { cls = "stop"; txt = "Campaign paused: nothing will be dialed"; }
  else if (mode === "PROGRESSIVE") { cls = "ok"; txt = "Safety controller: one call per free agent, every call tied to its agent"; }
  else if (s.shock_left > 0) { cls = "hold"; txt = `Safety controller: progressive only for ${Math.ceil(s.shock_left)} s, agents dropped sharply`; }
  else if (s.fallback_left > 0 || (!s.learning && s.abandon_rate >= s.limit && s.answered_window >= 30)) { cls = "stop"; txt = `Safety controller: progressive only, abandon rate ${pct(s.abandon_rate, 1)} reached the ${pct(s.limit)} limit`; }
  else if (s.learning) { cls = "hold"; txt = `Safety controller: progressive while learning the answer rate (${s.samples}/${s.need} calls)`; }
  else { cls = "ok"; txt = "Safety controller: predictive dialing allowed"; }
  const open = Object.entries(S.providers).filter(([, p]) => p.health.state === "OPEN").map(([n]) => n);
  if (open.length === Object.keys(S.providers).length) { cls = "stop"; txt = "Safety controller: every provider is down, dialing stopped"; }
  g.className = "gate " + cls; g.textContent = txt;
}
function renderScenarios() {
  const box = $("#scenarios");
  if (!box.children.length) {
    for (const [k, v] of Object.entries(S.scenarios)) {
      const b = document.createElement("button");
      b.dataset.k = k;
      b.innerHTML = `<b>${k}. ${esc(v.title)}</b><span>${esc(v.note)}</span>`;
      b.addEventListener("click", () => send("scenario", { value: k }));
      box.appendChild(b);
    }
  }
  const r = S.rates, sc = S.scenarios[S.scenario];
  const matches = r.scripted || (sc && sc.answer !== null && Math.abs(sc.answer - r.answer) < 0.006 && Math.abs(sc.talk - r.talk) < 1);
  for (const b of box.children) b.setAttribute("aria-pressed", String(matches && b.dataset.k === S.scenario));
  $("#scn-note").textContent = r.scripted ? "Scenario D changes by itself. Moving a slider replaces it with fixed values." : (matches ? "" : "Custom values.");
}
function renderRates() {
  if (draggingRate) return;
  $("#s-ans").value = Math.round(S.rates.answer * 100);
  $("#s-talk").value = Math.round(S.rates.talk);
  syncRateLabels();
}
function renderProviders() {
  const box = $("#providers");
  const info = { A: "Fast and reliable", B: "Slower, times out, duplicates and reorders events" };
  for (const [n, p] of Object.entries(S.providers)) {
    let el = box.querySelector(`[data-p="${n}"]`);
    if (!el) {
      el = document.createElement("div"); el.className = "prov"; el.dataset.p = n;
      el.innerHTML = `<div class="top"><b>Provider ${n}</b><span class="pill state"></span></div><small>${info[n] || ""}</small><div class="st muted"></div><button class="tgl"></button>`;
      el.querySelector(".tgl").addEventListener("click", () => send("outage", { provider: n, on: !S.providers[n].outage }));
      box.appendChild(el);
    }
    const pill = el.querySelector(".state");
    pill.className = "pill state " + p.health.state;
    pill.textContent = ({ CLOSED: "Healthy", OPEN: "Circuit open", HALF_OPEN: "Probing" })[p.health.state] || p.health.state;
    const st = p.stats;
    el.querySelector(".st").textContent = `${st.placed} placed, ${st.timeouts} timeouts, ${st.dups} repeats, ${st.reordered} late`;
    el.querySelector(".tgl").textContent = p.outage ? "Bring back online" : "Take offline";
  }
}
function renderWorkers() {
  $("#workers").innerHTML = S.workers.map(w => `<span class="${w.alive ? "" : "dead"}"><i></i>${esc(w.id)}</span>`).join("");
}
function renderKpis() {
  const rows = S.series, last = rows[rows.length - 1] || [];
  const win = rows.filter(r => r[0] >= (last[0] || 0) - 60);
  const util = win.length ? win.reduce((a, r) => a + r[1], 0) / win.length : 0;
  const occ = win.length ? win.reduce((a, r) => a + r[2], 0) / win.length : 0;
  const sf = S.safety;
  const back = rows.find(r => r[0] >= (last[0] || 0) - 120) || rows[0] || last;
  const dials = last.length ? (last[11] - back[11]) : 0, conn = last.length ? (last[12] - back[12]) : 0;
  const ab = sf.abandon_rate, hot = ab >= sf.limit ? "hot" : ab >= sf.limit * 0.5 ? "warm" : "";
  const inflight = last[3] || 0, online = S.now.online || 1;
  $("#kpis").innerHTML = `
    <div class="kpi"><div class="n">${pct(util)}</div><div class="l">of agent time on calls</div><div class="s">${pct(occ)} counting wrap-up, last minute</div></div>
    <div class="kpi"><div class="n">${pct(ab, 1)}</div><div class="l">of answered calls abandoned</div>
      <div class="meter ${hot}"><i style="width:${Math.min(100, ab / (sf.limit * 2) * 100)}%"></i><b style="left:50%" title="limit"></b></div>
      <div class="s">limit ${pct(sf.limit)}, ${sf.abandoned_window} of ${sf.answered_window} in the last 5 min</div></div>
    <div class="kpi"><div class="n">${inflight}</div><div class="l">calls ringing</div><div class="s">${(inflight / online).toFixed(1)} per agent online, ${S.now.held} answered waiting</div></div>
    <div class="kpi"><div class="n">${conn > 0 ? (dials / conn).toFixed(1) : "–"}</div><div class="l">dials per conversation</div><div class="s">last 2 minutes, ${S.totals.initiated || 0} dials in total</div></div>`;
}
function renderLegend() {
  const c = S.counts;
  $("#legend").innerHTML = STATES.filter(([k]) => k !== "OFFLINE" || c.OFFLINE)
    .map(([k, label, v]) => `<span><i style="background:var(${v});${k === "OFFLINE" ? "border:1px solid var(--rule)" : ""}"></i>${label} <em>${c[k] || 0}</em></span>`).join("");
}

// ---------------------------------------------------------------- the floor
const floor = { cells: [], cell: 0, g: 0, podW: 0, podH: 0, perRow: 1 };
function drawFloor() {
  const cv = $("#floor"), W = cv.clientWidth, agents = S.agents, n = agents.length;
  const pods = Math.ceil(n / 10);
  let cell = 22, g = 3, podGap = 10, perRow = 1, H = 0;
  for (cell = 24; cell >= 4; cell--) {
    g = Math.max(1, Math.round(cell * 0.16)); podGap = Math.max(4, Math.round(cell * 0.5));
    const pw = 5 * cell + 4 * g + podGap, ph = 2 * cell + g + podGap;
    perRow = Math.max(1, Math.floor((W + podGap) / pw));
    H = Math.ceil(pods / perRow) * ph;
    if (H <= 300 || cell === 4) break;
  }
  const dpr = window.devicePixelRatio || 1;
  const pw = 5 * cell + 4 * g + podGap, ph = 2 * cell + g + podGap;
  cv.style.height = Math.max(H - podGap, 20) + "px";
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round((H - podGap) * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round((H - podGap) * dpr); }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const colors = {}; for (const [k, , v] of STATES) colors[k] = css(v);
  const rule = css("--rule"), ink = css("--ink");
  const ring = new Set(S.ringing);
  floor.cell = cell; floor.g = g; floor.podW = pw; floor.podH = ph; floor.perRow = perRow; floor.n = n;
  const r = cell > 8 ? 2 : 1;
  for (let i = 0; i < n; i++) {
    const pod = Math.floor(i / 10), k = i % 10, col = k % 5, row = Math.floor(k / 5);
    const x = (pod % perRow) * pw + col * (cell + g), y = Math.floor(pod / perRow) * ph + row * (cell + g);
    const st = agents[i][1];
    ctx.fillStyle = colors[st] || "#fff";
    roundRect(ctx, x, y, cell, cell, r); ctx.fill();
    if (st === "OFFLINE") { ctx.strokeStyle = rule; ctx.lineWidth = 1; roundRect(ctx, x + .5, y + .5, cell - 1, cell - 1, r); ctx.stroke(); }
    if (ring.has(agents[i][0]) && cell >= 8) { ctx.fillStyle = ink; ctx.beginPath(); ctx.arc(x + cell - 3.5, y + 3.5, Math.max(1.2, cell * .1), 0, 6.3); ctx.fill(); }
  }
}
function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}
function bindFloorHover() {
  const cv = $("#floor"), tip = $("#tip");
  cv.addEventListener("mousemove", e => {
    if (!S) return;
    const b = cv.getBoundingClientRect(), x = e.clientX - b.left, y = e.clientY - b.top, f = floor;
    const pc = Math.floor(x / f.podW), pr = Math.floor(y / f.podH);
    const inx = x - pc * f.podW, iny = y - pr * f.podH, stepw = f.cell + f.g;
    const col = Math.floor(inx / stepw), row = Math.floor(iny / stepw);
    const idx = (pr * f.perRow + pc) * 10 + row * 5 + col;
    if (pc >= f.perRow || col > 4 || row > 1 || idx >= S.agents.length || idx < 0 || inx > 5 * stepw || iny > 2 * stepw) { tip.hidden = true; return; }
    const [id, st] = S.agents[idx];
    const label = (STATES.find(s => s[0] === st) || [, st])[1];
    tip.textContent = `${id}: ${label}${S.ringing.includes(id) ? ", call ringing" : ""}`;
    tip.style.left = (pc * f.podW + col * stepw + f.cell / 2) + "px"; tip.style.top = (pr * f.podH + row * stepw) + "px"; tip.hidden = false;
  });
  cv.addEventListener("mouseleave", () => tip.hidden = true);
}

// ---------------------------------------------------------------- charts
const hover = {};
const CH = {
  "c-util": { vid: "v-util", cols: [[1, "--s-connected", "on calls"], [2, "--s-wrap", "with wrap-up", true]], y: [0, 1], fmt: v => pct(v) },
  "c-ab": { vid: "v-ab", cols: [[5, "--bad", "abandoned"]], y: [0, null, 0.05], limit: 0.03, fmt: v => pct(v, 1) },
  "c-fl": { vid: "v-fl", cols: [[3, "--s-dialing", "ringing"], [4, "--good", "agents free"], [10, "--bad", "answered, waiting"]], y: [0, null, 10], fmt: v => Math.round(v) },
  "c-p": { vid: "v-p", cols: [[6, "--ink", "real"], [7, "--accent", "estimated"], [8, "--accent", "planned with", true]], y: [0, 1], fmt: v => pct(v) },
};
function renderCharts() {
  const rows = S.series.slice(-240);
  for (const [id, cfg] of Object.entries(CH)) drawChart(id, cfg, rows);
}
function drawChart(id, cfg, rows) {
  const svg = $("#" + id), W = svg.clientWidth || 400, H = 150, m = { l: 34, r: 8, t: 6, b: 16 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (rows.length < 2) { svg.innerHTML = ""; return; }
  const t0 = rows[0][0], t1 = rows[rows.length - 1][0] || t0 + 1;
  let [lo, hi, floorMax] = cfg.y;
  if (hi === null) { hi = Math.max(floorMax || 1, ...rows.flatMap(r => cfg.cols.map(c => r[c[0]]))) * 1.15; if (cfg.limit) hi = Math.max(hi, cfg.limit * 1.4); }
  const X = t => m.l + (t - t0) / Math.max(1, t1 - t0) * iw, Y = v => m.t + ih - (v - lo) / (hi - lo || 1) * ih;
  let out = "";
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4, y = Y(v);
    out += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}"/><text x="${m.l - 5}" y="${y + 3.5}" text-anchor="end">${cfg.fmt(v)}</text>`;
  }
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) { const t = t0 + (t1 - t0) * i / ticks; out += `<text x="${X(t)}" y="${H - 3}" text-anchor="${i === 0 ? "start" : i === ticks ? "end" : "middle"}">${mmss(t)}</text>`; }
  if (cfg.limit !== undefined) out += `<line class="limit" x1="${m.l}" x2="${W - m.r}" y1="${Y(cfg.limit)}" y2="${Y(cfg.limit)}"/><text x="${W - m.r}" y="${Y(cfg.limit) - 3}" text-anchor="end" style="fill:var(--bad)">limit ${pct(cfg.limit)}</text>`;
  for (const [ci, color, , dashed] of cfg.cols) {
    let d = "";
    rows.forEach((r, i) => { d += (i ? "L" : "M") + X(r[0]).toFixed(1) + " " + Y(r[ci]).toFixed(1); });
    out += `<path d="${d}" fill="none" stroke="var(${color})" stroke-width="1.8" ${dashed ? 'stroke-dasharray="4 3"' : ""} stroke-linejoin="round"/>`;
  }
  const ht = hover[id];
  let sel = rows[rows.length - 1];
  if (ht !== undefined) {
    sel = rows.reduce((a, r) => Math.abs(r[0] - ht) < Math.abs(a[0] - ht) ? r : a, rows[0]);
    out += `<line class="cross" x1="${X(sel[0])}" x2="${X(sel[0])}" y1="${m.t}" y2="${m.t + ih}"/>`;
    for (const [ci, color] of cfg.cols) out += `<circle cx="${X(sel[0])}" cy="${Y(sel[ci])}" r="3" fill="var(${color})"/>`;
  }
  svg.innerHTML = out;
  $("#" + cfg.vid).textContent = (ht !== undefined ? mmss(sel[0]) + "  " : "") + cfg.cols.map(c => `${c[2]} ${cfg.fmt(sel[c[0]])}`).join("   ");
  svg._geom = { m, iw, t0, t1 };
}
function bindChartHover() {
  for (const id of Object.keys(CH)) {
    const svg = $("#" + id);
    svg.addEventListener("mousemove", e => {
      const g = svg._geom; if (!g) return;
      const b = svg.getBoundingClientRect(), x = (e.clientX - b.left) * (svg.viewBox.baseVal.width / b.width);
      hover[id] = g.t0 + Math.max(0, Math.min(1, (x - g.m.l) / g.iw)) * (g.t1 - g.t0);
      if (S) renderCharts();
    });
    svg.addEventListener("mouseleave", () => { delete hover[id]; if (S) renderCharts(); });
  }
}

// ---------------------------------------------------------------- decision ledger
let lastLedgerKey = "";
function headline(d) {
  switch (d.verdict) {
    case "APPROVE": return `Started ${d.approved} call${d.approved === 1 ? "" : "s"}`;
    case "REDUCE": return `Asked for ${d.requested}, allowed ${d.approved}`;
    case "REJECT": return `Asked for ${d.requested}, refused`;
    case "FALLBACK_PROGRESSIVE": return `Asked for ${d.requested}, fell back to ${d.approved} progressive`;
    default: return d.verdict;
  }
}
const CHIP = { APPROVE: "Approved", REDUCE: "Cut", REJECT: "Refused", FALLBACK_PROGRESSIVE: "Progressive" };
function renderLedger() {
  const list = S.decisions.filter(d => !onlyCuts || d.verdict !== "APPROVE");
  const key = list.map(d => d.id).join(",") + "|" + [...expanded].join(",") + "|" + onlyCuts;
  if (key === lastLedgerKey) return;
  lastLedgerKey = key;
  const ol = $("#ledger"), top = ol.scrollTop;
  if (!list.length) { ol.innerHTML = `<li class="empty">${onlyCuts ? "Nothing has been cut recently." : "No dialing decisions yet. Press play."}</li>`; return; }
  ol.innerHTML = list.map(d => {
    const open = expanded.has(d.id);
    const text = d.text || "";
    let rules = "";
    if (open) {
      rules = `<table class="why">` + d.rules.map(r => `<tr class="${r.effect !== "PASS" ? "cut" : ""}"><td>${esc(r.rule)}</td><td>${r.effect !== "PASS" ? esc(r.effect.toLowerCase()) + ": " : ""}${esc(r.detail)}</td></tr>`).join("") + `</table>`;
    }
    return `<li><button class="hd" data-id="${d.id}" aria-expanded="${open}"><span class="t">${mmss(d.t)}</span><span class="what">${esc(headline(d))}</span><span class="chip ${d.verdict}">${CHIP[d.verdict] || d.verdict}</span></button>
      ${text ? `<p>${esc(text)}</p>` : ""}${rules}</li>`;
  }).join("");
  ol.scrollTop = top;
}
function bindLedger() {
  $("#ledger").addEventListener("click", e => {
    const b = e.target.closest(".hd"); if (!b) return;
    const id = +b.dataset.id;
    expanded.has(id) ? expanded.delete(id) : expanded.add(id);
    lastLedgerKey = ""; renderLedger();
  });
}

// ---------------------------------------------------------------- event log
let lastLog = "";
function renderLog() {
  const key = S.log.map(l => l.t + l.text).join("|");
  if (key === lastLog) return; lastLog = key;
  const ol = $("#log"), atEnd = ol.scrollTop + ol.clientHeight >= ol.scrollHeight - 8;
  ol.innerHTML = S.log.map(l => `<li class="${esc(l.kind)}"><span class="t">${mmss(l.t)}</span><span>${esc(l.text)}</span></li>`).join("");
  if (atEnd) ol.scrollTop = ol.scrollHeight;
}

bindControls(); bindFloorHover(); bindChartHover(); bindLedger();
window.addEventListener("resize", () => { if (S) { drawFloor(); renderCharts(); } });
poll();
})();
