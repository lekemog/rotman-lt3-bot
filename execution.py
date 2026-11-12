"""Tender entry and position-driven unwind for the Rotman simulator.

The desk never keeps its own idea of what the inventory should be. RIT's
reported position is the inventory, and every order the desk sends reduces it.
One child per ticker works at a time. A new child is sent only after the
previous one is terminal and the reported position reflects its fill, or has
been stable long enough to be taken as the truth.
"""

from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal
from http.client import HTTPException
import json
from statistics import pstdev
from threading import RLock
import time

from rotman_lt3 import (
    CAP, CENT, ChildOrder, RateLimited, Rejected, RITReader, Settings, decimal, evaluate_all,
    exit_levels, mids, plan_child, rules_for, shares,
)


Strategy = Settings
UNSURE = (OSError, HTTPException, RuntimeError, ValueError, KeyError, TypeError, ArithmeticError)
STRATEGY_FIELDS = {
    "market_fee": (0, 1), "tender_fee": (0, 1), "depth_haircut": (0, 90), "adverse_move": (0, 1),
    "refill": (0, 100), "min_edge": (0, 1), "patience": (0, 5), "decision_ticks": (2, 20),
    "min_heat_ticks": (5, 150), "ticks_per_child": (1, 20), "exit_reserve_ticks": (0, 60),
    "sweep_band": (1, 200), "limit_slippage_ticks": (0, 10), "rest_ms": (200, 5000),
    "market_closeout_ticks": (0, 60),
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
    def __init__(self, port, enable_orders):
        super().__init__("client", port)
        self.enable_orders = enable_orders

    def close(self):
        self.connection.close()

    def write(self, method, endpoint, **params):
        if not self.enable_orders:
            raise Rejected("Order submission is disabled for this session")
        return self.request(method, endpoint, **params)

    def accept(self, tender):
        return self.write("POST", f"tenders/{tender.id}")[0].get("success") is True

    def submit(self, ticker, side, quantity, limit):
        params = dict(ticker=ticker, action=side, quantity=quantity, type="MARKET" if limit is None else "LIMIT")
        if limit is not None:
            params["price"] = format(limit, "f")
        order, response = self.write("POST", "orders", **params)
        order["wait"] = max(0.0, float(decimal(response.getheader("X-Wait-Until") or 0)))
        return order

    def order(self, id_):
        return self.get(f"orders/{id_}")

    def cancel(self, id_):
        return self.write("DELETE", f"orders/{id_}")[0].get("success") is True

    def cancel_ticker(self, ticker):
        self.write("POST", "commands/cancel", ticker=ticker)
        return True


@dataclass
class Leg:
    """One working child order and the position it was sent against."""
    id: int
    side: str
    quantity: int
    before: int
    sent_at: float
    cancel_at: float | None = None
    fill: int | None = None
    done_at: float = 0.0


@dataclass
class Unsure:
    """A ticker whose last order request had no definite answer."""
    since: float
    position: int
    cancelled: bool = False


class Desk:
    def __init__(self, venue, mode, state_dir=None, record=False, clock=time.monotonic):
        self.venue = venue
        self.mode = mode
        self.state_dir = state_dir
        self.record = record
        self.clock = clock
        self.lock = RLock()
        self.strategy = Strategy()
        self.revision = 1
        self.events = deque(maxlen=80)
        self.history = deque(maxlen=180)
        self.snapshot = None
        self.read_at = 0.0
        self.status = "Connecting"
        self.armed = False       # Accept tenders that pass the model.
        self.unwinding = False   # Flatten whatever position RIT reports.
        self.halted = False
        self.alert = ""
        self.legs = {}
        self.unsure = {}
        self.pending = None
        self.next_submit = {}
        self.attempted = set()
        self.accepted = 0
        self.base_nlv = None
        if state_dir:
            try:
                saved = json.loads((state_dir / "strategy.json").read_text(encoding="utf-8"))
                self.strategy = strategy_from_dict(saved)
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, ArithmeticError):
                self.log("STRATEGY", "The saved profile does not match this version. Defaults loaded.")

    def log(self, event, message):
        row = dict(time=time.strftime("%H:%M:%S"), event=event, message=message)
        if self.snapshot:
            row["tick"] = self.snapshot["tick"]
        self.events.appendleft(row)
        if not self.state_dir:
            return
        try:
            with (self.state_dir / "activity.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass  # Keep the on-screen log and the unwind active if disk is unavailable.

    def save_strategy(self, data):
        strategy = strategy_from_dict(data)
        with self.lock:
            if self.state_dir:
                path = self.state_dir / "strategy.json"
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(asdict(strategy), default=str, indent=2) + "\n", encoding="utf-8")
                temp.replace(path)
            self.strategy = strategy
            self.revision += 1
            self.log("STRATEGY", f"Applied revision {self.revision}")

    def new_heat(self):
        self.legs.clear()
        self.unsure.clear()
        self.next_submit.clear()
        self.pending = None
        self.attempted.clear()
        self.history.clear()
        self.accepted = 0
        self.base_nlv = None

    def command(self, action, data):
        with self.lock:
            if action == "restart" and self.mode == "rehearsal":
                self.venue.restart(data.get("scenario"))
                self.armed = self.unwinding = self.halted = False
                self.alert = ""
                self.snapshot = None
                self.new_heat()
                self.log("REHEARSAL", "Started a fresh heat")
                return
            if action == "halt":
                self.armed = self.unwinding = False
                self.halted = True
                self.log("STOPPED", "No new tenders or orders. Working children are being cancelled."
                                    " Any remaining inventory is yours to manage in RIT.")
                return
            if action not in ("arm", "unwind"):
                raise ValueError("Unknown desk action")
            if not self.venue.enable_orders:
                raise ValueError("This RIT session is in monitor mode")
            if self.mode == "rit" and data.get("api_orders_enabled") is not True:
                raise ValueError("Confirm API Orders is enabled and automation is permitted for this heat")
            self.halted = False
            self.unwinding = True
            self.armed = action == "arm"
            if self.armed:
                self.log("ARMED", "Accepting tenders that pass the model and unwinding every position")
            else:
                self.log("UNWIND ONLY", "Tender decisions are yours. The desk flattens whatever RIT reports.")

    def poll(self):
        # Hold the lock across the snapshot and writes. Commands and strategy
        # revisions apply between complete iterations and never race an order.
        with self.lock:
            try:
                status, snapshot = self.venue.snapshot(200)
            except RateLimited:
                self.status = "RIT rate limit · waiting for a fresh snapshot"
                raise
            except UNSURE as error:
                self.venue.close()
                self.status = f"RIT data unavailable · {error}"
                return
            self.status = status
            if snapshot is None:
                return
            now = self.clock()
            previous = self.snapshot
            self.snapshot, self.read_at = snapshot, now
            if previous and (previous["period"] != snapshot["period"] or snapshot["tick"] < previous["tick"]):
                self.new_heat()
                state = "armed" if self.armed else "unwind only" if self.unwinding else "standing by"
                self.log("NEW HEAT", f"Heat clock restarted. The desk is {state}.")
            try:
                self.update_history(snapshot)
                snapshot["sigma"] = self.sigma()
                if self.base_nlv is None and snapshot.get("nlv") is not None:
                    self.base_nlv = decimal(snapshot["nlv"])
            except (ValueError, KeyError, TypeError, ArithmeticError):
                self.status = "RIT supplied an invalid order book"
                return
            if self.record and self.state_dir:
                try:
                    with (self.state_dir / "snapshots.jsonl").open("a", encoding="utf-8") as out:
                        out.write(json.dumps(snapshot, default=str) + "\n")
                except OSError:
                    self.record = False
                    self.log("RECORDING", "Snapshot recording stopped after a write error")
            self.step(snapshot, now)

    def update_history(self, snapshot):
        tick = snapshot["tick"]
        if self.history and self.history[-1]["tick"] == tick:
            return
        row = {"tick": tick}
        for ticker, mid in mids(snapshot).items():
            row[ticker] = None if mid is None else float(mid)
        self.history.append(row)

    def sigma(self):
        """Standard deviation of one-tick mid changes seen so far in this heat."""
        rows = list(self.history)
        out = {}
        for ticker in CAP:
            moves = [after[ticker] - before[ticker] for before, after in zip(rows, rows[1:])
                     if after["tick"] - before["tick"] == 1
                     and before[ticker] is not None and after[ticker] is not None]
            if len(moves) >= 12:
                out[ticker] = f"{pstdev(moves):.4f}"
        return out

    def decisions(self, snapshot):
        return evaluate_all(snapshot, self.strategy)

    def step(self, snapshot, now):
        alerts = []
        limited = None
        pick = self.choose(snapshot, now)
        for ticker in CAP:
            try:
                # Inventory is about to change in the picked ticker. Let its
                # working child finish and send nothing new until the tender is in.
                self.manage(ticker, snapshot, now, alerts, hold=bool(pick) and pick[0].ticker == ticker)
            except RateLimited as error:
                limited = error
            except UNSURE as error:
                self.venue.close()
                alerts.append(f"{ticker}: {error}")
        if pick:
            try:
                self.enter(*pick, snapshot, now)
            except RateLimited as error:
                limited = error
        self.alert = " · ".join(alerts)
        if limited:
            raise limited

    def settle(self, ticker, leg, position, now):
        """Follow a child to a terminal state and a matching position."""
        if leg.fill is None:
            try:
                order = self.venue.order(leg.id)
            except RateLimited:
                raise
            except UNSURE:
                self.venue.close()
                if now - leg.sent_at >= 5:
                    del self.legs[ticker]
                    self.unsure[ticker] = Unsure(now, position)
                    self.log("UNCONFIRMED", f"{ticker} child #{leg.id} could not be read. Cancelling and re-reading.")
                return False
            if order["status"] == "OPEN":
                rest = self.strategy.rest_ms / 1000
                stale = leg.cancel_at is None and (self.halted or not self.unwinding or now - leg.sent_at >= rest)
                if stale or (leg.cancel_at is not None and now - leg.cancel_at >= 2):
                    leg.cancel_at = now
                    try:
                        self.venue.cancel(leg.id)  # It may already have filled. The next read decides.
                    except Rejected:
                        pass
                return False
            self.finish(ticker, leg, order, now)
        expected = leg.before + (leg.fill if leg.side == "BUY" else -leg.fill)
        if position != expected:
            if now - leg.done_at < 2:
                return False
            self.log("ADOPTED", f"{ticker} position is {position:+,}, not the {expected:+,} this child implied."
                                " Working from the reported position.")
        del self.legs[ticker]
        return True

    def finish(self, ticker, leg, order, now):
        fill = shares(order.get("quantity_filled") or 0)
        if not 0 <= fill <= leg.quantity:
            raise RuntimeError("Order reported an invalid fill quantity")
        leg.fill, leg.done_at = fill, now
        if fill:
            self.log("FILL", f"#{leg.id} {leg.side} {fill:,}/{leg.quantity:,} {ticker} @ {decimal(order['vwap']):.4f}")
        else:
            self.log("NO FILL", f"#{leg.id} {leg.side} {leg.quantity:,} {ticker}")

    def manage(self, ticker, snapshot, now, alerts, hold=False):
        position = snapshot["positions"][ticker]
        leg = self.legs.get(ticker)
        foreign = [order for order in snapshot["orders"]
                   if order["ticker"] == ticker and not (leg and order["id"] == leg.id)]
        if leg and not self.settle(ticker, leg, position, now):
            return
        doubt = self.unsure.get(ticker)
        if doubt:
            if foreign and (not doubt.cancelled or now - doubt.since >= 2):
                self.venue.cancel_ticker(ticker)
                doubt.cancelled = True
            if foreign or position != doubt.position:
                doubt.position, doubt.since = position, now
            elif now - doubt.since >= 1.5:
                del self.unsure[ticker]
                self.log("CONFIRMED", f"{ticker} is quiet at {position:+,}. Resuming.")
            if ticker in self.unsure:
                alerts.append(f"{ticker}: confirming the position after an unanswered request")
                return
        if not position or not self.unwinding or hold:
            return
        if foreign:
            alerts.append(f"{ticker}: an order the desk did not send is working, so the desk is waiting")
            return
        if now < self.next_submit.get(ticker, 0):
            return
        child = self.child(ticker, position, snapshot)
        if child is None:
            return
        try:
            order = self.venue.submit(ticker, child.side, child.quantity, child.limit)
            order_id = shares(order["order_id"])
        except RateLimited as error:
            self.next_submit[ticker] = now + error.wait
            return
        except Rejected as error:
            self.next_submit[ticker] = now + 1
            self.log("REJECTED", f"{child.side} {child.quantity:,} {ticker} · {error}")
            return
        except UNSURE as error:
            self.venue.close()
            self.unsure[ticker] = Unsure(now, position)
            self.log("UNCONFIRMED", f"{child.side} {child.quantity:,} {ticker} had no definite answer ({error})."
                                    " Cancelling and re-reading the position before sending again.")
            return
        self.next_submit[ticker] = now + float(order.get("wait") or 0)
        leg = self.legs[ticker] = Leg(order_id, child.side, child.quantity, position, now)
        price = "market" if child.limit is None else f"limit {child.limit:.2f}"
        self.log("ORDER", f"#{order_id} {child.side} {child.quantity:,} {ticker} · {price}")
        if order.get("status") in ("TRANSACTED", "CANCELLED"):
            self.finish(ticker, leg, order, now)

    def child(self, ticker, position, snapshot):
        strategy = self.strategy
        cap, _ = rules_for(snapshot, ticker, strategy)
        remaining = snapshot["end_tick"] - snapshot["tick"]
        if remaining <= 0:
            return None
        if remaining <= strategy.market_closeout_ticks:
            side = "SELL" if position > 0 else "BUY"
            if not exit_levels(snapshot["books"][ticker], side):
                return None
            return ChildOrder(ticker, side, min(abs(position), cap), None)
        children = (abs(position) + cap - 1) // cap
        urgent = remaining - strategy.market_closeout_ticks <= children * strategy.ticks_per_child
        return plan_child(ticker, position, snapshot["books"][ticker], cap, CENT * strategy.sweep_band,
                          CENT * strategy.limit_slippage_ticks, urgent)

    def choose(self, snapshot, now):
        """Pick at most one tender to accept on this poll."""
        if self.pending:
            tender_id, ticker, before, sent_at = self.pending
            gone = all(row.get("tender_id") != tender_id for row in snapshot["tenders"])
            if not (gone and snapshot["positions"][ticker] != before) and now - sent_at < 3:
                return None
            self.pending = None
        if not self.armed or self.halted or self.unsure:
            return None
        candidates = [(tender, decision) for tender, decision, _ in self.decisions(snapshot)
                      if decision and decision.status == "ACCEPT" and tender.id not in self.attempted]
        return max(candidates, key=lambda item: (item[1].stressed_pnl, -item[0].expires), default=None)

    def enter(self, tender, decision, snapshot, now):
        leg = self.legs.get(tender.ticker)
        if leg:
            # A netting tender must never leave a child that could cross flat.
            if leg.fill is None and leg.cancel_at is None:
                leg.cancel_at = now
                try:
                    self.venue.cancel(leg.id)
                except (Rejected, RateLimited):
                    pass
                except UNSURE:
                    self.venue.close()
            return
        self.attempted.add(tender.id)
        offer = f"#{tender.id} {tender.side} {tender.quantity:,} {tender.ticker} @ {tender.price:.2f}"
        before = snapshot["positions"][tender.ticker]
        try:
            accepted = self.venue.accept(tender)
        except RateLimited:
            self.attempted.discard(tender.id)
            raise
        except Rejected as error:
            self.log("NOT ACCEPTED", f"{offer} · {error}")
            return
        except UNSURE as error:
            self.venue.close()
            self.pending = tender.id, tender.ticker, before, now
            self.log("UNCONFIRMED", f"{offer} had no definite answer ({error}). If RIT took it,"
                                    " the position will appear and be unwound.")
            return
        if not accepted:
            self.log("NOT ACCEPTED", f"{offer} · RIT did not confirm")
            return
        self.pending = tender.id, tender.ticker, before, now
        self.accepted += 1
        per_share = decision.stressed_pnl / tender.quantity * 100
        self.log("ACCEPTED", f"{offer} · model ${decision.stressed_pnl:,.0f} ({per_share:+.1f}¢/sh)"
                             f" · {decision.reason}")

    def state(self):
        with self.lock:
            snap = self.snapshot
            fresh = bool(snap and self.status == "ACTIVE" and self.clock() - self.read_at < 2)
            tenders = []
            if fresh:
                for tender, decision, raw in self.decisions(snap):
                    if tender:
                        edge = (None if decision.stressed_pnl is None
                                else decision.stressed_pnl / tender.quantity * 100)
                        tenders.append(dict(id=tender.id, ticker=tender.ticker, side=tender.side,
                                            quantity=tender.quantity, price=tender.price, status=decision.status,
                                            reason=decision.reason, visible=decision.visible_pnl,
                                            stressed=decision.stressed_pnl, edge=edge, netted=decision.netted,
                                            exit_quantity=decision.exit_quantity,
                                            taken=tender.id in self.attempted,
                                            remaining=max(0, tender.expires - snap["tick"])))
                    else:
                        tenders.append(dict(id=raw.get("tender_id"), status="REVIEW",
                                            reason="Verify the offer in the RIT client"))
            quotes = self.history[-1] if self.history else {}
            nlv = decimal(snap["nlv"]) if snap and snap.get("nlv") is not None else None
            rules = {ticker: rules_for(snap, ticker, self.strategy) for ticker in CAP} if snap else {}
            return dict(mode=self.mode, status=self.status, fresh=fresh, armed=self.armed,
                        unwinding=self.unwinding, order_enabled=self.venue.enable_orders, halted=self.halted,
                        alert=self.alert, tick=snap["tick"] if snap else 0,
                        end_tick=snap["end_tick"] if snap else 300,
                        case_name=snap["case_name"] if snap else "Rotman LT3",
                        positions=snap["positions"] if snap else {},
                        open_orders=snap["open_orders"] if snap else 0, tenders=tenders, quotes=quotes,
                        nlv=nlv, nlv_change=None if nlv is None or self.base_nlv is None else nlv - self.base_nlv,
                        fines=snap.get("fines") if snap else None, accepted=self.accepted,
                        sigma=(snap.get("sigma") or {}) if snap else {},
                        rules={ticker: dict(cap=cap, fee=fee) for ticker, (cap, fee) in rules.items()},
                        limits=(snap.get("limits") or {}) if snap else {},
                        history=list(self.history), events=list(self.events),
                        strategy=asdict(self.strategy), revision=self.revision)
