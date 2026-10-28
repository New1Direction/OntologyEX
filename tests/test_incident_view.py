"""Read-only incident evidence and hypothetical impact safety tests."""
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"ontology-extraction/scripts"))
import discover_repo
import control_room as room
import incident_view as incident

NOW=datetime(2026,10,27,16,0,tzinfo=timezone.utc)

class IncidentTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        (root/"compose.yml").write_text(
            "services:\n  web:\n    depends_on: [api]\n  api:\n    depends_on: [db]\n  db: {}\n",
            encoding="utf-8")
        self.topology=discover_repo.discover(root)
        self.ids={x:discover_repo.canonical_id("service","compose.yml",x)
                  for x in ("web","api","db")}

    def event(self,status="error",component="db",seconds=0,ttl=120,source="synthetic"):
        date=NOW-timedelta(seconds=seconds)
        return {"schema_version":1,"component_id":self.ids[component],
                "observed_at":date.isoformat().replace("+00:00","Z"),
                "status":status,"source":source,"expires_after_seconds":ttl,
                "metrics":{"queue_lag_seconds":125} if status=="warning" else {}}

    def case(self,events):
        # Simulate the shared, already validated observation feed used by the
        # local HTTP handler for both state projection and incident timeline.
        full=room.build_state(self.topology,list(events),{},NOW)
        full["snapshot_history"]=[{"commit_sha":"a"*40,
            "scanned_at":"2026-10-27T14:00:00Z","is_current":True}]
        return full

    def test_failure_is_unattested_and_possible_dependents_not_confirmed(self):
        events=[self.event()]
        result=incident.incident(self.case(events),events,self.ids["db"])
        self.assertEqual(result["component"]["reported_state"],"error")
        self.assertEqual(result["component"]["source_authenticity"],"UNATTESTED_LOCAL_INPUT")
        self.assertEqual(result["root_cause"],"UNDETERMINED")
        self.assertEqual(result["runtime_discovery"],"NOT_IMPLEMENTED")
        self.assertEqual(result["impact_semantics"],
                         "DECLARED_DEPENDENCY_POTENTIAL_IMPACT_NOT_CAUSAL_PROOF")
        self.assertFalse(result["execution_authorized"])
        self.assertEqual({x["label"] for x in result["possible_dependents"]},{"web","api"})
        self.assertTrue(all(x["independently_reported_status"]=="unknown"
                            for x in result["possible_dependents"]))
        self.assertEqual(result["source_history_semantics"],
                         "SOURCE_SCAN_VERSIONS_NOT_DEPLOYMENT_EVENTS")

    def test_stale_failure_cannot_be_presented_as_current_incident(self):
        events=[self.event(seconds=600,ttl=60)]
        result=incident.incident(self.case(events),events,self.ids["db"])
        self.assertEqual(result["component"]["reported_state"],"stale")
        self.assertFalse(result["timeline"][0]["fresh_at_evaluation"])
        self.assertEqual(result["possible_dependents"],[])
        self.assertEqual(result["root_cause"],"UNDETERMINED")

    def test_unknown_has_no_fabricated_observation(self):
        result=incident.incident(self.case([]),[],self.ids["db"])
        self.assertEqual(result["component"]["reported_state"],"unknown")
        self.assertEqual(result["timeline"],[])
        self.assertEqual(result["observation_count"],0)
        self.assertEqual(result["possible_dependents"],[])

    def test_future_samples_excluded_and_duplicates_order_independent(self):
        a=self.event(status="ok",seconds=5)
        b=self.event(status="error",seconds=0)
        future=self.event(status="error",seconds=-20)
        rows=[future,b,a]
        full=self.case(rows)
        x=incident.incident(full,rows,self.ids["db"])
        y=incident.incident(full,list(reversed(rows)),self.ids["db"])
        self.assertEqual(x,y)
        self.assertEqual(x["observation_count"],2)
        self.assertEqual([e["status"] for e in x["timeline"]],["ok","error"])

    def test_timeline_bounded_and_missing_evidence_not_invented(self):
        rows=[self.event(seconds=i*2,status="warning") for i in range(45)]
        full=self.case(rows)
        result=incident.incident(full,rows,self.ids["db"])
        self.assertEqual(result["observation_count"],45)
        self.assertEqual(result["observations_included"],32)
        self.assertEqual(result["older_observations_omitted"],13)
        self.assertEqual(len(result["timeline"]),32)
        source_ids={e["id"] for e in full["source_evidence"]}
        self.assertTrue(all(e["id"] in source_ids for e in result["source_evidence"]))

    def test_incident_unmapped_component_fails_closed(self):
        for bad in ("missing", "", "x\nhidden", "q"*1025):
            with self.subTest(bad=bad),self.assertRaises(incident.IncidentError):
                incident.incident(self.case([]),[],bad)

    def test_jsonl_metrics_allowed_only_and_no_runtime_proof(self):
        rows=[self.event(status="warning")]
        result=incident.incident(self.case(rows),rows,self.ids["db"])
        self.assertEqual(result["timeline"][0]["metrics"],{"queue_lag_seconds":125})
        self.assertIn("UNATTESTED",result["kind"])
        self.assertNotIn("PASSWORD",str(result))
        self.assertEqual(result["source_history"][0]["commit_sha"],"a"*40)
        self.assertNotIn("deployed",result["source_history_semantics"].lower())

if __name__=="__main__":
    unittest.main()
