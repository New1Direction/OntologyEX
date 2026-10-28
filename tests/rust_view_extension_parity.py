"""Parity and opt-in integration of the Rust PyO3 graph-query backend.

Requires a wheel installed by the CI job. Never contacts GitHub or a live service.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"ontology-extraction/scripts"))
sys.path.insert(0,str(ROOT/"tests"))
import graph_view_api as api
import rust_view_parity as fixture
import ontologyex_native

def check(label,expected,actual):
    fixture.equivalent(label,expected,actual)

def run():
    full,db=fixture.scenario(n=400)
    raw=json.dumps(full,sort_keys=True,separators=(",",":"),allow_nan=False)
    python=[]
    with patch.dict(os.environ, {"ONTOLOGYEX_VIEW_BACKEND":"python"}):
        python.append(("summary",(),api.summary(full)))
        for lens,q,off in [
            ("all","",0),("all","",90),("all","",180),
            ("services","web",0),("services","",360),
            ("api","",0),("database","",0),("all","s3",0),
        ]:
            python.append(("page",(lens,q,off),api.page(full,lens=lens,query=q,offset=off)))
        for id,depth in [
            (db,1),(db,2),(full["project"],1),(full["project"],2)
        ]:
            python.append(("neighborhood",(id,depth),api.neighborhood(full,id,depth=depth)))
        python.append(("node",(db,),api.node_detail(full,db)))
    with patch.dict(os.environ, {"ONTOLOGYEX_VIEW_BACKEND":"rust"}):
        for kind,args,expected in python:
            if kind=="summary":
                got=api.summary(full)
                direct=json.loads(ontologyex_native.summary_json(raw))
            elif kind=="page":
                lens,q,off=args
                got=api.page(full,lens=lens,query=q,offset=off)
                direct=json.loads(ontologyex_native.page_json(raw,lens,q,off))
            elif kind=="neighborhood":
                id,depth=args
                got=api.neighborhood(full,id,depth=depth)
                direct=json.loads(ontologyex_native.neighborhood_json(raw,id,depth))
            else:
                id=args[0]
                got=api.node_detail(full,id)
                direct=json.loads(ontologyex_native.node_json(raw,id))
            check(kind+" Python/Rust switch",expected,got)
            check(kind+" direct native extension",expected,direct)
    for invalid in [
        (ontologyex_native.page_json,(raw,"bogus","",0)),
        (ontologyex_native.neighborhood_json,(raw,db,3)),
        (ontologyex_native.node_json,(raw,"missing")),
        (ontologyex_native.summary_json,(json.dumps(dict(full,execution_authorized=True)),)),
    ]:
        func,args=invalid
        try:
            func(*args)
        except ValueError:
            pass
        else:
            raise AssertionError("native boundary accepted forbidden state")
    # Requested native binary absence must not silently fall back to Python.
    with patch.dict(os.environ, {"ONTOLOGYEX_VIEW_BACKEND":"unexpected"}):
        try: api.summary(full)
        except RuntimeError: pass
        else: raise AssertionError("unsupported backend was ignored")
    print("Rust PyO3 parity: 14 representative view cases on 400+ source nodes,"
          " exact Python equivalence and invalid-input refusal; no network")

if __name__=="__main__": run()
