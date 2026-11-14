"""Run the LT3 browser dashboard on this computer."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Event, Thread
import webbrowser

from execution import DemoVenue, Desk, RITVenue
from rotman_lt3 import RateLimited


WEB = Path(__file__).resolve().parent / "web"
ASSETS = {"/": ("index.html", "text/html"), "/desk.css": ("desk.css", "text/css"),
          "/desk.js": ("desk.js", "text/javascript")}


def handler_for(desk, port):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send_data(self, status, data, content_type):
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def send_json(self, status, data):
            self.send_data(status, json.dumps(data, default=str).encode(), "application/json")

        def do_GET(self):
            if self.headers.get("Host") not in hosts:
                self.send_json(403, {"error": "Open the dashboard on localhost"})
            elif self.path == "/api/state":
                self.send_json(200, desk.state())
            elif self.path in ASSETS:
                name, content_type = ASSETS[self.path]
                self.send_data(200, (WEB / name).read_bytes(), content_type)
            else:
                self.send_json(404, {"error": "Unknown route"})

        def do_POST(self):
            host = self.headers.get("Host")
            if host not in hosts or self.headers.get("Origin") != f"http://{host}":
                self.send_json(403, {"error": "Desk controls require a local dashboard tab"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192 or self.headers.get_content_type() != "application/json":
                    raise ValueError("Expected a small JSON request")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected a JSON object")
                if self.path == "/api/strategy":
                    desk.save_strategy(data, expected_revision=data.pop("expected_revision", None))
                elif self.path == "/api/command":
                    desk.command(data.get("action"), data)
                else:
                    self.send_json(404, {"error": "Unknown route"})
                    return
            except (ValueError, TypeError, OSError, ArithmeticError) as error:
                self.send_json(400, {"error": str(error)})
                return
            self.send_json(200, desk.state())

    return Handler


def poll_loop(desk, stop, idle=0.55, busy=0.55):
    """Poll quickly while a tender or inventory is on the clock, and steadily otherwise.

    A rate-limit reply means RIT acted on nothing. The desk stays armed, waits as
    told, and polls a little slower for the rest of the session.
    """
    while not stop.is_set():
        try:
            desk.poll()
            delay = busy if desk.busy() else idle
        except RateLimited as error:
            desk.rate_limited()
            busy = min(idle, busy + 0.1)
            delay = max(busy, min(error.wait, 2.0))
        stop.wait(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--windows", "--rit", action="store_true", help="Use the Windows RIT Client REST API")
    connection.add_argument("--mac", action="store_true", help="Use the Rotman DMA API on a MacBook")
    parser.add_argument("--enable-orders", action="store_true", help="Enable the arm control for RIT")
    parser.add_argument("--rit-port", type=int, help="RIT port (Windows local default 9999, Mac practice default 16540)")
    parser.add_argument("--dma-https", action="store_true", help="Use HTTPS when Rotman supplies a TLS DMA port")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-dir", type=Path, default=Path(".rotman"))
    parser.add_argument("--record", action="store_true", help="Record RIT snapshots locally for replay")
    parser.add_argument("--no-open", action="store_true", help="Leave opening the browser to the operator")
    parser.add_argument("--interval", type=float, default=0.55, help="Seconds between polls when nothing is live")
    parser.add_argument("--busy-interval", type=float,
                        help="Seconds between polls while a tender or inventory is live"
                             " (default 0.2 on Windows, 0.4 on a MacBook, 0.55 in rehearsal)")
    parser.add_argument("--book-depth", type=int, default=50,
                        help="Orders requested per side of each book. Raise it if large tenders show as uncovered")
    args = parser.parse_args()
    live = args.windows or args.mac
    if args.enable_orders and not live:
        parser.error("Use --enable-orders with --windows or --mac. Rehearsal can be armed from the dashboard.")
    if args.dma_https and not args.mac:
        parser.error("Use --dma-https with --mac")
    if not 1 <= args.port <= 65535 or (args.rit_port is not None and not 1 <= args.rit_port <= 65535):
        parser.error("Use port numbers between 1 and 65535")
    if args.busy_interval is None:
        args.busy_interval = 0.4 if args.mac else 0.2 if args.windows else args.interval
    if not 0.05 <= args.busy_interval <= args.interval <= 5:
        parser.error("Use 0.05 <= --busy-interval <= --interval <= 5 seconds")
    if not 20 <= args.book_depth <= 500:
        parser.error("Use a book depth between 20 and 500 orders")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    venue = RITVenue(args.rit_port, args.enable_orders, mode="dma" if args.mac else "client",
                     dma_https=args.dma_https) if live else DemoVenue()
    desk = Desk(venue, "rit" if live else "rehearsal", args.state_dir, args.record)
    desk.book_depth = args.book_depth
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(desk, args.port)) as server:
        stop = Event()
        worker = Thread(target=poll_loop, args=(desk, stop, args.interval, args.busy_interval), daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{args.port}"
        print(f"Rotman LT3 desk · {url}", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            worker.join(timeout=3)
            venue.close()
            if desk.inventory or desk.working:
                print("Execution was active. Check positions and working orders directly in RIT.")


if __name__ == "__main__":
    main()
