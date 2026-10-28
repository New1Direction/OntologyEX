"""Live feeds: real local HTTP probes, a Prometheus-shaped API, and OTLP traces.

The OTLP end-to-end test uses the real OpenTelemetry SDK and exporter when they are
installed, and is skipped otherwise. Everything runs on loopback.
"""
import gzip
import importlib.util
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import business_view as bv
import control_room as room
import discover_repo
import feeds

PAYMENTS = "discovery:service:compose.yml:payments"
API = "discovery:service:compose.yml:api"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
HAVE_OTEL = (importlib.util.find_spec("opentelemetry") is not None and
             importlib.util.find_spec("opentelemetry.exporter.otlp.proto.http") is not None)


class Fixture(BaseHTTPRequestHandler):
    """Health endpoints and a Prometheus-shaped /api/v1/query."""
    seen_auth = []
    series = {"up_api": [["1"]], "err_api": [["7.5"]], "lat_api": [["120"]],
              "two": [["1"], ["0"]], "nan": [["NaN"]]}

    def do_GET(self):
        route = urllib.parse.urlsplit(self.path)
        if route.path == "/healthz":
            return self.answer(200, b"ok")
        if route.path == "/fail":
            return self.answer(503, b"down")
        if route.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/healthz")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if route.path == "/slow":
            time.sleep(1.5)
            return self.answer(200, b"late")
        if route.path == "/api/v1/query":
            Fixture.seen_auth.append(self.headers.get("Authorization"))
            query = urllib.parse.parse_qs(route.query)["query"][0]
            rows = self.series.get(query)
            if rows is None:
                return self.answer(400, b'{"status":"error"}')
            result = [{"metric": {"i": str(i)}, "value": [1761652800, r[0]]} for i, r in enumerate(rows)]
            body = {"status": "success", "data": {"resultType": "vector", "result": result}}
            return self.answer(200, json.dumps(body).encode())
        return self.answer(404, b"")

    def answer(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_config(root: Path, **sections) -> Path:
    path = root / "feeds.json"
    path.write_text(json.dumps({"schema_version": 1, "kind": feeds.FEED_KIND,
                                "output": "observations.jsonl",
                                "allowed_hosts": ["127.0.0.1"], **sections}))
    return path


# Minimal protobuf encoder for ExportTraceServiceRequest, independent of the decoder.
def _varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(number, wire, payload):
    key = _varint((number << 3) | wire)
    if wire == 2:
        return key + _varint(len(payload)) + payload
    return key + payload


def proto_request(service, spans, *more):
    """spans: (kind, status code, duration ms[, trace ID, span ID, parent span ID]), IDs as
    bytes (b"" leaves one out). `more`: further (service, spans) resources in the request."""
    request = b""
    for name, rows in ((service, spans), *more):
        value = _field(1, 2, name.encode())                         # AnyValue.string_value
        kv = _field(1, 2, b"service.name") + _field(2, 2, value)    # KeyValue
        resource = _field(1, 2, kv)                                 # Resource.attributes
        encoded_spans = b""
        for kind, code, ms, *ids in rows:
            start = 1_761_652_800_000_000_000
            span = b"".join(_field(number, 2, raw) for number, raw in zip((1, 2, 4), ids) if raw)
            span += (_field(5, 2, b"GET /refund") + _field(6, 0, _varint(kind)) +
                     _field(7, 1, struct.pack("<Q", start)) +
                     _field(8, 1, struct.pack("<Q", start + int(ms * 1e6))) +
                     _field(15, 2, _field(3, 0, _varint(code))))
            encoded_spans += _field(2, 2, span)
        scope = _field(2, 2, encoded_spans)                         # ResourceSpans.scope_spans
        request += _field(1, 2, _field(1, 2, resource) + scope)     # request.resource_spans
    return request


class FeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    # -- configuration
    def test_config_refuses_unsafe_targets(self):
        cases = [
            {"probes": [{"component": API, "url": "http://user:pw@127.0.0.1/healthz"}]},
            {"probes": [{"component": API, "url": "http://example.com/healthz"}]},
            {"probes": [{"component": API, "url": "file:///etc/passwd"}]},
            {"probes": [{"component": "api", "url": self.base + "/healthz"}]},
            {"probes": [{"component": API, "url": self.base + "/healthz", "extra": 1}]},
            {"otlp": {"bind": "0.0.0.0", "services": {"payments": PAYMENTS}}},
            {"otlp": {"services": {"bad name!": PAYMENTS}}},
            {"prometheus": {"url": self.base, "targets": [{"component": API}]}},
            {},
        ]
        for sections in cases:
            with self.subTest(sections=sections), self.assertRaises(feeds.FeedError):
                feeds.load_config(write_config(self.root, **sections))

    def test_url_rules_are_strict_and_portable(self):
        hosts = {"127.0.0.1", "api.internal", "::1", "0x7f.1"}
        for url in ["http://api.internal/healthz", "HTTPS://API.internal:8443/x?y=1", "http://[::1]:9090/",
                    "http://127.0.0.1:0080/"]:
            with self.subTest(url=url):
                self.assertEqual(feeds._url(url, hosts, "t"), url)
        for url in ["http://api.internal/#x", "http://api.internal/ x", "http://api.internal/é",
                    "http://api.internal:8a/", "http://api.internal:70000/", "http://[127.0.0.1]/",
                    "http://[fe80::1%25eth0]/", "http://0x7f.1/", "http://127.000.0.1/", "http://api.internal/a[b]",
                    "http:api.internal/", "http://[::1"]:
            with self.subTest(url=url), self.assertRaises(feeds.FeedError):
                feeds._url(url, hosts, "t")

    def test_otlp_bind_must_be_a_plain_address(self):
        for bind in ["127.0.0.1%1", 2130706433, "localhost", "::ffff:127.0.0.1"]:
            with self.subTest(bind=bind), self.assertRaises(feeds.FeedError):
                feeds.load_config(write_config(self.root, otlp={"bind": bind, "services": {"payments": PAYMENTS}}))
        config = feeds.load_config(write_config(self.root, otlp={"bind": "::1", "services": {"payments": PAYMENTS}}))
        self.assertEqual(config["otlp"]["bind"], "::1")

    def test_config_json_is_strict(self):
        path = self.root / "feeds.json"
        for raw in [b"\xff\xfe{}", '{"schema_version": 1, "kind": "ONTOLOGYEX_FEEDS", "output": "o.jsonl", '
                    '"probes": [], "x": NaN}'.encode(), b'{"schema_version": 1, "schema_version": 1}']:
            path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(feeds.FeedError):
                feeds.load_config(path)

    def test_decode_request_is_strict(self):
        proto = proto_request("payments", [(2, 0, 1)])
        self.assertEqual(feeds.decode_request(proto, "application/x-protobuf", "")[0], 201)
        self.assertEqual(feeds.decode_request(gzip.compress(proto)[:-4], "application/x-protobuf", "gzip")[0], 400)
        self.assertEqual(feeds.decode_request(gzip.compress(proto) + b"x", "application/x-protobuf", "gzip")[0], 201)
        for body in [b'{"resourceSpans": [5]}', b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"kind": 2.0}]}]}]}',
                     b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"startTimeUnixNano": "-1"}]}]}]}',
                     b'{"resourceSpans": [], "x": 1e400}', b'{"resourceSpans": [], "x": "\\ud800"}',
                     b'{"resourceSpans": []' + b', "x": ' + b"[" * 64 + b"]" * 64 + b"}"]:
            with self.subTest(body=body[:60]):
                self.assertEqual(feeds.decode_request(body, "application/json", ""), (400, []))

    def test_prometheus_numbers_are_strings_in_prometheus_format(self):
        good = {"status": "success", "data": {"resultType": "scalar", "result": [1, "2.5"]}}
        self.assertEqual(feeds.prometheus_number(json.dumps(good).encode()), 2.5)
        for value in [2.5, " 1", "1_0", "NaN", "+Inf"]:
            body = {"status": "success", "data": {"resultType": "scalar", "result": [1, value]}}
            with self.subTest(value=value), self.assertRaises(feeds.FeedError):
                feeds.prometheus_number(json.dumps(body).encode())
        for raw in [b"[]", b"null", b'{"status": "success", "data": []}']:
            with self.subTest(raw=raw), self.assertRaises(feeds.FeedError):
                feeds.prometheus_number(raw)

    def test_non_loopback_otlp_is_allowed_with_a_token(self):
        config = feeds.load_config(write_config(self.root, otlp={
            "bind": "0.0.0.0", "token_env": "OTLP_TOKEN", "services": {"payments": PAYMENTS}}))
        self.assertEqual(config["otlp"]["bind"], "0.0.0.0")

    def test_classify_thresholds_are_inclusive(self):
        warn, err = {"error_rate_pct": 1}, {"error_rate_pct": 5}
        self.assertEqual(feeds.classify({"error_rate_pct": 0.5}, warn, err), "ok")
        self.assertEqual(feeds.classify({"error_rate_pct": 1}, warn, err), "warning")
        self.assertEqual(feeds.classify({"error_rate_pct": 5}, warn, err), "error")
        self.assertEqual(feeds.classify({}, warn, err, down=True), "error")

    # -- probes
    def test_probes(self):
        cases = {"/healthz": "ok", "/fail": "error", "/redirect": "error", "/slow": "error"}
        for path, expected in cases.items():
            with self.subTest(path=path):
                probe = {"component": API, "url": self.base + path, "timeout_seconds": 1}
                row, detail = feeds.run_probe(probe, NOW)
                self.assertEqual(row["status"], expected, detail)
                self.assertEqual(row["source"], "health_probe")
        row, _ = feeds.run_probe({"component": API, "url": self.base + "/healthz"}, NOW)
        self.assertIn("probe_latency_ms", row["metrics"])
        self.assertEqual(row["expires_after_seconds"], 90)
        closed = {"component": API, "url": f"http://127.0.0.1:{free_port()}/healthz", "timeout_seconds": 1}
        row, detail = feeds.run_probe(closed, NOW)
        self.assertEqual((row["status"], row["metrics"]), ("error", {}))
        self.assertTrue(detail)

    def test_probe_latency_warning(self):
        probe = {"component": API, "url": self.base + "/healthz", "warning": {"probe_latency_ms": 0}}
        self.assertEqual(feeds.run_probe(probe, NOW)[0]["status"], "warning")

    # -- prometheus
    def test_prometheus_target(self):
        os.environ["ONTOLOGYEX_TEST_PROM_TOKEN"] = "s3cret"
        self.addCleanup(os.environ.pop, "ONTOLOGYEX_TEST_PROM_TOKEN")
        prom = {"url": self.base, "token_env": "ONTOLOGYEX_TEST_PROM_TOKEN", "targets": []}
        target = {"component": API, "up": "up_api", "error_rate_pct": "err_api",
                  "p95_latency_ms": "lat_api", "warning": {"error_rate_pct": 1},
                  "error": {"error_rate_pct": 5}}
        Fixture.seen_auth.clear()
        row = feeds.run_prometheus(prom, target, NOW)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["source"], "prometheus")
        self.assertEqual(row["metrics"], {"error_rate_pct": 7.5, "p95_latency_ms": 120.0})
        self.assertEqual(set(Fixture.seen_auth), {"Bearer s3cret"})
        for query in ("two", "nan", "missing"):
            with self.subTest(query=query), self.assertRaises(feeds.FeedError):
                feeds.run_prometheus({"url": self.base, "targets": []},
                                     {"component": API, "up": query}, NOW)

    def test_runner_poll_once_writes_rows_the_control_room_accepts(self):
        config = feeds.load_config(write_config(self.root, probes=[
            {"component": API, "url": self.base + "/healthz"},
            {"component": PAYMENTS, "url": self.base + "/fail"}],
            prometheus={"url": self.base, "targets": [
                {"component": API, "up": "two"}]}))
        runner = feeds.FeedRunner(config)
        self.assertEqual(runner.poll_once(), 2)
        rows = room.read_observations(feeds.output_path(config))
        self.assertEqual({(r["component_id"], r["status"]) for r in rows},
                         {(API, "ok"), (PAYMENTS, "error")})
        status = json.loads(runner.status_path.read_text())
        self.assertFalse(status["feeds"]["prometheus " + API]["ok"])
        self.assertTrue(status["feeds"]["probe " + PAYMENTS]["ok"])  # the feed worked; the target is down
        self.assertIn("target error", status["feeds"]["probe " + PAYMENTS]["detail"])
        self.assertIn("exactly one series", status["feeds"]["prometheus " + API]["detail"])

    def test_store_keeps_newest_per_component(self):
        store = feeds.ObservationStore(self.root / "obs.jsonl", keep=3)
        for minute in range(6):
            moment = NOW.replace(minute=minute)
            store.add([feeds.observation(API, "ok", "health_probe", 30, {}, moment)])
        rows = room.read_observations(self.root / "obs.jsonl")
        self.assertEqual([r["observed_at"][14:16] for r in rows], ["03", "04", "05"])

    # -- OTLP
    def otlp_server(self, token="", **options):
        config = {"services": {"payments": PAYMENTS}, "min_requests": 3, "port": 0,
                  "warning": {"error_rate_pct": 1}, "error": {"error_rate_pct": 20}, **options}
        window = feeds.SpanWindow(config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), feeds.otlp_handler(window, token))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return window, f"http://127.0.0.1:{server.server_address[1]}/v1/traces"

    def post(self, url, body, mime, headers=None):
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": mime, **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_otlp_protobuf_json_and_gzip(self):
        window, url = self.otlp_server()
        spans = [(2, 0, 10), (2, 2, 30), (2, 0, 20), (3, 2, 5)]  # one CLIENT span ignored
        self.assertEqual(self.post(url, proto_request("payments", spans), "application/x-protobuf"), 200)
        payload = {"resourceSpans": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "payments"}}]},
            "scopeSpans": [{"spans": [{"kind": "SPAN_KIND_SERVER", "status": {"code": "STATUS_CODE_OK"},
                                       "startTimeUnixNano": "0", "endTimeUnixNano": "40000000"}]}]}]}
        self.assertEqual(self.post(url, json.dumps(payload).encode(), "application/json"), 200)
        compressed = gzip.compress(proto_request("checkout", [(2, 0, 1)]))
        self.assertEqual(self.post(url, compressed, "application/x-protobuf",
                                   {"Content-Encoding": "gzip"}), 200)
        rows = window.observations(time.monotonic(), NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["component_id"], PAYMENTS)
        self.assertEqual(rows[0]["metrics"], {"error_rate_pct": 25.0, "p95_latency_ms": 40.0})
        self.assertEqual(rows[0]["status"], "error")
        self.assertEqual(window.unmapped, {"checkout": 1})

    def test_otlp_refuses_bad_requests(self):
        window, url = self.otlp_server(token="t0ken")
        body = proto_request("payments", [(2, 0, 1)])
        self.assertEqual(self.post(url, body, "application/x-protobuf"), 401)
        auth = {"Authorization": "Bearer t0ken"}
        self.assertEqual(self.post(url, body, "text/plain", auth), 415)
        self.assertEqual(self.post(url, b"\xff\xff\xff", "application/x-protobuf", auth), 400)
        self.assertEqual(self.post(url.replace("/v1/traces", "/v1/logs"), body,
                                   "application/x-protobuf", auth), 404)
        self.assertEqual(self.post(url, body, "application/x-protobuf", auth), 200)
        self.assertEqual(window.accepted, 1)

    def test_otlp_token_compare_survives_non_ascii_headers(self):
        _, url = self.otlp_server(token="t0ken")
        body = proto_request("payments", [(2, 0, 1)])
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/x-protobuf", "Authorization": "Bearer t\xf6ken"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 401)

    def test_otlp_window_needs_traffic_and_expires(self):
        window = feeds.SpanWindow({"services": {"payments": PAYMENTS}, "min_requests": 2,
                                   "window_seconds": 10})
        window.ingest([("payments", 2, 0, 5.0, "", "", "")], arrived=100.0)
        self.assertEqual(window.observations(100.0, NOW), [])
        window.ingest([("payments", 2, 0, 5.0, "", "", "")], arrived=101.0)
        self.assertEqual(len(window.observations(101.0, NOW)), 1)
        self.assertEqual(window.observations(200.0, NOW), [])

    @unittest.skipUnless(HAVE_OTEL, "opentelemetry SDK and OTLP/HTTP exporter not installed")
    def test_real_opentelemetry_sdk_exports_to_the_receiver(self):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.trace import SpanKind, Status, StatusCode

        window, url = self.otlp_server(token="sdk-token")
        provider = TracerProvider(resource=Resource.create({"service.name": "payments"}))
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(
            endpoint=url, headers={"Authorization": "Bearer sdk-token"})))
        tracer = provider.get_tracer("ontologyex-test")
        for i in range(10):
            with tracer.start_as_current_span("POST /refund", kind=SpanKind.SERVER) as span:
                if i < 3:
                    span.set_status(Status(StatusCode.ERROR))
        with tracer.start_as_current_span("db query", kind=SpanKind.CLIENT):
            pass
        provider.force_flush()
        provider.shutdown()
        rows = window.observations(time.monotonic(), NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metrics"]["error_rate_pct"], 30.0)
        self.assertEqual(rows[0]["status"], "error")
        self.assertEqual(window.accepted, 10)

    def test_feed_samples_drive_the_business_view(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "compose.yml").write_text("services:\n  api:\n    depends_on: [payments]\n  payments: {}\n")
        config = feeds.load_config(write_config(self.root, probes=[
            {"component": PAYMENTS, "url": self.base + "/fail"},
            {"component": API, "url": self.base + "/healthz"}]))
        self.assertEqual(feeds.check_against(config, repo)["status"], "OK")
        feeds.FeedRunner(config).poll_once()
        state = room.build_state(discover_repo.discover(repo),
                                 room.read_observations(feeds.output_path(config)), {})
        nodes = {n["id"]: n for n in state["nodes"]}
        self.assertEqual(nodes[PAYMENTS]["status"], "error")
        self.assertEqual(nodes[PAYMENTS]["observation_source"], "health_probe")
        self.assertEqual(nodes[API]["status"], "ok")
        self.assertEqual(nodes[API]["potentially_impacted_by"], [PAYMENTS])
        health, reason = bv.rollup([{"label": "api", "status": "ok",
                                     "potentially_impacted_by": ["payments"]}])
        self.assertEqual(health, "POSSIBLY_IMPACTED")

    def test_check_reports_unknown_components(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "compose.yml").write_text("services:\n  api: {}\n")
        config = feeds.load_config(write_config(self.root, probes=[
            {"component": PAYMENTS, "url": self.base + "/healthz"}]))
        self.assertEqual(feeds.check_against(config, repo),
                         {"status": "UNKNOWN_COMPONENTS", "components": 1, "unknown": [PAYMENTS]})


if __name__ == "__main__":
    unittest.main()
