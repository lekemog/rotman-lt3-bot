"use strict";

const byId = (id) => document.getElementById(id);
const form = byId("strategy-form");
const whole = new Intl.NumberFormat("en-IE");
const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});
const money = (value) => (value == null ? "—" : usd.format(Number(value)));
const number = (value) => (value == null ? "—" : whole.format(value));
let state;
let draft = false;
let writing = false;
let lastRevision;

function node(tag, text, classes) {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (classes) el.className = classes;
  return el;
}

function text(id, value) {
  byId(id).textContent = value;
}

function tone(element, value) {
  element.classList.toggle("positive", Number(value) > 0);
  element.classList.toggle("negative", Number(value) < 0);
}

function applyForm(strategy) {
  for (const [name, value] of Object.entries(strategy)) {
    form.elements.namedItem(name).value = value;
  }
  draft = false;
}

function updateButtons() {
  const armBlocked =
    !state?.fresh ||
    !state.order_enabled ||
    state.armed ||
    state.halted ||
    state.fault ||
    state.open_orders ||
    Object.values(state.positions).some(Boolean) ||
    draft ||
    (state.mode === "rit" && !byId("api-ack").checked);
  byId("arm").disabled = Boolean(writing || armBlocked);
  byId("pause").disabled = Boolean(writing || !state?.armed);
  byId("halt").disabled = Boolean(
    writing || !state?.order_enabled || state.halted,
  );
  byId("reset").disabled = writing;
  byId("apply").disabled = writing || !draft;
  byId("restore").disabled = writing || !draft;
  byId("restart").disabled = writing;
}

function showMessage(message) {
  text("message", message);
  byId("message").hidden = !message;
}

function render(next) {
  state = next;
  const live = state.mode === "rit";
  text(
    "mode",
    live
      ? state.order_enabled
        ? "RIT · ORDER CONTROL"
        : "RIT · MONITOR"
      : "REHEARSAL",
  );
  text("feed", state.fresh ? "Data current" : state.status);
  byId("feed-dot").classList.toggle("ok", state.fresh);
  text("case-name", state.case_name);
  const left = Math.max(0, state.end_tick - state.tick);
  text(
    "timer",
    state.fresh
      ? `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`
      : "—",
  );
  text(
    "tick",
    state.fresh
      ? `Tick ${state.tick} / ${state.end_tick}`
      : "Waiting for current heat",
  );
  byId("progress").max = state.end_tick;
  byId("progress").value = state.tick;
  text(
    "engine",
    state.fault
      ? "REVIEW IN RIT"
      : state.halted
        ? "STOPPED"
        : state.armed
          ? "ARMED"
          : "ENTRIES PAUSED",
  );
  byId("engine").classList.toggle("armed", state.armed);
  byId("engine").classList.toggle(
    "halted",
    state.halted || Boolean(state.fault),
  );
  byId("reset").hidden = !state.halted && !state.fault;
  byId("ack").hidden = !live || !state.order_enabled || state.armed;
  if (!state.fresh) byId("api-ack").checked = false;
  byId("fault").hidden = !state.fault;
  text("fault", state.fault);
  text(
    "control-help",
    live && !state.order_enabled
      ? "Monitor mode uses the RIT data feed."
      : state.halted
        ? "Check remaining positions and orders in RIT."
        : state.armed
          ? "The desk unwinds confirmed tender inventory."
          : "Pausing entries lets an existing unwind finish.",
  );
  byId("rehearsal").hidden = live;
  for (const ticker of ["CRZY", "TAME"]) {
    const key = ticker.toLowerCase();
    const value = state.positions[ticker];
    text(`position-${key}`, state.fresh ? number(value) : "—");
    tone(byId(`position-${key}`), value);
    text(
      `quote-${key}`,
      `Mid ${state.fresh ? money(state.quotes[ticker]) : "—"}`,
    );
  }
  text("pnl", money(state.pnl));
  tone(byId("pnl"), state.pnl);
  text(
    "completed",
    `${state.completed} completed ${state.completed === 1 ? "tender" : "tenders"}`,
  );
  text("orders", state.fresh ? number(state.open_orders) : "—");
  text("revision", `Revision ${state.revision}`);
  if (!draft && lastRevision !== state.revision) applyForm(state.strategy);
  lastRevision = state.revision;
  renderTenders(state.tenders);
  renderEvents(state.events);
  draw(state.history);
  updateButtons();
}

function renderTenders(tenders) {
  text("tender-count", `${tenders.length} pending`);
  const body = byId("tenders");
  body.replaceChildren();
  if (!tenders.length) {
    const cell = node("td", "No active tenders", "empty");
    cell.colSpan = 5;
    const row = node("tr");
    row.append(cell);
    body.append(row);
  }
  for (const tender of tenders) {
    const row = node("tr");
    const offer = node(
      "td",
      tender.ticker
        ? `${tender.ticker} · ${tender.side} ${number(tender.quantity)}`
        : `Tender #${tender.id}`,
    );
    offer.append(
      node(
        "small",
        tender.price == null
          ? "Open the RIT tender dialog"
          : `${money(tender.price)} · #${tender.id}`,
      ),
    );
    const visible = node("td", money(tender.visible), "amount");
    const stressed = node("td", money(tender.stressed), "amount");
    tone(visible, tender.visible);
    tone(stressed, tender.stressed);
    const decision = node("td");
    decision.append(
      node("span", tender.status, `decision ${tender.status.toLowerCase()}`),
      node("small", tender.reason),
    );
    row.append(
      offer,
      node(
        "td",
        tender.remaining == null ? "—" : `${tender.remaining} ticks`,
        "amount",
      ),
      visible,
      stressed,
      decision,
    );
    body.append(row);
  }
}

function renderEvents(events) {
  const target = byId("events");
  target.replaceChildren();
  if (!events.length)
    target.append(node("li", "Waiting for desk activity", "empty"));
  for (const event of events) {
    const row = node("li");
    row.append(
      node("time", event.time),
      node("b", event.event),
      node("span", event.message),
    );
    target.append(row);
  }
}

function draw(history) {
  const canvas = byId("chart");
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const scale = window.devicePixelRatio || 1;
  canvas.width = width * scale;
  canvas.height = height * scale;
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  const series = ["CRZY", "TAME"].map((ticker) => {
    const baseline = history.find((row) => row[ticker])?.[ticker];
    return history.map((row) =>
      row[ticker] && baseline ? (row[ticker] / baseline) * 100 : null,
    );
  });
  const values = series.flat().filter((value) => value != null);
  ctx.font = "11px Consolas, monospace";
  ctx.fillStyle = "#9cb1bb";
  if (!values.length) {
    ctx.fillText("Waiting for two-sided quotes", 16, height / 2);
    return;
  }
  const low = Math.min(99.9, ...values);
  const high = Math.max(100.1, ...values);
  const pad = 17;
  const right = Math.max(pad + 1, width - 59);
  const y = (value) =>
    pad + ((high - value) / (high - low)) * (height - 2 * pad);
  for (const value of [high, (high + low) / 2, low]) {
    ctx.strokeStyle = "#293c47";
    ctx.beginPath();
    ctx.moveTo(pad, y(value));
    ctx.lineTo(right, y(value));
    ctx.stroke();
    ctx.fillText(value.toFixed(2), right + 7, y(value) + 4);
  }
  series.forEach((points, index) => {
    ctx.beginPath();
    ctx.lineWidth = 2;
    ctx.strokeStyle = index === 0 ? "#73e5c1" : "#89aef5";
    let connected = false;
    points.forEach((value, i) => {
      if (value == null) {
        connected = false;
        return;
      }
      const x = pad + (i / Math.max(1, points.length - 1)) * (right - pad);
      if (connected) ctx.lineTo(x, y(value));
      else ctx.moveTo(x, y(value));
      connected = true;
    });
    ctx.stroke();
  });
}

async function send(path, payload, success) {
  writing = true;
  updateButtons();
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    if (path === "/api/strategy") applyForm(data.strategy);
    if (payload.action === "arm") byId("api-ack").checked = false;
    render(data);
    showMessage(success);
  } catch (error) {
    showMessage(error.message);
  } finally {
    writing = false;
    updateButtons();
  }
}

form.addEventListener("input", () => {
  draft = true;
  updateButtons();
});
form.addEventListener("submit", (event) => {
  event.preventDefault();
  send(
    "/api/strategy",
    Object.fromEntries(new FormData(form)),
    "Strategy applied",
  );
});
byId("restore").addEventListener("click", () => {
  applyForm(state.strategy);
  updateButtons();
});
byId("api-ack").addEventListener("change", updateButtons);
for (const [button, action, message] of [
  ["arm", "arm", "Entries armed"],
  ["pause", "pause", "New entries paused"],
  [
    "halt",
    "halt",
    "Desk stopped. Check the RIT blotter for any remaining inventory.",
  ],
  ["reset", "reset", "Desk reset"],
]) {
  byId(button).addEventListener("click", () => {
    send(
      "/api/command",
      { action, api_orders_enabled: byId("api-ack").checked },
      message,
    );
  });
}
byId("rehearsal-form").addEventListener("submit", (event) => {
  event.preventDefault();
  send(
    "/api/command",
    { action: "restart", ...Object.fromEntries(new FormData(event.target)) },
    "Rehearsal restarted",
  );
});
window.addEventListener("resize", () => draw(state?.history || []));

async function refresh() {
  if (!writing) {
    try {
      const response = await fetch("/api/state", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!writing) render(data);
    } catch {
      if (state) state.fresh = false;
      byId("feed-dot").classList.remove("ok");
      text("feed", "Desk disconnected");
      updateButtons();
    }
  }
  window.setTimeout(refresh, 650);
}

updateButtons();
refresh();
