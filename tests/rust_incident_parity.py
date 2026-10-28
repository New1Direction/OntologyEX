"""Exact Rust/Python incident evidence parity on deterministic local source cases."""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"ontology-extraction/scripts"))
import discover_repo
import control_room
import incident_view

BIN=ROOT/"engine/target/debug/ontologyex-view"
NOW=datetime(2026,10,27,16,0,tzinfo=timezone.utc)


def event(component_id, seconds, status="error", ttl=120, source="synthetic", metric=0):
    return {"schema_version":1, "component_id":component_id,
            "observed_at":(NOW-timedelta(seconds=seconds)).isoformat().replace("+00:00","Z"),
            "status":status,"source":source,"expires_after_seconds":ttl,
            "metrics":{"queue_lag_seconds":metric} if metric else {}}


def rust(full,rows,id,expect_ok=True):
    p=subprocess.run([str(BIN),"incident",id],
                     input=json.dumps({"full":full,"rows":rows},separators=(",",":")).encode(),
                     capture_output=True,timeout=8)
    if not expect_ok:
        assert p.returncode!=0,p.stdout[:400]
        return None
    assert p.returncode==0,p.stderr[:700]
    return json.loads(p.stdout)


def main():
    assert BIN.is_file(),"native binary must be compiled first"
    with tempfile.TemporaryDirectory(prefix="ontologyex-incident-parity-") as folder:
        root=Path(folder)
        (root/"compose.yml").write_text(
            "services:\n  web:\n    depends_on: [api]\n  api:\n    depends_on: [db]\n  db: {}\n",
            encoding="utf-8")
        topology=discover_repo.discover(root)
        db=discover_repo.canonical_id("service","compose.yml","db")
        cases=[
            ("unknown",[]),
            ("failure",[event(db,0,"error")]),
            ("stale",[event(db,700,"error",30)]),
            ("healthy",[event(db,10,"ok")]),
            ("conflicting",[event(db,4,"ok"),event(db,4,"error")]),
            ("future ignored",[event(db,-5,"error"),event(db,0,"ok")]),
            ("metrics",[event(db,4,"warning",metric=42),
                        event(db,2,"error",metric=120)]),
            ("long history",[event(db,i*2,"warning",ttl=180,metric=i+1)
                             for i in range(46)]),
            ("different sources",[event(db,50,"ok",source="synthetic"),
                                  event(db,25,"error",source="health_probe")])
        ]
        for name,rows in cases:
            full=control_room.build_state(topology,rows,{},NOW)
            full["snapshot_history"]=[
                {"commit_sha":f"{i:040x}","scanned_at":"2026-10-27T14:00:00Z",
                 "is_current":i==0} for i in range(10)]
            full["discovery_source"]={"provider":"local"}
            full["discovery_freshness"]="AS_OF_LATEST_SUCCESSFUL_SCAN"
            full["durability"]="MEMORY_ONLY"
            expected=incident_view.incident(full,rows,db)
            actual=rust(full,rows,db)
            if expected != actual:
                for field in sorted(set(expected)|set(actual)):
                    if expected.get(field)!=actual.get(field):
                        raise AssertionError(f"{name}: {field} Python={str(expected.get(field))[:500]} "
                                             f"Rust={str(actual.get(field))[:500]}")
                raise AssertionError(f"{name} mismatch")
            assert actual["root_cause"]=="UNDETERMINED"
            assert not actual["execution_authorized"]
            assert len(actual["timeline"])<=32
            assert len(actual["possible_dependents"])<=40
            assert len(actual["direct_relationships"])<=40
            print("Rust incident exact parity:",name)
        # Rejection behavior: never accept a graph that grants authority.
        clean=control_room.build_state(topology,[],{},NOW)
        rust({**clean,"execution_authorized":True},[],db,expect_ok=False)
        rust(clean,[],db+"not-found",expect_ok=False)
        rust(clean,[{"schema_version":1,"component_id":db,"status":"bogus",
                     "source":"synthetic","observed_at":"2026-10-27T16:00:00Z",
                     "expires_after_seconds":120,"metrics":{}}],db,expect_ok=False)
        print("Rust incident parity: 9 cases and 3 invalid cases; no production access")


if __name__=="__main__":
    main()
