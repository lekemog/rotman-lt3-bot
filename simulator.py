"""Synthetic LT3 market used by the rehearsal dashboard and the backtest.

Everything here is an assumption about a market we have not measured yet. The
book follows an exogenous random-walk mid. Liquidity taken by the desk leaves a
hole at the top of that side of the book, and a scenario-specific share of the
hole closes each tick. Resting limit orders fill at their own price when the
book trades up or down to them. Use `calibrate.py` on recorded practice data to
see how far the real simulator is from these assumptions.
"""

from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from random import Random
import time

from rotman_lt3 import CAP, CENT, GROSS_LIMIT, NET_LIMIT, ZERO, Rejected, exit_levels, sweep


END = 300
FEE = Decimal("0.02")
TICKERS = tuple(CAP)


@dataclass(frozen=True)
class Scenario:
    name: str
    depth: float = 1.0
    vol: float = 1.25    # Cents of standard deviation per tick before rounding.
    refill: float = 0.5  # Share of consumed depth that returns each tick.
    poll: float = 0.5    # Seconds between desk polls in the backtest.
    shocks: bool = False
    overlap: bool = False
    drought: bool = False
    paired: bool = False
    tender_fee: Decimal = ZERO


SCENARIOS = (
    Scenario("Normal"),
    Scenario("Slow refill", refill=0.12),
    Scenario("Thin book", depth=0.48),
    Scenario("Volatile", vol=3.5),
    Scenario("Adverse jump and depth loss", vol=2.4, shocks=True),
    Scenario("Slow polling", vol=2.0, poll=3.0),
    Scenario("Overlapping tenders", overlap=True),
    Scenario("Opposing tenders", paired=True),
    Scenario("Late depth drought", drought=True),
    Scenario("Fee on both legs", tender_fee=2 * CENT),
)


class Heat:
    """One policy-independent path of mids, displayed depth and tender offers."""

    def __init__(self, scenario, seed):
        rng = Random(seed)
        self.scenario = scenario
        self.mid = {"CRZY": [1000], "TAME": [2500]}
        self.fades = {ticker: [] for ticker in TICKERS}
        self.tenders = []

        arrivals = [25, 75, 125, 180, 233]
        if scenario.overlap or scenario.paired:
            arrivals = [39, 40, 42, 131, 133]
        if scenario.drought:
            arrivals = [35, 95, 150, 225, 270]

        specs = []
        jumps = {ticker: {} for ticker in TICKERS}
        for id_, base_tick in enumerate(arrivals, 1):
            close = scenario.overlap or scenario.paired
            tick = base_tick + (rng.randint(-1, 1) if close else rng.randint(-5, 5))
            ticker = rng.choice(TICKERS)
            side = rng.choice(("BUY", "SELL"))
            if scenario.paired and specs and id_ in (2, 5):
                ticker = specs[-1][2]
                side = "SELL" if specs[-1][3] == "BUY" else "BUY"
            direction = 1 if side == "BUY" else -1
            quantity = rng.randint(33, 96 if ticker == "CRZY" else 74) * 1000
            concession = rng.randint(4, 24)
            specs.append((id_, tick, ticker, side, direction, quantity, concession))
            if scenario.shocks:
                shock = tick + rng.randint(1, 5)
                jumps[ticker][shock] = jumps[ticker].get(shock, 0) - direction * rng.randint(6, 24)
                self.fades[ticker].append((shock, shock + rng.randint(7, 16), 0.11))
            if scenario.drought and id_ == 5:
                self.fades[ticker].append((tick + rng.randint(2, 4), END, rng.uniform(0.0, 0.025)))

        for ticker in TICKERS:
            path = self.mid[ticker]
            for tick in range(1, END + 1):
                path.append(max(100, path[-1] + round(rng.gauss(0, scenario.vol)) + jumps[ticker].get(tick, 0)))
        for id_, tick, ticker, side, direction, quantity, concession in sorted(specs, key=lambda spec: spec[1]):
            price = (self.mid[ticker][tick] - direction * concession) * CENT
            self.tenders.append(dict(tender_id=id_, ticker=ticker, action=side, quantity=quantity,
                                     price=price, tick=tick, expires=min(tick + 30, END), is_fixed_bid=True))

        # Depth varies coherently over a few seconds. Each price level also has
        # its own independent noise. Seeds make future books policy-independent.
        self.seed = seed
        self.depth_scale = {ticker: [rng.uniform(0.65, 1.35) for _ in range(END // 4 + 2)]
                            for ticker in TICKERS}
        self.levels = cache(self._levels)

    def _levels(self, ticker, tick):
        rng = Random(self.seed * 1009 + tick * 3 + TICKERS.index(ticker))
        mid = self.mid[ticker][tick]
        scale = self.scenario.depth * self.depth_scale[ticker][tick // 4]
        for start, stop, multiplier in self.fades[ticker]:
            if start <= tick < stop:
                scale *= multiplier
        spread = 1 if scale >= 0.65 else 2
        depth_by_level = (21_000, 19_000, 17_000, 15_000, 13_000, 11_000, 9_000, 7_000)
        if ticker == "TAME":
            depth_by_level = (16_000, 14_000, 12_000, 11_000, 10_000, 8_000, 7_000, 6_000)
        return {
            side: [((mid + direction * (spread + level)) * CENT, int(depth * scale * rng.uniform(0.55, 1.45)))
                   for level, depth in enumerate(depth_by_level)]
            for side, direction in (("bids", -1), ("asks", 1))
        }


class SimVenue:
    """The venue interface the desk trades against, backed by a synthetic heat."""

    enable_orders = True

    def __init__(self, scenario="Normal", seed=20261112, clock=time.monotonic, speed=1.0, start_tick=0):
        self.clock, self.speed, self.start_tick = clock, speed, start_tick
        self.seed, self.round = seed, 0
        self.heat = None
        self.restart(scenario)

    def restart(self, scenario):
        profile = next((item for item in SCENARIOS if item.name == scenario), None)
        if profile is None:
            raise ValueError("Unknown rehearsal scenario")
        self.close()
        self.round += 1
        self.heat = Heat(profile, self.seed + self.round)
        self.started = self.clock()
        self.positions = dict.fromkeys(TICKERS, 0)
        self.cash = ZERO
        self.accepted = set()
        self.orders = {}
        self.holes = {}
        self.id = 0
        self.seen_tick = -1

    def close(self):
        if self.heat:
            self.heat.levels.cache_clear()

    def tick(self):
        return min(END, self.start_tick + int((self.clock() - self.started) * self.speed))

    def levels(self, ticker, side, tick):
        """Displayed levels on one side after the desk's own footprint."""
        missing, at = self.holes.get((ticker, side), (0, tick))
        missing = int(missing * (1 - self.heat.scenario.refill) ** max(0, tick - at))
        out = []
        for price, size in self.heat.levels(ticker, tick)[side]:
            taken = min(missing, size)
            missing -= taken
            if size > taken:
                out.append((price, size - taken))
        return out

    def book(self, ticker, tick):
        return {side: [dict(price=price, quantity=size, quantity_filled=0)
                       for price, size in self.levels(ticker, side, tick)]
                for side in ("bids", "asks")}

    def trade(self, order, tick, resting):
        """Fill an order against the book. A resting order trades at its limit."""
        ticker, selling = order["ticker"], order["action"] == "SELL"
        side = "bids" if selling else "asks"
        levels = self.levels(ticker, side, tick)
        limit = order["price"]
        if limit is not None:
            levels = [(price, size) for price, size in levels
                      if (price >= limit if selling else price <= limit)]
        left = order["quantity"] - order["quantity_filled"]
        fill = sweep(levels, left, order["action"])
        if not fill.filled:
            return
        value = fill.filled * limit if resting else fill.value
        missing, at = self.holes.get((ticker, side), (0, tick))
        missing = int(missing * (1 - self.heat.scenario.refill) ** max(0, tick - at))
        self.holes[ticker, side] = missing + fill.filled, tick
        self.cash += (value if selling else -value) - fill.filled * FEE
        self.positions[ticker] += -fill.filled if selling else fill.filled
        order["value"] += value
        order["quantity_filled"] += fill.filled
        order["vwap"] = order["value"] / order["quantity_filled"]
        if order["quantity_filled"] == order["quantity"]:
            order["status"] = "TRANSACTED"

    def advance(self):
        tick = self.tick()
        if tick != self.seen_tick:
            self.seen_tick = tick
            for order in self.orders.values():
                if order["status"] == "OPEN":
                    self.trade(order, tick, resting=True)
        return tick

    def nlv(self, tick):
        return self.cash + sum(position * self.heat.mid[ticker][tick] * CENT
                               for ticker, position in self.positions.items())

    def snapshot(self, depth):
        tick = self.advance()
        if tick >= END:
            return "STOPPED", None
        tenders = []
        for row in self.heat.tenders:
            if row["tick"] <= tick < row["expires"] and row["tender_id"] not in self.accepted:
                theirs = "SELL" if row["action"] == "BUY" else "BUY"
                ours = row["action"]
                preposition = "to" if ours == "BUY" else "from"
                caption = (f"An institution would like to {theirs} {row['quantity']:,} shares of {row['ticker']}"
                           f" {preposition} you at a price of ${row['price']:.2f}. Would you like to {ours} the"
                           f" shares {'from' if ours == 'BUY' else 'to'} them?")
                tenders.append(dict(row, period=self.round, caption=caption))
        working = [dict(id=id_, ticker=order["ticker"], action=order["action"], quantity=order["quantity"],
                        filled=order["quantity_filled"], price=order["price"])
                   for id_, order in self.orders.items() if order["status"] == "OPEN"]
        return "ACTIVE", dict(tick=tick, period=self.round, end_tick=END,
                              case_name=f"Rehearsal · {self.heat.scenario.name}", positions=dict(self.positions),
                              open_orders=len(working), orders=working, tenders=tenders,
                              rules={ticker: dict(cap=cap, fee=str(FEE)) for ticker, cap in CAP.items()},
                              limits=dict(net=NET_LIMIT, gross=GROSS_LIMIT), nlv=self.nlv(tick), fines=0,
                              books={ticker: self.book(ticker, tick) for ticker in TICKERS})

    def accept(self, tender):
        tick = self.tick()
        row = next((row for row in self.heat.tenders if row["tender_id"] == tender.id), None)
        if row is None or tender.id in self.accepted or not row["tick"] <= tick < row["expires"]:
            raise Rejected("The tender is no longer available")
        self.accepted.add(tender.id)
        signed = tender.direction * tender.quantity
        self.positions[tender.ticker] += signed
        self.cash -= signed * tender.price + tender.quantity * self.heat.scenario.tender_fee
        return True

    def submit(self, ticker, side, quantity, limit):
        if not 0 < quantity <= CAP[ticker]:
            raise Rejected("Order size outside the case limits")
        tick = self.advance()
        self.id += 1
        order = dict(order_id=self.id, status="OPEN", ticker=ticker, action=side, quantity=quantity,
                     quantity_filled=0, vwap=None, price=limit, value=ZERO)
        self.orders[self.id] = order
        if tick < END:
            self.trade(order, tick, resting=False)
        if limit is None and order["status"] == "OPEN":
            order["status"] = "CANCELLED"
        return dict(order)

    def order(self, id_):
        self.advance()
        return dict(self.orders[id_])

    def cancel(self, id_):
        if self.orders[id_]["status"] == "OPEN":
            self.orders[id_]["status"] = "CANCELLED"
        return True

    def cancel_ticker(self, ticker):
        for id_, order in self.orders.items():
            if order["ticker"] == ticker:
                self.cancel(id_)
        return True
