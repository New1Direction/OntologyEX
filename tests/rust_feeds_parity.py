#!/usr/bin/env python3
"""Differential test: Rust `ontologyex-feeds` vs Python `feeds.py`.

Same inputs, same answers, for every part of the feeds service:

1. configuration validation (and the effective configuration with defaults),
2. OTLP request decoding (protobuf and JSON, gzip, seeded byte mutations),
3. Prometheus response parsing,
4. the span window (seeded replays: error rate, p95, thresholds, expiry),
5. the observation store (merging with existing files, byte for byte),
6. `once` end to end against a local HTTP fixture (rows, masking timestamps),
7. the OTLP/HTTP receiver: status codes for the same requests, and the rows it emits.

    python tests/rust_feeds_parity.py BINARY [--seeds N]
"""
from __future__ import annotations

import argparse
import copy
import gzip
import json
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import feeds  # noqa: E402
from test_feeds import Fixture, proto_request  # noqa: E402

API = "discovery:service:compose.yml:api"
PAY = "discovery:service:compose.yml:payments"
BASE = {
    "schema_version": 1, "kind": "ONTOLOGYEX_FEEDS", "output": "observations.jsonl",
    "allowed_hosts": ["127.0.0.1", "api.internal", "::1", "prom.internal"],
    "keep_per_component": 6,
    "probes": [{"component": API, "url": "http://api.internal/healthz", "interval_seconds": 30,
                "timeout_seconds": 3, "expect_status": [200, 204], "warning": {"probe_latency_ms": 800}}],
    "prometheus": {"url": "http://prom.internal:9090", "token_env": "PROM_TOKEN", "interval_seconds": 30,
                   "targets": [{"component": PAY, "up": "up", "error_rate_pct": "err",
                                "warning": {"error_rate_pct": 1}, "error": {"error_rate_pct": 5.5}}]},
    "otlp": {"bind": "127.0.0.1", "port": 4318, "services": {"payments": PAY, "api": API},
             "window_seconds": 60, "emit_seconds": 15, "min_requests": 3,
             "warning": {"p95_latency_ms": 500}, "error": {"error_rate_pct": 10}},
}
URLS = [
    "http://api.internal/healthz", "https://api.internal:8443/x?y=1&z=%20", "HTTP://API.INTERNAL/", "http://api.internal",
    "http://api.internal:0/", "http://api.internal:65535/", "http://api.internal:65536/", "http://api.internal:0080/",
    "http://api.internal:/", "http://api.internal:8a/", "http://api.internal:80:90/", "http://[::1]:9090/",
    "http://[::1]/", "http://[::1", "http://::1]/", "http://x[::1]/", "http://[::1]x/", "http://[127.0.0.1]/",
    "http://[v1.x]/", "http://[fe80::1%25eth0]/", "http://127.0.0.1/", "http://127.000.0.1/", "http://127.1/",
    "http://0x7f.1/", "http://2130706433/", "http://user@api.internal/", "http://:pw@api.internal/",
    "http://api.internal/#frag", "http://api.internal/#", "ftp://api.internal/", "http:api.internal/",
    "http:/api.internal/", "//api.internal/", "api.internal/", "http://API.internal./", "http://api..internal/",
    "http://-api.internal/", "http://api_internal/", "http://api.internal/a[b]", "http://api.internal/?q=[1]",
    "http://api.internal/ space", "http://api.internal/\t", "http://api.internal/é", "http://api.internal/{x}",
    "http://api.internal/a|b", "http://api.internal/\"q\"", "http://other.internal/", "https://prom.internal",
    "http://api.internal/" + "a" * 2030, "http://api.internal/" + "a" * 2040, "", "h", "http://",
    "http://api.internal:99999999999999999999/", "http://api.internal/%zz", "http://api.internal/;a=b,c",
    "hTTp://api.internal/", "http+x://api.internal/", "http://[::ffff:127.0.0.1]/", "http://[0:0:0:0:0:0:0:1]/",
]
POOL = [None, True, False, 0, 1, -1, 2, 5, 30, 64, 65, 100, 101, 599, 600, 3600, 3601, 65535, 65536, 1.0, 1.5, 1e9,
        1.1e9, "", "x", "127.0.0.1", "::1", "0.0.0.0", "10.0.0.1", "::ffff:127.0.0.1", "127.0.0.1%1", [], [200], {},
        {"probe_latency_ms": 5}, {"bogus": 1}, {"error_rate_pct": 101}, 2 ** 70, -2 ** 70]


def python_effective(path: Path) -> dict:
    """What Rust's `validate` prints, computed from the Python reference."""
    c = feeds.load_config(path)
    th = lambda v: dict(v or {})  # noqa: E731
    prom = c.get("prometheus")
    otlp = c.get("otlp")
    return {
        "output": str(feeds.output_path(c)),
        "keep_per_component": c.get("keep_per_component", feeds.KEEP_PER_COMPONENT),
        "probes": [{"component": p["component"], "url": p["url"], "interval_seconds": p.get("interval_seconds", 30),
                    "timeout_seconds": p.get("timeout_seconds", 3), "expect_status": p.get("expect_status", [200]),
                    "warning": th(p.get("warning"))} for p in c.get("probes", [])],
        "prometheus": None if prom is None else {
            "url": prom["url"], "token_env": prom.get("token_env", ""), "interval_seconds": prom.get("interval_seconds", 30),
            "timeout_seconds": prom.get("timeout_seconds", 5),
            "targets": [{"component": t["component"], "up": t.get("up"), "error_rate_pct": t.get("error_rate_pct"),
                         "p95_latency_ms": t.get("p95_latency_ms"), "warning": th(t.get("warning")),
                         "error": th(t.get("error"))} for t in prom["targets"]]},
        "otlp": None if otlp is None else {
            "services": otlp["services"], "bind": otlp.get("bind", "127.0.0.1"), "port": otlp.get("port", 4318),
            "token_env": otlp.get("token_env", ""), "window_seconds": otlp.get("window_seconds", 60),
            "emit_seconds": otlp.get("emit_seconds", 15), "min_requests": otlp.get("min_requests", 5),
            "warning": th(otlp.get("warning")), "error": th(otlp.get("error"))},
    }


class Harness:
    def __init__(self, binary: Path, tmp: Path):
        self.binary, self.tmp = binary, tmp
        self.cases = self.failures = 0

    def rust(self, *args, data: bytes = b"", env=None) -> tuple[int, str]:
        run = subprocess.run([str(self.binary), *args], input=data, capture_output=True, timeout=60,
                             env={**os.environ, **(env or {})})
        return run.returncode, run.stdout.decode()

    def expect(self, ok: bool, label: str, detail: str = "") -> None:
        self.cases += 1
        if not ok:
            self.failures += 1
            print(f"MISMATCH {label}\n  {detail[:1500]}")

    # 1 ------------------------------------------------------------ configuration
    def config_case(self, config, label: str) -> None:
        path = self.tmp / "cfg" / f"{self.cases}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(config if isinstance(config, bytes) else json.dumps(config).encode())
        try:
            py = python_effective(path)
        except (feeds.FeedError, OSError, ValueError) as exc:
            py = f"ERROR {exc}"
        code, out = self.rust("validate", "--config", str(path))
        rs = json.loads(out) if code == 0 else f"ERROR {out.strip()}"
        same = (isinstance(py, str) and isinstance(rs, str)) or py == rs
        self.expect(same, f"config {label}", f"python: {py}\n  rust:   {rs}")

    def configs(self, seeds: int) -> None:
        self.config_case(BASE, "base")
        for u in URLS:
            for key in ("probe", "prometheus"):
                c = copy.deepcopy(BASE)
                if key == "probe":
                    c["probes"][0]["url"] = u
                else:
                    c["prometheus"]["url"] = u
                self.config_case(c, f"{key} url {u!r}")
        for raw in [b"\xff\xfe{}", b"{\"schema_version\": 1, \"schema_version\": 1}", b"[]", b"{", b"NaN",
                    json.dumps({**BASE, "kind": "X"}).encode(), json.dumps({**BASE, "schema_version": True}).encode(),
                    json.dumps({**BASE, "schema_version": 1.0}).encode(), json.dumps({**BASE, "extra": 1}).encode(),
                    json.dumps({k: v for k, v in BASE.items() if k not in ("probes", "prometheus", "otlp")}).encode(),
                    json.dumps({**BASE, "probes": None}).encode(), json.dumps({**BASE, "prometheus": None, "otlp": None,
                                                                               "probes": []}).encode(),
                    json.dumps({**BASE, "output": "/abs/x.jsonl"}).encode(), json.dumps({**BASE, "output": "x.json"}).encode(),
                    json.dumps({**BASE, "allowed_hosts": ["API.INTERNAL", "PROM.internal", "::1", "127.0.0.1"]}).encode(),
                    json.dumps({**BASE, "allowed_hosts": ["a" * 254]}).encode(),
                    ("{\"schema_version\":1,\"kind\":\"ONTOLOGYEX_FEEDS\",\"output\":\"\\ud800.jsonl\",\"probes\":[]}").encode(),
                    (json.dumps(BASE)[:-1] + ', "x": ' + "[" * 64 + "1" + "]" * 64 + "}").encode(),
                    (json.dumps(BASE)[:-1] + ', "x": ' + "[" * 63 + "1" + "]" * 63 + "}").encode()]:
            self.config_case(raw, f"raw {raw[:40]!r}")
        # Seeded field mutations across every section.
        paths = [("keep_per_component",), ("probes", 0, "interval_seconds"), ("probes", 0, "timeout_seconds"),
                 ("probes", 0, "expect_status"), ("probes", 0, "warning"), ("probes", 0, "component"),
                 ("prometheus", "token_env"), ("prometheus", "interval_seconds"), ("prometheus", "timeout_seconds"),
                 ("prometheus", "targets"), ("prometheus", "targets", 0, "up"), ("prometheus", "targets", 0, "error"),
                 ("otlp", "bind"), ("otlp", "port"), ("otlp", "token_env"), ("otlp", "window_seconds"),
                 ("otlp", "emit_seconds"), ("otlp", "min_requests"), ("otlp", "services"), ("otlp", "warning"),
                 ("allowed_hosts",), ("output",)]
        for s in range(seeds):
            rng = random.Random(f"config:{s}")
            c = copy.deepcopy(BASE)
            for _ in range(rng.randint(1, 3)):
                where = rng.choice(paths)
                node = c
                for k in where[:-1]:
                    ok = (isinstance(node, dict) and k in node) or (isinstance(node, list) and isinstance(k, int)
                                                                     and k < len(node))
                    node = node[k] if ok else None
                if not isinstance(node, (dict, list)) or (isinstance(node, list) and where[-1] >= len(node)):
                    continue  # an earlier mutation replaced this branch
                if rng.random() < 0.15 and isinstance(node, dict):
                    node.pop(where[-1], None)
                else:
                    node[where[-1]] = copy.deepcopy(rng.choice(POOL))
            self.config_case(c, f"seed {s}: {json.dumps(c)[:200]}")

    # 2 ------------------------------------------------------------ OTLP decoding
    def decode_case(self, body: bytes, mime: str, encoding: str, label: str) -> None:
        code, spans = feeds.decode_request(body, mime, encoding)
        py = {"code": code, "spans": [[s, k == 2, c == 2, d] for s, k, c, d in spans]}
        _, out = self.rust("decode", "--content-type", mime, "--content-encoding", encoding, data=body)
        rs = json.loads(out)
        self.expect(py == rs, f"decode {label}", f"python: {json.dumps(py)[:400]}\n  rust:   {json.dumps(rs)[:400]}")

    def decoding(self, seeds: int) -> None:
        proto = proto_request("payments", [(2, 0, 12.5), (2, 2, 40.0), (3, 0, 1.0), (2, 1, 0.0)])
        js = json.dumps({"resourceSpans": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "api"}}]},
            "scopeSpans": [{"spans": [
                {"kind": "SPAN_KIND_SERVER", "startTimeUnixNano": "1761652800000000000",
                 "endTimeUnixNano": "1761652800012500000", "status": {"code": "STATUS_CODE_ERROR"}},
                {"kind": 2, "startTimeUnixNano": 5, "endTimeUnixNano": 3},
                {"kind": "SPAN_KIND_CLIENT"}]}]}]}).encode()
        fixed = [
            (proto, "application/x-protobuf", ""), (proto, "application/x-protobuf; charset=x", " Identity "),
            (gzip.compress(proto), "application/x-protobuf", "gzip"), (gzip.compress(proto)[:-3], "application/x-protobuf", "gzip"),
            (gzip.compress(proto) + b"trailing", "application/x-protobuf", "GZIP"), (b"", "application/x-protobuf", "gzip"),
            (gzip.compress(proto) + gzip.compress(js), "application/x-protobuf", "gzip"),
            (js, "application/json", ""), (gzip.compress(js), "application/json", "gzip"), (js, "Application/JSON", ""),
            (js, "text/plain", ""), (js, "application/json", "br"), (proto, "application/json", ""),
            (b"", "application/json", ""), (b"", "application/x-protobuf", ""), (b"[]", "application/json", ""),
            (b'{"resourceSpans": null}', "application/json", ""), (b'{"resourceSpans": [5]}', "application/json", ""),
            (b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"kind": 2.0}]}]}]}', "application/json", ""),
            (b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"kind": true}]}]}]}', "application/json", ""),
            (b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"kind": -5}]}]}]}', "application/json", ""),
            (b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"kind": 18446744073709551616}]}]}]}', "application/json", ""),
            (b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"startTimeUnixNano": "18446744073709551616"}]}]}]}', "application/json", ""),
            (b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"startTimeUnixNano": "-1"}]}]}]}', "application/json", ""),
            (b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"startTimeUnixNano": 1.5}]}]}]}', "application/json", ""),
            (b'{"resourceSpans": [], "x": NaN}', "application/json", ""), (b'{"resourceSpans": [], "x": 1e400}', "application/json", ""),
            (b'{"resourceSpans": [], "x": "\\ud800"}', "application/json", ""),
            (b'{"resourceSpans": [], "x": ' + b"9" * 400 + b"}", "application/json", ""),
            (b'{"resourceSpans": [], "x": ' + b"[" * 64 + b"]" * 64 + b"}", "application/json", ""),
            (b'{"resourceSpans": [], "x": ' + b"[" * 63 + b"]" * 63 + b"}", "application/json", ""),
            (b'{"resourceSpans": [], "resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": 5}}]}}]}',
             "application/json", ""),
            ("\ufeff{}".encode(), "application/json", ""), (b"\xff\xfe{\x00}\x00", "application/json", ""),
        ]
        for i, (body, mime, enc) in enumerate(fixed):
            self.decode_case(body, mime, enc, f"fixed {i}")
        for s in range(seeds):
            rng = random.Random(f"decode:{s}")
            base, mime = rng.choice([(proto, "application/x-protobuf"), (js, "application/json")])
            data = bytearray(base)
            for _ in range(rng.randint(1, 3)):
                op, pos = rng.random(), rng.randrange(len(data) + 1)
                if op < 0.4 and data:
                    data[min(pos, len(data) - 1)] ^= 1 << rng.randrange(8)
                elif op < 0.7:
                    data[pos:pos] = bytes([rng.randrange(256)])
                elif data:
                    del data[min(pos, len(data) - 1)]
            body = bytes(data)
            enc = rng.choice(["", "", "gzip"])
            if enc == "gzip":
                body = gzip.compress(body)
                if rng.random() < 0.3:
                    body = body[:rng.randrange(len(body))]
            self.decode_case(body, mime, enc, f"seed {s}")

    # 3 ------------------------------------------------------------ Prometheus
    def prometheus(self) -> None:
        def body(result_type, result):
            return json.dumps({"status": "success", "data": {"resultType": result_type, "result": result}}).encode()
        cases = [body("vector", [{"metric": {}, "value": [1, v]}]) for v in
                 ["1", "0", "2.5", "-3", "1e3", "1E-3", ".5", "5.", "+1", "NaN", "+Inf", "-Inf", "Inf", " 1", "1_0",
                  "0x10", "inf", "nan", "", "1e", "1e400", "-0", "123456789.123456789"]]
        cases += [body("scalar", [1, "7"]), body("scalar", [1, 7]), body("scalar", "ab"), body("scalar", {"1": "x"}),
                  body("scalar", [1]), body("vector", []), body("vector", [{"value": [1, "1"]}] * 2),
                  body("vector", [{"value": "x1"}]), body("vector", ["x"]), body("vector", [{"x": 1}]), body("matrix", []),
                  b"[]", b"null", b"{}", b'{"status": "error"}', b'{"status": "success"}', b'{"status": "success", "data": []}',
                  b'{"status": "success", "data": {"result": []}}', b"not json", b"\xff", b'{"status": "success", "x": NaN}',
                  "\ufeff{}".encode()]
        for i, data in enumerate(cases):
            try:
                py = {"value": feeds.prometheus_number(data)}
            except feeds.FeedError:
                py = "ERROR"
            _, out = self.rust("prom-value", data=data)
            rs = json.loads(out)
            rs = rs if "value" in rs else "ERROR"
            self.expect(py == rs, f"prometheus {i}: {data[:80]!r}", f"python: {py}  rust: {rs}")

    # 4 ------------------------------------------------------------ span window
    def windows(self, seeds: int) -> None:
        moment = 1761652800
        for s in range(seeds):
            rng = random.Random(f"window:{s}")
            otlp = {"services": {"payments": PAY, "api": API}, "window_seconds": rng.choice([10, 60, 300]),
                    "emit_seconds": rng.choice([1, 15]), "min_requests": rng.choice([1, 3, 20]),
                    "warning": rng.choice([{}, {"p95_latency_ms": 250}, {"error_rate_pct": 2.5}]),
                    "error": rng.choice([{}, {"error_rate_pct": 10}, {"p95_latency_ms": 900.5}])}
            steps, at = [], 0.0
            for _ in range(rng.randint(1, 12)):
                at += rng.choice([0.0, 0.5, 3.0, 11.0, 70.0])
                if rng.random() < 0.3:
                    steps.append({"at": at, "emit": True})
                    continue
                spans = [[rng.choice(["payments", "api", "ghost", ""]), rng.choice([2, 2, 2, 1, 3]),
                          rng.choice([0, 0, 1, 2]), round(rng.uniform(0, 2000), rng.choice([0, 3, 9]))]
                         for _ in range(rng.randint(0, 40))]
                steps.append({"at": at, "spans": spans})
            steps.append({"at": at, "emit": True})
            window = feeds.SpanWindow(otlp)
            emitted = []
            stamp_at = datetime.fromtimestamp(moment, timezone.utc)
            for step in steps:
                if step.get("emit"):
                    emitted.append([json.dumps(r, sort_keys=True, separators=(",", ":"))
                                    for r in window.observations(step["at"], stamp_at)])
                else:
                    window.ingest([tuple(x) for x in step["spans"]], step["at"])
            py = {"emitted": emitted, "accepted": window.accepted, "unmapped": dict(sorted(window.unmapped.items()))}
            _, out = self.rust("window-replay", data=json.dumps({"otlp": otlp, "moment": moment, "steps": steps}).encode())
            rs = json.loads(out)
            self.expect(py == rs, f"window seed {s}", f"python: {json.dumps(py)[:700]}\n  rust:   {json.dumps(rs)[:700]}")

    # 5 ------------------------------------------------------------ store
    def stores(self, seeds: int) -> None:
        stamps = ["2026-10-28T08:00:00Z", "2026-10-28T10:00:00+02:00", "2026-10-28T08:00:00.5Z", "2026-10-28T07:59:59-00:00",
                  "2026-10-28T08:00Z", "2026-10-28T08:00:00.1234567Z", "2026-12-31T23:30:00-01:00"]
        for s in range(seeds):
            rng = random.Random(f"store:{s}")
            def row(i):
                metrics = rng.choice([{}, {"error_rate_pct": rng.choice([0, 1.5, 100])}, {"p95_latency_ms": rng.choice([3, 1e-05, 12.345])}])
                return {"schema_version": 1, "component_id": rng.choice([API, PAY]), "observed_at": rng.choice(stamps),
                        "status": rng.choice(["ok", "warning", "error"]), "source": rng.choice(["otel", "prometheus", "synthetic"]),
                        "expires_after_seconds": rng.choice([60, 90]), "metrics": metrics}
            existing = [row(i) for i in range(rng.randint(0, 14))]
            new = [row(i) for i in range(rng.randint(1, 6))]
            for r in new:  # new rows come from observation(): canonical stamps and floats
                r["observed_at"] = "2026-10-28T09:00:0%dZ" % rng.randrange(10)
                r["metrics"] = {k: float(v) for k, v in r["metrics"].items()}
            keep = rng.choice([2, 3, 8])
            outs = []
            for impl in ("python", "rust"):
                path = self.tmp / f"store-{impl}-{s}" / "observations.jsonl"
                path.parent.mkdir()
                if existing:
                    path.write_text("".join(json.dumps(r) + rng.choice(["\n", "\r\n"]) for r in existing) + "\n")
                if impl == "python":
                    feeds.ObservationStore(path, keep).add(copy.deepcopy(new))
                else:
                    code, out = self.rust("store-add", "--output", str(path), "--keep", str(keep), data=json.dumps(new).encode())
                    if code:
                        outs.append(f"ERROR {out}")
                        continue
                outs.append(path.read_text())
            self.expect(outs[0] == outs[1], f"store seed {s}", f"python: {outs[0][:600]}\n  rust:   {outs[1][:600]}")

    # 6 ------------------------------------------------------------ once, end to end
    def once(self) -> None:
        class Server(ThreadingHTTPServer):
            # ontologyex-feeds polls up to 16 targets at once; socketserver's default
            # listen queue of 5 makes macOS refuse the extra connections outright.
            request_queue_size = 64

        server = Server(("127.0.0.1", 0), Fixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        refused = socket.socket()
        refused.bind(("127.0.0.1", 0))
        closed = refused.getsockname()[1]
        refused.close()
        config = {"schema_version": 1, "kind": "ONTOLOGYEX_FEEDS", "output": "obs.jsonl", "allowed_hosts": ["127.0.0.1"],
                  "probes": [{"component": API, "url": f"{base}/healthz"},
                             {"component": PAY, "url": f"{base}/fail", "expect_status": [200]},
                             {"component": "discovery:service:compose.yml:redirect", "url": f"{base}/redirect"},
                             {"component": "discovery:service:compose.yml:down", "url": f"http://127.0.0.1:{closed}/"},
                             {"component": "discovery:service:compose.yml:slow", "url": f"{base}/slow", "timeout_seconds": 1},
                             {"component": "discovery:service:compose.yml:warn", "url": f"{base}/healthz",
                              "warning": {"probe_latency_ms": 0}}],
                  "prometheus": {"url": base, "token_env": "PARITY_PROM_TOKEN", "targets": [
                      {"component": "discovery:service:compose.yml:prom", "up": "up_api", "error_rate_pct": "err_api",
                       "p95_latency_ms": "lat_api", "warning": {"error_rate_pct": 5}, "error": {"p95_latency_ms": 100}},
                      {"component": "discovery:service:compose.yml:two", "up": "two"},
                      {"component": "discovery:service:compose.yml:nan", "up": "nan"},
                      {"component": "discovery:service:compose.yml:missing", "up": "missing"}]}}
        rows = {}
        os.environ["PARITY_PROM_TOKEN"] = "s3cret"
        for impl in ("python", "rust"):
            d = self.tmp / f"once-{impl}"
            d.mkdir()
            (d / "feeds.json").write_text(json.dumps(config))
            if impl == "python":
                runner = feeds.FeedRunner(feeds.load_config(d / "feeds.json"))
                runner.poll_once()
                ok = {k: v["ok"] for k, v in runner.status.items()}
            else:
                code, out = self.rust("once", "--config", str(d / "feeds.json"), env={"PARITY_PROM_TOKEN": "s3cret"})
                ok = {k: v["ok"] for k, v in json.loads(out)["feeds"].items()} if code == 0 else out
            masked = []
            for line in (d / "obs.jsonl").read_text().splitlines():
                r = json.loads(line)
                r["observed_at"] = "T"
                if "probe_latency_ms" in r["metrics"]:
                    r["metrics"]["probe_latency_ms"] = "ms"
                masked.append(r)
            status = json.loads((d / "obs.status.json").read_text())
            rows[impl] = (masked, ok, sorted(status["feeds"]), status["kind"])
        server.shutdown()
        py, rs = rows["python"], rows["rust"]
        detail = [f"{part}: python {a} rust {b}" for part, a, b in (
            ("rows", [r for r in py[0] if r not in rs[0]], [r for r in rs[0] if r not in py[0]]),
            ("ok", {k: v for k, v in py[1].items() if rs[1].get(k) != v} if isinstance(rs[1], dict) else py[1], rs[1]),
            ("status keys", py[2], rs[2]), ("kind", py[3], rs[3])) if a != b or part == "ok" and a]
        self.expect(py == rs, "once end to end", "\n  ".join(detail))
        self.expect(set(Fixture.seen_auth) <= {"Bearer s3cret"}, "prometheus bearer token", str(Fixture.seen_auth))

    # 7 ------------------------------------------------------------ OTLP receiver
    def receiver(self) -> None:
        def free_port():
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return sock.getsockname()[1]

        def post(port, path, body, headers):
            if len(body) > feeds.MAX_OTLP_BODY:  # declare the size only; the server refuses before reading
                with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                    sock.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n{head}\r\n".encode())
                    return int(sock.recv(64).split()[1])
            request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST", headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status
            except urllib.error.HTTPError as exc:
                return exc.code

        proto = proto_request("payments", [(2, 0, 10.0), (2, 2, 30.0), (2, 0, 20.0), (3, 0, 5.0)])
        requests = [
            ("/v1/traces", proto, {"Content-Type": "application/x-protobuf", "Authorization": "Bearer tok"}),
            ("/v1/traces?x=1", proto, {"Content-Type": "application/x-protobuf", "Authorization": "Bearer tok"}),
            ("/v1/traces", gzip.compress(proto), {"Content-Type": "application/x-protobuf", "Content-Encoding": "gzip",
                                                  "Authorization": "Bearer tok"}),
            ("/v1/traces", proto, {"Content-Type": "application/x-protobuf"}),
            ("/v1/traces", proto, {"Content-Type": "application/x-protobuf", "Authorization": "Bearer nope"}),
            ("/v1/metrics", proto, {"Content-Type": "application/x-protobuf", "Authorization": "Bearer tok"}),
            ("/v1/traces", b"{}", {"Content-Type": "text/plain", "Authorization": "Bearer tok"}),
            ("/v1/traces", b"{", {"Content-Type": "application/json", "Authorization": "Bearer tok"}),
            ("/v1/traces", proto, {"Content-Type": "application/x-protobuf", "Content-Encoding": "br", "Authorization": "Bearer tok"}),
            ("/v1/traces", b"x" * (4 * 1024 * 1024 + 1), {"Content-Type": "application/x-protobuf", "Authorization": "Bearer tok"}),
        ]
        codes, emitted = {}, {}
        for impl in ("python", "rust"):
            port = free_port()
            d = self.tmp / f"otlp-{impl}"
            d.mkdir()
            config = {"schema_version": 1, "kind": "ONTOLOGYEX_FEEDS", "output": "obs.jsonl",
                      "otlp": {"bind": "127.0.0.1", "port": port, "token_env": "PARITY_OTLP_TOKEN",
                               "services": {"payments": PAY}, "emit_seconds": 1, "min_requests": 3,
                               "error": {"error_rate_pct": 20}}}
            (d / "feeds.json").write_text(json.dumps(config))
            env = {**os.environ, "PARITY_OTLP_TOKEN": "tok"}
            command = ([str(self.binary), "run", "--config", str(d / "feeds.json")] if impl == "rust" else
                       [sys.executable, str(ROOT / "ontology-extraction/scripts/feeds.py"), "run", "--config", str(d / "feeds.json")])
            process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    try:
                        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                        break
                    except OSError:
                        time.sleep(0.1)
                codes[impl] = [post(port, path, body, headers) for path, body, headers in requests]
                deadline = time.time() + 10
                rows = []
                while time.time() < deadline:
                    out = d / "obs.jsonl"
                    if out.exists() and out.read_text().strip():
                        rows = [json.loads(x) for x in out.read_text().splitlines()]
                        break
                    time.sleep(0.2)
                for r in rows:
                    r["observed_at"] = "T"
                emitted[impl] = rows
            finally:
                process.terminate()
                process.wait(timeout=10)
        self.expect(codes["python"] == codes["rust"], "receiver status codes", f"python: {codes['python']}\n  rust: {codes['rust']}")
        self.expect(emitted["python"] == emitted["rust"] and emitted["rust"], "receiver rows",
                    f"python: {emitted['python']}\n  rust: {emitted['rust']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--seeds", type=int, default=300)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(args.binary.resolve(), Path(tmp))
        for part in (lambda: h.configs(args.seeds), lambda: h.decoding(args.seeds), h.prometheus,
                     lambda: h.windows(args.seeds), lambda: h.stores(args.seeds // 3), h.once, h.receiver):
            part()
    print(json.dumps({"cases": h.cases, "mismatches": h.failures}))
    return 1 if h.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
