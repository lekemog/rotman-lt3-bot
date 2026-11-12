"""Measure a recorded practice session and compare it with the strategy profile.

    python dashboard.py --rit --record        # monitor or trade a practice heat
    python calibrate.py                       # reads .rotman/snapshots.jsonl

Every number is measured from the recording. The suggestions at the end are
starting points for the strategy editor. They are only as good as the number of
heats and tenders in the file, which is printed first.
"""

import argparse
from collections import defaultdict
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from statistics import median, pstdev

from execution import STRATEGY_FIELDS, strategy_from_dict
from rotman_lt3 import CAP, ZERO, Settings, evaluate, exit_levels, mid_price, mids, parse_tender, sweep


def load(path):
    """Split the recording into heats. A heat ends when the clock goes back."""
    heats, previous = [], None
    with path.open(encoding="utf-8") as source:
        for line in source:
            try:
                snapshot = json.loads(line)
                key = snapshot.get("case_name"), snapshot["period"]
                tick = snapshot["tick"]
            except (ValueError, KeyError, TypeError):
                continue
            if previous is None or key != previous[0] or tick < previous[1]:
                heats.append([])
            heats[-1].append(snapshot)
            previous = key, tick
    return heats


def per_tick(heat):
    """Keep the last snapshot seen in each tick."""
    rows = {}
    for snapshot in heat:
        rows[snapshot["tick"]] = snapshot
    return [rows[tick] for tick in sorted(rows)]


def percentile(values, share):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))] if ordered else None


def cents(value):
    return "—" if value is None else f"{float(value) * 100:,.1f}¢"


def depth_within(levels, band):
    if not levels:
        return 0
    best = levels[0][0]
    return sum(size for price, size in levels if abs(price - best) <= band)


def market_report(heats):
    print("\nMARKET  (per ticker, all recorded heats)")
    for ticker in CAP:
        moves, spreads, near, impact, recovery = [], [], [], defaultdict(list), defaultdict(list)
        for heat in heats:
            rows = per_tick(heat)
            series = []
            for snapshot in rows:
                book = snapshot["books"][ticker]
                bids, asks = exit_levels(book, "SELL"), exit_levels(book, "BUY")
                mid = mid_price(book)
                if mid is None:
                    series.append(None)
                    continue
                spreads.append(asks[0][0] - bids[0][0])
                both = depth_within(bids, Decimal("0.10")), depth_within(asks, Decimal("0.10"))
                near.extend(both)
                series.append((snapshot["tick"], mid, both))
                for size in (10_000, 25_000, 50_000, 75_000):
                    for side, levels in (("SELL", bids), ("BUY", asks)):
                        walk = sweep(levels, size, side)
                        if walk.filled == size:
                            impact[size].append(abs(walk.vwap - mid))
                        else:
                            impact[size].append(None)
            for before, after in zip(series, series[1:]):
                if before and after and after[0] - before[0] == 1:
                    moves.append(float(after[1] - before[1]))
            # After a side loses 40% of its near depth in one tick, how much of
            # the loss is back one, two, three and five ticks later?
            for index in range(1, len(series)):
                if not series[index - 1] or not series[index]:
                    continue
                for side in (0, 1):
                    was, now = series[index - 1][2][side], series[index][2][side]
                    if was >= 5_000 and now <= was * 0.6:
                        for lag in (1, 2, 3, 5):
                            later = series[index + lag] if index + lag < len(series) else None
                            if later and later[0] - series[index][0] == lag:
                                recovery[lag].append((later[2][side] - now) / (was - now))
        if not spreads:
            print(f"  {ticker}: no two-sided quotes recorded")
            continue
        sigma = pstdev(moves) if len(moves) > 1 else 0.0
        print(f"  {ticker}: sigma {sigma * 100:.1f}¢ per tick ({sigma * 30 ** 0.5 * 100:.0f}¢ over a 30-tick window)"
              f" · median spread {cents(median(spreads))}"
              f" · depth within 10¢ of the touch: median {median(near):,.0f}, 10th percentile"
              f" {percentile(near, 0.1):,.0f}")
        parts = []
        for size, costs in impact.items():
            covered = [cost for cost in costs if cost is not None]
            share = 100 * len(covered) / len(costs)
            parts.append(f"{size // 1000}k {cents(median(covered)) if covered else '—'} ({share:.0f}% covered)")
        print(f"      cost of one immediate sweep against the mid: {' · '.join(parts)}")
        if recovery[1]:
            parts = [f"+{lag} tick{'s' if lag > 1 else ''}: {100 * median(values):.0f}%"
                     for lag, values in recovery.items() if values]
            print(f"      displayed depth back after a 40% one-tick drop ({len(recovery[1])} events,"
                  f" anyone's trades): {' · '.join(parts)}")


def tender_report(heats, settings):
    print("\nTENDERS  (model edge in cents per share, using the profile above with patience 0)")
    settings = replace(settings, patience=ZERO)
    drift, final, disagreements, taken = [], [], 0, 0
    for number, heat in enumerate(heats, 1):
        paths = {}
        for snapshot in heat:
            reference = mids(snapshot)
            flat = dict(snapshot, positions=dict.fromkeys(CAP, 0))
            for raw in snapshot["tenders"]:
                try:
                    tender = parse_tender(raw, reference)
                except (ValueError, KeyError, TypeError, ArithmeticError):
                    print(f"  heat {number} tender {raw.get('tender_id')}: could not be read. Caption: {raw.get('caption')!r}")
                    continue
                action = str(raw.get("action") or "").upper()
                if action in ("BUY", "SELL") and action != tender.side:
                    disagreements += 1
                decision = evaluate(tender, flat, settings)
                edge = None if decision.stressed_pnl is None else decision.stressed_pnl / tender.quantity
                mid = reference.get(tender.ticker)
                premium = None if mid is None else tender.direction * (mid - tender.price)
                paths.setdefault(tender.id, dict(tender=tender, rows=[]))["rows"].append(
                    (snapshot["tick"], edge, premium, snapshot["positions"][tender.ticker]))
        last_tick = heat[-1]["tick"]
        for item in paths.values():
            tender, rows = item["tender"], item["rows"]
            edges = [edge for _, edge, _, _ in rows if edge is not None]
            seen_until = rows[-1][0]
            accepted = seen_until < min(tender.expires, last_tick) - 1
            taken += accepted
            first = rows[0]
            line = (f"  heat {number} t{first[0]:03d} #{tender.id} {tender.ticker} {tender.side}"
                    f" {tender.quantity:,} @ {tender.price:.2f} · premium to mid {cents(first[2])}")
            if edges:
                line += (f" · edge first {cents(edges[0])}, best {cents(max(edges))}, worst {cents(min(edges))},"
                         f" last {cents(edges[-1])}")
                if not accepted and rows[-1][0] - rows[0][0] >= 20:
                    drift.append(float(edges[-1] - edges[0]))
                    final.append((float(edges[0]), float(edges[-1])))
            else:
                line += " · displayed depth never covered the block"
            print(line + (f" · taken at t{seen_until:03d}" if accepted else " · ran to expiry"))
    if not taken and not drift:
        print("  no tenders recorded")
    if disagreements:
        print(f"  WARNING: the API action field disagreed with the caption {disagreements} times."
              " The desk follows the caption. Check one tender by hand in RIT.")
    if drift:
        better = sum(last > max(first, 0) for first, last in final)
        print(f"  {len(drift)} tenders watched for a full window: edge moved {median(drift) * 100:+.1f}¢ (median)"
              f" from first sight to last, and {better} of {len(drift)} ended better than they started.")
        print("  A median near zero means waiting is free, as the random-walk argument says. A clearly"
              " negative median means other traders are racing for the same liquidity, so lower patience.")
    return drift


def unwind_report(heats, settings):
    """Compare each unwind's change in net liquid value with the entry model."""
    print("\nUNWINDS  (from reported positions and net liquid value)")
    settings = replace(settings, patience=ZERO)
    errors, found = [], False
    for number, heat in enumerate(heats, 1):
        start = None
        for before, after in zip(heat, heat[1:]):
            flat_before = not any(before["positions"].values())
            flat_after = not any(after["positions"].values())
            if flat_before and not flat_after:
                start, model = before, None
                peak = max(map(abs, after["positions"].values()))
                live = {raw.get("tender_id") for raw in after["tenders"]}
                gone = [raw for raw in before["tenders"] if raw.get("tender_id") not in live]
                if len(gone) == 1:
                    try:
                        tender = parse_tender(gone[0], mids(before))
                        if after["positions"][tender.ticker] * tender.direction > 0:
                            model = evaluate(tender, before, settings).stressed_pnl
                    except (ValueError, KeyError, TypeError, ArithmeticError):
                        pass
            elif start and not flat_after:
                peak = max(peak, *map(abs, after["positions"].values()))
            elif start and flat_after:
                found = True
                line = f"  heat {number} t{start['tick']:03d}→t{after['tick']:03d}: peak {peak:,} shares"
                line += f" · {after['tick'] - start['tick']} ticks to flat"
                if start.get("nlv") is not None and after.get("nlv") is not None:
                    change = Decimal(str(after["nlv"])) - Decimal(str(start["nlv"]))
                    line += f" · realised {change:+,.0f} ({change / peak * 100:+.1f}¢/share)"
                    if model is not None:
                        line += f" · model {model:+,.0f} ({model / peak * 100:+.1f}¢/share)"
                        errors.append(float((change - model) / peak))
                print(line)
                start = None
        if start:
            print(f"  heat {number}: the recording ends with inventory open since t{start['tick']:03d}")
            found = True
    if not found:
        print("  no positions recorded. Arm the desk or use Unwind only in a practice heat to measure fills.")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recording", type=Path, nargs="?", default=Path(".rotman/snapshots.jsonl"))
    parser.add_argument("--strategy", type=Path, default=Path(".rotman/strategy.json"),
                        help="Profile to evaluate tenders with. Defaults are used if it is missing.")
    args = parser.parse_args()
    try:
        heats = [heat for heat in load(args.recording) if len(heat) >= 5]
    except OSError as error:
        parser.error(f"Cannot read {args.recording}: {error}")
    settings = Settings()
    try:
        saved = json.loads(args.strategy.read_text(encoding="utf-8"))
        settings = strategy_from_dict({name: saved[name] for name in STRATEGY_FIELDS})
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        pass
    if not heats:
        parser.error("The recording holds no complete heat")

    ticks = sum(len(per_tick(heat)) for heat in heats)
    print(f"{len(heats)} heat(s), {ticks} ticks, {sum(map(len, heats))} snapshots from {args.recording}")
    last = heats[-1][-1]
    print("\nRULES REPORTED BY RIT  (case brief: fee 2¢, max order CRZY 25,000 and TAME 10,000,"
          " limits 100,000 net and 250,000 gross)")
    for ticker, rule in (last.get("rules") or {}).items():
        print(f"  {ticker}: fee {rule.get('fee', 'not reported')} · limit-order rebate"
              f" {rule.get('rebate', 'not reported')} · max order {rule.get('cap') or 'not reported'}")
    print(f"  limits: {last.get('limits') or 'not reported'} · fines so far: {last.get('fines')}")

    market_report(heats)
    drift = tender_report(heats, settings)
    errors = unwind_report(heats, settings)

    print("\nSUGGESTED STARTING POINTS")
    if len(errors) >= 4:
        gap = median(errors) * 100
        if gap < -1:
            verdict = "the model is too generous. Lower refill or raise the depth haircut."
        elif gap > 1:
            verdict = "the model is leaving tenders on the table. Raise refill or lower the adverse move."
        else:
            verdict = "the model matches the fills. Leave the entry model alone."
        print(f"  entry model: realised minus model is {gap:+.1f}¢/share (median of {len(errors)} unwinds), so {verdict}")
        print("      Each unwind also carries the mid's drift while inventory was held, so judge the median"
              " over several unwinds and never a single one.")
    else:
        print("  entry model: fewer than four matched unwinds. Trade more practice heats before changing"
              f" refill ({settings.refill}%) or the depth haircut ({settings.depth_haircut}%).")
    if len(drift) >= 5:
        verdict = "keep patience near 1" if median(drift) > -0.01 else "cut patience towards 0"
        print(f"  patience: {verdict} (median edge drift {median(drift) * 100:+.1f}¢ over {len(drift)} tenders).")
    else:
        print("  patience: watch at least five tenders to expiry in monitor mode before trusting a drift figure.")


if __name__ == "__main__":
    main()
