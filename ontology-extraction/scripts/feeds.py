#!/usr/bin/env python3
"""Live observability feeds for the control room: OTLP traces, Prometheus, HTTP probes.

A separate, opt-in process. It turns real telemetry into the control room's strict
observation rows (see control_room.read_observations) and keeps them in one bounded
JSONL file that `control_room.py --observations` reads. The control room itself
still makes no network calls.

- OTLP/HTTP receiver: apps or an OpenTelemetry Collector export traces here
  (protobuf or JSON, optional gzip, optional bearer token). Server spans become
  per-service error rate and p95 latency over a sliding window. A client span in one
  service and the server span it caused in another (same trace, parent ID) count as
  one observed call between them, kept in a calls file beside the observations: what
  the traces show, never a confirmed dependency.
- Prometheus: operator-written instant queries for up, error rate, and latency.
- HTTP probes: GET allowlisted health URLs; no redirects are followed.

Every sample is still source-reported: a probe answering 200 is one answer, not
proof of health, and nothing here repairs or changes a system.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict, deque
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
CALLER_KINDS = (3, 4)            # CLIENT, PRODUCER
CALLEE_KINDS = (2, 5)            # SERVER, CONSUMER
MAX_PENDING_CALLS = 100_000      # unmatched span halves per side, each kept one window
MAX_CALL_PAIRS = 1024
MAX_CALLS_BYTES = 1024 * 1024
MAX_COUNT = 2 ** 63 - 1
CALLS_KIND = "ONTOLOGYEX_OBSERVED_CALLS"
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
METRIC_RULES = ("error_rate_pct", "p95_latency_ms", "probe_latency_ms")
IDENT = re.compile(r"^discovery:[a-z_]+:[A-Za-z0-9_.~/%:-]{1,600}$")
SERVICE = re.compile(r"^[A-Za-z0-9_.:/@-]{1,200}$")
# RFC 3986 characters only (no spaces, controls, or fragments), so every
# implementation reads the same scheme, host, and port.
URL_TEXT = re.compile(r"[A-Za-z0-9\-._~:/?\[\]@!$&'()*+,;=%]{1,2048}")
HOST_NAME = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*")
PROM_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|NaN|[+-]?Inf")
MAX_JSON_DEPTH = 64


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


def _host_ok(host: str, bracketed: bool) -> bool:
    """A DNS name, a dotted-quad IPv4 address, or (in brackets) an IPv6 address."""
    if bracketed:
        try:
            return "%" not in host and ipaddress.ip_address(host).version == 6
        except ValueError:
            return False
    if HOST_NAME.fullmatch(host) is None:
        return False
    last = host.rsplit(".", 1)[-1]
    if last.isdigit() or last.startswith("0x"):  # numeric hosts must be plain IPv4
        try:
            return ipaddress.IPv4Address(host) is not None
        except ValueError:
            return False
    return True


def _url(value: object, hosts: set, where: str) -> str:
    check(type(value) is str and URL_TEXT.fullmatch(value) is not None,
          f"{where}: invalid URL (printable RFC 3986 characters, at most 2048)")
    check("#" not in value, f"{where}: no URL fragment")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.netloc.rpartition("@")[2].rpartition("]")[2].partition(":")[2]
        check(port == "" or port.isdigit(), f"{where}: invalid port")
        parts.port  # range check
    except ValueError as exc:
        raise FeedError(f"{where}: invalid URL") from exc
    check(parts.scheme in ("http", "https") and value[:len(parts.scheme) + 3].lower() == parts.scheme + "://",
          f"{where}: only http and https")
    check("@" not in parts.netloc, f"{where}: never put credentials in a URL; use token_env")
    # A bracketed host is the whole host: nothing before '[', only ":port" after ']'.
    # (Older Python versions' urlsplit does not check this itself.)
    check("[" not in parts.netloc or re.fullmatch(r"\[[^\[\]]*\](?::[0-9]*)?", parts.netloc) is not None,
          f"{where}: invalid URL")
    check(not any(c in parts.path + parts.query for c in "[]"), f"{where}: brackets only around an IPv6 host")
    check(parts.hostname is not None and _host_ok(parts.hostname, "[" in parts.netloc),
          f"{where}: invalid host")
    check(parts.hostname in hosts, f"{where}: host {parts.hostname!r} is not in allowed_hosts")
    return value


def load_config(path: Path) -> dict:
    """Strict feed configuration. Secrets are named by environment variable, never stored."""
    check(path.is_file() and not path.is_symlink(), "config must be a regular file")
    data = path.read_bytes()
    check(len(data) <= MAX_CONFIG_BYTES, "config too large")
    config = json_document(data, "config", pairs=control_room.no_duplicates)
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
            check(type(bind) is str and "%" not in bind, "otlp.bind must be an IP address")
            address = ipaddress.ip_address(bind)
        except ValueError as exc:
            raise FeedError("otlp.bind must be an IP address") from exc
        # 127.0.0.0/8 or ::1, the same answer on every Python version.
        loopback = (address in ipaddress.ip_network("127.0.0.0/8") if address.version == 4
                    else address == ipaddress.IPv6Address("::1"))
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

def retain(rows: list[dict], keep: int) -> list[dict]:
    """The newest `keep` samples per component (by time, then source and status), components in order."""
    by_component: dict[str, list[dict]] = {}
    for row in rows:
        by_component.setdefault(row["component_id"], []).append(row)
    kept = []
    for component in sorted(by_component):
        samples = sorted(by_component[component], key=lambda r: (r["observed_at"], r["source"], r["status"]))
        kept.extend(samples[-keep:])
    return kept


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
            kept = retain(existing + rows, self.keep)
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


def json_document(body: bytes, where: str, pairs=None):
    """Strict UTF-8 JSON with finite numbers, valid Unicode, and at most MAX_JSON_DEPTH levels."""
    def reject(_):
        raise ValueError("non-finite number")

    try:
        data = json.loads(body.decode("utf-8"), parse_constant=reject, object_pairs_hook=pairs)
    except (ValueError, RecursionError) as exc:
        raise FeedError(f"invalid {where} JSON") from exc
    stack = [(data, 1)]
    while stack:
        value, depth = stack.pop()
        if type(value) in (dict, list):
            check(depth <= MAX_JSON_DEPTH, f"{where} JSON nested too deeply")
            items = list(value.items()) if type(value) is dict else [(None, v) for v in value]
            for key, item in items:
                if key is not None:
                    stack.append((key, depth + 1))
                stack.append((item, depth + 1))
        elif type(value) is float:
            check(math.isfinite(value), f"non-finite number in {where} JSON")
        elif type(value) is int:
            try:
                float(value)  # as large as a double can hold
            except OverflowError as exc:
                raise FeedError(f"number out of range in {where} JSON") from exc
        elif type(value) is str:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise FeedError(f"invalid Unicode in {where} JSON") from exc
    return data


def prometheus_value(base: str, query: str, timeout: float, token: str, getter=fetch) -> float:
    """One scalar from an instant query that returns exactly one series."""
    url = base.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    status, body, _, error = getter(url, timeout, headers)
    check(status == 200, f"prometheus answered {error or status}")
    return prometheus_number(body)


def prometheus_number(body: bytes) -> float:
    try:
        data = json_document(body, "prometheus")
        check(type(data) is dict and data.get("status") == "success", "prometheus query failed")
        result = data["data"]["result"]
        kind = data["data"]["resultType"]
        if kind == "scalar":
            value = result[1]
        else:
            check(kind == "vector" and type(result) is list, "expected a vector or scalar result")
            check(len(result) == 1, f"query must return exactly one series, got {len(result)}")
            value = result[0]["value"][1]
        check(type(value) is str and PROM_NUMBER.fullmatch(value) is not None,
              "prometheus value is not a number")
        number = float(value)
    except FeedError:
        raise
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
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


def _proto_id(fields, number: int, size: int) -> str:
    """A trace or span ID as lowercase hex; empty when absent, all zero, or the wrong size."""
    raw = next(iter(_sub(fields, number)), b"")
    return raw.hex() if len(raw) == size and any(raw) else ""


def spans_from_protobuf(body: bytes) -> list[tuple]:
    """ExportTraceServiceRequest -> (service.name, kind, status code, duration ms,
    trace ID, span ID, parent span ID)."""
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
                out.append((service, kind, code, max(0, end - start) / 1e6,
                            _proto_id(sf, 1, 16), _proto_id(sf, 2, 8), _proto_id(sf, 4, 8)))
    return out


_KINDS = {"SPAN_KIND_SERVER": 2, "SPAN_KIND_CLIENT": 3, "SPAN_KIND_INTERNAL": 1,
          "SPAN_KIND_PRODUCER": 4, "SPAN_KIND_CONSUMER": 5, "SPAN_KIND_UNSPECIFIED": 0}
_CODES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}


def _fields(value, key: str, kinds, default):
    """value[key] if present (its type must be one of `kinds`), else default."""
    check(type(value) is dict, "invalid OTLP/JSON traces")
    item = value.get(key, default)
    check(type(item) in (kinds if type(kinds) is tuple else (kinds,)), "invalid OTLP/JSON traces")
    return item


def _enum(value, names: dict) -> int:
    """An enum as its name or as an integer in the int64/uint64 range."""
    if type(value) is str:
        return names.get(value, 0)
    check(-2 ** 63 <= value < 2 ** 64, "invalid OTLP/JSON traces")
    return value


def _nanos(value) -> int:
    """A uint64 as a JSON integer or a decimal string."""
    if type(value) is str:
        check(re.fullmatch(r"[0-9]{1,20}", value) is not None, "invalid OTLP/JSON traces")
        value = int(value)
    check(type(value) is int and 0 <= value < 2 ** 64, "invalid OTLP/JSON traces")
    return value


def _json_id(span: dict, key: str, size: int) -> str:
    """OTLP/JSON IDs are hex strings; empty when absent, all zero, or the wrong size."""
    text = _fields(span, key, str, "")
    valid = re.fullmatch(f"[0-9A-Fa-f]{{{2 * size}}}", text) and text.strip("0")
    return text.lower() if valid else ""


def spans_from_json(body: bytes) -> list[tuple]:
    """OTLP/JSON (lowerCamelCase, 64-bit integers as strings)."""
    data = json_document(body, "OTLP")
    out = []
    for rs in _fields(data, "resourceSpans", list, []):
        service = ""
        for kv in _fields(_fields(rs, "resource", dict, {}), "attributes", list, []):
            if _fields(kv, "key", str, "") == "service.name":
                service = _fields(_fields(kv, "value", dict, {}), "stringValue", str, "")
        for ss in _fields(rs, "scopeSpans", list, []):
            for span in _fields(ss, "spans", list, []):
                kind = _enum(_fields(span, "kind", (str, int), 0), _KINDS)
                code = _enum(_fields(_fields(span, "status", dict, {}), "code", (str, int), 0), _CODES)
                start = _nanos(span.get("startTimeUnixNano", 0))
                end = _nanos(span.get("endTimeUnixNano", 0))
                out.append((service, kind, code, max(0, end - start) / 1e6, _json_id(span, "traceId", 16),
                            _json_id(span, "spanId", 8), _json_id(span, "parentSpanId", 8)))
    return out


def p95_index(n: int) -> int:
    """Index of the nearest-rank 95th percentile in n sorted values."""
    return max(0, math.ceil(0.95 * n) - 1)


def p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return ordered[p95_index(len(ordered))]


class SpanWindow:
    """Server spans per service over a sliding window, timed by arrival (not span clocks),
    and calls between services from client and server spans that pair up."""

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
        # Unmatched halves of calls, oldest first: (trace, span ID) of client spans and
        # (trace, parent ID) of server spans. Each waits one window for its other half.
        self.callers: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()
        self.callees: OrderedDict[tuple[str, str], tuple[float, str, bool]] = OrderedDict()
        self.calls: dict[tuple[str, str], list[int]] = {}
        self.calls_dropped = 0
        self.lock = Lock()

    def ingest(self, spans: list[tuple], arrived: float) -> int:
        count = 0
        with self.lock:
            for pending in (self.callers, self.callees):
                while pending and next(iter(pending.values()))[0] < arrived - self.window:
                    pending.popitem(last=False)
            for service, kind, code, duration, trace, span_id, parent in spans:
                failed = code == ERROR_CODE
                if trace and span_id and kind in CALLER_KINDS:
                    callee = self.callees.pop((trace, span_id), None)
                    if callee is not None:
                        self._pair(service, callee[1], callee[2])
                    elif (trace, span_id) not in self.callers:
                        self._wait(self.callers, (trace, span_id), (arrived, service))
                elif trace and parent and kind in CALLEE_KINDS:
                    caller = self.callers.pop((trace, parent), None)
                    if caller is not None:
                        self._pair(caller[1], service, failed)
                    elif (trace, parent) not in self.callees:
                        self._wait(self.callees, (trace, parent), (arrived, service, failed))
                if kind != SERVER_KIND:
                    continue
                if service not in self.services:
                    if len(self.unmapped) < 64 or service in self.unmapped:
                        self.unmapped[service] = self.unmapped.get(service, 0) + 1
                    continue
                queue = self.spans.setdefault(service, deque(maxlen=MAX_SPANS_PER_SERVICE))
                queue.append((arrived, failed, duration))
                count += 1
            self.accepted += count
        return count

    def _wait(self, pending: OrderedDict, key: tuple[str, str], half: tuple) -> None:
        if len(pending) >= MAX_PENDING_CALLS:
            self.calls_dropped += 1
        else:
            pending[key] = half

    def _pair(self, caller: str, callee: str, failed: bool) -> None:
        """One observed call. Calls within one service, or from an unnamed one, are not edges."""
        if caller == callee or not (SERVICE.fullmatch(caller) and SERVICE.fullmatch(callee)):
            return
        row = self.calls.get((caller, callee))
        if row is None:
            if len(self.calls) >= MAX_CALL_PAIRS:
                self.calls_dropped += 1
                return
            row = self.calls[(caller, callee)] = [0, 0]
        row[0] += 1
        row[1] += failed

    def call_counts(self) -> list[tuple[str, str, int, int]]:
        """(caller, callee, calls, errors) since start, sorted."""
        with self.lock:
            return [(a, b, n, e) for (a, b), (n, e) in sorted(self.calls.items())]

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


# ---------------------------------------------------------------- observed calls file

def read_calls(path: Path) -> list[dict]:
    """The rows of a calls file, checked strictly. The file is telemetry, not a decision."""
    document = json_document(control_room.safe_read(path, MAX_CALLS_BYTES), "calls file",
                             pairs=control_room.no_duplicates)
    check(type(document) is dict and set(document) == {"schema_version", "kind", "generated_at",
                                                        "calls", "semantics"}, "calls file: unexpected fields")
    check(document["schema_version"] == 1 and type(document["schema_version"]) is int
          and document["kind"] == CALLS_KIND, "unsupported calls file")
    check(type(document["generated_at"]) is str and STAMP.fullmatch(document["generated_at"]) is not None,
          "calls file: invalid generated_at")
    rows = document["calls"]
    check(type(rows) is list and len(rows) <= MAX_CALL_PAIRS, "calls file: too many rows")
    seen = set()
    for row in rows:
        check(type(row) is dict and set(row) == {"caller_service", "callee_service", "caller", "callee",
                                                 "calls", "errors", "first_seen", "last_seen"},
              "calls file: unexpected row fields")
        a, b = row["caller_service"], row["callee_service"]
        check(type(a) is str and type(b) is str and SERVICE.fullmatch(a) is not None
              and SERVICE.fullmatch(b) is not None and a != b and (a, b) not in seen,
              "calls file: invalid or repeated service pair")
        seen.add((a, b))
        for key in ("caller", "callee"):
            check(row[key] is None or (type(row[key]) is str and IDENT.fullmatch(row[key]) is not None),
                  "calls file: invalid component")
        n, e = row["calls"], row["errors"]
        check(type(n) is int and type(e) is int and 0 < n <= MAX_COUNT and 0 <= e <= n,
              "calls file: invalid counts")
        check(all(type(row[k]) is str and STAMP.fullmatch(row[k]) is not None for k in ("first_seen", "last_seen"))
              and row["first_seen"] <= row["last_seen"], "calls file: invalid times")
    return rows


def merge_calls(totals: dict, seen: dict, current: list, moment: datetime) -> None:
    """Add what the window counted since the last merge to the running totals, in place.

    `totals` maps (caller, callee) to {"calls", "errors", "first_seen", "last_seen"};
    `seen` maps it to the window's (calls, errors) at the last merge.
    """
    now = stamp(moment)
    for caller, callee, calls, errors in current:
        before = seen.get((caller, callee), (0, 0))
        seen[(caller, callee)] = (calls, errors)
        if calls <= before[0]:
            continue
        row = totals.get((caller, callee))
        if row is None:
            if len(totals) >= MAX_CALL_PAIRS:
                continue
            row = totals[(caller, callee)] = {"calls": 0, "errors": 0, "first_seen": now, "last_seen": now}
        row["calls"] = min(MAX_COUNT, row["calls"] + calls - before[0])
        row["errors"] = min(row["calls"], row["errors"] + max(0, errors - before[1]))
        row["last_seen"] = now


def calls_document(totals: dict, services: dict, moment: datetime) -> dict:
    return {"schema_version": 1, "kind": CALLS_KIND, "generated_at": stamp(moment),
            "calls": [{"caller_service": a, "callee_service": b, "caller": services.get(a),
                       "callee": services.get(b), **row} for (a, b), row in sorted(totals.items())],
            "semantics": "OBSERVED_IN_TRACES_NOT_CONFIRMED_DEPENDENCIES"}


def decode_request(body: bytes, content_type: str, content_encoding: str) -> tuple[int, list]:
    """(200 or 201 for protobuf, spans), or (400 or 415, []): what the receiver does with a body."""
    mime = content_type.split(";")[0].strip()
    try:
        encoding = content_encoding.strip().lower()
        if encoding == "gzip":
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = inflater.decompress(body, MAX_OTLP_DECOMPRESSED)
            check(not inflater.unconsumed_tail, "decompressed body too large")
            check(inflater.eof, "truncated gzip body")
        elif encoding not in ("", "identity"):
            return 415, []
        if mime == "application/x-protobuf":
            return 201, spans_from_protobuf(body)
        if mime == "application/json":
            return 200, spans_from_json(body)
        return 415, []
    except (FeedError, zlib.error, RecursionError):
        return 400, []


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
            if token and not hmac.compare_digest(self.headers.get("Authorization", "").encode("latin-1"),
                                                 f"Bearer {token}".encode("utf-8")):
                return self.reply(401, b'{"error":"bearer token required"}')
            declared = self.headers.get("Content-Length", "")
            length = int(declared) if declared.isascii() and declared.isdigit() else -1
            if not 0 <= length <= MAX_OTLP_BODY:
                return self.reply(413, b'{"error":"body too large or unknown length"}')
            body = self.rfile.read(length)
            code, spans = decode_request(body, self.headers.get("Content-Type", ""),
                                         self.headers.get("Content-Encoding", ""))
            if code == 415:
                return self.reply(415, b'{"error":"use gzip or no compression, and application/x-protobuf '
                                       b'or application/json"}')
            if code == 400:
                return self.reply(400, b'{"error":"invalid OTLP traces"}')
            window.ingest(spans, time.monotonic())
            if code == 201:
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
        self.calls_path = output_path(config).with_suffix(".calls.json")
        self.status: dict[str, dict] = {}
        self.window = SpanWindow(config["otlp"]) if "otlp" in config else None
        # Calls counted before a restart stay; an unreadable file is refused, not overwritten.
        self.call_totals = ({(r["caller_service"], r["callee_service"]): {
            k: r[k] for k in ("calls", "errors", "first_seen", "last_seen")} for r in read_calls(self.calls_path)}
            if self.window and self.calls_path.exists() else {})
        self.calls_seen: dict = {}
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
            self.write_calls()
        self.write_status()
        return written

    def write_calls(self) -> None:
        moment = now_utc()
        merge_calls(self.call_totals, self.calls_seen, self.window.call_counts(), moment)
        document = calls_document(self.call_totals, self.config["otlp"]["services"], moment)
        with tempfile.NamedTemporaryFile("w", dir=self.calls_path.parent, prefix=".feed-calls-",
                                         delete=False, encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, indent=2)
        Path(handle.name).replace(self.calls_path)

    def write_status(self) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
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
