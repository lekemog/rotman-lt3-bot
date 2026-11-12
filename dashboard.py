"""Run the LT3 browser dashboard on this computer."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Event, Thread
import webbrowser

from execution import Desk, RITVenue
from simulator import SimVenue
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
                    desk.save_strategy(data)
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


def poll_loop(desk, stop, interval):
    while not stop.is_set():
        try:
            desk.poll()
            delay = interval
        except RateLimited as error:
            delay = min(2.0, error.wait)
        except Exception as error:  # Keep the unwind alive through anything unexpected.
            desk.log("ERROR", f"{type(error).__name__}: {error}")
            delay = interval
        stop.wait(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rit", action="store_true", help="Connect to the Windows RIT Client REST API")
    parser.add_argument("--enable-orders", action="store_true", help="Enable the arm control for RIT")
    parser.add_argument("--rit-port", type=int, default=9999)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-dir", type=Path, default=Path(".rotman"))
    parser.add_argument("--record", action="store_true", help="Record snapshots under the state directory for calibrate.py")
    parser.add_argument("--no-open", action="store_true", help="Leave opening the browser to the operator")
    parser.add_argument("--interval", type=float, default=0.4, help="Seconds between RIT polls")
    args = parser.parse_args()
    if args.enable_orders and not args.rit:
        parser.error("Use --enable-orders with --rit. Rehearsal can be armed from the dashboard.")
    if not all(1 <= port <= 65535 for port in (args.port, args.rit_port)):
        parser.error("Use port numbers between 1 and 65535")
    if not 0.1 <= args.interval <= 5:
        parser.error("Use a poll interval between 0.1 and 5 seconds")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    venue = RITVenue(args.rit_port, args.enable_orders) if args.rit else SimVenue(speed=1.5, start_tick=15)
    desk = Desk(venue, "rit" if args.rit else "rehearsal", args.state_dir, args.record)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(desk, args.port)) as server:
        stop = Event()
        worker = Thread(target=poll_loop, args=(desk, stop, args.interval), daemon=True)
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
            if desk.legs or (desk.snapshot and any(desk.snapshot["positions"].values())):
                print("The desk stopped with inventory or a working order. Check RIT directly.")


if __name__ == "__main__":
    main()
