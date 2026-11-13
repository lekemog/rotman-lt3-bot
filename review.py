"""Show where a recorded practice session made and left money.

    python3 dashboard.py --mac --enable-orders --record    # trade a practice heat
    python3 review.py                                      # reads .rotman/

For every tender in the recording this prints the premium on offer, what the
desk decided and why, and for accepted tenders what the exit cost. It then
replays the declined tenders under the "Capture more" settings. Figures come
from recorded books and the desk's own activity log. They are not RIT's
official P&L.
"""

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from decimal import Decimal
import json
from pathlib import Path
import re

from execution import STRATEGY_FIELDS, Strategy, strategy_from_dict
from rotman_lt3 import CENT, evaluate, exit_levels, parse_tender


ACCEPTED = re.compile(r"#(\d+) (BUY|SELL) ([\d,]+) (\w+) @ ([\d.]+)")
FLAT = re.compile(r"Tender #(\d+) completed · model P&L \$(-?[\d,.]+)")


def load_heats(path):
    heats, previous = [], None
    with path.open(encoding="utf-8") as source:
        for line in source:
            try:
                snapshot = json.loads(line)
                key = snapshot["period"], snapshot["tick"]
            except (ValueError, KeyError, TypeError):
                continue
            if previous is None or key[0] != previous[0] or key[1] < previous[1]:
                heats.append([])
            heats[-1].append(snapshot)
            previous = key
    return [heat for heat in heats if len(heat) >= 5]


def load_results(path):
    """Realised model P&L per completed tender, keyed by the offer's terms."""
    results, open_offers = defaultdict(list), {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return results
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") == "ACCEPTED" and (match := ACCEPTED.search(row.get("message", ""))):
            id_, side, quantity, ticker, price = match.groups()
            open_offers[id_] = id_, side, int(quantity.replace(",", "")), ticker, Decimal(price)
        elif row.get("event") == "FLAT" and (match := FLAT.search(row.get("message", ""))):
            key = open_offers.pop(match.group(1), None)
            if key:
                results[key].append(Decimal(match.group(2).replace(",", "")))
    return results


def mid(book):
    bids, asks = exit_levels(book, "SELL"), exit_levels(book, "BUY")
    return (bids[0][0] + asks[0][0]) / 2 if bids and asks else None


def cents(value):
    return "    —" if value is None else f"{float(value) * 100:+5.1f}¢"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", type=Path, default=Path(".rotman"))
    args = parser.parse_args()
    try:
        heats = load_heats(args.state_dir / "snapshots.jsonl")
    except OSError as error:
        parser.error(f"No recording found. Run the dashboard with --record first. ({error})")
    if not heats:
        parser.error("The recording holds no complete heat")
    results = load_results(args.state_dir / "activity.jsonl")
    active = Strategy()
    try:
        saved = json.loads((args.state_dir / "strategy.json").read_text(encoding="utf-8"))
        defaults = asdict(active)
        active = strategy_from_dict({name: saved.get(name, defaults[name]) for name in STRATEGY_FIELDS})
    except (OSError, ValueError, TypeError, AttributeError, ArithmeticError):
        pass
    capture = replace(active, refill=max(active.refill, 50), adverse_move=min(active.adverse_move, 2 * CENT),
                      ticks_per_child=min(active.ticks_per_child, 4),
                      market_closeout_ticks=max(active.market_closeout_ticks, 8))

    print(f"{len(heats)} heat(s) from {args.state_dir}. All figures are cents per share of the tender.")
    print("premium = tender price against the mid · sweep = profit from one immediate sweep after fees")
    taken_count = seen_count = 0
    exit_costs, realised_total, declined_value, capture_extra = [], Decimal(0), Decimal(0), 0
    reasons = Counter()
    for number, heat in enumerate(heats, 1):
        print(f"\nHEAT {number}  (ticks {heat[0]['tick']}–{heat[-1]['tick']})")
        offers = {}
        for index, snapshot in enumerate(heat):
            flat = dict(snapshot, positions=dict.fromkeys(snapshot["positions"], 0), open_orders=0)
            for raw in snapshot["tenders"]:
                try:
                    tender = parse_tender(raw)
                    now = evaluate(tender, flat, active.tender_settings(tender))
                    wider = evaluate(tender, flat, capture.tender_settings(tender))
                except (ValueError, KeyError, TypeError, ArithmeticError):
                    continue
                reference = mid(snapshot["books"][tender.ticker])
                premium = None if reference is None else tender.direction * (reference - tender.price)
                sweep_now = None if now.visible_pnl is None else now.visible_pnl / tender.quantity
                item = offers.setdefault(tender.id, dict(tender=tender, first=snapshot["tick"], rows=[]))
                item["rows"].append((index, snapshot["tick"], premium, sweep_now, now, wider))
        for item in offers.values():
            tender, rows = item["tender"], item["rows"]
            seen_count += 1
            last_index, last_tick, premium, _, decision, wider = rows[-1]
            after = heat[last_index + 1] if last_index + 1 < len(heat) else None
            taken = bool(after and after["positions"][tender.ticker] * tender.direction > 0
                         and last_tick < tender.expires - 1)
            sweeps = [row[3] for row in rows if row[3] is not None]
            line = (f"  t{item['first']:03d} #{tender.id} {tender.ticker} {tender.side:<4} {tender.quantity:>7,}"
                    f" @ {tender.price:>6.2f} · premium {cents(rows[0][2])} · sweep first {cents(sweeps[0] if sweeps else None)}"
                    f" best {cents(max(sweeps) if sweeps else None)}")
            if taken:
                taken_count += 1
                flat_at = next((later["tick"] for later in heat[last_index + 1:]
                                if not later["positions"][tender.ticker]), None)
                line += f" · TAKEN t{last_tick:03d}"
                if flat_at is not None:
                    line += f", flat in {flat_at - last_tick} ticks"
                outcome = results.get((str(tender.id), tender.side, tender.quantity, tender.ticker, tender.price))
                if outcome:
                    pnl = outcome.pop(0)
                    realised_total += pnl
                    per_share = pnl / tender.quantity
                    line += f" · realised {cents(per_share)}"
                    if premium is not None:
                        cost = premium - active.market_fee - per_share
                        exit_costs.append(cost)
                        line += f" · exit cost {cents(cost)} against the mid"
            else:
                accepts = [row for row in rows if row[4].status == "ACCEPT"]
                wider_accepts = [row for row in rows if row[5].status == "ACCEPT"]
                # The reason that held for most of the window, not the expiry notice at its end.
                held = Counter(row[4].reason for row in rows if row[4].status == "WAIT" and row[4].reason)
                why = held.most_common(1)[0][0] if held else decision.reason or decision.status
                reasons[why] += 1
                line += f" · not taken ({why})"
                if sweeps and max(sweeps) > 0:
                    declined_value += max(sweeps) * tender.quantity
                if accepts:
                    line += " · cleared the active settings at some point"
                elif wider_accepts:
                    capture_extra += 1
                    line += f" · Capture more accepts at t{wider_accepts[0][1]:03d}"
            print(line)

    print(f"\nSUMMARY  {taken_count} of {seen_count} tenders taken")
    if realised_total:
        print(f"  realised on matched tenders: ${realised_total:,.0f}")
    if exit_costs:
        ordered = sorted(exit_costs)
        print(f"  exit cost against the mid: median {cents(ordered[len(ordered) // 2])}, worst {cents(ordered[-1])}"
              f" over {len(ordered)} unwinds. This is what crossing the book cost on top of the 2¢ fee.")
    if seen_count > taken_count:
        print(f"  not taken: {dict(reasons)}")
        print(f"  best one-sweep profit visible on tenders not taken: ${declined_value:,.0f}")
        print(f"  tenders the Capture more settings accept that the active settings never did: {capture_extra}")
    print("\nHOW TO READ IT")
    print("  A large exit cost means the unwind is the leak. Try a sweep band of 3 to 5 cents for a heat and"
          " compare this figure.")
    print("  Tenders lost to 'Heat closeout window' or to the model with a positive best sweep are the entry leak."
          " Capture more addresses those.")


if __name__ == "__main__":
    main()
