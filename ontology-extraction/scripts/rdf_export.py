#!/usr/bin/env python3
"""Export verified OntologyEX declared graphs as deterministic RDF N-Triples.

A standards-based interchange format, NOT independently checked ontology
entailment, SHACL conformance, approval or operational telemetry.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import quote

import graph_view
from policy_check import DomainError, require

RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"
PROV_SOURCE = "<http://www.w3.org/ns/prov#wasDerivedFrom>"
XSD_INTEGER = "<http://www.w3.org/2001/XMLSchema#integer>"
BASE = "urn:ontologyex:v1:"
KINDS = frozenset(["evidence", *(row[2] for row in graph_view.SPECS)])
RELATIONS = frozenset({
    "SUPPORTED_BY", "ANCHORED_TO", "IMPLEMENTS", "APPLIES_TO",
    "DOMAIN", "RANGE", "CONSUMES", "PRODUCES", "ACTOR_ROLE",
    "DECOMPOSES_TO", "BINDS_TO", "USED_BY_TASK",
})
MAX_TRIPLES = 100000


def iri(category: str, value: str) -> str:
    require(category in ("node", "kind", "edge", "attr"), "invalid IRI category")
    require(type(value) is str and 0 < len(value) <= 4096, "invalid IRI value")
    # URN namespace is local. No network dereference and no dependency on cwd.
    return f"<{BASE}{category}:{quote(value, safe='-._~')}>"


def literal(value: str) -> str:
    """N-Triples string token: avoid JSON-only escapes such as backspace."""
    require(type(value) is str and len(value) <= 8192, "invalid RDF literal")
    escaped = []
    for char in value:
        n = ord(char)
        if char == "\\":
            escaped.append("\\\\")
        elif char == '"':
            escaped.append('\\"')
        elif char == "\t":
            escaped.append("\\t")
        elif char == "\n":
            escaped.append("\\n")
        elif char == "\r":
            escaped.append("\\r")
        elif n < 0x20 or n == 0x7f:
            escaped.append(f"\\u{n:04X}")
        elif n > 0xffff:
            escaped.append(f"\\U{n:08X}")
        elif n >= 0x80:
            escaped.append(f"\\u{n:04X}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def serialize(graph: dict) -> str:
    """Exact authored-graph assertions, sorted for reproducibility."""
    require(type(graph) is dict and graph.get("schema_version") == 1 and
            graph.get("kind") in ("COMPILED_DECLARATION_PROJECTION",
                                  "CROSS_SCOPE_DECLARATION_VIEW") and
            graph.get("execution_authorized") is False,
            "not an unapproved OntologyEX graph")
    unresolved = graph.get("unresolved")
    require(type(unresolved) is list and not unresolved,
            "unresolved graph references: inspect graph_view JSON before RDF export")
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    require(type(nodes) is list and type(edges) is list and
            len(nodes) <= graph_view.MAX_NODES and len(edges) <= graph_view.MAX_EDGES,
            "invalid or oversized graph")
    statements = set()
    node_ids = set()

    def add(subject: str, predicate: str, obj: str) -> None:
        require(len(statements) < MAX_TRIPLES, "RDF triple budget exceeded")
        statements.add(f"{subject} {predicate} {obj} .")

    for node in nodes:
        require(type(node) is dict and node.get("kind") in KINDS and
                type(node.get("id")) is str and type(node.get("local_id")) is str,
                "invalid typed node")
        key = node["id"]
        require(key not in node_ids, "duplicate node ID")
        node_ids.add(key)
        subject = iri("node", key)
        add(subject, RDF_TYPE, iri("kind", node["kind"]))
        add(subject, RDFS_LABEL, literal(node["local_id"]))
        if node["kind"] == "evidence":
            require(type(node.get("path")) is str and
                    type(node.get("sha256")) is str and len(node["sha256"]) == 64 and
                    type(node.get("start_line")) is int and type(node.get("end_line")) is int,
                    "invalid evidence metadata")
            for field in ("path", "sha256", "evidence_kind"):
                require(type(node.get(field)) is str, "invalid evidence attribute")
                add(subject, iri("attr", field), literal(node[field]))
            for field in ("start_line", "end_line"):
                require(node[field] > 0, "invalid line number")
                add(subject, iri("attr", field),
                    f'{literal(str(node[field]))}^^{XSD_INTEGER}')

    for edge in edges:
        require(type(edge) is dict and edge.get("relation") in RELATIONS and
                type(edge.get("source")) is str and type(edge.get("target")) is str,
                "unknown/invalid graph predicate")
        require(edge["source"] in node_ids and edge["target"] in node_ids,
                "graph edge references an absent node")
        relation = edge["relation"]
        predicate = PROV_SOURCE if relation == "SUPPORTED_BY" else iri("edge", relation)
        add(iri("node", edge["source"]), predicate, iri("node", edge["target"]))

    # Unresolved references are not silently promoted to RDF facts.
    # The caller must read graph.unresolved separately for review.
    return "\n".join(sorted(statements)) + ("\n" if statements else "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill", type=Path, nargs="+",
                        help="one verified skill or distinct scopes to compose")
    args = parser.parse_args()
    try:
        graph = (graph_view.project(args.skill[0]) if len(args.skill) == 1
                 else graph_view.compose(args.skill))
        result = serialize(graph)
        sys.stdout.write(result)
        return 0
    except (DomainError, OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        sys.stderr.write(f"RDF export refused: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
