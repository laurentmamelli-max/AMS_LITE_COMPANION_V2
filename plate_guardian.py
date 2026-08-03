# -*- coding: utf-8 -*-
"""Safety-first plate monitoring primitives for AMS Lite Companion V2.

This module deliberately does not contain any printer command.  Bambu Studio
does not currently expose a documented LAN/SD-card API for skipping a single
object, so V2 records evidence and asks the user before any future actuator is
considered.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable


class GuardianError(ValueError):
    """Raised when a detector submits malformed or unsafe evidence."""


class PlateGuardian:
    """Persist visual observations and create reviewable failure proposals.

    An alert requires several *distinct* frames for the same object inside a
    short time window.  The result is only a proposal: no printer control is
    implemented here or elsewhere in the V2 core.
    """

    capability = {
        "status": "unsupported",
        "reason": (
            "Aucune commande Bambu LAN/SD fiable pour annuler un seul objet "
            "n'est validée dans V2."
        ),
    }
    defect_types = {"spaghetti", "stringing", "detachment", "warping", "extrusion_anomaly", "anomaly"}

    def __init__(
        self,
        path: Path,
        *,
        min_confidence: float = 0.88,
        required_frames: int = 3,
        evidence_window_seconds: float = 45.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not 0.0 < min_confidence <= 1.0:
            raise ValueError("La confiance minimale doit être comprise entre 0 et 1")
        if required_frames < 2:
            raise ValueError("Au moins deux images sont nécessaires")
        if evidence_window_seconds <= 0:
            raise ValueError("La fenêtre de preuve doit être positive")
        self.path = path
        self.min_confidence = min_confidence
        self.required_frames = required_frames
        self.evidence_window_seconds = evidence_window_seconds
        self.clock = clock
        self.lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS guardian_observations (
                    id INTEGER PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    object_id TEXT NOT NULL,
                    object_label TEXT NOT NULL,
                    defect_type TEXT NOT NULL DEFAULT 'anomaly',
                    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                    source TEXT NOT NULL,
                    frame_sha256 TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS guardian_observations_by_object
                    ON guardian_observations(object_id, observed_at DESC);
                CREATE TABLE IF NOT EXISTS guardian_proposals (
                    id TEXT PRIMARY KEY,
                    object_id TEXT NOT NULL,
                    object_label TEXT NOT NULL,
                    defect_type TEXT NOT NULL DEFAULT 'anomaly',
                    status TEXT NOT NULL CHECK(status IN ('pending_confirmation', 'continue', 'dismissed')),
                    confidence REAL NOT NULL,
                    evidence_count INTEGER NOT NULL,
                    first_observed_at REAL NOT NULL,
                    last_observed_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    decided_at REAL,
                    decision_note TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS guardian_proposals_by_status
                    ON guardian_proposals(status, created_at DESC);
                CREATE TABLE IF NOT EXISTS guardian_audit (
                    id INTEGER PRIMARY KEY,
                    proposal_id TEXT REFERENCES guardian_proposals(id),
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            observation_columns = {row["name"] for row in connection.execute("PRAGMA table_info(guardian_observations)")}
            proposal_columns = {row["name"] for row in connection.execute("PRAGMA table_info(guardian_proposals)")}
            if "defect_type" not in observation_columns:
                connection.execute("ALTER TABLE guardian_observations ADD COLUMN defect_type TEXT NOT NULL DEFAULT 'anomaly'")
            if "defect_type" not in proposal_columns:
                connection.execute("ALTER TABLE guardian_proposals ADD COLUMN defect_type TEXT NOT NULL DEFAULT 'anomaly'")

    @staticmethod
    def _text(value: Any, field: str, maximum: int) -> str:
        result = str(value or "").strip()
        if not result:
            raise GuardianError(f"{field} est requis")
        if len(result) > maximum:
            raise GuardianError(f"{field} est trop long")
        return result

    @staticmethod
    def _frame_hash(value: Any) -> str:
        result = str(value or "").strip().lower()
        if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
            raise GuardianError("frame_sha256 doit être une empreinte SHA-256")
        return result

    def observe(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record detector evidence and maybe return a pending human review."""
        object_id = self._text(payload.get("object_id"), "object_id", 120)
        object_label = self._text(payload.get("object_label") or object_id, "object_label", 120)
        source = self._text(payload.get("source", "camera"), "source", 40)
        defect_type = str(payload.get("defect_type") or "anomaly").strip().lower()
        if defect_type not in self.defect_types:
            raise GuardianError("defect_type inconnu")
        frame_sha256 = self._frame_hash(payload.get("frame_sha256"))
        try:
            confidence = float(payload.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise GuardianError("confidence doit être un nombre") from exc
        if not 0.0 <= confidence <= 1.0:
            raise GuardianError("confidence doit être comprise entre 0 et 1")
        observed_at = float(payload.get("observed_at", self.clock()))
        now = self.clock()
        if observed_at > now + 10 or observed_at < now - 24 * 60 * 60:
            raise GuardianError("Horodatage d'observation invalide")
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if not idempotency_key:
            # A detector may retry a frame with a slightly different timestamp.
            # The same image must never count twice toward a safety proposal.
            signature = f"{object_id}\n{defect_type}\n{frame_sha256}\n{source}"
            idempotency_key = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        if len(idempotency_key) > 160:
            raise GuardianError("idempotency_key est trop long")

        with self.lock, self._connect() as connection:
            try:
                connection.execute(
                    """INSERT INTO guardian_observations(
                           idempotency_key, object_id, object_label, defect_type, confidence,
                           source, frame_sha256, observed_at, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (idempotency_key, object_id, object_label, defect_type, confidence, source,
                     frame_sha256, observed_at, now),
                )
            except sqlite3.IntegrityError:
                return {"accepted": False, "duplicate": True, "proposal": None}
            proposal = self._maybe_create_proposal(connection, object_id, object_label, defect_type, now)
            return {"accepted": True, "duplicate": False, "proposal": proposal}

    def _maybe_create_proposal(
        self,
        connection: sqlite3.Connection,
        object_id: str,
        object_label: str,
        defect_type: str,
        now: float,
    ) -> dict[str, Any] | None:
        existing = connection.execute(
            """SELECT * FROM guardian_proposals
               WHERE object_id = ? AND defect_type = ? AND status = 'pending_confirmation'
               ORDER BY created_at DESC LIMIT 1""",
            (object_id, defect_type),
        ).fetchone()
        if existing:
            return self._proposal_dict(existing)
        rows = connection.execute(
            """SELECT confidence, frame_sha256, observed_at FROM guardian_observations
               WHERE object_id = ? AND defect_type = ? AND observed_at >= ? AND confidence >= ?
               ORDER BY observed_at DESC""",
            (object_id, defect_type, now - self.evidence_window_seconds, self.min_confidence),
        ).fetchall()
        unique_rows: list[sqlite3.Row] = []
        seen_frames: set[str] = set()
        for row in rows:
            if row["frame_sha256"] in seen_frames:
                continue
            seen_frames.add(row["frame_sha256"])
            unique_rows.append(row)
            if len(unique_rows) == self.required_frames:
                break
        if len(unique_rows) < self.required_frames:
            return None
        proposal_id = secrets.token_hex(16)
        ordered = sorted(unique_rows, key=lambda row: float(row["observed_at"]))
        confidence = sum(float(row["confidence"]) for row in ordered) / len(ordered)
        connection.execute(
            """INSERT INTO guardian_proposals(
                   id, object_id, object_label, defect_type, status, confidence, evidence_count,
                   first_observed_at, last_observed_at, created_at
               ) VALUES (?, ?, ?, ?, 'pending_confirmation', ?, ?, ?, ?, ?)""",
            (proposal_id, object_id, object_label, defect_type, confidence, len(ordered),
             float(ordered[0]["observed_at"]), float(ordered[-1]["observed_at"]), now),
        )
        detail = {
            "object_id": object_id,
            "defect_type": defect_type,
            "evidence_count": len(ordered),
            "frame_sha256": [row["frame_sha256"] for row in ordered],
            "capability": self.capability["status"],
        }
        connection.execute(
            "INSERT INTO guardian_audit(proposal_id, event_type, detail_json, created_at) VALUES (?, ?, ?, ?)",
            (proposal_id, "proposal_created", json.dumps(detail, sort_keys=True), now),
        )
        row = connection.execute("SELECT * FROM guardian_proposals WHERE id = ?", (proposal_id,)).fetchone()
        return self._proposal_dict(row)

    @staticmethod
    def _proposal_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "object_id": row["object_id"],
            "object_label": row["object_label"],
            "defect_type": row["defect_type"],
            "status": row["status"],
            "confidence": round(float(row["confidence"]), 4),
            "evidence_count": int(row["evidence_count"]),
            "first_observed_at": float(row["first_observed_at"]),
            "last_observed_at": float(row["last_observed_at"]),
            "created_at": float(row["created_at"]),
            "decided_at": float(row["decided_at"]) if row["decided_at"] is not None else None,
            "decision_note": row["decision_note"],
        }

    def decide(self, proposal_id: str, decision: str, note: str = "") -> dict[str, Any]:
        """Record a human decision.  This never sends a command to a printer."""
        proposal_id = self._text(proposal_id, "proposal_id", 80)
        decision = self._text(decision, "decision", 30)
        if decision not in {"continue", "dismiss"}:
            raise GuardianError("decision doit être continue ou dismiss")
        note = str(note or "").strip()[:500]
        now = self.clock()
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM guardian_proposals WHERE id = ?", (proposal_id,)).fetchone()
            if row is None:
                raise GuardianError("Proposition introuvable")
            if row["status"] != "pending_confirmation":
                return self._proposal_dict(row)
            connection.execute(
                """UPDATE guardian_proposals
                   SET status = ?, decision_note = ?, decided_at = ? WHERE id = ?""",
                (decision, note, now, proposal_id),
            )
            connection.execute(
                "INSERT INTO guardian_audit(proposal_id, event_type, detail_json, created_at) VALUES (?, ?, ?, ?)",
                (proposal_id, "human_decision", json.dumps({"decision": decision, "note": note}), now),
            )
            updated = connection.execute("SELECT * FROM guardian_proposals WHERE id = ?", (proposal_id,)).fetchone()
            return self._proposal_dict(updated)

    def state(self) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            pending = connection.execute(
                "SELECT * FROM guardian_proposals WHERE status = 'pending_confirmation' ORDER BY created_at DESC"
            ).fetchall()
            latest = connection.execute(
                "SELECT * FROM guardian_proposals ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            observations = connection.execute("SELECT COUNT(*) FROM guardian_observations").fetchone()[0]
            breakdown = connection.execute(
                """SELECT defect_type, status, COUNT(*) AS count
                   FROM guardian_proposals GROUP BY defect_type, status
                   ORDER BY defect_type, status"""
            ).fetchall()
        by_defect: dict[str, dict[str, int]] = {}
        for row in breakdown:
            defect = str(row["defect_type"])
            by_defect.setdefault(defect, {})[str(row["status"])] = int(row["count"])
        return {
            "mode": "observation_only",
            "capability": dict(self.capability),
            "observations_count": int(observations),
            "pending_proposals": [self._proposal_dict(row) for row in pending],
            "recent_proposals": [self._proposal_dict(row) for row in latest],
            "history_by_defect": by_defect,
        }
