"""Safe object cartography for sliced G-code.

The mapper supports both generic slicer markers and the repeated Bambu Studio
``start/stop printing object`` sections.  It creates a single aggregate for a
Bambu object across every layer, keeping its exact toolpath line ranges and XY
envelope.  It never guesses an object when no explicit marker exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable


_GENERIC_START = re.compile(r"^\s*;\s*(?:OBJECT|PRINTING_OBJECT)\s*:\s*(.+?)\s*$", re.I)
_GENERIC_END = re.compile(r"^\s*;\s*(?:STOP_PRINTING_OBJECT|END_OBJECT)\s*:?\s*(.*?)\s*$", re.I)
_BAMBU_START = re.compile(
    r"^\s*;\s*start\s+printing\s+object\s*,\s*unique\s+label\s+id\s*:\s*(.+?)\s*$", re.I
)
_BAMBU_END = re.compile(
    r"^\s*;\s*stop\s+printing\s+object\s*,\s*unique\s+label\s+id\s*:\s*(.+?)\s*$", re.I
)
_XY = re.compile(r"\b([XY])\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\b", re.I)
_E = re.compile(r"\bE\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\b", re.I)


def _safe_identifier(value: str, fallback: str = "objet") -> str:
    return re.sub(r"[^a-z0-9._:-]+", "-", value.lower()).strip("-.")[:120] or fallback


@dataclass
class _OpenSegment:
    object_id: str
    label: str
    start_line: int
    min_x: float | None = None
    max_x: float | None = None
    min_y: float | None = None
    max_y: float | None = None

    def point(self, x: float | None, y: float | None) -> None:
        if x is not None:
            self.min_x = x if self.min_x is None else min(self.min_x, x)
            self.max_x = x if self.max_x is None else max(self.max_x, x)
        if y is not None:
            self.min_y = y if self.min_y is None else min(self.min_y, y)
            self.max_y = y if self.max_y is None else max(self.max_y, y)


@dataclass
class _ObjectAggregate:
    object_id: str
    label: str
    start_line: int
    end_line: int
    min_x: float | None = None
    max_x: float | None = None
    min_y: float | None = None
    max_y: float | None = None
    segment_count: int = 0
    line_ranges: list[dict[str, int]] = field(default_factory=list)

    def add(self, segment: _OpenSegment, end_line: int, *, max_ranges: int) -> None:
        self.segment_count += 1
        self.start_line = min(self.start_line, segment.start_line)
        self.end_line = max(self.end_line, end_line)
        for axis in ("x", "y"):
            low, high = getattr(segment, f"min_{axis}"), getattr(segment, f"max_{axis}")
            if low is not None:
                current_low = getattr(self, f"min_{axis}")
                current_high = getattr(self, f"max_{axis}")
                setattr(self, f"min_{axis}", low if current_low is None else min(current_low, low))
                setattr(self, f"max_{axis}", high if current_high is None else max(current_high, high))
        if len(self.line_ranges) < max_ranges:
            self.line_ranges.append({"start_line": segment.start_line, "end_line": end_line})

    def public(self, *, ranges_truncated: bool) -> dict[str, object]:
        bounds = None
        if None not in (self.min_x, self.max_x, self.min_y, self.max_y):
            bounds = {
                "min_x": round(float(self.min_x), 3), "max_x": round(float(self.max_x), 3),
                "min_y": round(float(self.min_y), 3), "max_y": round(float(self.max_y), 3),
            }
        return {
            "id": self.object_id,
            "label": self.label,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "bounds_xy": bounds,
            "segment_count": self.segment_count,
            "line_ranges": self.line_ranges,
            "line_ranges_truncated": ranges_truncated,
        }


def map_gcode_objects(text: str, *, max_objects: int = 200, max_ranges_per_object: int = 2000) -> list[dict[str, object]]:
    """Return explicitly identified object regions and their XY envelopes.

    Line numbers are one-based.  The line ranges are precise until the explicit
    safety cap; the cap prevents an unusually fragmented G-code from growing
    the persistent state without bound.
    """
    aggregates: dict[str, _ObjectAggregate] = {}
    truncated: set[str] = set()
    current: _OpenSegment | None = None
    x = y = None
    generic_identifiers: dict[str, int] = {}

    def close_current(end_line: int) -> None:
        nonlocal current
        if current is None:
            return
        aggregate = aggregates.get(current.object_id)
        if aggregate is None:
            if len(aggregates) >= max_objects:
                current = None
                return
            aggregate = _ObjectAggregate(current.object_id, current.label, current.start_line, end_line)
            aggregates[current.object_id] = aggregate
        before = len(aggregate.line_ranges)
        aggregate.add(current, max(current.start_line, end_line), max_ranges=max_ranges_per_object)
        if before == max_ranges_per_object:
            truncated.add(current.object_id)
        current = None

    def open_segment(object_id: str, label: str, line_number: int) -> None:
        nonlocal current
        close_current(line_number - 1)
        if object_id not in aggregates and len(aggregates) >= max_objects:
            return
        current = _OpenSegment(object_id, label, line_number)

    for line_number, line in enumerate(text.splitlines(), 1):
        bambu_start = _BAMBU_START.match(line)
        generic_start = _GENERIC_START.match(line)
        if bambu_start:
            raw_id = bambu_start.group(1).strip()[:120]
            if raw_id:
                open_segment(_safe_identifier(raw_id), f"Objet Bambu #{raw_id}", line_number)
            continue
        if generic_start:
            label = generic_start.group(1).strip()[:120] or "Objet"
            base = _safe_identifier(label)
            generic_identifiers[base] = generic_identifiers.get(base, 0) + 1
            suffix = "" if generic_identifiers[base] == 1 else f"-{generic_identifiers[base]}"
            open_segment(base + suffix, label, line_number)
            continue

        values = {axis.upper(): float(value) for axis, value in _XY.findall(line)}
        x, y = values.get("X", x), values.get("Y", y)
        # A Bambu object section also contains fast travel moves that cross
        # the whole plate before the nozzle starts depositing material.  Those
        # moves describe the head, not the printed object: including them made
        # many object envelopes overlap almost the entire build plate.
        #
        # Keep the global X/Y position up to date for partial coordinates, but
        # contribute a point only when the motion has a positive extrusion and
        # an explicit X or Y coordinate.  This covers both relative and
        # absolute extrusion G-code while excluding travel and retraction.
        motion = line.lstrip().upper().startswith(("G0", "G1", "G2", "G3"))
        extrusion = [float(value) for value in _E.findall(line)]
        if current and motion and ("X" in values or "Y" in values) and any(value > 0 for value in extrusion):
            current.point(x, y)

        bambu_end = _BAMBU_END.match(line)
        if bambu_end:
            close_current(line_number)
            continue
        if _GENERIC_END.match(line):
            close_current(line_number)

    close_current(len(text.splitlines()) or 1)
    return [aggregate.public(ranges_truncated=object_id in truncated) for object_id, aggregate in aggregates.items()]


def object_map_summary(objects: Iterable[dict[str, object]]) -> dict[str, object]:
    entries = list(objects)
    mapped_with_bounds = sum(1 for item in entries if item.get("bounds_xy") is not None)
    return {
        "status": "mapped" if entries else "unavailable",
        "object_count": len(entries),
        "objects_with_bounds": mapped_with_bounds,
        "reason": "Balises d’objets trouvées dans le G-code" if entries else (
            "Le G-code ne contient pas de balises d’objets exploitables"
        ),
    }
