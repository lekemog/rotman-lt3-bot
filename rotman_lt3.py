"""Tender model for the RITCxDublin 2026 liability trading case.

Python 3.10+, standard library only. Start with `python rotman_lt3.py demo`.
Use `watch client --record practice.jsonl` with the Windows RIT Client running
and its REST API enabled. `watch dma` uses the published Dublin practice port.
`replay practice.jsonl` re-evaluates recorded snapshots with the chosen settings.

The model answers one question per tender: how much better or worse off is the
book if this block is added to the inventory already held? A tender that offsets
inventory is worth the market exit it saves. A tender that adds inventory is
worth its price against a child-by-child walk of the exit side of the book.
"""

import argparse
import base64
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from getpass import getpass
from http.client import HTTPConnection, HTTPException
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
    # Entry model
    market_fee: Decimal = Decimal("0.02")  # Used when RIT does not report a fee.
    tender_fee: Decimal = ZERO
    depth_haircut: int = 10        # % of displayed depth assumed to vanish.
    adverse_move: Decimal = CENT   # $/share the exit side is assumed to slip.
    refill: int = 50               # % of consumed depth assumed back by the next child.
    min_edge: Decimal = ZERO       # Required model profit, $/share of the tender.
    patience: Decimal = Decimal(1) # Extra edge to act early, in sigmas of the mid over the window left.
    decision_ticks: int = 3        # Never accept this close to tender expiry.
    # Time budget
    min_heat_ticks: int = 20
    ticks_per_child: int = 3
    exit_reserve_ticks: int = 10
    # Unwind
    sweep_band: int = 5            # Cents through the touch one child may take.
    limit_slippage_ticks: int = 1  # Cents of cushion on a fully covered child.
    rest_ms: int = 800             # How long an unfilled remainder rests.
    market_closeout_ticks: int = 12


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
    visible_pnl: Decimal | None   # One immediate sweep of the displayed book.
    stressed_pnl: Decimal | None  # The entry model. Decisions use this figure.
    exit_quantity: int            # Shares left to unwind in this ticker if accepted.
    netted: int                   # Shares of the tender that offset inventory.


@dataclass(frozen=True)
class ChildOrder:
    ticker: str
    side: str
    quantity: int
    limit: Decimal | None  # None is a market order.


def parse_tender(raw, mid=None):
    """Resolve the ticker and our side of the offer.

    The caption is what the trader is asked, so an unambiguous caption decides.
    The API `action` field is used when the caption cannot be read, and only if
    the price is on the favourable side of the market for that reading.
    """
    caption = raw.get("caption") or ""
    ticker_tokens = set(re.findall(r"\b(?:CRZY|TAME)\b", caption.upper()))
    ticker = raw.get("ticker")
    if ticker is None and len(ticker_tokens) == 1:
        ticker = next(iter(ticker_tokens))
    if ticker not in CAP or ticker_tokens - {ticker}:
        raise ValueError("Tender ticker is missing or ambiguous")
    if raw.get("is_fixed_bid") is not True:
        raise ValueError("Expected a fixed-price tender")
    quantity = abs(shares(raw["quantity"]))
    price = decimal(raw["price"])
    if quantity == 0 or price <= 0:
        raise ValueError("Expected a positive tender size and price")

    sides = {word.upper() for word in re.findall(
        r"\b(?:would you like|do you want)\s+to\s+(buy|sell)\b", caption, re.I
    )}
    institution_sides = re.findall(
        r"\b(?:institution(?:al client|al trader)?|client|counterparty)\s+"
        r"(?:would like to|wants to|is offering to|has offered to|will|wishes to)\s+"
        r"(buy|sell)\b", caption, re.I
    )
    sides.update("SELL" if word.upper() == "BUY" else "BUY" for word in institution_sides)
    if len(sides) > 1:
        raise ValueError("Confirm BUY/SELL in the RIT tender dialog")
    if sides:
        side = sides.pop()
    else:
        side = str(raw.get("action") or "").upper()
        mids = mid or {}
        reference = mids.get(ticker)
        if side not in ("BUY", "SELL") or reference is None or (side == "BUY") != (price < reference):
            raise ValueError("Confirm BUY/SELL in the RIT tender dialog")
    return Tender(shares(raw["tender_id"]), ticker, side, quantity, price,
                  shares(raw["expires"]), shares(raw["period"]))


def exit_levels(book, exit_side):
    """Aggregate unfilled order quantities at each price, best price first."""
    prices = defaultdict(int)
    selling = exit_side == "SELL"
    rows = book.get("bids", book.get("bid")) if selling else book.get("asks", book.get("ask"))
    for row in rows or ():
        left = shares(row["quantity"]) - shares(row.get("quantity_filled") or 0)
        price = decimal(row["price"])
        if left > 0 and price > 0:
            prices[price] += left
    return sorted(prices.items(), reverse=selling)


def mid_price(book):
    bids, asks = exit_levels(book, "SELL"), exit_levels(book, "BUY")
    return (bids[0][0] + asks[0][0]) / 2 if bids and asks else None


def sweep(levels, quantity, exit_side, haircut=0, adverse_move=ZERO, cap=None, refill=0):
    """Walk the exit side child by child.

    Each child takes at most `cap` shares. Liquidity taken by earlier children
    leaves a hole at the top of the book, and `refill` percent of that hole is
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
            stressed = price - adverse_move if exit_side == "SELL" else price + adverse_move
            if stressed <= 0:
                break
            got += take
            value += take * stressed
            worst = stressed
            if got == child:
                break
        filled += got
        hole += got
        if got < child:
            break
    return Sweep(filled, value, worst)


def flatten_value(position, levels, cap, fee, haircut, adverse_move, refill, extend):
    """Model cash from flattening a signed position, after market fees.

    `extend` prices shares beyond the displayed depth at the worst displayed
    price. It is used for inventory already held, where it understates the cost
    of the exit and therefore understates the benefit of a netting tender.
    """
    quantity = abs(position)
    if not quantity:
        return ZERO
    side = "SELL" if position > 0 else "BUY"
    walk = sweep(levels, quantity, side, haircut, adverse_move, cap, refill)
    value = walk.value
    if walk.filled < quantity:
        if not extend or walk.worst_price is None:
            return None
        value += (quantity - walk.filled) * walk.worst_price
    return (value if position > 0 else -value) - quantity * fee


def rules_for(snapshot, ticker, settings):
    rules = (snapshot.get("rules") or {}).get(ticker) or {}
    cap = shares(rules.get("cap") or 0) or CAP[ticker]
    fee = decimal(rules["fee"]) if rules.get("fee") is not None else settings.market_fee
    return cap, fee


def evaluate(tender, snapshot, settings):
    ticker = tender.ticker
    positions = snapshot["positions"]
    current = positions[ticker]
    new = current + tender.direction * tender.quantity
    netted = max(0, abs(current) - abs(new)) if current * tender.direction < 0 else 0
    cap, fee = rules_for(snapshot, ticker, settings)
    cash = -tender.direction * tender.quantity * tender.price - tender.quantity * settings.tender_fee

    def change(haircut, adverse, refill):
        book = snapshot["books"][ticker]
        after = flatten_value(new, exit_levels(book, "SELL" if new > 0 else "BUY"),
                              cap, fee, haircut, adverse, refill, extend=False)
        before = flatten_value(current, exit_levels(book, "SELL" if current > 0 else "BUY"),
                               cap, fee, haircut, adverse, refill, extend=True)
        return None if after is None or before is None else cash + after - before

    visible = change(0, ZERO, 0)
    stressed = change(settings.depth_haircut, settings.adverse_move, settings.refill)
    decision = Decision("WAIT", "", visible, stressed, abs(new), netted)

    tick = snapshot["tick"]
    left = tender.expires - tick
    if tender.period != snapshot["period"] or left <= 0:
        return replace(decision, status="EXPIRED", reason="Tender window closed")
    if left <= settings.decision_ticks:
        return replace(decision, status="PASS", reason="Tender at expiry buffer")
    children = (abs(new) + cap - 1) // cap
    budget = max(settings.min_heat_ticks,
                 settings.exit_reserve_ticks + settings.ticks_per_child * children)
    if new and abs(new) > abs(current) and snapshot["end_tick"] - tick <= budget:
        return replace(decision, status="PASS", reason="Heat closeout window")

    limits = snapshot.get("limits") or {}
    projected = dict(positions)
    projected[ticker] = new
    if (abs(sum(projected.values())) > (limits.get("net") or NET_LIMIT)
            or sum(map(abs, projected.values())) > (limits.get("gross") or GROSS_LIMIT)):
        return replace(decision, reason="Projected net or gross limit exceeded")
    if stressed is None:
        return replace(decision, reason="Displayed depth cannot cover the exit")
    if stressed <= tender.quantity * settings.min_edge:
        return replace(decision, reason="Model P&L below the acceptance threshold")
    # The tender is a free option until it expires and the mid has no drift, so
    # a thin edge is worth more held than exercised. Act early only when the edge
    # is already large against how far the mid can travel in the time left.
    window = max(0, min(left - settings.decision_ticks, snapshot["end_tick"] - tick - budget) - 1)
    sigma = decimal((snapshot.get("sigma") or {}).get(ticker) or 0)
    if not netted and stressed <= tender.quantity * (
            settings.min_edge + settings.patience * sigma * Decimal(window).sqrt()):
        return replace(decision, reason="Profitable now, holding the option for a better entry")
    reason = "Offsets inventory" if netted == tender.quantity else (
        "Offsets inventory, exit clears fees" if netted else "Modelled exit clears fees and edge")
    return replace(decision, status="ACCEPT", reason=reason)


def evaluate_all(snapshot, settings):
    """Evaluate every live tender, then look for opposing pairs.

    Two offers on opposite sides of one ticker can lock a spread between two
    institutions with little or no market exit. The first leg of such a pair can
    fail the model on its own, so it is accepted on the strength of the pair.
    Returns (tender, decision, raw) rows. Unreadable offers come back as
    (None, None, raw).
    """
    reference = mids(snapshot)
    rows = []
    for raw in snapshot["tenders"]:
        try:
            tender = parse_tender(raw, reference)
            rows.append([tender, evaluate(tender, snapshot, settings), raw])
        except (ValueError, KeyError, TypeError, ArithmeticError):
            rows.append([None, None, raw])
    for row in rows:
        first, alone, _ = row
        if (not first or alone.status != "WAIT" or alone.stressed_pnl is None
                or not alone.reason.startswith(("Model", "Profitable"))):
            continue
        positions = dict(snapshot["positions"])
        positions[first.ticker] += first.direction * first.quantity
        held = dict(snapshot, positions=positions)
        best = None
        for second, _, _ in rows:
            if (second and second.ticker == first.ticker and second.side != first.side
                    and second.expires - snapshot["tick"] > settings.decision_ticks + 2):
                follow = evaluate(second, held, settings)
                if follow.status == "ACCEPT" and (best is None or follow.stressed_pnl > best[1]):
                    best = second, follow.stressed_pnl
        if best and alone.stressed_pnl + best[1] > settings.min_edge * (first.quantity + best[0].quantity):
            row[1] = replace(alone, status="ACCEPT", stressed_pnl=alone.stressed_pnl + best[1],
                             reason=f"Pairs with #{best[0].id} on the other side")
    return [tuple(row) for row in rows]


def plan_child(ticker, position, book, cap, band, cushion, urgent):
    """Describe one inventory-reducing marketable limit order.

    A patient child takes the displayed liquidity within `band` of the touch and
    rests any remainder at the last price it took. An urgent child is priced to
    take the whole chunk from the displayed book.
    """
    if not position:
        return None
    side = "SELL" if position > 0 else "BUY"
    quantity = min(abs(position), cap)
    levels = exit_levels(book, side)
    if not levels:
        return None
    worse = -1 if side == "SELL" else 1
    best = levels[0][0]
    bound = None if urgent else best + worse * band
    reached, limit = 0, best
    for price, available in levels:
        if bound is not None and (price - bound) * worse > 0:
            break
        limit = price
        reached += available
        if reached >= quantity:
            break
    if reached >= quantity or urgent:
        limit += worse * cushion
        if bound is not None and (limit - bound) * worse > 0:
            limit = bound
    return ChildOrder(ticker, side, quantity, max(CENT, limit))


class RateLimited(Exception):
    def __init__(self, wait):
        self.wait = wait


class Rejected(RuntimeError):
    """RIT refused the request, so nothing was changed."""


class RITReader:
    def __init__(self, mode, port):
        if mode == "client":
            self.connection = HTTPConnection("127.0.0.1", port, timeout=2)
            self.headers = {"X-API-Key": getpass("RIT Client API key: ")}
        else:
            self.connection = HTTPConnection("flserver.rotman.utoronto.ca", 16540, timeout=2)
            credentials = f"{input('RIT Trader ID: ')}:{getpass('RIT password: ')}"
            self.headers = {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}
        self.limits = None, {}

    def request(self, method, endpoint, **params):
        path = f"/v1/{endpoint}"
        if params:
            path += "?" + urlencode(params)
        self.connection.request(method, path, headers=self.headers)
        response = self.connection.getresponse()
        body = response.read()
        if response.status == 429:
            try:
                wait = response.getheader("Retry-After") or json.loads(body)["wait"]
                wait = float(decimal(wait))
            except (ValueError, ArithmeticError, KeyError, TypeError):
                wait = 1.0
            raise RateLimited(max(0.05, wait))
        if 400 <= response.status < 500:
            raise Rejected(f"RIT refused {method} {endpoint} with HTTP {response.status}")
        if response.status != 200:
            raise RuntimeError(f"RIT {method} {endpoint} returned HTTP {response.status}")
        return json.loads(body), response

    def get(self, endpoint, **params):
        return self.request("GET", endpoint, **params)[0]

    def case_limits(self, period):
        if self.limits[0] != period:
            found = {}
            try:
                rows = self.get("limits")
                net = min((shares(row["net_limit"]) for row in rows if row.get("net_limit")), default=0)
                gross = min((shares(row["gross_limit"]) for row in rows if row.get("gross_limit")), default=0)
                found = {key: value for key, value in (("net", net), ("gross", gross)) if value > 0}
            except (Rejected, ValueError, KeyError, TypeError, ArithmeticError):
                pass  # The case brief limits apply.
            self.limits = period, found
        return self.limits[1]

    def snapshot(self, depth):
        started = time.monotonic()
        before = self.get("case")
        if before["status"] != "ACTIVE":
            return before["status"], None

        securities = {row["ticker"]: row for row in self.get("securities")}
        if set(securities) != set(CAP):
            raise RuntimeError("Expected exactly the Dublin case tickers CRZY and TAME")
        positions = {ticker: shares(securities[ticker]["position"]) for ticker in CAP}
        rules = {}
        for ticker in CAP:
            row = securities[ticker]
            rules[ticker] = dict(cap=shares(row.get("max_trade_size") or 0))
            if row.get("trading_fee") is not None:
                rules[ticker]["fee"] = str(decimal(row["trading_fee"]))
            if row.get("limit_order_rebate") is not None:
                rules[ticker]["rebate"] = str(decimal(row["limit_order_rebate"]))
        orders = [dict(id=shares(order["order_id"]), ticker=order["ticker"], action=order["action"],
                       quantity=shares(order["quantity"]), filled=shares(order.get("quantity_filled") or 0),
                       price=order.get("price"))
                  for order in self.get("orders", status="OPEN")]
        tenders = self.get("tenders")
        books = {}
        for ticker in CAP:
            book = self.get("securities/book", ticker=ticker, limit=depth)
            books[ticker] = {
                name: [{field: order.get(field, 0) for field in ("price", "quantity", "quantity_filled")}
                       for order in book.get(name, book.get(name[:-1])) or ()]
                for name in ("bids", "asks")
            }
        trader = self.get("trader")
        limits = self.case_limits(shares(before["period"]))
        after = self.get("case")
        tick = shares(after["tick"])
        period = shares(after["period"])
        if (after["status"] != "ACTIVE" or before["period"] != after["period"]
                or not 0 <= tick - shares(before["tick"]) <= 1
                or time.monotonic() - started > 1.5):
            return "SNAPSHOT TOO OLD OR HEAT CHANGED", None

        fields = ("tender_id", "ticker", "period", "tick", "expires", "caption", "action",
                  "is_fixed_bid", "price", "quantity")
        return "ACTIVE", dict(tick=tick, period=period, end_tick=shares(after["ticks_per_period"]),
                              case_name=after.get("name", "RIT"), positions=positions, books=books,
                              rules=rules, limits=limits, orders=orders, open_orders=len(orders),
                              nlv=trader.get("nlv"), fines=trader.get("total_fines"),
                              tenders=[{key: row[key] for key in fields if key in row} for row in tenders])


def mids(snapshot):
    return {ticker: mid_price(book) for ticker, book in snapshot["books"].items()}


def report(snapshot, settings):
    for tender, decision, raw in evaluate_all(snapshot, settings):
        if tender is None:
            print(f"  Tender {raw.get('tender_id', '?')}: REVIEW IN RIT")
            continue
        per_share = ("—" if decision.stressed_pnl is None
                     else f"{decision.stressed_pnl / tender.quantity * 100:+.1f}¢/sh")
        print(f"  #{tender.id} {tender.ticker} {tender.side} {tender.quantity:,} @ ${tender.price:.2f}"
              f" | {decision.status} — {decision.reason}"
              f" | sweep now {money(decision.visible_pnl)} | model {money(decision.stressed_pnl)} ({per_share})"
              f" | to unwind {decision.exit_quantity:,} | netted {decision.netted:,}"
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
                    open_orders=0, orders=[], books=dict(CRZY=book, TAME=book), tenders=[])
    for id_, price in enumerate(("9.90", "9.97", "9.85"), 1):
        snapshot["tenders"].append(dict(tender_id=id_, period=1, expires=125,
                                       caption="An institution would like to SELL 50,000 shares of CRZY"
                                               f" to you at a price of ${price}. Would you like to BUY"
                                               " the shares from them?",
                                       is_fixed_bid=True, price=price, quantity=50_000))
    return snapshot


def demo(settings):
    snapshot = demo_snapshot()
    print("The case brief's book at tick 100. The brief accepts $9.90 and declines $9.97."
          " The $9.85 offer is synthetic.")
    for tender in snapshot["tenders"]:
        report(dict(snapshot, tenders=[tender]), settings)
    held = dict(snapshot, positions=dict(CRZY=50_000, TAME=0), tenders=[dict(
        tender_id=4, period=1, expires=125, is_fixed_bid=True, price="10.02", quantity=40_000,
        caption="An institution would like to BUY 40,000 shares of CRZY from you at a price of $10.02."
                " Would you like to SELL the shares to them?")])
    print("Long 50,000 CRZY, then an institution bids for 40,000:")
    report(held, settings)
    child = plan_child("CRZY", 50_000, snapshot["books"]["CRZY"], CAP["CRZY"],
                       CENT * settings.sweep_band, CENT * settings.limit_slippage_ticks, urgent=False)
    print(f"First unwind child for a 50,000 long: {child.side} {child.quantity:,} {child.ticker}"
          f" at a limit of ${child.limit:.2f}.")


def self_test():
    snapshot = demo_snapshot()
    settings = Settings()
    accept, decline, strong = (parse_tender(raw) for raw in snapshot["tenders"])
    levels = exit_levels(snapshot["books"]["CRZY"], "SELL")
    assert sweep(levels, 50_000, "SELL").vwap == Decimal("9.9338")
    assert sweep(levels, 50_000, "SELL", cap=25_000, refill=0) == sweep(levels, 50_000, "SELL")
    assert sweep(levels, 50_000, "SELL", cap=25_000, refill=100).value == 2 * sweep(levels, 25_000, "SELL").value
    assert sweep(levels, 90_000, "SELL").filled == 65_000

    quote = evaluate(accept, snapshot, settings)
    assert quote.visible_pnl == Decimal("690") and quote.stressed_pnl == Decimal("796")
    assert quote.status == "ACCEPT" and quote.exit_quantity == 50_000 and quote.netted == 0
    assert evaluate(decline, snapshot, settings).status == "WAIT"
    assert evaluate(decline, snapshot, settings).visible_pnl == Decimal("-2810")
    assert evaluate(strong, snapshot, settings).status == "ACCEPT"
    full_sweep = replace(settings, refill=0)
    assert evaluate(accept, snapshot, full_sweep).stressed_pnl == Decimal("-279")
    assert evaluate(accept, snapshot, replace(settings, tender_fee=CENT * 2)).status == "WAIT"
    volatile = dict(snapshot, sigma=dict(CRZY="0.02"))
    assert evaluate(accept, volatile, settings).reason.startswith("Profitable now")
    assert evaluate(accept, volatile, replace(settings, patience=ZERO)).status == "ACCEPT"
    assert evaluate(strong, volatile, replace(settings, patience=Decimal("0.5"))).status == "ACCEPT"
    assert evaluate(replace(accept, expires=104), volatile, settings).status == "ACCEPT"
    assert evaluate(accept, dict(snapshot, rules=dict(CRZY=dict(cap=25_000, fee="0.05"))), settings).status == "WAIT"

    short = replace(strong, side="SELL", ticker="TAME", quantity=6_000, price=Decimal("10.10"))
    assert evaluate(short, snapshot, settings).visible_pnl == Decimal("450")
    assert evaluate(replace(short, quantity=54_000), snapshot, settings).visible_pnl is None
    assert evaluate(replace(strong, quantity=100_001), snapshot, settings).reason.startswith("Projected")
    gross_case = dict(snapshot, positions=dict(CRZY=130_000, TAME=-120_000))
    assert evaluate(strong, gross_case, settings).reason.startswith("Projected")
    assert evaluate(strong, dict(snapshot, limits=dict(net=40_000)), settings).reason.startswith("Projected")
    assert evaluate(strong, dict(snapshot, tick=122), settings).status == "PASS"
    assert evaluate(replace(strong, expires=300), dict(snapshot, tick=280), settings).status == "PASS"
    assert evaluate(replace(strong, period=2), snapshot, settings).status == "EXPIRED"

    # A tender against inventory is worth the market exit it saves.
    long = dict(snapshot, positions=dict(CRZY=50_000, TAME=0))
    offset = replace(strong, side="SELL", quantity=50_000, price=Decimal("9.96"))
    netting = evaluate(offset, dict(long, sigma=dict(CRZY="0.50")), settings)
    assert netting.status == "ACCEPT" and netting.netted == 50_000 and netting.exit_quantity == 0
    assert evaluate(replace(offset, expires=300), dict(long, tick=295), settings).status == "ACCEPT"
    assert evaluate(replace(offset, price=Decimal("9.80")), long, settings).status == "WAIT"
    assert evaluate(strong, dict(snapshot, positions=dict(CRZY=60_000, TAME=0)), settings).reason.startswith("Projected")

    # Neither leg of this pair clears the model alone. Together they lock 4c a share.
    pair = dict(snapshot, tenders=[
        dict(snapshot["tenders"][1], tender_id=8, price="9.96"),
        dict(snapshot["tenders"][1], tender_id=9, price="10.00",
             caption="An institution would like to BUY 50,000 shares of CRZY from you at a price of $10.00."
                     " Would you like to SELL the shares to them?")])
    (buy, first, _), (sell, second, _) = evaluate_all(pair, settings)
    assert evaluate(buy, pair, settings).status == evaluate(sell, pair, settings).status == "WAIT"
    assert first.status == "ACCEPT" and first.reason == "Pairs with #9 on the other side"
    assert first.stressed_pnl == second.stressed_pnl == Decimal("2000") and second.status == "ACCEPT"

    book = snapshot["books"]["CRZY"]
    patient = plan_child("CRZY", 50_000, book, 25_000, CENT * 5, CENT, urgent=False)
    assert patient == ChildOrder("CRZY", "SELL", 25_000, Decimal("9.94"))
    assert plan_child("CRZY", 50_000, book, 25_000, CENT * 2, CENT, False).limit == Decimal("9.98")
    assert plan_child("CRZY", 50_000, book, 50_000, CENT * 5, CENT, True).limit == Decimal("9.87")
    assert plan_child("TAME", -3_000, book, 10_000, CENT * 5, CENT, False) == ChildOrder(
        "TAME", "BUY", 3_000, Decimal("10.01"))
    assert plan_child("TAME", -30_000, book, 10_000, CENT * 5, CENT, False).limit == Decimal("10.03")
    assert plan_child("CRZY", 0, book, 25_000, CENT, CENT, False) is None
    assert plan_child("CRZY", 5_000, dict(bids=[], asks=[]), 25_000, CENT, CENT, True) is None
    order = dict(price="9.99", quantity=10_000, quantity_filled=9_000)
    assert exit_levels(dict(bids=book["bids"] + [order]), "SELL")[0] == (Decimal("9.99"), 1_000)
    assert exit_levels(dict(bid=book["bids"]), "SELL") == levels

    raw = dict(snapshot["tenders"][0], ticker="CRZY", quantity=-50_000, price="10.05",
               caption="An institution would like to BUY CRZY. Would you like to SELL the shares?")
    assert parse_tender(raw).side == "SELL"
    try:
        parse_tender(dict(raw, caption="An institution would like to BUY CRZY. Would you like to BUY the shares?"))
        raise AssertionError("A contradictory caption should be reviewed manually")
    except ValueError:
        pass
    bare = dict(raw, caption="CRZY block", action="SELL")
    assert parse_tender(bare, dict(CRZY=Decimal("9.99"))).side == "SELL"
    for reference in (None, dict(CRZY=Decimal("10.20"))):
        try:
            parse_tender(bare, reference)
            raise AssertionError("An unreadable caption needs a favourable price to trust the action field")
        except ValueError:
            pass
    print("LT3 self-test passed.")


def watch(args, settings):
    client = RITReader(args.mode, args.local_port)
    record = args.record.open("x", encoding="utf-8") if args.record else None
    try:
        while True:
            try:
                status, snapshot = client.snapshot(args.book_depth)
            except RateLimited as error:
                print(f"Rate limited. Recommendations paused for {error.wait:.2f}s.", flush=True)
                time.sleep(error.wait)
                continue
            except (OSError, HTTPException, RuntimeError) as error:
                client.connection.close()
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
                    record.write(json.dumps(snapshot, default=str) + "\n")
                    record.flush()
            time.sleep(args.interval)
    finally:
        if record:
            record.close()
        client.connection.close()


def add_settings(parser):
    parser.add_argument("--market-fee", type=decimal, default=Settings.market_fee)
    parser.add_argument("--tender-fee", type=decimal, default=Settings.tender_fee)
    parser.add_argument("--depth-haircut", type=int, default=Settings.depth_haircut,
                        help="Percent of displayed depth removed in the model")
    parser.add_argument("--adverse-move", type=decimal, default=Settings.adverse_move,
                        help="Model price slip in dollars per share")
    parser.add_argument("--refill", type=int, default=Settings.refill,
                        help="Percent of consumed depth assumed back before the next child")
    parser.add_argument("--min-edge", type=decimal, default=Settings.min_edge,
                        help="Required model profit in dollars per share")
    parser.add_argument("--min-heat-ticks", type=int, default=Settings.min_heat_ticks)
    parser.add_argument("--decision-ticks", type=int, default=Settings.decision_ticks)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    add_settings(commands.add_parser("demo"))
    commands.add_parser("self-test")
    replay_parser = commands.add_parser("replay")
    replay_parser.add_argument("input", type=Path)
    add_settings(replay_parser)
    watch_parser = commands.add_parser("watch")
    watch_parser.add_argument("mode", choices=("client", "dma"))
    watch_parser.add_argument("--record", type=Path, help="New JSONL file for practice snapshots")
    watch_parser.add_argument("--interval", type=decimal, default=Decimal("1.0"))
    watch_parser.add_argument("--local-port", type=int, default=9999)
    watch_parser.add_argument("--book-depth", type=int, default=200)
    add_settings(watch_parser)
    args = parser.parse_args()
    if args.command == "self-test":
        self_test()
        return

    settings = Settings(market_fee=args.market_fee, tender_fee=args.tender_fee,
                        depth_haircut=args.depth_haircut, adverse_move=args.adverse_move,
                        refill=args.refill, min_edge=args.min_edge,
                        min_heat_ticks=args.min_heat_ticks, decision_ticks=args.decision_ticks)
    if (not 0 <= settings.depth_haircut < 100 or not 0 <= settings.refill <= 100
            or min(settings.market_fee, settings.tender_fee, settings.adverse_move, settings.min_edge,
                   settings.min_heat_ticks, settings.decision_ticks) < 0):
        parser.error("Use non-negative settings, a depth haircut below 100% and a refill of 0 to 100%")
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
