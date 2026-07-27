import tempfile
import unittest
import sqlite3
from pathlib import Path

from autopilot import AutoPilotPlanner, skip_objects_payload


class AutoPilotTests(unittest.TestCase):
    def test_confirmed_alert_is_review_only_but_has_an_explicit_manual_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AutoPilotPlanner(Path(tmp) / "autopilot.sqlite3").state(
                {"pending_proposals": [{"id": "p1", "object_id": "cube"}]},
                {"token": "print-1", "object_map": {"objects": [{
                    "id": "cube", "protocol_object_id": 944,
                    "protocol_identity": "slice_info.config", "bounds_xy": {"min_x": 1},
                }]}},
            )
        self.assertEqual("alert_only_with_manual_exclusion", state["capability"]["mode"])
        self.assertFalse(state["capability"]["enabled"])
        self.assertEqual("notify_only", state["alerts"][0]["action"])
        self.assertTrue(state["plans"][0]["object_known"])
        self.assertEqual("ready_for_manual_preparation", state["plans"][0]["status"])
        self.assertEqual([944], state["plans"][0]["request_preview"]["print"]["obj_list"])

    def test_unknown_object_remains_blocked_for_manual_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AutoPilotPlanner(Path(tmp) / "autopilot.sqlite3").state(
                {"pending_proposals": [{"id": "p2", "object_id": "unknown"}]},
                {"token": "print-2", "object_map": {"objects": [{"id": "cube"}]}},
            )
        self.assertFalse(state["plans"][0]["object_known"])
        self.assertEqual("blocked_by_preflight", state["plans"][0]["status"])

    def test_rejects_empty_or_multiple_object_manual_instructions(self):
        with self.assertRaises(ValueError):
            skip_objects_payload([])
        with self.assertRaises(ValueError):
            skip_objects_payload([944, 955])

    def test_explicit_manual_preparation_is_idempotent_and_never_a_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            planner = AutoPilotPlanner(Path(tmp) / "autopilot.sqlite3")
            guardian = {"pending_proposals": [{"id": "p3", "object_id": "944", "object_label": "Pièce"}]}
            job = {"token": "job-token", "object_map": {"objects": [{
                "id": "944", "protocol_object_id": 944, "protocol_identity": "slice_info.config",
                "bounds_xy": {"min_x": 1, "max_x": 2},
            }]}}
            prepared = planner.prepare_manual("p3", guardian, job)
            repeated = planner.prepare_manual("p3", guardian, job)
            self.assertEqual(prepared["id"], repeated["id"])
            self.assertEqual([944], prepared["instruction"]["print"]["obj_list"])
            self.assertEqual("prepared_manually", prepared["status"])
            self.assertIn("ne l’a pas envoyée", prepared["message"])

    def test_v23_local_manual_history_is_migrated_without_becoming_an_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autopilot.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE autopilot_plans (
                        id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE,
                        job_token TEXT NOT NULL, object_id TEXT NOT NULL,
                        protocol_object_id INTEGER NOT NULL, command_json TEXT NOT NULL,
                        status TEXT NOT NULL, created_at REAL NOT NULL
                    );
                    INSERT INTO autopilot_plans VALUES
                    ('legacy', 'proposal', 'job', '944', 944,
                     '{"print":{"obj_list":[944]}}', 'prepared', 1.0);
                    """
                )
            state = AutoPilotPlanner(path).state({"pending_proposals": []}, None)
        self.assertEqual("legacy", state["prepared"][0]["id"])
        self.assertEqual("prepared_manually", state["prepared"][0]["status"])
        self.assertEqual([944], state["prepared"][0]["instruction"]["print"]["obj_list"])


if __name__ == "__main__":
    unittest.main()
