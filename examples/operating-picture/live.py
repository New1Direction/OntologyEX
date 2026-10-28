#!/usr/bin/env python3
"""Live feeds demo: real local services, real probes, real OTLP traces. Loopback only.

Starts small HTTP services (api, ledger, payments, web) with /healthz endpoints,
probes them through feeds.py, and, when the OpenTelemetry SDK and OTLP/HTTP exporter
are installed, sends server spans from `api` and `payments` to the feed's OTLP
receiver with the standard exporter. The control room then shows health that came
from those feeds, not from a prepared file.

    python -m pip install 'opentelemetry-sdk==1.44.0' 'opentelemetry-exporter-otlp-proto-http==1.44.0'
    python examples/operating-picture/live.py
    # open http://127.0.0.1:8765/business

The services are toys. `--fail payments` (the default) makes its health check
answer 503 and 30% of its requests fail, so you can watch it propagate.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import random
import socket
import sys
from pathlib import Path
from threading import Event, Thread
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction" / "scripts"))
import control_room
import feeds

spec = importlib.util.spec_from_file_location("ontologyex_live_demo", HERE / "demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

SERVICES = ("api", "ledger", "payments", "web")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def toy_service(name: str, failing: bool) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            code = 503 if failing and self.path == "/healthz" else 200
            body = json.dumps({"service": name, "healthy": code == 200}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def traffic(endpoint: str, failing: set[str], stop: Event) -> bool:
    """Real spans through the OpenTelemetry SDK's OTLP/HTTP exporter, if installed."""
    if importlib.util.find_spec("opentelemetry.exporter.otlp.proto.http") is None:
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import SpanKind, Status, StatusCode

    tracers = {}
    for name in ("api", "payments"):
        provider = TracerProvider(resource=Resource.create({"service.name": name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint),
                                                       schedule_delay_millis=1000))
        tracers[name] = (provider, provider.get_tracer("ontologyex-live-demo"))

    def loop():
        rng = random.Random(7)
        while not stop.is_set():
            for name, (_, tracer) in tracers.items():
                with tracer.start_as_current_span("POST /refund", kind=SpanKind.SERVER) as span:
                    time.sleep(rng.uniform(0.002, 0.03 if name == "api" else 0.12))
                    if name in failing and rng.random() < 0.3:
                        span.set_status(Status(StatusCode.ERROR))
            stop.wait(0.05)
        for provider, _ in tracers.values():
            provider.shutdown()

    Thread(target=loop, name="ontologyex-demo-traffic", daemon=True).start()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "build/operating-picture-live")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--fail", action="append", choices=SERVICES, default=None)
    args = parser.parse_args()
    failing = set(args.fail or ["payments"])
    built = demo.build(args.out)
    services = {name: toy_service(name, name in failing) for name in SERVICES}
    otlp_port = free_port()
    config = {
        "schema_version": 1, "kind": feeds.FEED_KIND, "output": "live-observations.jsonl",
        "allowed_hosts": ["127.0.0.1"],
        "probes": [{"component": demo.SERVICE + name,
                    "url": f"http://127.0.0.1:{server.server_address[1]}/healthz",
                    "interval_seconds": 5, "timeout_seconds": 2}
                   for name, server in sorted(services.items())],
        "otlp": {"port": otlp_port, "window_seconds": 30, "emit_seconds": 5, "min_requests": 5,
                 "services": {"api": demo.SERVICE + "api", "payments": demo.SERVICE + "payments"},
                 "warning": {"error_rate_pct": 2, "p95_latency_ms": 100},
                 "error": {"error_rate_pct": 10}},
    }
    config_path = args.out / "feeds.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    runner = feeds.FeedRunner(feeds.load_config(config_path))
    Thread(target=runner.run, name="ontologyex-feeds", daemon=True).start()
    stop = Event()
    sending = traffic(f"http://127.0.0.1:{otlp_port}/v1/traces", failing, stop)
    print(json.dumps({"status": "LIVE_DEMO", "feeds_config": str(config_path),
                      "observations": str(feeds.output_path(runner.config)),
                      "otlp_traces": f"http://127.0.0.1:{otlp_port}/v1/traces",
                      "otel_traffic": "SENDING" if sending else "NOT_INSTALLED_PROBES_ONLY",
                      "failing": sorted(failing)}, indent=2), flush=True)
    print(f"Open http://127.0.0.1:{args.port}/business", flush=True)
    time.sleep(1)
    try:
        control_room.serve(Path(built["repo"]), feeds.output_path(runner.config), None, args.port,
                           skills=[Path(built["skill"])], links=Path(built["links"]),
                           events=Path(built["events"]))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        runner.stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
