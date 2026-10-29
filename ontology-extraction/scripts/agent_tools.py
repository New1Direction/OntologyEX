#!/usr/bin/env python3
"""Structured, permission-aware tools for agents over the same model the control room shows.

    python agent_tools.py serve --control-room http://127.0.0.1:8791 \
        --agent claude --operator "Dana" --audit REPO/.ontologyex/agent-audit.jsonl
    python agent_tools.py call get_entity_context '{"id": "payments-domain:object:Payment"}' \
        --bundle BUNDLE.json --agent claude --operator "Dana" --audit audit.jsonl
    python agent_tools.py tools

`serve` speaks the Model Context Protocol over stdio (JSON-RPC 2.0, one message per
line), so any MCP client can register it. `call` runs one tool and prints the result,
for agents without MCP and for scripts.

The model comes from a running control room (`/api/bundle`, loopback only) or from a
bundle file (`control_room.py ... --bundle-only`). Answers come from entity_context.py,
the same code behind the entity page; the Rust engine gives the same answers.

Every tool declares its kind, and the kind decides what it may do:

- READ_ONLY tools (search, context, explain, impact, recent decisions, open
  suggestions, decisions whose evidence changed) change nothing.
- ADDITIVE tools (propose_link) append a proposal to a proposals file and nothing
  else. A proposal is a suggestion: it shows in the control room beside the ranked
  ones, and only a person confirms or rejects it there, through the existing decision
  route and its history. A rejected link stays rejected whoever proposes it again.
- There are no destructive tools, and the server refuses to register one.

An access policy (JSON) lists the tools an agent may call and the files and entities it
may not see. Without one, the read tools are allowed and propose_link is not. Files
that commonly hold secrets (.env, keys, credentials) are always withheld. Refusals say
what to change.

Every call, allowed or refused, is appended to an audit file before the answer is
returned: who acted (the agent), for whom (the operator, as stated, not verified),
which tool, a digest of the arguments, the entity IDs named, and the outcome. If the
audit cannot be written, the call is refused: there is no unaudited access.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import sys
from urllib.parse import urlsplit
import urllib.request

import entity_context
import link_suggest

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER = {"name": "ontologyex", "title": "OntologyEX", "version": "1"}
MAX_LINE = 1024 * 1024
MAX_BUNDLE = 64 * 1024 * 1024
READ_ONLY, ADDITIVE, DESTRUCTIVE = "READ_ONLY", "ADDITIVE", "DESTRUCTIVE"
ALWAYS_WITHHELD = (".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
                   "*credentials*", "*secret*", ".npmrc", ".pypirc", ".netrc", "*.kdbx")
WITHHELD = "[withheld by access policy]"


class ToolError(ValueError):
    """A refusal or invalid call. `status` is what the audit and the agent see."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def _text(name: str, description: str, n: int = 1024) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": n, "description": description}


def _schema(required: dict, optional: dict | None = None) -> dict:
    return {"type": "object", "properties": {**required, **(optional or {})}, "required": sorted(required),
            "additionalProperties": False}


ENTITY_ID = _text("id", "an entity ID from search_entities (business object or action, or system component)")
TOOLS = {
    "search_entities": {
        "kind": READ_ONLY, "title": "Search entities",
        "description": "Find business objects, actions and system components whose name, ID or summary has "
                       "every word of the query. Start here to get entity IDs.",
        "input": _schema({"query": _text("query", "words to look for", 200)})},
    "get_entity_context": {
        "kind": READ_ONLY, "title": "Entity context",
        "description": "Everything known about one entity: why it exists, its relationships in both directions "
                       "with standing (CONFIRMED by a person, SOURCE-declared, INFERRED, UNKNOWN), who confirmed "
                       "each and when, what relies on it, open suggestions, unknowns, recent decisions, and the "
                       "source files (path and sha256) behind it.",
        "input": _schema({"id": ENTITY_ID})},
    "explain_relationship": {
        "kind": READ_ONLY, "title": "Explain a relationship",
        "description": "Why two entities are connected: every path of up to three hops, strongest first. A path "
                       "is as strong as its weakest hop; each hop names its basis, decision and evidence.",
        "input": _schema({"from": ENTITY_ID, "to": ENTITY_ID})},
    "analyze_impact": {
        "kind": READ_ONLY, "title": "What could break",
        "description": "What relies on an entity, up to four hops, with the path and the weakest standing along "
                       "it. Declared reachability, not a measured blast radius.",
        "input": _schema({"id": ENTITY_ID})},
    "recent_changes": {
        "kind": READ_ONLY, "title": "Recent decisions",
        "description": "The newest recorded link and dependency decisions (who, when, note), for one entity or "
                       "for everything.",
        "input": _schema({}, {"id": ENTITY_ID})},
    "list_suggestions": {
        "kind": READ_ONLY, "title": "Open suggestions",
        "description": "Proposals nobody has decided yet (ranked links, calls seen in traces, agent proposals), "
                       "with strength, signals and who proposed them; for one entity or for everything.",
        "input": _schema({}, {"id": ENTITY_ID})},
    "list_stale_decisions": {
        "kind": READ_ONLY, "title": "Decisions to re-check",
        "description": "Confirmed links whose evidence (the file hashes recorded when a person confirmed them) "
                       "changed since. They read as INFERRED until a person re-confirms or undoes them; name the "
                       "changed files when you report them.",
        "input": _schema({})},
    "propose_link": {
        "kind": ADDITIVE, "title": "Propose a link",
        "description": "Propose that a domain-skill binding runs on a system component, with your reason. This "
                       "only adds a suggestion: a person confirms or rejects it in the control room. Links a "
                       "person rejected cannot be proposed again.",
        "input": _schema({"skill": _text("skill", "the domain skill name", 200),
                          "binding": _text("binding", "the binding ID (an entity context names it in 'via')", 200),
                          "component": _text("component", "the system component's entity ID"),
                          "reason": _text("reason", "why, in a sentence or two, citing files where you can", 500)})},
}
for _name, _tool in TOOLS.items():  # every tool declares a kind, and none may destroy
    if _tool["kind"] not in (READ_ONLY, ADDITIVE):
        raise SystemExit(f"tool {_name} must be READ_ONLY or ADDITIVE")


def annotations(name: str) -> dict:
    kind = TOOLS[name]["kind"]
    return {"title": TOOLS[name]["title"], "readOnlyHint": kind == READ_ONLY, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False}


# ---------------------------------------------------------------- policy

def load_policy(path: Path | None) -> dict:
    """The access policy. Unknown keys or tools fail closed at startup."""
    if path is None:
        return {"allow_tools": sorted(n for n, t in TOOLS.items() if t["kind"] == READ_ONLY),
                "deny_paths": list(ALWAYS_WITHHELD), "deny_entities": [], "source": "default (read tools only)"}
    data = json.loads(path.read_text("utf-8"))
    if type(data) is not dict or not set(data) <= {"schema_version", "allow_tools", "deny_paths", "deny_entities"} \
            or data.get("schema_version") != 1:
        raise ToolError("INVALID_POLICY", "policy needs schema_version 1 and only allow_tools, deny_paths, "
                                          "deny_entities")
    allow = data.get("allow_tools", [])
    for key in ("allow_tools", "deny_paths", "deny_entities"):
        value = data.get(key, [])
        if type(value) is not list or len(value) > 500 or not all(type(v) is str and 0 < len(v) <= 300 for v in value):
            raise ToolError("INVALID_POLICY", f"{key} must be a list of strings")
    unknown = sorted(set(allow) - set(TOOLS))
    if unknown:
        raise ToolError("INVALID_POLICY", "unknown tools in allow_tools: " + ", ".join(unknown))
    return {"allow_tools": sorted(set(allow)), "deny_paths": [*ALWAYS_WITHHELD, *data.get("deny_paths", [])],
            "deny_entities": list(data.get("deny_entities", [])), "source": str(path)}


def path_withheld(policy: dict, path) -> bool:
    if type(path) is not str or not path:
        return False
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(name, g) for g in policy["deny_paths"])


def entity_withheld(policy: dict, entity_id) -> bool:
    return type(entity_id) is str and any(fnmatch.fnmatchcase(entity_id, g) for g in policy["deny_entities"])


def redact(policy: dict, value, hidden: set[str]):
    """Withhold denied files and entities everywhere in an answer, including inside text."""
    if isinstance(value, dict):
        if "path" in value and path_withheld(policy, value["path"]):
            return {"withheld": True, "reason": WITHHELD}
        out = {k: redact(policy, v, hidden) for k, v in value.items()}
        if isinstance(value.get("changed"), list):  # file paths a decision's evidence check names
            out["changed"] = [WITHHELD if path_withheld(policy, p) else redact(policy, p, hidden)
                              for p in value["changed"]]
        if value.get("id") in hidden and "label" in value:
            out.update({"id": WITHHELD, "label": WITHHELD})
        return out
    if isinstance(value, list):
        return [redact(policy, v, hidden) for v in value]
    if isinstance(value, str):
        for entity_id in hidden:
            if entity_id and entity_id in value:
                value = value.replace(entity_id, WITHHELD)
        return value
    return value


# ---------------------------------------------------------------- sources

class Source:
    """Where the model comes from: a loopback control room, or a bundle file. Read fresh per call."""

    def __init__(self, control_room: str | None, bundle: Path | None):
        if (control_room is None) == (bundle is None):
            raise ToolError("INVALID_SOURCE", "give exactly one of --control-room URL or --bundle FILE")
        if control_room is not None:
            parts = urlsplit(control_room)
            if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1") \
                    or parts.path not in ("", "/") or parts.query or parts.username:
                raise ToolError("INVALID_SOURCE", "the control room must be a loopback http://127.0.0.1:PORT URL")
            self.url = f"http://{parts.netloc}/api/bundle"
        self.control_room, self.bundle_path = control_room, bundle
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback never goes via a proxy

    def bundle(self) -> dict:
        try:
            if self.bundle_path is not None:
                if self.bundle_path.stat().st_size > MAX_BUNDLE:
                    raise ToolError("SOURCE_UNAVAILABLE", "the bundle file is too large")
                data = json.loads(self.bundle_path.read_text("utf-8"))
            else:
                with self.opener.open(self.url, timeout=30) as response:
                    data = json.loads(response.read(MAX_BUNDLE + 1)[:MAX_BUNDLE])
        except (OSError, ValueError) as exc:
            raise ToolError("SOURCE_UNAVAILABLE", "could not read the model (is the control room running?): "
                            + str(exc)[:200]) from exc
        if type(data) is not dict or type(data.get("state")) is not dict \
                or data["state"].get("execution_authorized") is not False:
            raise ToolError("SOURCE_UNAVAILABLE", "the source is not a context bundle that authorizes nothing")
        return data


# ---------------------------------------------------------------- audit

class Audit:
    """Append-only record of every call. A failed write refuses the call."""

    def __init__(self, path: Path, agent: str, operator: str):
        self.path, self.agent, self.operator = path, agent, operator
        self.session = secrets.token_hex(8)
        self.ordinal = 0
        if path.is_symlink():
            raise ToolError("INVALID_AUDIT", "the audit path must not be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, tool: str, kind: str, args: dict, status: str, entity_ids: list[str], detail: str = "") -> str:
        self.ordinal += 1
        record = {"id": f"{self.session}-{self.ordinal:06d}",
                  "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "actor": f"agent:{self.agent}", "on_behalf_of": self.operator,
                  "on_behalf_of_verified": False, "tool": tool, "kind": kind,
                  "args_sha256": hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest(),
                  "entity_ids": entity_ids[:10], "status": status, "detail": detail[:300]}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record["id"]


# ---------------------------------------------------------------- tools

def check_args(name: str, args) -> dict:
    schema = TOOLS[name]["input"]
    if type(args) is not dict:
        raise ToolError("INVALID_ARGUMENTS", "arguments must be an object")
    extra = sorted(set(args) - set(schema["properties"]))
    missing = sorted(set(schema["required"]) - set(args))
    if extra or missing:
        raise ToolError("INVALID_ARGUMENTS", f"{name} takes {', '.join(sorted(schema['properties'])) or 'nothing'}"
                        + (f"; unexpected {', '.join(extra)}" if extra else "")
                        + (f"; missing {', '.join(missing)}" if missing else ""))
    for key, value in args.items():
        spec = schema["properties"][key]
        if type(value) is not str or not spec["minLength"] <= len(value) <= spec["maxLength"] \
                or not all(" " <= ch != "\x7f" for ch in value):
            raise ToolError("INVALID_ARGUMENTS", f"{key} must be text of 1..{spec['maxLength']} characters "
                                                 "without control characters")
    return args


class Tools:
    def __init__(self, source: Source, policy: dict, audit: Audit, proposals: Path | None):
        self.source, self.policy, self.audit, self.proposals = source, policy, audit, proposals

    def call(self, name: str, args) -> dict:
        """Run one tool under the policy; audit it whatever happens."""
        kind = TOOLS[name]["kind"] if name in TOOLS else "UNKNOWN"
        named = [v for k, v in (args.items() if type(args) is dict else []) if k in ("id", "from", "to", "component")
                 and type(v) is str][:10]
        try:
            if name not in TOOLS:
                raise ToolError("UNKNOWN_TOOL", f"no tool named {name}; call tools/list")
            if name not in self.policy["allow_tools"]:
                raise ToolError("NOT_ALLOWED", f"{name} is not allowed by the access policy ({self.policy['source']}). "
                                + ("To allow it, pass --policy with a file whose allow_tools includes propose_link, "
                                   "and --proposals FILE; a person still confirms each proposal in the control room."
                                   if kind == ADDITIVE else "Add it to allow_tools in the policy file."))
            args = check_args(name, args)
            result = getattr(self, name)(args)
        except ToolError as exc:
            self.audit.write(name, kind, args if type(args) is dict else {}, exc.status, named, str(exc))
            raise
        except entity_context.ContextError as exc:
            status = "NOT_FOUND" if "not found" in str(exc) else "INVALID_ARGUMENTS"
            self.audit.write(name, kind, args, status, named, str(exc))
            raise ToolError(status, str(exc) + ("; use search_entities to find IDs" if status == "NOT_FOUND" else ""))
        self.audit.write(name, kind, args, "OK", named)
        return result

    def _model(self, *ids: str) -> tuple[dict, set[str]]:
        bundle = self.source.bundle()
        hidden = {i for i in entity_context.entity_ids(bundle) if entity_withheld(self.policy, i)}
        for entity_id in ids:
            if entity_id in hidden:
                raise ToolError("NOT_ALLOWED", "that entity is withheld by the access policy (deny_entities)")
        return bundle, hidden

    def _out(self, value: dict, hidden: set[str]) -> dict:
        return redact(self.policy, value, hidden)

    def search_entities(self, args: dict) -> dict:
        bundle, hidden = self._model()
        found = entity_context.search(bundle, args["query"])
        found["results"] = [r for r in found["results"] if r["id"] not in hidden]
        return self._out(found, hidden)

    def get_entity_context(self, args: dict) -> dict:
        bundle, hidden = self._model(args["id"])
        return self._out(entity_context.context(bundle, args["id"]), hidden)

    def explain_relationship(self, args: dict) -> dict:
        bundle, hidden = self._model(args["from"], args["to"])
        return self._out(entity_context.explain(bundle, args["from"], args["to"]), hidden)

    def analyze_impact(self, args: dict) -> dict:
        bundle, hidden = self._model(args["id"])
        found = entity_context.context(bundle, args["id"])
        return self._out({"schema_version": 1, "kind": "IMPACT", "entity": found["entity"],
                          "relied_on_by": found["relied_on_by"], "unknowns": found["unknowns"],
                          "semantics": found["impact_semantics"], "execution_authorized": False}, hidden)

    def recent_changes(self, args: dict) -> dict:
        bundle, hidden = self._model(*args.values())
        if "id" in args:
            found = entity_context.context(bundle, args["id"])
            return self._out({"schema_version": 1, "kind": "RECENT_DECISIONS", "entity": found["entity"],
                              "changes": found["recent_changes"], "execution_authorized": False}, hidden)
        return self._out(entity_context.recent(bundle), hidden)

    def list_suggestions(self, args: dict) -> dict:
        bundle, hidden = self._model(*args.values())
        found = entity_context.open_suggestions(bundle)
        if "id" in args:
            if args["id"] not in entity_context.entity_ids(bundle):
                raise entity_context.ContextError("entity not found")
            found["suggestions"] = [s for s in found["suggestions"]
                                    if args["id"] in (s["source"]["id"], s["target"]["id"])]
            found["total"] = len(found["suggestions"])
        return self._out(found, hidden)

    def list_stale_decisions(self, args: dict) -> dict:
        bundle, hidden = self._model()
        return self._out(entity_context.stale_decisions(bundle), hidden)

    def propose_link(self, args: dict) -> dict:
        if self.proposals is None:
            raise ToolError("NOT_ALLOWED", "proposals are off: start agent_tools with --proposals FILE and the "
                                           "control room with the same --proposals FILE")
        bundle, hidden = self._model(args["component"])
        picture = bundle.get("business") or {}
        key = (args["skill"], args["binding"], args["component"])
        bindings = {(row["skill"], b["id"] if isinstance(b, dict) else b)
                    for row in [*picture.get("actions", []), *picture.get("objects", [])]
                    for b in row.get("bindings", [])}
        if key[:2] not in bindings:
            raise ToolError("NOT_FOUND", f"no binding {args['binding']} in skill {args['skill']}; an entity "
                                         "context names bindings in 'via' (skill:binding:ID)")
        nodes = {n["id"]: n for n in bundle["state"]["nodes"]}
        if key[2] not in nodes or nodes[key[2]]["kind"] not in link_suggest.KINDS:
            raise ToolError("NOT_FOUND", "component must be a discovered system component of kind "
                                         + ", ".join(link_suggest.KINDS))
        for row in picture.get("link_decided", []):
            if (row.get("skill"), row.get("binding"), row.get("component")) == key:
                if row.get("decision") in ("confirm", "confirmed"):
                    raise ToolError("ALREADY_DECIDED", "a person already confirmed this link")
                raise ToolError("REJECTED_BY_PERSON", f"{row.get('declared_by') or 'a person'} rejected this link; "
                                "rejections are sticky. Ask the operator to undo the rejection in the control "
                                "room if the evidence changed.")
        proposal_id = link_suggest.suggestion_id(*key)
        mine = f"agent:{self.audit.agent}"
        if any(p["id"] == proposal_id and p["proposed_by"] == mine
               for p in link_suggest.read_proposals(self.proposals)):
            return {"status": "ALREADY_PROPOSED", "id": proposal_id, "execution_authorized": False}
        link_suggest.append_proposal(self.proposals, {
            "kind": link_suggest.PROPOSAL_KIND, "id": proposal_id,
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "proposed_by": mine,
            "on_behalf_of": self.audit.operator, "skill": key[0], "binding": key[1], "component": key[2],
            "reason": args["reason"]})
        return {"status": "PROPOSED", "id": proposal_id, "applied": False,
                "next": "A person confirms or rejects it in the control room (Business page or the entity page). "
                        "Nothing changes until then.", "execution_authorized": False}


# ---------------------------------------------------------------- MCP over stdio

def handle(tools: Tools, message) -> dict | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    if type(message) is not dict or message.get("jsonrpc") != "2.0" or type(message.get("method")) is not str:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    method, params, msg_id = message["method"], message.get("params") or {}, message.get("id")
    if "id" not in message:
        return None  # notifications (initialized, cancelled) need no answer
    def ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if method == "initialize":
        asked = params.get("protocolVersion") if type(params) is dict else None
        return ok({"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                   "instructions": "Read tools answer from the operator's local OntologyEX model. Each "
                                   "relationship has a standing: CONFIRMED (a person decided), SOURCE (a file "
                                   "declares it), INFERRED (proposed or reached, not decided) or UNKNOWN. Say "
                                   "which when you answer. propose_link only suggests; a person decides."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "title": t["title"], "description": t["description"],
                              "inputSchema": t["input"], "annotations": annotations(n)}
                             for n, t in TOOLS.items() if n in tools.policy["allow_tools"]]})
    if method == "tools/call":
        if type(params) is not dict or type(params.get("name")) is not str:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "tools/call needs a name"}}
        try:
            result = tools.call(params["name"], params.get("arguments", {}))
            return ok({"content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}],
                       "structuredContent": result, "isError": False})
        except ToolError as exc:
            refusal = {"status": exc.status, "error": str(exc)[:600], "execution_authorized": False}
            return ok({"content": [{"type": "text", "text": json.dumps(refusal, sort_keys=True)}],
                       "structuredContent": refusal, "isError": True})
        except OSError as exc:  # the audit could not be written: refuse rather than answer unaudited
            return ok({"content": [{"type": "text", "text": "Refused: the audit record could not be written ("
                                    + str(exc)[:200] + ")."}], "isError": True})
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(tools: Tools, stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        if len(line) > MAX_LINE:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "message too large"}}
        else:
            try:
                response = handle(tools, json.loads(line))
            except ValueError:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if response is not None:
            stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            stdout.flush()


def build(args) -> Tools:
    for name, value in (("--agent", args.agent), ("--operator", args.operator)):
        if not 0 < len(value) <= 120 or not all(" " <= ch != "\x7f" for ch in value):
            raise ToolError("INVALID_ARGUMENTS", f"{name} must be 1..120 printable characters")
    return Tools(Source(args.control_room, args.bundle), load_policy(args.policy),
                 Audit(args.audit, args.agent, args.operator), args.proposals)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("tools", help="list the tools, their kinds and inputs")
    for name in ("serve", "call"):
        p = commands.add_parser(name)
        if name == "call":
            p.add_argument("tool")
            p.add_argument("arguments", nargs="?", default="{}", help="the tool's arguments as a JSON object")
        p.add_argument("--control-room", help="a running control room, e.g. http://127.0.0.1:8791")
        p.add_argument("--bundle", type=Path, help="a bundle file from control_room.py --bundle-only")
        p.add_argument("--agent", required=True, help="the agent's name, recorded as the actor")
        p.add_argument("--operator", required=True, help="the person the agent acts for (stated, not verified)")
        p.add_argument("--audit", type=Path, required=True, help="append-only audit file (JSON lines)")
        p.add_argument("--policy", type=Path, help="access policy (JSON); default: read tools only")
        p.add_argument("--proposals", type=Path, help="where propose_link appends (give the control room the same file)")
    args = parser.parse_args()
    if args.command == "tools":
        print(json.dumps([{"name": n, "kind": t["kind"], "annotations": annotations(n), "input": t["input"]}
                          for n, t in TOOLS.items()], indent=2))
        return 0
    try:
        tools = build(args)
        if args.command == "serve":
            serve(tools)
            return 0
        result = tools.call(args.tool, json.loads(args.arguments))
    except ToolError as exc:
        print(json.dumps({"status": exc.status, "error": str(exc)[:600]}))
        return 2
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)[:300]}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
