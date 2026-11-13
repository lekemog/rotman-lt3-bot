"use strict";

const el = (id) => document.getElementById(id);
const form = el("strategy-form");
const radios = [...document.querySelectorAll("input[name='entry-mode']")];
const whole = new Intl.NumberFormat("en-IE");
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const number = (value) => value == null ? "—" : whole.format(value);
const money = (value) => value == null ? "—" : usd.format(Number(value));
const entryMode = () => radios.find((radio) => radio.checked).value;
const text = (id, value) => { el(id).textContent = value; };
const badge = (id, value, tone = "") => {
  text(id, value);
  el(id).dataset.tone = tone;
};
const fields = {
  depth_haircut: ["Depth removed", "%"],
  adverse_move: ["Adverse move", " $/share"],
  min_edge: ["Minimum edge", " $/share"],
  market_fee: ["Market fee", " $/share"],
  tender_fee: ["Tender fee", " $/share"],
  decision_ticks: ["Expiry buffer", " ticks"],
  min_heat_ticks: ["Minimum time left", " ticks"],
  ticks_per_child: ["Budget per order", " ticks"],
  exit_reserve_ticks: ["Exit reserve", " ticks"],
  limit_slippage_ticks: ["Limit price cushion", "¢"],
  market_closeout_ticks: ["Market closeout window", " ticks"],
  refill: ["Book refill between orders", "%"],
  sweep_band: ["Sweep band per order", "¢"],
};

let state;
let offer;
let selectedId;
let pickerKey = "";
let eventKey = "";
let draftRevision = null;
let loadedRevision;
let writing = false;

function node(tag, label, css) {
  const element = document.createElement(tag);
  if (label != null) element.textContent = label;
  if (css) element.className = css;
  return element;
}

function amount(id, value, format = money) {
  text(id, format(value));
  el(id).classList.toggle("positive", Number(value) > 0);
  el(id).classList.toggle("negative", Number(value) < 0);
}

function flat() {
  return state?.fresh && !state.inventory && !state.open_orders &&
    !Object.values(state.positions).some(Boolean);
}

function updateControls() {
  const live = state?.mode === "rit";
  const reviewArmed = state?.armed && state.entry_mode === "review";
  const autoArmed = state?.armed && state.entry_mode === "auto";
  const dirty = draftRevision != null;
  el("arm").disabled = writing || !flat() || !state?.order_enabled || Boolean(state?.fault || state?.halted || state?.armed || dirty) ||
    (live && !el("api-ack").checked);
  el("pause").disabled = writing || !state?.armed;
  el("halt").disabled = writing || !state?.order_enabled || state?.halted;
  el("reset").disabled = writing || !state?.fresh || Boolean(state?.open_orders) ||
    Object.values(state?.positions || {}).some(Boolean);
  el("approve").hidden = !reviewArmed || !offer?.ticker;
  el("approve").disabled = writing || !flat() || !reviewArmed || !offer || offer.status !== "ACCEPT" ||
    Boolean(state?.approval != null || dirty || state?.fault || state?.halted);
  el("skip").disabled = writing || !state?.fresh || autoArmed || offer?.id == null;
  for (const radio of radios) radio.disabled = writing || Boolean(state?.armed) || !state?.order_enabled;
  text("arm", "Arm " + (entryMode() === "review" ? "Review" : "Auto"));
  el("apply").disabled = writing || !dirty;
  el("restore").disabled = writing || !dirty;
  el("restart").disabled = writing;
  for (const button of form.querySelectorAll("[data-preset]")) button.disabled = writing || !state;
}

function fillForm() {
  for (const [key, value] of Object.entries(state.strategy)) {
    form.elements.namedItem(key).value = value;
  }
  loadedRevision = state.revision;
  draftRevision = null;
}

function renderDraft() {
  const active = state?.strategy;
  if (!active) return;
  const data = Object.fromEntries(new FormData(form));
  const edits = [];
  for (const [key, [label, unit]] of Object.entries(fields)) {
    if (data[key] === "" || Number(data[key]) !== Number(active[key])) {
      const proposed = data[key] === "" ? "missing value" : data[key] + unit;
      edits.push(node("li", label + " · " + active[key] + unit + " → " + proposed));
    }
  }
  if (!edits.length) draftRevision = null;
  const behind = draftRevision != null && draftRevision !== state.revision;
  if (behind) edits.unshift(node("li", "The active revision changed. Discard edits to reload it."));
  el("draft").hidden = edits.length === 0;
  el("changes").replaceChildren(...edits);
  updateControls();
}

function stagePreset(preset) {
  const active = state.strategy;
  const changes = preset === "defensive"
    ? { depth_haircut: Math.max(active.depth_haircut, 25), adverse_move: Math.max(active.adverse_move, 0.02) }
    : preset === "capture"
    ? { refill: Math.max(active.refill, 50), adverse_move: Math.min(active.adverse_move, 0.02),
        ticks_per_child: Math.min(active.ticks_per_child, 4),
        market_closeout_ticks: Math.max(active.market_closeout_ticks, 8) }
    : { min_heat_ticks: Math.max(active.min_heat_ticks, 35), ticks_per_child: Math.max(active.ticks_per_child, 8),
        exit_reserve_ticks: Math.max(active.exit_reserve_ticks, 15) };
  for (const [key, value] of Object.entries(changes)) form.elements.namedItem(key).value = value;
  draftRevision ??= loadedRevision;
  renderDraft();
}

function renderSession() {
  const live = state.mode === "rit";
  const connection = state.connection === "dma" ? "MacBook · DMA" : "Windows · RIT Client";
  badge("connection", live ? connection + (state.order_enabled ? "" : " · Monitor") : "Rehearsal");
  el("recording").hidden = !state.recording;
  text("feed", state.fresh ? "Feed current" : state.status);
  el("feed-dot").classList.toggle("ok", state.fresh);
  text("case-name", state.case_name);
  const remaining = Math.max(0, state.end_tick - state.tick);
  text("timer", state.fresh ? Math.floor(remaining / 60) + ":" + String(remaining % 60).padStart(2, "0") : "—");
  text("tick", state.fresh ? "Tick " + state.tick + " / " + state.end_tick : "Waiting for current heat");
  el("progress").max = state.end_tick;
  el("progress").value = state.tick;

  if (state.fault) badge("engine", "Review in RIT", "bad");
  else if (state.halted) badge("engine", "Manual takeover", "bad");
  else if (!state.fresh) badge("engine", "Feed unavailable", "warn");
  else if (state.armed) badge("engine", state.entry_mode === "review" ? "Review armed" : "Auto armed", "good");
  else badge("engine", "Entries paused");

  if (state.armed) radios.find((radio) => radio.value === state.entry_mode).checked = true;
  el("ack").hidden = !live || !state.order_enabled || state.armed || state.halted || Boolean(state.fault);
  if (!state.fresh || state.armed) el("api-ack").checked = false;
  el("fault").hidden = !state.fault;
  text("fault", state.fault);
  el("rehearsal").hidden = live;
  updateHelp();
}

function updateHelp() {
  if (!state) return;
  let help;
  if (state.fault || state.halted) help = "Check orders and positions in RIT before taking over.";
  else if (!state.order_enabled) help = "Monitor mode. The dashboard shows the bot’s recommendation.";
  else if (state.armed) help = state.entry_mode === "review"
    ? "You approve each eligible tender. The bot manages the exit."
    : "The bot accepts eligible tenders and manages exits automatically.";
  else if (state.inventory) help = "Entries are paused. The bot is still managing the exit.";
  else help = entryMode() === "review" ? "Approve each entry yourself, then let the bot unwind."
    : "Let the bot accept eligible tenders and unwind automatically.";
  text("control-help", help);
}

function priority(a, b) {
  const eligible = Number(b.status === "ACCEPT") - Number(a.status === "ACCEPT");
  if (eligible) return eligible;
  if (a.status === "ACCEPT" && Number(a.stressed) !== Number(b.stressed)) {
    return Number(b.stressed) - Number(a.stressed);
  }
  return (a.remaining ?? Infinity) - (b.remaining ?? Infinity);
}

function renderOffer() {
  const tenders = state.fresh ? [...state.tenders].sort(priority) : [];
  const select = el("offer-select");
  offer = tenders.find((tender) => String(tender.id) === selectedId) || tenders[0];
  if (offer) selectedId = String(offer.id);
  const nextKey = JSON.stringify(tenders.map((tender) => [tender.id, tender.ticker, tender.side, tender.quantity]));
  if (pickerKey !== nextKey) {
    pickerKey = nextKey;
    select.replaceChildren(...tenders.map((tender) => {
      const label = tender.ticker ? tender.side + " " + number(tender.quantity) + " " + tender.ticker : "Offer #" + tender.id;
      const option = node("option", label);
      option.value = tender.id;
      return option;
    }));
  }
  if (offer) select.value = offer.id;
  el("picker").hidden = tenders.length < 2;
  el("offer-details").hidden = !offer?.ticker;
  el("decision-reason").hidden = !offer;
  el("tender-actions").hidden = !offer;
  if (!offer) {
    text("tender-title", state.fresh ? "Waiting for an offer" : "Waiting for fresh data");
    text("tender-subtitle", state.fresh ? "Active offers will appear here." : "Tender controls resume once the heat feed is current.");
    badge("tender-status", "No tender");
    return;
  }
  text("tender-title", offer.ticker ? offer.side + " " + number(offer.quantity) + " " + offer.ticker : "Offer #" + offer.id);
  text("tender-subtitle", offer.ticker ? money(offer.price) + " per share · Tender #" + offer.id : "Check the tender in the RIT client");
  const queued = state.approval === offer.id;
  const reviewArmed = state.armed && state.entry_mode === "review";
  const labels = { WAIT: ["Wait", "warn"], PASS: ["Pass", "warn"], REVIEW: ["Check in RIT", "warn"], EXPIRED: ["Expired", "bad"] };
  const status = queued ? ["Checking latest book", "warn"] : offer.status === "ACCEPT"
    ? [reviewArmed ? "Ready for approval" : "Eligible", "good"] : (labels[offer.status] || [offer.status, ""]);
  badge("tender-status", ...status);
  text("decision-reason", offer.reason);
  if (!offer.ticker) return;
  amount("stressed", offer.stressed);
  amount("visible", offer.visible);
  text("expires", offer.remaining + " ticks");
  el("expires").dataset.tone = offer.remaining <= state.strategy.decision_ticks ? "bad" : "";
  text("depth", number(offer.depth) + " / " + number(offer.quantity) + " shares");
  el("depth-bar").value = Math.min(100, offer.depth / offer.quantity * 100);
  const left = Math.max(0, state.end_tick - state.tick);
  text("budget", left + " ticks left in heat · exit budget " + offer.budget + " ticks");
  text("approve", queued ? "Approval queued" : "Approve " + offer.side.toLowerCase() + " tender");
}

function renderPosition() {
  const inventory = state.inventory;
  const working = state.working;
  const hasPosition = Object.values(state.positions).some(Boolean);
  el("exit-progress").hidden = !inventory;
  el("reset").hidden = !state.fault && !state.halted;
  let title = state.fresh ? "Flat" : "Position unverified";
  let detail = state.fresh ? "No inventory to unwind." : "Last known exposure needs confirmation in RIT.";
  if (inventory) {
    title = state.halted || state.fault ? "Check remaining inventory in RIT"
      : inventory.confirmed ? "Unwinding " + inventory.ticker : "Confirming tender fill";
    detail = "Tender #" + inventory.id + " · Exit side " + inventory.exit_side.toLowerCase() +
      (state.fresh ? "" : " · Last known inventory");
    text("exit-remaining", number(inventory.remaining));
    el("exit-bar").value = (1 - inventory.remaining / inventory.quantity) * 100;
    text("working", working ? (working.cancelling ? "Cancellation requested for " : "Reconciling ") +
      "order #" + working.id + " · " + number(working.quantity) + " submitted"
      : "No managed child order working");
  } else if (state.fresh && (hasPosition || state.open_orders)) {
    title = "Check account in RIT";
    detail = "This desk has no confirmed tender to manage.";
  }
  text("inventory-title", title);
  text("exit-detail", detail);
  badge("order-count", state.fresh ? number(state.open_orders) + " open" : "Orders unverified",
    state.fresh && state.open_orders ? "warn" : "");
  for (const ticker of ["CRZY", "TAME"]) {
    amount("position-" + ticker.toLowerCase(), state.fresh ? state.positions[ticker] : null, number);
  }
  amount("pnl", state.pnl);
  text("completed", state.completed + " completed");
}

function renderEvents() {
  const nextKey = JSON.stringify(state.events);
  if (nextKey === eventKey) return;
  eventKey = nextKey;
  const rows = state.events.map((event) => {
    const row = node("li");
    row.append(node("time", event.time), node("b", event.event), node("span", event.message));
    return row;
  });
  if (!rows.length) rows.push(node("li", "No desk activity yet", "empty"));
  el("events").replaceChildren(...rows);
}

function render(next) {
  if (state && state.heat !== next.heat) {
    selectedId = undefined;
    el("api-ack").checked = false;
  }
  state = next;
  renderSession();
  renderOffer();
  renderPosition();
  renderEvents();
  badge("revision", "Revision " + state.revision);
  const active = state.strategy;
  text("active-profile", "Active · " + active.depth_haircut + "% depth removed · " +
    (Number(active.adverse_move) * 100).toLocaleString("en-IE") + "¢ adverse move · " + active.min_heat_ticks + "-tick exit floor");
  if (draftRevision == null && loadedRevision !== state.revision) fillForm();
  renderDraft();
  if (el("market-tape").open) draw(state.history);
}

function notify(message, error = false) {
  const box = el("message");
  box.hidden = !message;
  box.classList.toggle("danger", error);
  text("message", message);
}

async function send(path, payload, success) {
  if (writing) return;
  writing = true;
  updateControls();
  try {
    const response = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Desk command failed");
    if (path === "/api/strategy") draftRevision = null;
    render(data);
    notify(success);
  } catch (error) {
    notify(error.message, true);
  } finally {
    writing = false;
    updateControls();
  }
}

function command(action, message, options = {}) {
  send("/api/command", { action, ...options }, message);
}

form.addEventListener("input", () => {
  draftRevision ??= loadedRevision;
  renderDraft();
});
form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (draftRevision == null) return;
  send("/api/strategy", { ...Object.fromEntries(new FormData(form)), expected_revision: draftRevision },
    "Strategy applied");
});
for (const button of form.querySelectorAll("[data-preset]")) {
  button.addEventListener("click", () => stagePreset(button.dataset.preset));
}
el("restore").addEventListener("click", () => { fillForm(); renderDraft(); });
el("api-ack").addEventListener("change", updateControls);
radios.forEach((radio) => radio.addEventListener("change", () => { updateHelp(); updateControls(); }));
el("arm").addEventListener("click", () => command("arm", entryMode() === "review" ? "Review armed" : "Auto armed",
  { entry_mode: entryMode(), api_orders_enabled: el("api-ack").checked }));
el("pause").addEventListener("click", () => command("pause", "New entries paused"));
el("halt").addEventListener("click", () => command("halt", "Takeover started. Confirm the cancellation and remaining inventory in RIT."));
el("reset").addEventListener("click", () => command("reset", "Desk reset. Entries are paused."));
el("offer-select").addEventListener("change", (event) => {
  selectedId = event.target.value;
  renderOffer();
  updateControls();
});
el("approve").addEventListener("click", () => command("approve", "Approval queued for a fresh book check", {
  tender_id: offer.id, heat: state.heat, period: state.period, revision: state.revision,
  ticker: offer.ticker, side: offer.side, quantity: offer.quantity, price: offer.price,
}));
el("skip").addEventListener("click", () => command("skip", "Offer skipped by the desk for this heat",
  { tender_id: offer.id, heat: state.heat, period: state.period }));
el("rehearsal-form").addEventListener("submit", (event) => {
  event.preventDefault();
  command("restart", "Rehearsal restarted", Object.fromEntries(new FormData(event.target)));
});
el("market-tape").addEventListener("toggle", () => { if (el("market-tape").open) draw(state?.history || []); });
window.addEventListener("resize", () => { if (el("market-tape").open) draw(state?.history || []); });

async function refresh() {
  if (!writing) {
    try {
      const response = await fetch("/api/state", { cache: "no-store" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      if (!writing && (!state || data.heat > state.heat ||
          (data.heat === state.heat && data.revision >= state.revision && data.tick >= state.tick))) render(data);
    } catch {
      if (state && !writing) render({ ...state, fresh: false, status: "Desk disconnected" });
    }
  }
  window.setTimeout(refresh, 650);
}

updateControls();
refresh();
function draw(history) {
  const canvas = el("chart");
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

