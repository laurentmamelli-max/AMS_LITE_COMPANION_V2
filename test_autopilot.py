import tempfile
import unittest
from pathlib import Path

from autopilot import AutoPilotPlanner, skip_objects_payload


class AutoPilotTests(unittest.TestCase):
    def test_pending_alert_yields_a_preparable_plan_only_for_known_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AutoPilotPlanner(Path(tmp) / "autopilot.sqlite3").state(
                {"pending_proposals": [{"id": "p1", "object_id": "cube"}]},
                {"token": "print-1", "object_map": {"objects": [{
                    "id": "cube", "protocol_object_id": 944,
                    "protocol_identity": "slice_info.config", "bounds_xy": {"min_x": 1},
                }]}},
            )
        self.assertEqual("prepared_command_only", state["capability"]["mode"])
        self.assertFalse(state["capability"]["enabled"])
        self.assertTrue(state["plans"][0]["object_known"])
        self.assertEqual("ready_to_prepare", state["plans"][0]["status"])
        self.assertEqual([944], state["plans"][0]["request_preview"]["print"]["obj_list"])

    def test_unknown_object_remains_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AutoPilotPlanner(Path(tmp) / "autopilot.sqlite3").state(
                {"pending_proposals": [{"id": "p2", "object_id": "unknown"}]},
                {"token": "print-2", "object_map": {"objects": [{"id": "cube"}]}},
            )
        self.assertFalse(state["plans"][0]["object_known"])
        self.assertEqual("blocked_by_preflight", state["plans"][0]["status"])

    def test_rejects_empty_or_multiple_object_payloads(self):
        with self.assertRaises(ValueError):
            skip_objects_payload([])
        with self.assertRaises(ValueError):
            skip_objects_payload([944, 955])

    def test_prepares_an_idempotent_canonical_skip_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            planner = AutoPilotPlanner(Path(tmp) / "autopilot.sqlite3")
            guardian = {"pending_proposals": [{"id": "p3", "object_id": "944", "object_label": "Pièce"}]}
            job = {"token": "job-token", "object_map": {"objects": [{
                "id": "944", "protocol_object_id": 944, "protocol_identity": "slice_info.config",
                "bounds_xy": {"min_x": 1, "max_x": 2},
            }]}}
            prepared = planner.prepare("p3", guardian, job)
            repeated = planner.prepare("p3", guardian, job)
            self.assertEqual(prepared["id"], repeated["id"])
            self.assertEqual([944], prepared["command"]["print"]["obj_list"])
            self.assertEqual("skip_objects", prepared["command"]["print"]["command"])


if __name__ == "__main__":
    unittest.main()
