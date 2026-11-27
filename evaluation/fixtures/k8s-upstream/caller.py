"""Small dev caller whose open circuit persists in its process after upstream returns."""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Lock
from time import monotonic
from urllib.request import ProxyHandler, build_opener


ORDER_CHECK = "/api/orders/check?item=a"
STOCK_PATH = "/api/stock/a"
ORIGIN = "http://lp-eval-inventory-route.dev.svc.cluster.local:8080"
HTTP = build_opener(ProxyHandler({}))


class Caller:
    def __init__(self, stock):
        self.stock = stock
        self.lock = Lock()
        self.open = False

    def request(self):
        with self.lock:
            if self.open:
                return 503, {"error": "inventory check unavailable"}
            try:
                with HTTP.open(self.stock + STOCK_PATH, timeout=1.5) as response:
                    data = json.load(response)
                if (data.get("item") != "a" or type(data.get("quantity")) is not int
                        or type(data.get("available")) is not bool):
                    raise ValueError("invalid inventory contract")
            except (OSError, ValueError) as exc:
                self.open = True
                print(f"ERROR inventory check unavailable item=a cause={type(exc).__name__}", flush=True)
                return 503, {"error": "inventory check unavailable"}
            return 200, {"item": "a", "orderable": data["available"], "inventory": data}


def serve(port):
    caller = Caller(ORIGIN)
    counts = Counter()
    durations = Counter()
    metrics = Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            began = monotonic()
            if self.path == "/actuator/health/readiness":
                self.send(200, b'{"status":"UP"}')
            elif self.path == "/actuator/prometheus":
                with metrics:
                    content = "".join(
                        f'http_server_requests_seconds_count{{method="GET",uri="/api/orders/check",status="{status}"}} {n}\n'
                        f'http_server_requests_seconds_sum{{method="GET",uri="/api/orders/check",status="{status}"}} {durations[status]}\n'
                        for status, n in sorted(counts.items())
                    )
                self.send(200, content.encode(), "text/plain; version=0.0.4")
            elif self.path == ORDER_CHECK:
                status, body = caller.request()
                with metrics:
                    counts[status] += 1
                    durations[status] += monotonic() - began
                self.send(status, json.dumps(body).encode())
            else:
                self.send(404, b'{"error":"not found"}')

        def send(self, status, payload, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    serve(int(os.environ.get("SERVICE_PORT", "8080")))
