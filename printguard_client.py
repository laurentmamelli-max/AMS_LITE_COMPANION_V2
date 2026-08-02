"""Small, dependency-free bridge to a local PrintGuard hub.

PrintGuard stays an independent GPL application.  Companion only uses its
documented loopback HTTP API and never gives it printer credentials or any
printer-control capability.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class PrintGuardError(RuntimeError):
    """A local PrintGuard service could not be reached or returned bad data."""


def _base_url(value: str) -> str:
    url = str(value or "http://127.0.0.1:8000").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise PrintGuardError("L’adresse PrintGuard doit commencer par http:// ou https://")
    return url


def _request(url: str, *, data: bytes | None = None, token: str = "", content_type: str = "") -> Any:
    headers = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read(512).decode("utf-8", "replace")
        raise PrintGuardError(f"PrintGuard a refusé la demande ({exc.code}) : {body or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PrintGuardError("PrintGuard local est indisponible. Lance son application puis réessaie.") from exc
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrintGuardError("Réponse PrintGuard invalide") from exc


def check(base_url: str, token: str = "") -> dict[str, Any]:
    """Return its documented state.  This is a read-only health check."""
    payload = _request(f"{_base_url(base_url)}/api/v1/state", token=token)
    if not isinstance(payload, dict):
        raise PrintGuardError("État PrintGuard invalide")
    return payload


def classify(jpeg: bytes, base_url: str, token: str = "", sensitivity: float = 1.0) -> dict[str, Any]:
    """Classify one already captured frame through PrintGuard's public API."""
    if not jpeg:
        raise PrintGuardError("Capture JPEG vide")
    sensitivity = max(0.1, min(4.0, float(sensitivity)))
    payload = _request(
        f"{_base_url(base_url)}/api/v1/classify?sensitivity={sensitivity:.2f}",
        data=jpeg,
        token=token,
        content_type="image/jpeg",
    )
    if not isinstance(payload, dict):
        raise PrintGuardError("Classification PrintGuard invalide")
    prediction = str(payload.get("prediction") or "unknown")
    if prediction not in {"success", "failure", "unknown"}:
        raise PrintGuardError("Prédiction PrintGuard invalide")
    try:
        score = float(payload.get("defect_score"))
        margin = float(payload.get("margin"))
    except (TypeError, ValueError) as exc:
        raise PrintGuardError("Scores PrintGuard invalides") from exc
    return {
        "prediction": prediction,
        "defect_score": max(0.0, min(1.0, score)),
        "margin": margin,
        "distances": payload.get("distances") if isinstance(payload.get("distances"), dict) else {},
    }
