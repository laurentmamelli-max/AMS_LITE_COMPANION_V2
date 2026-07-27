import unittest

from autopilot import AutoPilotPlanner


class AutoPilotTests(unittest.TestCase):
    def test_pending_alert_yields_a_simulated_plan_only_for_known_object(self):
        state = AutoPilotPlanner().state(
            {"pending_proposals": [{"id": "p1", "object_id": "cube"}]},
            {"object_map": {"objects": [{"id": "cube", "bounds_xy": {"min_x": 1}}]}},
        )
        self.assertEqual("simulation_only", state["capability"]["mode"])
        self.assertFalse(state["capability"]["enabled"])
        self.assertTrue(state["plans"][0]["object_known"])
        self.assertEqual("blocked_by_safety_gate", state["plans"][0]["status"])

    def test_unknown_object_remains_blocked(self):
        state = AutoPilotPlanner().state(
            {"pending_proposals": [{"id": "p2", "object_id": "unknown"}]},
            {"object_map": {"objects": [{"id": "cube"}]}},
        )
        self.assertFalse(state["plans"][0]["object_known"])


if __name__ == "__main__":
    unittest.main()
