"""The synthetic replay must score only information available before an incident."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("early_warning_replay", ROOT / "evals/early_warning_replay.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


class EarlyWarningReplayTests(unittest.TestCase):
    def setUp(self):
        self.dataset = json.loads(replay.DATASET.read_text(encoding="utf-8"))

    def test_constructed_cases_show_tradeoff_and_missing_data(self):
        report = replay.run(self.dataset)
        self.assertEqual(report["summary"]["single_sample"],
                         {"hit": 2, "miss": 1, "false_alarm": 2, "no_alarm": 1, "insufficient_data": 2})
        self.assertEqual(report["summary"]["two_samples"],
                         {"hit": 1, "miss": 2, "false_alarm": 1, "no_alarm": 2, "insufficient_data": 2})
        persistent = next(c for c in report["cases"] if c["id"] == "persistent-before-failure")
        self.assertEqual(persistent["policies"]["single_sample"]["lead_seconds"], 300)
        self.assertEqual(persistent["policies"]["two_samples"]["lead_seconds"], 240)

    def test_future_readings_and_decisions_at_or_after_failure_are_excluded(self):
        case = next(c for c in self.dataset["cases"] if c["id"] == "abrupt-failure")
        case["decision_minutes"] = [0, 1, 2, 3, 4, 5, 6, 7]
        before = replay.replay_case(self.dataset, case)
        case["readings"].extend([[7, 1000], [8, 1000]])
        after = replay.replay_case(self.dataset, case)
        self.assertEqual(before, after)
        self.assertEqual(after["policies"]["two_samples"]["outcome"], "miss")
        failure = replay.room.timestamp(after["failure_at"])
        for decision in after["decisions"]:
            instant = replay.room.timestamp(decision["at"])
            self.assertLess(instant, failure)
            self.assertTrue(all(replay.room.timestamp(t) <= instant for t in decision["visible_observations"]))

    def test_single_reading_does_not_count_as_coverage_for_two_sample_rule(self):
        case = {"id": "one-reading", "readings": [[5, 700], [6, 950]], "failure_minute": 6}
        result = replay.replay_case(self.dataset, case)
        self.assertEqual(result["policies"]["single_sample"]["outcome"], "hit")
        self.assertEqual(result["policies"]["two_samples"]["outcome"], "insufficient_data")

    def test_gaps_after_initial_coverage_do_not_count_as_clean_or_missed(self):
        case = {"id": "telemetry-stopped", "readings": [[0, 300], [1, 300]], "failure_minute": 6}
        result = replay.replay_case(self.dataset, case)
        self.assertEqual(result["policies"]["single_sample"]["outcome"], "insufficient_data")
        self.assertEqual(result["policies"]["two_samples"]["outcome"], "insufficient_data")


if __name__ == "__main__":
    unittest.main()
