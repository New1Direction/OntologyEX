"""Compare the Rust view implementation to the existing Python graph protocol.

Runs only in CI after the native binary has compiled. No network, database,
GitHub token, live telemetry or production infrastructure.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"ontology-extraction/scripts"))
import control_room
import dependency_suggest
import discover_repo
import graph_view_api

BINARY=ROOT/"engine/target/debug/ontologyex-view"
NOW=datetime(2026,10,27,23,0,tzinfo=timezone.utc)


def native(full, *args):
    payload=json.dumps(full,sort_keys=True,separators=(",",":")).encode()
    output=subprocess.run([str(BINARY),*map(str,args)],
                          input=payload,capture_output=True,timeout=10)
    if output.returncode!=0:
        raise AssertionError(
            f"Rust view rejected expected valid input: {output.stderr[:600]!r}")
    return json.loads(output.stdout)


def equivalent(label,expected,actual):
    if expected != actual:
        for field in sorted(set(expected)|set(actual)):
            if expected.get(field)!=actual.get(field):
                raise AssertionError(
                    f"{label} mismatched {field}: expected "
                    f"{str(expected.get(field))[:450]} vs native "
                    f"{str(actual.get(field))[:450]}")
        raise AssertionError(label+" output mismatch")


def scenario(n=120):
    with tempfile.TemporaryDirectory(prefix="ontologyex-rust-parity-") as name:
        root=Path(name)
        manifests="services:\n  web:\n    depends_on: [api]\n  api:\n    depends_on: [db]\n  db: {}\n"
        manifests+="".join(f"  s{i:03d}: {{}}\n" for i in range(n))
        (root/"compose.yml").write_text(manifests)
        (root/"package.json").write_text('{"name":"parity"}')
        topo=discover_repo.discover(root)
        # A dependency seen in traces and confirmed by a person: s000 calls api.
        evidence={"caller_services":["s000 -> api"],"calls":30,"errors":0,
                  "first_seen":"2026-10-27T20:00:00Z","last_seen":"2026-10-27T22:00:00Z"}
        topo=dependency_suggest.with_confirmed(topo,[{
            "source":discover_repo.canonical_id("service","compose.yml","s000"),
            "target":discover_repo.canonical_id("service","compose.yml","api"),
            "declared_by":"parity","note":"","evidence":evidence}],"dependencies.json")
        db=discover_repo.canonical_id("service","compose.yml","db")
        rows=[{
            "schema_version":1,"component_id":db,
            "observed_at":"2026-10-27T22:59:30Z",
            "expires_after_seconds":120,"status":"error","source":"synthetic",
            "metrics":{"queue_lag_seconds":7}
        }]
        full=control_room.build_state(topo,rows,{},NOW)
        full.update({
            "snapshot_history":[{"commit_sha":"a"*40,"scanned_at":"2026-10-27T22:58:00Z",
                                 "is_current":True}],
            "discovery_source":{"provider":"local"},
            "discovery_freshness":"AS_OF_LATEST_SUCCESSFUL_SCAN",
            "discovery_scanned_at":"2026-10-27T23:00:00Z",
            "durability":"MEMORY_ONLY"
        })
        return full,db


def run():
    full,db=scenario()
    assert len(full["nodes"])>=123
    caller=discover_repo.canonical_id("service","compose.yml","s000")
    assert next(n for n in full["nodes"] if n["id"]==caller)["potentially_impacted_by"]==[db]
    equivalent("summary",graph_view_api.summary(full),native(full,"summary"))
    for lens,q,off in [
        ("all","",0),("all","",90),("all","",180),
        ("services","",0),("services","web",0),
        ("database","",0),("api","",0),
        ("all","s0",0),("all","missing-no-such-node",0),
    ]:
        equivalent(f"page({lens},{q},{off})",
                   graph_view_api.page(full,lens=lens,query=q,offset=off),
                   native(full,"page",lens,q,off))
    for center,depth in [
        (db,1),(db,2),
        (discover_repo.canonical_id("service","compose.yml","web"),1),
        (discover_repo.canonical_id("service","compose.yml","web"),2),
        (full["project"],1),(full["project"],2),(caller,1),(caller,2),
    ]:
        equivalent(f"neighborhood({center},{depth})",
                   graph_view_api.neighborhood(full,center,depth=depth),
                   native(full,"neighborhood",center,depth))
    for id in (db,full["project"]):
        equivalent("node("+id+")",graph_view_api.node_detail(full,id),
                   native(full,"node",id))
    # Both implementations must reject a source state that grants execution.
    fake=dict(full,execution_authorized=True)
    rejected=subprocess.run([str(BINARY),"summary"],
                            input=json.dumps(fake).encode(),
                            capture_output=True,timeout=10)
    assert rejected.returncode!=0
    # And reject unsupported traversal depth, empty IDs and invalid lens.
    for args in [("neighborhood",db,"3"),("node","missing-id"),
                 ("page","invalid","","0"),("page","all","","-1")]:
        result=subprocess.run([str(BINARY),*args],
                              input=json.dumps(full).encode(),
                              capture_output=True,timeout=10)
        assert result.returncode!=0,args
    print("Rust/Python parity: summary + 9 pages + 8 neighborhoods (one over a confirmed CALLS edge) + 2 node details; "
          "invalid inputs refused; no production calls")


if __name__=="__main__":
    run()
