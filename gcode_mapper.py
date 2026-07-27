"""Safe, best-effort object cartography for sliced G-code.

Object comments are optional and vary among slicer versions.  This parser only
returns objects explicitly delimited by comments; it never fabricates an
object-to-toolpath link when the source does not provide one.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


_START = re.compile(r"^\s*;\s*(?:OBJECT|PRINTING_OBJECT)\s*:\s*(.+?)\s*$", re.I)
_END = re.compile(r"^\s*;\s*(?:STOP_PRINTING_OBJECT|END_OBJECT)\s*:?\s*(.*?)\s*$", re.I)
_XY = re.compile(r"\b([XY])\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\b", re.I)


@dataclass
class _OpenObject:
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

    def close(self, end_line: int) -> dict[str, object]:
        bounds = None
        if None not in (self.min_x, self.max_x, self.min_y, self.max_y):
            bounds = {
                "min_x": round(float(self.min_x), 3), "max_x": round(float(self.max_x), 3),
                "min_y": round(float(self.min_y), 3), "max_y": round(float(self.max_y), 3),
            }
        return {
            "id": self.object_id, "label": self.label,
            "start_line": self.start_line, "end_line": end_line,
            "bounds_xy": bounds,
        }


def map_gcode_objects(text: str, *, max_objects: int = 200) -> list[dict[str, object]]:
    """Return explicit object regions and their observed XY envelope.

    Lines are one-based so a report can be compared directly to a G-code editor.
    The currently open region closes at EOF when the slicer omits an end marker.
    """
    objects: list[dict[str, object]] = []
    current: _OpenObject | None = None
    x = y = None
    identifiers: dict[str, int] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        start = _START.match(line)
        if start:
            if current:
                objects.append(current.close(line_number - 1))
            label = start.group(1).strip()[:120] or "Objet"
            base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "objet"
            identifiers[base] = identifiers.get(base, 0) + 1
            suffix = "" if identifiers[base] == 1 else f"-{identifiers[base]}"
            current = _OpenObject(base + suffix, label, line_number)
            if len(objects) >= max_objects:
                break
            continue
        if current:
            values = {axis.upper(): float(value) for axis, value in _XY.findall(line)}
            x = values.get("X", x)
            y = values.get("Y", y)
            if line.lstrip().upper().startswith(("G0", "G1", "G2", "G3")):
                current.point(x, y)
            if _END.match(line):
                objects.append(current.close(line_number))
                current = None
    if current and len(objects) < max_objects:
        objects.append(current.close(len(text.splitlines()) or 1))
    return objects


def object_map_summary(objects: Iterable[dict[str, object]]) -> dict[str, object]:
    entries = list(objects)
    return {
        "status": "mapped" if entries else "unavailable",
        "object_count": len(entries),
        "reason": "Balises d’objets trouvées dans le G-code" if entries else (
            "Le G-code ne contient pas de balises d’objets exploitables"
        ),
    }
