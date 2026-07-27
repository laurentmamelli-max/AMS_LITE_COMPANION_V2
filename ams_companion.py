#!/usr/bin/env python3
"""AMS Lite Companion V2 - local filament usage tracker for Bambu printers.

Uses only the Python standard library.  It reads per-filament ``used_g`` from
a sliced Bambu/Orca .gcode.3mf and observes RUNNING -> FINISH over the
printer's local MQTT endpoint.  It never sends print commands.
"""

from __future__ import annotations

import argparse
import colorsys
import csv
from datetime import datetime
import hashlib
import io
import json
import os
import queue
import re
import secrets
import signal
import shutil
import socket
import sqlite3
import ssl
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from plate_guardian import PlateGuardian
from bambu_camera import CameraError, capture_jpeg, discover_certificate_sha256
from autopilot import AutoPilotPlanner
from gcode_mapper import map_gcode_objects, object_map_summary


APP_DIR = Path.home() / "Library" / "Application Support" / "AMS Lite Companion V2"
BAMBU_STUDIO_CONFIG = Path.home() / "Library" / "Application Support" / "BambuStudio" / "BambuStudio.conf"
STATE_FILE = APP_DIR / "state.json"
# Developers and the test suite may redirect diagnostic output without ever
# mixing simulated failures into the user's application log.  Production keeps
# the fixed, private path below because the override is unset.
_log_override = os.environ.get("AMS_COMPANION_LOG_FILE", "").strip()
LOG_FILE = Path(_log_override).expanduser() if _log_override else APP_DIR / "companion.log"
INVENTORY_FILE = APP_DIR / "inventory.sqlite3"
GUARDIAN_FILE = APP_DIR / "guardian.sqlite3"
EVENTS_FILE = APP_DIR / "events.sqlite3"
HOST, PORT = "127.0.0.1", 8766
__version__ = "2.2.0"
MAX_IMPORT_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 200
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 100
MAX_GCODE_OBJECT_MAP_BYTES = 24 * 1024 * 1024
# A human may prepare a job, then start it well after the 3MF is written by
# Bambu Studio. Keep that candidate available for a reasonable test/prepare
# window, while MQTT print commands remain short-lived below. The parsed
# metadata is persisted so Bambu Studio may safely remove its temporary file.
MAX_AUTO_IMPORT_AGE_SECONDS = 24 * 60 * 60
MAX_PRINT_REQUEST_AGE_SECONDS = 90
TERMINAL_OK = {"FINISH", "FINISHED", "COMPLETED", "COMPLETE"}
RUNNING = {"RUNNING", "PRINTING", "PREPARE", "PREPARING", "SLICING"}
TERMINAL_BAD = {"FAILED", "CANCEL", "CANCELLED", "CANCELED"}
TERMINAL_STATES = TERMINAL_OK | TERMINAL_BAD


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def capture_print_folder(name: str, task_id: str, started_at: str | None = None) -> str:
    """Return a predictable, filesystem-safe directory name for one print."""
    stamp = re.sub(r"[^0-9]", "", started_at or "")[:14] or time.strftime("%Y%m%d-%H%M%S")
    readable = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "impression"
    identity = re.sub(r"[^a-zA-Z0-9._-]+", "-", task_id).strip("-.")[:32] or "sans-id"
    return f"print-{stamp}-{identity}-{readable}"


def read_bambu_studio_credentials(path: Path = BAMBU_STUDIO_CONFIG) -> dict[str, str]:
    """Read the locally saved LAN identity without ever exposing its access code.

    Bambu Studio stores the selected printer serial in ``app`` and the LAN
    code in a serial-keyed dictionary.  This helper is deliberately local-only
    and returns data to the caller rather than logging it.
    """
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Configuration Bambu Studio introuvable ou illisible") from exc
    if not isinstance(saved, dict):
        raise ValueError("Configuration Bambu Studio invalide")
    app = saved.get("app") if isinstance(saved.get("app"), dict) else {}
    serial = str(app.get("user_last_selected_machine") or "").strip()
    codes = saved.get("user_access_code")
    if not isinstance(codes, dict):
        codes = saved.get("access_code")
    code = str(codes.get(serial) or "").strip() if isinstance(codes, dict) and serial else ""
    if not serial or not code:
        raise ValueError("Aucune imprimante LAN enregistrée dans Bambu Studio")
    return {"serial": serial, "access_code": code}


def secure_directory(path: Path) -> None:
    """Create the local data directory with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def log(message: str) -> None:
    line = f"{now_iso()} {message}\n"
    try:
        secure_directory(LOG_FILE.parent)
        with LOG_FILE.open("a", encoding="utf-8") as out:
            out.write(line)
        os.chmod(LOG_FILE, 0o600)
    except OSError:
        # Tests and read-only recovery environments may not expose a writable
        # macOS home directory. Runtime state still uses its explicit path.
        pass
    print(line, end="", flush=True)


def default_state() -> dict[str, Any]:
    return {
        "version": 2,
        "config": {"ip": "", "serial": "", "access_code": ""},
        "camera": {
            "enabled": False,
            "certificate_sha256": "",
            "capture_every_layers": 5,
            "last_seen_layer": 0,
            "last_requested_layer": 0,
            "status": "Caméra non configurée",
        },
        "spools": {
            str(i): {"name": f"Bobine A{i}", "initial_g": 1000.0, "remaining_g": 1000.0}
            for i in range(1, 5)
        },
        "armed_job": None,
        "active_job": None,
        "accounted": [],
        "history": [],
        "printer": {"connected": False, "state": "INCONNU", "progress": 0, "job": "",
                    "rfid_status": "En attente de lecture RFID"},
        "bridge": {
            "enabled": True,
            "fallback_enabled": True,
            "default_mapping": {str(i): str(i) for i in range(1, 5)},
            "status": "En attente de Bambu Studio",
            "last_file": "",
            "last_sha256": "",
            "last_detected_at": "",
            "mapping_source": "",
            "request_capture": False,
            "mapping_confirmation_required": False,
            "mapping_conflict": [],
        },
        "recovery_notice": "",
    }


def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    state = default_state()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            for key in state:
                if key not in loaded:
                    continue
                if key == "bridge" and isinstance(loaded[key], dict):
                    state[key].update(loaded[key])
                    defaults = default_state()["bridge"]["default_mapping"]
                    defaults.update(state[key].get("default_mapping", {}))
                    state[key]["default_mapping"] = defaults
                else:
                    state[key] = loaded[key]
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{path.stem}.corrompu-{stamp}{path.suffix}")
            index = 2
            while backup.exists():
                backup = path.with_name(f"{path.stem}.corrompu-{stamp}-{index}{path.suffix}")
                index += 1
            try:
                os.replace(path, backup)
                os.chmod(backup, 0o600)
                state["recovery_notice"] = (
                    f"État illisible sauvegardé dans {backup.name}. "
                    "La configuration doit être vérifiée avant utilisation."
                )
                log(f"État illisible sauvegardé: {backup.name} ({exc})")
            except OSError as backup_exc:
                state["recovery_notice"] = "État illisible: aucune donnée n’a été écrasée."
                log(f"État illisible, sauvegarde impossible: {backup_exc}")
    return state


def atomic_save(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    secure_directory(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def inventory_path_for_state(state_path: Path) -> Path:
    """Keep test and portable state files isolated from the real inventory."""
    return INVENTORY_FILE if state_path == STATE_FILE else state_path.with_name("inventory.sqlite3")


def guardian_path_for_state(state_path: Path) -> Path:
    """Keep a test instance's guardian journal out of the real V2 data."""
    return GUARDIAN_FILE if state_path == STATE_FILE else state_path.with_name("guardian.sqlite3")


def events_path_for_state(state_path: Path) -> Path:
    """Keep the durable MQTT audit trail beside the state it describes."""
    return EVENTS_FILE if state_path == STATE_FILE else state_path.with_name("events.sqlite3")


class EventJournal:
    """Small, local-only audit journal for MQTT print reports.

    The raw MQTT payload is deliberately never persisted: reports can vary by
    firmware and may contain fields that are irrelevant to an audit.  The
    compact summary is written before Companion changes local state, then
    marked processed (or failed) afterwards.
    """

    MAX_EVENTS = 2_000

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        secure_directory(self.path.parent)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return connection

    def initialize(self) -> None:
        with self.lock, self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS mqtt_events (
                    id TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '',
                    layer INTEGER,
                    progress INTEGER,
                    job TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT 'received',
                    processed_at TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT ''
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS mqtt_events_received ON mqtt_events(received_at DESC)"
            )

    def record(self, payload: dict[str, Any]) -> str | None:
        report = payload.get("print")
        if not isinstance(report, dict):
            return None
        raw_state = report.get("gcode_state") or report.get("print_status") or ""
        try:
            layer = int(float(report.get("layer_num", report.get("layer", report.get("current_layer", 0)))))
        except (TypeError, ValueError):
            layer = None
        try:
            progress = max(0, min(100, int(_float(report.get("mc_percent", 0)))))
        except (TypeError, ValueError):
            progress = None
        event_id = secrets.token_hex(16)
        values = (
            event_id, now_iso(),
            str(report.get("subtask_id") or report.get("task_id") or ""),
            str(raw_state).upper(), layer, progress,
            str(report.get("subtask_name") or report.get("gcode_file") or "")[:240],
        )
        with self.lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO mqtt_events(id, received_at, task_id, state, layer, progress, job)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""", values
            )
            connection.execute(
                """DELETE FROM mqtt_events WHERE id IN (
                    SELECT id FROM mqtt_events ORDER BY received_at DESC LIMIT -1 OFFSET ?
                )""", (self.MAX_EVENTS,)
            )
        return event_id

    def mark(self, event_id: str | None, outcome: str, detail: str = "") -> None:
        if not event_id:
            return
        with self.lock, self._connect() as connection:
            connection.execute(
                "UPDATE mqtt_events SET outcome = ?, processed_at = ?, detail = ? WHERE id = ?",
                (outcome, now_iso(), detail[:240], event_id),
            )

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT received_at, task_id, state, layer, progress, job, outcome, processed_at, detail
                   FROM mqtt_events ORDER BY received_at DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


class Inventory:
    """Durable spool catalogue and the four temporary AMS assignments."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        secure_directory(self.path.parent)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return connection

    def initialize(self, legacy_state: dict[str, Any]) -> None:
        legacy_spools = legacy_state.get("spools", legacy_state)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS spools (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    material TEXT NOT NULL DEFAULT '',
                    brand TEXT NOT NULL DEFAULT '',
                    color TEXT NOT NULL DEFAULT '',
                    rfid_tag TEXT NOT NULL DEFAULT '',
                    rfid_info TEXT NOT NULL DEFAULT '',
                    storage_location TEXT NOT NULL DEFAULT '',
                    low_stock_g REAL NOT NULL DEFAULT 100,
                    cost_eur REAL NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    initial_g REAL NOT NULL,
                    remaining_g REAL NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS slot_assignments (
                    slot TEXT PRIMARY KEY CHECK(slot IN ('1', '2', '3', '4')),
                    spool_id INTEGER NOT NULL UNIQUE REFERENCES spools(id),
                    assigned_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inventory_history (
                    id INTEGER PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    spool_id INTEGER REFERENCES spools(id),
                    slot TEXT,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS print_settlements (
                    settlement_key TEXT PRIMARY KEY,
                    deductions_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS legacy_history_imports (
                    legacy_key TEXT PRIMARY KEY
                );
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(spools)")}
            if "rfid_tag" not in columns:
                connection.execute("ALTER TABLE spools ADD COLUMN rfid_tag TEXT NOT NULL DEFAULT ''")
            if "rfid_info" not in columns:
                connection.execute("ALTER TABLE spools ADD COLUMN rfid_info TEXT NOT NULL DEFAULT ''")
            if "storage_location" not in columns:
                connection.execute("ALTER TABLE spools ADD COLUMN storage_location TEXT NOT NULL DEFAULT ''")
            if "low_stock_g" not in columns:
                connection.execute("ALTER TABLE spools ADD COLUMN low_stock_g REAL NOT NULL DEFAULT 100")
            if "cost_eur" not in columns:
                connection.execute("ALTER TABLE spools ADD COLUMN cost_eur REAL NOT NULL DEFAULT 0")
            if "notes" not in columns:
                connection.execute("ALTER TABLE spools ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS spools_rfid_tag ON spools(rfid_tag) WHERE rfid_tag != ''"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS spools_catalog_filter ON spools(archived, material, brand, storage_location)"
            )
            # Early builds stored the Bambu RGB value (for example #C12E1F).
            # The catalogue is user-facing, so migrate those values to a
            # readable French colour name as soon as it opens.
            for row in connection.execute("SELECT id, color FROM spools WHERE color GLOB '#[0-9A-Fa-f]*'"):
                connection.execute("UPDATE spools SET color = ? WHERE id = ?", (rfid_color(row["color"]), row["id"]))
            # Earlier RFID imports kept opaque machine labels such as A01-W2.
            # Convert those existing records immediately; later RFID reports
            # still preserve any name the user has chosen themselves.
            for row in connection.execute("SELECT id, name, material, color FROM spools WHERE archived = 0"):
                suggested = descriptive_spool_name(str(row["material"]), str(row["color"]))
                if suggested and is_machine_spool_name(str(row["name"])):
                    connection.execute("UPDATE spools SET name = ?, updated_at = ? WHERE id = ?", (suggested, now_iso(), row["id"]))
            if not connection.execute("SELECT COUNT(*) FROM spools").fetchone()[0]:
                for slot in map(str, range(1, 5)):
                    legacy = legacy_spools.get(slot, {})
                    name = str(legacy.get("name") or f"Bobine A{slot}")[:80]
                    initial_g = max(0.0, _float(legacy.get("initial_g", 1000)))
                    remaining_g = max(0.0, _float(legacy.get("remaining_g", initial_g)))
                    created_at = now_iso()
                    cursor = connection.execute(
                        """
                        INSERT INTO spools(name, initial_g, remaining_g, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (name, initial_g, remaining_g, created_at, created_at),
                    )
                    spool_id = int(cursor.lastrowid)
                    connection.execute(
                        "INSERT INTO slot_assignments(slot, spool_id, assigned_at) VALUES (?, ?, ?)",
                        (slot, spool_id, created_at),
                    )
                    connection.execute(
                        """
                        INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at)
                        VALUES ('migration', ?, ?, 'Bobine existante importée depuis state.json', ?)
                        """,
                        (spool_id, slot, created_at),
                    )
            self._import_legacy_history(connection, legacy_state.get("history", []))

    @staticmethod
    def _import_legacy_history(connection: sqlite3.Connection, legacy_history: Any) -> None:
        """Backfill per-spool history from the pre-catalogue state.json log."""
        if not isinstance(legacy_history, list):
            return
        migration_rows = connection.execute(
            "SELECT spool_id, slot, created_at FROM inventory_history WHERE event_type = 'migration'"
        ).fetchall()
        if not migration_rows:
            return
        first_catalogue_at = min(str(row["created_at"]) for row in migration_rows)
        legacy_slots = {str(row["slot"]): int(row["spool_id"]) for row in migration_rows if row["slot"]}
        spool_ids = {int(row["id"]) for row in connection.execute("SELECT id FROM spools")}
        for job in legacy_history:
            if not isinstance(job, dict):
                continue
            occurred_at = str(job.get("ended_at") or job.get("started_at") or job.get("armed_at") or "")
            # New catalogue events are already recorded in SQLite. Only import
            # the log that predates the catalogue migration.
            if not occurred_at or occurred_at >= first_catalogue_at:
                continue
            legacy_key = "legacy:" + str(job.get("token") or job.get("task_id") or occurred_at)
            if connection.execute(
                "SELECT 1 FROM legacy_history_imports WHERE legacy_key = ?", (legacy_key,)
            ).fetchone():
                continue
            deductions = job.get("deductions") if isinstance(job.get("deductions"), list) else []
            if not deductions and job.get("deducted"):
                deductions = job.get("lines") if isinstance(job.get("lines"), list) else []
            for deduction in deductions:
                if not isinstance(deduction, dict):
                    continue
                slot = str(deduction.get("slot") or "")
                raw_spool_id = deduction.get("spool_id")
                try:
                    spool_id = int(raw_spool_id)
                except (TypeError, ValueError):
                    spool_id = legacy_slots.get(slot)
                if spool_id not in spool_ids:
                    spool_id = legacy_slots.get(slot)
                if spool_id not in spool_ids:
                    continue
                used_g = max(0.0, _float(deduction.get("used_g")))
                before = deduction.get("before_g")
                after = deduction.get("after_g")
                detail = f"Historique importé · -{round(used_g, 3)} g"
                if before is not None and after is not None:
                    detail += f" · {round(_float(before), 3)} → {round(_float(after), 3)} g"
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('deduct', ?, ?, ?, ?)",
                    (spool_id, slot or None, detail, occurred_at),
                )
                connection.execute(
                    "UPDATE spools SET created_at = MIN(created_at, ?) WHERE id = ?",
                    (occurred_at, spool_id),
                )
            connection.execute(
                "INSERT INTO legacy_history_imports(legacy_key) VALUES (?)", (legacy_key,))

    @staticmethod
    def _spool_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "material": row["material"],
            "brand": row["brand"],
            "color": row["color"],
            "rfid_tag": row["rfid_tag"],
            "rfid_info": row["rfid_info"],
            "storage_location": row["storage_location"],
            "low_stock_g": _float(row["low_stock_g"]),
            "cost_eur": _float(row["cost_eur"]),
            "notes": row["notes"],
            "initial_g": row["initial_g"],
            "remaining_g": row["remaining_g"],
            "archived": bool(row["archived"]),
            "slot": row["slot"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def public_state(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT spools.*, slot_assignments.slot
                FROM spools
                LEFT JOIN slot_assignments ON slot_assignments.spool_id = spools.id
                WHERE spools.archived = 0
                ORDER BY spools.id DESC
                """
            ).fetchall()
        spools = [self._spool_dict(row) for row in rows]
        return {
            "spools": spools,
            "slots": {spool["slot"]: spool["id"] for spool in spools if spool["slot"]},
        }

    def summary(self) -> list[dict[str, Any]]:
        """User-facing usage totals for the catalogue overview."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT spools.id, spools.name, spools.initial_g, spools.remaining_g,
                       slot_assignments.slot,
                       SUM(CASE WHEN inventory_history.event_type = 'deduct' THEN 1 ELSE 0 END) AS print_count,
                       MAX(CASE WHEN inventory_history.event_type = 'deduct' THEN inventory_history.created_at END) AS last_used_at
                FROM spools
                LEFT JOIN slot_assignments ON slot_assignments.spool_id = spools.id
                LEFT JOIN inventory_history ON inventory_history.spool_id = spools.id
                WHERE spools.archived = 0
                GROUP BY spools.id
                ORDER BY spools.name COLLATE NOCASE, spools.id
                """
            ).fetchall()
        return [{
            "id": int(row["id"]), "name": row["name"], "slot": row["slot"],
            "initial_g": _float(row["initial_g"]), "remaining_g": _float(row["remaining_g"]),
            "print_count": int(row["print_count"] or 0), "last_used_at": row["last_used_at"],
        } for row in rows]

    def catalog_overview(self) -> dict[str, Any]:
        """Small aggregate payload for a large catalogue dashboard."""
        with self._connect() as connection:
            totals = connection.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(remaining_g), 0) AS remaining_g,
                       COALESCE(SUM(initial_g), 0) AS initial_g,
                       COALESCE(SUM(CASE WHEN remaining_g <= low_stock_g THEN 1 ELSE 0 END), 0) AS low_stock,
                       COALESCE(SUM(CASE WHEN storage_location = '' THEN 1 ELSE 0 END), 0) AS unlocated
                FROM spools WHERE archived = 0
                """
            ).fetchone()
            locations = connection.execute(
                """
                SELECT storage_location, COUNT(*) AS count FROM spools
                WHERE archived = 0 AND storage_location != ''
                GROUP BY storage_location ORDER BY count DESC, storage_location COLLATE NOCASE LIMIT 12
                """
            ).fetchall()
        return {
            "count": int(totals["count"]), "remaining_g": _float(totals["remaining_g"]),
            "initial_g": _float(totals["initial_g"]), "low_stock": int(totals["low_stock"]),
            "unlocated": int(totals["unlocated"]),
            "locations": [{"name": row["storage_location"], "count": int(row["count"])} for row in locations],
        }

    def slot_spools(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT spools.*, slot_assignments.slot
                FROM slot_assignments
                JOIN spools ON spools.id = slot_assignments.spool_id
                WHERE spools.archived = 0
                """
            ).fetchall()
        return {str(row["slot"]): self._spool_dict(row) for row in rows}

    def create_spool(self, data: dict[str, Any]) -> dict[str, Any]:
        material = str(data.get("material", "")).strip()[:40]
        color = str(data.get("color", "")).strip()[:40]
        name = str(data.get("name", "")).strip()[:80] or descriptive_spool_name(material, color)
        if not name:
            raise ValueError("Indiquez au moins la matière ou la couleur de la bobine")
        initial_g = max(0.0, _float(data.get("initial_g", 1000)))
        remaining_g = max(0.0, _float(data.get("remaining_g", initial_g)))
        created_at = history_date_iso(data.get("created_at"))
        values = (
            name,
            material,
            str(data.get("brand", "")).strip()[:60],
            color,
            str(data.get("storage_location", "")).strip()[:80],
            max(0.0, _float(data.get("low_stock_g", 100))),
            max(0.0, _float(data.get("cost_eur", 0))),
            str(data.get("notes", "")).strip()[:500],
            initial_g,
            remaining_g,
            created_at,
            created_at,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO spools(name, material, brand, color, storage_location, low_stock_g, cost_eur, notes,
                                   initial_g, remaining_g, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            spool_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO inventory_history(event_type, spool_id, detail, created_at) VALUES ('create', ?, ?, ?)",
                (spool_id, "Nouvelle bobine", created_at),
            )
        return self.spool(spool_id)

    def spool(self, spool_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT spools.*, slot_assignments.slot
                FROM spools LEFT JOIN slot_assignments ON slot_assignments.spool_id = spools.id
                WHERE spools.id = ? AND spools.archived = 0
                """,
                (spool_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Bobine introuvable")
        return self._spool_dict(row)

    def history_for_spool(self, spool_id: int) -> dict[str, Any]:
        spool = self.spool(spool_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_type, slot, detail, created_at
                FROM inventory_history WHERE spool_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (spool_id,),
            ).fetchall()
        return {
            "spool": spool,
            "events": [{
                "id": row["id"], "type": row["event_type"], "slot": row["slot"],
                "detail": row["detail"], "created_at": row["created_at"],
            } for row in rows],
        }

    def update_spool(self, spool_id: int, data: dict[str, Any]) -> dict[str, Any]:
        current = self.spool(spool_id)
        material = str(data.get("material", current["material"])).strip()[:40]
        color = str(data.get("color", current["color"])).strip()[:40]
        name = str(data.get("name", current["name"])).strip()[:80]
        if not name:
            raise ValueError("Donnez un nom à la bobine")
        initial_g = max(0.0, _float(data.get("initial_g", current["initial_g"])))
        remaining_g = max(0.0, _float(data.get("remaining_g", current["remaining_g"])))
        created_at = history_date_iso(data["created_at"]) if "created_at" in data else current["created_at"]
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE spools
                SET name = ?, material = ?, brand = ?, color = ?, storage_location = ?, low_stock_g = ?, cost_eur = ?, notes = ?,
                    initial_g = ?, remaining_g = ?, created_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    material,
                    str(data.get("brand", current["brand"])).strip()[:60],
                    color,
                    str(data.get("storage_location", current["storage_location"])).strip()[:80],
                    max(0.0, _float(data.get("low_stock_g", current["low_stock_g"]))),
                    max(0.0, _float(data.get("cost_eur", current["cost_eur"]))),
                    str(data.get("notes", current["notes"])).strip()[:500],
                    initial_g,
                    remaining_g,
                    created_at,
                    now_iso(),
                    spool_id,
                ),
            )
            if "created_at" in data:
                first_event = connection.execute(
                    "SELECT id FROM inventory_history WHERE spool_id = ? ORDER BY id ASC LIMIT 1",
                    (spool_id,),
                ).fetchone()
                if first_event is not None:
                    connection.execute(
                        "UPDATE inventory_history SET created_at = ? WHERE id = ?",
                        (created_at, first_event["id"]),
                    )
        return self.spool(spool_id)

    def delete_spool(self, spool_id: int) -> dict[str, Any]:
        """Permanently remove a spool and every event attached to it."""
        with self._connect() as connection:
            spool = connection.execute(
                "SELECT id, name FROM spools WHERE id = ? AND archived = 0", (spool_id,)
            ).fetchone()
            if spool is None:
                raise ValueError("Bobine introuvable")
            assignment = connection.execute(
                "SELECT slot FROM slot_assignments WHERE spool_id = ?", (spool_id,)
            ).fetchone()
            connection.execute("DELETE FROM slot_assignments WHERE spool_id = ?", (spool_id,))
            connection.execute("DELETE FROM inventory_history WHERE spool_id = ?", (spool_id,))
            connection.execute("DELETE FROM spools WHERE id = ?", (spool_id,))
        return {"message": f"{spool['name']} et son historique ont été supprimés."}

    def archive_spools(self, spool_ids: list[int]) -> dict[str, Any]:
        """Hide inactive stock without destroying its audit trail."""
        ids = sorted({int(spool_id) for spool_id in spool_ids if int(spool_id) > 0})
        if not ids:
            raise ValueError("Sélectionnez au moins une bobine")
        marks = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT id, name FROM spools WHERE archived = 0 AND id IN ({marks})", ids
            ).fetchall()
            if not rows:
                raise ValueError("Aucune bobine active dans la sélection")
            connection.execute(f"DELETE FROM slot_assignments WHERE spool_id IN ({marks})", ids)
            connection.execute(
                f"UPDATE spools SET archived = 1, updated_at = ? WHERE id IN ({marks})", [now_iso(), *ids]
            )
            for row in rows:
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, detail, created_at) VALUES ('archive', ?, ?, ?)",
                    (row["id"], "Bobine archivée depuis le catalogue", now_iso()),
                )
        return {"count": len(rows), "message": f"{len(rows)} bobine(s) archivée(s), historique conservé."}

    def bulk_update(self, data: dict[str, Any]) -> dict[str, Any]:
        ids = data.get("ids")
        if not isinstance(ids, list):
            raise ValueError("Sélection invalide")
        spool_ids = sorted({int(value) for value in ids if str(value).isdigit()})
        if not spool_ids:
            raise ValueError("Sélectionnez au moins une bobine")
        action = str(data.get("action", ""))
        if action == "archive":
            return self.archive_spools(spool_ids)
        if action not in {"location", "threshold"}:
            raise ValueError("Action de lot inconnue")
        marks = ",".join("?" for _ in spool_ids)
        now = now_iso()
        with self._connect() as connection:
            if action == "location":
                location = str(data.get("storage_location", "")).strip()[:80]
                connection.execute(
                    f"UPDATE spools SET storage_location = ?, updated_at = ? WHERE archived = 0 AND id IN ({marks})",
                    [location, now, *spool_ids],
                )
                detail = f"Emplacement mis à jour : {location or 'non renseigné'}"
                event = "location"
            else:
                threshold = max(0.0, _float(data.get("low_stock_g")))
                connection.execute(
                    f"UPDATE spools SET low_stock_g = ?, updated_at = ? WHERE archived = 0 AND id IN ({marks})",
                    [threshold, now, *spool_ids],
                )
                detail = f"Seuil d’alerte défini à {round(threshold, 1)} g"
                event = "threshold"
            for spool_id in spool_ids:
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, detail, created_at) VALUES (?, ?, ?, ?)",
                    (event, spool_id, detail, now),
                )
        return {"count": len(spool_ids), "message": f"{len(spool_ids)} bobine(s) mises à jour."}

    def export_csv(self) -> bytes:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT spools.*, slot_assignments.slot FROM spools
                LEFT JOIN slot_assignments ON slot_assignments.spool_id = spools.id
                WHERE spools.archived = 0 ORDER BY spools.name COLLATE NOCASE, spools.id"""
            ).fetchall()
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["id", "nom", "matiere", "marque", "couleur", "poids_initial_g", "poids_restant_g", "seuil_alerte_g", "emplacement", "ams", "cout_eur", "date_ajout", "notes"])
        for row in rows:
            writer.writerow([row["id"], row["name"], row["material"], row["brand"], row["color"], row["initial_g"], row["remaining_g"], row["low_stock_g"], row["storage_location"], row["slot"] or "", row["cost_eur"], row["created_at"], row["notes"]])
        return output.getvalue().encode("utf-8-sig")

    def sync_rfid_slot(self, slot: str, data: dict[str, str]) -> tuple[dict[str, Any], bool]:
        """Associate an AMS slot with the physical RFID tag currently read there.

        A tag UID (or the printer-provided tray UUID) is required: material and
        colour alone are not enough to tell two identical rolls apart.
        """
        if slot not in {"1", "2", "3", "4"}:
            raise ValueError("Emplacement AMS invalide")
        tag = str(data.get("tag") or "").strip()[:128]
        if not tag:
            raise ValueError("Identifiant RFID absent")
        now = now_iso()
        name = str(data.get("name") or "Bobine Bambu Lab")[:80]
        material = str(data.get("material") or "")[:40]
        brand = str(data.get("brand") or "Bambu Lab")[:60]
        color = str(data.get("color") or "")[:40]
        info = str(data.get("info") or "")[:80]
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, name FROM spools WHERE rfid_tag = ? AND archived = 0", (tag,)
            ).fetchone()
            changed = False
            if row is None:
                # Preserve a migrated placeholder and its manually entered
                # weight when this is the first RFID reading for that slot.
                current = connection.execute(
                    """
                    SELECT spools.* FROM slot_assignments
                    JOIN spools ON spools.id = slot_assignments.spool_id
                    WHERE slot_assignments.slot = ? AND spools.archived = 0
                    """,
                    (slot,),
                ).fetchone()
                placeholder = current and (
                    current["name"] == f"Bobine A{slot}"
                    and not current["material"] and not current["brand"] and not current["color"]
                    and not current["rfid_tag"]
                )
                if placeholder:
                    spool_id = int(current["id"])
                    connection.execute(
                        """
                        UPDATE spools SET name = ?, material = ?, brand = ?, color = ?,
                        rfid_tag = ?, rfid_info = ?, updated_at = ? WHERE id = ?
                        """,
                        (name, material, brand, color, tag, info, now, spool_id),
                    )
                    detail = "Bobine existante associée au tag RFID"
                else:
                    cursor = connection.execute(
                        """
                        INSERT INTO spools(name, material, brand, color, rfid_tag, rfid_info,
                                           initial_g, remaining_g, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, 1000, 1000, ?, ?)
                        """,
                        (name, material, brand, color, tag, info, now, now),
                    )
                    spool_id = int(cursor.lastrowid)
                    detail = "Nouvelle bobine créée depuis le tag RFID"
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('rfid', ?, ?, ?, ?)",
                    (spool_id, slot, detail, now),
                )
                changed = True
            else:
                spool_id = int(row["id"])
                machine_name = str(row["name"] or "")
                name = str(data.get("name") or "").strip()[:80]
                should_rename = is_machine_spool_name(machine_name) and bool(name)
                # Refresh the descriptive fields supplied by the printer but
                # preserve a name the owner may have personalised.
                connection.execute(
                    """
                    UPDATE spools SET name = CASE WHEN ? THEN ? ELSE name END,
                    material = CASE WHEN ? != '' THEN ? ELSE material END,
                    brand = CASE WHEN ? != '' THEN ? ELSE brand END,
                    color = CASE WHEN ? != '' THEN ? ELSE color END,
                    rfid_info = CASE WHEN ? != '' THEN ? ELSE rfid_info END, updated_at = ?
                    WHERE id = ?
                    """,
                    (should_rename, name, material, material, brand, brand, color, color, info, info, now, spool_id),
                )
            assigned = connection.execute(
                "SELECT spool_id FROM slot_assignments WHERE slot = ?", (slot,)
            ).fetchone()
            if assigned is None or int(assigned["spool_id"]) != spool_id:
                connection.execute("DELETE FROM slot_assignments WHERE spool_id = ?", (spool_id,))
                connection.execute("DELETE FROM slot_assignments WHERE slot = ?", (slot,))
                connection.execute(
                    "INSERT INTO slot_assignments(slot, spool_id, assigned_at) VALUES (?, ?, ?)",
                    (slot, spool_id, now),
                )
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('assign', ?, ?, ?, ?)",
                    (spool_id, slot, "Bobine placée automatiquement après lecture RFID", now),
                )
                changed = True
        return self.spool(spool_id), changed

    def assign(self, slot: str, spool_id: int | None) -> dict[str, Any]:
        """Place a spool in an AMS slot without silently losing another one.

        Moving a spool onto an occupied slot exchanges the two positions when
        the selected spool already has one.  A repeated save is a no-op, so the
        UI can safely retry without duplicating inventory history.
        """
        if slot not in {"1", "2", "3", "4"}:
            raise ValueError("Emplacement AMS invalide")
        assigned_at = now_iso()
        with self._connect() as connection:
            if spool_id is None:
                current = connection.execute(
                    "SELECT spool_id FROM slot_assignments WHERE slot = ?", (slot,)
                ).fetchone()
                if current is None:
                    return {"action": "unchanged", "message": f"A{slot} est déjà libre."}
                connection.execute("DELETE FROM slot_assignments WHERE slot = ?", (slot,))
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('remove', ?, ?, ?, ?)",
                    (int(current["spool_id"]), slot, "Bobine retirée de l'AMS", assigned_at),
                )
                return {"action": "removed", "message": f"Bobine retirée de A{slot}."}
            selected = connection.execute(
                "SELECT id, name FROM spools WHERE id = ? AND archived = 0", (spool_id,)
            ).fetchone()
            if selected is None:
                raise ValueError("Bobine introuvable")
            source = connection.execute(
                "SELECT slot FROM slot_assignments WHERE spool_id = ?", (spool_id,)
            ).fetchone()
            occupant = connection.execute(
                """
                SELECT slot_assignments.spool_id, spools.name
                FROM slot_assignments JOIN spools ON spools.id = slot_assignments.spool_id
                WHERE slot_assignments.slot = ?
                """, (slot,),
            ).fetchone()
            source_slot = str(source["slot"]) if source else ""
            if occupant is not None and int(occupant["spool_id"]) == spool_id:
                return {"action": "unchanged", "message": f"{selected['name']} est déjà en A{slot}."}

            if source_slot:
                connection.execute("DELETE FROM slot_assignments WHERE slot = ?", (source_slot,))
            if occupant is not None:
                connection.execute("DELETE FROM slot_assignments WHERE slot = ?", (slot,))
            connection.execute(
                "INSERT INTO slot_assignments(slot, spool_id, assigned_at) VALUES (?, ?, ?)",
                (slot, spool_id, assigned_at),
            )
            if occupant is not None and source_slot:
                displaced_id = int(occupant["spool_id"])
                connection.execute(
                    "INSERT INTO slot_assignments(slot, spool_id, assigned_at) VALUES (?, ?, ?)",
                    (source_slot, displaced_id, assigned_at),
                )
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('assign', ?, ?, ?, ?)",
                    (spool_id, slot, f"Échange A{source_slot} → A{slot}", assigned_at),
                )
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('assign', ?, ?, ?, ?)",
                    (displaced_id, source_slot, f"Échange A{slot} → A{source_slot}", assigned_at),
                )
                return {
                    "action": "swapped",
                    "message": f"Échange effectué : {selected['name']} est en A{slot}, {occupant['name']} passe en A{source_slot}.",
                }
            if occupant is not None:
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('remove', ?, ?, ?, ?)",
                    (int(occupant["spool_id"]), slot, f"Remplacée par {selected['name']}", assigned_at),
                )
                detail = f"Placée en A{slot}, remplace {occupant['name']}"
                action = "replaced"
            elif source_slot:
                detail = f"Déplacée de A{source_slot} vers A{slot}"
                action = "moved"
            else:
                detail = "Bobine placée dans l'AMS"
                action = "placed"
            connection.execute(
                "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('assign', ?, ?, ?, ?)",
                (spool_id, slot, detail, assigned_at),
            )
        return {"action": action, "message": f"{selected['name']} est maintenant en A{slot}."}

    def unassign(self, spool_id: int) -> dict[str, Any]:
        assigned_at = now_iso()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT slot FROM slot_assignments WHERE spool_id = ?", (spool_id,)
            ).fetchone()
            if row is None:
                return {"action": "unchanged", "message": "Cette bobine est déjà hors AMS."}
            connection.execute("DELETE FROM slot_assignments WHERE spool_id = ?", (spool_id,))
            connection.execute(
                "INSERT INTO inventory_history(event_type, spool_id, slot, detail, created_at) VALUES ('remove', ?, ?, ?, ?)",
                (spool_id, row["slot"] if row else None, "Bobine retirée de l'AMS", assigned_at),
            )
        return {"action": "removed", "message": f"Bobine retirée de A{row['slot']}."}

    def spool_id_for_slot(self, slot: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT spool_id FROM slot_assignments WHERE slot = ?", (slot,)
            ).fetchone()
        return int(row["spool_id"]) if row else None

    def deduct(self, spool_id: int, used_g: float) -> tuple[float, float]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT remaining_g FROM spools WHERE id = ? AND archived = 0", (spool_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Bobine introuvable au moment du décompte")
            before = _float(row["remaining_g"])
            after = round(max(0.0, before - max(0.0, used_g)), 3)
            connection.execute(
                "UPDATE spools SET remaining_g = ?, updated_at = ? WHERE id = ?",
                (after, now_iso(), spool_id),
            )
            connection.execute(
                "INSERT INTO inventory_history(event_type, spool_id, detail, created_at) VALUES ('deduct', ?, ?, ?)",
                (spool_id, f"-{round(used_g, 3)} g · {round(before, 3)} → {round(after, 3)} g", now_iso()),
            )
        return before, after

    def settle_print(self, settlement_key: str, lines: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """Debit every spool once, in one SQLite transaction.

        The durable settlement key is the authority for idempotency.  It is
        deliberately stored with the inventory rather than only in state.json,
        so a crash between the debit and JSON save cannot charge a job twice.
        """
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT deductions_json FROM print_settlements WHERE settlement_key = ?", (settlement_key,)
            ).fetchone()
            if existing is not None:
                return json.loads(existing["deductions_json"]), False
            deductions: list[dict[str, Any]] = []
            for line in lines:
                spool_id = int(line["spool_id"])
                used_g = max(0.0, _float(line["used_g"]))
                row = connection.execute(
                    "SELECT remaining_g FROM spools WHERE id = ? AND archived = 0", (spool_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("Bobine introuvable au moment du décompte")
                before = _float(row["remaining_g"])
                after = round(max(0.0, before - used_g), 3)
                connection.execute(
                    "UPDATE spools SET remaining_g = ?, updated_at = ? WHERE id = ?",
                    (after, now_iso(), spool_id),
                )
                connection.execute(
                    "INSERT INTO inventory_history(event_type, spool_id, detail, created_at) VALUES ('deduct', ?, ?, ?)",
                    (spool_id, f"-{round(used_g, 3)} g · {round(before, 3)} → {round(after, 3)} g", now_iso()),
                )
                deductions.append({
                    "slot": str(line["slot"]), "spool_id": spool_id, "used_g": used_g,
                    "before_g": before, "after_g": after,
                })
            connection.execute(
                "INSERT INTO print_settlements(settlement_key, deductions_json, created_at) VALUES (?, ?, ?)",
                (settlement_key, json.dumps(deductions, ensure_ascii=False), now_iso()),
            )
        return deductions, True


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def descriptive_spool_name(material: str, color: str) -> str:
    """Produce a readable default, for example ``PLA bleu``."""
    return " ".join(part for part in (material.strip(), color.strip()) if part)[:80]


def is_machine_spool_name(name: str) -> bool:
    """Recognise opaque labels sent by Bambu RFID without touching user names."""
    return bool(re.fullmatch(r"A\d{2}-[A-Z0-9-]+", name.strip(), re.I)) or name.strip() in {
        "Bobine Bambu Lab", "Bobine inconnue",
    }


def history_date_iso(value: Any) -> str:
    """Accept an optional ISO date for stock that predates Companion."""
    text = str(value or "").strip()
    if not text:
        return now_iso()
    try:
        date = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Date d’ajout invalide") from exc
    if date > datetime.now().date():
        raise ValueError("La date d’ajout ne peut pas être dans le futur")
    return f"{date.isoformat()}T12:00:00{time.strftime('%z')}"


def rfid_identity(value: Any) -> str:
    """Return a usable physical tag identifier, never Bambu's all-zero sentinel."""
    candidate = re.sub(r"[^0-9A-Za-z_-]", "", str(value or "")).upper()
    return "" if not candidate or set(candidate) == {"0"} else candidate


def rfid_color(value: Any) -> str:
    color = str(value or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9A-Fa-f]{8}", color):
        color = color[:6]
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        return color[:40]
    red, green, blue = (int(color[index:index + 2], 16) / 255 for index in (0, 2, 4))
    hue, saturation, brightness = colorsys.rgb_to_hsv(red, green, blue)
    if brightness < 0.16:
        return "Noir"
    if saturation < 0.13:
        if brightness > 0.9:
            return "Blanc"
        return "Gris clair" if brightness > 0.58 else "Gris"
    degrees = hue * 360
    if degrees < 15 or degrees >= 345:
        return "Rouge"
    if degrees < 42:
        return "Orange"
    if degrees < 68:
        return "Jaune"
    if degrees < 165:
        return "Vert"
    if degrees < 205:
        return "Cyan"
    if degrees < 258:
        return "Bleu"
    if degrees < 295:
        return "Violet"
    if degrees < 338:
        return "Rose"
    return "Rouge"


def rfid_slots(report: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    """Read the documented/observed AMS MQTT tray layouts without guessing a spool.

    A1/AMS Lite reports use the same ``print.ams.ams[].tray[]`` family as
    other Bambu models, while firmware revisions sometimes omit the outer AMS
    list. Only a non-zero tag UID or tray UUID qualifies as an RFID reading.
    """
    source = report.get("ams")
    groups: list[Any]
    if isinstance(source, dict):
        nested = source.get("ams")
        groups = nested if isinstance(nested, list) else [source]
    elif isinstance(source, list):
        groups = source
    else:
        return []
    result: list[tuple[str, dict[str, str]]] = []
    for group_index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        trays = group.get("tray") or group.get("trays") or []
        if not isinstance(trays, list):
            continue
        for tray_index, tray in enumerate(trays):
            if not isinstance(tray, dict):
                continue
            try:
                tray_id = int(tray.get("id", tray_index))
            except (TypeError, ValueError):
                tray_id = tray_index
            slot = group_index * 4 + tray_id + 1
            if slot not in {1, 2, 3, 4}:
                continue
            tag = rfid_identity(tray.get("tag_uid")) or rfid_identity(tray.get("tray_uuid"))
            if not tag:
                continue
            material = str(tray.get("tray_type") or tray.get("type") or "").strip()[:40]
            color = rfid_color(tray.get("tray_color") or tray.get("color"))
            brand = str(tray.get("tray_sub_brands") or "Bambu Lab").strip()[:60]
            # ``tray_id_name`` is often an opaque SKU such as A01-W2. The
            # material and colour are stable and useful in the catalogue.
            name = descriptive_spool_name(material, color) or brand or "Bobine Bambu Lab"
            result.append((str(slot), {
                "tag": tag,
                "info": str(tray.get("tray_info_idx") or "").strip()[:80],
                "name": name,
                "material": material,
                "brand": brand,
                "color": color,
            }))
    return result


def parse_slice_info(data: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(data)
    plates: list[dict[str, Any]] = []
    plate_nodes = [node for node in root.iter() if local_name(node.tag) == "plate"]
    if not plate_nodes:
        plate_nodes = [root]
    for pidx, plate in enumerate(plate_nodes, 1):
        filaments: list[dict[str, Any]] = []
        seen: set[tuple[str, float]] = set()
        for node in plate.iter():
            if local_name(node.tag) != "filament":
                continue
            attrs = {local_name(k): v for k, v in node.attrib.items()}
            used = _float(attrs.get("used_g") or attrs.get("weight") or attrs.get("used_weight"))
            if used <= 0:
                continue
            fid = str(attrs.get("id") or attrs.get("filament_id") or len(filaments) + 1)
            key = (fid, round(used, 5))
            if key in seen:
                continue
            seen.add(key)
            filaments.append({
                "id": fid,
                "type": attrs.get("type") or attrs.get("filament_type") or "Filament",
                "color": attrs.get("color") or attrs.get("filament_color") or "",
                "used_g": round(used, 3),
            })
        plate_id = str(plate.attrib.get("id") or plate.attrib.get("index") or pidx)
        if filaments:
            plates.append({"id": plate_id, "filaments": filaments})
    return plates


def parse_gcode_weights(text: str) -> list[dict[str, Any]]:
    patterns = [
        r"total filament weight \[g\]\s*[:=]\s*([^\r\n;]+)",
        r"filament used \[g\]\s*[:=]\s*([^\r\n;]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        values = [_float(v) for v in re.split(r"[,; ]+", match.group(1).strip())]
        values = [v for v in values if v > 0]
        if values:
            return [{"id": str(i + 1), "type": "Filament", "color": "", "used_g": round(v, 3)}
                    for i, v in enumerate(values)]
    return []


def validate_3mf_archive(archive: zipfile.ZipFile) -> None:
    """Reject archives whose declared contents are unsafe to inspect locally."""
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("Archive 3MF trop complexe (trop de fichiers)")
    total = 0
    for entry in entries:
        if entry.is_dir():
            continue
        if entry.file_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("Archive 3MF trop volumineuse après décompression")
        if entry.compress_size and entry.file_size / entry.compress_size > MAX_ARCHIVE_COMPRESSION_RATIO:
            raise ValueError("Archive 3MF avec taux de compression anormal")
        total += entry.file_size
        if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("Archive 3MF trop volumineuse après décompression")


def extract_3mf_object_map(archive: zipfile.ZipFile) -> dict[str, Any]:
    """Read explicit object markers from every bounded plate G-code."""
    names = [name for name in archive.namelist() if re.search(r"(?:metadata/)?plate_\d+\.gcode$", name, re.I)]
    objects: list[dict[str, Any]] = []
    for name in sorted(names):
        number_match = re.search(r"plate_(\d+)", name, re.I)
        plate = number_match.group(1) if number_match else ""
        with archive.open(name) as source:
            raw_gcode = source.read(MAX_GCODE_OBJECT_MAP_BYTES + 1)
        truncated = len(raw_gcode) > MAX_GCODE_OBJECT_MAP_BYTES
        text = raw_gcode[:MAX_GCODE_OBJECT_MAP_BYTES].decode("utf-8", "replace")
        for item in map_gcode_objects(text):
            objects.append({"plate": plate, "source_truncated": truncated, **item})
    return {**object_map_summary(objects), "objects": objects,
            "source_max_bytes": MAX_GCODE_OBJECT_MAP_BYTES}


def extract_3mf_plates(archive: zipfile.ZipFile) -> list[dict[str, Any]]:
    validate_3mf_archive(archive)
    names = archive.namelist()
    slice_names = [n for n in names if n.lower().endswith("metadata/slice_info.config")]
    plates: list[dict[str, Any]] = []
    if slice_names:
        plates = parse_slice_info(archive.read(slice_names[0]))
    if not plates:
        for name in sorted(n for n in names if re.search(r"metadata/plate_\d+\.gcode$", n, re.I)):
            with archive.open(name) as gcode:
                text = gcode.read(250000).decode("utf-8", "replace")
            filaments = parse_gcode_weights(text)
            if filaments:
                number = re.search(r"plate_(\d+)", name, re.I).group(1)
                plates.append({"id": number, "filaments": filaments})
    return plates


def parsed_3mf_result(plates: list[dict[str, Any]], digest: str, filename: str,
                      object_map: dict[str, Any] | None = None) -> dict[str, Any]:
    if not plates:
        raise ValueError("Aucune consommation used_g trouvée. Exportez d’abord le plateau tranché en .gcode.3mf.")
    return {"filename": Path(filename).name, "sha256": digest, "plates": plates,
            "object_map": object_map or {"status": "unavailable", "object_count": 0,
                                          "reason": "Cartographie non analysée", "objects": []}}


def parse_3mf(raw: bytes, filename: str = "travail.3mf") -> dict[str, Any]:
    if len(raw) > MAX_IMPORT_BYTES:
        raise ValueError("Fichier trop volumineux (32 Mo maximum)")
    digest = hashlib.sha256(raw).hexdigest()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        plates = extract_3mf_plates(archive)
        object_map = extract_3mf_object_map(archive)
    return parsed_3mf_result(plates, digest, filename, object_map)


def parse_3mf_path(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_IMPORT_BYTES:
            raise ValueError("Fichier trop volumineux (32 Mo maximum)")
    except OSError:
        raise
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    with zipfile.ZipFile(path) as archive:
        plates = extract_3mf_plates(archive)
        object_map = extract_3mf_object_map(archive)
    return parsed_3mf_result(plates, digest.hexdigest(), path.name, object_map)


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value % 128
        value //= 128
        if value:
            byte |= 0x80
        out.append(byte)
        if not value:
            return bytes(out)


def mqtt_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def read_varint(sock: ssl.SSLSocket) -> int:
    multiplier, value = 1, 0
    for _ in range(4):
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("Connexion MQTT fermée")
        value += (byte[0] & 127) * multiplier
        if not byte[0] & 128:
            return value
        multiplier *= 128
    raise ValueError("Longueur MQTT invalide")


def recv_exact(sock: ssl.SSLSocket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("Connexion MQTT fermée")
        data.extend(chunk)
    return bytes(data)


@dataclass
class MQTTConfig:
    ip: str
    serial: str
    access_code: str


class LocalMQTT(threading.Thread):
    def __init__(self, app: "Companion") -> None:
        super().__init__(name="local-mqtt", daemon=True)
        self.app = app
        self.stop_event = threading.Event()
        self.restart_event = threading.Event()
        self.retry_count = 0

    def restart(self) -> None:
        self.restart_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.restart_event.set()

    def run(self) -> None:
        delay = 2
        while not self.stop_event.is_set():
            cfg = self.app.mqtt_config()
            if not cfg.ip or not cfg.serial or not cfg.access_code:
                self.restart_event.wait(2)
                self.restart_event.clear()
                continue
            try:
                self.session(cfg)
                delay = 2
            except Exception as exc:
                self.app.set_connected(False)
                self.retry_count += 1
                log(
                    f"MQTT déconnecté: {exc}; reconnexion {self.retry_count} "
                    f"prévue dans {delay} s"
                )
                self.restart_event.wait(delay)
                self.restart_event.clear()
                delay = min(delay * 2, 30)

    def session(self, cfg: MQTTConfig) -> None:
        # Bambu printers commonly use a self-signed certificate.  Keep the
        # compatible TLS handshake, then pin the certificate on first trusted
        # connection and refuse any later substitution.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((cfg.ip, 8883), timeout=10)
        try:
            sock = context.wrap_socket(raw, server_hostname=cfg.ip)
            fingerprint = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
            self.app.verify_or_remember_mqtt_certificate(fingerprint)
            sock.settimeout(5)
            client_id = f"ams-companion-{os.getpid()}-{int(time.time())}"
            payload = mqtt_string(client_id) + mqtt_string("bblp") + mqtt_string(cfg.access_code)
            variable = mqtt_string("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 30)
            sock.sendall(bytes([0x10]) + encode_varint(len(variable) + len(payload)) + variable + payload)
            header = recv_exact(sock, 1)
            body = recv_exact(sock, read_varint(sock))
            if header[0] >> 4 != 2 or len(body) < 2 or body[1] != 0:
                raise ConnectionError(f"Authentification MQTT refusée ({body.hex()})")
            report_topic = f"device/{cfg.serial}/report"
            request_topic = f"device/{cfg.serial}/request"
        # Several A1/A1 mini firmwares close the entire MQTT connection when a
        # third-party client subscribes to the write-only ``request`` topic.
        # Subscribe only to the supported report channel; request remains the
        # publication target for pushall.
            sub = struct.pack("!H", 1) + mqtt_string(report_topic) + b"\x00"
            sock.sendall(bytes([0x82]) + encode_varint(len(sub)) + sub)
            request = json.dumps({"pushing": {"sequence_id": "1", "command": "pushall"}}, separators=(",", ":")).encode()
            publish = mqtt_string(request_topic) + request
            sock.sendall(bytes([0x30]) + encode_varint(len(publish)) + publish)
            self.app.set_connected(True)
            log(f"MQTT connecté à {cfg.ip} ({cfg.serial})")
            if self.retry_count:
                log(f"MQTT reconnecté après {self.retry_count} tentative(s)")
                self.retry_count = 0
            last_ping = time.monotonic()
            while not self.stop_event.is_set() and not self.restart_event.is_set():
                try:
                    first = sock.recv(1)
                    if not first:
                        raise ConnectionError("socket fermée")
                    remaining = read_varint(sock)
                    packet = recv_exact(sock, remaining)
                    kind = first[0] >> 4
                    if kind == 3 and len(packet) >= 2:
                        topic_len = struct.unpack("!H", packet[:2])[0]
                        offset = 2 + topic_len
                        if first[0] & 0x06:
                            offset += 2
                        try:
                            incoming_topic = packet[2:2 + topic_len].decode("utf-8", "replace")
                            incoming = json.loads(packet[offset:].decode("utf-8"))
                            if isinstance(incoming, dict):
                                try:
                                    self.app.on_mqtt_message(incoming_topic, incoming)
                                except Exception as exc:
                                    log(f"Événement MQTT ignoré sans redémarrer la connexion: {exc}")
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            pass
                except socket.timeout:
                    pass
                if time.monotonic() - last_ping > 20:
                    sock.sendall(b"\xC0\x00")
                    last_ping = time.monotonic()
            self.restart_event.clear()
        finally:
            self.app.set_connected(False)
            try:
                raw.close()
            except OSError:
                pass


def default_bridge_roots() -> list[Path]:
    """Directories where Bambu Studio creates its automatic print archives."""
    home = Path.home()
    candidates = [
        Path(tempfile.gettempdir()) / "bamboo_model",
        home / "Library" / "Application Support" / "BambuStudio" / "tmp" / "bamboo_model",
        home / "Library" / "Application Support" / "BambuStudio" / "tmp",
    ]
    result: list[Path] = []
    for path in candidates:
        if path not in result:
            result.append(path)
    return result


def decode_ams_mapping(value: Any) -> list[int]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [item for item in re.split(r"[,; ]+", value.strip("[] ")) if item]
    if not isinstance(value, (list, tuple)):
        return []
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            return []
    return result


class StudioBridge(threading.Thread):
    """Watches the private print archive generated by official Bambu Studio."""

    def __init__(self, app: "Companion", roots: list[Path] | None = None,
                 poll_interval: float = 1.0, stable_seconds: float = 1.0) -> None:
        super().__init__(name="bambu-studio-bridge", daemon=True)
        self.app = app
        self.roots = roots or default_bridge_roots()
        self.poll_interval = poll_interval
        self.stable_seconds = stable_seconds
        self.stop_event = threading.Event()
        self.started_at = time.time()
        self.observed: dict[str, tuple[int, int, float]] = {}
        self.handled: dict[str, tuple[int, int]] = {}
        self.latest_handled_mtime_ns = 0

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        log("Passerelle Bambu Studio active")
        while not self.stop_event.wait(self.poll_interval):
            self.scan_once()

    def candidates(self) -> list[Path]:
        files: list[Path] = []
        for root in self.roots:
            try:
                if root.exists():
                    files.extend(path for path in root.rglob("*.3mf")
                                 if path.parent.name.lower() == "metadata"
                                 and not path.name.lower().endswith("_config.3mf"))
            except OSError as exc:
                log(f"Passerelle: dossier temporaire illisible {root}: {exc}")
        try:
            return sorted(set(files), key=lambda path: path.stat().st_mtime_ns, reverse=True)
        except OSError:
            return files

    def scan_once(self) -> None:
        now = time.time()
        for path in self.candidates():
            try:
                stat = path.stat()
            except OSError:
                continue
            # Ignore old archives already present before Companion started.
            if stat.st_mtime < self.started_at - 30:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            key = str(path)
            if stat.st_mtime_ns <= self.latest_handled_mtime_ns:
                continue
            if self.handled.get(key) == signature:
                continue
            previous = self.observed.get(key)
            if previous is None or previous[:2] != signature:
                self.observed[key] = (signature[0], signature[1], now)
                # This is the newest unhandled archive. Do not fall back to an
                # older one while Bambu Studio is still writing it.
                break
            if now - previous[2] < self.stable_seconds:
                break
            try:
                parsed = parse_3mf_path(path)
                after = path.stat()
            except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError):
                # Bambu Studio may still be writing the ZIP. A changed size or
                # timestamp will automatically trigger another attempt.
                break
            if (after.st_size, after.st_mtime_ns) != signature:
                self.observed[key] = (after.st_size, after.st_mtime_ns, now)
                break
            self.handled[key] = signature
            self.latest_handled_mtime_ns = stat.st_mtime_ns
            self.app.on_studio_archive(path, parsed)
            break
        self.app.bridge_tick()


class Companion:
    def __init__(self, state_path: Path = STATE_FILE,
                 bridge_roots: list[Path] | None = None) -> None:
        self.state_path = state_path
        self.lock = threading.RLock()
        self.state = load_state(state_path)
        # A camera read runs in a daemon thread.  If the app is stopped while
        # that read is active, its transient lock must never survive into the
        # next launch and block every later scheduled capture.
        camera = self.state.setdefault("camera", {})
        if camera.pop("capture_in_progress", False):
            camera["capture_in_progress"] = False
            atomic_save(self.state, state_path)
        self.inventory = Inventory(inventory_path_for_state(state_path))
        self.inventory.initialize(self.state)
        self.guardian = PlateGuardian(guardian_path_for_state(state_path))
        self.events = EventJournal(events_path_for_state(state_path))
        self.events.initialize()
        self.autopilot = AutoPilotPlanner()
        previous_spools = json.dumps(self.state.get("spools", {}), sort_keys=True)
        self._sync_spools_from_inventory()
        if json.dumps(self.state["spools"], sort_keys=True) != previous_spools:
            atomic_save(self.state, self.state_path)
        self.last_import: dict[str, Any] | None = None
        self.auto_import: dict[str, Any] | None = None
        self.pending_request: dict[str, Any] | None = None
        self.untracked_running: dict[str, Any] | None = None
        armed = self.state.get("armed_job")
        if armed and armed.get("auto_bridge"):
            armed_epoch = _float(armed.get("armed_epoch"))
            if not armed_epoch or time.time() - armed_epoch > 600:
                self.state["armed_job"] = None
                self.state["bridge"]["status"] = "Ancien armement automatique supprimé au démarrage"
                atomic_save(self.state, self.state_path)
        self._restore_recent_auto_import()
        # A relaunch during an already running print must not leave the
        # existing images orphaned in the common capture directory.  Adopt
        # only the currently ungrouped images: future captures are tagged as
        # soon as their print starts.
        if str(self.state.get("printer", {}).get("state") or "").upper() in RUNNING:
            self._ensure_camera_print_session_locked({}, "", adopt_ungrouped=True)
            self.save()
        self.mqtt = LocalMQTT(self)
        self.bridge = StudioBridge(self, bridge_roots)
        camera = self.state.get("camera", {})
        if (camera.get("pending_capture_layer") and camera.get("enabled")
                and camera.get("certificate_sha256")):
            threading.Thread(target=self._capture_pending_camera, daemon=True).start()

    def save(self) -> None:
        atomic_save(self.state, self.state_path)

    def _restore_recent_auto_import(self) -> None:
        """Resume a recently prepared Bambu file after a Companion restart."""
        bridge = self.state.get("bridge", {})
        saved = bridge.get("recent_import")
        if isinstance(saved, dict) and time.time() - _float(saved.get("detected_epoch")) <= MAX_AUTO_IMPORT_AGE_SECONDS:
            self.auto_import = json.loads(json.dumps(saved))
            self.last_import = {key: value for key, value in self.auto_import.items()
                                if key not in {"source_path", "detected_epoch"}}
            if self._try_auto_arm_locked():
                self.save()
                log("Passerelle: fichier Bambu Studio restauré après relance")
            return
        raw_path = str(bridge.get("last_file") or "")
        if not raw_path:
            return
        try:
            path = Path(raw_path)
            if not path.is_file() or time.time() - path.stat().st_mtime > MAX_AUTO_IMPORT_AGE_SECONDS:
                return
            parsed = parse_3mf_path(path)
        except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError):
            return
        detected = dict(parsed)
        detected["source_path"] = str(path)
        detected["detected_epoch"] = path.stat().st_mtime
        self.auto_import = detected
        self.last_import = parsed
        if self._try_auto_arm_locked():
            self.save()
            log(f"Passerelle: fichier récent restauré après relance {path}")

    def _sync_spools_from_inventory(self) -> None:
        """Maintain the legacy A1-A4 view while SQLite owns spool records."""
        installed = self.inventory.slot_spools()
        self.state["spools"] = {
            slot: {
                "name": installed.get(slot, {}).get("name", f"A{slot} libre"),
                "initial_g": installed.get(slot, {}).get("initial_g", 0.0),
                "remaining_g": installed.get(slot, {}).get("remaining_g", 0.0),
                "spool_id": installed.get(slot, {}).get("id"),
            }
            for slot in map(str, range(1, 5))
        }

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            self._sync_spools_from_inventory()
            clean = json.loads(json.dumps(self.state))
            clean["config"]["access_code"] = "" if not self.state["config"].get("access_code") else "********"
            clean["imported"] = self.last_import
            clean["auto_import_available"] = self.auto_import is not None
            clean["inventory"] = self.inventory.public_state()
            clean["inventory_summary"] = self.inventory.summary()
            clean["inventory_overview"] = self.inventory.catalog_overview()
            clean["guardian"] = self.guardian.state()
            clean["autopilot"] = self.autopilot.state(clean["guardian"], clean.get("active_job"))
            clean["events"] = self.events.recent()
            clean["vision_storage"] = self.vision_storage()
            return clean

    def supervision_report(self) -> dict[str, Any]:
        """Build a portable, local-only supervision report without secrets.

        The report deliberately exposes a compact operational snapshot rather
        than the complete state file: LAN credentials, printer identifiers,
        absolute paths and raw MQTT payloads never leave the application data
        store through this endpoint.
        """
        state = self.public_state()
        active_job = state.get("active_job") or {}
        object_map = active_job.get("object_map") if isinstance(active_job, dict) else {}
        events = self.events.recent(100)
        outcomes: dict[str, int] = {}
        for event in events:
            outcome = str(event.get("outcome") or "received")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return {
            "schema_version": 1,
            "generated_at": now_iso(),
            "application": {"name": "AMS Lite Companion V2", "version": __version__},
            "print": {
                "state": state.get("printer", {}).get("state", ""),
                "progress": state.get("printer", {}).get("progress", 0),
                "job": state.get("printer", {}).get("job", ""),
                "tracking_active": bool(active_job),
                "object_map": object_map_summary(object_map.get("objects", [])) if isinstance(object_map, dict) else {
                    "status": "unavailable", "object_count": 0,
                },
            },
            "vision": {
                "enabled": bool(state.get("camera", {}).get("enabled")),
                "status": state.get("camera", {}).get("status", ""),
                "capture_every_layers": state.get("camera", {}).get("capture_every_layers", 5),
                "storage": state.get("vision_storage", {}),
            },
            "guardian": state.get("guardian", {}),
            "autopilot": state.get("autopilot", {}),
            "reliability": {"event_count": len(events), "outcomes": outcomes, "events": events},
        }

    def vision_storage(self) -> dict[str, int]:
        """Return the exact local footprint of indexed Vision captures."""
        root = self.state_path.parent / "captures"
        count = completed = active = total_bytes = 0
        for image in self.state.get("camera", {}).get("captures", []):
            if not isinstance(image, dict):
                continue
            filename = str(image.get("file") or "")
            folder = str(image.get("folder") or "")
            if not re.fullmatch(r"layer-\d{5}-\d{8}-\d{6}\.jpg", filename):
                continue
            if folder and not re.fullmatch(r"print-[a-zA-Z0-9._-]+", folder):
                continue
            try:
                total_bytes += (root / folder / filename if folder else root / filename).stat().st_size
            except OSError:
                continue
            count += 1
            if folder:
                completed += 1
            else:
                active += 1
        return {"count": count, "bytes": total_bytes, "completed": completed, "active": active}

    def observe_plate_guardian(self, data: dict[str, Any]) -> dict[str, Any]:
        """Accept detector evidence; the guardian has no printer-control path."""
        with self.lock:
            object_map = (self.state.get("active_job") or {}).get("object_map") or {}
            mapped = object_map.get("objects") if isinstance(object_map, dict) else []
            if isinstance(object_map, dict) and object_map.get("status") == "mapped" and isinstance(mapped, list):
                known = {str(item.get("id") or "") for item in mapped if isinstance(item, dict)}
                if known and str(data.get("object_id") or "") not in known:
                    raise ValueError("Objet détecté absent de la cartographie G-code active")
        result = self.guardian.observe(data)
        proposal = result.get("proposal")
        if proposal and result.get("accepted"):
            log(
                "Gardien de plateau: alerte à confirmer pour "
                f"{proposal['object_label']} ({proposal['evidence_count']} images)"
            )
        return result

    def decide_plate_guardian(self, proposal_id: str, data: dict[str, Any]) -> dict[str, Any]:
        result = self.guardian.decide(
            proposal_id,
            str(data.get("decision") or ""),
            str(data.get("note") or ""),
        )
        log(f"Gardien de plateau: décision humaine {result['status']} pour {result['object_label']}")
        return result

    def mqtt_config(self) -> MQTTConfig:
        with self.lock:
            c = self.state["config"]
            return MQTTConfig(c.get("ip", ""), c.get("serial", ""), c.get("access_code", ""))

    def verify_or_remember_mqtt_certificate(self, fingerprint: str) -> None:
        with self.lock:
            config = self.state["config"]
            remembered = str(config.get("mqtt_certificate_sha256") or "")
            if remembered and not secrets.compare_digest(remembered, fingerprint):
                raise ConnectionError(
                    "Le certificat MQTT de l’imprimante a changé; vérifiez le réseau avant de le réinitialiser."
                )
            if not remembered:
                config["mqtt_certificate_sha256"] = fingerprint
                self.save()
                log("Certificat MQTT local épinglé pour les prochaines connexions")

    def set_connected(self, connected: bool) -> None:
        with self.lock:
            self.state["printer"]["connected"] = connected

    def on_mqtt_message(self, topic: str, payload: dict[str, Any]) -> None:
        if topic.endswith("/request"):
            self.on_print_request(payload)
        elif topic.endswith("/report"):
            self.on_message(payload)

    def _sync_rfid_from_report_locked(self, report: dict[str, Any]) -> bool:
        readings = rfid_slots(report)
        if not readings:
            return False
        changed = False
        synced = []
        for slot, data in readings:
            spool, slot_changed = self.inventory.sync_rfid_slot(slot, data)
            changed = changed or slot_changed
            synced.append(f"A{slot} : {spool['name']}")
        self._sync_spools_from_inventory()
        status = "RFID synchronisé — " + " · ".join(synced)
        changed = changed or self.state["printer"].get("rfid_status") != status
        self.state["printer"]["rfid_status"] = status
        return changed

    def on_print_request(self, payload: dict[str, Any]) -> None:
        report = payload.get("print")
        if not isinstance(report, dict) or "ams_mapping" not in report:
            return
        mapping = decode_ams_mapping(report.get("ams_mapping"))
        if not mapping:
            return
        source = str(report.get("param") or report.get("file") or report.get("url") or "")
        plate_match = re.search(r"plate_(\d+)\.gcode", source, re.I)
        with self.lock:
            self.pending_request = {
                "mapping": mapping,
                "plate": plate_match.group(1) if plate_match else "",
                "job": str(report.get("subtask_name") or report.get("project_name") or ""),
                "received_epoch": time.time(),
            }
            bridge = self.state["bridge"]
            bridge["request_capture"] = True
            bridge["status"] = (
                "Commande d’impression Bambu Studio détectée"
                if self.auto_import
                else "Commande reçue — attente du fichier Bambu Studio"
            )
            self._try_auto_arm_locked()
            self.save()

    def on_studio_archive(self, path: Path, parsed: dict[str, Any]) -> None:
        with self.lock:
            if not self.state["bridge"].get("enabled", True):
                return
            detected = dict(parsed)
            detected["source_path"] = str(path)
            detected["detected_epoch"] = time.time()
            self.auto_import = detected
            self.last_import = parsed
            bridge = self.state["bridge"]
            bridge["last_file"] = str(path)
            bridge["last_sha256"] = parsed["sha256"]
            bridge["last_detected_at"] = now_iso()
            bridge["recent_import"] = detected
            bridge["status"] = "Fichier Bambu Studio récupéré — armement automatique"
            log(f"Passerelle: archive détectée {path}")
            self._try_auto_arm_locked()
            self.save()

    def configure_bridge(self, data: dict[str, Any]) -> None:
        with self.lock:
            bridge = self.state["bridge"]
            if "enabled" in data:
                bridge["enabled"] = bool(data["enabled"])
            if "fallback_enabled" in data:
                bridge["fallback_enabled"] = bool(data["fallback_enabled"])
            incoming = data.get("default_mapping", {})
            for filament_id in map(str, range(1, 5)):
                slot = str(incoming.get(filament_id, bridge["default_mapping"].get(filament_id, filament_id)))
                if slot in {"1", "2", "3", "4"}:
                    bridge["default_mapping"][filament_id] = slot
            if not bridge["enabled"]:
                bridge["status"] = "Passerelle désactivée"
            elif not self.auto_import:
                bridge["status"] = "En attente de Bambu Studio"
            self._try_auto_arm_locked()
            self.save()

    def bridge_tick(self) -> None:
        with self.lock:
            if self._try_auto_arm_locked():
                self.save()

    def _mapping_from_request(self, filaments: list[dict[str, Any]]) -> dict[str, str]:
        request = self.pending_request
        if not request or not self.auto_import:
            return {}
        # The command itself must be recent. The 3MF may legitimately have
        # been prepared earlier, but an old command must never arm a new file.
        if time.time() - request["received_epoch"] > MAX_PRINT_REQUEST_AGE_SECONDS:
            return {}
        if time.time() - self.auto_import["detected_epoch"] > MAX_AUTO_IMPORT_AGE_SECONDS:
            return {}
        values = request["mapping"]
        result: dict[str, str] = {}
        for position, filament in enumerate(filaments):
            filament_id = str(filament["id"])
            try:
                index = int(filament_id) - 1
            except ValueError:
                index = position
            if index < 0 or index >= len(values):
                return {}
            tray = values[index]
            if tray < 0 or tray > 3:
                return {}
            result[filament_id] = str(tray + 1)
        return result

    def _try_auto_arm_locked(self, force_fallback: bool = False) -> bool:
        bridge = self.state["bridge"]
        if not bridge.get("enabled", True) or not self.auto_import or self.state.get("active_job"):
            return False
        existing = self.state.get("armed_job")
        age = time.time() - self.auto_import["detected_epoch"]
        if age > MAX_AUTO_IMPORT_AGE_SECONDS:
            self.auto_import = None
            self.pending_request = None
            # An expired import cannot be confirmed safely. Return the bridge
            # to its idle state instead of leaving a misleading permanent
            # "confirmation required" warning on screen.
            if existing and existing.get("auto_bridge"):
                self.state["armed_job"] = None
            bridge.pop("recent_import", None)
            bridge["mapping_source"] = ""
            bridge["status"] = "Ancien import ignoré — en attente du prochain travail Bambu Studio"
            log("Passerelle: import automatique expiré, attente d’un nouveau travail")
            return True
        if existing and not existing.get("auto_bridge"):
            changed = bridge.get("status") != "Fichier détecté, travail manuel conservé"
            bridge["status"] = "Fichier détecté, travail manuel conservé"
            return changed

        plates = self.auto_import.get("plates", [])
        if not plates:
            return False
        requested_plate = self.pending_request.get("plate", "") if self.pending_request else ""
        plate = next((item for item in plates if str(item["id"]) == requested_plate), None)
        if plate is None and len(plates) == 1:
            plate = plates[0]
        if plate is None:
            changed = bridge.get("status") != "Fichier récupéré, plateau en attente"
            bridge["status"] = "Fichier récupéré, plateau en attente"
            return changed

        filaments = plate["filaments"]
        defaults = bridge.get("default_mapping", {})
        saved_mapping = {
            str(item["id"]): str(defaults.get(str(item["id"]), ""))
            for item in filaments
        }
        if any(slot not in {"1", "2", "3", "4"} for slot in saved_mapping.values()):
            changed = bridge.get("status") != "Correspondance AMS à compléter"
            bridge["status"] = "Correspondance AMS à compléter"
            return changed

        requested_mapping = self._mapping_from_request(filaments)
        mapping = requested_mapping or saved_mapping
        mapping_source = "Commande Bambu Studio" if requested_mapping else "Correspondance enregistrée"
        lines = [{"slot": mapping[str(item["id"])], "used_g": item["used_g"], "filament": item}
                 for item in filaments]
        token = hashlib.sha256(f"{self.auto_import['sha256']}:{plate['id']}".encode()).hexdigest()
        if (existing and existing.get("auto_bridge") and existing.get("token") == token
                and existing.get("mapping_source") == mapping_source
                and existing.get("lines") == lines):
            return False

        if requested_mapping and requested_mapping != saved_mapping and not force_fallback:
            conflicts = [
                {"filament_id": str(item["id"]), "saved_slot": saved_mapping[str(item["id"])],
                 "bambu_slot": requested_mapping[str(item["id"])]}
                for item in filaments
                if requested_mapping[str(item["id"])] != saved_mapping[str(item["id"])]
            ]
            if existing and existing.get("auto_bridge"):
                # Do not start the older saved mapping if Bambu Studio has
                # explicitly announced a different one.
                self.state["armed_job"] = None
            changed = (
                bridge.get("status") != "Correspondance AMS modifiée — confirmation requise"
                or bridge.get("mapping_conflict") != conflicts
            )
            bridge["mapping_confirmation_required"] = True
            bridge["mapping_conflict"] = conflicts
            bridge["mapping_source"] = "Commande Bambu Studio"
            bridge["status"] = "Correspondance AMS modifiée — confirmation requise"
            if changed:
                detail = ", ".join(
                    f"filament {item['filament_id']} : A{item['saved_slot']} → A{item['bambu_slot']}"
                    for item in conflicts
                )
                log(f"Passerelle: confirmation requise ({detail})")
            return changed

        if not requested_mapping and not bridge.get("fallback_enabled", True) and not force_fallback:
            changed = bridge.get("status") != "Correspondance AMS absente — confirmation requise"
            bridge["mapping_confirmation_required"] = True
            bridge["mapping_conflict"] = []
            bridge["status"] = "Correspondance AMS absente — confirmation requise"
            return changed
        job_name = ""
        if self.pending_request:
            job_name = self.pending_request.get("job", "")
        self.state["armed_job"] = {
            "token": token,
            "file": job_name or self.auto_import["filename"],
            "plate": str(plate["id"]),
            "lines": lines,
            "armed_at": now_iso(),
            "armed_epoch": time.time(),
            "auto_bridge": True,
            "mapping_source": mapping_source,
            "object_map": self.auto_import.get("object_map", {}),
        }
        bridge["mapping_source"] = mapping_source
        bridge["mapping_confirmation_required"] = False
        bridge["mapping_conflict"] = []
        bridge["status"] = f"Travail armé automatiquement ({mapping_source})"
        log(f"Passerelle: travail armé automatiquement, plateau {plate['id']}, source={mapping_source}")
        return True

    def confirm_auto_import(self) -> dict[str, Any]:
        """Explicitly accept the AMS mapping announced by Bambu Studio."""
        with self.lock:
            if not self.auto_import:
                raise ValueError("Aucun fichier Bambu Studio récent à confirmer")
            if not self._try_auto_arm_locked(force_fallback=True):
                if not self.state.get("armed_job"):
                    raise ValueError("Le fichier ne peut pas être armé automatiquement")
            self.save()
            return self.state["armed_job"]

    def use_saved_mapping_for_auto_import(self) -> dict[str, Any]:
        """Discard Bambu's conflicting request and deliberately keep saved slots."""
        with self.lock:
            if not self.auto_import:
                raise ValueError("Aucun fichier Bambu Studio récent à traiter")
            # The explicit command is the source of the conflict. Once the
            # owner chooses their saved mapping, do not let a later poll bring
            # that same command back and silently reverse the choice.
            self.pending_request = None
            if not self._try_auto_arm_locked(force_fallback=True):
                if not self.state.get("armed_job"):
                    raise ValueError("La correspondance enregistrée ne peut pas être utilisée")
            self.save()
            return self.state["armed_job"]

    def configure(self, data: dict[str, Any]) -> None:
        with self.lock:
            current = self.state["config"]
            current["ip"] = str(data.get("ip", current.get("ip", ""))).strip()
            serial = str(data.get("serial", current.get("serial", ""))).strip()
            if serial != current.get("serial", ""):
                current.pop("mqtt_certificate_sha256", None)
            current["serial"] = serial
            code = str(data.get("access_code", "")).strip()
            if code and code != "********":
                current["access_code"] = code
            camera = self.state.setdefault("camera", {})
            if "camera_enabled" in data:
                camera["enabled"] = bool(data["camera_enabled"])
            if "camera_certificate_sha256" in data:
                fingerprint = str(data["camera_certificate_sha256"] or "").strip().lower()
                if fingerprint and (len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint)):
                    raise ValueError("L’empreinte TLS de la caméra doit contenir 64 caractères hexadécimaux")
                camera["certificate_sha256"] = fingerprint
            if camera.get("enabled") and camera.get("certificate_sha256"):
                camera["status"] = (
                    f"Surveillance active — une capture toutes les "
                    f"{int(camera.get('capture_every_layers', 5) or 5)} couches"
                )
            elif not camera.get("enabled"):
                camera["status"] = "Captures automatiques désactivées"
            self.save()
        self.mqtt.restart()

    def import_bambu_studio_configuration(self, data: dict[str, Any]) -> dict[str, Any]:
        """Reuse the current Bambu Studio LAN identity for this local Companion."""
        ip = str(data.get("ip") or "").strip()
        if not ip:
            raise ValueError("L’adresse IP locale de l’imprimante est requise")
        try:
            socket.inet_aton(ip)
        except OSError as exc:
            raise ValueError("Adresse IP locale invalide") from exc
        credentials = read_bambu_studio_credentials()
        self.configure({"ip": ip, **credentials})
        return {"ok": True, "ip": ip, "serial": credentials["serial"], "access_code_imported": True}

    def discover_camera_certificate(self) -> dict[str, str]:
        with self.lock:
            host = str(self.state["config"].get("ip") or "")
        return {"fingerprint": discover_certificate_sha256(host)}

    def update_spools(self, data: dict[str, Any]) -> None:
        with self.lock:
            for slot in map(str, range(1, 5)):
                incoming = data.get(slot, {})
                spool_id = self.inventory.spool_id_for_slot(slot)
                if spool_id is not None and incoming:
                    self.inventory.update_spool(spool_id, incoming)
            self._sync_spools_from_inventory()
            self.save()

    def create_spool(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            spool = self.inventory.create_spool(data)
            self._sync_spools_from_inventory()
            self.save()
            return spool

    def assign_spool(self, data: dict[str, Any]) -> dict[str, Any]:
        slot = str(data.get("slot") or "")
        raw_spool_id = data.get("spool_id")
        spool_id = None if raw_spool_id in (None, "") else int(raw_spool_id)
        with self.lock:
            if not slot:
                if spool_id is None:
                    raise ValueError("Choisissez une bobine à retirer")
                result = self.inventory.unassign(spool_id)
            else:
                result = self.inventory.assign(slot, spool_id)
            self._sync_spools_from_inventory()
            self.save()
            return {"ok": True, **result}

    def update_inventory_spool(self, spool_id: int, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            spool = self.inventory.update_spool(spool_id, data)
            self._sync_spools_from_inventory()
            self.save()
            return spool

    def delete_inventory_spool(self, spool_id: int) -> dict[str, Any]:
        with self.lock:
            active_job = self.state.get("active_job") or {}
            active_spool_ids = {
                int(line["spool_id"])
                for line in active_job.get("lines", [])
                if line.get("spool_id") is not None
            }
            if spool_id in active_spool_ids:
                raise ValueError("Impossible de supprimer une bobine utilisée par l’impression en cours")
            result = self.inventory.delete_spool(spool_id)
            cleaned_history = []
            for job in self.state.get("history", []):
                clean = dict(job)
                lines = [line for line in job.get("lines", []) if line.get("spool_id") != spool_id]
                deductions = [line for line in job.get("deductions", []) if line.get("spool_id") != spool_id]
                clean["lines"] = lines
                if "deductions" in clean:
                    clean["deductions"] = deductions
                if lines or deductions or ("lines" not in job and "deductions" not in job):
                    cleaned_history.append(clean)
            self.state["history"] = cleaned_history
            self._sync_spools_from_inventory()
            self.save()
            return {"ok": True, **result}

    def archive_inventory_spools(self, spool_ids: list[int]) -> dict[str, Any]:
        with self.lock:
            active_ids = {
                int(line["spool_id"]) for line in (self.state.get("active_job") or {}).get("lines", [])
                if line.get("spool_id") is not None
            }
            if active_ids.intersection(spool_ids):
                raise ValueError("Impossible d’archiver une bobine utilisée par l’impression en cours")
            result = self.inventory.archive_spools(spool_ids)
            self._sync_spools_from_inventory()
            self.save()
            return {"ok": True, **result}

    def bulk_inventory_update(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            ids = [int(value) for value in data.get("ids", []) if str(value).isdigit()]
            if str(data.get("action")) == "archive":
                return self.archive_inventory_spools(ids)
            result = self.inventory.bulk_update(data)
            self._sync_spools_from_inventory()
            self.save()
            return {"ok": True, **result}

    def inventory_csv(self) -> bytes:
        with self.lock:
            return self.inventory.export_csv()

    def spool_history(self, spool_id: int) -> dict[str, Any]:
        with self.lock:
            return self.inventory.history_for_spool(spool_id)

    def import_3mf(self, raw: bytes, filename: str) -> dict[str, Any]:
        parsed = parse_3mf(raw, filename)
        with self.lock:
            self.last_import = parsed
        return parsed

    def arm(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if not self.last_import:
                raise ValueError("Importez d’abord un .gcode.3mf tranché")
            plate_id = str(data.get("plate", ""))
            plate = next((p for p in self.last_import["plates"] if str(p["id"]) == plate_id), None)
            if not plate:
                raise ValueError("Plateau introuvable")
            mappings = {str(m["filament_id"]): str(m["slot"]) for m in data.get("mappings", [])}
            lines = []
            for filament in plate["filaments"]:
                slot = mappings.get(str(filament["id"]))
                if slot not in {"1", "2", "3", "4"}:
                    raise ValueError(f"Associez le filament {filament['id']} à A1–A4")
                lines.append({"slot": slot, "used_g": filament["used_g"], "filament": filament})
            token = hashlib.sha256(f"{self.last_import['sha256']}:{plate_id}".encode()).hexdigest()
            self.state["armed_job"] = {
                "token": token, "file": self.last_import["filename"], "plate": plate_id,
                "lines": lines, "armed_at": now_iso(),
                "object_map": self.last_import.get("object_map", {}),
            }
            self.save()
            return self.state["armed_job"]

    def _record_untracked_terminal_locked(self, state: str, report: dict[str, Any], task_id: str) -> bool:
        """Keep a terminal job visible even when no 3MF could be associated.

        Bambu Studio can emit a terminal status while a print is still queued or
        being prepared. There is no active job to debit in that case, but
        silently discarding the frame makes the history misleading.
        """
        file_name = str(
            report.get("subtask_name")
            or report.get("gcode_file")
            or self.state["printer"].get("job")
            or "Impression non associée"
        )
        for recent in self.state.get("history", [])[:3]:
            if (
                recent.get("untracked")
                and recent.get("result") == state
                and recent.get("task_id", "") == task_id
                and recent.get("file") == file_name
            ):
                return False
        self.state["history"].insert(0, {
            "file": file_name,
            "task_id": task_id,
            "result": state,
            "ended_at": now_iso(),
            "deducted": False,
            "untracked": True,
            "tracking_note": (
                "Terminée sans décompte : aucun fichier 3MF associé"
                if state in TERMINAL_OK
                else "Annulée avant le démarrage du suivi filament"
            ),
        })
        self.state["history"] = self.state["history"][:100]
        log(f"Travail {state} conservé dans l’historique sans suivi actif: {file_name}")
        return True

    def on_message(self, payload: dict[str, Any]) -> None:
        """Journalise un rapport avant de le traiter, sans bloquer MQTT."""
        event_id = self.events.record(payload)
        try:
            self._on_message(payload)
        except Exception as exc:
            self.events.mark(event_id, "failed", str(exc))
            raise
        self.events.mark(event_id, "processed")

    def _on_message(self, payload: dict[str, Any]) -> None:
        report = payload.get("print")
        if not isinstance(report, dict):
            return
        with self.lock:
            rfid_changed = self._sync_rfid_from_report_locked(report)
            if rfid_changed:
                self.save()
            printer = self.state["printer"]
            raw_state = (
                report.get("gcode_state")
                or report.get("print_status")
                or printer.get("state", "INCONNU")
            )
            state = str(raw_state).upper()
            task_id = str(report.get("subtask_id") or report.get("task_id") or "")
            active = self.state.get("active_job")

            # A terminal frame for an earlier task can arrive after the next
            # print has started (for example after a local MQTT reconnect).
            # Never let that stale frame change the UI state or debit the
            # currently active task.
            if (
                state in TERMINAL_STATES
                and active
                and task_id
                and active.get("task_id")
                and task_id != active["task_id"]
            ):
                log(
                    "État terminal ignoré pour un autre travail: "
                    f"task={task_id}, actif={active['task_id']}"
                )
                return

            printer["state"] = state
            printer["progress"] = max(
                0,
                min(100, int(_float(report.get("mc_percent", printer.get("progress", 0))))),
            )
            printer["job"] = str(
                report.get("subtask_name")
                or report.get("gcode_file")
                or printer.get("job", "")
            )
            if state in RUNNING:
                self._ensure_camera_print_session_locked(report, task_id)
            self._schedule_camera_capture_locked(report, state)
            if (state in RUNNING and active and task_id and active.get("task_id")
                    and task_id != active.get("task_id")):
                # Companion may have missed the terminal frame during a network
                # outage. Never charge that stale job against a newer print.
                self.state["history"].insert(0, {
                    **active,
                    "result": "REMPLACÉ",
                    "ended_at": now_iso(),
                    "deducted": False,
                })
                self.state["history"] = self.state["history"][:100]
                self.state["active_job"] = None
                log(f"Ancien travail abandonné sans déduction: task={active.get('task_id')} remplacé par {task_id}")
                self.save()

            if state in RUNNING and not self.state.get("active_job"):
                self._try_auto_arm_locked()
            if state in RUNNING and self.state.get("armed_job") and not self.state.get("active_job"):
                active = json.loads(json.dumps(self.state["armed_job"]))
                missing_slots = []
                for line in active["lines"]:
                    spool_id = self.inventory.spool_id_for_slot(line["slot"])
                    line["spool_id"] = spool_id
                    if spool_id is None:
                        missing_slots.append(f"A{line['slot']}")
                if missing_slots:
                    active["tracking_error"] = (
                        "Bobine non enregistrée au démarrage : " + ", ".join(missing_slots)
                    )
                    log(active["tracking_error"])
                active.update({"task_id": task_id, "started_at": now_iso(), "saw_running": True})
                self.state["active_job"] = active
                session = self.state.get("camera", {}).get("active_print")
                if isinstance(session, dict) and str(session.get("task_id") or "") == task_id:
                    job_name = str(active.get("file") or "").strip()
                    if job_name:
                        session["name"] = job_name
                        session["folder"] = capture_print_folder(job_name, task_id, str(session.get("started_at") or ""))
                self.untracked_running = None
                self.state["armed_job"] = None
                # Once RUNNING has bound this task to a prepared job, a
                # previously displayed confirmation prompt is obsolete.  Keep
                # the status aligned with the real tracking state.
                bridge = self.state["bridge"]
                bridge["mapping_confirmation_required"] = False
                bridge["mapping_conflict"] = []
                bridge["status"] = "Impression en cours, suivi filament actif"
                log(f"Travail détecté: {active['file']} plateau {active['plate']} task={task_id or '?'}")
                self.save()
            active = self.state.get("active_job")
            if not active:
                if state in RUNNING:
                    self.untracked_running = {
                        "task_id": task_id,
                        "file": self.state["printer"].get("job", ""),
                        "started_at": now_iso(),
                    }
                    return
                saw_untracked_running = bool(self.untracked_running) and (
                    not task_id
                    or not self.untracked_running.get("task_id")
                    or task_id == self.untracked_running.get("task_id")
                )
                should_record = state in TERMINAL_BAD or (state in TERMINAL_OK and saw_untracked_running)
                if state in TERMINAL_STATES:
                    self.untracked_running = None
                if should_record and self._record_untracked_terminal_locked(state, report, task_id):
                    self.state["bridge"]["status"] = (
                        "Impression terminée sans décompte, en attente de Bambu Studio"
                        if state in TERMINAL_OK
                        else "Impression annulée, en attente de Bambu Studio"
                    )
                    self.save()
                if state in TERMINAL_STATES:
                    self._finalize_camera_print_session_locked(
                        state,
                        task_id,
                        str(report.get("subtask_name") or report.get("gcode_file") or printer.get("job") or ""),
                    )
                    self.save()
                return
            # An application restart resumes an already bound job without
            # passing through the arming block above.  Clear any obsolete
            # mapping-confirmation message as soon as its RUNNING frame is
            # received, while keeping the existing job and its debit intact.
            if state in RUNNING and active.get("saw_running"):
                bridge = self.state["bridge"]
                if (
                    bridge.get("mapping_confirmation_required")
                    or bridge.get("mapping_conflict")
                    or bridge.get("status") != "Impression en cours, suivi filament actif"
                ):
                    bridge["mapping_confirmation_required"] = False
                    bridge["mapping_conflict"] = []
                    bridge["status"] = "Impression en cours, suivi filament actif"
                    self.save()
            if task_id and not active.get("task_id"):
                active["task_id"] = task_id
            if state in TERMINAL_BAD:
                self._finalize_camera_print_session_locked(state, task_id, str(active.get("file") or ""))
                self.state["history"].insert(0, {**active, "result": state, "ended_at": now_iso(), "deducted": False})
                self.state["history"] = self.state["history"][:100]
                self.state["active_job"] = None
                self.auto_import = None
                self.pending_request = None
                self.state["bridge"].pop("recent_import", None)
                self.state["bridge"]["status"] = "Impression arrêtée, en attente de Bambu Studio"
                log(f"Travail {state}: aucune déduction")
                self.save()
            elif state in TERMINAL_OK and active.get("saw_running"):
                key = f"{self.state['config'].get('serial','')}:{active.get('task_id') or active['token']}"
                missing_slots = [
                    line["slot"]
                    for line in active["lines"]
                    if not line.get("spool_id") and not self.inventory.spool_id_for_slot(line["slot"])
                ]
                if missing_slots:
                    self._finalize_camera_print_session_locked(state, task_id, str(active.get("file") or ""))
                    self.state["history"].insert(0, {
                        **active,
                        "result": "SUIVI_INCOMPLET",
                        "ended_at": now_iso(),
                        "deducted": False,
                    })
                    self.state["history"] = self.state["history"][:100]
                    self.state["active_job"] = None
                    self.auto_import = None
                    self.pending_request = None
                    self.state["bridge"].pop("recent_import", None)
                    self.state["bridge"]["status"] = "Impression terminée sans décompte (bobine non enregistrée)"
                    log(f"Travail terminé sans décompte: bobine absente dans A{', A'.join(missing_slots)}")
                    self.save()
                    return
                if key not in self.state["accounted"]:
                    settlement_lines = []
                    for line in active["lines"]:
                        spool_id = line.get("spool_id") or self.inventory.spool_id_for_slot(line["slot"])
                        if spool_id is None:
                            raise ValueError(f"Aucune bobine enregistrée dans A{line['slot']}")
                        settlement_lines.append({
                            "slot": line["slot"], "spool_id": int(spool_id), "used_g": line["used_g"],
                        })
                    deductions, newly_settled = self.inventory.settle_print(key, settlement_lines)
                    self._sync_spools_from_inventory()
                    self.state["accounted"].append(key)
                    self.state["accounted"] = self.state["accounted"][-1000:]
                    self.state["history"].insert(0, {**active, "result": state, "ended_at": now_iso(), "deducted": True, "deductions": deductions})
                    log(f"Travail {'terminé et débité' if newly_settled else 'déjà comptabilisé'}: {key}")
                self._finalize_camera_print_session_locked(state, task_id, str(active.get("file") or ""))
                self.state["history"] = self.state["history"][:100]
                self.state["active_job"] = None
                self.auto_import = None
                self.pending_request = None
                self.state["bridge"].pop("recent_import", None)
                self.state["bridge"]["status"] = "Impression terminée, en attente de Bambu Studio"
                self.save()


    def _ensure_camera_print_session_locked(
        self, report: dict[str, Any], task_id: str, *, adopt_ungrouped: bool = False
    ) -> dict[str, Any]:
        """Start (or resume) the camera session belonging to the running print."""
        camera = self.state.setdefault("camera", {})
        name = str(
            report.get("subtask_name")
            or report.get("gcode_file")
            or self.state.get("printer", {}).get("job")
            or "Impression Bambu"
        ).strip()
        current = camera.get("active_print")
        if isinstance(current, dict):
            current_task = str(current.get("task_id") or "")
            if not task_id or not current_task or current_task == task_id:
                if task_id:
                    current["task_id"] = task_id
                if name:
                    current["name"] = name
                return current
            # A new task arrived without the terminal MQTT frame of the old
            # one.  Preserve the first session before creating the next.
            self._finalize_camera_print_session_locked("REMPLACÉ", current_task, str(current.get("name") or ""))

        started_at = now_iso()
        session_id = hashlib.sha256(f"{started_at}:{task_id}:{name}".encode("utf-8")).hexdigest()[:16]
        session = {
            "id": session_id,
            "task_id": task_id,
            "name": name,
            "started_at": started_at,
            "folder": capture_print_folder(name, task_id, started_at),
        }
        camera["active_print"] = session
        if adopt_ungrouped:
            for image in camera.get("captures", []):
                if isinstance(image, dict) and not image.get("print_id") and not image.get("folder"):
                    image["print_id"] = session_id
        log(f"Vision: session de captures ouverte pour {name}")
        return session

    def _finalize_camera_print_session_locked(self, result: str, task_id: str, fallback_name: str) -> None:
        """Move the session's images into its own print directory exactly once."""
        camera = self.state.setdefault("camera", {})
        session = camera.get("active_print")
        if not isinstance(session, dict):
            return
        session_task = str(session.get("task_id") or "")
        if task_id and session_task and task_id != session_task:
            return
        if task_id and not session_task:
            session["task_id"] = task_id
        if fallback_name and not session.get("name"):
            session["name"] = fallback_name
        session_id = str(session.get("id") or "")
        folder_name = str(session.get("folder") or "")
        if not session_id or not re.fullmatch(r"print-[a-zA-Z0-9._-]+", folder_name):
            log("Vision: session de captures invalide, rangement ignoré")
            return
        root = self.state_path.parent / "captures"
        destination = root / folder_name
        secure_directory(destination)
        moved = 0
        for image in camera.get("captures", []):
            if not isinstance(image, dict) or image.get("print_id") != session_id:
                continue
            filename = str(image.get("file") or "")
            if not re.fullmatch(r"layer-\d{5}-\d{8}-\d{6}\.jpg", filename):
                continue
            source = root / filename
            target = destination / filename
            try:
                if source.is_file():
                    shutil.move(str(source), str(target))
                    os.chmod(target, 0o600)
                if target.is_file():
                    image["folder"] = folder_name
                    image["print_name"] = str(session.get("name") or fallback_name or "Impression Bambu")
                    moved += 1
            except OSError as exc:
                log(f"Vision: déplacement impossible pour {filename}: {exc}")
        completed = camera.setdefault("completed_prints", [])
        completed.insert(0, {
            "id": session_id,
            "task_id": session.get("task_id") or task_id,
            "name": session.get("name") or fallback_name or "Impression Bambu",
            "folder": folder_name,
            "started_at": session.get("started_at"),
            "ended_at": now_iso(),
            "result": result,
            "capture_count": moved,
        })
        camera["completed_prints"] = completed[:100]
        camera["active_print"] = None
        camera["status"] = f"{moved} capture(s) rangée(s) dans {folder_name}"
        log(f"Vision: {moved} capture(s) déplacée(s) vers {folder_name}")

    def delete_capture_print(self, folder_name: str) -> dict[str, Any]:
        """Delete one completed print's camera folder and its indexed images."""
        if not re.fullmatch(r"print-[a-zA-Z0-9._-]+", folder_name):
            raise ValueError("Dossier de captures invalide")
        with self.lock:
            camera = self.state.setdefault("camera", {})
            completed = camera.get("completed_prints", [])
            target = next(
                (item for item in completed if isinstance(item, dict) and item.get("folder") == folder_name),
                None,
            )
            if not target:
                raise ValueError("Impression de captures introuvable")
            root = self.state_path.parent / "captures"
            folder = root / folder_name
            if folder.exists():
                if not folder.is_dir() or folder.is_symlink():
                    raise ValueError("Dossier de captures non valide")
                shutil.rmtree(folder)
            before = len(camera.get("captures", []))
            camera["captures"] = [
                image for image in camera.get("captures", [])
                if not (isinstance(image, dict) and image.get("folder") == folder_name)
            ]
            deleted = before - len(camera["captures"])
            camera["completed_prints"] = [item for item in completed if item is not target]
            camera["status"] = f"{deleted} capture(s) supprimée(s) — espace libéré"
            self.save()
            log(f"Vision: dossier de captures supprimé {folder_name} ({deleted} image(s))")
            return {"ok": True, "deleted": deleted, "folder": folder_name}

    def delete_active_capture_session(self, session_id: str) -> dict[str, Any]:
        """Free space from the currently running print without stopping it."""
        if not re.fullmatch(r"[a-f0-9]{16}", session_id):
            raise ValueError("Session de captures invalide")
        with self.lock:
            camera = self.state.setdefault("camera", {})
            active = camera.get("active_print")
            if not isinstance(active, dict) or active.get("id") != session_id:
                raise ValueError("Cette impression n’est plus en cours")
            root = self.state_path.parent / "captures"
            kept: list[dict[str, Any]] = []
            deleted = 0
            for image in camera.get("captures", []):
                if not isinstance(image, dict) or image.get("print_id") != session_id:
                    kept.append(image)
                    continue
                filename = str(image.get("file") or "")
                if re.fullmatch(r"layer-\d{5}-\d{8}-\d{6}\.jpg", filename):
                    try:
                        (root / filename).unlink(missing_ok=True)
                    except OSError as exc:
                        log(f"Vision: suppression impossible pour {filename}: {exc}")
                        kept.append(image)
                        continue
                deleted += 1
            camera["captures"] = kept
            camera["status"] = f"{deleted} capture(s) de l’impression en cours supprimée(s)"
            self.save()
            log(f"Vision: captures en cours supprimées ({deleted} image(s))")
            return {"ok": True, "deleted": deleted, "session_id": session_id}

    def _schedule_camera_capture_locked(self, report: dict[str, Any], state: str) -> None:
        """Queue one future camera capture at each configured layer interval."""
        # Vision is observational.  It must also cover a print already in
        # progress when Companion starts, even if the consumption bridge did
        # not arm that job first.
        if state not in RUNNING:
            return
        camera = self.state.setdefault("camera", {})
        try:
            layer = int(float(report.get("layer_num", report.get("layer", report.get("current_layer", 0)))))
        except (TypeError, ValueError):
            return
        every = int(camera.get("capture_every_layers", 5) or 5)
        if layer <= 0 or every <= 0:
            return
        camera["last_seen_layer"] = max(int(camera.get("last_seen_layer", 0) or 0), layer)
        if layer % every or layer <= int(camera.get("last_requested_layer", 0) or 0):
            return
        camera["last_requested_layer"] = layer
        camera["pending_capture_layer"] = layer
        active_print = camera.get("active_print")
        camera["pending_capture_session_id"] = (
            str(active_print.get("id") or "") if isinstance(active_print, dict) else ""
        )
        camera["status"] = f"Capture demandée à la couche {layer}"
        # Persist the request before starting the daemon.  A macOS relaunch
        # can otherwise kill the thread between this point and the JPEG save.
        self.save()
        log(f"Caméra: capture planifiée à la couche {layer}")
        if camera.get("enabled") and camera.get("certificate_sha256"):
            threading.Thread(target=self._capture_pending_camera, daemon=True).start()

    def _capture_pending_camera(self) -> None:
        """Capture outside the MQTT lock; failures remain local and observable."""
        with self.lock:
            camera = self.state.setdefault("camera", {})
            layer = int(camera.get("pending_capture_layer", 0) or 0)
            if not layer or camera.get("capture_in_progress"):
                return
            camera["capture_in_progress"] = True
            session_id = str(camera.get("pending_capture_session_id") or "")
            host = str(self.state["config"].get("ip") or "")
            code = str(self.state["config"].get("access_code") or "")
            fingerprint = str(camera.get("certificate_sha256") or "")
        try:
            frame = capture_jpeg(host, code, fingerprint)
            root = self.state_path.parent / "captures"
            secure_directory(root)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            filename = f"layer-{layer:05d}-{stamp}.jpg"
            with self.lock:
                completed = next(
                    (item for item in self.state["camera"].get("completed_prints", [])
                     if isinstance(item, dict) and item.get("id") == session_id),
                    None,
                )
                completed_folder = str(completed.get("folder") or "") if completed else ""
            image_folder = root
            if re.fullmatch(r"print-[a-zA-Z0-9._-]+", completed_folder):
                image_folder = root / completed_folder
                secure_directory(image_folder)
            image_path = image_folder / filename
            image_path.write_bytes(frame.jpeg)
            os.chmod(image_path, 0o600)
            result = {"layer": layer, "captured_at": now_iso(), "file": filename,
                      "sha256": frame.sha256, "size_bytes": len(frame.jpeg), "print_id": session_id}
            if completed_folder:
                result["folder"] = completed_folder
                result["print_name"] = str(completed.get("name") or "Impression Bambu")
            with self.lock:
                camera = self.state["camera"]
                camera.setdefault("captures", []).insert(0, result)
                camera["captures"] = camera["captures"][:200]
                camera["pending_capture_layer"] = 0
                camera["pending_capture_session_id"] = ""
                camera["status"] = f"Capture enregistrée — couche {layer}"
                self.save()
        except CameraError as exc:
            with self.lock:
                camera = self.state["camera"]
                camera["pending_capture_layer"] = 0
                camera["pending_capture_session_id"] = ""
                camera["status"] = f"Capture caméra impossible : {exc}"
                self.save()
        finally:
            with self.lock:
                camera = self.state["camera"]
                camera["capture_in_progress"] = False
                self.save()


class Handler(BaseHTTPRequestHandler):
    server_version = f"AMSLiteCompanion/{__version__}"

    @property
    def app(self) -> Companion:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    @property
    def api_token(self) -> str:
        return self.server.api_token  # type: ignore[attr-defined]

    def _local_request_is_valid(self) -> bool:
        port = self.server.server_port
        allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if self.headers.get("Host", "") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in allowed_origins:
            return False
        supplied = self.headers.get("X-AMS-Token", "")
        return bool(supplied) and secrets.compare_digest(supplied, self.api_token)

    def _require_api_access(self) -> bool:
        if self._local_request_is_valid():
            return True
        self.send_json({"error": "Accès local non autorisé"}, 403)
        return False

    def _local_capture_request_is_valid(self, query: dict[str, list[str]]) -> bool:
        """Image tags cannot attach our API header; keep this scoped to localhost.

        The page is rendered with the per-launch local token, so a capture URL
        is still unusable from another process without that page's token.
        """
        port = self.server.server_port
        if self.headers.get("Host", "") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return False
        supplied = query.get("token", [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, self.api_token)

    def send_jpeg(self, raw: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_json(self, value: Any, status: int = 200) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_csv(self, raw: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="ams-lite-catalogue.csv"')
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Longueur de requête invalide") from exc
        if not 0 <= length <= MAX_IMPORT_BYTES:
            raise ValueError("Fichier trop volumineux (32 Mo maximum)")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("Requête incomplète")
        return body

    def json_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            raise ValueError("Content-Type application/json requis")
        value = json.loads(self.body())
        if not isinstance(value, dict):
            raise ValueError("Objet JSON requis")
        return value

    def do_GET(self) -> None:
        request_url = urllib.parse.urlparse(self.path)
        path = request_url.path
        if self.path == "/" or self.path.startswith("/?"):
            query = urllib.parse.parse_qs(request_url.query)
            raw = (render_vision_html(self.api_token) if query.get("vision") else render_html(self.api_token)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; object-src 'none'")
            self.end_headers()
            self.wfile.write(raw)
        elif path == "/api/health":
            self.send_json({"ok": True})
        elif match := re.fullmatch(r"/api/captures/(layer-\d{5}-\d{8}-\d{6}\.jpg)", path):
            query = urllib.parse.parse_qs(request_url.query)
            if not self._local_capture_request_is_valid(query):
                self.send_json({"error": "Accès local non autorisé"}, 403)
                return
            filename = match.group(1)
            with self.app.lock:
                listed = next(
                    (item for item in self.app.state.get("camera", {}).get("captures", [])
                     if isinstance(item, dict) and item.get("file") == filename),
                    None,
                )
            folder = str(listed.get("folder") or "") if listed else ""
            if folder and not re.fullmatch(r"print-[a-zA-Z0-9._-]+", folder):
                self.send_error(404)
                return
            image_path = self.app.state_path.parent / "captures" / folder / filename if folder else (
                self.app.state_path.parent / "captures" / filename
            )
            if not listed or not image_path.is_file():
                self.send_error(404)
                return
            try:
                self.send_jpeg(image_path.read_bytes())
            except OSError:
                self.send_error(404)
        elif not self._require_api_access():
            return
        elif path == "/api/state":
            self.send_json(self.app.public_state())
        elif path == "/api/events":
            self.send_json({"events": self.app.events.recent(100)})
        elif path == "/api/report.json":
            self.send_json(self.app.supervision_report())
        elif path == "/api/inventory/export.csv":
            self.send_csv(self.app.inventory_csv())
        elif match := re.fullmatch(r"/api/inventory/spools/(\d+)/history", path):
            self.send_json(self.app.spool_history(int(match.group(1))))
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        try:
            request_url = urllib.parse.urlparse(self.path)
            path = request_url.path
            if not self._require_api_access():
                return
            if path == "/api/config":
                self.app.configure(self.json_body())
                self.send_json({"ok": True})
            elif path == "/api/config/import-bambu-studio":
                self.send_json(self.app.import_bambu_studio_configuration(self.json_body()))
            elif path == "/api/camera/discover":
                self.json_body()
                self.send_json(self.app.discover_camera_certificate())
            elif match := re.fullmatch(r"/api/captures/(print-[a-zA-Z0-9._-]+)/delete", path):
                self.json_body()
                self.send_json(self.app.delete_capture_print(match.group(1)))
            elif match := re.fullmatch(r"/api/captures/session/([a-f0-9]{16})/delete", path):
                self.json_body()
                self.send_json(self.app.delete_active_capture_session(match.group(1)))
            elif path == "/api/bridge":
                self.app.configure_bridge(self.json_body())
                self.send_json({"ok": True})
            elif path == "/api/spools":
                self.app.update_spools(self.json_body())
                self.send_json({"ok": True})
            elif path == "/api/inventory/spools":
                self.send_json(self.app.create_spool(self.json_body()), 201)
            elif path == "/api/inventory/assign":
                self.send_json(self.app.assign_spool(self.json_body()))
            elif match := re.fullmatch(r"/api/inventory/spools/(\d+)/archive", path):
                self.json_body()
                self.send_json(self.app.archive_inventory_spools([int(match.group(1))]))
            elif match := re.fullmatch(r"/api/inventory/spools/(\d+)/delete", path):
                self.json_body()
                self.send_json(self.app.delete_inventory_spool(int(match.group(1))))
            elif path == "/api/inventory/bulk":
                self.send_json(self.app.bulk_inventory_update(self.json_body()))
            elif match := re.fullmatch(r"/api/inventory/spools/(\d+)", path):
                self.send_json(self.app.update_inventory_spool(int(match.group(1)), self.json_body()))
            elif path == "/api/import":
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type not in {"application/octet-stream", "application/zip", ""}:
                    raise ValueError("Type de fichier 3MF invalide")
                query = urllib.parse.parse_qs(request_url.query)
                filename = query.get("filename", ["travail.3mf"])[0]
                self.send_json(self.app.import_3mf(self.body(), filename))
            elif path == "/api/arm":
                self.send_json(self.app.arm(self.json_body()))
            elif path == "/api/bridge/confirm":
                self.json_body()
                self.send_json(self.app.confirm_auto_import())
            elif path == "/api/bridge/use-saved":
                self.json_body()
                self.send_json(self.app.use_saved_mapping_for_auto_import())
            elif path == "/api/guardian/observe":
                self.send_json(self.app.observe_plate_guardian(self.json_body()), 201)
            elif match := re.fullmatch(r"/api/guardian/proposals/([a-f0-9]{32})/decision", path):
                self.send_json(self.app.decide_plate_guardian(match.group(1), self.json_body()))
            elif path == "/api/shutdown":
                self.json_body()
                self.send_json({"ok": True, "message": "Companion arrêté proprement"})
                log("Arrêt demandé depuis le tableau de bord")
                # shutdown() must run outside the request-handling thread.
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_error(404)
        except Exception as exc:
            log(f"Erreur API {self.path}: {exc}")
            self.send_json({"error": "Requête invalide ou donnée non exploitable"}, 400)


HTML = r'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AMS Lite Companion V2</title><style>
body.embedded .spools-card{order:1!important}body.embedded .printer-card{order:2!important}.inventory-card{display:none}.catalog-fields{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.catalog-actions button{width:100%}#catalogWindow{display:none}.catalog-window{max-width:1400px;margin:auto}.catalog-toolbar{display:flex;justify-content:space-between;align-items:end;gap:18px}.catalog-toolbar h2{font-size:24px;margin:0}.table-wrap{overflow:auto;border:1px solid #dfe3e7;border-radius:12px;background:white}.catalog-table{width:100%;border-collapse:collapse;min-width:1120px}.catalog-table th{background:#f0f3f5;color:#4e5863;text-align:left;font-size:12px;white-space:nowrap}.catalog-table th,.catalog-table td{padding:9px;border-bottom:1px solid #e7eaed;vertical-align:middle}.catalog-table tr:last-child td{border-bottom:0}.catalog-table tr[data-spool]{cursor:pointer}.catalog-table tr.selected td{background:#eaf8ef}.catalog-table input,.catalog-table select{min-width:90px;padding:7px;border:1px solid transparent;background:transparent;border-radius:6px}.catalog-table input:focus,.catalog-table select:focus{background:white;border-color:#00ae42;outline:none}.catalog-table .id-cell{color:#69717b;font-variant-numeric:tabular-nums}.catalog-table .actions{white-space:nowrap}.catalog-table .actions button{margin:0 3px 0 0;padding:8px 10px;font-size:12px}.catalog-add{display:grid;grid-template-columns:1.5fr repeat(3,1fr) .8fr .8fr .9fr auto;gap:8px;align-items:end;margin-top:14px;padding:14px;background:white;border:1px solid #dfe3e7;border-radius:12px}.catalog-add label{margin-top:0}.spool-timeline{margin-top:16px;padding:16px;background:white;border:1px solid #dfe3e7;border-radius:12px}.spool-timeline h3{margin:0 0 4px}.timeline{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(170px,1fr);gap:12px;overflow-x:auto;padding:26px 4px 6px;position:relative}.timeline:before{content:'';position:absolute;left:26px;right:26px;top:34px;height:3px;background:#cdebd8}.timeline-event{position:relative;z-index:1;padding-top:20px}.timeline-dot{position:absolute;top:0;left:12px;width:18px;height:18px;border-radius:50%;background:#00ae42;border:4px solid #eaf8ef}.timeline-event.remove .timeline-dot{background:#ef9b20}.timeline-event.deduct .timeline-dot{background:#3976db}.timeline-event .when{font-size:11px;color:#69717b}.timeline-event .what{font-weight:700;font-size:13px;margin:5px 0}.timeline-event .detail{font-size:12px;color:#505861}.timeline-empty{color:#69717b;padding:16px 0}body.catalog-view .wrap{max-width:none;padding:20px}body.catalog-view h1,body.catalog-view .sub,body.catalog-view .grid{display:none}body.catalog-view #catalogWindow{display:block}@media(max-width:700px){.catalog-fields{grid-template-columns:1fr 1fr}.catalog-add{grid-template-columns:1fr 1fr}}
.catalog-summary{margin:16px 0}.catalog-summary h3{margin:0 0 8px}.summary-table{min-width:720px}.level-track{width:130px;height:8px;background:#e8edf0;border-radius:99px;overflow:hidden;margin-top:4px}.level-fill{height:100%;background:#00a23d;border-radius:99px}.weight-chart{margin:14px 0 4px;padding:12px;border:1px solid #e7eaed;border-radius:10px;background:#fbfcfc}.weight-chart h4{margin:0 0 4px}.weight-chart svg{width:100%;height:190px;display:block}.weight-chart .axis{stroke:#dfe3e7;stroke-width:1}.weight-chart .line{fill:none;stroke:#00a23d;stroke-width:3;stroke-linejoin:round;stroke-linecap:round}.weight-chart .point{fill:#fff;stroke:#00a23d;stroke-width:3}.weight-chart .point:hover{fill:#00a23d;cursor:help}.chart-label{font-size:11px;fill:#69717b}
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#20242a;background:#f4f5f6}body{margin:0}.wrap{max-width:1050px;margin:auto;padding:24px}h1{margin:0 0 4px}.sub{color:#69717b;margin-bottom:20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}.card{background:white;border:1px solid #dfe3e7;border-radius:14px;padding:18px;box-shadow:0 2px 10px #0000000b}.wide{grid-column:1/-1}h2{font-size:17px;margin:0 0 14px}label{display:block;font-size:12px;color:#656d76;margin:9px 0 4px}input,select,button{box-sizing:border-box;border:1px solid #cbd1d7;border-radius:8px;padding:9px;font:inherit}input,select{width:100%}input[type=checkbox]{width:auto;margin-right:7px}button{background:#00ae42;color:white;border:0;font-weight:600;cursor:pointer;margin-top:12px}button.secondary{background:#59636e}.status{display:inline-flex;gap:7px;align-items:center;font-weight:600}.dot{width:10px;height:10px;border-radius:50%;background:#d33}.on .dot{background:#00ae42}.spools{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.spool{padding:12px;border:1px solid #e1e4e7;border-radius:10px}.spool b{color:#00a23d}.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.bridge-map,.catalog-form{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.catalog{display:grid;grid-template-columns:1fr 160px;gap:12px;align-items:end;border-top:1px solid #eee;padding:12px 0}.catalog:first-child{border-top:0;padding-top:0}.check{font-size:14px;color:#20242a}.notice{padding:10px;border-radius:8px;background:#eef8f1;margin:10px 0}.guardian-alert{background:#fff4e9;color:#8a3d00}.guardian-actions{display:flex;gap:8px;flex-wrap:wrap}.guardian-actions button{margin-top:8px}.object-map{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px}.object-chip{border:1px solid #dfe3e7;border-radius:8px;padding:9px;font-size:12px;overflow-wrap:anywhere}.object-chip b{color:#0b6d32}.object-chip span{color:#69717b}.error{background:#ffecec;color:#a11}.muted{color:#69717b;font-size:13px;overflow-wrap:anywhere}.line{display:grid;grid-template-columns:1fr 100px 90px;gap:8px;align-items:end}.history{font-size:13px;border-top:1px solid #eee;padding:8px 0}body.embedded .wrap{padding:10px;max-width:none}body.embedded h1,body.embedded .sub,body.embedded .manual-card,body.embedded .shutdown-card,body.embedded .inventory-card{display:none}body.embedded .grid{grid-template-columns:1fr;gap:10px}body.embedded .wide{grid-column:auto}body.embedded .card{padding:14px;border-radius:10px;box-shadow:none}body.embedded .spools-card{order:1}body.embedded .printer-card{order:2}body.embedded .bridge-card{order:3}body.embedded .guardian-card{order:4}body.embedded .gcode-card{order:5}body.embedded .history-card{order:6}@media(max-width:700px){.spools,.bridge-map,.catalog-form{grid-template-columns:1fr 1fr}.catalog{grid-template-columns:1fr}.line{grid-template-columns:1fr}.wrap{padding:12px}}</style></head><body><div class="wrap">
<h1>AMS Lite Companion V2</h1><div class="sub">Compteur local v2.2.0 — environnement indépendant lié à Bambu Studio officiel.</div><div id="msg"></div>
<div class="grid"><section class="card printer-card"><h2>Imprimante locale</h2><div id="conn" class="status"><span class="dot"></span><span>Déconnectée</span></div><div id="pstate"></div>
<label>Adresse IP</label><input id="ip" placeholder="192.168.1.50"><label>Numéro de série</label><input id="serial" placeholder="01S00A..."><label>Code d’accès LAN <span class="muted">(laisse vide pour conserver le code enregistré)</span></label><input id="code" type="password" placeholder="8 chiffres"><button onclick="saveConfig()">Enregistrer et connecter</button></section>
<section class="card bridge-card"><h2>Passerelle Bambu Studio</h2><div id="bridgeStatus" class="notice">En attente de Bambu Studio</div><label class="check"><input id="autoEnabled" type="checkbox">Récupérer automatiquement le .gcode.3mf</label><label class="check"><input id="fallbackEnabled" type="checkbox">Armer avec la correspondance A1–A4 enregistrée ci-dessous</label><div class="bridge-map" id="bridgeMap"></div><button onclick="saveBridge()">Enregistrer la passerelle</button><div id="bridgeDetails" class="muted"></div></section>
<section class="card wide guardian-card"><h2>Gardien de plateau</h2><div id="guardianStatus" class="notice">Initialisation du gardien…</div><div id="guardianDetails" class="muted"></div></section>
<section class="card wide autopilot-card"><h2>AutoPilot</h2><div id="autopilot" class="notice">Vérification des garde-fous…</div></section>
<section class="card wide gcode-card"><h2>Cartographie G-code</h2><div id="gcodeMap" class="muted">Analyse du plateau…</div></section>
<section class="card wide spools-card"><h2>Bobines actuellement dans l’AMS Lite</h2><div id="rfidStatus" class="muted">En attente de lecture RFID</div><div class="spools" id="spools"></div><button onclick="saveSpools()">Enregistrer les poids</button><button class="secondary" onclick="openCatalog()">Gérer le catalogue de bobines…</button><button class="secondary" onclick="openVision()">Ouvrir le centre Vision…</button></section>
<section class="card wide history-card"><h2>Historique</h2><div id="history">Aucun travail comptabilisé.</div></section>
<section class="card wide audit-card"><h2>Journal de fiabilité</h2><div id="audit" class="muted">Aucun événement MQTT enregistré.</div><button class="secondary" onclick="exportSupervisionReport()">Télécharger le rapport de supervision</button></section>
<section class="card wide shutdown-card"><h2>Companion</h2><p>Utilise ce bouton après l’impression pour enregistrer et arrêter complètement Companion.</p><button class="secondary" onclick="shutdownCompanion()">Arrêter Companion</button></section></div><section id="catalogWindow" class="catalog-window"><div class="catalog-toolbar"><div><h2>Gestionnaire de bobines</h2><p class="muted">Stock, emplacement, alertes et historique. Conçu pour rester fluide avec plusieurs centaines de bobines.</p></div><button class="secondary no-top" onclick="exportCatalog()">Exporter CSV</button></div><section id="inventoryKpis" class="inventory-kpis"></section><section class="catalog-controls"><input id="catalogSearch" type="search" oninput="setCatalogFilter('query',this.value)" placeholder="Rechercher nom, matière, marque, couleur, emplacement…"><select id="catalogMaterial" onchange="setCatalogFilter('material',this.value)"></select><select id="catalogBrand" onchange="setCatalogFilter('brand',this.value)"></select><select id="catalogLocation" onchange="setCatalogFilter('location',this.value)"></select><select id="catalogStatus" onchange="setCatalogFilter('status',this.value)"><option value="all">Tous les états</option><option value="low">À commander</option><option value="ams">Dans l’AMS</option><option value="unlocated">Emplacement à définir</option></select><select id="catalogSort" onchange="setCatalogFilter('sort',this.value)"><option value="name">Trier : nom</option><option value="remaining">Trier : stock restant</option><option value="recent">Trier : dernière utilisation</option><option value="created">Trier : ajout récent</option></select></section><section id="bulkBar" class="bulk-bar"><strong id="selectionCount">Aucune sélection</strong><input id="bulkLocation" placeholder="Emplacement, ex. Étagère B-03"><button class="secondary no-top" onclick="runBulk('location')">Déplacer</button><input id="bulkThreshold" type="number" min="0" step="1" placeholder="Seuil g"><button class="secondary no-top" onclick="runBulk('threshold')">Seuil</button><button class="danger no-top" onclick="runBulk('archive')">Archiver</button></section><div class="table-wrap scalable-table"><table class="catalog-table"><thead><tr><th><input id="selectAllCatalog" type="checkbox" onchange="togglePageSelection(this.checked)" title="Sélectionner la page"></th><th>Bobine</th><th>Matière / couleur</th><th>Emplacement</th><th>Stock</th><th>Dernière utilisation</th><th>Impr.</th><th></th></tr></thead><tbody id="catalog"></tbody></table></div><div id="catalogPager" class="catalog-pager"></div><section id="spoolDetail" class="spool-detail"><h3>Fiche de bobine</h3><p class="muted">Sélectionne une bobine dans la liste pour consulter ou modifier sa fiche.</p></section><section class="catalog-add"><div><label>Nom descriptif <span class="muted">(automatique)</span></label><input id="newSpoolName" oninput="this.dataset.custom='1'" placeholder="PLA bleu mat"></div><div><label>Matière</label><input id="newSpoolMaterial" oninput="autoNewSpoolName()" placeholder="PLA"></div><div><label>Marque</label><input id="newSpoolBrand" placeholder="Bambu Lab"></div><div><label>Couleur</label><input id="newSpoolColor" oninput="autoNewSpoolName()" placeholder="Bleu"></div><div><label>Initial (g)</label><input id="newSpoolInitial" type="number" min="0" step="0.1" value="1000"></div><div><label>Restant (g)</label><input id="newSpoolRemaining" type="number" min="0" step="0.1" value="1000"></div><div><label>Emplacement</label><input id="newSpoolLocation" placeholder="Étagère B-03"></div><div><label>Seuil (g)</label><input id="newSpoolThreshold" type="number" min="0" step="1" value="100"></div><div><label>Date d’ajout</label><input id="newSpoolDate" type="date"></div><button onclick="createSpool()">Ajouter</button></section></section></div>
<script>
const embedded=new URLSearchParams(location.search).get('embedded')==='1',catalogView=new URLSearchParams(location.search).get('catalog')==='1',apiToken='__API_TOKEN__';if(embedded)document.body.classList.add('embedded');if(catalogView)document.body.classList.add('catalog-view');let S=null, imported=null, formDirty=false, selectedSpoolId=null, pendingDeleteId=null, catalogLoaded=false,catalogState={query:'',material:'all',brand:'all',location:'all',status:'all',sort:'name',page:0,pageSize:50,selected:new Set()};const $=id=>document.getElementById(id);function msg(t,e=false){$('msg').textContent=t||'';$('msg').className=t?`notice ${e?'error':''}`:''}function openCatalog(){if(window.webkit?.messageHandlers?.companion)window.webkit.messageHandlers.companion.postMessage('openCatalog');else window.open('/?catalog=1','ams-lite-catalog')}
const visualStyle=document.createElement('style');visualStyle.textContent='.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}.catalog-summary{margin:16px 0}.catalog-summary h3{margin:0 0 8px}.summary-table{min-width:720px}.level-track{width:130px;height:8px;background:#e8edf0;border-radius:99px;overflow:hidden;margin-top:4px}.level-fill{height:100%;background:#00a23d;border-radius:99px}.weight-chart{margin:14px 0 4px;padding:12px;border:1px solid #e7eaed;border-radius:10px;background:#fbfcfc}.weight-chart h4{margin:0 0 4px}.weight-chart svg{width:100%;height:190px;display:block}.weight-chart .axis{stroke:#dfe3e7;stroke-width:1}.weight-chart .line{fill:none;stroke:#00a23d;stroke-width:3;stroke-linejoin:round;stroke-linecap:round}.weight-chart .point{fill:#fff;stroke:#00a23d;stroke-width:3}.weight-chart .point:hover{fill:#00a23d;cursor:help}.chart-label{font-size:11px;fill:#69717b}';document.head.append(visualStyle);
const advancedStyle=document.createElement('style');advancedStyle.textContent='.no-top{margin-top:0}.inventory-kpis{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:10px;margin:18px 0}.kpi{padding:14px;border:1px solid #dfe3e7;border-radius:12px;background:#fff}.kpi b{display:block;font-size:23px;color:#19222b}.kpi span{font-size:12px;color:#69717b}.kpi.alert b{color:#c34918}.catalog-controls{display:grid;grid-template-columns:2fr repeat(5,minmax(120px,1fr));gap:8px;margin:12px 0}.catalog-controls input,.catalog-controls select{margin:0}.bulk-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:12px 0;padding:10px 12px;border:1px solid #cfe6d7;border-radius:10px;background:#f5fbf7}.bulk-bar input{width:170px}.danger{background:#b5392e}.scalable-table{max-height:610px}.catalog-table tr.catalog-row:hover td{background:#f4fbf6}.catalog-table tr.catalog-row.selected td{background:#e9f7ee}.catalog-table .stock-cell{min-width:160px}.stock-line{display:flex;align-items:center;gap:8px;white-space:nowrap}.stock-line .level-track{width:76px;margin:0}.stock-low{color:#b94018;font-weight:700}.status-chip{display:inline-block;border-radius:99px;padding:3px 7px;background:#edf1f3;color:#4e5863;font-size:11px}.status-chip.low{background:#fff0e9;color:#b94018}.status-chip.ams{background:#e6f8ec;color:#087535}.catalog-pager{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:12px 0}.catalog-pager button{margin:0}.spool-detail{margin:18px 0;padding:18px;border:1px solid #dfe3e7;border-radius:12px;background:#fff}.detail-grid{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:10px}.detail-grid .detail-wide{grid-column:span 2}.detail-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.detail-actions button{margin:0}.catalog-add{grid-template-columns:repeat(5,minmax(120px,1fr)) auto}.catalog-add label{display:block}@media(max-width:900px){.inventory-kpis{grid-template-columns:repeat(2,1fr)}.catalog-controls{grid-template-columns:1fr 1fr}.detail-grid{grid-template-columns:1fr 1fr}.detail-grid .detail-wide{grid-column:span 2}.catalog-add{grid-template-columns:1fr 1fr}}';document.head.append(advancedStyle);
async function api(path,opt={}){let headers=new Headers(opt.headers||{});headers.set('X-AMS-Token',apiToken);if(opt.body&&typeof opt.body==='string'&&!headers.has('Content-Type'))headers.set('Content-Type','application/json');let r=await fetch(path,{...opt,headers,credentials:'same-origin'}),j=await r.json();if(!r.ok)throw Error(j.error||'Erreur');return j}
function render(s){S=s;if(catalogView){if(!catalogLoaded){renderCatalog(s.inventory);catalogLoaded=true}return}$('conn').className='status '+(s.printer.connected?'on':'');$('conn').lastElementChild.textContent=s.printer.connected?'Connectée':'Déconnectée';$('pstate').textContent=`${s.printer.state||''} ${s.printer.progress||0}% ${s.printer.job||''}`;$('rfidStatus').textContent=s.printer.rfid_status||'En attente de lecture RFID';
if(!formDirty){$('ip').value=s.config.ip||'';$('serial').value=s.config.serial||'';$('code').placeholder=s.config.access_code?'Code enregistré':'8 chiffres';
$('autoEnabled').checked=!!s.bridge.enabled;$('fallbackEnabled').checked=!!s.bridge.fallback_enabled;
$('bridgeMap').innerHTML=[1,2,3,4].map(i=>`<div><label>Filament ${i}</label><select id="bm${i}">${[1,2,3,4].map(slot=>`<option value="${slot}" ${String(s.bridge.default_mapping[i])==String(slot)?'selected':''}>A${slot}</option>`).join('')}</select></div>`).join('');
$('spools').innerHTML=[1,2,3,4].map(i=>{let x=s.spools[i]||{};return x.spool_id?`<div class="spool"><b>A${i}</b><label>Nom</label><input id="n${i}" value="${esc(x.name)}"><div class="row"><div><label>Initial (g)</label><input id="i${i}" type="number" step="0.1" value="${x.initial_g}"></div><div><label>Restant (g)</label><input id="r${i}" type="number" step="0.1" value="${x.remaining_g}"></div></div></div>`:`<div class="spool"><b>A${i}</b><div class="muted">Libre — choisis une bobine dans le catalogue.</div></div>`}).join('');}
if(!formDirty)renderCatalog(s.inventory);
$('bridgeStatus').textContent=s.bridge.status||'En attente de Bambu Studio';let bd=[];if(s.bridge.last_file)bd.push(`Dernier fichier : ${s.bridge.last_file}`);if(s.bridge.mapping_source)bd.push(`Correspondance : ${s.bridge.mapping_source}`);if(s.bridge.request_capture)bd.push('Capture des commandes AMS disponible sur ce Mac');let conflicts=s.bridge.mapping_conflict||[];if(conflicts.length)bd.push('Changement détecté : '+conflicts.map(x=>`filament ${x.filament_id} : A${x.saved_slot} → A${x.bambu_slot}`).join(', '));let bj=s.active_job?.auto_bridge?s.active_job:s.armed_job?.auto_bridge?s.armed_job:null;if(bj)bd.push('Décompte : '+bj.lines.map(x=>`filament ${x.filament.id} → A${x.slot} (${x.used_g} g)`).join(', '));let confirmButton=s.bridge.mapping_confirmation_required?'<button class="secondary" onclick="confirmDetectedImport()">Confirmer le changement AMS</button>':'';$('bridgeDetails').innerHTML=bd.map(esc).join('<br>')+confirmButton;
$('guardianStatus').className='notice';let guardian=s.guardian||{},pending=guardian.pending_proposals||[],capability=guardian.capability||{};if(pending.length){let proposal=pending[0];$('guardianStatus').className='notice guardian-alert';$('guardianStatus').textContent=`Alerte ${proposal.defect_type||'anomaly'} à vérifier : ${proposal.object_label} (${proposal.evidence_count} images, confiance ${Math.round(100*proposal.confidence)} %)`;$('guardianDetails').innerHTML=`V2 ne commande pas l’imprimante. Décide seulement du suivi de cette alerte.<div class="guardian-actions"><button class="secondary" onclick="decideGuardian('${proposal.id}','continue')">Continuer à surveiller</button><button class="secondary" onclick="decideGuardian('${proposal.id}','dismiss')">Écarter l’alerte</button></div>`}else{$('guardianStatus').textContent='Mode observation : aucune action imprimante n’est disponible.';$('guardianDetails').textContent=`${guardian.observations_count||0} observation(s) journalisée(s). ${capability.reason||''}`}let autopilot=s.autopilot||{},pilotCapability=autopilot.capability||{},plans=autopilot.plans||[];$('autopilot').textContent=plans.length?`${plans.length} plan(s) simulé(s) bloqué(s) par le garde-fou : ${plans.map(p=>p.object_known?`${p.defect_type||'anomaly'} · objet ${p.object_id} cartographié`:`${p.defect_type||'anomaly'} · objet ${p.object_id} non cartographié`).join(' · ')}`:(pilotCapability.reason||'Aucun plan AutoPilot actif.');
$('guardianStatus').className='notice';let guardian=s.guardian||{},pending=guardian.pending_proposals||[],capability=guardian.capability||{};if(pending.length){let proposal=pending[0];$('guardianStatus').className='notice guardian-alert';$('guardianStatus').textContent=`Alerte ${proposal.defect_type||'anomaly'} à vérifier : ${proposal.object_label} (${proposal.evidence_count} images, confiance ${Math.round(100*proposal.confidence)} %)`;$('guardianDetails').innerHTML=`V2 ne commande pas l’imprimante. Décide seulement du suivi de cette alerte.<div class="guardian-actions"><button class="secondary" onclick="decideGuardian('${proposal.id}','continue')">Continuer à surveiller</button><button class="secondary" onclick="decideGuardian('${proposal.id}','dismiss')">Écarter l’alerte</button></div>`}else{$('guardianStatus').textContent='Mode observation : aucune action imprimante n’est disponible.';$('guardianDetails').textContent=`${guardian.observations_count||0} observation(s) journalisée(s). ${capability.reason||''}`}let autopilot=s.autopilot||{},pilotCapability=autopilot.capability||{},plans=autopilot.plans||[];$('autopilot').textContent=plans.length?`${plans.length} plan(s) simulé(s) bloqué(s) par le garde-fou : ${plans.map(p=>p.object_known?`${p.defect_type||'anomaly'} · objet ${p.object_id} cartographié`:`${p.defect_type||'anomaly'} · objet ${p.object_id} non cartographié`).join(' · ')}`:(pilotCapability.reason||'Aucun plan AutoPilot actif.');let objectMap=(s.active_job||s.armed_job||{}).object_map||{},objects=Array.isArray(objectMap.objects)?objectMap.objects:[];$('gcodeMap').innerHTML=objects.length?`<p class="muted">${objects.length} objet(s) cartographié(s) par le G-code.</p><div class="object-map">${objects.map(o=>{let b=o.bounds_xy;return `<div class="object-chip"><b>${esc(o.label||('Objet '+o.id))}</b><br><span>${b?`X ${b.min_x}–${b.max_x} · Y ${b.min_y}–${b.max_y}`:'Zone XY indisponible'} · ${o.segment_count||0} segment(s)</span></div>`}).join('')}</div>`:`<p>${esc(objectMap.reason||'Aucune cartographie exploitable pour ce travail.')}</p>`;
$('history').innerHTML=s.history.length?s.history.map(h=>`<div class="history"><b>${esc(h.file||'Travail')}</b> — ${esc(h.result)} — ${h.deducted?'déduction effectuée':'aucune déduction'}${h.tracking_note?`<br><span class="muted">${esc(h.tracking_note)}</span>`:''}<br>${esc(h.ended_at||'')}</div>`).join(''):'Aucun travail enregistré.';let audit=s.events||[];$('audit').innerHTML=audit.length?audit.slice(0,8).map(e=>`<div class="history"><b>${esc(e.state||'MQTT')}</b>${e.task_id?` · ${esc(e.task_id)}`:''}${e.layer!=null?` · couche ${esc(e.layer)}`:''}${e.progress!=null?` · ${esc(e.progress)} %`:''}<br><span class="muted">${esc(e.received_at)} · ${e.outcome==='processed'?'traité':e.outcome==='failed'?'à vérifier':'reçu'}${e.detail?` · ${esc(e.detail)}`:''}</span></div>`).join(''):'Aucun événement MQTT enregistré.'}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function dateValue(value){return String(value||'').slice(0,10)}function suggestedName(material,color){return [material.trim(),color.trim()].filter(Boolean).join(' ')}function autoNewSpoolName(){let name=$('newSpoolName');if(!name.dataset.custom)name.value=suggestedName($('newSpoolMaterial').value,$('newSpoolColor').value)}function autoCatalogSpoolName(id){let name=$('cn'+id);if(!name.dataset.custom&&(/^Bobine A[1-4]$/.test(name.value)||/^A\d{2}-[A-Z0-9-]+$/i.test(name.value)||!name.value))name.value=suggestedName($('cm'+id).value,$('cc'+id).value)}
function renderCatalog(inventory){let spools=inventory?.spools||[],occupants=Object.fromEntries(spools.filter(x=>x.slot).map(x=>[String(x.slot),x]));let label=(slot,x)=>{let other=occupants[String(slot)];return other&&other.id!==x.id?`A${slot} · échange avec ${other.name}`:`A${slot}${other?' · position actuelle':''}`};if($('newSpoolDate')&&!$('newSpoolDate').value)$('newSpoolDate').value=new Date().toISOString().slice(0,10);$('catalog').innerHTML=spools.length?spools.map(x=>`<tr data-spool="${x.id}" class="${selectedSpoolId===x.id?'selected':''}" onclick="selectSpool(${x.id})"><td class="id-cell"><button class="secondary" onclick="event.stopPropagation();selectSpool(${x.id})">#${x.id}</button></td><td><input onclick="event.stopPropagation()" oninput="this.dataset.custom='1'" id="cn${x.id}" value="${esc(x.name)}"></td><td><input onclick="event.stopPropagation()" oninput="autoCatalogSpoolName(${x.id})" id="cm${x.id}" value="${esc(x.material)}"></td><td><input onclick="event.stopPropagation()" id="cb${x.id}" value="${esc(x.brand)}"></td><td><input onclick="event.stopPropagation()" oninput="autoCatalogSpoolName(${x.id})" id="cc${x.id}" value="${esc(x.color)}"></td><td><input onclick="event.stopPropagation()" id="ci${x.id}" type="number" min="0" step="0.1" value="${x.initial_g}"></td><td><input onclick="event.stopPropagation()" id="cr${x.id}" type="number" min="0" step="0.1" value="${x.remaining_g}"></td><td><input onclick="event.stopPropagation()" id="cd${x.id}" type="date" value="${esc(dateValue(x.created_at))}"></td><td><select onclick="event.stopPropagation()" onchange="formDirty=true" id="catalogSlot${x.id}"><option value="" ${!x.slot?'selected':''}>Hors AMS</option>${[1,2,3,4].map(slot=>`<option value="${slot}" ${String(x.slot)===String(slot)?'selected':''}>${esc(label(slot,x))}</option>`).join('')}</select></td><td class="actions"><button onclick="saveCatalogSpool(${x.id},event)">Enregistrer</button><button class="secondary" onclick="event.stopPropagation();selectSpool(${x.id})">Historique</button><button class="secondary" onclick="deleteSpool(${x.id},event)">${pendingDeleteId===x.id?'Confirmer':'Supprimer'}</button></td></tr>`).join(''):'<tr><td colspan="10" class="muted">Aucune bobine dans le catalogue.</td></tr>'}
function timelineLabel(type){return({migration:'Catalogue initialisé',create:'Bobine ajoutée',rfid:'RFID lu',assign:'Placée dans l’AMS',remove:'Retirée de l’AMS',archive:'Supprimée du catalogue',deduct:'Impression comptabilisée'})[type]||type}
function timelineDate(value){let date=new Date(value);return Number.isNaN(date.getTime())?esc(value):date.toLocaleString('fr-FR',{dateStyle:'medium',timeStyle:'short'})}
function renderCatalogSummary(rows){let catalog=$('catalog');if(!catalog)return;let body=$('catalogSummary');if(!body){let section=document.createElement('section');section.className='catalog-summary';section.innerHTML='<h3>Synthèse des bobines</h3><div class="table-wrap"><table class="catalog-table summary-table"><thead><tr><th>Bobine</th><th>Emplacement</th><th>Poids restant</th><th>Niveau</th><th>Dernière utilisation</th><th>Impressions</th></tr></thead><tbody id="catalogSummary"></tbody></table></div>';catalog.closest('.table-wrap').before(section);body=$('catalogSummary')}body.innerHTML=(rows||[]).map(x=>{let pct=x.initial_g?Math.max(0,Math.min(100,100*x.remaining_g/x.initial_g)):0;return `<tr><td><b>${esc(x.name)}</b></td><td>${x.slot?'A'+esc(x.slot):'<span class="muted">Hors AMS</span>'}</td><td>${Number(x.remaining_g).toFixed(1)} g / ${Number(x.initial_g).toFixed(1)} g</td><td><b>${pct.toFixed(0)} %</b><div class="level-track"><div class="level-fill" style="width:${pct}%"></div></div></td><td>${x.last_used_at?timelineDate(x.last_used_at):'<span class="muted">Jamais</span>'}</td><td>${x.print_count||0}</td></tr>`}).join('')||'<tr><td colspan="6" class="muted">Aucune bobine.</td></tr>'}
function renderWeightChart(data){let timeline=$('timeline'),target=$('weightChart');if(!target&&timeline){target=document.createElement('div');target.id='weightChart';timeline.before(target)}if(!target)return;let spool=data.spool,events=data.events||[],points=[{when:spool.created_at,weight:Number(spool.initial_g),label:'Poids initial'}];for(const event of events.filter(e=>e.type==='deduct')){let match=String(event.detail||'').match(/→\s*([0-9.]+)\s*g/);if(match)points.push({when:event.created_at,weight:Number(match[1]),label:event.detail||'Impression comptabilisée'})}if(points.length===1){target.className='weight-chart';target.innerHTML='<h4>Évolution du poids</h4><p class="muted">La courbe apparaîtra après la première impression comptabilisée.</p>';return}let width=680,height=190,pad={l:42,r:16,t:18,b:30},max=Math.max(...points.map(p=>p.weight),Number(spool.initial_g),1),min=0,plotW=width-pad.l-pad.r,plotH=height-pad.t-pad.b,x=i=>pad.l+(points.length===1?0:i*plotW/(points.length-1)),y=v=>pad.t+(max-v)*plotH/(max-min),poly=points.map((p,i)=>`${x(i).toFixed(1)},${y(p.weight).toFixed(1)}`).join(' ');target.className='weight-chart';target.innerHTML=`<h4>Évolution du poids</h4><p class="muted">Survole un point pour voir la consommation.</p><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Évolution du poids de la bobine"><line class="axis" x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${height-pad.b}"/><line class="axis" x1="${pad.l}" y1="${height-pad.b}" x2="${width-pad.r}" y2="${height-pad.b}"/><text class="chart-label" x="2" y="${pad.t+4}">${max.toFixed(0)} g</text><text class="chart-label" x="18" y="${height-pad.b+4}">0 g</text><polyline class="line" points="${poly}"/>${points.map((p,i)=>`<circle class="point" cx="${x(i)}" cy="${y(p.weight)}" r="5"><title>${esc(timelineDate(p.when))} · ${p.weight.toFixed(2)} g${i?` · ${esc(p.label)}`:''}</title></circle>`).join('')}<text class="chart-label" x="${pad.l}" y="${height-7}">${esc(timelineDate(points[0].when))}</text><text class="chart-label" text-anchor="end" x="${width-pad.r}" y="${height-7}">${esc(timelineDate(points[points.length-1].when))}</text></svg>`}
const baseRenderCatalog=renderCatalog;renderCatalog=function(inventory){renderCatalogSummary((S&&S.inventory_summary)||[]);return baseRenderCatalog(inventory)};const baseRenderTimeline=renderTimeline;renderTimeline=function(data){baseRenderTimeline(data);renderWeightChart(data)};
function renderTimeline(data){let spool=data.spool,events=data.events||[];$('timelineTitle').textContent='Historique · '+spool.name;$('timelineSummary').textContent=`${spool.remaining_g} g restants sur ${spool.initial_g} g${spool.slot?` · actuellement en A${spool.slot}`:' · hors AMS'}`;$('timeline').className='timeline';$('timeline').innerHTML=events.length?events.map(event=>`<article class="timeline-event ${esc(event.type)}"><span class="timeline-dot"></span><div class="when">${timelineDate(event.created_at)}</div><div class="what">${esc(timelineLabel(event.type))}${event.slot?` · A${esc(event.slot)}`:''}</div><div class="detail">${esc(event.detail||'')}</div></article>`).join(''):'<div class="timeline-empty">Aucun événement pour cette bobine.</div>'}
async function selectSpool(id){pendingDeleteId=null;selectedSpoolId=id;document.querySelectorAll('#catalog tr[data-spool]').forEach(row=>row.classList.toggle('selected',Number(row.dataset.spool)===id));try{renderTimeline(await api('/api/inventory/spools/'+id+'/history'))}catch(e){msg(e.message,true)}}
function userIsSelectingText(){let active=document.activeElement;if(active&&(active.tagName==='INPUT'||active.tagName==='TEXTAREA')&&active.selectionStart!==active.selectionEnd)return true;let selection=window.getSelection();return !!(selection&&selection.rangeCount&&!selection.isCollapsed&&selection.toString().trim())}
async function refresh(){if(userIsSelectingText())return;try{render(await api('/api/state'))}catch(e){msg(e.message,true)}}const refreshTimer=setInterval(refresh,3000);
async function saveConfig(){try{await api('/api/config',{method:'POST',body:JSON.stringify({ip:$('ip').value,serial:$('serial').value,access_code:$('code').value})});formDirty=false;msg('Configuration enregistrée.');refresh()}catch(e){msg(e.message,true)}}
async function saveBridge(){let m={};for(let i=1;i<=4;i++)m[i]=$('bm'+i).value;try{await api('/api/bridge',{method:'POST',body:JSON.stringify({enabled:$('autoEnabled').checked,fallback_enabled:$('fallbackEnabled').checked,default_mapping:m})});formDirty=false;msg('Passerelle enregistrée.');refresh()}catch(e){msg(e.message,true)}}
async function saveSpools(){let x={};for(let i=1;i<=4;i++)if(S.spools[i]?.spool_id)x[i]={name:$('n'+i).value,initial_g:+$('i'+i).value,remaining_g:+$('r'+i).value};try{await api('/api/spools',{method:'POST',body:JSON.stringify(x)});formDirty=false;msg('Poids enregistrés.');refresh()}catch(e){msg(e.message,true)}}
async function decideGuardian(id,decision){try{await api('/api/guardian/proposals/'+id+'/decision',{method:'POST',body:JSON.stringify({decision})});msg('Décision enregistrée. Aucune commande n’a été envoyée à l’imprimante.');refresh()}catch(e){msg(e.message,true)}}
async function createSpool(){try{await api('/api/inventory/spools',{method:'POST',body:JSON.stringify({name:$('newSpoolName').value,material:$('newSpoolMaterial').value,brand:$('newSpoolBrand').value,color:$('newSpoolColor').value,initial_g:+$('newSpoolInitial').value,remaining_g:+$('newSpoolRemaining').value,created_at:$('newSpoolDate').value})});['newSpoolName','newSpoolMaterial','newSpoolBrand','newSpoolColor'].forEach(id=>$(id).value='');delete $('newSpoolName').dataset.custom;formDirty=false;catalogLoaded=false;msg('Bobine ajoutée au catalogue. Choisis maintenant sa voie AMS.');refresh()}catch(e){msg(e.message,true)}}
async function saveCatalogSpool(id,event){event?.stopPropagation();let slot=$('catalogSlot'+id).value;try{await api('/api/inventory/spools/'+id,{method:'POST',body:JSON.stringify({name:$('cn'+id).value,material:$('cm'+id).value,brand:$('cb'+id).value,color:$('cc'+id).value,initial_g:+$('ci'+id).value,remaining_g:+$('cr'+id).value,created_at:$('cd'+id).value})});let placement=await api('/api/inventory/assign',{method:'POST',body:JSON.stringify({spool_id:id,slot})});formDirty=false;catalogLoaded=false;msg(placement.message||'Bobine enregistrée.');refresh()}catch(e){msg(e.message,true)}}
async function deleteSpool(id,event){event?.stopPropagation();if(pendingDeleteId!==id){pendingDeleteId=id;event.currentTarget.textContent='Confirmer';msg('Clique encore sur Confirmer pour supprimer définitivement cette bobine et son historique.');return}try{let result=await api('/api/inventory/spools/'+id+'/delete',{method:'POST',body:'{}'});pendingDeleteId=null;if(selectedSpoolId===id){selectedSpoolId=null;$('timelineTitle').textContent='Historique de la bobine';$('timelineSummary').textContent='Clique une ligne du catalogue pour afficher sa frise chronologique.';$('timeline').className='timeline-empty';$('timeline').textContent='Aucune bobine sélectionnée.'}formDirty=false;catalogLoaded=false;msg(result.message||'Bobine supprimée.');refresh()}catch(e){msg(e.message,true)}}
async function shutdownCompanion(){if(!confirm('Arrêter AMS Lite Companion V2 ? Bambu Studio restera ouvert.'))return;try{await api('/api/shutdown',{method:'POST',body:'{}'});clearInterval(refreshTimer);document.body.innerHTML='<div class="wrap"><div class="card"><h1>Companion V2 arrêté</h1><p>Les niveaux et l’historique V2 sont enregistrés. Tu peux fermer cet onglet.</p></div></div>'}catch(e){msg(e.message,true)}}
async function confirmDetectedImport(){try{await api('/api/bridge/confirm',{method:'POST',body:'{}'});msg('Travail détecté confirmé. Lance l’impression dans Bambu Studio.');refresh()}catch(e){msg(e.message,true)}}
async function importFile(){let f=$('file').files[0];if(!f)return msg('Choisis un fichier .gcode.3mf.',true);try{imported=await api('/api/import?filename='+encodeURIComponent(f.name),{method:'POST',body:await f.arrayBuffer()});renderMappings();msg('Consommation extraite du fichier.')}catch(e){msg(e.message,true)}}
function renderMappings(){let plates=imported.plates;$('mapping').innerHTML=`<label>Plateau imprimé</label><select id="plate" onchange="renderMappings()">${plates.map(p=>`<option value="${p.id}" ${$('plate')&&$('plate').value==p.id?'selected':''}>Plateau ${p.id}</option>`).join('')}</select><div id="lines"></div><button onclick="arm()">Armer ce travail</button>`;let p=plates.find(x=>String(x.id)==$('plate').value)||plates[0];$('lines').innerHTML=p.filaments.map(f=>`<div class="line"><div><label>Filament ${esc(f.id)} ${esc(f.type)}</label><div>${f.used_g} g</div></div><div><label>Emplacement</label><select data-fid="${esc(f.id)}">${[1,2,3,4].map(i=>`<option value="${i}">A${i}</option>`).join('')}</select></div></div>`).join('')}
async function arm(){let mappings=[...$('lines').querySelectorAll('select')].map(x=>({filament_id:x.dataset.fid,slot:x.value}));try{await api('/api/arm',{method:'POST',body:JSON.stringify({plate:$('plate').value,mappings})});msg('Travail armé. Lance maintenant l’impression avec Bambu Studio officiel.');refresh()}catch(e){msg(e.message,true)}}refresh();
function markDirty(e){if(e.target.matches('#ip,#serial,#code,#spools input,#autoEnabled,#fallbackEnabled,#bridgeMap select,#catalog input,#catalog select'))formDirty=true}document.addEventListener('input',markDirty);document.addEventListener('change',markDirty);
/* v1.5 catalogue: the compact rows stay fast even with several hundred spools. */
function catalogueSummary(){return new Map((S?.inventory_summary||[]).map(row=>[Number(row.id),row]))}
function optionValues(spools,key){return [...new Set(spools.map(x=>String(x[key]||'').trim()).filter(Boolean))].sort((a,b)=>a.localeCompare(b,'fr'))}
function fillFilter(id,label,values,current){let el=$(id);if(!el)return;let wanted=current||'all';el.innerHTML=`<option value="all">${label}</option>`+values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');el.value=[...el.options].some(o=>o.value===wanted)?wanted:'all'}
function setCatalogFilter(key,value){catalogState[key]=value;catalogState.page=0;renderCatalog(S?.inventory)}
function catalogFiltered(){let spools=[...(S?.inventory?.spools||[])],summary=catalogueSummary(),query=String(catalogState.query||'').trim().toLocaleLowerCase('fr');spools=spools.filter(x=>{let usage=summary.get(Number(x.id))||{},hay=[x.name,x.material,x.brand,x.color,x.storage_location,x.rfid_tag].join(' ').toLocaleLowerCase('fr');if(query&&!hay.includes(query))return false;if(catalogState.material!=='all'&&x.material!==catalogState.material)return false;if(catalogState.brand!=='all'&&x.brand!==catalogState.brand)return false;if(catalogState.location!=='all'&&x.storage_location!==catalogState.location)return false;if(catalogState.status==='low'&&Number(x.remaining_g)>Number(x.low_stock_g))return false;if(catalogState.status==='ams'&&!x.slot)return false;if(catalogState.status==='unlocated'&&x.storage_location)return false;return true});let compare={name:(a,b)=>a.name.localeCompare(b.name,'fr'),remaining:(a,b)=>Number(a.remaining_g)-Number(b.remaining_g),recent:(a,b)=>String((summary.get(Number(b.id))||{}).last_used_at||'').localeCompare(String((summary.get(Number(a.id))||{}).last_used_at||'')),created:(a,b)=>String(b.created_at).localeCompare(String(a.created_at))}[catalogState.sort]||((a,b)=>a.name.localeCompare(b.name,'fr'));return {spools:spools.sort(compare),summary}}
function catalogStatus(spool){if(Number(spool.remaining_g)<=Number(spool.low_stock_g))return ['À commander','low'];if(spool.slot)return ['AMS A'+spool.slot,'ams'];if(!spool.storage_location)return ['À ranger',''];return ['Disponible','']}
function renderCatalogSummary(){let target=$('inventoryKpis'),overview=S?.inventory_overview||{};if(!target)return;let initial=Number(overview.initial_g||0),remaining=Number(overview.remaining_g||0),pct=initial?Math.round(100*remaining/initial):0;target.innerHTML=`<article class="kpi"><b>${Number(overview.count||0)}</b><span>bobines actives</span></article><article class="kpi"><b>${remaining.toFixed(0)} g</b><span>stock total · ${pct} %</span></article><article class="kpi alert"><b>${Number(overview.low_stock||0)}</b><span>à commander</span></article><article class="kpi"><b>${Number(overview.unlocated||0)}</b><span>sans emplacement</span></article><article class="kpi"><b>${Number(overview.locations?.length||0)}</b><span>emplacements utilisés</span></article>`}
function renderCatalog(inventory){let spools=inventory?.spools||[];renderCatalogSummary();fillFilter('catalogMaterial','Toutes les matières',optionValues(spools,'material'),catalogState.material);fillFilter('catalogBrand','Toutes les marques',optionValues(spools,'brand'),catalogState.brand);fillFilter('catalogLocation','Tous les emplacements',optionValues(spools,'storage_location'),catalogState.location);if($('catalogSearch')&&$('catalogSearch').value!==catalogState.query)$('catalogSearch').value=catalogState.query;let result=catalogFiltered(),rows=result.spools,summary=result.summary,totalPages=Math.max(1,Math.ceil(rows.length/catalogState.pageSize));catalogState.page=Math.min(catalogState.page,totalPages-1);let start=catalogState.page*catalogState.pageSize,page=rows.slice(start,start+catalogState.pageSize);catalogState.selected=new Set([...catalogState.selected].filter(id=>rows.some(x=>Number(x.id)===Number(id))));$('catalog').innerHTML=page.length?page.map(x=>{let info=summary.get(Number(x.id))||{},pct=x.initial_g?Math.max(0,Math.min(100,100*Number(x.remaining_g)/Number(x.initial_g))):0,status=catalogStatus(x),last=info.last_used_at?timelineDate(info.last_used_at):'Jamais';return `<tr data-spool="${x.id}" class="catalog-row ${selectedSpoolId===x.id?'selected':''}" onclick="selectSpool(${x.id})"><td><input type="checkbox" onclick="event.stopPropagation()" onchange="toggleCatalogSelection(${x.id},this.checked)" ${catalogState.selected.has(x.id)?'checked':''}></td><td><b>${esc(x.name)}</b><br><span class="muted">#${x.id}</span></td><td>${esc(x.material||'—')}<br><span class="muted">${esc([x.brand,x.color].filter(Boolean).join(' · ')||'')}</span></td><td>${x.slot?`<b>A${x.slot}</b> · `:''}${esc(x.storage_location||'À définir')}</td><td class="stock-cell"><div class="stock-line"><b class="${status[1]==='low'?'stock-low':''}">${Number(x.remaining_g).toFixed(0)} g</b><div class="level-track"><div class="level-fill" style="width:${pct}%"></div></div><span>${pct.toFixed(0)} %</span></div><span class="status-chip ${status[1]}">${status[0]} · seuil ${Number(x.low_stock_g).toFixed(0)} g</span></td><td>${last}</td><td>${Number(info.print_count||0)}</td><td><button class="secondary no-top" onclick="event.stopPropagation();selectSpool(${x.id})">Fiche</button></td></tr>`}).join(''):`<tr><td colspan="8" class="muted">Aucune bobine ne correspond aux filtres.</td></tr>`;let count=$('selectionCount');if(count)count.textContent=catalogState.selected.size?`${catalogState.selected.size} bobine(s) sélectionnée(s)`:'Aucune sélection';let all=$('selectAllCatalog');if(all)all.checked=page.length>0&&page.every(x=>catalogState.selected.has(x.id));$('catalogPager').innerHTML=`<span class="muted">${rows.length} résultat(s) · ${start+1}-${Math.min(start+page.length,rows.length)} sur ${rows.length}</span><span><button class="secondary no-top" onclick="changeCatalogPage(-1)" ${catalogState.page?'':'disabled'}>Précédent</button> <button class="secondary no-top" onclick="changeCatalogPage(1)" ${catalogState.page<totalPages-1?'':'disabled'}>Suivant</button></span>`;renderSpoolDetail()}
function changeCatalogPage(delta){catalogState.page=Math.max(0,catalogState.page+delta);renderCatalog(S?.inventory)}
function toggleCatalogSelection(id,checked){if(checked)catalogState.selected.add(id);else catalogState.selected.delete(id);renderCatalog(S?.inventory)}
function togglePageSelection(checked){let result=catalogFiltered().spools,start=catalogState.page*catalogState.pageSize;for(let spool of result.slice(start,start+catalogState.pageSize)){if(checked)catalogState.selected.add(spool.id);else catalogState.selected.delete(spool.id)}renderCatalog(S?.inventory)}
function detailSlots(spool){return `<option value="">Hors AMS</option>`+[1,2,3,4].map(slot=>`<option value="${slot}" ${String(spool.slot)===String(slot)?'selected':''}>AMS A${slot}</option>`).join('')}
function renderSpoolDetail(){let target=$('spoolDetail');if(!target)return;let spool=(S?.inventory?.spools||[]).find(x=>Number(x.id)===Number(selectedSpoolId));if(!spool){target.innerHTML='<h3>Fiche de bobine</h3><p class="muted">Sélectionne une bobine dans la liste pour consulter ou modifier sa fiche.</p>';return}target.innerHTML=`<div class="catalog-toolbar"><div><h3>Fiche · ${esc(spool.name)}</h3><p class="muted">Les modifications et déplacements sont enregistrés dans son historique.</p></div><span class="status-chip ${catalogStatus(spool)[1]}">${catalogStatus(spool)[0]}</span></div><div class="detail-grid"><div><label>Nom</label><input id="detailName" value="${esc(spool.name)}"></div><div><label>Matière</label><input id="detailMaterial" value="${esc(spool.material)}"></div><div><label>Marque</label><input id="detailBrand" value="${esc(spool.brand)}"></div><div><label>Couleur</label><input id="detailColor" value="${esc(spool.color)}"></div><div><label>Poids initial (g)</label><input id="detailInitial" type="number" min="0" step="0.1" value="${spool.initial_g}"></div><div><label>Poids restant (g)</label><input id="detailRemaining" type="number" min="0" step="0.1" value="${spool.remaining_g}"></div><div><label>Seuil d’alerte (g)</label><input id="detailThreshold" type="number" min="0" step="1" value="${spool.low_stock_g}"></div><div><label>Dans l’AMS</label><select id="detailSlot">${detailSlots(spool)}</select></div><div class="detail-wide"><label>Emplacement physique</label><input id="detailLocation" value="${esc(spool.storage_location)}" placeholder="Étagère B-03"></div><div><label>Coût (€)</label><input id="detailCost" type="number" min="0" step="0.01" value="${spool.cost_eur}"></div><div><label>Date d’ajout</label><input id="detailDate" type="date" value="${esc(dateValue(spool.created_at))}"></div><div class="detail-wide"><label>Notes</label><input id="detailNotes" value="${esc(spool.notes)}" placeholder="Fournisseur, finition, référence…"></div></div><div class="detail-actions"><button onclick="saveSelectedSpool()">Enregistrer la fiche</button><button class="secondary" onclick="archiveSelectedSpool()">Archiver</button><button class="danger" onclick="deleteSelectedSpool()">Supprimer définitivement</button></div><section class="spool-timeline"><h3 id="timelineTitle">Historique · ${esc(spool.name)}</h3><p id="timelineSummary" class="muted">Chargement de l’historique…</p><div id="timeline" class="timeline-empty">Chargement…</div></section>`}
selectSpool=async function(id){selectedSpoolId=id;renderCatalog(S?.inventory);try{let data=await api('/api/inventory/spools/'+id+'/history');renderTimeline(data);renderWeightChart(data)}catch(e){msg(e.message,true)}};
async function saveSelectedSpool(){if(!selectedSpoolId)return;try{await api('/api/inventory/spools/'+selectedSpoolId,{method:'POST',body:JSON.stringify({name:$('detailName').value,material:$('detailMaterial').value,brand:$('detailBrand').value,color:$('detailColor').value,initial_g:+$('detailInitial').value,remaining_g:+$('detailRemaining').value,low_stock_g:+$('detailThreshold').value,storage_location:$('detailLocation').value,cost_eur:+$('detailCost').value,notes:$('detailNotes').value,created_at:$('detailDate').value})});await api('/api/inventory/assign',{method:'POST',body:JSON.stringify({spool_id:selectedSpoolId,slot:$('detailSlot').value})});catalogLoaded=false;msg('Fiche enregistrée.');refresh()}catch(e){msg(e.message,true)}}
async function archiveSelectedSpool(){if(!selectedSpoolId||!confirm('Archiver cette bobine ? Son historique sera conservé.'))return;let id=selectedSpoolId;try{let result=await api('/api/inventory/spools/'+id+'/archive',{method:'POST',body:'{}'});catalogState.selected.delete(id);selectedSpoolId=null;catalogLoaded=false;msg(result.message);refresh()}catch(e){msg(e.message,true)}}
async function deleteSelectedSpool(){if(!selectedSpoolId||!confirm('Supprimer définitivement cette bobine et tout son historique ?'))return;try{let result=await api('/api/inventory/spools/'+selectedSpoolId+'/delete',{method:'POST',body:'{}'});catalogState.selected.delete(selectedSpoolId);selectedSpoolId=null;catalogLoaded=false;msg(result.message);refresh()}catch(e){msg(e.message,true)}}
async function runBulk(action){let ids=[...catalogState.selected];if(!ids.length)return msg('Sélectionne au moins une bobine.',true);if(action==='archive'&&!confirm(`Archiver ${ids.length} bobine(s) ? Leur historique sera conservé.`))return;let body={ids,action};if(action==='location')body.storage_location=$('bulkLocation').value;if(action==='threshold')body.low_stock_g=+$('bulkThreshold').value;try{let result=await api('/api/inventory/bulk',{method:'POST',body:JSON.stringify(body)});if(action==='archive'){catalogState.selected.clear();selectedSpoolId=null}catalogLoaded=false;msg(result.message);refresh()}catch(e){msg(e.message,true)}}
async function exportCatalog(){try{let response=await fetch('/api/inventory/export.csv',{headers:{'X-AMS-Token':apiToken},credentials:'same-origin'});if(!response.ok)throw Error('Export impossible');let blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='ams-lite-catalogue.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);msg('Export CSV téléchargé.')}catch(e){msg(e.message,true)}}
async function exportSupervisionReport(){try{let response=await fetch('/api/report.json',{headers:{'X-AMS-Token':apiToken},credentials:'same-origin'});if(!response.ok)throw Error('Rapport impossible');let blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='ams-lite-rapport-supervision.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);msg('Rapport de supervision téléchargé.')}catch(e){msg(e.message,true)}}
createSpool=async function(){try{await api('/api/inventory/spools',{method:'POST',body:JSON.stringify({name:$('newSpoolName').value,material:$('newSpoolMaterial').value,brand:$('newSpoolBrand').value,color:$('newSpoolColor').value,initial_g:+$('newSpoolInitial').value,remaining_g:+$('newSpoolRemaining').value,storage_location:$('newSpoolLocation').value,low_stock_g:+$('newSpoolThreshold').value,created_at:$('newSpoolDate').value})});['newSpoolName','newSpoolMaterial','newSpoolBrand','newSpoolColor','newSpoolLocation'].forEach(id=>$(id).value='');delete $('newSpoolName').dataset.custom;catalogLoaded=false;msg('Bobine ajoutée au catalogue.');refresh()}catch(e){msg(e.message,true)}};
const visionView=new URLSearchParams(location.search).get('vision')==='1';
function openVision(){if(window.webkit?.messageHandlers?.companion)window.webkit.messageHandlers.companion.postMessage('openVision');else window.open('/?vision=1','ams-lite-vision')}
function renderVision(s){let camera=s.camera||{},captures=camera.captures||[];$('visionStatus').textContent=camera.status||'Caméra non configurée';$('visionEnabled').checked=!!camera.enabled;$('visionFingerprint').value=camera.certificate_sha256||'';$('visionMeta').textContent=`Cadence : une image toutes les ${camera.capture_every_layers||5} couches · dernière couche vue : ${camera.last_seen_layer||'—'}`;$('visionGallery').innerHTML=captures.length?captures.map(x=>`<article><b>Couche ${x.layer}</b><br><span>${esc(x.captured_at)}</span><br><code>${esc(x.file)}</code></article>`).join(''):'<p>Aucune capture enregistrée.</p>'}
async function saveVision(){try{await api('/api/config',{method:'POST',body:JSON.stringify({camera_enabled:$('visionEnabled').checked,camera_certificate_sha256:$('visionFingerprint').value})});$('visionStatus').textContent='Configuration Vision enregistrée.'}catch(e){$('visionStatus').textContent=e.message}}
async function discoverVision(){try{let result=await api('/api/camera/discover',{method:'POST',body:'{}'});$('visionFingerprint').value=result.fingerprint;$('visionStatus').textContent='Empreinte détectée. Vérifie-la puis enregistre la configuration.'}catch(e){$('visionStatus').textContent=e.message}}
if(visionView){let button=document.createElement('button');button.className='secondary';button.textContent='Détecter la caméra';button.onclick=discoverVision;$('visionFingerprint').before(button)}
if(visionView){clearInterval(refreshTimer);document.body.innerHTML='<main class="wrap"><h1>Centre Vision</h1><p class="sub">Surveillance locale : aucune commande n’est envoyée à l’imprimante.</p><section class="card"><div id="visionStatus" class="notice">Chargement…</div><label class="check"><input id="visionEnabled" type="checkbox">Activer les captures automatiques</label><label>Empreinte TLS de la caméra</label><input id="visionFingerprint" placeholder="64 caractères hexadécimaux"><button onclick="saveVision()">Enregistrer la configuration</button><p id="visionMeta" class="muted"></p></section><section class="card wide"><h2>Captures</h2><div id="visionGallery" class="vision-gallery"></div></section></main>';let style=document.createElement('style');style.textContent='.vision-gallery{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.vision-gallery article{border:1px solid #dfe3e7;padding:12px;border-radius:8px}.vision-gallery code{font-size:11px;overflow-wrap:anywhere}';document.head.append(style);api('/api/state').then(renderVision).catch(e=>{$('visionStatus').textContent=e.message})}else{let dashboardRender=render;render=function(s){dashboardRender(s);let host=document.querySelector(".spools-card");if(host&&!$('openVisionButton')){let button=document.createElement('button');button.id='openVisionButton';button.className='secondary';button.textContent='Ouvrir le centre Vision…';button.onclick=openVision;host.append(button)}}}
if(!visionView){let removeLegacyVisionButton=render;render=function(s){removeLegacyVisionButton(s);$('openVisionButton')?.remove()}}
</script></body></html>'''


def render_html(api_token: str) -> str:
    return HTML.replace("__API_TOKEN__", api_token)


def render_vision_html(api_token: str) -> str:
    """Standalone Vision window; deliberately independent from the compact panel."""
    return f'''<!doctype html><meta charset="utf-8"><title>Centre Vision</title>
<style>body{{font-family:-apple-system,sans-serif;background:#f4f5f6;color:#20242a;margin:0}}main{{max-width:1100px;margin:auto;padding:28px}}section{{background:#fff;border:1px solid #dfe3e7;border-radius:10px;padding:18px;margin-top:16px}}input{{width:100%;box-sizing:border-box;padding:9px;margin:6px 0 12px}}button{{padding:9px 13px;background:#00a23d;border:0;border-radius:7px;color:#fff;font-weight:600}}.secondary{{background:#59636e}}#gallery{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}}article{{border:1px solid #dfe3e7;border-radius:8px;padding:12px}}.muted{{color:#69717b}}</style>
<style>#gallery img{{display:block;width:100%;margin-top:10px;border-radius:6px;background:#e9edf0}}#gallery article{{overflow:hidden}}.capture-group{{margin:18px 0 28px;padding-top:4px;border-top:1px solid #dfe3e7}}.capture-group:first-child{{border-top:0;margin-top:0}}.capture-group-head{{display:flex;gap:12px;align-items:center;justify-content:space-between;margin:8px 0 10px}}.capture-group-head h3{{margin:0;font-size:16px}}.capture-group-head button{{margin:0;background:#b5392e}}.capture-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}.capture-card{{display:block;width:100%;padding:0;background:#fff;color:#20242a;text-align:left;border:0;margin:0;cursor:pointer}}.capture-card:hover img{{outline:3px solid #00a23d}}.capture-modal{{position:fixed;inset:0;z-index:10;background:#000b;display:grid;place-items:center;padding:24px;box-sizing:border-box}}.capture-modal[hidden]{{display:none}}.capture-dialog{{position:relative;box-sizing:border-box;width:min(1080px,calc(100vw - 32px));max-height:calc(100vh - 32px);overflow:auto;background:#fff;border-radius:12px;padding:20px;box-shadow:0 12px 50px #0008}}.capture-dialog img{{display:block;max-width:100%;max-height:68vh;margin:12px auto 0;border-radius:8px;background:#e9edf0}}.capture-close{{position:absolute;right:14px;top:8px;margin:0;background:#59636e}}.capture-info{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:14px}}.capture-info div{{background:#f5f7f8;border-radius:7px;padding:9px;overflow-wrap:anywhere}}.capture-info b{{display:block;font-size:12px;color:#66707a;margin-bottom:3px}}</style><main><h1>Centre Vision</h1><p class="muted">Surveillance locale en lecture seule.</p><section><h2>Connexion Bambu</h2><p>Adresse de l’imprimante : <b>192.168.1.24</b></p><button class="secondary" onclick="importBambuStudio()">Importer la configuration de Bambu Studio</button><p class="muted">Le code LAN reste uniquement sur ce Mac.</p></section><section><h2>Caméra</h2><p id="status">Chargement…</p><label><input id="enabled" type="checkbox"> Activer les captures toutes les 5 couches</label><label>Empreinte TLS</label><input id="fingerprint" placeholder="Détecte-la automatiquement"><button class="secondary" onclick="discover()">Détecter la caméra</button> <button onclick="save()">Approuver et enregistrer</button><p id="meta" class="muted"></p></section><section><h2>Captures par impression</h2><div id="gallery"></div></section></main><div id="captureModal" class="capture-modal" hidden onclick="if(event.target===this)closeCapture()"><section class="capture-dialog"><button class="capture-close" onclick="closeCapture()">Fermer</button><h2 id="captureTitle">Capture</h2><img id="captureLarge" alt="Capture agrandie"><div id="captureInfo" class="capture-info"></div></section></div>
<script>const visionStatus=document.getElementById('status'),visionEnabled=document.getElementById('enabled'),visionFingerprint=document.getElementById('fingerprint'),visionMeta=document.getElementById('meta'),visionGallery=document.getElementById('gallery'),captureModal=document.getElementById('captureModal'),captureTitle=document.getElementById('captureTitle'),captureLarge=document.getElementById('captureLarge'),captureInfo=document.getElementById('captureInfo');</script>
<script>const token={api_token!r};async function api(p,o={{}}){{let r=await fetch(p,{{...o,headers:{{'X-AMS-Token':token,'Content-Type':'application/json'}}}}),j=await r.json();if(!r.ok)throw Error(j.error||'Erreur');return j}}function captureURL(file){{return `/api/captures/${{encodeURIComponent(file)}}?token=${{encodeURIComponent(token)}}`}}function esc(v){{return String(v??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}function show(s){{let c=s.camera||{{}},x=Array.isArray(c.captures)?c.captures:[],storage=s.vision_storage||{{}},size=(Number(storage.bytes||0)/1048576).toFixed(1);visionStatus.textContent=c.status||'Caméra non configurée';visionEnabled.checked=!!c.enabled;visionFingerprint.value=c.certificate_sha256||'';visionMeta.textContent=`Cadence : 5 couches · dernière couche : ${{c.last_seen_layer||'—'}} · ${{storage.count||0}} image(s) · ${{size}} Mo`;window.visionCaptures=x;let groups=new Map();x.forEach((i,index)=>{{let key=i.folder||'en-cours';if(!groups.has(key))groups.set(key,{{name:i.print_name||(i.folder?'Impression terminée':'Impression en cours'),folder:i.folder||'',id:i.print_id||'',items:[]}});groups.get(key).items.push({{i,index}})}});visionGallery.innerHTML=x.length?[...groups.values()].map(g=>`<div class="capture-group"><div class="capture-group-head"><div><h3>${{esc(g.name)}}</h3><span>${{g.items.length}} image(s)</span></div>${{g.folder?`<button onclick="deletePrint('${{esc(g.folder)}}')">Supprimer</button>`:g.id?`<button onclick="deleteCurrentPrint('${{esc(g.id)}}')">Supprimer</button>`:''}}</div><div class="capture-grid">${{g.items.map(({{i,index}})=>`<article><button class="capture-card" onclick="openCapture(${{index}})"><b>Couche ${{i.layer}}</b><br>${{i.captured_at}}<img src="${{captureURL(i.file)}}" alt="Capture de la couche ${{i.layer}}"></button></article>`).join('')}}</div></div>`).join(''):'Aucune capture.'}}function openCapture(index){{let i=(window.visionCaptures||[])[index];if(!i)return;captureTitle.textContent=`Capture · couche ${{i.layer}}`;captureLarge.src=captureURL(i.file);captureLarge.alt=`Capture agrandie de la couche ${{i.layer}}`;captureInfo.innerHTML=`<div><b>Impression</b>${{esc(i.print_name||'En cours')}}</div><div><b>Couche</b>${{i.layer}}</div><div><b>Date</b>${{i.captured_at}}</div><div><b>Fichier</b>${{i.file}}</div><div><b>Empreinte SHA-256</b>${{i.sha256||'—'}}</div>`;captureModal.hidden=false}}function closeCapture(){{captureModal.hidden=true;captureLarge.removeAttribute('src')}}async function deletePrint(folder){{if(!confirm('Supprimer définitivement toutes les captures de cette impression ?'))return;try{{await api(`/api/captures/${{encodeURIComponent(folder)}}/delete`,{{method:'POST',body:'{{}}'}});closeCapture();await load()}}catch(e){{visionStatus.textContent=e.message}}}}async function deleteCurrentPrint(id){{if(!confirm('Supprimer les captures déjà prises pour cette impression ? Les prochaines captures continueront normalement.'))return;try{{await api(`/api/captures/session/${{encodeURIComponent(id)}}/delete`,{{method:'POST',body:'{{}}'}});closeCapture();await load()}}catch(e){{visionStatus.textContent=e.message}}}}async function load(){{try{{show(await api('/api/state'))}}catch(e){{visionStatus.textContent=e.message}}}}async function importBambuStudio(){{try{{let r=await api('/api/config/import-bambu-studio',{{method:'POST',body:JSON.stringify({{ip:'192.168.1.24'}})}});visionStatus.textContent=`Configuration importée pour ${{r.serial}}. Active ensuite le mode LAN de l’imprimante.`;load()}}catch(e){{visionStatus.textContent=e.message}}}}async function discover(){{try{{visionFingerprint.value=(await api('/api/camera/discover',{{method:'POST',body:'{{}}'}})).fingerprint;visionStatus.textContent='Empreinte détectée : approuve-la.'}}catch(e){{visionStatus.textContent=e.message}}}}async function save(){{try{{await api('/api/config',{{method:'POST',body:JSON.stringify({{camera_enabled:visionEnabled.checked,camera_certificate_sha256:visionFingerprint.value}})}});load()}}catch(e){{visionStatus.textContent=e.message}}}}document.addEventListener('keydown',e=>{{if(e.key==='Escape')closeCapture()}});load();setInterval(load,3000);</script>'''


def run_server(open_browser: bool = True, state_path: Path = STATE_FILE, api_token: str | None = None) -> None:
    app = Companion(state_path)
    app.mqtt.start()
    app.bridge.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.app = app  # type: ignore[attr-defined]
    server.api_token = api_token or secrets.token_urlsafe(32)  # type: ignore[attr-defined]
    log(f"Interface disponible sur http://{HOST}:{PORT}")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.bridge.stop()
        app.mqtt.stop()
        app.bridge.join(timeout=2)
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Compteur local AMS Lite")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--api-token", help="jeton d’API locale fourni par l’application macOS")
    parser.add_argument("--parse", metavar="FICHIER", help="analyse un .gcode.3mf puis quitte")
    args = parser.parse_args()
    if args.parse:
        path = Path(args.parse)
        print(json.dumps(parse_3mf(path.read_bytes(), path.name), ensure_ascii=False, indent=2))
        return
    run_server(not args.no_browser, api_token=args.api_token)


if __name__ == "__main__":
    main()
