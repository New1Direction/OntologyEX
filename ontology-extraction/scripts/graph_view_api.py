"""Read-only bounded graph projection for the local OntologyEX control room.

The server holds the full source-backed graph; browsers request small, searchable
views. Every response is explicit about omitted nodes/edges, missing coverage,
and UNKNOWN runtime health. This is not a tenant auth boundary or a SaaS API.
"""
from __future__ import annotations

import re

MAX_PAGE = 90
MAX_QUERY = 90
MAX_EDGES = 300
MAX_DETAIL_EDGES = 40
MAX_ALERTS = 18
MAX_GAPS = 24
MAX_OFFSET = 10000

ALLOWED_LENSES = frozenset({"all", "api", "database", "services"})
KIND_LENSES = {
    "api": frozenset({"project", "api_route", "source_module"}),
    "database": frozenset({"project", "declared_db_table", "source_module"}),
    "services": frozenset({"project", "declared_service"}),
}
SEVERITY = {"error": 0, "warning": 1, "stale": 2, "unknown": 3, "ok": 4}
ASCII_SEARCH = re.compile(r"[\x20-\x7e]{0,90}\Z")


class ViewError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ViewError(message)


def valid_query(q):
    require(type(q) is str and len(q) <= MAX_QUERY and
            ASCII_SEARCH.fullmatch(q) is not None, "invalid search query")
    return q.lower().strip()


def valid_lens(lens):
    require(lens in ALLOWED_LENSES, "unsupported graph lens")
    return lens


def parse_offset(raw):
    require(type(raw) is str and re.fullmatch(r"(?:0|[1-9][0-9]{0,4})", raw) is not None,
            "invalid pagination offset")
    value = int(raw)
    require(value <= MAX_OFFSET, "offset exceeds bounded search budget")
    return value


def priority(node):
    risk = node.get("potentially_impacted_by") or []
    return (SEVERITY.get(node.get("status"), 5),
            0 if risk else 1, node.get("label", "").lower(), node.get("id", ""))


def in_lens(node, lens):
    return lens == "all" or node.get("kind") in KIND_LENSES[lens]


def summary(full):
    """Control-room bootstrap without complete topology or source inventory."""
    nodes = full["nodes"]
    active = [n for n in nodes if n.get("status") in ("error", "warning") or
              n.get("potentially_impacted_by")]
    active.sort(key=priority)
    # Only the incident list is pre-materialized, without raw source/evidence.
    alerts = [{
        "id": n["id"], "label": n["label"], "status": n["status"],
        "possible_impact": bool(n.get("potentially_impacted_by")),
    } for n in active[:MAX_ALERTS]]
    return {
        "schema_version": 1,
        "kind": "LOCAL_CONTROL_ROOM_SUMMARY",
        "mode": full["mode"],
        "generated_at": full["generated_at"],
        "project": full["project"],
        "coverage": full["coverage"],
        "total_nodes": len(nodes),
        "total_edges": len(full["edges"]),
        "alerts": alerts,
        "total_alerts": len(active),
        "gaps": full["gaps"][:MAX_GAPS],
        "total_gaps": len(full["gaps"]),
        "unresolved_count": len(full["unresolved"]),
        "discovery_source": full.get("discovery_source", {"provider": "local"}),
        "discovery_freshness": full.get("discovery_freshness", "UNKNOWN"),
        "discovery_scanned_at": full.get("discovery_scanned_at"),
        "durability": full.get("durability", "MEMORY_ONLY"),
        "snapshot_history": full.get("snapshot_history", [])[:8],
        "runtime_discovery": full["runtime_discovery"],
        "execution_authorized": False,
    }


def page(full, *, lens="all", query="", offset=0, selected=None):
    """Return only visible searchable nodes, internal edges and cited evidence."""
    lens = valid_lens(lens)
    q = valid_query(query)
    require(type(offset) is int and 0 <= offset <= MAX_OFFSET, "invalid offset")
    require(selected is None or
            (type(selected) is str and 0 < len(selected) <= 1024),
            "invalid selected ID")
    candidates = [n for n in full["nodes"] if in_lens(n, lens) and
                  (not q or q in n["id"].lower() or q in n["label"].lower())]
    candidates.sort(key=lambda n: (0 if selected and n["id"] == selected else 1,
                                   *priority(n)))
    total = len(candidates)
    subset = candidates[offset:offset + MAX_PAGE]
    ids = {n["id"] for n in subset}
    related = [e for e in full["edges"] if e["source"] in ids and e["target"] in ids]
    related.sort(key=lambda e:(e["source"], e["target"], e["relation"],
                               e.get("source_line", 0)))
    edges = related[:MAX_EDGES]
    evidence_keys = {n.get("evidence_id") for n in subset} | {
        e.get("evidence_id") for e in edges}
    ev = [record for record in full["source_evidence"] if record["id"] in evidence_keys]
    nxt = offset + len(subset)
    return {
        "schema_version": 1, "kind": "BOUNDED_GRAPH_PAGE",
        "summary": summary(full),
        "lens": lens, "query": query, "offset": offset,
        "nodes": subset, "edges": edges, "source_evidence": ev,
        "visible_count": len(subset), "total_matches": total,
        "total_nodes": len(full["nodes"]),
        "hidden_matches": max(0, total - len(subset)),
        "next_offset": nxt if nxt < total and nxt <= MAX_OFFSET else None,
        "edge_count_in_page": len(related),
        "edges_omitted": max(0, len(related) - len(edges)),
        "coverage_semantics": "SERVER_SIDE_SOURCE_GRAPH_SLICE_NOT_FULL_BUSINESS",
        "runtime_discovery": full["runtime_discovery"],
        "execution_authorized": False,
    }


def node_detail(full, node_id):
    require(type(node_id) is str and 0 < len(node_id) <= 1024,
            "invalid detail identity")
    node = next((n for n in full["nodes"] if n["id"] == node_id), None)
    if node is None:
        raise ViewError("node not found")
    relevant = [e for e in full["edges"]
                if e["source"] == node_id or e["target"] == node_id]
    relevant.sort(key=lambda e:(e["relation"], e["source"], e["target"],
                                e.get("source_line", 0)))
    limited = relevant[:MAX_DETAIL_EDGES]
    ev_ids = {node.get("evidence_id")} | {e.get("evidence_id") for e in limited}
    return {
        "schema_version": 1, "kind": "BOUNDED_NODE_DETAIL",
        "node": node, "edges": limited, "total_related_edges": len(relevant),
        "edges_omitted": max(0,len(relevant)-len(limited)),
        "source_evidence": [e for e in full["source_evidence"]
                            if e["id"] in ev_ids],
        "impact_semantics": full["impact_semantics"],
        "execution_authorized": False,
    }


# Relations are source-declared links. Neither a static import nor a Compose
# dependency is proof that a runtime call, database query or outage occurred.
MAX_NEIGHBORHOOD_DEPTH = 2
MAX_NEIGHBORHOOD_NODES = MAX_PAGE
RELATION_ORDER = {
    "DEPENDS_ON": 0, "STATIC_IMPORTS": 1,
    "CALLS": 2, "READS_FROM": 3, "WRITES_TO": 4,
    "DECLARES_TABLE_CANDIDATE": 5, "DECLARES": 6,
}


def valid_depth(raw):
    require(type(raw) is str and raw in ("1", "2"),
            "neighborhood depth must be 1 or 2")
    return int(raw)


def neighborhood(full, node_id, *, depth=1):
    """One/two-hop bounded undirected *navigation* over directed cited edges.

    Every edge retains the original source->target direction and relation,
    because walking an edge in reverse is not a causal dependency. Navigation
    is not a blast-radius calculation or confirmation of runtime behavior.
    """
    require(type(node_id) is str and 0 < len(node_id) <= 1024,
            "invalid neighborhood node identity")
    require(type(depth) is int and 1 <= depth <= MAX_NEIGHBORHOOD_DEPTH,
            "unsupported neighborhood traversal depth")
    by_id = {node["id"]: node for node in full["nodes"]}
    require(node_id in by_id, "neighborhood node not found")
    from collections import defaultdict, deque
    adj = defaultdict(list)
    for e in full["edges"]:
        a, b = e["source"], e["target"]
        # Corrupt/dangling graph references never lead to fabricated nodes.
        if a not in by_id or b not in by_id:
            continue
        adj[a].append((b, e))
        if b != a:
            adj[b].append((a, e))
    def edge_rank(edge):
        return (RELATION_ORDER.get(edge["relation"], 7),
                edge["relation"], edge["source"], edge["target"],
                edge.get("source_line", 0), edge.get("evidence_id", ""))
    visited = {node_id}
    queue = deque([(node_id, 0)])
    clipped = set()
    explored = 0
    max_seen_depth = 0
    while queue:
        current, dist = queue.popleft()
        if dist >= depth:
            continue
        explored += 1
        choices = sorted(adj.get(current, []), key=lambda item: (
            # Put actual reported failures and warnings first, then
            # the more descriptive static/declared relationships.
            priority(by_id[item[0]])[0], edge_rank(item[1]), item[0]))
        for other, edge in choices:
            if other in visited:
                continue
            if len(visited) >= MAX_NEIGHBORHOOD_NODES:
                clipped.add(other)
                continue
            visited.add(other)
            queue.append((other, dist + 1))
            max_seen_depth = max(max_seen_depth, dist + 1)
    ordered_nodes = [by_id[node_id]] + sorted(
        (by_id[key] for key in visited if key != node_id),
        key=lambda n: (priority(n), n["id"]))
    internal = sorted((e for e in full["edges"]
                       if e["source"] in visited and e["target"] in visited),
                      key=edge_rank)
    edges = internal[:MAX_EDGES]
    evidence_keys = {node.get("evidence_id") for node in ordered_nodes} | {
        e.get("evidence_id") for e in edges}
    evidence = [item for item in full["source_evidence"]
                if item["id"] in evidence_keys]
    direct_out = sum(e["source"] == node_id for e in full["edges"])
    direct_in = sum(e["target"] == node_id for e in full["edges"])
    return {
        "schema_version": 1,
        "kind": "BOUNDED_GRAPH_NEIGHBORHOOD",
        "summary": summary(full),
        "center_id": node_id,
        "requested_depth": depth,
        "depth_reached": max_seen_depth,
        "nodes": ordered_nodes,
        "edges": edges,
        "source_evidence": evidence,
        "visible_count": len(ordered_nodes),
        "total_matches": len(ordered_nodes),
        "total_nodes": len(full["nodes"]),
        "offset": 0,
        "next_offset": None,
        "direct_incoming_edges": direct_in,
        "direct_outgoing_edges": direct_out,
        "explored_nodes": explored,
        "truncated_node_candidates": len(clipped),
        "edge_count_in_view": len(internal),
        "edges_omitted": max(0, len(internal) - len(edges)),
        "is_complete_within_depth": not clipped and len(internal) <= MAX_EDGES,
        "scope": "BIDIRECTIONAL_GRAPH_NAVIGATION_NOT_CAUSAL_IMPACT",
        "runtime_discovery": full["runtime_discovery"],
        "execution_authorized": False,
    }
