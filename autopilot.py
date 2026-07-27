"""Safety gate for the future single-object AutoPilot.

This module intentionally produces only auditable simulations.  No Bambu
printer-control primitive is imported or called from here: an actuator can be
connected only after its protocol has passed the documented validation gate.
"""

from __future__ import annotations

from typing import Any


class AutoPilotPlanner:
    capability = {
        "mode": "simulation_only",
        "enabled": False,
        "reason": (
            "L’exclusion d’un objet Bambu n’est pas activée tant qu’une commande "
            "documentée, idempotente et testée sur l’imprimante ciblée n’est pas disponible."
        ),
    }

    def state(self, guardian: dict[str, Any], active_job: dict[str, Any] | None) -> dict[str, Any]:
        object_map = (active_job or {}).get("object_map") or {}
        mapped = {
            str(item.get("id") or ""): item
            for item in object_map.get("objects", []) if isinstance(item, dict)
        } if isinstance(object_map, dict) else {}
        plans = []
        for proposal in guardian.get("pending_proposals", []):
            if not isinstance(proposal, dict):
                continue
            object_id = str(proposal.get("object_id") or "")
            plans.append({
                "proposal_id": proposal.get("id"),
                "object_id": object_id,
                "defect_type": proposal.get("defect_type", "anomaly"),
                "object_known": object_id in mapped,
                "bounds_xy": mapped.get(object_id, {}).get("bounds_xy"),
                "action": "simulation_exclusion_unitaire",
                "status": "blocked_by_safety_gate",
            })
        return {"capability": dict(self.capability), "plans": plans}
