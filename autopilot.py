"""V3 safety policy: alerts first, manual exclusion only on explicit request.

AutoPilot never publishes to a printer.  A user may explicitly prepare one
canonical Bambu ``skip_objects`` instruction from the dashboard, where it is
stored locally for review.  No popup, detector, timer or background task can
call that path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


def skip_objects_payload(object_ids: list[int], *, timestamp: int | None = None) -> dict[str, Any]:
    """Build one locally stored manual instruction, with no transport side effect."""
    ids = sorted({int(value) for value in object_ids})
    if not ids or any(value <= 0 for value in ids):
        raise ValueError("Au moins un identifiant d’objet Bambu positif est requis")
    if len(ids) != 1:
        raise ValueError("Une exclusion manuelle ne peut viser qu’un seul objet")
    return {
        "print": {
            "sequence_id": "0",
            "command": "skip_objects",
            "timestamp": int(time.time() if timestamp is None else timestamp),
            "obj_list": ids,
        }
    }


class AutoPilotPlanner:
    """Build reviewable alerts and opt-in manual exclusion instructions only."""

    capability = {
        "mode": "alert_only_with_manual_exclusion",
        "enabled": False,
        "manual_exclusion_available": True,
        "reason": (
            "V3 n’automatise aucune décision : les alertes demandent une vérification. "
            "L’exclusion ne peut être préparée que manuellement depuis le tableau de bord "
            "et n’est jamais envoyée par Companion."
        ),
    }

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS manual_exclusions (
                    id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE,
                    job_token TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    protocol_object_id INTEGER NOT NULL,
                    instruction_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('prepared_manually')),
                    created_at REAL NOT NULL
                );
                """
            )
            # V2.3 stored locally prepared, never-published instructions in
            # ``autopilot_plans``.  Preserve them when upgrading so the manual
            # audit history is not silently lost.
            legacy = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'autopilot_plans'"
            ).fetchone()
            if legacy:
                connection.execute(
                    """INSERT OR IGNORE INTO manual_exclusions(
                           id, proposal_id, job_token, object_id, protocol_object_id,
                           instruction_json, status, created_at
                       )
                       SELECT id, proposal_id, job_token, object_id, protocol_object_id,
                              command_json, 'prepared_manually', created_at
                       FROM autopilot_plans"""
                )

    @staticmethod
    def _mapped_objects(active_job: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        object_map = (active_job or {}).get("object_map") or {}
        items = object_map.get("objects", []) if isinstance(object_map, dict) else []
        return {
            str(item.get("id") or ""): item
            for item in items if isinstance(item, dict) and str(item.get("id") or "")
        }

    def _plan_for(self, proposal: dict[str, Any], active_job: dict[str, Any] | None) -> dict[str, Any]:
        object_id = str(proposal.get("object_id") or "")
        mapped = self._mapped_objects(active_job)
        item = mapped.get(object_id)
        protocol_id = item.get("protocol_object_id") if item else None
        valid_protocol_id = isinstance(protocol_id, int) and protocol_id > 0
        task_token = str((active_job or {}).get("token") or "")
        ready = bool(item and valid_protocol_id and task_token and not item.get("protocol_skipped"))
        preview = skip_objects_payload([protocol_id], timestamp=0) if valid_protocol_id else None
        return {
            "proposal_id": str(proposal.get("id") or ""),
            "object_id": object_id,
            "object_label": str(proposal.get("object_label") or (item or {}).get("label") or object_id),
            "defect_type": str(proposal.get("defect_type") or "anomaly"),
            "object_known": item is not None,
            "protocol_object_id": protocol_id if valid_protocol_id else None,
            "protocol_identity": (item or {}).get("protocol_identity", "unavailable"),
            "bounds_xy": (item or {}).get("bounds_xy"),
            "action": "manual_exclusion",
            "request_preview": preview,
            "status": "ready_for_manual_preparation" if ready else "blocked_by_preflight",
            "preflight": {
                "active_job": bool(task_token),
                "object_mapped": item is not None,
                "canonical_bambu_id": valid_protocol_id,
                "already_skipped": bool((item or {}).get("protocol_skipped")),
            },
        }

    @staticmethod
    def _alert_for(proposal: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "proposal_id": plan["proposal_id"],
            "object_id": plan["object_id"],
            "object_label": plan["object_label"],
            "defect_type": plan["defect_type"],
            "confidence": proposal.get("confidence"),
            "evidence_count": proposal.get("evidence_count"),
            "object_mapped": plan["object_known"],
            "status": "review_required",
            "action": "notify_only",
        }

    def _saved(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM manual_exclusions ORDER BY created_at DESC LIMIT 30"
            ).fetchall()
        return [{
            "id": row["id"], "proposal_id": row["proposal_id"], "job_token": row["job_token"],
            "object_id": row["object_id"], "protocol_object_id": int(row["protocol_object_id"]),
            "instruction": json.loads(row["instruction_json"]), "status": row["status"],
            "created_at": float(row["created_at"]),
        } for row in rows]

    def state(self, guardian: dict[str, Any], active_job: dict[str, Any] | None) -> dict[str, Any]:
        proposals = [item for item in guardian.get("pending_proposals", []) if isinstance(item, dict)]
        plans = [self._plan_for(item, active_job) for item in proposals]
        alerts = [self._alert_for(proposal, plan) for proposal, plan in zip(proposals, plans)]
        return {"capability": dict(self.capability), "alerts": alerts, "plans": plans, "prepared": self._saved()}

    def prepare_manual(self, proposal_id: str, guardian: dict[str, Any], active_job: dict[str, Any] | None) -> dict[str, Any]:
        """Persist an explicit user request; it deliberately does not control hardware."""
        proposal = next(
            (item for item in guardian.get("pending_proposals", [])
             if isinstance(item, dict) and str(item.get("id")) == proposal_id), None
        )
        if proposal is None:
            raise ValueError("Alerte introuvable ou déjà décidée")
        plan = self._plan_for(proposal, active_job)
        if plan["status"] != "ready_for_manual_preparation":
            raise ValueError("Préconditions de l’exclusion manuelle non satisfaites")
        job_token = str((active_job or {}).get("token"))
        protocol_id = int(plan["protocol_object_id"])
        instruction_id = hashlib.sha256(f"manual:{proposal_id}:{job_token}:{protocol_id}".encode()).hexdigest()[:32]
        instruction = skip_objects_payload([protocol_id])
        now = time.time()
        with self.lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM manual_exclusions WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO manual_exclusions(
                           id, proposal_id, job_token, object_id, protocol_object_id,
                           instruction_json, status, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'prepared_manually', ?)""",
                    (instruction_id, proposal_id, job_token, plan["object_id"], protocol_id,
                     json.dumps(instruction, separators=(",", ":"), sort_keys=True), now),
                )
                row = connection.execute(
                    "SELECT * FROM manual_exclusions WHERE id = ?", (instruction_id,)
                ).fetchone()
            else:
                row = existing
        return {
            "id": row["id"], "proposal_id": row["proposal_id"], "object_id": row["object_id"],
            "protocol_object_id": int(row["protocol_object_id"]),
            "instruction": json.loads(row["instruction_json"]), "status": row["status"],
            "created_at": float(row["created_at"]),
            "message": (
                "Exclusion manuelle préparée et journalisée. Companion ne l’a pas envoyée à l’imprimante."
            ),
        }
