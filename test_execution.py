import contextlib
from dataclasses import asdict, replace
from decimal import Decimal
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from backtest import run_heat
from dashboard import handler_for
from execution import Desk, RITVenue, Strategy
from rotman_lt3 import CAP, demo_snapshot
from simulator import SCENARIOS, SimVenue


@contextlib.contextmanager
def server_for(handler):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


BUY_TENDER = dict(tender_id=3, period=1, expires=125, is_fixed_bid=True, price="9.85", quantity=50_000,
                  action="BUY", caption="An institution would like to SELL 50,000 shares of CRZY to you at a"
                                        " price of $9.85. Would you like to BUY the shares from them?")
SELL_TENDER = dict(tender_id=4, period=1, expires=125, is_fixed_bid=True, price="10.80", quantity=20_000,
                   action="SELL", caption="An institution would like to BUY 20,000 shares of TAME from you at"
                                          " a price of $10.80. Would you like to SELL the shares to them?")


class MockRIT:
    """A local stand-in for the RIT Client REST API with a deep, static book."""

    def __init__(self, tenders=(BUY_TENDER,)):
        self.case = dict(name="Dublin LT3", period=1, tick=100, ticks_per_period=300, status="ACTIVE")
        self.positions = dict.fromkeys(CAP, 0)
        self.books = demo_snapshot()["books"]
        self.tenders = [dict(row) for row in tenders]
        self.orders = {}
        self.writes = []
        self.extremes = {ticker: [0, 0] for ticker in CAP}
        self.rest_orders = {1}           # Order ids that fill 5,000 shares and rest.
        self.fail_read = False
        self.fail_accept_after_commit = False
        self.fail_order_after_commit = set()
        self.reject_orders = 0

    def move(self, ticker, change):
        self.positions[ticker] += change
        low, high = self.extremes[ticker]
        self.extremes[ticker] = [min(low, self.positions[ticker]), max(high, self.positions[ticker])]

    def paths(self, verb):
        return [path for action, path, _ in self.writes if action == verb]

    def handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def answer(self, data, status=200):
                payload = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                url = urlsplit(self.path)
                if url.path == "/v1/case":
                    self.answer(mock.case, 503 if mock.fail_read else 200)
                elif url.path == "/v1/securities":
                    self.answer([dict(ticker=ticker, position=float(position), trading_fee=0.02,
                                      limit_order_rebate=0, max_trade_size=CAP[ticker])
                                 for ticker, position in mock.positions.items()])
                elif url.path == "/v1/orders":
                    self.answer([order for order in mock.orders.values() if order["status"] == "OPEN"])
                elif url.path == "/v1/tenders":
                    self.answer(mock.tenders)
                elif url.path == "/v1/trader":
                    self.answer(dict(trader_id="mock", nlv=1234.5, total_fines=0))
                elif url.path == "/v1/limits":
                    self.answer([dict(name="LIMIT-STOCK", net_limit=100_000, gross_limit=250_000)])
                elif url.path == "/v1/securities/book":
                    self.answer(mock.books[parse_qs(url.query)["ticker"][0]])
                elif url.path.startswith("/v1/orders/"):
                    self.answer(mock.orders[int(url.path.split("/")[-1])])
                else:
                    self.answer({}, 404)

            def do_POST(self):
                url = urlsplit(self.path)
                params = parse_qs(url.query)
                mock.writes.append(("POST", url.path, params))
                if url.path.startswith("/v1/tenders/"):
                    id_ = int(url.path.split("/")[-1])
                    tender = next((row for row in mock.tenders if row["tender_id"] == id_), None)
                    if tender is None:
                        self.answer({"code": "NOT_FOUND"}, 404)
                        return
                    mock.tenders.remove(tender)
                    ticker = "CRZY" if "CRZY" in tender["caption"] else "TAME"
                    mock.move(ticker, tender["quantity"] * (1 if tender["action"] == "BUY" else -1))
                    self.answer({"success": True}, 500 if mock.fail_accept_after_commit else 200)
                elif url.path == "/v1/orders":
                    if mock.reject_orders:
                        mock.reject_orders -= 1
                        self.answer({"code": "BAD_REQUEST"}, 400)
                        return
                    qty = int(params["quantity"][0])
                    action = params["action"][0]
                    ticker = params["ticker"][0]
                    id_ = len(mock.orders) + 1
                    fill = min(5000, qty) if id_ in mock.rest_orders else qty
                    mock.move(ticker, fill if action == "BUY" else -fill)
                    mock.orders[id_] = dict(order_id=id_, ticker=ticker, action=action, quantity=qty,
                                            type=params["type"][0], price=params.get("price", [None])[0],
                                            quantity_filled=fill, vwap=9.98 if action == "SELL" else 10.0,
                                            status="OPEN" if fill < qty else "TRANSACTED")
                    self.answer(mock.orders[id_], 500 if id_ in mock.fail_order_after_commit else 200)
                elif url.path == "/v1/commands/cancel":
                    ids = [id_ for id_, order in mock.orders.items()
                           if order["status"] == "OPEN" and order["ticker"] == params["ticker"][0]]
                    for id_ in ids:
                        mock.orders[id_]["status"] = "CANCELLED"
                    self.answer({"cancelled_order_ids": ids})
                else:
                    self.answer({}, 404)

            def do_DELETE(self):
                mock.writes.append(("DELETE", self.path, {}))
                mock.orders[int(self.path.split("/")[-1])]["status"] = "CANCELLED"
                self.answer({"success": True})

        return Handler


@contextlib.contextmanager
def rit_desk(mock, enable_orders=True):
    with TemporaryDirectory() as folder, server_for(mock.handler()) as server:
        with patch("rotman_lt3.getpass", return_value="mock-api-key"):
            venue = RITVenue(server.server_port, enable_orders)
        desk = Desk(venue, "rit", Path(folder), clock=Clock())
        try:
            yield desk
        finally:
            venue.close()


def run(desk, polls, step=0.5):
    for _ in range(polls):
        desk.poll()
        desk.clock.now += step


ACK = {"api_orders_enabled": True}


class ExecutionTests(unittest.TestCase):
    def test_both_sides_fill_and_flatten(self):
        for tender, ticker, cap, action in ((BUY_TENDER, "CRZY", 25_000, "SELL"),
                                            (SELL_TENDER, "TAME", 10_000, "BUY")):
            with self.subTest(ticker=ticker):
                mock = MockRIT([tender])
                with rit_desk(mock) as desk:
                    desk.poll()
                    self.assertEqual(mock.writes, [])
                    with self.assertRaises(ValueError):
                        desk.command("arm", {})
                    desk.command("arm", ACK)
                    desk.poll()
                    self.assertEqual(mock.paths("POST"), [f"/v1/tenders/{tender['tender_id']}"])
                    run(desk, 12)
                    orders = [args for verb, path, args in mock.writes if path == "/v1/orders"]
                    self.assertEqual(orders[0]["quantity"], [str(cap)])
                    self.assertTrue(all(row["action"] == [action] and int(row["quantity"][0]) <= cap
                                        for row in orders))
                    self.assertEqual(mock.paths("DELETE"), ["/v1/orders/1"])
                    self.assertFalse(any(mock.positions.values()))
                    low, high = mock.extremes[ticker]
                    self.assertEqual(min(abs(low), abs(high)), 0)
                    self.assertEqual(desk.accepted, 1)
                    self.assertFalse(desk.legs or desk.unsure or desk.alert)
                    state = desk.state()
                    self.assertEqual(state["nlv"], Decimal("1234.5"))
                    self.assertEqual(state["rules"]["CRZY"], dict(cap=25_000, fee=Decimal("0.02")))

    def test_filled_children_chain_without_waiting(self):
        mock = MockRIT()
        mock.rest_orders = set()
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", ACK)
            run(desk, 4)  # Accept, then one child per poll.
            self.assertEqual(len(mock.orders), 2)
            self.assertFalse(any(mock.positions.values()))

    def test_monitor_mode_and_expiring_tender(self):
        mock = MockRIT()
        with rit_desk(mock, enable_orders=False) as desk:
            desk.poll()
            for action in ("arm", "unwind"):
                with self.assertRaises(ValueError):
                    desk.command(action, ACK)
            self.assertEqual(desk.state()["tenders"][0]["status"], "ACCEPT")
            self.assertEqual(mock.writes, [])
        mock = MockRIT()
        mock.tenders[0]["expires"] = 103
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", ACK)
            run(desk, 3)
            self.assertEqual(mock.writes, [])

    def test_unanswered_accept_is_not_repeated_and_inventory_is_unwound(self):
        mock = MockRIT()
        mock.fail_accept_after_commit = True
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", ACK)
            run(desk, 14)
            self.assertEqual(mock.paths("POST").count("/v1/tenders/3"), 1)
            self.assertFalse(any(mock.positions.values()))
            self.assertEqual(mock.extremes["CRZY"], [0, 50_000])

    def test_unanswered_order_is_cancelled_before_sending_again(self):
        mock = MockRIT()
        mock.fail_order_after_commit = {1}
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", ACK)
            run(desk, 3)
            self.assertIn("CRZY", desk.unsure)
            self.assertIn("/v1/commands/cancel", mock.paths("POST"))
            self.assertEqual(len(mock.orders), 1)
            run(desk, 12)
            self.assertFalse(any(mock.positions.values()) or desk.unsure)
            self.assertEqual(mock.extremes["CRZY"], [0, 50_000])

    def test_stop_cancels_the_child_and_unwind_resumes(self):
        mock = MockRIT()
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", ACK)
            run(desk, 2, step=0.1)
            desk.command("halt", {})
            run(desk, 6)
            self.assertEqual([verb for verb, _, _ in mock.writes], ["POST", "POST", "DELETE"])
            self.assertEqual(mock.positions["CRZY"], 45_000)
            self.assertTrue(desk.halted)
            desk.command("unwind", ACK)
            run(desk, 8)
            self.assertFalse(any(mock.positions.values()) or desk.armed)

    def test_feed_outage_keeps_the_desk_armed(self):
        mock = MockRIT()
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", ACK)
            mock.fail_read = True
            desk.poll()
            self.assertFalse(desk.state()["fresh"])
            self.assertEqual(mock.writes, [])
            mock.fail_read = False
            run(desk, 12)
            self.assertTrue(desk.armed)
            self.assertFalse(any(mock.positions.values()))

    def test_unwind_only_flattens_a_manually_accepted_tender(self):
        mock = MockRIT()
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("unwind", ACK)
            run(desk, 3)
            self.assertEqual(mock.writes, [])
            mock.tenders.clear()
            mock.move("CRZY", 50_000)  # The trader accepts in the RIT dialog.
            run(desk, 12)
            self.assertNotIn("/v1/tenders/3", mock.paths("POST"))
            self.assertFalse(any(mock.positions.values()))

    def test_waits_for_an_order_it_did_not_send(self):
        mock = MockRIT([])
        mock.positions["TAME"] = -8_000
        mock.orders[1] = dict(order_id=1, ticker="TAME", action="BUY", quantity=8_000, type="LIMIT",
                              price=9.9, quantity_filled=0, vwap=None, status="OPEN")
        mock.rest_orders = set()
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("unwind", ACK)
            run(desk, 4)
            self.assertEqual(mock.writes, [])
            self.assertIn("did not send", desk.state()["alert"])
            mock.orders[1]["status"] = "CANCELLED"
            run(desk, 3)
            self.assertFalse(any(mock.positions.values()))
            self.assertEqual(mock.extremes["TAME"], [0, 0])

    def test_netting_tender_pulls_the_working_child_first(self):
        mock = MockRIT()
        opposite = dict(tender_id=7, period=1, expires=140, is_fixed_bid=True, price="10.02",
                        quantity=40_000, action="SELL",
                        caption="An institution would like to BUY 40,000 shares of CRZY from you at a price"
                                " of $10.02. Would you like to SELL the shares to them?")
        with rit_desk(mock) as desk:
            desk.strategy = replace(Strategy(), rest_ms=5000)
            desk.poll()
            desk.command("arm", ACK)
            run(desk, 2, step=0.1)         # Accept, then child #1 fills 5,000 and rests.
            self.assertEqual(mock.orders[1]["status"], "OPEN")
            mock.tenders.append(opposite)
            run(desk, 1, step=0.1)
            self.assertEqual(mock.orders[1]["status"], "CANCELLED")
            self.assertNotIn("/v1/tenders/7", mock.paths("POST"))
            run(desk, 10)
            self.assertIn("/v1/tenders/7", mock.paths("POST"))
            self.assertFalse(any(mock.positions.values()))
            self.assertEqual(mock.extremes["CRZY"], [0, 50_000])
            self.assertEqual(desk.accepted, 2)

    def test_rejected_order_is_retried_later(self):
        mock = MockRIT()
        mock.reject_orders = 1
        mock.rest_orders = set()
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", ACK)
            run(desk, 2)
            self.assertEqual(mock.positions["CRZY"], 50_000)
            self.assertFalse(desk.unsure)
            run(desk, 6)
            self.assertFalse(any(mock.positions.values()))

    def test_time_budget_and_market_closeout(self):
        mock = MockRIT([dict(SELL_TENDER, quantity=55_000, expires=299)])
        mock.case["tick"] = 272
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", ACK)
            self.assertEqual(desk.state()["tenders"][0]["reason"], "Heat closeout window")
            run(desk, 2)
            self.assertEqual(mock.writes, [])
        mock = MockRIT([])
        mock.case["tick"] = 292
        mock.positions["CRZY"] = 30_000
        mock.rest_orders = set()
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("unwind", ACK)
            run(desk, 3)
            self.assertEqual([order["type"] for order in mock.orders.values()], ["MARKET", "MARKET"])
            self.assertFalse(any(mock.positions.values()))

    def test_new_heat_keeps_the_desk_armed(self):
        mock = MockRIT([])
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", ACK)
            mock.case.update(period=2, tick=3)
            mock.tenders.append(dict(BUY_TENDER, period=2, expires=33))
            mock.rest_orders = set()
            run(desk, 6)
            self.assertTrue(desk.armed)
            self.assertEqual(desk.accepted, 1)
            self.assertFalse(any(mock.positions.values()))


class SimulationTests(unittest.TestCase):
    def test_every_scenario_ends_flat_within_limits(self):
        for scenario in SCENARIOS:
            if scenario.name in ("Late depth drought", "Slow polling"):
                continue  # Liquidity or control is deliberately withheld in these.
            with self.subTest(scenario=scenario.name):
                for seed in (1, 2, 3):
                    _, accepted, residual = run_heat(scenario, seed, Strategy())
                    self.assertEqual(residual, 0)
                    self.assertLessEqual(accepted, 5)

    def test_opposing_tenders_are_netted(self):
        scenario = next(item for item in SCENARIOS if item.name == "Opposing tenders")
        netted = 0
        for seed in range(1, 9):
            clock = Clock()
            venue = SimVenue(scenario.name, seed, clock)
            desk = Desk(venue, "rehearsal", clock=clock)
            messages = []
            desk.log = lambda event, message: messages.append(message)
            desk.poll()
            desk.command("arm", {})
            while clock.now < 100 + 300:
                desk.poll()
                clock.now += 0.5
                self.assertLessEqual(abs(sum(venue.positions.values())), 100_000)
            netted += sum("Offsets" in message or "Pairs" in message for message in messages)
            self.assertFalse(any(venue.positions.values()))
        self.assertGreater(netted, 0)


class DashboardTests(unittest.TestCase):
    def test_profile_persistence_and_origin(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            venue = SimVenue()
            desk = Desk(venue, "rehearsal", directory)
            with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as server:
                server.RequestHandlerClass = handler_for(desk, server.server_port)
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    conn.request("GET", "/")
                    page = conn.getresponse()
                    self.assertEqual(page.status, 200)
                    self.assertIn(b"Apply strategy", page.read())
                    payload = json.dumps(dict(asdict(Strategy()), adverse_move=Decimal("0.02")), default=str)
                    headers = {"Content-Type": "application/json"}
                    conn.request("POST", "/api/strategy", payload, headers)
                    blocked = conn.getresponse()
                    self.assertEqual(blocked.status, 403)
                    blocked.read()
                    headers["Origin"] = f"http://127.0.0.1:{server.server_port}"
                    conn.request("POST", "/api/strategy", payload, headers)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())["revision"], 2)
                    self.assertEqual(Desk(venue, "rehearsal", directory).strategy.adverse_move, Decimal("0.02"))
                    (directory / "strategy.json").write_text('{"depth_haircut": 10}', encoding="utf-8")
                    self.assertEqual(Desk(venue, "rehearsal", directory).strategy, Strategy())
                finally:
                    conn.close()
                    server.shutdown()
                    thread.join()
                    venue.close()


if __name__ == "__main__":
    unittest.main()
