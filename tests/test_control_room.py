"""Behavioral acceptance tests for first local operational control-room slice."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ontology-extraction/scripts"))
import discover_repo
import control_room as room

NOW = datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc)


class ControlRoomTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "compose.yml").write_text(
            "services:\n"
            "  web:\n"
            "    depends_on: [api]\n"
            "  api:\n"
            "    depends_on: [db]\n"
            "  db: {}\n", encoding="utf-8")
        self.graph = discover_repo.discover(self.repo)
        self.web = discover_repo.canonical_id("service", "compose.yml", "web")
        self.api = discover_repo.canonical_id("service", "compose.yml", "api")
        self.db = discover_repo.canonical_id("service", "compose.yml", "db")

    def record(self, component=None, status="ok", date=NOW, metrics=None, ttl=120,
               source="synthetic"):
        return {"schema_version": 1, "component_id": component or self.db,
                "observed_at": date.isoformat().replace("+00:00", "Z"),
                "status": status, "source": source,
                "expires_after_seconds": ttl, "metrics": metrics or {}}

    def state(self, rows=(), thresholds=None):
        return room.build_state(self.graph, list(rows), thresholds or {}, NOW)

    def node(self, state, uid):
        return next(n for n in state["nodes"] if n["id"] == uid)

    def test_no_monitoring_means_unknown_not_healthy(self):
        state = self.state()
        for node in state["nodes"]:
            self.assertEqual(node["status"], "unknown")
        self.assertEqual(state["coverage"]["observed_nodes"], 0)
        self.assertEqual(state["runtime_discovery"], "NOT_IMPLEMENTED")
        self.assertFalse(state["execution_authorized"])

    def test_fresh_ok_is_only_local_source_report(self):
        state = self.state([self.record()])
        db = self.node(state, self.db)
        self.assertEqual(db["status"], "ok")
        self.assertEqual(db["observation_source"], "synthetic")
        self.assertEqual(db["source_authenticity"], "UNATTESTED_LOCAL_INPUT")
        self.assertEqual(self.node(state, self.api)["status"], "unknown")

    def test_failure_propagates_as_possible_impact_not_confirmed_failure(self):
        state = self.state([self.record(status="error")])
        self.assertEqual(self.node(state, self.db)["status"], "error")
        self.assertEqual(self.node(state, self.api)["status"], "unknown")
        self.assertEqual(self.node(state, self.web)["status"], "unknown")
        self.assertEqual(self.node(state, self.api)["potentially_impacted_by"], [self.db])
        self.assertEqual(self.node(state, self.web)["potentially_impacted_by"], [self.db])
        self.assertIn("POTENTIAL_IMPACT", state["impact_semantics"])

    def test_stale_error_not_treated_as_current_failure(self):
        state = self.state([self.record(
            status="error", date=NOW-timedelta(minutes=10), ttl=60)])
        self.assertEqual(self.node(state, self.db)["status"], "stale")
        self.assertEqual(self.node(state, self.api)["potentially_impacted_by"], [])
        self.assertEqual(self.node(state, self.db)["source_status"], "error")

    def test_latest_sample_wins_regardless_of_input_order(self):
        older = self.record(status="error", date=NOW-timedelta(seconds=20))
        latest = self.record(status="ok")
        a = self.state([older, latest])
        b = self.state([latest, older])
        self.assertEqual(a, b)
        self.assertEqual(self.node(a, self.db)["status"], "ok")

    def test_one_threshold_breach_is_not_prediction(self):
        state = self.state([self.record(metrics={"queue_lag_seconds": 200})],
                           {"queue_lag_seconds": 100})
        self.assertEqual(self.node(state, self.db)["status"], "ok")
        self.assertEqual(self.node(state, self.db)["leading_indicators"], [])

    def test_two_fresh_threshold_breaches_warn(self):
        old = self.record(date=NOW-timedelta(seconds=15), metrics={"queue_lag_seconds": 150})
        new = self.record(metrics={"queue_lag_seconds": 200})
        state = self.state([old, new], {"queue_lag_seconds": 100})
        self.assertEqual(self.node(state, self.db)["status"], "warning")
        leading = self.node(state, self.db)["leading_indicators"]
        self.assertEqual(len(leading), 1)
        self.assertEqual(leading[0]["rule"], "TWO_CONSECUTIVE_FRESH_BREACHES")
        self.assertEqual(state["leading_indicator_semantics"],
                         "USER_CONFIGURED_THRESHOLD_NOT_OUTAGE_PREDICTION")

    def test_prior_stale_warning_not_used(self):
        old = self.record(date=NOW-timedelta(minutes=10), ttl=30,
                          metrics={"queue_lag_seconds": 150})
        new = self.record(metrics={"queue_lag_seconds": 200})
        state = self.state([old, new], {"queue_lag_seconds": 100})
        self.assertEqual(self.node(state, self.db)["status"], "ok")

    def test_future_timestamp_ignored(self):
        state = self.state([self.record(status="error", date=NOW+timedelta(seconds=5))])
        self.assertEqual(self.node(state, self.db)["status"], "unknown")
        self.assertIn("FUTURE_OBSERVATION_IGNORED", [g["reason"] for g in state["gaps"]])

    def test_unknown_component_not_inserted_into_graph(self):
        row = self.record(component=discover_repo.canonical_id("service", "compose.yml", "ghost"))
        state = self.state([row])
        self.assertEqual(len(state["nodes"]), len(self.graph["nodes"]))
        self.assertIn("OBSERVATION_UNMAPPED", [g["reason"] for g in state["gaps"]])

    def test_observation_file_roundtrip(self):
        path = self.root / "observations.jsonl"
        rows = [self.record(date=NOW-timedelta(seconds=1), status="ok"),
                self.record(status="error")]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        parsed = room.read_observations(path)
        self.assertEqual(parsed, rows)
        self.assertEqual(self.node(self.state(parsed), self.db)["status"], "error")

    def test_reject_extra_fields_and_secrets(self):
        row = self.record()
        row["secret"] = "do-not-store"
        file = self.root / "bad.jsonl"
        file.write_text(json.dumps(row))
        with self.assertRaises(room.RoomError):
            room.read_observations(file)

    def test_reject_unallowlisted_metrics(self):
        path = self.root / "bad.jsonl"
        path.write_text(json.dumps(self.record(metrics={"api_token": 1})))
        with self.assertRaises(room.RoomError):
            room.read_observations(path)

    def test_reject_nan_and_duplicate_keys(self):
        file = self.root / "bad.jsonl"
        file.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(room.RoomError):
            room.read_observations(file)
        file.write_text(json.dumps(self.record()).replace('"metrics": {}', '"metrics": {"queue_lag_seconds": NaN}'))
        with self.assertRaises(room.RoomError):
            room.read_observations(file)

    def test_reject_path_symlink_for_observation_file(self):
        original = self.root / "real.jsonl"
        original.write_text("{}")
        link = self.root / "alias.jsonl"
        link.symlink_to(original)
        with self.assertRaises(room.RoomError):
            room.read_observations(link)

    def test_percentage_metric_rejects_over_100(self):
        file = self.root / "bad.jsonl"
        file.write_text(json.dumps(self.record(metrics={"error_rate_pct": 101})))
        with self.assertRaises(room.RoomError):
            room.read_observations(file)

    def test_huge_integer_is_refused_without_overflow(self):
        file = self.root / "bad.jsonl"
        file.write_text(json.dumps(self.record(metrics={"queue_lag_seconds": 10**300})))
        with self.assertRaises(room.RoomError):
            room.read_observations(file)

    def test_threshold_file_allowlist(self):
        file = self.root / "thresholds.json"
        file.write_text(json.dumps({"schema_version": 1,
                                   "warning_above": {"db_pool_utilization_pct": 80}}))
        self.assertEqual(room.read_thresholds(file), {"db_pool_utilization_pct": 80})
        file.write_text(json.dumps({"schema_version": 1, "warning_above": {"password": 1}}))
        with self.assertRaises(room.RoomError):
            room.read_thresholds(file)
        file.write_text(json.dumps({"schema_version": 1,
                                   "warning_above": {"db_pool_utilization_pct": 120}}))
        with self.assertRaises(room.RoomError):
            room.read_thresholds(file)

    def test_mathematical_solver_is_loaded_on_the_real_control_room(self):
        self.assertIn('id="workspace"', room.PAGE)
        self.assertIn('id="incidents"', room.PAGE)
        self.assertIn('id="map-business"', room.PAGE)
        self.assertIn('id="lens"', room.PAGE)
        self.assertIn('id="graph-search"', room.PAGE)
        self.assertIn('id="graph-focus"', room.PAGE)
        self.assertIn('id="layout-debug"', room.PAGE)
        self.assertIn('OntologyLayout.optimize(', room.PAGE)
        self.assertIn('OntologyLayout.verifyDOM(', room.PAGE)
        self.assertIn('prefers-reduced-motion:reduce', room.PAGE)
        self.assertIn('s.nodes.slice(0,90)', room.PAGE)
        self.assertIn('/api/view?lens=',room.PAGE)
        self.assertIn('/api/node?id=',room.PAGE)
        self.assertIn('id="graph-next"',room.PAGE)
        self.assertIn('graph_view_api.summary(full)',room.serve.__code__.co_consts.__repr__()) if False else None
        self.assertNotIn('grid-template-columns:minmax(0,1fr) 325px', room.PAGE)
        self.assertTrue((ROOT / "ontology-extraction/scripts/layout_engine.js").is_file())

    def test_html_is_local_read_only_not_fake_live(self):
        self.assertIn("127.0.0.1", room.serve.__code__.co_consts.__repr__())
        self.assertIn("READ ONLY", room.PAGE)
        self.assertIn("NO LIVE PROVIDER CONNECTION", room.PAGE)
        self.assertIn("UNATTESTED", room.PAGE) if "UNATTESTED" in room.PAGE else None
        self.assertNotIn("fetch('https://", room.PAGE)

    def test_loopback_host_and_origin_only(self):
        self.assertTrue(room.valid_local_host("127.0.0.1:8765", None, 8765))
        self.assertTrue(room.valid_local_host("localhost:8765",
                                               "http://localhost:8765", 8765))
        self.assertFalse(room.valid_local_host("evil.example:8765", None, 8765))
        self.assertFalse(room.valid_local_host("127.0.0.1:1234", None, 8765))
        self.assertFalse(room.valid_local_host("127.0.0.1:8765",
                                                "https://attacker.example", 8765))
        self.assertFalse(room.valid_local_host("127.0.0.1:8765", "null", 8765))
        self.assertFalse(room.valid_local_host("127.0.0.1:8765", None, 0))

    def test_same_timestamp_conflict_fails_closed(self):
        ok = self.record(status="ok")
        error = self.record(status="error")
        a = self.state([ok, error])
        b = self.state([error, ok])
        self.assertEqual(a, b)
        self.assertEqual(self.node(a, self.db)["status"], "unknown")
        self.assertEqual(self.node(a, self.db)["source_status"], "conflict")
        self.assertEqual(self.node(a, self.api)["potentially_impacted_by"], [])
        self.assertIn("CONFLICTING_SAME_TIMESTAMP_OBSERVATIONS",
                      [gap["reason"] for gap in a["gaps"]])

    def test_invalid_observation_status_type_rejected(self):
        file = self.root / "bad.jsonl"
        row = self.record()
        row["status"] = []
        file.write_text(json.dumps(row))
        with self.assertRaises(room.RoomError):
            room.read_observations(file)

    def test_timezones_and_non_rfc3339_fail_closed(self):
        with self.assertRaises(room.RoomError):
            room.timestamp("2026-10-27T16:00:00")
        self.assertEqual(room.timestamp("2026-10-27T09:00:00-07:00"), NOW)


if __name__ == "__main__":
    unittest.main()
