"""Order lifecycle for the Rotman simulator and its local rehearsal venue."""

from collections import deque
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from http.client import HTTPException
import json
from threading import RLock
import time
from urllib.parse import urlencode

from rotman_lt3 import (
    CAP, CENT, ZERO, RateLimited, RITReader, Settings, Tender, decimal,
    evaluate, exit_levels, next_unwind, parse_tender, shares,
)
from rotman_lt3_backtest import Heat, SCENARIOS, arrived_order


TERMINAL = {"TRANSACTED", "CANCELLED"}


@dataclass(frozen=True)
class Strategy(Settings):
    decision_ticks: int = 4
    ticks_per_child: int = 6
    exit_reserve_ticks: int = 10
    limit_slippage_ticks: int = 1
    market_closeout_ticks: int = 0

    def tender_settings(self, tender):
        chunks = (tender.quantity + CAP[tender.ticker] - 1) // CAP[tender.ticker]
        budget = self.exit_reserve_ticks + self.ticks_per_child * chunks
        return replace(self, min_heat_ticks=max(self.min_heat_ticks, budget))


STRATEGY_FIELDS = {
    "depth_haircut": (0, 90), "adverse_move": (0, 1), "min_edge": (0, 1),
    "market_fee": (0, 1), "tender_fee": (0, 1), "decision_ticks": (2, 20),
    "min_heat_ticks": (5, 150), "ticks_per_child": (1, 20),
    "exit_reserve_ticks": (0, 60), "limit_slippage_ticks": (0, 10),
    "market_closeout_ticks": (0, 30),
}


def strategy_from_dict(data):
    if set(data) != set(STRATEGY_FIELDS):
        raise ValueError("Send the complete strategy profile")
    defaults = Strategy()
    converted = {}
    for name, (low, high) in STRATEGY_FIELDS.items():
        parser = decimal if isinstance(getattr(defaults, name), Decimal) else shares
        value = parser(data[name])
        if not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high}")
        converted[name] = value
    return Strategy(**converted)


class RITVenue(RITReader):
    def __init__(self, port, enable_orders, *, mode="client", dma_https=False):
        super().__init__(mode, port, dma_https=dma_https)
        self.enable_orders = enable_orders
        self.next_submit = 0.0

    def write(self, method, endpoint, **params):
        if not self.enable_orders:
            raise RuntimeError("Order submission is disabled for this session")
        path = f"/v1/{endpoint}"
        if params:
            path += "?" + urlencode(params)
        self.connection.request(method, path, headers=self.headers)
        response = self.connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(f"RIT returned HTTP {response.status} for {method} {endpoint}")
        if endpoint == "orders":
            wait = float(decimal(response.getheader("X-Wait-Until") or 0))
            self.next_submit = time.monotonic() + max(0, wait)
        return json.loads(body)

    def accept(self, tender):
        params = {"price": format(tender.price, "f")} if self.connection_mode == "dma" else {}
        return self.write("POST", f"tenders/{tender.id}", **params)["success"] is True

    def submit(self, ticker, side, qty, limit):
        params = dict(ticker=ticker, action=side, quantity=qty, type="MARKET" if limit is None else "LIMIT")
        if limit is not None:
            params["price"] = format(limit, "f")
        return shares(self.write("POST", "orders", **params)["order_id"])

    def order(self, id_):
        return self.get(f"orders/{id_}")

    def cancel(self, id_):
        return self.write("DELETE", f"orders/{id_}")["success"] is True


class DemoVenue:
    enable_orders = True
    next_submit = 0.0
    connection_mode = "demo"

    def __init__(self):
        self.round = 0
        self.restart("Normal")

    def restart(self, scenario):
        profile = next((item for item in SCENARIOS if item.name == scenario
                        and item.name not in {"Slow polling and fills", "Fee on both legs"}), None)
        if profile is None:
            raise ValueError("Unknown rehearsal scenario")
        if self.round:
            self.close()
        self.round += 1
        self.heat = Heat(profile, 20261112 + self.round)
        self.started = time.monotonic()
        self.positions = dict.fromkeys(CAP, 0)
        self.accepted = set()
        self.orders = {}
        self.id = 0

    def close(self):
        self.heat.book.cache_clear()

    def tick(self):
        return min(299, 20 + int((time.monotonic() - self.started) * 1.5))

    def snapshot(self, depth):
        if self.tick() >= 299:
            return "STOPPED", None
        tick = self.tick()
        tenders = []
        for start, tender in self.heat.tenders:
            if start <= tick < tender.expires and tender.id not in self.accepted:
                opposite = "SELL" if tender.side == "BUY" else "BUY"
                tenders.append(dict(tender_id=tender.id, ticker=tender.ticker, quantity=tender.quantity,
                                    price=tender.price, expires=tender.expires, period=self.round,
                                    is_fixed_bid=True, caption=f"An institution would like to {opposite} {tender.ticker}."))
        working = [id_ for id_, order in self.orders.items() if order["status"] == "OPEN"]
        return "ACTIVE", dict(tick=tick, period=self.round, end_tick=300,
                              case_name=f"Rehearsal · {self.heat.scenario.name}", positions=dict(self.positions),
                              open_orders=len(working), open_order_ids=working, tenders=tenders,
                              books={ticker: self.heat.book(ticker, tick) for ticker in CAP})

    def accept(self, tender):
        if tender.id in self.accepted or tender.expires <= self.tick():
            return False
        self.accepted.add(tender.id)
        self.positions[tender.ticker] = tender.direction * tender.quantity
        return True

    def submit(self, ticker, side, qty, limit):
        self.id += 1
        position_side = "SELL" if side == "BUY" else "BUY"
        tender = Tender(0, ticker, position_side, qty, ZERO, 0, self.round)
        fill = arrived_order(self.heat.book(ticker, self.tick()), tender, qty, limit)
        self.positions[ticker] -= tender.direction * fill.filled
        status = "TRANSACTED" if fill.filled == qty else ("CANCELLED" if limit is None else "OPEN")
        self.orders[self.id] = dict(status=status, ticker=ticker, action=side, quantity=qty,
                                    quantity_filled=fill.filled, vwap=fill.vwap)
        return self.id

    def order(self, id_):
        return dict(self.orders[id_])

    def cancel(self, id_):
        self.orders[id_]["status"] = "CANCELLED"
        return True


@dataclass
class Inventory:
    tender: Tender
    expected: int
    sent_at: float
    market_fee: Decimal
    tender_fee: Decimal
    confirmed: bool = False
    proceeds: Decimal = ZERO


@dataclass
class WorkingOrder:
    id: int
    quantity: int
    sent_at: float
    cancelled_at: float | None = None
    terminal_at: float | None = None


class Desk:
    def __init__(self, venue, mode, state_dir, record=False):
        self.venue = venue
        self.mode = mode
        self.state_dir = state_dir
        self.record = record
        self.lock = RLock()
        self.strategy = Strategy()
        self.revision = 1
        self.events = deque(maxlen=60)
        self.history = deque(maxlen=180)
        self.snapshot = None
        self.read_at = 0.0
        self.clock_at = 0.0
        self.heat = 1
        self.status = "Connecting"
        self.armed = False
        self.entry_mode = "auto"
        self.halted = False
        self.fault = ""
        self.inventory = None
        self.working = None
        self.attempted = set()
        self.skipped = set()
        self.approval = None
        self.pnl = ZERO
        self.completed = 0
        path = state_dir / "strategy.json"
        if path.exists():
            self.strategy = strategy_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def log(self, event, message):
        row = dict(time=time.strftime("%H:%M:%S"), event=event, message=message)
        self.events.appendleft(row)
        try:
            with (self.state_dir / "activity.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass  # Keep the on-screen log and existing unwind active if disk is unavailable.

    def save_strategy(self, data, expected_revision=None):
        strategy = strategy_from_dict(data)
        with self.lock:
            if expected_revision is not None and expected_revision != self.revision:
                raise ValueError("The strategy changed in another tab. Revert edits to load the active revision.")
            path = self.state_dir / "strategy.json"
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(asdict(strategy), default=str, indent=2) + "\n", encoding="utf-8")
            temp.replace(path)
            self.strategy = strategy
            self.revision += 1
            self.approval = None
            self.log("STRATEGY", f"Applied revision {self.revision}")

    def command(self, action, data):
        with self.lock:
            if action == "restart" and self.mode == "rehearsal":
                self.venue.restart(data.get("scenario"))
                self.heat += 1
                self.armed = self.halted = False
                self.fault = ""
                self.snapshot = self.inventory = self.working = None
                self.attempted.clear()
                self.skipped.clear()
                self.approval = None
                self.history.clear()
                self.pnl, self.completed = ZERO, 0
                self.log("REHEARSAL", "Started a fresh heat")
                return
            if action == "pause":
                self.armed = False
                self.approval = None
                self.log("PAUSED", "New entries paused. Any managed position will continue unwinding.")
                return
            if action == "halt":
                self.armed = False
                self.approval = None
                self.halted = True
                self.log("HALTED", "Only cancellation and reconciliation of the managed order remain enabled.")
                return
            fresh = self.is_fresh()
            flat = fresh and not any(self.snapshot["positions"].values()) and not self.snapshot["open_orders"]
            if action == "reset":
                if not flat:
                    raise ValueError("Confirm a flat account with no working orders in RIT first")
                self.halted, self.fault = False, ""
                self.inventory = self.working = None
                self.approval = None
                self.log("RESET", "Confirmed a flat account. Entries remain paused.")
                return
            if action == "arm":
                entry_mode = data.get("entry_mode", "auto")
                if entry_mode not in ("auto", "review"):
                    raise ValueError("Choose Review or Auto for tender entries")
                if not self.venue.enable_orders:
                    raise ValueError("This RIT session is in monitor mode")
                if self.mode == "rit" and data.get("api_orders_enabled") is not True:
                    raise ValueError("Confirm API Orders is enabled and automation is permitted for this heat")
                if not flat or self.fault or self.halted or self.inventory:
                    raise ValueError("Arming requires a fresh, flat account with no fault or working order")
                self.entry_mode = entry_mode
                self.approval = None
                self.armed = True
                message = "Waiting for operator approval on each tender" if entry_mode == "review" else "Automatic tender selection and unwind enabled"
                self.log("ARMED", message)
                return
            if action in ("approve", "skip"):
                self.review_tender(action, data, fresh, flat)
                return
            raise ValueError("Unknown desk action")

    def is_fresh(self):
        return self.snapshot is not None and self.status == "ACTIVE" and time.monotonic() - self.read_at < 2

    def review_tender(self, action, data, fresh, flat):
        if not fresh or data.get("heat") != self.heat or data.get("period") != self.snapshot["period"]:
            raise ValueError("This tender view is stale. Wait for the current heat to refresh.")
        id_ = shares(data.get("tender_id"))
        offer = next((item for item in self.decisions(self.snapshot) if item[2].get("tender_id") == id_), None)
        if not offer or id_ in self.attempted or id_ in self.skipped:
            raise ValueError("This tender is no longer available to review")
        tender, decision, _ = offer
        if action == "skip":
            self.skipped.add(id_)
            if self.approval and self.approval[0].id == id_:
                self.approval = None
            self.log("SKIPPED", f"Tender #{id_} ignored by the desk for this heat")
            return
        if not (self.armed and self.entry_mode == "review" and flat and not self.inventory
                and not self.halted and not self.fault and not self.approval):
            raise ValueError("Arm Review while flat to approve a tender")
        if data.get("revision") != self.revision or not decision or decision.status != "ACCEPT":
            raise ValueError("The tender no longer clears the active strategy. Review the updated quote.")
        if (data.get("ticker"), data.get("side"), data.get("quantity"), decimal(data.get("price"))) != (
                tender.ticker, tender.side, tender.quantity, tender.price):
            raise ValueError("The tender terms changed. Review the updated offer.")
        self.approval = (tender, time.monotonic() + 3)
        self.log("APPROVED", f"Tender #{id_} queued for a fresh book check")

    def fail(self, message):
        self.armed = False
        self.approval = None
        self.fault = message
        self.log("REVIEW IN RIT", message)

    def feed_error(self, message):
        with self.lock:
            self.approval = None
            if self.armed:
                self.armed = False
                self.log("FEED", "Entries paused after a feed error")
            self.status = message

    def poll(self):
        # Hold the lock across the snapshot and writes. Restart, halt and strategy
        # revisions apply between complete iterations and never race a submission.
        with self.lock:
            try:
                status, snapshot = self.venue.snapshot(50)
            except RateLimited:
                raise
            except (OSError, HTTPException, ValueError, RuntimeError, KeyError, TypeError) as error:
                self.venue.close()
                self.feed_error(f"RIT data unavailable · {error}")
                return
            self.status = status
            if snapshot is None:
                self.approval = None
                if status in {"STOPPED", "PAUSED"}:
                    self.armed = False
                return
            now = time.monotonic()
            previous = self.snapshot
            self.snapshot, self.read_at = snapshot, now
            changed = previous and (previous["period"] != snapshot["period"] or snapshot["tick"] < previous["tick"])
            if not previous or changed or previous["tick"] != snapshot["tick"]:
                self.clock_at = now
            elif now - self.clock_at > 3:
                self.feed_error("RIT case clock stopped advancing")
                return
            if changed:
                self.heat += 1
                self.armed = False
                self.attempted.clear()
                self.skipped.clear()
                self.approval = None
                self.history.clear()
                self.completed, self.pnl = 0, ZERO
                if self.inventory or self.working:
                    self.fail("Heat changed while an execution was active. Reconcile in RIT.")
            try:
                self.update_history(snapshot)
            except (ValueError, KeyError, TypeError, ArithmeticError):
                self.feed_error("RIT supplied an invalid order book")
                return
            if self.record and self.mode == "rit":
                try:
                    with (self.state_dir / "snapshots.jsonl").open("a", encoding="utf-8") as out:
                        out.write(json.dumps(snapshot, default=str) + "\n")
                except OSError:
                    self.record = False
                    self.log("RECORDING", "Snapshot recording stopped after a write error")
            if self.fault:
                return
            try:
                self.step(snapshot, now)
            except RateLimited:
                raise
            except (OSError, HTTPException, ValueError, RuntimeError, KeyError, TypeError, ArithmeticError) as error:
                self.venue.close()
                self.fail(f"Execution could not be confirmed. Check the RIT blotter before resetting. {error}")

    def update_history(self, snapshot):
        tick = snapshot["tick"]
        if self.history and self.history[-1]["tick"] == tick:
            return
        row = {"tick": tick}
        for ticker, book in snapshot["books"].items():
            bids, asks = exit_levels(book, "SELL"), exit_levels(book, "BUY")
            row[ticker] = float((bids[0][0] + asks[0][0]) / 2) if bids and asks else None
        self.history.append(row)

    def decisions(self, snapshot):
        output = []
        for row in snapshot["tenders"]:
            try:
                tender = parse_tender(row)
                output.append((tender, evaluate(tender, snapshot, self.strategy.tender_settings(tender)), row))
            except (ValueError, KeyError, TypeError, ArithmeticError):
                output.append((None, None, row))
        return output

    def step(self, snapshot, now):
        if self.inventory:
            self.manage_inventory(snapshot, now)
            return
        if not self.armed or self.halted:
            return
        if snapshot["open_orders"] or any(snapshot["positions"].values()):
            self.fail("Unmanaged position or working order. Clear it in RIT before arming.")
            return
        candidates = [(tender, decision) for tender, decision, _ in self.decisions(snapshot)
                      if decision and decision.status == "ACCEPT"
                      and tender.id not in self.attempted and tender.id not in self.skipped]
        if self.entry_mode == "review":
            if not self.approval:
                return
            approved, deadline = self.approval
            self.approval = None
            candidates = [item for item in candidates if item[0] == approved and now <= deadline]
            if not candidates:
                self.log("APPROVAL LAPSED", f"Tender #{approved.id} changed, expired or no longer clears the strategy")
        if not candidates:
            return
        tender, decision = max(candidates, key=lambda item: (item[1].stressed_pnl, -item[0].expires))
        self.attempted.add(tender.id)
        if not self.venue.accept(tender):
            self.fail(f"Tender {tender.id} was not confirmed by RIT")
            return
        self.inventory = Inventory(tender, tender.direction * tender.quantity, time.monotonic(),
                                   self.strategy.market_fee, self.strategy.tender_fee)
        self.log("ACCEPTED", f"#{tender.id} {tender.side} {tender.quantity:,} {tender.ticker} @ {tender.price:.2f} · stressed ${decision.stressed_pnl:,.0f}")

    def manage_inventory(self, snapshot, now):
        inventory = self.inventory
        tender = inventory.tender
        position = snapshot["positions"][tender.ticker]
        own_id = self.working.id if self.working else None
        if any(value for ticker, value in snapshot["positions"].items() if ticker != tender.ticker):
            raise RuntimeError("Unexpected inventory in the other ticker")
        if set(snapshot["open_order_ids"]) - {own_id}:
            raise RuntimeError("An unmanaged order is working")
        if self.working:
            self.reconcile_order(position, now)
            return
        if position != inventory.expected:
            if not inventory.confirmed and position == 0 and now - inventory.sent_at < 5:
                return
            raise RuntimeError(f"Expected {inventory.expected:+,} shares, observed {position:+,}")
        inventory.confirmed = True
        if self.halted or now < self.venue.next_submit:
            return
        book = snapshot["books"][tender.ticker]
        child = next_unwind(tender, position, book, open_orders=0)
        remaining = snapshot["end_tick"] - snapshot["tick"]
        if 0 < remaining <= self.strategy.market_closeout_ticks:
            qty, limit = min(abs(position), CAP[tender.ticker]), None
        elif child:
            qty = child.quantity
            slip = CENT * self.strategy.limit_slippage_ticks
            limit = max(CENT, child.limit - slip) if child.side == "SELL" else child.limit + slip
        else:
            return
        order_id = self.venue.submit(tender.ticker, tender.exit_side, qty, limit)
        self.working = WorkingOrder(order_id, qty, time.monotonic())
        price = "market" if limit is None else f"limit {limit:.2f}"
        self.log("ORDER", f"#{order_id} {tender.exit_side} {qty:,} {tender.ticker} · {price}")

    def reconcile_order(self, position, now):
        working = self.working
        inventory = self.inventory
        tender = inventory.tender
        try:
            order = self.venue.order(working.id)
        except RateLimited:
            raise
        except (OSError, HTTPException, RuntimeError) as error:
            self.venue.close()
            if now - working.sent_at < 5:
                self.feed_error(f"Order confirmation delayed · {error}")
                return
            raise
        if (order["ticker"] != tender.ticker or order["action"] != tender.exit_side
                or shares(order["quantity"]) != working.quantity):
            raise RuntimeError("Order details disagree with the submitted order")
        fill = shares(order["quantity_filled"])
        if not 0 <= fill <= working.quantity:
            raise RuntimeError("Order reported an invalid fill quantity")
        status = order["status"]
        if status == "OPEN":
            if working.cancelled_at is None and (self.halted or now - working.sent_at >= 0.45):
                working.cancelled_at = time.monotonic()
                self.venue.cancel(working.id)  # It may already have filled. Recheck the order.
                self.log("CANCEL SENT", f"Working child #{working.id}")
            elif working.cancelled_at is not None and now - working.cancelled_at >= 5:
                raise RuntimeError("Cancellation was not confirmed")
            return
        if status not in TERMINAL:
            raise RuntimeError(f"Unexpected order status {status}")
        if working.terminal_at is None:
            working.terminal_at = now
        expected = inventory.expected - tender.direction * fill
        if position != expected:
            if now - working.terminal_at < 5:
                return
            raise RuntimeError("Position did not reconcile to the terminal order fill")
        self.working = None
        inventory.expected = expected
        if fill:
            fill_price = decimal(order["vwap"])
            if fill_price <= 0:
                raise RuntimeError("RIT reported a non-positive fill VWAP")
            inventory.proceeds += fill * fill_price
            self.log("FILL", f"#{working.id} {fill:,}/{working.quantity:,} @ {fill_price:.4f} · {abs(expected):,} left")
        if expected:
            return
        fees = tender.quantity * (inventory.market_fee + inventory.tender_fee)
        pnl = tender.direction * (inventory.proceeds - tender.quantity * tender.price) - fees
        self.pnl += pnl
        self.completed += 1
        self.inventory = None
        self.log("FLAT", f"Tender #{tender.id} completed · model P&L ${pnl:,.2f}")

    def state(self):
        with self.lock:
            snap = self.snapshot
            fresh = self.is_fresh()
            tenders = []
            if fresh:
                for tender, decision, raw in self.decisions(snap):
                    id_ = raw.get("tender_id")
                    if id_ in self.attempted or id_ in self.skipped:
                        continue
                    if tender:
                        budget = self.strategy.tender_settings(tender).min_heat_ticks
                        tenders.append(dict(id=tender.id, ticker=tender.ticker, side=tender.side,
                                            quantity=tender.quantity, price=tender.price, status=decision.status,
                                            reason=decision.reason, visible=decision.visible_pnl,
                                            stressed=decision.stressed_pnl, depth=decision.stressed.filled, budget=budget,
                                            remaining=max(0, tender.expires - snap["tick"])))
                    else:
                        tenders.append(dict(id=id_, status="REVIEW",
                                            reason="Verify the offer in the RIT client"))
            quotes = self.history[-1] if self.history else {}
            inventory = None
            if self.inventory:
                tender = self.inventory.tender
                inventory = dict(id=tender.id, ticker=tender.ticker, quantity=tender.quantity,
                                 remaining=abs(self.inventory.expected), exit_side=tender.exit_side,
                                 confirmed=self.inventory.confirmed)
            working = None
            if self.working:
                working = dict(id=self.working.id, quantity=self.working.quantity,
                               cancelling=self.working.cancelled_at is not None)
            return dict(mode=self.mode, connection=self.venue.connection_mode,
                        status=self.status, fresh=bool(fresh), armed=self.armed,
                        order_enabled=self.venue.enable_orders, halted=self.halted, fault=self.fault,
                        entry_mode=self.entry_mode, heat=self.heat, period=snap["period"] if snap else None,
                        approval=self.approval[0].id if self.approval else None,
                        inventory=inventory, working=working, recording=self.record and self.mode == "rit",
                        tick=snap["tick"] if snap else 0, end_tick=snap["end_tick"] if snap else 300,
                        case_name=snap["case_name"] if snap else "Rotman LT3", positions=snap["positions"] if snap else {},
                        open_orders=snap["open_orders"] if snap else 0, tenders=tenders, quotes=quotes,
                        pnl=self.pnl, completed=self.completed, history=list(self.history), events=list(self.events),
                        strategy=asdict(self.strategy), revision=self.revision)
