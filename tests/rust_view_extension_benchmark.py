"""Measure full Python -> Rust extension marshaling costs, not a speedup claim.

No failure threshold: do not activate Rust by default based on microbenchmarks.
"""
import json
import os
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"ontology-extraction/scripts"),str(ROOT/"tests")]
import graph_view_api as api
from rust_view_parity import scenario
import ontologyex_native

def measure(mode, full, target):
    timings={}
    with patch.dict(os.environ,{"ONTOLOGYEX_VIEW_BACKEND":mode}):
        operations={
            "summary":lambda:api.summary(full),
            "page":lambda:api.page(full,lens="all",query="",offset=90),
            "neighborhood":lambda:api.neighborhood(full,target,depth=2),
            "node":lambda:api.node_detail(full,target),
        }
        for key,operation in operations.items():
            for _ in range(5):operation()
            samples=[]
            for _ in range(35):
                start=time.perf_counter()
                result=operation()
                assert not result["execution_authorized"]
                samples.append((time.perf_counter()-start)*1000)
            samples.sort()
            timings[key]={"median_ms":round(statistics.median(samples),4),
                          "p95_ms":round(samples[int(len(samples)*0.95)],4)}
    return timings

def measure_indexed(full,target):
    raw=json.dumps(full,sort_keys=True,separators=(",",":"),allow_nan=False)
    start=time.perf_counter()
    index=ontologyex_native.GraphIndex(raw)
    build_ms=(time.perf_counter()-start)*1000
    operations={
        "summary":index.summary_json,
        "page":lambda:index.page_json("all","",90),
        "neighborhood":lambda:index.neighborhood_json(target,2),
        "node":lambda:index.node_json(target),
    }
    timings={}
    for key,operation in operations.items():
        for _ in range(5):json.loads(operation())
        measurements=[]
        for _ in range(35):
            start=time.perf_counter()
            result=json.loads(operation())
            assert not result["execution_authorized"]
            measurements.append((time.perf_counter()-start)*1000)
        measurements.sort()
        timings[key]={"median_ms":round(statistics.median(measurements),4),
                      "p95_ms":round(measurements[int(len(measurements)*0.95)],4)}
    return {"initial_build_ms":round(build_ms,4),"per_query":timings}


def run():
    full,target=scenario(n=400)
    results={"graph_nodes":len(full["nodes"]),
             "graph_edges":len(full["edges"]),
             "method":"whole Python API call vs Rust JSON FFI vs cached native index",
             "python":measure("python",full,target),
             "rust":measure("rust",full,target),
             "rust_cached":measure_indexed(full,target),
             "caveat":"Cached index must be rebuilt whenever source state changes; default stays Python pending a safe state-revision cache"}
    print(json.dumps(results,sort_keys=True,indent=2))
if __name__=="__main__":run()
