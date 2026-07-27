"""Auditable single-object exclusion preparation for Bambu prints.

V2.3 prepares the exact local MQTT command but does not publish it.  A future
physical actuator must explicitly consume this journal after hardware
validation; planning alone never controls a printer.
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
    """Build the documented local MQTT request without any transport side effect."""
    ids = sorted({int(value) for value in object_ids})
    if not ids or any(value <= 0 for value in ids):
        raise ValueError("Au moins un identifiant d’objet Bambu positif est requis")
    if len(ids) != 1:
        raise ValueError("V2.3 prépare exclusivement une exclusion unitaire")
    return {
        "print": {
            "sequence_id": "0",
            "command": "skip_objects",
            "timestamp": int(time.time() if timestamp is None else timestamp),
            "obj_list": ids,
        }
    }


class AutoPilotPlanner:
    capability = {
        "mode": "prepared_command_only",
        "enabled": False,
        "reason": (
            "V2.3 prépare et journalise une exclusion unitaire, mais ne publie aucune "
            "commande tant que le protocole n’est pas validé sur l’imprimante ciblée."
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
                CREATE TABLE IF NOT EXISTS autopilot_plans (
                    id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE,
                    job_token TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    protocol_object_id INTEGER NOT NULL,
                    command_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('prepared')),
                    created_at REAL NOT NULL
                );
                """
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
        payload = skip_objects_payload([protocol_id], timestamp=0) if valid_protocol_id else None
        return {
            "proposal_id": proposal.get("id"),
            "object_id": object_id,
            "object_label": proposal.get("object_label") or (item or {}).get("label") or object_id,
            "defect_type": proposal.get("defect_type", "anomaly"),
            "object_known": item is not None,
            "protocol_object_id": protocol_id if valid_protocol_id else None,
            "protocol_identity": (item or {}).get("protocol_identity", "unavailable"),
            "bounds_xy": (item or {}).get("bounds_xy"),
            "action": "skip_objects",
            "request_preview": payload,
            "status": "ready_to_prepare" if ready else "blocked_by_preflight",
            "preflight": {
                "active_job": bool(task_token),
                "object_mapped": item is not None,
                "canonical_bambu_id": valid_protocol_id,
                "already_skipped": bool((item or {}).get("protocol_skipped")),
            },
        }

    def _saved(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM autopilot_plans ORDER BY created_at DESC LIMIT 30"
            ).fetchall()
        return [{
            "id": row["id"], "proposal_id": row["proposal_id"], "job_token": row["job_token"],
            "object_id": row["object_id"], "protocol_object_id": int(row["protocol_object_id"]),
            "command": json.loads(row["command_json"]), "status": row["status"],
            "created_at": float(row["created_at"]),
        } for row in rows]

    def state(self, guardian: dict[str, Any], active_job: dict[str, Any] | None) -> dict[str, Any]:
        plans = [self._plan_for(proposal, active_job)
                 for proposal in guardian.get("pending_proposals", []) if isinstance(proposal, dict)]
        return {"capability": dict(self.capability), "plans": plans, "prepared": self._saved()}

    def prepare(self, proposal_id: str, guardian: dict[str, Any], active_job: dict[str, Any] | None) -> dict[str, Any]:
        proposal = next(
            (item for item in guardian.get("pending_proposals", [])
             if isinstance(item, dict) and str(item.get("id")) == proposal_id), None
        )
        if proposal is None:
            raise ValueError("Proposition AutoPilot introuvable ou déjà décidée")
        plan = self._plan_for(proposal, active_job)
        if plan["status"] != "ready_to_prepare":
            raise ValueError("Préconditions d’exclusion unitaire non satisfaites")
        job_token = str((active_job or {}).get("token"))
        protocol_id = int(plan["protocol_object_id"])
        plan_id = hashlib.sha256(f"{proposal_id}:{job_token}:{protocol_id}".encode()).hexdigest()[:32]
        command = skip_objects_payload([protocol_id])
        now = time.time()
        with self.lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM autopilot_plans WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO autopilot_plans(
                           id, proposal_id, job_token, object_id, protocol_object_id,
                           command_json, status, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?)""",
                    (plan_id, proposal_id, job_token, plan["object_id"], protocol_id,
                     json.dumps(command, separators=(",", ":"), sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM autopilot_plans WHERE id = ?", (plan_id,)).fetchone()
            else:
                row = existing
        return {
            "id": row["id"], "proposal_id": row["proposal_id"], "object_id": row["object_id"],
            "protocol_object_id": int(row["protocol_object_id"]),
            "command": json.loads(row["command_json"]), "status": row["status"],
            "created_at": float(row["created_at"]),
            "message": "Exclusion unitaire préparée et journalisée ; aucune commande n’a été envoyée.",
        }
