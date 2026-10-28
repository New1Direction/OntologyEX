#!/usr/bin/env python3
"""Run a SPARQL file over an OntologyEX N-Triples export with sparq (optional).

    python -m pip install 'sparq-rdf==0.1.6'
    python examples/sparql/run.py graph.nt examples/sparql/review-blockers.rq

Prints tab-separated rows. Query results describe authored declarations only;
they are not approvals, entailments, or runtime observations.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def run(graph: Path, query: Path) -> list[dict[str, str]]:
    import sparq  # Optional dependency: PyPI distribution sparq-rdf.

    g = sparq.Graph.load(graph, format="ntriples")
    result = g.query(query.read_text(encoding="utf-8"))
    return [{var: row[var].value for var in result.vars if var in row} for row in result]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("graph", type=Path, help="N-Triples from rdf_export.py")
    parser.add_argument("query", type=Path, nargs="+", help="one or more .rq files")
    args = parser.parse_args()
    try:
        for query in args.query:
            rows = run(args.graph, query)
            print(f"# {query.name}: {len(rows)} row(s)")
            for row in rows:
                print("\t".join(f"{k}={v}" for k, v in row.items()))
        return 0
    except ImportError:
        sys.stderr.write("sparq is not installed: python -m pip install 'sparq-rdf==0.1.6'\n")
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write(f"query failed: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
