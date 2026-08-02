# -*- coding: utf-8 -*-
"""Read-only comparison of multiple Bambu camera views from one layer.

The camera is attached to the moving print head.  Its pose changes between
captures, so a red difference map would imply a plate alignment we cannot
prove from every image.  The Centre Vision therefore presents the genuine
views side by side for human review; it never triggers a printer action.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any


class TemporalVisionError(RuntimeError):
    """A comparison cannot be made safely from the two local frames."""


def _opencv() -> tuple[Any, Any]:
    try:
        import cv2  # type: ignore[import-not-found]
        import numpy  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TemporalVisionError("Moteur Vision OpenCV non installé") from exc
    return cv2, numpy


def _side_by_side(reference: Any, candidate: Any, cv2: Any, numpy: Any) -> dict[str, Any]:
    """Return the two genuine views without implying a geometric alignment."""
    height, width = reference.shape[:2]
    max_width = 820
    scale = min(1.0, max_width / width)
    display_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    left = cv2.resize(reference, display_size, interpolation=cv2.INTER_AREA)
    right = cv2.resize(candidate, display_size, interpolation=cv2.INTER_AREA)
    cv2.putText(left, "Vue de reference", (14, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.78, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(right, "Autre point de vue", (14, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.78, (255, 255, 255), 2, cv2.LINE_AA)
    ok, encoded = cv2.imencode(".jpg", numpy.hstack([left, right]), [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise TemporalVisionError("Prévisualisation de comparaison impossible")
    return {
        "aligned": False, "matches": 0, "inliers": 0, "regions": [],
        "message": "Vues côte à côte : aucun rouge n’est affiché sans recalage fiable de la plaque.",
        "preview_data_url": "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii"),
    }


def compare(reference_path: Path, candidate_path: Path) -> dict[str, Any]:
    """Return two genuine views without claiming a geometric comparison."""
    cv2, numpy = _opencv()
    reference = cv2.imread(str(reference_path), cv2.IMREAD_COLOR)
    candidate = cv2.imread(str(candidate_path), cv2.IMREAD_COLOR)
    if reference is None or candidate is None:
        raise TemporalVisionError("Une des captures est illisible")
    return _side_by_side(reference, candidate, cv2, numpy)
