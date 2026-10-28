#!/usr/bin/env python3
"""Live observability feeds for the control room: OTLP traces, Prometheus, HTTP probes.

A separate, opt-in process. It turns real telemetry into the control room's strict
observation rows (see control_room.read_observations) and keeps them in one bounded
JSONL file that `control_room.py --observations` reads. The control room itself
still makes no network calls.

- OTLP/HTTP receiver: apps or an OpenTelemetry Collector export traces here
  (protobuf or JSON, optional gzip, optional bearer token). Server spans become
  per-service error rate and p95 latency over a sliding window.
- Prometheus: operator-written instant queries for up, error rate, and latency.
- HTTP probes: GET allowlisted health URLs; no redirects are followed.

Every sample is still source-reported: a probe answering 200 is one answer, not
proof of health, and nothing here repairs or changes a system.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile
from threading import Event, Lock, Thread
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

import control_room

FEED_KIND = "ONTOLOGYEX_FEEDS"
MAX_CONFIG_BYTES = 65536
MAX_TARGETS = 256
MAX_BODY = 1024 * 1024
MAX_OTLP_BODY = 4 * 1024 * 1024
MAX_OTLP_DECOMPRESSED = 16 * 1024 * 1024
MAX_SPANS_PER_SERVICE = 100_000
KEEP_PER_COMPONENT = 8
SERVER_KIND = 2
ERROR_CODE = 2
METRIC_RULES = ("error_rate_pct", "p95_latency_ms", "probe_latency_ms")
IDENT = re.compile(r"^discovery:[a-z_]+:[A-Za-z0-9_.~/%:-]{1,600}$")
SERVICE = re.compile(r"^[A-Za-z0-9_.:/@-]{1,200}$")


class FeedError(ValueError):
    """Invalid feed configuration or input; nothing is written."""


def check(ok: bool, message: str) -> None:
    if not ok:
        raise FeedError(message)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- configuration

def _keys(value: object, required: set, optional: set, where: str) -> dict:
    check(type(value) is dict, f"{where}: expected an object")
    check(required <= set(value) <= required | optional, f"{where}: unexpected or missing fields")
    return value


def _seconds(value: object, low: int, high: int, where: str) -> int:
    check(type(value) is int and low <= value <= high, f"{where}: expected {low}..{high} seconds")
    return value


def _thresholds(value: object, where: str) -> dict:
    rules = _keys(value if value is not None else {}, set(), set(METRIC_RULES), where)
    for name, limit in rules.items():
        check(type(limit) in (int, float) and math.isfinite(limit) and 0 <= limit <= 1e9,
              f"{where}.{name}: invalid threshold")
        check(name != "error_rate_pct" or limit <= 100, f"{where}.{name}: percentage above 100")
    return rules


def _component(value: object, where: str) -> str:
    check(type(value) is str and IDENT.fullmatch(value) is not None,
          f"{where}: expected a discovered component ID (discovery:...)")
    return value


def _url(value: object, hosts: set, where: str) -> str:
    check(type(value) is str and len(value) <= 2048, f"{where}: invalid URL")
    parts = urllib.parse.urlsplit(value)
    check(parts.scheme in ("http", "https"), f"{where}: only http and https")
    check(parts.username is None and parts.password is None,
          f"{where}: never put credentials in a URL; use token_env")
    check(not parts.fragment, f"{where}: no URL fragment")
    check(parts.hostname is not None and parts.hostname.lower() in hosts,
          f"{where}: host {parts.hostname!r} is not in allowed_hosts")
    return value


def load_config(path: Path) -> dict:
    """Strict feed configuration. Secrets are named by environment variable, never stored."""
    check(path.is_file() and not path.is_symlink(), "config must be a regular file")
    data = path.read_bytes()
    check(len(data) <= MAX_CONFIG_BYTES, "config too large")
    try:
        config = json.loads(data, object_pairs_hook=control_room.no_duplicates)
    except (ValueError, control_room.RoomError) as exc:
        raise FeedError(f"config is not valid JSON: {exc}") from exc
    _keys(config, {"schema_version", "kind", "output"},
          {"allowed_hosts", "keep_per_component", "probes", "prometheus", "otlp"}, "config")
    check(config["schema_version"] == 1 and config["kind"] == FEED_KIND, "unsupported feed config")
    check(type(config["output"]) is str and config["output"].endswith(".jsonl"),
          "output must be a .jsonl path")
    hosts = config.get("allowed_hosts", [])
    check(type(hosts) is list and len(hosts) <= 64 and
          all(type(h) is str and 0 < len(h) <= 253 for h in hosts), "invalid allowed_hosts")
    hosts = {h.lower() for h in hosts}
    keep = config.get("keep_per_component", KEEP_PER_COMPONENT)
    check(type(keep) is int and 2 <= keep <= 64, "keep_per_component must be 2..64")
    check(any(k in config for k in ("probes", "prometheus", "otlp")), "configure at least one feed")

    probes = config.get("probes", [])
    check(type(probes) is list and len(probes) <= MAX_TARGETS, "invalid probes")
    for i, probe in enumerate(probes):
        where = f"probes[{i}]"
        _keys(probe, {"component", "url"},
              {"interval_seconds", "timeout_seconds", "expect_status", "warning"}, where)
        _component(probe["component"], where)
        _url(probe["url"], hosts, where)
        _seconds(probe.get("interval_seconds", 30), 5, 3600, where)
        _seconds(probe.get("timeout_seconds", 3), 1, 30, where)
        expect = probe.get("expect_status", [200])
        check(type(expect) is list and 0 < len(expect) <= 16 and
              all(type(c) is int and 100 <= c <= 599 for c in expect), f"{where}: invalid expect_status")
        _thresholds(probe.get("warning"), where + ".warning")

    prom = config.get("prometheus")
    if prom is not None:
        _keys(prom, {"url", "targets"}, {"token_env", "interval_seconds", "timeout_seconds"}, "prometheus")
        _url(prom["url"], hosts, "prometheus.url")
        check(type(prom.get("token_env", "")) is str and len(prom.get("token_env", "")) <= 64,
              "prometheus.token_env must name an environment variable")
        _seconds(prom.get("interval_seconds", 30), 5, 3600, "prometheus")
        _seconds(prom.get("timeout_seconds", 5), 1, 30, "prometheus")
        targets = prom["targets"]
        check(type(targets) is list and 0 < len(targets) <= MAX_TARGETS, "invalid prometheus.targets")
        for i, target in enumerate(targets):
            where = f"prometheus.targets[{i}]"
            _keys(target, {"component"}, {"up", "error_rate_pct", "p95_latency_ms", "warning", "error"}, where)
            _component(target["component"], where)
            queries = [target.get(k) for k in ("up", "error_rate_pct", "p95_latency_ms") if k in target]
            check(queries and all(type(q) is str and 0 < len(q) <= 2000 for q in queries),
                  f"{where}: give at least one PromQL query")
            _thresholds(target.get("warning"), where + ".warning")
            _thresholds(target.get("error"), where + ".error")

    otlp = config.get("otlp")
    if otlp is not None:
        _keys(otlp, {"services"}, {"bind", "port", "token_env", "window_seconds", "emit_seconds",
                                   "min_requests", "warning", "error"}, "otlp")
        bind = otlp.get("bind", "127.0.0.1")
        try:
            loopback = ipaddress.ip_address(bind).is_loopback
        except ValueError as exc:
            raise FeedError("otlp.bind must be an IP address") from exc
        token_env = otlp.get("token_env", "")
        check(type(token_env) is str and len(token_env) <= 64, "otlp.token_env must name a variable")
        check(loopback or token_env, "a non-loopback OTLP listener requires token_env")
        port = otlp.get("port", 4318)
        check(type(port) is int and 0 <= port <= 65535, "invalid otlp.port")
        services = otlp["services"]
        check(type(services) is dict and 0 < len(services) <= MAX_TARGETS, "invalid otlp.services")
        for name, component in services.items():
            check(SERVICE.fullmatch(name) is not None, f"otlp.services: invalid service.name {name!r}")
            _component(component, f"otlp.services[{name}]")
        _seconds(otlp.get("window_seconds", 60), 10, 3600, "otlp.window_seconds")
        _seconds(otlp.get("emit_seconds", 15), 1, 600, "otlp.emit_seconds")
        check(type(otlp.get("min_requests", 5)) is int and 1 <= otlp.get("min_requests", 5) <= 100000,
              "otlp.min_requests must be 1..100000")
        _thresholds(otlp.get("warning"), "otlp.warning")
        _thresholds(otlp.get("error"), "otlp.error")
    config["_root"] = path.resolve().parent
    return config


def output_path(config: dict) -> Path:
    out = Path(config["output"])
    return out if out.is_absolute() else config["_root"] / out


def ttl(interval: int) -> int:
    """A sample expires after three missed intervals, so silence becomes STALE."""
    return max(60, min(86400, 3 * interval))


def classify(metrics: dict, warning: dict, error: dict, down: bool = False) -> str:
    """At or above an error threshold is error; at or above a warning threshold is warning."""
    if down or any(metrics.get(k) is not None and metrics[k] >= v for k, v in error.items()):
        return "error"
    if any(metrics.get(k) is not None and metrics[k] >= v for k, v in warning.items()):
        return "warning"
    return "ok"


def observation(component: str, status: str, source: str, interval: int, metrics: dict,
                moment: datetime) -> dict:
    clean = {k: round(float(v), 3) for k, v in metrics.items() if v is not None and math.isfinite(v)}
    for k in ("error_rate_pct",):
        if k in clean:
            clean[k] = min(100.0, max(0.0, clean[k]))
    clean = {k: min(1e9, max(0.0, v)) for k, v in clean.items()}
    return {"schema_version": 1, "component_id": component, "observed_at": stamp(moment),
            "status": status, "source": source, "expires_after_seconds": ttl(interval),
            "metrics": clean}


# ---------------------------------------------------------------- bounded store

class ObservationStore:
    """Keeps the newest samples per component in one JSONL file, rewritten atomically."""

    def __init__(self, path: Path, keep: int = KEEP_PER_COMPONENT):
        self.path = path
        self.keep = keep
        self.lock = Lock()

    def add(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self.lock:
            existing = (control_room.read_observations(self.path)
                        if self.path.exists() else [])
            by_component: dict[str, list[dict]] = {}
            for row in existing + rows:
                by_component.setdefault(row["component_id"], []).append(row)
            kept = []
            for component in sorted(by_component):
                samples = sorted(by_component[component],
                                 key=lambda r: (r["observed_at"], r["source"], r["status"]))
                kept.extend(samples[-self.keep:])
            text = "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in kept)
            check(len(kept) <= control_room.MAX_OBSERVATIONS and
                  len(text.encode()) <= control_room.MAX_OBSERVATION_BYTES,
                  "observation store budget exceeded; lower keep_per_component")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=self.path.parent, prefix=".observations-",
                                             delete=False, encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            Path(handle.name).replace(self.path)
            control_room.read_observations(self.path)  # the control room must accept it
            return len(rows)


# ---------------------------------------------------------------- outbound HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect is an answer, not a new target
        return None


# No proxies: targets are allowlisted hosts reached directly.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def fetch(url: str, timeout: float, headers: dict | None = None) -> tuple[int | None, bytes, float, str]:
    """GET with a byte cap. Returns (status, body, elapsed_ms, error)."""
    request = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "ontologyex-feeds/1", **(headers or {})})
    started = time.monotonic()
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read(MAX_BODY + 1)
            status = response.status
    except urllib.error.HTTPError as exc:
        body, status = exc.read(MAX_BODY + 1) if exc.fp else b"", exc.code
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return None, b"", (time.monotonic() - started) * 1000, type(reason).__name__
    elapsed = (time.monotonic() - started) * 1000
    if len(body) > MAX_BODY:
        return status, b"", elapsed, "RESPONSE_TOO_LARGE"
    return status, body, elapsed, ""


def run_probe(probe: dict, moment: datetime | None = None, getter=fetch) -> tuple[dict, str]:
    status, _, elapsed, error = getter(probe["url"], probe.get("timeout_seconds", 3))
    expected = probe.get("expect_status", [200])
    down = status is None or status not in expected
    metrics = {"probe_latency_ms": elapsed} if status is not None else {}
    state = classify(metrics, _thresholds(probe.get("warning"), "probe"), {}, down=down)
    detail = error or (f"HTTP {status}" if down else "")
    return (observation(probe["component"], state, "health_probe",
                        probe.get("interval_seconds", 30), metrics, moment or now_utc()), detail)


def prometheus_value(base: str, query: str, timeout: float, token: str, getter=fetch) -> float:
    """One scalar from an instant query that returns exactly one series."""
    url = base.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    status, body, _, error = getter(url, timeout, headers)
    check(status == 200, f"prometheus answered {error or status}")
    try:
        data = json.loads(body)
        check(data.get("status") == "success", "prometheus query failed")
        result = data["data"]["result"]
        kind = data["data"]["resultType"]
        if kind == "scalar":
            value = result[1]
        else:
            check(kind == "vector" and type(result) is list, "expected a vector or scalar result")
            check(len(result) == 1, f"query must return exactly one series, got {len(result)}")
            value = result[0]["value"][1]
        number = float(value)
    except FeedError:
        raise
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise FeedError(f"unexpected prometheus response: {type(exc).__name__}") from exc
    check(math.isfinite(number), "prometheus returned a non-finite value")
    return number


def run_prometheus(prom: dict, target: dict, moment: datetime | None = None,
                   getter=fetch) -> dict:
    token = os.environ.get(prom.get("token_env", ""), "") if prom.get("token_env") else ""
    timeout = prom.get("timeout_seconds", 5)
    metrics = {}
    down = False
    if "up" in target:
        down = prometheus_value(prom["url"], target["up"], timeout, token, getter) < 1
    for name in ("error_rate_pct", "p95_latency_ms"):
        if name in target:
            metrics[name] = prometheus_value(prom["url"], target[name], timeout, token, getter)
    state = classify(metrics, _thresholds(target.get("warning"), "w"),
                     _thresholds(target.get("error"), "e"), down=down)
    return observation(target["component"], state, "prometheus",
                       prom.get("interval_seconds", 30), metrics, moment or now_utc())


# ---------------------------------------------------------------- OTLP traces

def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        check(pos < len(buf) and shift < 64, "truncated protobuf varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def proto_fields(buf: bytes) -> list[tuple[int, int, object]]:
    """(field, wire type, value) for one protobuf message. Unknown fields are kept raw."""
    fields, pos = [], 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        field, wire = key >> 3, key & 7
        check(field > 0, "invalid protobuf field")
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 1:
            check(pos + 8 <= len(buf), "truncated fixed64")
            value = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
        elif wire == 2:
            size, pos = _varint(buf, pos)
            check(pos + size <= len(buf), "truncated protobuf bytes")
            value = buf[pos:pos + size]
            pos += size
        elif wire == 5:
            check(pos + 4 <= len(buf), "truncated fixed32")
            value = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
        else:
            raise FeedError("unsupported protobuf wire type")
        fields.append((field, wire, value))
    return fields


def _sub(fields, number):
    return [v for f, w, v in fields if f == number and w == 2]


def spans_from_protobuf(body: bytes) -> list[tuple[str, int, int, float]]:
    """ExportTraceServiceRequest -> (service.name, kind, status code, duration ms)."""
    out = []
    for resource_spans in _sub(proto_fields(body), 1):
        rs = proto_fields(resource_spans)
        service = ""
        for resource in _sub(rs, 1):
            for kv in _sub(proto_fields(resource), 1):
                kvf = proto_fields(kv)
                key = b"".join(_sub(kvf, 1)).decode("utf-8", "replace")
                if key == "service.name":
                    for any_value in _sub(kvf, 2):
                        for text in _sub(proto_fields(any_value), 1):
                            service = text.decode("utf-8", "replace")
        for scope_spans in _sub(rs, 2):
            for span in _sub(proto_fields(scope_spans), 2):
                sf = proto_fields(span)
                kind = next((v for f, w, v in sf if f == 6 and w == 0), 0)
                start = next((v for f, w, v in sf if f == 7 and w == 1), 0)
                end = next((v for f, w, v in sf if f == 8 and w == 1), 0)
                code = 0
                for status in _sub(sf, 15):
                    code = next((v for f, w, v in proto_fields(status) if f == 3 and w == 0), 0)
                out.append((service, kind, code, max(0, end - start) / 1e6))
    return out


_KINDS = {"SPAN_KIND_SERVER": 2, "SPAN_KIND_CLIENT": 3, "SPAN_KIND_INTERNAL": 1,
          "SPAN_KIND_PRODUCER": 4, "SPAN_KIND_CONSUMER": 5, "SPAN_KIND_UNSPECIFIED": 0}
_CODES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}


def spans_from_json(body: bytes) -> list[tuple[str, int, int, float]]:
    """OTLP/JSON (lowerCamelCase, 64-bit integers as strings)."""
    try:
        data = json.loads(body)
        out = []
        for rs in data.get("resourceSpans", []):
            service = ""
            for kv in rs.get("resource", {}).get("attributes", []):
                if kv.get("key") == "service.name":
                    service = str(kv.get("value", {}).get("stringValue", ""))
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    kind = span.get("kind", 0)
                    kind = _KINDS.get(kind, 0) if type(kind) is str else int(kind)
                    code = span.get("status", {}).get("code", 0)
                    code = _CODES.get(code, 0) if type(code) is str else int(code)
                    start = int(span.get("startTimeUnixNano", 0))
                    end = int(span.get("endTimeUnixNano", 0))
                    out.append((service, kind, code, max(0, end - start) / 1e6))
        return out
    except (ValueError, TypeError, AttributeError) as exc:
        raise FeedError("invalid OTLP/JSON traces") from exc


def p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


class SpanWindow:
    """Server spans per service over a sliding window, timed by arrival (not span clocks)."""

    def __init__(self, otlp: dict):
        self.services = otlp["services"]
        self.window = otlp.get("window_seconds", 60)
        self.emit = otlp.get("emit_seconds", 15)
        self.min_requests = otlp.get("min_requests", 5)
        self.warning = _thresholds(otlp.get("warning"), "w")
        self.error = _thresholds(otlp.get("error"), "e")
        self.spans: dict[str, deque] = {}
        self.unmapped: dict[str, int] = {}
        self.accepted = 0
        self.lock = Lock()

    def ingest(self, spans: list[tuple[str, int, int, float]], arrived: float) -> int:
        count = 0
        with self.lock:
            for service, kind, code, duration in spans:
                if kind != SERVER_KIND:
                    continue
                if service not in self.services:
                    if len(self.unmapped) < 64 or service in self.unmapped:
                        self.unmapped[service] = self.unmapped.get(service, 0) + 1
                    continue
                queue = self.spans.setdefault(service, deque(maxlen=MAX_SPANS_PER_SERVICE))
                queue.append((arrived, code == ERROR_CODE, duration))
                count += 1
            self.accepted += count
        return count

    def observations(self, arrived: float, moment: datetime | None = None) -> list[dict]:
        rows = []
        with self.lock:
            for service, queue in sorted(self.spans.items()):
                while queue and queue[0][0] < arrived - self.window:
                    queue.popleft()
                if len(queue) < self.min_requests:
                    continue  # too little traffic to say anything; the last sample expires
                errors = sum(1 for _, failed, _ in queue if failed)
                metrics = {"error_rate_pct": 100.0 * errors / len(queue),
                           "p95_latency_ms": p95([d for _, _, d in queue])}
                rows.append(observation(self.services[service],
                                        classify(metrics, self.warning, self.error), "otel",
                                        self.emit, metrics, moment or now_utc()))
        return rows


def otlp_handler(window: SpanWindow, token: str):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code: int, body: bytes, mime: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if urllib.parse.urlsplit(self.path).path != "/v1/traces":
                return self.reply(404, b'{"error":"only /v1/traces is accepted"}')
            if token and not hmac.compare_digest(self.headers.get("Authorization", ""),
                                                 f"Bearer {token}"):
                return self.reply(401, b'{"error":"bearer token required"}')
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_OTLP_BODY:
                return self.reply(413, b'{"error":"body too large or unknown length"}')
            body = self.rfile.read(length)
            mime = self.headers.get("Content-Type", "").split(";")[0].strip()
            try:
                encoding = self.headers.get("Content-Encoding", "").strip().lower()
                if encoding == "gzip":
                    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    body = inflater.decompress(body, MAX_OTLP_DECOMPRESSED)
                    check(not inflater.unconsumed_tail, "decompressed body too large")
                elif encoding not in ("", "identity"):
                    return self.reply(415, b'{"error":"use gzip or no compression"}')
                if mime == "application/x-protobuf":
                    spans = spans_from_protobuf(body)
                elif mime == "application/json":
                    spans = spans_from_json(body)
                else:
                    return self.reply(415, b'{"error":"use application/x-protobuf or application/json"}')
            except (FeedError, zlib.error, RecursionError):
                return self.reply(400, b'{"error":"invalid OTLP traces"}')
            window.ingest(spans, time.monotonic())
            if mime == "application/x-protobuf":
                return self.reply(200, b"", "application/x-protobuf")  # empty ExportTraceServiceResponse
            return self.reply(200, b"{}")

        def log_message(self, *args):
            return

    return Handler


# ---------------------------------------------------------------- runner

class FeedRunner:
    def __init__(self, config: dict, getter=fetch):
        self.config = config
        self.getter = getter
        self.store = ObservationStore(output_path(config), config.get("keep_per_component", KEEP_PER_COMPONENT))
        self.status_path = output_path(config).with_suffix(".status.json")
        self.status: dict[str, dict] = {}
        self.window = SpanWindow(config["otlp"]) if "otlp" in config else None
        self.stop = Event()
        self.server = None

    def _record(self, feed: str, ok: bool, detail: str = "") -> None:
        self.status[feed] = {"last_run": stamp(now_utc()), "ok": ok, "detail": detail[:200]}

    def poll_once(self) -> int:
        rows = []
        for probe in self.config.get("probes", []):
            row, detail = run_probe(probe, getter=self.getter)
            rows.append(row)
            # The feed worked: a 503 or a refused connection is a verdict about the target.
            self._record("probe " + probe["component"], True,
                         f"target {row['status']}" + (f" ({detail})" if detail else ""))
        prom = self.config.get("prometheus")
        for target in (prom or {}).get("targets", []):
            name = "prometheus " + target["component"]
            try:
                rows.append(run_prometheus(prom, target, getter=self.getter))
                self._record(name, True)
            except FeedError as exc:
                self._record(name, False, str(exc))  # no sample: the last one expires to STALE
        written = self.store.add(rows)
        self.write_status()
        return written

    def emit_otlp(self) -> int:
        rows = self.window.observations(time.monotonic()) if self.window else []
        written = self.store.add(rows)
        if self.window:
            self.status["otlp"] = {"last_run": stamp(now_utc()), "ok": True,
                                   "detail": f"{self.window.accepted} server spans accepted",
                                   "unmapped_services": dict(sorted(self.window.unmapped.items()))}
        self.write_status()
        return written

    def write_status(self) -> None:
        document = {"schema_version": 1, "kind": "ONTOLOGYEX_FEED_STATUS",
                    "generated_at": stamp(now_utc()), "feeds": self.status,
                    "otlp_listening": (f"{self.server.server_address[0]}:{self.server.server_address[1]}"
                                       if self.server else None),
                    "semantics": "SOURCE_REPORTED_TELEMETRY_NOT_INDEPENDENTLY_ATTESTED"}
        with tempfile.NamedTemporaryFile("w", dir=self.status_path.parent, prefix=".feed-status-",
                                         delete=False, encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, indent=2)
        Path(handle.name).replace(self.status_path)

    def start_otlp(self) -> None:
        otlp = self.config["otlp"]
        token = os.environ.get(otlp.get("token_env", ""), "") if otlp.get("token_env") else ""
        check(not otlp.get("token_env") or token, f"environment variable {otlp.get('token_env')} is empty")
        self.server = ThreadingHTTPServer((otlp.get("bind", "127.0.0.1"), otlp.get("port", 4318)),
                                          otlp_handler(self.window, token))
        Thread(target=self.server.serve_forever, name="ontologyex-otlp", daemon=True).start()

    def run(self) -> None:
        if self.window:
            self.start_otlp()
        intervals = [p.get("interval_seconds", 30) for p in self.config.get("probes", [])]
        if "prometheus" in self.config:
            intervals.append(self.config["prometheus"].get("interval_seconds", 30))
        poll_every = min(intervals) if intervals else None
        next_poll = next_emit = time.monotonic()
        try:
            while not self.stop.is_set():
                current = time.monotonic()
                if poll_every and current >= next_poll:
                    self.poll_once()
                    next_poll = current + poll_every
                if self.window and current >= next_emit:
                    self.emit_otlp()
                    next_emit = current + self.window.emit
                self.stop.wait(0.5)
        finally:
            if self.server:
                self.server.shutdown()


def check_against(config: dict, repo: Path) -> dict:
    """Which configured components exist in the repository's discovered map."""
    import discover_repo

    known = {n["id"] for n in discover_repo.discover(repo)["nodes"]}
    used = {p["component"] for p in config.get("probes", [])}
    used |= {t["component"] for t in config.get("prometheus", {}).get("targets", [])}
    used |= set(config.get("otlp", {}).get("services", {}).values())
    return {"status": "OK" if used <= known else "UNKNOWN_COMPONENTS",
            "components": len(used), "unknown": sorted(used - known)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "once", "check"):
        p = commands.add_parser(name)
        p.add_argument("--config", type=Path, required=True)
        if name == "check":
            p.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.command == "check":
            result = check_against(config, args.repo)
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "OK" else 2
        runner = FeedRunner(config)
        if args.command == "once":
            print(json.dumps({"written": runner.poll_once(), "output": str(output_path(config)),
                              "feeds": runner.status}, indent=2))
            return 0
        print(f"OntologyEX feeds writing {output_path(config)}", flush=True)
        if runner.window:
            otlp = config["otlp"]
            print(f"OTLP/HTTP traces: http://{otlp.get('bind', '127.0.0.1')}:{otlp.get('port', 4318)}/v1/traces",
                  flush=True)
        runner.run()
        return 0
    except (FeedError, control_room.RoomError, OSError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
