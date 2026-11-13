"""Synthetic, event-driven stress tests for rotman_lt3.py.

Place both scripts in one directory. Run `python rotman_lt3_backtest.py`.
Use --heats and --seed to rerun the same experiment with different samples.
Each policy sees the same exogenous book and tender path for a given heat.
One tick is treated as one second. Depth, price paths, arrival and execution
delays are model assumptions. Depth replenishes across ticks. A child order
trades only through executable prices at arrival. Unfilled limit shares are
assumed cancelled after an extra delay, with zero passive fills during that delay.
Residual positions are marked at the last mid and penalized by $1/share.
The simulator models practice execution and does not connect to RIT.
"""

import argparse
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import cache
from math import ceil
from random import Random
from statistics import mean

from rotman_lt3 import (
    CAP, CENT, NET_LIMIT, ZERO, Settings, Tender, evaluate, exit_levels,
    next_unwind, sweep,
)


END = 300
RESIDUAL_FINE = Decimal(1)
TICKERS = tuple(CAP)


@dataclass(frozen=True)
class Scenario:
    name: str
    depth: float = 1.0
    vol: float = 1.25  # Cents of standard deviation per tick before rounding.
    poll: int = 1
    accept_delay: int = 1
    order_delay: int = 1
    cancel_delay: int = 1
    shocks: bool = False
    overlap: bool = False
    drought: bool = False
    tender_fee: Decimal = ZERO


@dataclass(frozen=True)
class Policy:
    name: str
    settings: Settings = Settings()
    fallback_ticks: int = 0
    ticks_per_child: int = 0


@dataclass(frozen=True)
class Result:
    pnl: Decimal
    accepted: int
    partials: int
    expired_requests: int
    residual: int


SCENARIOS = (
    Scenario("Normal"),
    Scenario("Thin book", depth=0.48),
    Scenario("Adverse jump and depth loss", vol=2.4, shocks=True),
    Scenario("Slow polling and fills", vol=2.0, poll=3, accept_delay=3, order_delay=3, cancel_delay=2),
    Scenario("Overlapping tenders", overlap=True),
    Scenario("Late depth drought", drought=True, poll=2, order_delay=2, cancel_delay=2),
    Scenario("Fee on both legs", tender_fee=2 * CENT),
)
POLICIES = (
    Policy("Visible only", replace(Settings(), depth_haircut=0, adverse_move=ZERO)),
    Policy("Starter"),
    Policy("Stricter entry", replace(Settings(), depth_haircut=25, adverse_move=2 * CENT)),
    Policy("Starter + proposed closeout", fallback_ticks=12),
    Policy("Starter + time budget", ticks_per_child=6),
)


class Heat:
    def __init__(self, scenario, seed):
        rng = Random(seed)
        self.scenario = scenario
        self.mid = {"CRZY": [1000], "TAME": [2500]}
        self.fades = {ticker: [] for ticker in TICKERS}
        self.tenders = []

        arrivals = [25, 75, 125, 180, 233]
        if scenario.overlap:
            arrivals = [39, 40, 42, 131, 133]
        if scenario.drought:
            arrivals = [35, 95, 150, 225, 270]

        specs = []
        jumps = {ticker: {} for ticker in TICKERS}
        for id_, base_tick in enumerate(arrivals, 1):
            tick = base_tick + rng.randint(-1, 1) if scenario.overlap else base_tick + rng.randint(-5, 5)
            ticker = rng.choice(TICKERS)
            side = rng.choice(("BUY", "SELL"))
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
            for tick in range(1, END):
                path.append(max(100, path[-1] + round(rng.gauss(0, scenario.vol)) + jumps[ticker].get(tick, 0)))
        for id_, tick, ticker, side, direction, quantity, concession in sorted(specs, key=lambda spec: spec[1]):
            price = (self.mid[ticker][tick] - direction * concession) * CENT
            self.tenders.append((tick, Tender(id_, ticker, side, quantity, price, min(tick + 30, END), 1)))

        # Depth varies coherently over a few seconds. Each price level also has
        # its own independent noise. Seeds make future books policy-independent.
        self.seed = seed
        self.depth_scale = {}
        for ticker in TICKERS:
            self.depth_scale[ticker] = [rng.uniform(0.65, 1.35) for _ in range(END // 4 + 1)]
        self.book = cache(self._book)

    def _book(self, ticker, tick):
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
            side: [dict(price=(mid + direction * (spread + level)) * CENT,
                        quantity=int(depth * scale * rng.uniform(0.55, 1.45)), quantity_filled=0)
                   for level, depth in enumerate(depth_by_level)]
            for side, direction in (("bids", -1), ("asks", 1))
        }


def select_tender(tick, tenders, get_book, policy):
    choices = []
    for tender in tenders:
        settings = policy.settings
        if policy.ticks_per_child:
            chunks = ceil(tender.quantity / CAP[tender.ticker])
            budget = 10 + chunks * policy.ticks_per_child
            settings = replace(settings, min_heat_ticks=max(settings.min_heat_ticks, budget))
        snapshot = dict(tick=tick, period=1, end_tick=END, positions=dict.fromkeys(TICKERS, 0),
                        open_orders=0, books={tender.ticker: get_book(tender.ticker, tick)})
        decision = evaluate(tender, snapshot, settings)
        if decision.status == "ACCEPT":
            choices.append((decision.stressed_pnl, -tender.expires, tender))
    return max(choices, key=lambda choice: choice[:2])[-1] if choices else None


def arrived_order(book, tender, quantity, limit):
    levels = exit_levels(book, tender.exit_side)
    if limit is not None:
        if tender.exit_side == "SELL":
            levels = [(price, size) for price, size in levels if price >= limit]
        else:
            levels = [(price, size) for price, size in levels if price <= limit]
    return sweep(levels, quantity, tender.exit_side)


def run_heat(heat, policy):
    scenario = heat.scenario
    accepted_ids = set()
    cash = ZERO
    active, accept_at, pending_order = None, None, None
    position = 0
    partials, expired_requests, next_action = 0, 0, 0
    for tick in range(END):
        if accept_at == tick:
            accept_at = None
            if tick < active.expires:
                position = active.direction * active.quantity
                assert abs(position) <= NET_LIMIT
                cash -= position * active.price + active.quantity * scenario.tender_fee
                accepted_ids.add(active.id)
            else:
                expired_requests += 1
                active = None

        if pending_order and pending_order[0] == tick:
            _, quantity, limit = pending_order
            pending_order = None
            assert active and active.id in accepted_ids
            assert 0 < quantity <= min(abs(position), CAP[active.ticker])
            fill = arrived_order(heat.book(active.ticker, tick), active, quantity, limit)
            cash += active.direction * fill.value - fill.filled * Settings.market_fee
            position -= active.direction * fill.filled
            if fill.filled < quantity:
                partials += int(fill.filled > 0)
                if limit is not None:
                    next_action = tick + scenario.cancel_delay
            if not position:
                active = None

        if tick % scenario.poll or tick < next_action or accept_at is not None or pending_order:
            continue

        if active:
            if END - tick <= policy.fallback_ticks:
                pending_order = (tick + scenario.order_delay, min(abs(position), CAP[active.ticker]), None)
                continue
            child = next_unwind(active, position, heat.book(active.ticker, tick), open_orders=0)
            if child:
                pending_order = (tick + scenario.order_delay, child.quantity, child.limit)
            continue

        eligible = [tender for start, tender in heat.tenders
                    if start <= tick < tender.expires and tender.id not in accepted_ids]
        active = select_tender(tick, eligible, heat.book, policy)
        if active:
            accept_at = tick + scenario.accept_delay

    if position:
        cash += position * heat.mid[active.ticker][-1] * CENT - abs(position) * RESIDUAL_FINE
    return Result(cash, len(accepted_ids), partials, expired_requests, abs(position))


def deterministic_tests():
    from rotman_lt3 import demo_snapshot, parse_tender

    snapshot = demo_snapshot()
    marginal, _, strong = map(parse_tender, snapshot["tenders"])
    assert evaluate(marginal, snapshot, Settings()).stressed_pnl == Decimal("-279")
    assert evaluate(strong, snapshot, Settings()).status == "ACCEPT"
    empty = dict(bids=[], asks=[])
    assert next_unwind(strong, 50_000, empty, open_orders=0) is None

    book = dict(bids=[dict(price="9.95", quantity=20_000, quantity_filled=15_000),
                      dict(price="9.94", quantity=50_000, quantity_filled=0)], asks=[])
    fill = arrived_order(book, strong, 25_000, Decimal("9.95"))
    assert fill.filled == 5_000 and fill.value == Decimal("49_750")
    assert next_unwind(strong, 45_000, book, open_orders=1) is None
    assert next_unwind(strong, 45_000, book, open_orders=0).quantity == 25_000
    assert arrived_order(book, strong, 25_000, Decimal("9.96")).filled == 0

    short = replace(strong, side="SELL", ticker="TAME", price=Decimal("10.10"))
    assert next_unwind(short, -50_000, snapshot["books"]["TAME"], 0).quantity == 10_000
    assert next_unwind(short, -4_000, snapshot["books"]["TAME"], 0).quantity == 4_000
    assert next_unwind(short, 1_000, snapshot["books"]["TAME"], 0) is None
    assert evaluate(strong, dict(snapshot, positions=dict(CRZY=60_000, TAME=0)), Settings()).status == "WAIT"
    assert evaluate(strong, dict(snapshot, positions=dict(CRZY=130_000, TAME=-120_000)), Settings()).reason.startswith("Projected")
    assert evaluate(strong, dict(snapshot, tick=123), Settings()).status == "PASS"
    assert evaluate(replace(strong, expires=END), dict(snapshot, tick=END - 20), Settings()).status == "PASS"
    assert evaluate(replace(strong, period=2), snapshot, Settings()).status == "EXPIRED"
    assert evaluate(marginal, snapshot, replace(Settings(), tender_fee=2 * CENT)).visible_pnl == Decimal("-310")

    class LateTape:
        scenario = Scenario("Scripted")
        tenders = [(275, replace(strong, expires=END))]
        mid = {"CRZY": [990] * END}

        @staticmethod
        def book(ticker, tick):
            if tick in (275, 276):
                return dict(bids=[dict(price="10.00", quantity=70_000, quantity_filled=0)], asks=[])
            if tick >= 288:
                return dict(bids=[dict(price="9.99" if tick % 2 == 0 else "9.98",
                                       quantity=25_000 if tick % 2 else 1_000, quantity_filled=0)], asks=[])
            return empty

    # At every post-drought quote, a better price is thin and the next tick
    # either gaps through the limit or displays only 1,000 executable shares.
    # A working-order cancel delay can phase-lock the limit-only strategy.
    before = run_heat(LateTape(), POLICIES[1])
    after = run_heat(LateTape(), POLICIES[3])
    assert before.residual > 0 and after.residual == 0, (before, after)

    class ExpiryTape:
        scenario = Scenario("Scripted expiry race", accept_delay=3)
        tenders = [(100, replace(strong, expires=103))]
        mid = {"CRZY": [990] * END}

        @staticmethod
        def book(ticker, tick):
            return dict(bids=[dict(price="10.00", quantity=80_000, quantity_filled=0)], asks=[])

    expired = run_heat(ExpiryTape(), POLICIES[1])
    assert expired.expired_requests == 1 and expired.accepted == 0 and expired.pnl == 0
    return before, after


def summarize(results):
    ordered = sorted(result.pnl for result in results)
    size = len(results)
    return (mean(ordered), ordered[ceil(size * 0.05) - 1],
            100 * sum(result.pnl < 0 for result in results) / size,
            mean(result.accepted for result in results),
            sum(result.partials for result in results),
            sum(result.expired_requests for result in results),
            sum(bool(result.residual) for result in results))


def print_summary(scenario, policy, results):
    pnl, tail, loss, accepted, partials, expired, residual = summarize(results)
    print(f"{scenario:<27} {policy:<28} {pnl:>9,.0f} {tail:>9,.0f} {loss:>6.1f}%"
          f" {accepted:>7.2f} {partials:>8} {expired:>8} {residual:>8}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heats", type=int, default=250, help="Heats per scenario")
    parser.add_argument("--seed", type=int, default=20261111)
    parser.add_argument("--checks-only", action="store_true")
    args = parser.parse_args()
    if args.heats < 1:
        parser.error("Use at least one heat per scenario")
    before, after = deterministic_tests()
    print("Deterministic cases passed. Scripted late liquidity recovery left"
          f" {before.residual:,} shares on the starter and {after.residual:,} on proposed closeout.")
    if args.checks_only:
        return
    print(f"{args.heats * len(SCENARIOS):,} synthetic heats per main policy, seed {args.seed}.")
    print(f"{'Scenario':<27} {'Policy':<28} {'Mean $':>9} {'5th % $':>9} {'Losses':>7}"
          f" {'Acc/Ht':>7} {'Partials':>8} {'Expired':>8} {'Unflat':>8}")
    pooled = {policy.name: [] for policy in POLICIES}
    for scenario in SCENARIOS:
        results = {policy.name: [] for policy in POLICIES}
        for i in range(args.heats):
            heat = Heat(scenario, args.seed + i)
            for policy in POLICIES:
                results[policy.name].append(run_heat(heat, policy))
            if scenario.tender_fee:
                adjusted = Policy("Starter, correct fee", replace(Settings(), tender_fee=scenario.tender_fee))
                results.setdefault(adjusted.name, []).append(run_heat(heat, adjusted))
            heat.book.cache_clear()
        for policy_name, samples in results.items():
            print_summary(scenario.name, policy_name, samples)
            if policy_name in pooled:
                pooled[policy_name].extend(samples)
    for policy_name, samples in pooled.items():
        print_summary("Pooled, equally weighted", policy_name, samples)


if __name__ == "__main__":
    main()
