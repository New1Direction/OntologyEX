"""Bounded source-backed neighborhood navigation. No runtime inference."""
import importlib.util
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"ontology-extraction/scripts"))
import github_discovery as gh
import control_room as room
import graph_view_api as api

spec=importlib.util.spec_from_file_location("neighborhood_fixtures", ROOT/"tests/test_github_discovery.py")
fixture=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=fixture
spec.loader.exec_module(fixture)


class NeighborhoodTests(unittest.TestCase):
    def setUp(self):
        source=fixture.GitHubFixture()
        self.graph=room.build_state(gh.discover_github(source.repo,source),[],{})
        self.graph["discovery_source"]={"provider":"github"}
        self.graph["discovery_freshness"]="AS_OF_LATEST_SUCCESSFUL_SCAN"
        self.graph["snapshot_history"]=[]
        self.graph["durability"]="MEMORY_ONLY"
        self.graph["discovery_scanned_at"]=self.graph["generated_at"]
        self.services={n["label"]:n for n in self.graph["nodes"]
                       if n["kind"]=="declared_service"}

    def test_one_hop_preserves_dependency_direction_not_outage(self):
        web=self.services["web"]
        api_node=self.services["api"]
        db=self.services["db"]
        n=api.neighborhood(self.graph,api_node["id"],depth=1)
        self.assertEqual(n["kind"],"BOUNDED_GRAPH_NEIGHBORHOOD")
        self.assertEqual(n["center_id"],api_node["id"])
        self.assertEqual(n["requested_depth"],1)
        self.assertEqual({x["id"] for x in n["nodes"]},
                         {api_node["id"],web["id"],db["id"],
                          self.graph["project"]})
        relations=[e for e in n["edges"] if e["relation"]=="DEPENDS_ON"]
        self.assertEqual({(e["source"],e["target"]) for e in relations},
                         {(web["id"],api_node["id"]),(api_node["id"],db["id"])})
        self.assertEqual(n["direct_incoming_edges"],2)
        self.assertEqual(n["direct_outgoing_edges"],1)
        self.assertEqual(n["scope"],"BIDIRECTIONAL_GRAPH_NAVIGATION_NOT_CAUSAL_IMPACT")
        self.assertTrue(all(x["status"]=="unknown" for x in n["nodes"]))
        self.assertFalse(n["execution_authorized"])
        evidence={x["id"] for x in n["source_evidence"]}
        self.assertTrue(all(e["evidence_id"] in evidence for e in n["edges"]))

    def test_two_hops_reaches_database_from_web_and_reverse(self):
        web=self.services["web"]
        api_id=self.services["api"]["id"]
        db_id=self.services["db"]["id"]
        a=api.neighborhood(self.graph,web["id"],depth=1)
        b=api.neighborhood(self.graph,web["id"],depth=2)
        self.assertIn(api_id,{n["id"] for n in a["nodes"]})
        self.assertNotIn(db_id,{n["id"] for n in a["nodes"]})
        self.assertIn(db_id,{n["id"] for n in b["nodes"]})
        self.assertEqual(b["depth_reached"],2)
        reverse=api.neighborhood(self.graph,db_id,depth=2)
        self.assertIn(web["id"],{n["id"] for n in reverse["nodes"]})
        self.assertEqual(reverse["direct_incoming_edges"],2)
        self.assertEqual(reverse["direct_outgoing_edges"],0)

    def test_cycle_terminates_and_avoids_inventing_impact(self):
        synthetic=dict(self.graph)
        synthetic["edges"]=list(self.graph["edges"])
        web_id=self.services["web"]["id"]
        db_id=self.services["db"]["id"]
        evidence=synthetic["edges"][0]["evidence_id"]
        synthetic["edges"].append({"source":db_id,"target":web_id,
                                   "relation":"STATIC_IMPORTS","evidence_id":evidence,
                                   "state":"SOURCE_SYNTAX_CANDIDATE"})
        n=api.neighborhood(synthetic,db_id,depth=2)
        self.assertEqual(len({x["id"] for x in n["nodes"]}),len(n["nodes"]))
        self.assertTrue(n["is_complete_within_depth"])
        self.assertFalse(any(x["status"]=="error" for x in n["nodes"]))

    def test_star_caps_nodes_and_reports_missing_neighbors(self):
        star=dict(self.graph)
        center=self.services["web"]["id"]
        ev=self.graph["edges"][0]["evidence_id"]
        star["nodes"]=list(self.graph["nodes"])
        star["edges"]=list(self.graph["edges"])
        for i in range(220):
            name=f"extra_{i:03d}"
            node={"id":f"discovery:source_module:gh1234/{name}",
                  "label":name,"kind":"source_module","status":"unknown",
                  "evidence_id":ev,"potentially_impacted_by":[]}
            star["nodes"].append(node)
            star["edges"].append({"source":center,"target":node["id"],
                "relation":"STATIC_IMPORTS","evidence_id":ev,
                "state":"SOURCE_SYNTAX_CANDIDATE"})
        n=api.neighborhood(star,center,depth=2)
        self.assertEqual(len(n["nodes"]),90)
        self.assertGreater(n["truncated_node_candidates"],100)
        self.assertFalse(n["is_complete_within_depth"])
        self.assertLessEqual(len(n["edges"]),300)
        self.assertEqual(n["direct_outgoing_edges"],221)
        self.assertEqual(len(n["source_evidence"]),len({r["id"] for r in n["source_evidence"]}))
        self.assertTrue(all(e["source"] in {v["id"] for v in n["nodes"]} and
                            e["target"] in {v["id"] for v in n["nodes"]}
                            for e in n["edges"]))

    def test_edge_truncation_is_explicit_and_cites_accepted_edges(self):
        graph=dict(self.graph)
        center=self.services["web"]["id"]
        neighbor=self.services["api"]["id"]
        ev=self.graph["edges"][0]["evidence_id"]
        graph["edges"]=list(self.graph["edges"])+[
            {"source":center,"target":neighbor,
             "relation":f"STATIC_IMPORTS_{i:04d}","evidence_id":ev}
            for i in range(420)]
        n=api.neighborhood(graph,center,depth=1)
        self.assertEqual(len(n["edges"]),300)
        self.assertGreater(n["edges_omitted"],100)
        self.assertFalse(n["is_complete_within_depth"])
        self.assertEqual(n["direct_outgoing_edges"],421)

    def test_no_source_access_or_unverified_runtime_claims(self):
        for depth in (1,2):
            n=api.neighborhood(self.graph,self.services["api"]["id"],depth=depth)
            self.assertNotIn("SECRET=",str(n))
            self.assertNotIn("private_token",str(n))
            self.assertNotIn("confirmed",n["scope"].lower())
            self.assertEqual(n["runtime_discovery"],"NOT_IMPLEMENTED")

    def test_strict_query_inputs_and_missing_id(self):
        with self.assertRaises(api.ViewError):
            api.neighborhood(self.graph,"discovery:service:nonexistent")
        for val in (0,3,-1,"1",None,True):
            with self.subTest(depth=val),self.assertRaises(api.ViewError):
                api.neighborhood(self.graph,self.services["web"]["id"],depth=val)
        for raw in ("0","3","-1","2.5",None,"1&cmd=exec"):
            with self.subTest(raw=raw),self.assertRaises(api.ViewError):
                api.valid_depth(raw)
        self.assertEqual(api.valid_depth("2"),2)

    def test_determinism_on_edge_reordering(self):
        a=api.neighborhood(self.graph,self.services["api"]["id"],depth=2)
        reordered=dict(self.graph)
        reordered["edges"]=list(reversed(self.graph["edges"]))
        b=api.neighborhood(reordered,self.services["api"]["id"],depth=2)
        self.assertEqual(a,b)


if __name__=="__main__":
    unittest.main()
