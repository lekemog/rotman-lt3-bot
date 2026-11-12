import contextlib
from dataclasses import asdict
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

from dashboard import handler_for
from execution import DemoVenue, Desk, RITVenue, Strategy
from rotman_lt3 import CAP, demo_snapshot


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


class MockRIT:
    def __init__(self, side="BUY"):
        fixture = demo_snapshot()
        self.case = dict(name="Dublin LT3", period=1, tick=100, ticks_per_period=300, status="ACTIVE")
        self.positions = dict.fromkeys(CAP, 0)
        self.books = fixture["books"]
        self.tenders = [fixture["tenders"][-1]]
        self.tenders[0]["action"] = "SELL"
        self.side = side
        if side == "SELL":
            self.tenders[0].update(caption="An institution would like to BUY TAME.",
                                   quantity=-20_000, price="10.80", action="BUY")
        self.orders = {}
        self.writes = []
        self.authorization = None
        self.fail_read = False
        self.fail_accept_after_commit = False

    @property
    def ticker(self):
        return "CRZY" if self.side == "BUY" else "TAME"

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
                mock.authorization = self.headers.get("Authorization")
                url = urlsplit(self.path)
                if url.path == "/v1/case":
                    self.answer(mock.case, 503 if mock.fail_read else 200)
                elif url.path == "/v1/securities":
                    self.answer([dict(ticker=ticker, position=position) for ticker, position in mock.positions.items()])
                elif url.path == "/v1/orders":
                    self.answer([order for order in mock.orders.values() if order["status"] == "OPEN"])
                elif url.path == "/v1/tenders":
                    self.answer(mock.tenders)
                elif url.path == "/v1/securities/book":
                    self.answer(mock.books[parse_qs(url.query)["ticker"][0]])
                elif url.path.startswith("/v1/orders/"):
                    self.answer(mock.orders[int(url.path.split("/")[-1])])
                else:
                    self.answer({}, 404)

            def do_POST(self):
                url = urlsplit(self.path)
                mock.writes.append(("POST", url.path, parse_qs(url.query)))
                if url.path.startswith("/v1/tenders/"):
                    tender = mock.tenders.pop()
                    mock.positions[mock.ticker] = abs(tender["quantity"]) * (1 if mock.side == "BUY" else -1)
                    self.answer({"success": True}, 500 if mock.fail_accept_after_commit else 200)
                elif url.path == "/v1/orders":
                    params = parse_qs(url.query)
                    qty = int(params["quantity"][0])
                    action = params["action"][0]
                    ticker = params["ticker"][0]
                    id_ = len(mock.orders) + 1
                    fill = min(5000, qty) if id_ == 1 else qty
                    mock.positions[ticker] += fill if action == "BUY" else -fill
                    mock.orders[id_] = dict(order_id=id_, ticker=ticker, action=action, quantity=qty,
                                            quantity_filled=fill, vwap="9.98" if action == "SELL" else "10.00",
                                            status="OPEN" if fill < qty else "TRANSACTED")
                    self.answer(mock.orders[id_])
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
        desk = Desk(venue, "rit", Path(folder))
        try:
            yield desk
        finally:
            venue.close()


class ExecutionTests(unittest.TestCase):
    def review_payload(self, desk):
        state = desk.state()
        offer = state["tenders"][0]
        return dict(tender_id=offer["id"], heat=state["heat"], period=state["period"],
                    revision=state["revision"], ticker=offer["ticker"], side=offer["side"],
                    quantity=offer["quantity"], price=offer["price"])

    def test_review_requires_approval_and_rechecks_the_book(self):
        mock = MockRIT()
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", {"api_orders_enabled": True, "entry_mode": "review"})
            desk.poll()
            self.assertEqual(mock.writes, [])
            payload = self.review_payload(desk)
            with self.assertRaises(ValueError):
                desk.command("approve", dict(payload, quantity=payload["quantity"] + 1))
            with self.assertRaises(ValueError):
                desk.command("approve", dict(payload, heat=payload["heat"] - 1))
            desk.command("approve", payload)
            bids = mock.books["CRZY"]["bids"]
            mock.books["CRZY"]["bids"] = []
            desk.poll()
            self.assertEqual(mock.writes, [])
            self.assertEqual(desk.events[0]["event"], "APPROVAL LAPSED")
            mock.books["CRZY"]["bids"] = bids
            desk.poll()
            self.assertEqual(mock.writes, [])
            desk.command("approve", self.review_payload(desk))
            self.assertEqual(desk.state()["approval"], payload["tender_id"])
            desk.poll()
            self.assertEqual([write[1] for write in mock.writes], ["/v1/tenders/3"])
            self.assertFalse(desk.state()["inventory"]["confirmed"])

    def test_skip_holds_when_switching_to_auto(self):
        mock = MockRIT()
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("skip", self.review_payload(desk))
            desk.command("arm", {"api_orders_enabled": True})
            desk.poll()
            self.assertEqual(mock.writes, [])
            self.assertEqual(desk.state()["tenders"], [])

    def test_strategy_revision_invalidates_approval(self):
        mock = MockRIT()
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", {"api_orders_enabled": True, "entry_mode": "review"})
            payload = self.review_payload(desk)
            desk.command("approve", payload)
            desk.save_strategy(dict(asdict(desk.strategy), depth_haircut=15), expected_revision=1)
            with self.assertRaises(ValueError):
                desk.save_strategy(asdict(Strategy()), expected_revision=1)
            desk.poll()
            self.assertEqual(mock.writes, [])
            self.assertIsNone(desk.state()["approval"])

    def test_mac_dma_uses_basic_auth_and_sends_fixed_tender_price(self):
        mock = MockRIT()
        with TemporaryDirectory() as folder, server_for(mock.handler()) as server:
            def connect(host, port, timeout):
                self.assertEqual((host, port), ("flserver.rotman.utoronto.ca", 16540))
                return HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)

            with patch("rotman_lt3.HTTPConnection", side_effect=connect), patch("builtins.input", return_value="team"), patch("rotman_lt3.getpass", return_value="pass"):
                venue = RITVenue(None, True, mode="dma")
            try:
                desk = Desk(venue, "rit", Path(folder))
                desk.poll()
                self.assertEqual(mock.authorization, "Basic dGVhbTpwYXNz")
                self.assertEqual(desk.state()["connection"], "dma")
                desk.command("arm", {"api_orders_enabled": True})
                desk.poll()
                self.assertEqual(mock.writes[0], ("POST", "/v1/tenders/3", {"price": ["9.85"]}))
                desk.poll()
                self.assertEqual(mock.writes[1][1], "/v1/orders")
            finally:
                venue.close()

    def test_both_sides_fill_and_reconcile(self):
        for side, cap, action in (("BUY", 25_000, "SELL"), ("SELL", 10_000, "BUY")):
            with self.subTest(side=side):
                mock = MockRIT(side)
                with rit_desk(mock) as desk:
                    desk.poll()
                    self.assertEqual(mock.writes, [])
                    with self.assertRaises(ValueError):
                        desk.command("arm", {})
                    desk.command("arm", {"api_orders_enabled": True})
                    desk.poll()
                    self.assertEqual([write[1] for write in mock.writes], ["/v1/tenders/3"])
                    desk.command("pause", {})
                    desk.poll()
                    desk.working.sent_at -= 1
                    desk.poll()
                    self.assertEqual(mock.writes[-1][:2], ("DELETE", "/v1/orders/1"))
                    for _ in range(10):
                        desk.poll()
                    orders = [args for verb, path, args in mock.writes if verb == "POST" and path == "/v1/orders"]
                    self.assertGreater(len(orders), 1)
                    self.assertEqual(orders[0]["quantity"], [str(cap)])
                    self.assertTrue(all(row["action"] == [action] and int(row["quantity"][0]) <= cap for row in orders))
                    self.assertEqual(sum(order["quantity_filled"] for order in mock.orders.values()),
                                     50_000 if side == "BUY" else 20_000)
                    self.assertEqual(desk.completed, 1)
                    self.assertFalse(any(mock.positions.values()))
                    self.assertFalse(desk.fault)

    def test_read_only_and_expired_tender(self):
        mock = MockRIT()
        with rit_desk(mock, enable_orders=False) as desk:
            desk.poll()
            with self.assertRaises(ValueError):
                desk.command("arm", {"api_orders_enabled": True})
            self.assertEqual(mock.writes, [])
        mock = MockRIT()
        mock.tenders[0]["expires"] = 104
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", {"api_orders_enabled": True})
            desk.poll()
            self.assertEqual(mock.writes, [])

    def test_ambiguous_write_is_never_retried(self):
        mock = MockRIT()
        mock.fail_accept_after_commit = True
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", {"api_orders_enabled": True})
            desk.poll()
            desk.poll()
            self.assertEqual(len(mock.writes), 1)
            self.assertIn("could not be confirmed", desk.fault)
            self.assertFalse(desk.armed)
            with self.assertRaises(ValueError):
                desk.command("reset", {})

    def test_stop_cancels_only_managed_child(self):
        mock = MockRIT()
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", {"api_orders_enabled": True})
            desk.poll()
            desk.poll()
            desk.command("halt", {})
            for _ in range(5):
                desk.poll()
            self.assertEqual([action for action, _, _ in mock.writes], ["POST", "POST", "DELETE"])
            self.assertEqual(mock.positions["CRZY"], 45_000)
            self.assertTrue(desk.halted)

    def test_feed_recovers_with_entries_paused(self):
        mock = MockRIT()
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", {"api_orders_enabled": True})
            mock.fail_read = True
            desk.poll()
            self.assertFalse(desk.state()["fresh"])
            mock.fail_read = False
            desk.poll()
            self.assertTrue(desk.state()["fresh"])
            self.assertFalse(desk.armed)
            self.assertEqual(mock.writes, [])

    def test_larger_tame_needs_more_time(self):
        mock = MockRIT("SELL")
        mock.tenders[0].update(quantity=-55_000, expires=299)
        mock.case["tick"] = 262
        with rit_desk(mock) as desk:
            desk.poll()
            desk.command("arm", {"api_orders_enabled": True})
            self.assertEqual(desk.state()["tenders"][0]["reason"], "Heat closeout window")
            desk.poll()
            self.assertEqual(mock.writes, [])


class DashboardTests(unittest.TestCase):
    def test_profile_persistence_and_origin(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            venue = DemoVenue()
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
                    self.assertIn(b"Apply changes", page.read())
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
                finally:
                    conn.close()
                    server.shutdown()
                    thread.join()
                    venue.close()


if __name__ == "__main__":
    unittest.main()
