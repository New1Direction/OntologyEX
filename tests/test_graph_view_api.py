"""Server-side graph projection: bounded responses, search, pagination and trust."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"ontology-extraction/scripts"))
import control_room as room
import github_discovery as gh
import graph_view_api as api

spec=importlib.util.spec_from_file_location("view_fixtures",ROOT/"tests/test_github_discovery.py")
fix=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=fix
spec.loader.exec_module(fix)

class GraphViewTests(unittest.TestCase):
    def setUp(self):
        fixture=fix.GitHubFixture()
        self.full=room.build_state(gh.discover_github(fixture.repo,fixture),[],{})
        self.full["discovery_source"]={"provider":"github"}
        self.full["discovery_freshness"]="AS_OF_LATEST_SUCCESSFUL_SCAN"
        self.full["snapshot_history"]=[]
        self.full["durability"]="MEMORY_ONLY"
        self.full["discovery_scanned_at"]=self.full["generated_at"]

    def test_summary_is_not_an_entire_graph_export(self):
        sm=api.summary(self.full)
        self.assertEqual(sm["kind"],"LOCAL_CONTROL_ROOM_SUMMARY")
        self.assertEqual(sm["total_nodes"],len(self.full["nodes"]))
        self.assertEqual(sm["total_edges"],len(self.full["edges"]))
        for forbidden in ("nodes","edges","source_evidence","unresolved"):
            self.assertNotIn(forbidden,sm)
        self.assertEqual(sm["runtime_discovery"],"NOT_IMPLEMENTED")
        self.assertFalse(sm["execution_authorized"])

    def test_page_is_bounded_and_contains_only_referenced_evidence(self):
        p=api.page(self.full)
        self.assertLessEqual(len(p["nodes"]),90)
        self.assertLessEqual(len(p["edges"]),300)
        ids={n["id"] for n in p["nodes"]}
        source_ids={e["id"] for e in p["source_evidence"]}
        for e in p["edges"]:
            self.assertIn(e["source"],ids)
            self.assertIn(e["target"],ids)
            self.assertIn(e["evidence_id"],source_ids)
        self.assertNotIn("SECRET=",json.dumps(p))
        self.assertFalse(p["execution_authorized"])

    def test_search_finds_correct_single_node_and_only_lens(self):
        api_nodes=[n for n in self.full["nodes"] if n["kind"]=="declared_service"]
        self.assertTrue(api_nodes)
        target=api_nodes[0]
        p=api.page(self.full,lens="services",query=target["label"])
        self.assertTrue(any(n["id"]==target["id"] for n in p["nodes"]))
        self.assertTrue(all(n["kind"] in ("project","declared_service")
                            for n in p["nodes"]))
        self.assertFalse(api.page(self.full,lens="database",query=target["label"])["nodes"])

    def test_large_graph_has_bounded_pagination_without_hidden_loss(self):
        bigger=dict(self.full)
        bigger["nodes"]=list(self.full["nodes"])
        for i in range(413):
            bigger["nodes"].append({
                "id":f"discovery:source_module:synthetic/{i:04d}",
                "kind":"source_module","label":f"synthetic-module-{i:04d}",
                "status":"unknown","potentially_impacted_by":[],"evidence_id":None,
                "ci_run":None,"source_status":None,"observed_at":None})
        seen=set()
        off=0
        for _ in range(10):
            page=api.page(bigger,lens="all",offset=off)
            batch={n["id"] for n in page["nodes"]}
            self.assertFalse(seen & batch)
            seen|=batch
            self.assertLessEqual(len(batch),90)
            if page["next_offset"] is None: break
            off=page["next_offset"]
        self.assertEqual(len(seen),len(bigger["nodes"]))
        self.assertEqual(api.summary(bigger)["total_nodes"],len(seen))
        found=api.page(bigger,lens="all",query="synthetic-module-0412")
        self.assertEqual(found["total_matches"],1)
        self.assertEqual(found["nodes"][0]["label"],"synthetic-module-0412")

    def test_node_detail_returns_bounded_explicit_evidence(self):
        target=next(n for n in self.full["nodes"] if n["kind"]=="declared_service")
        info=api.node_detail(self.full,target["id"])
        self.assertEqual(info["node"]["id"],target["id"])
        self.assertLessEqual(len(info["edges"]),40)
        self.assertEqual(info["total_related_edges"],len(
            [e for e in self.full["edges"] if e["source"]==target["id"] or e["target"]==target["id"]]))
        self.assertEqual(info["impact_semantics"],self.full["impact_semantics"])
        self.assertFalse(info["execution_authorized"])
        with self.assertRaises(api.ViewError):
            api.node_detail(self.full,"discovery:fake:other")

    def test_failure_does_not_change_unobserved_health(self):
        node=next(n for n in self.full["nodes"] if n["kind"]=="declared_service")
        node["status"]="error"
        sm=api.summary(self.full)
        self.assertEqual(sm["alerts"][0]["status"],"error")
        p=api.page(self.full,lens="services")
        self.assertEqual(next(n["status"] for n in p["nodes"] if n["id"]==node["id"]),"error")
        self.assertFalse(sm["execution_authorized"])

    def test_query_validation_and_offset_fail_closed(self):
        for raw in ("-1","0001","10001","bad","1e3"):
            with self.subTest(raw=raw),self.assertRaises(api.ViewError):
                api.parse_offset(raw)
        for lens in ("../private","alll","",None):
            with self.subTest(lens=lens),self.assertRaises(api.ViewError):
                api.page(self.full,lens=lens)
        for q in ("\n","x"*91,"\u0000"):
            with self.subTest(q=q),self.assertRaises(api.ViewError):
                api.page(self.full,query=q)

    def test_relation_truncation_is_reported(self):
        bigger=dict(self.full)
        project=bigger["project"]
        n=next(n for n in bigger["nodes"] if n["id"]!=project)
        bigger["edges"]=[{
            "source":project,"target":n["id"],"relation":f"RELATION_{i:03d}",
            "evidence_id":n["evidence_id"]
        } for i in range(400)]
        p=api.page(bigger)
        self.assertEqual(len(p["edges"]),300)
        self.assertEqual(p["edges_omitted"],100)
        detail=api.node_detail(bigger,n["id"])
        self.assertEqual(len(detail["edges"]),40)
        self.assertEqual(detail["edges_omitted"],360)

if __name__=="__main__":
    unittest.main()
