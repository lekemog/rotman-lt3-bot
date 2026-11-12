"""Run the real desk against synthetic heats.

    python backtest.py --heats 40

Each policy trades the same exogenous mid path, displayed depth and tender
offers for a given heat. Fills come from `simulator.py`, so the numbers compare
policies under stated assumptions. They are no forecast of RIT results. Shares
left at the end of a heat are marked at the last mid and fined $1 each.
"""

import argparse
from dataclasses import replace
from decimal import Decimal
from math import ceil
from statistics import mean

from execution import Desk, Strategy
from rotman_lt3 import ZERO
from simulator import END, SCENARIOS, SimVenue


RESIDUAL_FINE = Decimal(1)
POLICIES = {
    "Full-sweep entry": replace(Strategy(), refill=0),
    "Default": Strategy(),
    "Loose entry": replace(Strategy(), refill=80, depth_haircut=0, adverse_move=ZERO),
    "Default, one-shot sweeps": replace(Strategy(), sweep_band=200),
    "Accept at once": replace(Strategy(), patience=ZERO),
    "Patience 2 sigma": replace(Strategy(), patience=Decimal(2)),
    "Default, no closeout": replace(Strategy(), market_closeout_ticks=0),
}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def run_heat(scenario, seed, strategy):
    clock = Clock()
    venue = SimVenue(scenario.name, seed, clock)
    desk = Desk(venue, "rehearsal", clock=clock)
    desk.strategy = strategy
    desk.poll()
    desk.command("arm", {})
    while clock.now < END:
        desk.poll()
        clock.now += scenario.poll
    residual = sum(map(abs, venue.positions.values()))
    pnl = venue.nlv(END) - residual * RESIDUAL_FINE
    venue.close()
    return pnl, desk.accepted, residual


def summarize(results):
    ordered = sorted(result[0] for result in results)
    size = len(results)
    return (mean(ordered), ordered[ceil(size * 0.05) - 1],
            100 * sum(pnl < 0 for pnl in ordered) / size,
            mean(result[1] for result in results), sum(bool(result[2]) for result in results))


def print_summary(scenario, policy, results):
    pnl, tail, loss, accepted, unflat = summarize(results)
    print(f"{scenario:<28} {policy:<26} {pnl:>9,.0f} {tail:>9,.0f} {loss:>6.1f}% {accepted:>7.2f} {unflat:>7}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--heats", type=int, default=40, help="Heats per scenario")
    parser.add_argument("--seed", type=int, default=20261111)
    parser.add_argument("--scenario", action="append", choices=[item.name for item in SCENARIOS])
    args = parser.parse_args()
    if args.heats < 1:
        parser.error("Use at least one heat per scenario")
    scenarios = [item for item in SCENARIOS if not args.scenario or item.name in args.scenario]
    print(f"{args.heats} synthetic heats per scenario and policy, seed {args.seed}.")
    print(f"{'Scenario':<28} {'Policy':<26} {'Mean $':>9} {'5th % $':>9} {'Losses':>7} {'Acc/Ht':>7} {'Unflat':>7}")
    pooled = {name: [] for name in POLICIES}
    for scenario in scenarios:
        for name, strategy in POLICIES.items():
            results = [run_heat(scenario, args.seed + i, strategy) for i in range(args.heats)]
            print_summary(scenario.name, name, results)
            pooled[name].extend(results)
    for name, results in pooled.items():
        print_summary("Pooled, equally weighted", name, results)


if __name__ == "__main__":
    main()
