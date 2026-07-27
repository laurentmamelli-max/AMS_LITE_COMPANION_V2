from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from plate_guardian import GuardianError, PlateGuardian


def frame(number: int) -> str:
    return hashlib.sha256(f"frame-{number}".encode("utf-8")).hexdigest()


class PlateGuardianTests(unittest.TestCase):
    def guardian(self, directory: str, now: float = 1000.0) -> tuple[PlateGuardian, list[float]]:
        clock = [now]
        return PlateGuardian(Path(directory) / "guardian.sqlite3", clock=lambda: clock[0]), clock

    def observation(self, number: int, *, confidence: float = 0.94, observed_at: float | None = None) -> dict:
        value = {
            "object_id": "object-2",
            "object_label": "Porte-clés bleu",
            "confidence": confidence,
            "source": "simulation",
            "frame_sha256": frame(number),
        }
        if observed_at is not None:
            value["observed_at"] = observed_at
        return value

    def test_alert_requires_three_distinct_high_confidence_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            guardian, clock = self.guardian(directory)
            self.assertIsNone(guardian.observe(self.observation(1))["proposal"])
            clock[0] += 5
            self.assertIsNone(guardian.observe(self.observation(2))["proposal"])
            clock[0] += 5
            proposal = guardian.observe(self.observation(3))["proposal"]
            self.assertIsNotNone(proposal)
            self.assertEqual("pending_confirmation", proposal["status"])
            self.assertEqual(3, proposal["evidence_count"])
            self.assertEqual("unsupported", guardian.state()["capability"]["status"])

    def test_low_confidence_and_repeated_frame_do_not_create_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            guardian, clock = self.guardian(directory)
            self.assertIsNone(guardian.observe(self.observation(1, confidence=0.5))["proposal"])
            clock[0] += 1
            accepted = guardian.observe(self.observation(2))
            self.assertTrue(accepted["accepted"])
            clock[0] += 1
            duplicate = guardian.observe(self.observation(2))
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(2, guardian.state()["observations_count"])
            self.assertEqual([], guardian.state()["pending_proposals"])

    def test_different_defect_types_keep_separate_evidence_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            guardian, clock = self.guardian(directory)
            for index in range(3):
                guardian.observe({
                    "object_id": "cube", "object_label": "Cube", "defect_type": "warping",
                    "confidence": 0.95, "source": "camera", "frame_sha256": f"{index:064x}",
                    "observed_at": clock[0],
                })
                clock[0] += 1
            proposal = guardian.state()["pending_proposals"][0]
            self.assertEqual("warping", proposal["defect_type"])
            result = guardian.observe({
                "object_id": "cube", "object_label": "Cube", "defect_type": "spaghetti",
                "confidence": 0.99, "source": "camera", "frame_sha256": "f" * 64,
                "observed_at": clock[0],
            })
            self.assertIsNone(result["proposal"])

    def test_human_decision_is_persisted_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            guardian, clock = self.guardian(directory)
            for number in range(1, 4):
                guardian.observe(self.observation(number))
                clock[0] += 1
            proposal = guardian.state()["pending_proposals"][0]
            decided = guardian.decide(proposal["id"], "continue", "Vérifier dans cinq minutes")
            self.assertEqual("continue", decided["status"])
            reopened = PlateGuardian(Path(directory) / "guardian.sqlite3", clock=lambda: clock[0])
            self.assertEqual("continue", reopened.state()["recent_proposals"][0]["status"])
            self.assertEqual("continue", reopened.decide(proposal["id"], "dismiss")["status"])

    def test_invalid_frame_hash_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            guardian, _ = self.guardian(directory)
            with self.assertRaisesRegex(GuardianError, "SHA-256"):
                guardian.observe({**self.observation(1), "frame_sha256": "not-a-hash"})


if __name__ == "__main__":
    unittest.main()
