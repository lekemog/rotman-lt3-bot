"""Tender decision support for the RITCxDublin 2026 liability trading case.

Python 3.10+, standard library only. Start with `python rotman_lt3.py demo`.
Use `watch client --record practice.jsonl` with the Windows RIT Client running
and its REST API enabled. `watch dma` uses the published Dublin practice port.
`replay practice.jsonl` re-evaluates recorded snapshots with the chosen settings.

Watch and replay advise on one tender at a time. Accept, decline and unwind in
the RIT interface. Only unwind after acceptance and the position are confirmed.
Use the CLI fee settings to match the practice transaction log. By default the
model charges $0.02 per market share and assumes no separate tender commission.
The stress settings are starting assumptions, to be calibrated from practice.
"""

import argparse
import base64
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from functools import partial
from getpass import getpass
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode


CAP = {"CRZY": 25_000, "TAME": 10_000}
NET_LIMIT = 100_000
GROSS_LIMIT = 250_000
ZERO = Decimal(0)
CENT = Decimal("0.01")
MAX_SNAPSHOT_AGE = 1.5
STALE_SNAPSHOT = "SNAPSHOT TOO OLD OR HEAT CHANGED"


def decimal(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Expected a decimal number") from error
    if not number.is_finite():
        raise ValueError("Expected a finite number")
    return number


def shares(value):
    number = decimal(value)
    if number != int(number):
        raise ValueError("Expected a whole share count")
    return int(number)


@dataclass(frozen=True)
class Settings:
    market_fee: Decimal = Decimal("0.02")
    tender_fee: Decimal = ZERO
    depth_haircut: int = 10
    adverse_move: Decimal = CENT
    min_edge: Decimal = ZERO
    min_heat_ticks: int = 20
    decision_ticks: int = 2
    refill: int = 0  # % of consumed depth assumed back before the next child order.


@dataclass(frozen=True)
class Tender:
    id: int
    ticker: str
    side: str  # Our action, as displayed to the trader.
    quantity: int
    price: Decimal
    expires: int
    period: int

    @property
    def direction(self):
        return 1 if self.side == "BUY" else -1

    @property
    def exit_side(self):
        return "SELL" if self.side == "BUY" else "BUY"


@dataclass(frozen=True)
class Sweep:
    filled: int
    value: Decimal
    worst_price: Decimal | None

    @property
    def vwap(self):
        return self.value / self.filled if self.filled else None


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str
    visible: Sweep
    stressed: Sweep
    visible_pnl: Decimal | None
    stressed_pnl: Decimal | None


@dataclass(frozen=True)
class ChildOrder:
    ticker: str
    side: str
    quantity: int
    limit: Decimal


def parse_tender(raw):
    """Resolve the ticker and our side from the offer shown to the trader."""
    caption = raw.get("caption") or ""
    ticker_tokens = set(re.findall(r"\b(?:CRZY|TAME)\b", caption.upper()))
    ticker = raw.get("ticker")
    if ticker is None and len(ticker_tokens) == 1:
        ticker = next(iter(ticker_tokens))
    if ticker not in CAP or ticker_tokens - {ticker}:
        raise ValueError("Tender ticker is missing or ambiguous")

    trader_sides = {word.upper() for word in re.findall(
        r"\b(?:would you like|do you want)\s+to\s+(buy|sell)\b", caption, re.I
    )}
    institution_sides = re.findall(
        r"\b(?:institution(?:al client|al trader)?|client|counterparty)\s+"
        r"(?:would like to|wants to|is offering to|has offered to|will|wishes to)\s+"
        r"(buy|sell)\b", caption, re.I
    )
    trader_sides.update("SELL" if word.upper() == "BUY" else "BUY"
                        for word in institution_sides)
    if len(trader_sides) != 1:
        raise ValueError("Confirm BUY/SELL in the RIT tender dialog")

    if raw.get("is_fixed_bid") is not True:
        raise ValueError("Expected a fixed-price tender")
    quantity = abs(shares(raw["quantity"]))
    price = decimal(raw["price"])
    if quantity == 0 or price <= 0:
        raise ValueError("Expected a positive tender size and price")
    return Tender(shares(raw["tender_id"]), ticker, trader_sides.pop(), quantity,
                  price, shares(raw["expires"]), shares(raw["period"]))


def exit_levels(book, exit_side):
    """Aggregate unfilled order quantities at each price, best price first."""
    prices = defaultdict(int)
    selling = exit_side == "SELL"
    for row in book["bids" if selling else "asks"]:
        left = shares(row["quantity"]) - shares(row["quantity_filled"])
        price = decimal(row["price"])
        if left > 0 and price > 0:
            prices[price] += left
    return sorted(prices.items(), reverse=selling)


def sweep(levels, quantity, exit_side, haircut=0, adverse_move=ZERO, cap=None, refill=0):
    """Walk the required shares child by child, including a partial last level.

    Each child takes at most `cap` shares. Liquidity taken by earlier children
    leaves a hole at the top of the book and `refill` percent of that hole is
    back before the next child. With refill 0 this is one sweep of the book.
    """
    cap = cap or quantity
    levels = [(price, size * (100 - haircut) // 100) for price, size in levels]
    filled, value, worst, hole = 0, ZERO, None, 0
    while filled < quantity:
        child = min(cap, quantity - filled)
        hole = hole * (100 - refill) // 100
        skip, got = hole, 0
        for price, available in levels:
            if skip >= available:
                skip -= available
                continue
            take = min(child - got, available - skip)
            skip = 0
            stressed_price = price - adverse_move if exit_side == "SELL" else price + adverse_move
            if stressed_price <= 0:
                break
            got += take
            value += take * stressed_price
            worst = stressed_price
            if got == child:
                break
        filled += got
        hole += got
        if got < child:
            break
    return Sweep(filled, value, worst)


def evaluate(tender, snapshot, settings):
    levels = exit_levels(snapshot["books"][tender.ticker], tender.exit_side)
    visible = sweep(levels, tender.quantity, tender.exit_side)
    stressed = sweep(levels, tender.quantity, tender.exit_side, settings.depth_haircut,
                     settings.adverse_move, CAP[tender.ticker], settings.refill)
    entry_value = tender.price * tender.quantity
    fees = tender.quantity * (settings.market_fee + settings.tender_fee)
    visible_pnl = (tender.direction * (visible.value - entry_value) - fees
                   if visible.filled == tender.quantity else None)
    stressed_pnl = (tender.direction * (stressed.value - entry_value) - fees
                    if stressed.filled == tender.quantity else None)
    decision = Decision("WAIT", "", visible, stressed, visible_pnl, stressed_pnl)

    tick = snapshot["tick"]
    if tender.period != snapshot["period"] or tender.expires <= tick:
        return replace(decision, status="EXPIRED", reason="Tender window closed")
    if tender.expires - tick <= settings.decision_ticks:
        return replace(decision, status="PASS", reason="Tender at expiry buffer")
    if snapshot["end_tick"] - tick <= settings.min_heat_ticks:
        return replace(decision, status="PASS", reason="Heat closeout window")

    positions = snapshot["positions"]
    projected = dict(positions)
    projected[tender.ticker] += tender.direction * tender.quantity
    if abs(sum(projected.values())) > NET_LIMIT or sum(map(abs, projected.values())) > GROSS_LIMIT:
        return replace(decision, reason="Projected net or gross limit exceeded")
    if any(positions.values()) or snapshot["open_orders"]:
        return replace(decision, reason="Clear existing inventory and working orders first")
    if stressed_pnl is None:
        return replace(decision, reason="Stressed depth cannot cover the block")
    if stressed_pnl <= settings.min_edge * tender.quantity:
        return replace(decision, reason="Stressed P&L below acceptance threshold")
    return replace(decision, status="ACCEPT", reason="Full stressed unwind clears fees and edge")


def next_unwind(confirmed_tender, position, book, open_orders, band=None):
    """Suggest one marketable limit for confirmed inventory from a flat start.

    Recheck fills and position before asking for another slice. A partially
    filled limit can rest in the book. Cancel and confirm any remainder before
    asking again. This function only describes an order.
    """
    if open_orders or position * confirmed_tender.direction <= 0:
        return None
    if abs(position) > confirmed_tender.quantity:
        raise ValueError("Position exceeds confirmed tender inventory")
    ticker = confirmed_tender.ticker
    side = confirmed_tender.exit_side
    levels = exit_levels(book, side)
    if band is not None and levels:
        # A patient child takes only what is displayed within `band` of the
        # touch. The rest of the chunk waits for the book to refill.
        best = levels[0][0]
        levels = [(price, size) for price, size in levels if abs(price - best) <= band]
    quote = sweep(levels, min(abs(position), CAP[ticker]), side)
    if quote.filled == 0:
        return None
    return ChildOrder(ticker, side, quote.filled, quote.worst_price)


class RateLimited(Exception):
    def __init__(self, wait):
        self.wait = wait


class RITReader:
    def __init__(self, mode, port=None, *, dma_https=False):
        self.connection_mode = mode
        self.read_pool = None
        self.read_connections = []
        self.snapshot_timing = {}
        if mode == "client":
            self.connection = HTTPConnection("127.0.0.1", port or 9999, timeout=2)
            self.headers = {"X-API-Key": getpass("RIT Client API key: ")}
        else:
            connect = HTTPSConnection if dma_https else HTTPConnection
            self.connect_dma = partial(connect, "flserver.rotman.utoronto.ca", port or 16540, timeout=2)
            self.connection = self.connect_dma()
            credentials = f"{input('RIT Trader ID: ')}:{getpass('RIT password: ')}"
            self.headers = {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}

    def close(self):
        if self.read_pool is not None:
            self.read_pool.shutdown(wait=True)
            self.read_pool = None
        for connection in self.read_connections:
            connection.close()
        self.read_connections.clear()
        self.connection.close()

    def get(self, endpoint, **params):
        return self.read(self.connection, endpoint, **params)

    def read(self, connection, endpoint, **params):
        path = f"/v1/{endpoint}"
        if params:
            path += "?" + urlencode(params)
        connection.request("GET", path, headers=self.headers)
        response = connection.getresponse()
        body = response.read()
        if response.status == 429:
            try:
                wait = response.getheader("Retry-After") or json.loads(body)["wait"]
                wait = float(decimal(wait))
            except (ValueError, ArithmeticError, KeyError, TypeError):
                wait = 1.0
            raise RateLimited(max(0.25, wait))
        if response.status != 200:
            raise RuntimeError(f"RIT {endpoint} returned HTTP {response.status}")
        return json.loads(body)

    def read_snapshot_data(self, depth):
        requests = [("securities", {}), ("orders", {"status": "OPEN"}), ("tenders", {})]
        requests.extend(("securities/book", {"ticker": ticker, "limit": depth}) for ticker in CAP)
        if self.connection_mode == "client":
            return [self.get(endpoint, **params) for endpoint, params in requests]

        # One persistent connection per independent GET. The main connection
        # remains serial for the clock checks and order submissions.
        if self.read_pool is None:
            self.read_pool = ThreadPoolExecutor(max_workers=len(requests))
            self.read_connections = [self.connect_dma() for _ in requests]
        pending = [self.read_pool.submit(self.read, connection, endpoint, **params)
                   for connection, (endpoint, params) in zip(self.read_connections, requests)]
        errors = [future.exception() for future in pending]
        limits = [error for error in errors if isinstance(error, RateLimited)]
        if limits:
            raise max(limits, key=lambda error: error.wait)
        return [future.result() for future in pending]

    def snapshot(self, depth):
        started = time.monotonic()
        self.snapshot_timing = {}
        try:
            return self.read_snapshot(depth, started)
        finally:
            self.snapshot_timing["total_ms"] = round((time.monotonic() - started) * 1000)

    def read_snapshot(self, depth, started):
        self.snapshot_timing["stage"] = "initial clock"
        before = self.get("case")
        self.snapshot_timing["clock_ms"] = round((time.monotonic() - started) * 1000)
        if before["status"] != "ACTIVE":
            return before["status"], None
        if time.monotonic() - started > MAX_SNAPSHOT_AGE:
            return STALE_SNAPSHOT, None

        self.snapshot_timing["stage"] = "account and books"
        data_started = time.monotonic()
        securities, open_orders, tenders, *raw_books = self.read_snapshot_data(depth)
        self.snapshot_timing["data_ms"] = round((time.monotonic() - data_started) * 1000)
        if time.monotonic() - started > MAX_SNAPSHOT_AGE:
            return STALE_SNAPSHOT, None

        positions = {row["ticker"]: shares(row["position"]) for row in securities}
        if set(positions) != set(CAP):
            raise RuntimeError("Expected exactly the Dublin case tickers CRZY and TAME")
        books = {}
        for ticker, book in zip(CAP, raw_books):
            books[ticker] = {
                side: [{field: order[field] for field in ("price", "quantity", "quantity_filled")}
                       for order in book[side]]
                for side in ("bids", "asks")
            }
        self.snapshot_timing["stage"] = "clock recheck"
        confirm_started = time.monotonic()
        after = self.get("case")
        self.snapshot_timing["recheck_ms"] = round((time.monotonic() - confirm_started) * 1000)
        tick = shares(after["tick"])
        period = shares(after["period"])
        if (after["status"] != "ACTIVE" or before["period"] != after["period"]
                or not 0 <= tick - shares(before["tick"]) <= 1
                or time.monotonic() - started > MAX_SNAPSHOT_AGE):
            return STALE_SNAPSHOT, None

        fields = ("tender_id", "ticker", "period", "expires", "caption",
                  "is_fixed_bid", "price", "quantity")
        return "ACTIVE", dict(tick=tick, period=period, end_tick=shares(after["ticks_per_period"]),
                              case_name=after.get("name", "RIT"), positions=positions, books=books,
                              open_orders=len(open_orders),
                              open_order_ids=[shares(order["order_id"]) for order in open_orders],
                              tenders=[{key: row[key] for key in fields if key in row} for row in tenders])


def report(snapshot, settings):
    results = []
    for raw in snapshot["tenders"]:
        try:
            tender = parse_tender(raw)
            results.append((tender, evaluate(tender, snapshot, settings)))
        except (ValueError, ArithmeticError, KeyError, TypeError) as error:
            print(f"  Tender {raw.get('tender_id', '?')}: REVIEW IN RIT ({error})")

    best = max((item for item in results if item[1].status == "ACCEPT"),
               key=lambda item: (item[1].stressed_pnl, -item[0].expires), default=None)
    for tender, decision in results:
        if decision.status == "ACCEPT" and tender != best[0]:
            decision = replace(decision, status="WAIT", reason="Another tender has higher stressed P&L")
        visible = money(decision.visible_pnl)
        stressed = money(decision.stressed_pnl)
        vwap = "—" if decision.visible.vwap is None else f"${decision.visible.vwap:.4f}"
        print(f"  #{tender.id} {tender.ticker} {tender.side} {tender.quantity:,} @ ${tender.price:.2f}"
              f" | {decision.status} — {decision.reason}"
              f" | exit VWAP {vwap} | visible {visible} | stressed {stressed}"
              f" | stressed depth {decision.stressed.filled:,}/{tender.quantity:,}"
              f" | expiry {max(0, tender.expires - snapshot['tick'])} ticks")


def money(amount):
    if amount is None:
        return "—"
    sign = "-" if amount < 0 else "+"
    return f"{sign}${abs(amount):,.2f}"


def demo_snapshot():
    # Price and volume inputs from the case brief's example book.
    bids = [("9.98", 8000), ("9.95", 25000), ("9.90", 7000), ("9.88", 10000), ("9.84", 15000)]
    asks = [("10.00", 5000), ("10.03", 3000), ("10.07", 2000), ("10.42", 18000), ("10.45", 25000)]
    book = {side: [dict(price=p, quantity=q, quantity_filled=0) for p, q in levels]
            for side, levels in (("bids", bids), ("asks", asks))}
    snapshot = dict(period=1, tick=100, end_tick=300, positions=dict(CRZY=0, TAME=0),
                    open_orders=0, books=dict(CRZY=book, TAME=book), tenders=[])
    for id_, price in enumerate(("9.90", "9.97", "9.85"), 1):
        snapshot["tenders"].append(dict(tender_id=id_, period=1, expires=125,
                                       caption="An institution would like to sell CRZY to you."
                                               " Would you like to BUY the shares?",
                                       is_fixed_bid=True, price=price, quantity=50_000))
    return snapshot


def demo(settings):
    snapshot = demo_snapshot()
    print("Independent tender examples at tick 100. The $9.85 offer is synthetic.")
    for tender in snapshot["tenders"]:
        report(dict(snapshot, tenders=[tender]), settings)
    strong_tender = parse_tender(snapshot["tenders"][-1])
    child = next_unwind(strong_tender, 50_000, snapshot["books"]["CRZY"], open_orders=0)
    print(f"Example after confirmed $9.85 acceptance: {child.side} {child.quantity:,}"
          f" {child.ticker} at a limit of ${child.limit:.2f}, then check fills and remaining position.")


def self_test():
    snapshot = demo_snapshot()
    settings = Settings()
    long, poor, strong = map(parse_tender, snapshot["tenders"])
    quote = evaluate(long, snapshot, settings)
    assert quote.visible.vwap == Decimal("9.9338")
    assert quote.visible_pnl == Decimal("690") and quote.stressed_pnl == Decimal("-279")
    assert evaluate(long, snapshot, replace(settings, tender_fee=CENT * 2)).visible_pnl == Decimal("-310")
    assert evaluate(poor, snapshot, settings).visible_pnl == Decimal("-2810")
    assert evaluate(strong, snapshot, settings).status == "ACCEPT"
    short = replace(strong, side="SELL", ticker="TAME", quantity=6_000, price=Decimal("10.10"))
    assert evaluate(short, snapshot, settings).visible_pnl == Decimal("450")
    assert evaluate(replace(short, quantity=54_000), snapshot, settings).visible_pnl is None
    assert evaluate(replace(strong, quantity=100_001), snapshot, settings).reason.startswith("Projected")
    gross_case = dict(snapshot, positions=dict(CRZY=130_000, TAME=-120_000))
    assert evaluate(strong, gross_case, settings).reason.startswith("Projected")
    assert evaluate(strong, dict(snapshot, tick=123), settings).status == "PASS"
    assert evaluate(replace(strong, expires=300), dict(snapshot, tick=280), settings).status == "PASS"
    assert evaluate(strong, dict(snapshot, open_orders=1), settings).status == "WAIT"
    assert evaluate(strong, dict(snapshot, positions=dict(CRZY=0, TAME=1)), settings).status == "WAIT"
    book = snapshot["books"]["CRZY"]
    assert next_unwind(strong, 50_000, book, 0) == ChildOrder("CRZY", "SELL", 25_000, Decimal("9.95"))
    assert next_unwind(short, -3_000, book, 0) == ChildOrder("TAME", "BUY", 3_000, Decimal("10.00"))
    assert next_unwind(strong, -5_000, book, 0) is None
    assert next_unwind(strong, 5_000, book, 1) is None
    order = dict(price="9.99", quantity=10_000, quantity_filled=9_000)
    assert exit_levels(dict(bids=book["bids"] + [order]), "SELL")[0] == (Decimal("9.99"), 1_000)
    raw = dict(snapshot["tenders"][0], ticker="CRZY", action="BUY", quantity=-50_000,
               caption="An institution would like to BUY CRZY. Would you like to SELL the shares?")
    assert parse_tender(raw).side == "SELL"
    for caption in ("CRZY", "An institution would like to BUY CRZY. Would you like to BUY the shares?"):
        try:
            parse_tender(dict(raw, caption=caption))
        except ValueError:
            continue
        raise AssertionError("Ambiguous offer should be reviewed manually")
    # Pricing the exit as two half-refilled children accepts the brief's example.
    levels = exit_levels(book, "SELL")
    assert sweep(levels, 50_000, "SELL", cap=25_000, refill=0) == sweep(levels, 50_000, "SELL")
    assert sweep(levels, 50_000, "SELL", cap=25_000, refill=100).value == 2 * sweep(levels, 25_000, "SELL").value
    refilled = replace(settings, refill=50)
    assert evaluate(long, snapshot, refilled).stressed_pnl == Decimal("796")
    assert evaluate(long, snapshot, refilled).status == "ACCEPT"
    assert evaluate(poor, snapshot, refilled).status == "WAIT"
    # A banded child takes only what is displayed near the touch.
    assert next_unwind(strong, 50_000, book, 0, band=CENT * 2) == ChildOrder("CRZY", "SELL", 8_000, Decimal("9.98"))
    assert next_unwind(strong, 50_000, book, 0, band=CENT * 3) == ChildOrder("CRZY", "SELL", 25_000, Decimal("9.95"))
    assert next_unwind(short, -6_000, book, 0, band=ZERO) == ChildOrder("TAME", "BUY", 5_000, Decimal("10.00"))
    print("LT3 self-test passed.")


def watch(args, settings):
    client = RITReader(args.mode, args.local_port if args.mode == "client" else None)
    record = args.record.open("x", encoding="utf-8") if args.record else None
    try:
        while True:
            try:
                status, snapshot = client.snapshot(args.book_depth)
            except RateLimited as error:
                print(f"Rate limited. Recommendations paused for {error.wait:.2f}s.", flush=True)
                time.sleep(error.wait)
                continue
            except (OSError, HTTPException) as error:
                client.close()
                print(f"Connection interrupted. Recommendations paused ({error}).", flush=True)
                time.sleep(args.interval)
                continue

            if snapshot is None:
                print(status, flush=True)
            else:
                pos = snapshot["positions"]
                print(f"Tick {snapshot['tick']:03d} | CRZY {pos['CRZY']:+,} | TAME {pos['TAME']:+,}"
                      f" | {snapshot['open_orders']} working orders", flush=True)
                report(snapshot, settings)
                if record:
                    record.write(json.dumps(snapshot) + "\n")
                    record.flush()
            time.sleep(args.interval)
    finally:
        if record:
            record.close()
        client.close()


def probe(args):
    client = RITReader(args.mode, args.rit_port, dma_https=args.dma_https)
    fresh, inactive = 0, 0
    try:
        for index in range(1, args.samples + 1):
            delay = 1.0
            try:
                status, snapshot = client.snapshot(50)
                if snapshot:
                    fresh += 1
                    status = f"FRESH · tick {snapshot['tick']}"
                elif status in ("STOPPED", "PAUSED"):
                    inactive += 1
            except RateLimited as error:
                status, delay = f"RATE LIMITED · wait {error.wait:.2f}s", max(delay, error.wait)
            except (OSError, HTTPException, RuntimeError) as error:
                status = f"{client.snapshot_timing['stage']} · {error}"
                client.close()
                if "returned HTTP 401" in str(error):
                    print(f"{index:02d} | {status}", flush=True)
                    break
            timing = client.snapshot_timing
            parts = [f"{label} {timing[key]} ms" for label, key in
                     (("clock", "clock_ms"), ("account/books", "data_ms"), ("recheck", "recheck_ms"))
                     if key in timing]
            print(f"{index:02d} | {status} | total {timing['total_ms']} ms"
                  + (f" ({', '.join(parts)})" if parts else ""), flush=True)
            if index < args.samples:
                time.sleep(delay)
        print(f"{fresh}/{index - inactive} fresh snapshots across non-paused samples · "
              f"{inactive} paused or stopped samples excluded")
    finally:
        client.close()


def add_settings(parser):
    parser.add_argument("--market-fee", type=decimal, default=Settings.market_fee)
    parser.add_argument("--tender-fee", type=decimal, default=Settings.tender_fee)
    parser.add_argument("--depth-haircut", type=int, default=Settings.depth_haircut,
                        help="Percent of displayed depth removed in the stress case")
    parser.add_argument("--adverse-move", type=decimal, default=Settings.adverse_move,
                        help="Stress price move in dollars per share")
    parser.add_argument("--min-edge", type=decimal, default=Settings.min_edge,
                        help="Required stressed profit in dollars per share")
    parser.add_argument("--min-heat-ticks", type=int, default=Settings.min_heat_ticks)
    parser.add_argument("--decision-ticks", type=int, default=Settings.decision_ticks)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    add_settings(commands.add_parser("demo"))
    commands.add_parser("self-test")
    probe_parser = commands.add_parser("probe", help="Measure read-only practice feed freshness")
    probe_parser.add_argument("mode", choices=("client", "dma"))
    probe_parser.add_argument("--samples", type=int, default=8)
    probe_parser.add_argument("--rit-port", type=int)
    probe_parser.add_argument("--dma-https", action="store_true")
    replay_parser = commands.add_parser("replay")
    replay_parser.add_argument("input", type=Path)
    add_settings(replay_parser)
    watch_parser = commands.add_parser("watch")
    watch_parser.add_argument("mode", choices=("client", "dma"))
    watch_parser.add_argument("--record", type=Path, help="New JSONL file for practice snapshots")
    watch_parser.add_argument("--interval", type=decimal, default=Decimal("1.0"))
    watch_parser.add_argument("--local-port", type=int, default=9999)
    watch_parser.add_argument("--book-depth", type=int, default=50)
    add_settings(watch_parser)
    args = parser.parse_args()
    if args.command == "self-test":
        self_test()
        return
    if args.command == "probe":
        if not 1 <= args.samples <= 30 or (args.rit_port is not None and not 1 <= args.rit_port <= 65535):
            parser.error("Use 1–30 samples and a valid RIT port")
        if args.dma_https and args.mode != "dma":
            parser.error("Use --dma-https with DMA")
        try:
            probe(args)
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    settings = Settings(args.market_fee, args.tender_fee, args.depth_haircut, args.adverse_move,
                        args.min_edge, args.min_heat_ticks, args.decision_ticks)
    if not 0 <= settings.depth_haircut < 100 or min(settings.market_fee, settings.tender_fee,
            settings.adverse_move, settings.min_edge, settings.min_heat_ticks, settings.decision_ticks) < 0:
        parser.error("Use non-negative settings and a depth haircut below 100%")
    if args.command == "watch" and (args.interval < Decimal("0.25") or args.book_depth < 1
                                    or not 1 <= args.local_port <= 65535):
        parser.error("Use an interval of at least 0.25s, positive depth and a valid local port")

    try:
        if args.command == "demo":
            demo(settings)
        elif args.command == "watch":
            args.interval = float(args.interval)
            watch(args, settings)
        else:
            with args.input.open(encoding="utf-8") as source:
                for line in source:
                    snapshot = json.loads(line)
                    if snapshot["tenders"]:
                        print(f"Tick {snapshot['tick']:03d}")
                        report(snapshot, settings)
    except KeyboardInterrupt:
        print("\nStopped.")
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        sys.exit(f"LT3: {error}")


if __name__ == "__main__":
    main()
