// LT3 desk logic (rotman_lt3.py evaluate/sweep + execution.py unwind) ported to run
// inside the RIT web client tab. Paste into the console on /Trading/Index.
// Settings: "Capture more" + "Erratic traders" from STRATEGY.md.
// Stop with LT3.stop(). Run only ONE trader per account (no Python desk at the same time).
(() => {
  if (window.LT3 && window.LT3.running) window.LT3.stop();
  const S = {
    haircut: 6, adverse: 0.02, refill: 50, fee: 0.02, minEdge: 0,
    decisionTicks: 2, minHeatTicks: 8, ticksPerChild: 4, reserve: 8,
    slip: 0.01, closeout: 8, guardBps: 100, netLimit: 100000, grossLimit: 250000,
    restMs: 700, passiveRestMs: 2000, loopMs: 250,
  };
  const CAP = { CRZY: 25000, TAME: 10000 };
  const me = (document.body.innerText.match(/whsc-\d+/) || [""])[0];
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const r2 = x => Math.round(x * 100) / 100;
  const st = { running: true, mids: { CRZY: [], TAME: [] }, working: null, nextSubmit: 0, attempted: new Set(), log: [] };

  const box = document.createElement("div");
  box.id = "lt3box";
  box.style.cssText = "position:fixed;right:8px;bottom:8px;width:460px;max-height:260px;overflow:auto;z-index:99999;background:#111;color:#0f0;font:11px monospace;padding:6px;border:2px solid #0a0;opacity:.92";
  document.getElementById("lt3box")?.remove();
  document.body.appendChild(box);
  const log = (...a) => {
    const line = new Date().toLocaleTimeString() + " " + a.join(" ");
    st.log.push(line); console.log("[LT3]", line);
    box.innerHTML = "<b>LT3 bot " + (st.running ? "RUNNING" : "STOPPED") + "</b> · LT3.stop() to halt<br>" + st.log.slice(-18).reverse().join("<br>");
  };

  async function get(url) {
    for (let k = 0; k < 6; k++) {
      try {
        const r = await fetch(url, { cache: "no-store" });
        const d = await r.json();
        if (d && d.success === false && /429/.test(d.error || "")) { await sleep(250 + 150 * k); continue; }
        return d;
      } catch (e) { await sleep(200); }
    }
    return null;
  }
  async function post(url, body) {
    const opt = { method: "POST" };
    if (body) { opt.headers = { "Content-Type": "application/json" }; opt.body = JSON.stringify(body); }
    const r = await fetch(url, opt);
    try { return await r.json(); } catch (e) { return { success: r.ok }; }
  }
  const order = (ticker, action, quantity, price) => post("/Trading/PlaceOrder", {
    ticker, type: price == null ? "MARKET" : "LIMIT", action, quantity, price: price == null ? null : r2(price),
  });

  // exit_levels: aggregate other traders' quantity per price, best first.
  function levels(book, exitSide) {
    const m = new Map();
    for (const row of (exitSide === "SELL" ? book.bids : book.asks) || []) {
      if (me && row.traderId === me) continue;
      const q = +row.qty, p = +row.price;
      if (q > 0 && p > 0) m.set(p, (m.get(p) || 0) + q);
    }
    return [...m.entries()].sort((a, b) => exitSide === "SELL" ? b[0] - a[0] : a[0] - b[0]);
  }
  // sweep: walk the book child by child with haircut, adverse move and refill.
  function sweep(lv, quantity, exitSide, haircut = 0, adverse = 0, cap = null, refill = 0) {
    cap = cap || quantity;
    lv = lv.map(([p, s]) => [p, Math.floor(s * (100 - haircut) / 100)]);
    let filled = 0, value = 0, worst = null, hole = 0;
    while (filled < quantity) {
      const child = Math.min(cap, quantity - filled);
      hole = Math.floor(hole * (100 - refill) / 100);
      let skip = hole, got = 0;
      for (const [price, avail] of lv) {
        if (skip >= avail) { skip -= avail; continue; }
        const take = Math.min(child - got, avail - skip); skip = 0;
        const sp = exitSide === "SELL" ? price - adverse : price + adverse;
        if (sp <= 0) break;
        got += take; value += take * sp; worst = sp;
        if (got === child) break;
      }
      filled += got; hole += got;
      if (got < child) break;
    }
    return { filled, value, worst };
  }
  const median = a => { const s = [...a].sort((x, y) => x - y); return s.length ? s[Math.floor(s.length / 2)] : null; };

  function evaluate(t, book, tick, end, pos) {
    const dir = t.action === "BUY" ? 1 : -1, exitSide = dir > 0 ? "SELL" : "BUY";
    const lv = levels(book, exitSide);
    const chunks = Math.ceil(t.quantity / CAP[t.ticker]);
    const minHeat = Math.max(S.minHeatTicks, S.reserve + S.ticksPerChild * chunks);
    const sw = sweep(lv, t.quantity, exitSide, S.haircut, S.adverse, CAP[t.ticker], S.refill);
    const pnl = sw.filled === t.quantity ? dir * (sw.value - t.price * t.quantity) - t.quantity * S.fee : null;
    if (t.expires - tick <= S.decisionTicks) return ["PASS", "expiry buffer", pnl];
    if (end - tick <= minHeat) return ["PASS", "heat closeout window", pnl];
    const proj = { ...pos }; proj[t.ticker] += dir * t.quantity;
    const net = Math.abs(proj.CRZY + proj.TAME), gross = Math.abs(proj.CRZY) + Math.abs(proj.TAME);
    if (net > S.netLimit || gross > S.grossLimit) return ["WAIT", "would breach limits", pnl];
    if (pos.CRZY || pos.TAME || st.working) return ["WAIT", "inventory still open", pnl];
    if (pnl == null) return ["WAIT", "depth cannot cover block", pnl];
    if (pnl <= S.minEdge * t.quantity) return ["WAIT", "stressed P&L " + Math.round(pnl), pnl];
    return ["ACCEPT", "stressed P&L $" + Math.round(pnl), pnl];
  }

  async function unwind(ticker, position, tick, end) {
    const now = Date.now();
    if (st.working) {
      if (now - st.working.at < st.working.rest) return;
      await post("/Trading/CancelAllOrders");           // clear any resting remainder, re-read position next loop
      st.working = null; st.nextSubmit = Date.now() + 150; return;
    }
    if (now < st.nextSubmit) return;
    const exitSide = position > 0 ? "SELL" : "BUY";
    const book = await get("/Trading/OrderBookJson?ticker=" + ticker);
    if (!book || !book.success) return;
    const remaining = end - tick;
    const qty0 = Math.min(Math.abs(position), CAP[ticker]);
    const chunks = Math.ceil(Math.abs(position) / CAP[ticker]);
    const hurried = remaining <= S.reserve + S.ticksPerChild * chunks;
    const closeout = remaining > 0 && remaining <= S.closeout;
    const theirs = levels(book, exitSide);
    let qty, price, rest = S.restMs, note = "";
    if (closeout || !theirs.length && hurried) { qty = qty0; price = null; note = "market closeout"; }
    else {
      const ref = median(st.mids[ticker].slice(-11, -1));
      const gap = theirs.length && ref ? (exitSide === "SELL" ? ref - theirs[0][0] : theirs[0][0] - ref) : 0;
      if (!hurried && S.guardBps && st.mids[ticker].length >= 6 && gap * 10000 > S.guardBps * ref) {
        const ours = levels(book, exitSide === "SELL" ? "BUY" : "SELL");
        const step = exitSide === "SELL" ? -0.01 : 0.01;
        price = ours.length ? (theirs.length && Math.abs(ours[0][0] - theirs[0][0]) >= 0.02 ? ours[0][0] + step : ours[0][0]) : theirs[0][0] - step;
        qty = qty0; rest = S.passiveRestMs; note = "hole guard: resting";
      } else {
        const q = sweep(theirs, qty0, exitSide);
        if (!q.filled) return;
        qty = q.filled; price = exitSide === "SELL" ? Math.max(0.01, q.worst - S.slip) : q.worst + S.slip;
      }
    }
    const res = await order(ticker, exitSide, qty, price);
    st.working = { at: Date.now(), rest };
    log("ORDER", exitSide, qty, ticker, price == null ? "MKT" : "@" + r2(price), note, res && res.success === false ? "ERR " + (res.error || "") : "ok");
  }

  async function loop() {
    log("started as", me || "?", "· settings Capture more + guard");
    while (st.running) {
      try {
        const c = await get("/Trading/CaseStateJson");
        if (!c || !c.success || c.status !== "ACTIVE") { await sleep(800); continue; }
        const tick = c.tick, end = c.ticksPerPeriod;
        const sec = await get("/Trading/SecuritiesJson");
        if (!sec || !sec.success) { await sleep(S.loopMs); continue; }
        const pos = { CRZY: 0, TAME: 0 };
        for (const s of sec.securities) {
          pos[s.ticker] = s.position;
          if (s.bid > 0 && s.ask > 0) { const a = st.mids[s.ticker]; if (!a.length || a.at(-1)[0] !== tick) a.push([tick, (s.bid + s.ask) / 2]); if (a.length > 30) a.shift(); }
        }
        for (const k in st.mids) st.mids[k] = st.mids[k]; // keep shape
        const midsOnly = {}; for (const k in st.mids) midsOnly[k] = st.mids[k].map(x => x[1]);
        const held = ["CRZY", "TAME"].find(k => pos[k] !== 0);
        if (held) {
          const saved = st.mids; st.mids = midsOnly;
          await unwind(held, pos[held], tick, end);
          st.mids = saved;
        } else {
          if (st.working && Date.now() - st.working.at > st.working.rest) { await post("/Trading/CancelAllOrders"); st.working = null; }
          const td = await get("/Trading/TenderJson");
          if (td && td.success && td.hasTender && td.tender && !st.attempted.has(td.tender.tenderId)) {
            const t = td.tender;
            const book = await get("/Trading/OrderBookJson?ticker=" + t.ticker);
            if (book && book.success) {
              const [status, why] = evaluate(t, book, tick, end, pos);
              if (status === "ACCEPT") {
                st.attempted.add(t.tenderId);
                const r = await post("/Trading/AcceptTender?tenderId=" + encodeURIComponent(t.tenderId));
                log("ACCEPT #" + t.tenderId, t.action, t.quantity, t.ticker, "@" + t.price, "·", why, r && r.success === false ? "ERR " + (r.error || "") : "ok");
                st.nextSubmit = Date.now() + 300;
              } else if (st.lastWhy !== t.tenderId + why) {
                st.lastWhy = t.tenderId + why;
                log(status, "#" + t.tenderId, t.action, t.quantity, t.ticker, "@" + t.price, "·", why);
              }
            }
          }
        }
      } catch (e) { log("error", e.message); }
      await sleep(S.loopMs);
    }
    log("stopped");
  }
  window.LT3 = { get running() { return st.running; }, stop() { st.running = false; log("stop requested"); }, S, st };
  loop();
})();
