"""Calls seen in traces become dependency suggestions; only a person's decision changes the map.

Covers the feeds side (pairing client and server spans, the calls file across
restarts) and the decision side (suggestions, confirm and reject, CALLS edges that
carry possible impact, the opt-in HTTP route, and the CLI).
"""
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))
import control_room as room
import dependency_suggest as ds
import discover_repo
import feeds
from policy_check import DomainError
from test_feeds import proto_request

HAVE_OTEL = (importlib.util.find_spec("opentelemetry") is not None and
             importlib.util.find_spec("opentelemetry.exporter.otlp.proto.http") is not None)
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
CHECKOUT = "discovery:service:compose.yml:checkout"
PAYMENTS = "discovery:service:compose.yml:payments"
DB = "discovery:service:compose.yml:db"
SERVICES = {"checkout": CHECKOUT, "checkout-v2": CHECKOUT, "payments": PAYMENTS, "db": DB,
            "retired": "discovery:service:compose.yml:retired"}
TRACE = "0102030405060708090a0b0c0d0e0f10"
COMPOSE = """\
services:
  checkout:
    image: shop/checkout
    depends_on: [db]
  payments:
    image: shop/payments
  db:
    image: postgres:16
"""


def span(service, kind, span_id="", parent="", code=0, trace=TRACE):
    return (service, kind, code, 1.0, trace, span_id, parent)


def window(**otlp):
    return feeds.SpanWindow({"services": SERVICES, **otlp})


def calls_row(caller_service, callee_service, calls=10, errors=0, first="2026-10-28T08:00:00Z",
              last="2026-10-28T11:00:00Z"):
    return {"caller_service": caller_service, "callee_service": callee_service,
            "caller": SERVICES.get(caller_service), "callee": SERVICES.get(callee_service),
            "calls": calls, "errors": errors, "first_seen": first, "last_seen": last}


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "compose.yml").write_text(COMPOSE)
    return repo


class Pairing(unittest.TestCase):
    def test_client_then_server_and_server_then_client(self):
        w = window()
        w.ingest([span("checkout", 3, span_id="00000000000000a1")], 0.0)
        w.ingest([span("payments", 2, span_id="00000000000000b1", parent="00000000000000a1", code=2)], 1.0)
        w.ingest([span("payments", 2, span_id="00000000000000b2", parent="00000000000000a2")], 2.0)
        w.ingest([span("checkout", 3, span_id="00000000000000a2")], 3.0)
        self.assertEqual(w.call_counts(), [("checkout", "payments", 2, 1)])
        self.assertEqual((len(w.callers), len(w.callees)), (0, 0))

    def test_producer_and_consumer_pair_like_client_and_server(self):
        w = window()
        w.ingest([span("checkout", 4, span_id="00000000000000a1"),
                  span("db", 5, parent="00000000000000a1", span_id="00000000000000c1")], 0.0)
        self.assertEqual(w.call_counts(), [("checkout", "db", 1, 0)])

    def test_unmatched_halves_wait_one_window_only(self):
        w = window(window_seconds=10)
        w.ingest([span("checkout", 3, span_id="00000000000000a1")], 0.0)
        w.ingest([span("payments", 2, parent="00000000000000a1")], 10.5)  # the client half expired
        self.assertEqual(w.call_counts(), [])
        self.assertEqual((len(w.callers), len(w.callees)), (0, 1))

    def test_what_is_not_a_call_between_services(self):
        w = window()
        w.ingest([
            span("checkout", 3, span_id="00000000000000a1"), span("checkout", 2, parent="00000000000000a1"),
            span("", 3, span_id="00000000000000a2"), span("payments", 2, parent="00000000000000a2"),
            span("bad name", 3, span_id="00000000000000a3"), span("payments", 2, parent="00000000000000a3"),
            span("checkout", 1, span_id="00000000000000a4"), span("payments", 2, parent="00000000000000a4"),
            span("checkout", 3, span_id="00000000000000a5", trace=""), span("payments", 2, parent="00000000000000a5"),
            span("checkout", 3, span_id="00000000000000a6"),
            span("payments", 2, parent="00000000000000a6", trace="ffffffffffffffffffffffffffffffff"),
        ], 0.0)
        self.assertEqual(w.call_counts(), [])
        # Unmapped services still pair: the calls file names them so a person can map them.
        w.ingest([span("ghost", 3, span_id="00000000000000a7"), span("payments", 2, parent="00000000000000a7")], 1.0)
        self.assertEqual(w.call_counts(), [("ghost", "payments", 1, 0)])

    def test_bounds(self):
        with mock.patch.object(feeds, "MAX_PENDING_CALLS", 2), mock.patch.object(feeds, "MAX_CALL_PAIRS", 1):
            w = window()
            w.ingest([span("checkout", 3, span_id=f"{i:016x}") for i in range(1, 5)], 0.0)
            self.assertEqual((len(w.callers), w.calls_dropped), (2, 2))
            w.ingest([span("payments", 2, parent=f"{1:016x}"), span("db", 2, parent=f"{2:016x}")], 1.0)
            self.assertEqual((w.call_counts(), w.calls_dropped), ([("checkout", "payments", 1, 0)], 3))

    def test_ids_decode_from_protobuf_and_json(self):
        trace, client = bytes(range(1, 17)), bytes(range(1, 9))
        body = proto_request("payments", [(2, 0, 1.0, trace, bytes(range(9, 17)), client), (2, 0, 1.0, bytes(16), client),
                                          (2, 0, 1.0, trace[:15], client)])
        _, spans = feeds.decode_request(body, "application/x-protobuf", "")
        self.assertEqual([s[4:] for s in spans], [(TRACE, "090a0b0c0d0e0f10", "0102030405060708"),
                                                  ("", "0102030405060708", ""), ("", "0102030405060708", "")])
        js = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [
            {"traceId": TRACE.upper(), "spanId": "0102030405060708", "parentSpanId": "0" * 16},
            {"traceId": TRACE[:-1], "spanId": "xx" * 8}]}]}]}).encode()
        _, spans = feeds.decode_request(js, "application/json", "")
        self.assertEqual([s[4:] for s in spans], [(TRACE, "0102030405060708", ""), ("", "", "")])
        bad = b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": 5}]}]}]}'
        self.assertEqual(feeds.decode_request(bad, "application/json", ""), (400, []))

    @unittest.skipUnless(HAVE_OTEL, "opentelemetry SDK and OTLP/HTTP exporter not installed")
    def test_real_sdk_calls_between_two_services(self):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.propagate import extract, inject
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.trace import SpanKind, Status, StatusCode

        w = window()
        server = ThreadingHTTPServer(("127.0.0.1", 0), feeds.otlp_handler(w, ""))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/traces"

        def provider(name):
            p = TracerProvider(resource=Resource.create({"service.name": name}))
            p.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=url)))
            return p

        checkout, payments = provider("checkout"), provider("payments")
        for i in range(4):
            with checkout.get_tracer("t").start_as_current_span("POST /pay", kind=SpanKind.CLIENT):
                headers = {}
                inject(headers)  # W3C traceparent, as an HTTP client instrumentation sends it
                with payments.get_tracer("t").start_as_current_span(
                        "pay", kind=SpanKind.SERVER, context=extract(headers)) as handled:
                    if i == 0:
                        handled.set_status(Status(StatusCode.ERROR))
        checkout.shutdown()
        payments.shutdown()
        self.assertEqual(w.call_counts(), [("checkout", "payments", 4, 1)])


class CallsFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"schema_version": 1, "kind": feeds.FEED_KIND, "output": str(Path(self.tmp.name) / "obs.jsonl"),
                       "otlp": {"services": SERVICES}}

    def test_merge_counts_only_what_is_new(self):
        totals, seen = {}, {}
        feeds.merge_calls(totals, seen, [("checkout", "payments", 3, 1)], NOW)
        feeds.merge_calls(totals, seen, [("checkout", "payments", 3, 1)], NOW.replace(hour=13))
        feeds.merge_calls(totals, seen, [("checkout", "payments", 5, 1)], NOW.replace(hour=14))
        self.assertEqual(totals, {("checkout", "payments"): {"calls": 5, "errors": 1, "first_seen": "2026-10-28T12:00:00Z",
                                                             "last_seen": "2026-10-28T14:00:00Z"}})

    def test_totals_survive_a_restart_and_a_bad_file_is_refused(self):
        runner = feeds.FeedRunner(self.config)
        runner.window.ingest([span("checkout", 3, span_id="00000000000000a1"),
                              span("payments", 2, parent="00000000000000a1")], 0.0)
        runner.emit_otlp()
        path = Path(self.tmp.name) / "obs.calls.json"
        rows = feeds.read_calls(path)
        self.assertEqual([(r["caller"], r["callee"], r["calls"]) for r in rows], [(CHECKOUT, PAYMENTS, 1)])
        again = feeds.FeedRunner(self.config)
        again.window.ingest([span("checkout", 3, span_id="00000000000000a2"),
                             span("payments", 2, parent="00000000000000a2")], 0.0)
        again.emit_otlp()
        self.assertEqual(feeds.read_calls(path)[0]["calls"], 2)
        path.write_text("{}")
        with self.assertRaises(feeds.FeedError):
            feeds.FeedRunner(self.config)
        self.assertEqual(path.read_text(), "{}")

    def test_read_calls_is_strict(self):
        path = Path(self.tmp.name) / "c.json"
        good = feeds.calls_document({("checkout", "payments"): {"calls": 2, "errors": 0, "first_seen": "2026-10-28T08:00:00Z",
                                                                "last_seen": "2026-10-28T09:00:00Z"}}, SERVICES, NOW)
        path.write_text(json.dumps(good))
        self.assertEqual(len(feeds.read_calls(path)), 1)
        for change in ({"errors": 3}, {"calls": 0}, {"calls": True}, {"first_seen": "2026-10-29T00:00:00Z"},
                       {"caller_service": "payments"}, {"caller": "nope"}, {"last_seen": "2026-10-28T09:00:00"}):
            row = {**good["calls"][0], **change}
            path.write_text(json.dumps({**good, "calls": [row]}))
            with self.assertRaises(feeds.FeedError, msg=change):
                feeds.read_calls(path)
        path.write_text(json.dumps(good)[:-1] + ', "kind": "x"}')
        with self.assertRaises(feeds.FeedError):
            feeds.read_calls(path)


class Suggestions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(Path(self.tmp.name))
        self.state = room.build_state(discover_repo.discover(self.repo), [], {}, NOW)
        self.calls = [calls_row("checkout", "payments", 25, 2), calls_row("checkout-v2", "payments", 5, first="2026-10-27T00:00:00Z"),
                      calls_row("checkout", "db", 40), calls_row("ghost", "payments", 3),
                      calls_row("retired", "payments", 3), calls_row("payments", "db", 2)]

    def test_suggests_only_undeclared_calls_with_their_evidence(self):
        result = ds.suggest(self.state, self.calls, [], [])
        self.assertEqual(result["execution_authorized"], False)
        self.assertEqual([(s["source"], s["target"], s["strength"]) for s in result["suggestions"]],
                         [(CHECKOUT, PAYMENTS, "STRONG"), (PAYMENTS, DB, "WEAK")])
        first = result["suggestions"][0]
        self.assertEqual(first["evidence"], {"caller_services": ["checkout -> payments", "checkout-v2 -> payments"],
                                             "calls": 30, "errors": 2, "first_seen": "2026-10-27T00:00:00Z",
                                             "last_seen": "2026-10-28T11:00:00Z"})
        self.assertIn("called payments 30 times", first["signals"][0]["detail"])
        self.assertEqual([(c["source"], c["target"]) for c in result["corroborated"]], [(CHECKOUT, DB)])
        self.assertEqual({g["source"]: g["reason"] for g in result["gaps"]},
                         {"ghost -> payments": "SERVICE_NOT_MAPPED_TO_A_COMPONENT",
                          "retired -> payments": "COMPONENT_NOT_IN_SYSTEM_MAP"})
        self.assertEqual(ds.suggest(self.state, None, [], [])["status"], "NO_OBSERVED_CALLS")

    def test_confirm_adds_a_calls_edge_that_carries_impact(self):
        path = Path(self.tmp.name) / "deps.json"
        suggestion = ds.suggest(self.state, self.calls, [], [])["suggestions"][0]
        result = ds.decide(path, suggestion, "confirm", " ana ", "seen in staging")
        self.assertEqual((result["status"], result["dependency"]["declared_by"]), ("CONFIRMED", "ana"))
        self.assertEqual(ds.decide(path, suggestion, "reject", "bo")["status"], "UNCHANGED")
        confirmed, rejected = ds.read_dependency_document(path)
        self.assertEqual((len(confirmed), rejected), (1, []))
        topology = ds.with_confirmed(discover_repo.discover(self.repo), confirmed, path.name)
        edge = next(e for e in topology["edges"] if e["relation"] == "CALLS")
        self.assertEqual((edge["source"], edge["target"]), (CHECKOUT, PAYMENTS))
        self.assertIn(edge["evidence_id"], {e["id"] for e in topology["source_evidence"]})
        failing = [{"schema_version": 1, "component_id": PAYMENTS, "observed_at": "2026-10-28T11:59:00Z",
                    "status": "error", "source": "health_probe", "expires_after_seconds": 120, "metrics": {}}]
        state = room.build_state(topology, failing, {}, NOW)
        nodes = {n["id"]: n for n in state["nodes"]}
        self.assertEqual(nodes[CHECKOUT]["potentially_impacted_by"], [PAYMENTS])
        before = room.build_state(discover_repo.discover(self.repo), failing, {}, NOW)
        self.assertEqual({n["id"]: n for n in before["nodes"]}[CHECKOUT]["potentially_impacted_by"], [])
        self.assertEqual(ds.suggest(state, self.calls, confirmed, [])["suggestions"][0]["target"], DB)

    def test_reject_hides_and_a_missing_component_is_a_gap(self):
        path = Path(self.tmp.name) / "deps.json"
        suggestions = ds.suggest(self.state, self.calls, [], [])["suggestions"]
        ds.decide(path, suggestions[1], "reject", "ana")
        confirmed, rejected = ds.read_dependency_document(path)
        after = ds.suggest(self.state, self.calls, confirmed, rejected)
        self.assertEqual((len(after["suggestions"]), after["rejected_not_shown"]), (1, 1))
        gone = [{**suggestions[0], "source": "discovery:service:compose.yml:gone"}]
        topology = ds.with_confirmed(discover_repo.discover(self.repo),
                                     [{"source": gone[0]["source"], "target": PAYMENTS, "declared_by": "a", "note": "",
                                       "evidence": suggestions[0]["evidence"]}], "deps.json")
        self.assertFalse(any(e["relation"] == "CALLS" for e in topology["edges"]))
        self.assertEqual(topology["gaps"][-1]["reason"], "CONFIRMED_DEPENDENCY_COMPONENT_MISSING")

    def test_decisions_refuse_what_they_cannot_stand_behind(self):
        path = Path(self.tmp.name) / "deps.json"
        suggestion = ds.suggest(self.state, self.calls, [], [])["suggestions"][0]
        for args in (("maybe", "ana"), ("confirm", " "), ("confirm", "x" * 121)):
            with self.assertRaises(DomainError):
                ds.decide(path, suggestion, *args)
        with self.assertRaises(DomainError):
            ds.decide(path, {**suggestion, "status": "CONFIRMED"}, "confirm", "ana")
        self.assertFalse(path.exists())
        row = {"source": CHECKOUT, "target": PAYMENTS, "declared_by": "a", "note": "", "evidence": suggestion["evidence"]}
        for document in ({"schema_version": 1, "kind": ds.DEPENDENCY_KIND, "confirmed": [row], "rejected": [row]},
                         {"schema_version": 1, "kind": ds.DEPENDENCY_KIND, "confirmed": [{**row, "target": CHECKOUT}]},
                         {"schema_version": 1, "kind": ds.DEPENDENCY_KIND, "confirmed": [],  "x": 1},
                         {"schema_version": 1, "kind": ds.DEPENDENCY_KIND,
                          "confirmed": [{**row, "evidence": {**row["evidence"], "errors": 99}}]}):
            path.write_text(json.dumps(document))
            with self.assertRaises(DomainError):
                ds.read_dependency_document(path)


class Surfaces(unittest.TestCase):
    """The opt-in HTTP route on the business page, and the CLI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = make_repo(root)
        self.observations = root / "obs.jsonl"
        self.observations.touch()
        self.calls = root / "obs.calls.json"
        self.calls.write_text(json.dumps(feeds.calls_document(
            {("checkout", "payments"): {"calls": 25, "errors": 0, "first_seen": "2026-10-28T08:00:00Z",
                                        "last_seen": "2026-10-28T09:00:00Z"}}, SERVICES, NOW)))
        self.deps = root / "deps.json"

    def serve(self, *extra):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, str(SCRIPTS / "control_room.py"), "--repo", str(self.repo),
                                    "--observations", str(self.observations), "--port", str(port), *extra],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)))
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                with urllib.request.urlopen(base + "/api/business", timeout=2) as resp:
                    return base, json.load(resp)
            except OSError:
                time.sleep(0.1)
        self.fail("control room did not start")

    def post(self, base, body, token):
        request = urllib.request.Request(base + "/api/dependencies/decision", method="POST", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Origin": base,
                                                  "X-OntologyEX-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_read_only_by_default(self):
        base, data = self.serve("--dependencies", str(self.deps))
        self.assertEqual(data["dependency_decisions"], "READ_ONLY_USE_CLI")
        row = data["dependency_suggestions"]["suggestions"][0]
        self.assertIn("dependency_suggest.py confirm", row["confirm_command"])
        self.assertEqual(self.post(base, {"id": row["id"], "decision": "confirm", "by": "a", "note": ""}, "")[1]["status"],
                         "LINK_DECISIONS_DISABLED")

    def test_opt_in_route_confirms_and_the_map_shows_the_edge(self):
        base, data = self.serve("--dependencies", str(self.deps), "--allow-link-decisions")
        self.assertEqual(data["dependency_decisions"], "ENABLED_LOCAL_TOKEN")
        with urllib.request.urlopen(base + "/business", timeout=2) as resp:
            token = resp.read().decode().split('name="ontologyex-write-token" content="')[1].split('"')[0]
        row = data["dependency_suggestions"]["suggestions"][0]
        body = {"id": row["id"], "decision": "confirm", "by": "ana", "note": ""}
        self.assertEqual(self.post(base, body, "wrong")[0], 403)
        self.assertEqual(self.post(base, {**body, "id": "nope"}, token)[0], 400)
        status, result = self.post(base, body, token)
        self.assertEqual((status, result["status"], result["authenticated"]), (200, "CONFIRMED", False))
        self.assertEqual(self.post(base, body, token)[0], 400)  # decided: no longer a current suggestion
        confirmed, _ = ds.read_dependency_document(self.deps)
        self.assertEqual([(r["source"], r["target"], r["declared_by"]) for r in confirmed], [(CHECKOUT, PAYMENTS, "ana")])
        with urllib.request.urlopen(base + "/api/view?lens=all&q=&offset=0", timeout=5) as resp:
            edges = json.load(resp)["edges"]
        self.assertIn(("CALLS", CHECKOUT, PAYMENTS), {(e["relation"], e["source"], e["target"]) for e in edges})

    def test_each_route_needs_its_own_file(self):
        base, _ = self.serve("--dependencies", str(self.deps), "--allow-link-decisions")
        with urllib.request.urlopen(base + "/business", timeout=2) as resp:
            token = resp.read().decode().split('name="ontologyex-write-token" content="')[1].split('"')[0]
        request = urllib.request.Request(base + "/api/links/decision", method="POST", data=json.dumps(
            {"id": "x", "decision": "confirm", "by": "a", "note": ""}).encode(), headers={
                "Content-Type": "application/json", "Origin": base, "X-OntologyEX-Token": token})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual((caught.exception.code, json.load(caught.exception)["status"]), (403, "LINK_DECISIONS_DISABLED"))

    def test_link_decisions_alone_still_need_links_and_a_skill(self):
        run = subprocess.run([sys.executable, str(SCRIPTS / "control_room.py"), "--repo", str(self.repo),
                              "--port", "0", "--allow-link-decisions"], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 1)
        self.assertIn("--dependencies", json.loads(run.stdout)["reason"])

    def test_cli_suggest_and_confirm(self):
        command = [sys.executable, str(SCRIPTS / "dependency_suggest.py")]
        common = ["--repo", str(self.repo), "--calls", str(self.calls), "--dependencies", str(self.deps)]
        run = subprocess.run([*command, "suggest", *common], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout)
        suggestion = json.loads(run.stdout)["suggestions"][0]
        run = subprocess.run([*command, "confirm", *common, "--id", suggestion["id"], "--by", "ana"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(run.stdout)["status"], "CONFIRMED")
        run = subprocess.run([*command, "suggest", *common], capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(run.stdout)["suggestions"], [])
        run = subprocess.run([*command, "confirm", *common, "--id", "nope", "--by", "ana"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual((run.returncode, json.loads(run.stdout)["status"]), (1, "ERROR"))


if __name__ == "__main__":
    unittest.main()
