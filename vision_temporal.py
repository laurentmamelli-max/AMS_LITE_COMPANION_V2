# -*- coding: utf-8 -*-
"""Read-only temporal comparison for successive Bambu camera captures.

The camera is attached to the moving print head, so raw pixels from two
captures cannot be compared directly.  This module first registers the plate
texture with ORB/RANSAC, then highlights only the regions that differ after
that registration.  It is deliberately evidence for a human review, never a
printer command or an object-exclusion decision.
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


def compare(reference_path: Path, candidate_path: Path) -> dict[str, Any]:
    """Align *candidate* to *reference* and return an inspectable comparison.

    The response contains a JPEG data URL with the reference on the left and
    the registered candidate (changed regions in red) on the right.
    """
    cv2, numpy = _opencv()
    reference = cv2.imread(str(reference_path), cv2.IMREAD_COLOR)
    candidate = cv2.imread(str(candidate_path), cv2.IMREAD_COLOR)
    if reference is None or candidate is None:
        raise TemporalVisionError("Une des captures est illisible")
    ref_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    candidate_gray = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY)
    orb = cv2.ORB_create(nfeatures=2800, fastThreshold=7)
    ref_points, ref_descriptors = orb.detectAndCompute(ref_gray, None)
    candidate_points, candidate_descriptors = orb.detectAndCompute(candidate_gray, None)
    if ref_descriptors is None or candidate_descriptors is None:
        raise TemporalVisionError("Pas assez de détails communs entre les deux vues")
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(candidate_descriptors, ref_descriptors, k=2)
    good = [first for first, second in pairs if first.distance < 0.72 * second.distance]
    if len(good) < 16:
        raise TemporalVisionError("Pas assez de repères communs pour recaler les deux vues")
    source = numpy.float32([candidate_points[item.queryIdx].pt for item in good]).reshape(-1, 1, 2)
    destination = numpy.float32([ref_points[item.trainIdx].pt for item in good]).reshape(-1, 1, 2)
    matrix, inlier_mask = cv2.findHomography(source, destination, cv2.RANSAC, 3.5)
    if matrix is None or inlier_mask is None:
        raise TemporalVisionError("Perspective commune introuvable entre les deux vues")
    inliers = int(numpy.count_nonzero(inlier_mask))
    if inliers < 12 or inliers / len(good) < 0.35:
        raise TemporalVisionError("Recalage insuffisamment fiable entre les deux vues")
    height, width = reference.shape[:2]
    aligned = cv2.warpPerspective(candidate, matrix, (width, height), flags=cv2.INTER_LINEAR)
    valid = cv2.warpPerspective(numpy.full(candidate.shape[:2], 255, dtype=numpy.uint8), matrix, (width, height))
    # Luma/chroma variation after geometric registration.  Exclude the outer
    # warp edge, retain substantial regions only and leave the judgment to the
    # person looking at the resulting red overlay.
    ref_lab = cv2.cvtColor(reference, cv2.COLOR_BGR2LAB).astype(numpy.int16)
    aligned_lab = cv2.cvtColor(aligned, cv2.COLOR_BGR2LAB).astype(numpy.int16)
    difference = numpy.mean(numpy.abs(ref_lab - aligned_lab), axis=2).astype(numpy.uint8)
    changed = ((difference >= 28) & (valid > 245)).astype(numpy.uint8) * 255
    changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, numpy.ones((3, 3), dtype=numpy.uint8))
    changed = cv2.morphologyEx(changed, cv2.MORPH_CLOSE, numpy.ones((7, 7), dtype=numpy.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(changed, 8)
    regions: list[dict[str, float]] = []
    retained = numpy.zeros_like(changed)
    for index in range(1, count):
        x, y, region_width, region_height, area = stats[index]
        if area < 180 or region_width < 10 or region_height < 10:
            continue
        retained[labels == index] = 255
        regions.append({
            "x": round(float(x) / width, 4), "y": round(float(y) / height, 4),
            "width": round(float(region_width) / width, 4),
            "height": round(float(region_height) / height, 4), "area_px": int(area),
        })
    overlay = cv2.addWeighted(reference, 0.58, aligned, 0.42, 0)
    overlay[retained > 0] = (35, 45, 230)
    max_width = 820
    scale = min(1.0, max_width / width)
    display_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    left = cv2.resize(reference, display_size, interpolation=cv2.INTER_AREA)
    right = cv2.resize(overlay, display_size, interpolation=cv2.INTER_AREA)
    cv2.putText(left, "Vue de reference", (14, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.78, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(right, "Vue recalee - changements en rouge", (14, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
    ok, encoded = cv2.imencode(".jpg", numpy.hstack([left, right]), [cv2.IMWRITE_JPEG_QUALITY, 86])
    if not ok:
        raise TemporalVisionError("Prévisualisation de comparaison impossible")
    return {
        "aligned": True, "matches": len(good), "inliers": inliers,
        "regions": regions[:30], "preview_data_url": "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii"),
    }
