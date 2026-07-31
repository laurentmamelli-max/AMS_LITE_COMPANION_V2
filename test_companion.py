import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Keep test fixtures and their expected failures out of the user's persistent
# Companion journal.  ``ams_companion`` reads this before it is imported.
_TEST_LOG_DIR = tempfile.TemporaryDirectory(prefix="ams-companion-tests-")
os.environ["AMS_COMPANION_LOG_FILE"] = str(Path(_TEST_LOG_DIR.name) / "companion.log")

import ams_companion as ac


def sample_3mf(*weights):
    filaments = "".join(
        f'<filament id="{i+1}" type="PLA" color="#ffffff" used_g="{w}" />'
        for i, w in enumerate(weights)
    )
    xml = f'<config><plate id="1">{filaments}</plate></config>'.encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Metadata/slice_info.config", xml)
    return out.getvalue()


def sample_mapped_3mf():
    xml = b'''<config><plate id="1"><filament id="1" type="PLA" color="#ffffff" used_g="8" /></plate>
    <object identify_id="944" name="Piece test.stl" skipped="false" />
    <object identify_id="955" name="Deja ignore.stl" skipped="true" /></config>'''
    gcode = b'''; start printing object, unique label id: 944
G1 X10 Y20
; stop printing object, unique label id: 944
; start printing object, unique label id: 944
G1 X12 Y25
; stop printing object, unique label id: 944
'''
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Metadata/slice_info.config", xml)
        archive.writestr("Metadata/plate_1.gcode", gcode)
    return out.getvalue()


def sample_bambu_indexed_3mf():
    xml = b'''<config><plate><metadata key="index" value="3" />
    <filament id="1" type="PLA" color="#ffffff" used_g="8" />
    <object identify_id="944" name="Piece test.stl" skipped="false" /></plate></config>'''
    gcode = b'''; start printing object, unique label id: 944
G1 X10 Y20
; stop printing object, unique label id: 944
'''
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Metadata/slice_info.config", xml)
        archive.writestr("Metadata/plate_3.gcode", gcode)
    return out.getvalue()


class ManualMQTTStub:
    """Test double: records the explicit request without opening a socket."""

    def __init__(self):
        self.sent: list[dict] = []

    def publish_manual_skip(self, payload, *, timeout_seconds=7.0):
        self.sent.append(payload)
        return {"status": "published", "published_at": "2026-07-28T00:00:00+0200", "message": "Publié (test)"}


class CompanionTests(unittest.TestCase):
    def test_imports_lan_identity_from_bambu_studio_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "BambuStudio.conf"
            config_path.write_text(json.dumps({
                "app": {"user_last_selected_machine": "0300DA612300365"},
                "user_access_code": {"0300DA612300365": "12345678"},
            }), encoding="utf-8")
            credentials = ac.read_bambu_studio_credentials(config_path)
            self.assertEqual("0300DA612300365", credentials["serial"])
            self.assertEqual("12345678", credentials["access_code"])
            app = ac.Companion(Path(tmp) / "state.json")
            original = ac.read_bambu_studio_credentials
            try:
                ac.read_bambu_studio_credentials = lambda: credentials
                result = app.import_bambu_studio_configuration({"ip": "192.168.1.24"})
            finally:
                ac.read_bambu_studio_credentials = original
            self.assertTrue(result["access_code_imported"])
            self.assertEqual("192.168.1.24", app.state["config"]["ip"])
            self.assertEqual("12345678", app.state["config"]["access_code"])

    def test_parse_per_filament(self):
        parsed = ac.parse_3mf(sample_3mf(18.2, 3.5), "test.gcode.3mf")
        self.assertEqual([18.2, 3.5], [x["used_g"] for x in parsed["plates"][0]["filaments"]])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.gcode.3mf"
            path.write_bytes(sample_3mf(18.2, 3.5))
            streamed = ac.parse_3mf_path(path)
            self.assertEqual(parsed["plates"], streamed["plates"])
            self.assertEqual(parsed["sha256"], streamed["sha256"])

    def test_uses_slice_info_identity_for_a_mapped_bambu_object(self):
        parsed = ac.parse_3mf(sample_mapped_3mf(), "mapped.gcode.3mf")
        objects = {item["id"]: item for item in parsed["object_map"]["objects"]}
        self.assertEqual(944, objects["944"]["protocol_object_id"])
        self.assertEqual("slice_info.config", objects["944"]["protocol_identity"])
        self.assertIn("Piece test.stl", objects["944"]["label"])
        self.assertTrue(objects["955"]["protocol_skipped"])

    def test_manual_arm_keeps_the_selected_plate_object_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            parsed = ac.parse_3mf(sample_mapped_3mf(), "mapped.gcode.3mf")
            expected_map = parsed["plates"][0]["object_map"]
            # Bambu's temporary project archive can have a per-plate map but
            # no global G-code map.  Manual arming must not lose the former.
            parsed["object_map"] = {"status": "unavailable", "objects": []}
            app.last_import = parsed
            armed = app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "1"}]})
            self.assertEqual(expected_map, armed["object_map"])

    def test_bambu_plate_metadata_index_selects_its_matching_gcode_map(self):
        parsed = ac.parse_3mf(sample_bambu_indexed_3mf(), "bambu.3mf")
        self.assertEqual("3", parsed["plates"][0]["id"])
        self.assertEqual("mapped", parsed["plates"][0]["object_map"]["status"])
        self.assertEqual(["944"], [item["id"] for item in parsed["plates"][0]["object_map"]["objects"]])

    def test_sends_a_manual_guardian_exclusion_only_after_explicit_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.state["active_job"] = {
                "token": "test-job", "object_map": ac.parse_3mf(sample_mapped_3mf())["object_map"],
            }
            for index in range(3):
                result = app.observe_plate_guardian({
                    "object_id": "944", "object_label": "Piece test", "confidence": 0.95,
                    "source": "test", "frame_sha256": f"{index:064x}",
                })
            state = app.public_state()
            self.assertEqual("notify_only", state["autopilot"]["alerts"][0]["action"])
            prepared = app.prepare_manual_exclusion(result["proposal"]["id"])
            self.assertEqual([944], prepared["instruction"]["print"]["obj_list"])
            self.assertEqual("prepared_manually", prepared["status"])
            self.assertEqual(1, len(app.public_state()["autopilot"]["prepared"]))
            transport = ManualMQTTStub()
            app.mqtt = transport
            app.state["printer"].update({"connected": True, "state": "RUNNING"})
            executed = app.execute_manual_exclusion(result["proposal"]["id"])
            self.assertEqual("published", executed["transport"]["status"])
            self.assertEqual([944], transport.sent[0]["print"]["obj_list"])
            self.assertEqual("published", app.public_state()["autopilot"]["dispatches"][0]["status"])
            with self.assertRaisesRegex(ValueError, "déjà"):
                app.execute_manual_exclusion(result["proposal"]["id"])
            self.assertEqual(1, len(transport.sent))

    def test_manual_mqtt_packet_is_single_object_and_cannot_survive_a_disconnect(self):
        class SocketRecorder:
            def __init__(self):
                self.packets = []

            def sendall(self, packet):
                self.packets.append(packet)

        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            mqtt = app.mqtt
            done = threading.Event()
            request = {
                "payload": {"print": {"sequence_id": "0", "command": "skip_objects", "obj_list": [944]}},
                "done": done, "created_at": time.monotonic(), "cancelled": False,
            }
            mqtt.manual_commands.put(request)
            socket_recorder = SocketRecorder()
            mqtt._drain_manual_commands(socket_recorder, "device/test/request")
            self.assertTrue(done.is_set())
            self.assertEqual("published", request["result"]["status"])
            self.assertEqual(1, len(socket_recorder.packets))
            self.assertEqual(0x30, socket_recorder.packets[0][0])
            self.assertIn(b'"command":"skip_objects"', socket_recorder.packets[0])

            pending = {"done": threading.Event(), "cancelled": False}
            mqtt.manual_commands.put(pending)
            mqtt._cancel_pending_manual_commands("connexion perdue")
            self.assertTrue(pending["done"].is_set())
            self.assertTrue(pending["cancelled"])
            self.assertEqual("connexion perdue", pending["error"])

    def test_finish_deducts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.last_import = ac.parse_3mf(sample_3mf(43), "job.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "1"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "42"}})
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "42"}})
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "42"}})
            self.assertEqual(957, app.state["spools"]["1"]["remaining_g"])
            self.assertEqual(1, len(app.state["accounted"]))

    def test_guardian_evidence_is_isolated_and_never_controls_the_printer(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            for index in range(3):
                digest = __import__("hashlib").sha256(f"frame-{index}".encode()).hexdigest()
                result = app.observe_plate_guardian({
                    "object_id": "part-1", "object_label": "Pièce test",
                    "confidence": 0.95, "source": "simulation", "frame_sha256": digest,
                })
            self.assertTrue(result["accepted"])
            self.assertEqual("pending_confirmation", result["proposal"]["status"])
            self.assertEqual("unsupported", app.public_state()["guardian"]["capability"]["status"])
            self.assertEqual("INCONNU", app.state["printer"]["state"])

    def test_camera_requests_one_capture_every_five_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.last_import = ac.parse_3mf(sample_3mf(1), "camera.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "1"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "cam", "layer_num": 4}})
            self.assertNotIn("pending_capture_layer", app.state["camera"])
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "cam", "layer_num": 5}})
            self.assertEqual(5, app.state["camera"]["pending_capture_layer"])
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "cam", "layer_num": 5}})
            self.assertEqual(5, app.state["camera"]["last_requested_layer"])

    def test_camera_observes_a_running_job_not_armed_by_the_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "external", "layer_num": 10}})
            self.assertEqual(10, app.state["camera"]["last_seen_layer"])
            self.assertEqual(10, app.state["camera"]["pending_capture_layer"])

    def test_camera_resets_its_layer_cursor_when_a_new_print_session_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            with app.lock:
                app._ensure_camera_print_session_locked({"subtask_name": "ancien.3mf"}, "old-task")
                app.state["camera"].update({
                    "last_seen_layer": 195, "last_requested_layer": 195,
                    "pending_capture_layer": 195, "pending_capture_session_id": "old-session",
                })
                app._ensure_camera_print_session_locked({"subtask_name": "nouveau.3mf"}, "new-task")
                self.assertEqual(0, app.state["camera"]["last_seen_layer"])
                self.assertEqual(0, app.state["camera"]["last_requested_layer"])
                app._schedule_camera_capture_locked({"layer_num": 5}, "RUNNING")
            self.assertEqual(5, app.state["camera"]["pending_capture_layer"])
            self.assertEqual(5, app.state["camera"]["last_requested_layer"])

    def test_camera_configuration_reports_active_surveillance(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.configure({
                "camera_enabled": True,
                "camera_certificate_sha256": "a" * 64,
            })
            self.assertIn("Surveillance active", app.state["camera"]["status"])

    def test_centre_vision_renders_capture_images(self):
        page = ac.render_vision_html("test-token")
        self.assertIn('function captureURL(file)', page)
        self.assertIn('/api/captures/${encodeURIComponent(file)}?token=${encodeURIComponent(token)}', page)
        self.assertIn('.capture-card img', page)
        self.assertIn('function openCapture(index)', page)
        self.assertIn('id="captureModal"', page)
        self.assertIn('ce n’est pas une vidéo en direct', page)
        self.assertIn('Calibrer le plateau sur une capture', page)
        self.assertIn('contour(s) rouges projetés depuis le G-code', page)

    def test_camera_capture_lock_is_cleared_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = ac.default_state()
            state["camera"]["capture_in_progress"] = True
            ac.atomic_save(state, path)
            app = ac.Companion(path)
            self.assertFalse(app.state["camera"]["capture_in_progress"])

    def test_camera_request_is_persisted_before_capture_thread_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app = ac.Companion(path)
            app.on_message({"print": {"gcode_state": "RUNNING", "layer_num": 5}})
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(5, persisted["camera"]["pending_capture_layer"])

    def test_mqtt_events_are_durable_and_never_store_the_lan_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app = ac.Companion(path)
            app.configure({"access_code": "87654321"})
            app.on_message({"print": {
                "gcode_state": "RUNNING", "subtask_id": "audit-1",
                "subtask_name": "audit.gcode.3mf", "layer_num": 15, "mc_percent": 42,
                "access_code": "87654321",
            }})
            event = app.public_state()["events"][0]
            self.assertEqual("processed", event["outcome"])
            self.assertEqual("audit-1", event["task_id"])
            self.assertEqual(15, event["layer"])
            self.assertNotIn("87654321", json.dumps(event))
            restarted = ac.Companion(path)
            restored = restarted.events.recent()[0]
            self.assertEqual("audit-1", restored["task_id"])
            self.assertEqual("processed", restored["outcome"])

    def test_supervision_report_is_compact_and_never_contains_lan_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.configure({"access_code": "87654321", "ip": "192.168.1.24", "serial": "SECRET-SERIAL"})
            app.state["active_job"] = {"object_map": {
                "status": "mapped", "objects": [{"id": "cube", "label": "Cube"}],
            }}
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "report-1", "layer_num": 5}})
            report = app.supervision_report()
            self.assertEqual(1, report["schema_version"])
            self.assertEqual("3.0.4", report["application"]["version"])
            self.assertEqual(1, report["reliability"]["event_count"])
            self.assertEqual("processed", report["reliability"]["events"][0]["outcome"])
            self.assertEqual("mapped", report["print"]["object_map"]["status"])
            self.assertEqual(1, report["print"]["object_map"]["object_count"])
            encoded = json.dumps(report)
            self.assertNotIn("87654321", encoded)
            self.assertNotIn("192.168.1.24", encoded)
            self.assertNotIn("SECRET-SERIAL", encoded)

    def test_supervision_snapshot_explains_healthy_and_attention_states(self):
        state = ac.default_state()
        state["printer"] = {"connected": True, "state": "RUNNING", "progress": 42, "job": "piece.3mf"}
        state["camera"].update({"enabled": True, "certificate_sha256": "a" * 64,
                                "active_print": {"id": "vision-1"}})
        state["active_job"] = {"object_map": {"status": "mapped", "objects": [{
            "id": "944", "protocol_object_id": 944,
        }]}}
        state["guardian"] = {"pending_proposals": [], "observations_count": 3}
        state["autopilot"] = {"prepared": [], "plans": []}
        state["vision_storage"] = {"count": 2}
        stamp = "2026-07-28T00:00:00+0200"
        summary = ac.build_supervision_snapshot(
            state, [{"received_at": stamp, "outcome": "processed"}],
            now_epoch=ac.iso_epoch(stamp) + 15,
        )
        self.assertEqual("ok", summary["overall"]["level"])
        self.assertEqual(1, summary["mapping"]["canonical_object_count"])
        self.assertEqual(15, summary["reliability"]["latest_event_age_seconds"])
        summary = ac.build_supervision_snapshot(
            state, [{"received_at": stamp, "outcome": "failed"}],
            now_epoch=ac.iso_epoch(stamp) + 15,
        )
        self.assertEqual("critical", summary["overall"]["level"])
        self.assertEqual(1, summary["reliability"]["failed_events"])

    def test_archives_redacted_manual_and_finished_print_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.configure({"access_code": "87654321", "ip": "192.168.1.24", "serial": "SECRET-SERIAL"})
            manual = app.archive_supervision_report()
            archived = app.archived_supervision_report(manual["id"])
            self.assertEqual("manual", manual["reason"])
            self.assertNotIn("87654321", json.dumps(archived))
            self.assertNotIn("192.168.1.24", json.dumps(archived))
            app.last_import = ac.parse_3mf(sample_3mf(4), "rapport.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "1"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "report-42"}})
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "report-42"}})
            history = app.reports.recent()
            self.assertIn("print_finished", [item["reason"] for item in history])
            restarted = ac.Companion(Path(tmp) / "state.json")
            self.assertGreaterEqual(len(restarted.reports.recent()), 2)

    def test_vision_storage_reports_only_indexed_capture_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app = ac.Companion(path)
            root = path.parent / "captures"
            root.mkdir()
            active = "layer-00005-20260727-230000.jpg"
            completed = "layer-00010-20260727-230500.jpg"
            folder = "print-20260727-job-impression"
            (root / active).write_bytes(b"abc")
            (root / folder).mkdir()
            (root / folder / completed).write_bytes(b"defgh")
            (root / "orphan.jpg").write_bytes(b"ignored")
            app.state["camera"]["captures"] = [
                {"file": active}, {"file": completed, "folder": folder},
            ]
            self.assertEqual(
                {"count": 2, "bytes": 8, "completed": 1, "active": 1},
                app.vision_storage(),
            )

    def test_guardian_rejects_an_object_missing_from_active_gcode_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.state["active_job"] = {
                "object_map": {"status": "mapped", "objects": [{"id": "cube", "label": "Cube"}]},
            }
            with self.assertRaisesRegex(ValueError, "cartographie G-code"):
                app.observe_plate_guardian({
                    "object_id": "inconnu", "object_label": "Inconnu", "confidence": 0.99,
                    "source": "test", "frame_sha256": "a" * 64,
                })

    def test_finished_print_moves_its_captures_into_a_dedicated_folder_and_can_delete_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app = ac.Companion(path)
            app.on_message({"print": {
                "gcode_state": "RUNNING", "subtask_id": "vision-42",
                "subtask_name": "Boite test.gcode.3mf", "layer_num": 5,
            }})
            session = app.state["camera"]["active_print"]
            filename = "layer-00005-20260727-230000.jpg"
            root = path.parent / "captures"
            root.mkdir()
            (root / filename).write_bytes(b"jpeg")
            app.state["camera"]["captures"] = [{
                "layer": 5, "captured_at": ac.now_iso(), "file": filename,
                "sha256": "a" * 64, "print_id": session["id"],
            }]
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "vision-42"}})
            folder = session["folder"]
            self.assertTrue((root / folder / filename).is_file())
            self.assertEqual(folder, app.state["camera"]["captures"][0]["folder"])
            self.assertEqual(1, app.state["camera"]["completed_prints"][0]["capture_count"])
            deleted = app.delete_capture_print(folder)
            self.assertEqual(1, deleted["deleted"])
            self.assertFalse((root / folder).exists())
            self.assertEqual([], app.state["camera"]["captures"])

    def test_inventory_summary_reports_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.last_import = ac.parse_3mf(sample_3mf(7), "summary.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "1"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "summary-1"}})
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "summary-1"}})
            row = next(item for item in app.public_state()["inventory_summary"] if item["slot"] == "1")
            self.assertEqual(1, row["print_count"])
            self.assertEqual(993, row["remaining_g"])
            self.assertTrue(row["last_used_at"])

    def test_catalogue_metadata_bulk_actions_and_csv_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            spool = app.create_spool({
                "name": "PLA bleu mat", "material": "PLA", "brand": "Bambu Lab", "color": "Bleu",
                "initial_g": 1000, "remaining_g": 80, "storage_location": "Étagère B-03",
                "low_stock_g": 120, "cost_eur": 22.5, "notes": "Test de finition",
            })
            self.assertEqual("Étagère B-03", spool["storage_location"])
            self.assertEqual(120, spool["low_stock_g"])
            overview = app.public_state()["inventory_overview"]
            self.assertGreaterEqual(overview["low_stock"], 1)
            self.assertIn("PLA bleu mat", app.inventory_csv().decode("utf-8-sig"))

            moved = app.bulk_inventory_update({
                "ids": [spool["id"]], "action": "location", "storage_location": "Bac Atelier",
            })
            self.assertTrue(moved["ok"])
            limited = app.bulk_inventory_update({
                "ids": [spool["id"]], "action": "threshold", "low_stock_g": 60,
            })
            self.assertEqual(1, limited["count"])
            updated = app.inventory.spool(spool["id"])
            self.assertEqual("Bac Atelier", updated["storage_location"])
            self.assertEqual(60, updated["low_stock_g"])

            archived = app.archive_inventory_spools([spool["id"]])
            self.assertTrue(archived["ok"])
            self.assertNotIn(spool["id"], [x["id"] for x in app.public_state()["inventory"]["spools"]])
            with app.inventory._connect() as connection:
                row = connection.execute("SELECT archived FROM spools WHERE id = ?", (spool["id"],)).fetchone()
                events = connection.execute(
                    "SELECT event_type FROM inventory_history WHERE spool_id = ?", (spool["id"],)
                ).fetchall()
            self.assertEqual(1, row["archived"])
            self.assertIn("archive", [event["event_type"] for event in events])

    def test_multispool_settlement_stays_idempotent_after_crash_before_state_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app = ac.Companion(path)
            app.last_import = ac.parse_3mf(sample_3mf(10.5, 4.25), "multi.gcode.3mf")
            app.arm({"plate": "1", "mappings": [
                {"filament_id": "1", "slot": "1"}, {"filament_id": "2", "slot": "4"},
            ]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "crash-safe"}})
            active = app.state["active_job"]
            # Simulate a process crash just after SQLite commits, before the
            # state file records the accounting key.
            app.inventory.settle_print(f":{active['task_id']}", [
                {"slot": line["slot"], "spool_id": line["spool_id"], "used_g": line["used_g"]}
                for line in active["lines"]
            ])
            restarted = ac.Companion(path)
            restarted.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "crash-safe"}})
            self.assertEqual(989.5, restarted.state["spools"]["1"]["remaining_g"])
            self.assertEqual(995.75, restarted.state["spools"]["4"]["remaining_g"])
            self.assertEqual(1, len(restarted.state["accounted"]))

    def test_corrupt_state_is_preserved_before_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not valid json", encoding="utf-8")
            state = ac.load_state(path)
            self.assertEqual(1, len(list(Path(tmp).glob("state.corrompu-*.json"))))
            self.assertIn("sauvegardé", state["recovery_notice"])
            self.assertFalse(path.exists())

    def test_3mf_rejects_compression_bomb(self):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Metadata/slice_info.config", b"0" * (2 * 1024 * 1024))
        with self.assertRaisesRegex(ValueError, "compression anormal"):
            ac.parse_3mf(raw.getvalue(), "bomb.gcode.3mf")

    def test_mqtt_certificate_is_pinned_after_first_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.verify_or_remember_mqtt_certificate("a" * 64)
            self.assertEqual("a" * 64, app.state["config"]["mqtt_certificate_sha256"])
            app.verify_or_remember_mqtt_certificate("a" * 64)
            with self.assertRaisesRegex(ConnectionError, "certificat MQTT"):
                app.verify_or_remember_mqtt_certificate("b" * 64)

    def test_cancel_does_not_deduct(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.last_import = ac.parse_3mf(sample_3mf(12), "job.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "2"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "43"}})
            app.on_message({"print": {"gcode_state": "FAILED", "subtask_id": "43"}})
            self.assertEqual(1000, app.state["spools"]["2"]["remaining_g"])
            self.assertEqual("FAILED", app.state["history"][0]["result"])
            self.assertFalse(app.state["history"][0]["deducted"])

    def test_cancel_before_running_is_kept_in_history_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            cancelled = {
                "gcode_state": "CANCEL",
                "subtask_id": "cancelled-before-running",
                "subtask_name": "test-annule.gcode.3mf",
            }
            app.on_message({"print": cancelled})
            app.on_message({"print": cancelled})

            self.assertEqual(1, len(app.state["history"]))
            entry = app.state["history"][0]
            self.assertEqual("CANCEL", entry["result"])
            self.assertFalse(entry["deducted"])
            self.assertTrue(entry["untracked"])

    def test_finished_untracked_print_is_kept_in_history_without_deduction(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.on_message({"print": {
                "gcode_state": "RUNNING",
                "subtask_id": "finished-without-3mf",
                "subtask_name": "test-sans-fichier.gcode.3mf",
            }})
            app.on_message({"print": {
                "gcode_state": "FINISH",
                "subtask_id": "finished-without-3mf",
                "subtask_name": "test-sans-fichier.gcode.3mf",
            }})

            entry = app.state["history"][0]
            self.assertEqual("FINISH", entry["result"])
            self.assertFalse(entry["deducted"])
            self.assertIn("sans décompte", entry["tracking_note"])

    def test_startup_finish_without_running_does_not_create_false_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "previous-task"}})
            self.assertEqual([], app.state["history"])

    def test_never_below_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.update_spools({"3": {"remaining_g": 2}})
            app.last_import = ac.parse_3mf(sample_3mf(9), "job.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "3"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "44"}})
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "44"}})
            self.assertEqual(0, app.state["spools"]["3"]["remaining_g"])

    def test_inventory_migrates_legacy_spools_and_restores_a_swapped_spool(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = ac.default_state()
            state["spools"]["1"].update({"name": "PLA rouge", "initial_g": 1000, "remaining_g": 382})
            ac.atomic_save(state, state_path)

            app = ac.Companion(state_path)
            inventory = app.public_state()["inventory"]["spools"]
            red = next(spool for spool in inventory if spool["name"] == "PLA rouge")
            self.assertEqual("1", red["slot"])
            self.assertEqual(382, red["remaining_g"])

            green = app.create_spool({
                "name": "PLA vert",
                "material": "PLA",
                "brand": "eSun",
                "color": "vert",
                "initial_g": 1000,
                "remaining_g": 746,
            })
            app.assign_spool({"slot": "1", "spool_id": green["id"]})
            self.assertEqual("PLA vert", app.state["spools"]["1"]["name"])
            app.assign_spool({"slot": "", "spool_id": green["id"]})
            self.assertIsNone(app.state["spools"]["1"]["spool_id"])

            app.assign_spool({"slot": "1", "spool_id": red["id"]})
            restored = next(spool for spool in app.public_state()["inventory"]["spools"] if spool["id"] == red["id"])
            self.assertEqual("1", restored["slot"])
            self.assertEqual(382, restored["remaining_g"])

    def test_moving_an_installed_spool_to_an_occupied_slot_swaps_them_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            first_id = app.state["spools"]["1"]["spool_id"]
            second_id = app.state["spools"]["2"]["spool_id"]

            result = app.assign_spool({"slot": "2", "spool_id": first_id})
            self.assertEqual("swapped", result["action"])
            self.assertEqual(first_id, app.state["spools"]["2"]["spool_id"])
            self.assertEqual(second_id, app.state["spools"]["1"]["spool_id"])
            self.assertIn("Échange effectué", result["message"])

            before = len(app.spool_history(first_id)["events"])
            retry = app.assign_spool({"slot": "2", "spool_id": first_id})
            self.assertEqual("unchanged", retry["action"])
            self.assertEqual(before, len(app.spool_history(first_id)["events"]))

    def test_placing_an_unassigned_spool_retires_previous_occupant_without_losing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            previous_id = app.state["spools"]["1"]["spool_id"]
            green = app.create_spool({"name": "PLA vert", "initial_g": 1000, "remaining_g": 750})

            result = app.assign_spool({"slot": "1", "spool_id": green["id"]})
            self.assertEqual("replaced", result["action"])
            self.assertEqual(green["id"], app.state["spools"]["1"]["spool_id"])
            inventory = {spool["id"]: spool for spool in app.public_state()["inventory"]["spools"]}
            self.assertIsNone(inventory[previous_id]["slot"])
            self.assertIn("remove", [event["type"] for event in app.spool_history(previous_id)["events"]])

    def test_removing_an_already_unassigned_spool_does_not_create_duplicate_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            spool_id = app.state["spools"]["1"]["spool_id"]
            app.assign_spool({"slot": "", "spool_id": spool_id})
            before = len(app.spool_history(spool_id)["events"])
            retry = app.assign_spool({"slot": "", "spool_id": spool_id})
            self.assertEqual("unchanged", retry["action"])
            self.assertEqual(before, len(app.spool_history(spool_id)["events"]))

    def test_deleting_a_spool_frees_its_slot_and_erases_its_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            spool_id = app.state["spools"]["1"]["spool_id"]

            result = app.delete_inventory_spool(spool_id)

            self.assertTrue(result["ok"])
            self.assertIn("historique", result["message"])
            self.assertIsNone(app.state["spools"]["1"]["spool_id"])
            self.assertNotIn(
                spool_id,
                [spool["id"] for spool in app.public_state()["inventory"]["spools"]],
            )
            with app.inventory._connect() as connection:
                spool_count = connection.execute(
                    "SELECT COUNT(*) FROM spools WHERE id = ?", (spool_id,)
                ).fetchone()[0]
                history_count = connection.execute(
                    "SELECT COUNT(*) FROM inventory_history WHERE spool_id = ?", (spool_id,)
                ).fetchone()[0]
            self.assertEqual(0, spool_count)
            self.assertEqual(0, history_count)
            with self.assertRaisesRegex(ValueError, "introuvable"):
                app.spool_history(spool_id)

    def test_deleting_a_spool_removes_its_entries_from_global_print_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            deleted_id = app.state["spools"]["1"]["spool_id"]
            kept_id = app.state["spools"]["2"]["spool_id"]
            app.state["history"] = [{
                "lines": [{"spool_id": deleted_id}, {"spool_id": kept_id}],
                "deductions": [{"spool_id": deleted_id}, {"spool_id": kept_id}],
            }, {"lines": [{"spool_id": deleted_id}]}]

            app.delete_inventory_spool(deleted_id)

            self.assertEqual(1, len(app.state["history"]))
            self.assertEqual([kept_id], [line["spool_id"] for line in app.state["history"][0]["lines"]])
            self.assertEqual([kept_id], [line["spool_id"] for line in app.state["history"][0]["deductions"]])

    def test_startup_persists_the_inventory_slot_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            app = ac.Companion(state_path)

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(app.state["spools"], saved["spools"])
            self.assertIsNotNone(saved["spools"]["1"]["spool_id"])

    def test_legacy_print_history_is_imported_once_with_its_original_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = ac.default_state()
            state["history"] = [{
                "token": "old-print", "ended_at": "2026-07-25T14:25:00+0200", "deducted": True,
                "deductions": [{"slot": "1", "used_g": 12.5, "before_g": 1000, "after_g": 987.5}],
            }]
            ac.atomic_save(state, state_path)

            app = ac.Companion(state_path)
            spool_id = app.state["spools"]["1"]["spool_id"]
            events = app.spool_history(spool_id)["events"]
            imported = [event for event in events if "Historique importé" in event["detail"]]
            self.assertEqual(1, len(imported))
            self.assertEqual("2026-07-25", imported[0]["created_at"][:10])

            restarted = ac.Companion(state_path)
            events_after_restart = restarted.spool_history(spool_id)["events"]
            self.assertEqual(1, len([event for event in events_after_restart if "Historique importé" in event["detail"]]))

    def test_spool_name_and_first_history_entry_can_be_backdated(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            spool = app.create_spool({
                "material": "PLA", "color": "bleu", "initial_g": 1000,
                "remaining_g": 700, "created_at": "2024-05-12",
            })

            self.assertEqual("PLA bleu", spool["name"])
            self.assertEqual("2024-05-12", spool["created_at"][:10])
            history = app.spool_history(spool["id"])["events"]
            self.assertEqual("2024-05-12", history[0]["created_at"][:10])

            app.update_inventory_spool(spool["id"], {"created_at": "2023-01-03"})
            updated = app.spool_history(spool["id"])
            self.assertEqual("2023-01-03", updated["spool"]["created_at"][:10])
            self.assertEqual("2023-01-03", updated["events"][0]["created_at"][:10])

    def test_spool_date_cannot_be_in_the_future(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            with self.assertRaisesRegex(ValueError, "futur"):
                app.create_spool({"material": "PLA", "color": "bleu", "created_at": "2999-01-01"})

    def test_cannot_archive_spool_used_by_active_print(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            spool_id = app.state["spools"]["1"]["spool_id"]
            app.last_import = ac.parse_3mf(sample_3mf(10), "job.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "1"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "archive-test"}})

            with self.assertRaisesRegex(ValueError, "impression en cours"):
                app.delete_inventory_spool(spool_id)

    def test_deduction_stays_with_spool_that_started_the_print(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.update_spools({"1": {"name": "PLA rouge", "remaining_g": 100}})
            red_id = app.state["spools"]["1"]["spool_id"]
            green = app.create_spool({"name": "PLA vert", "initial_g": 100, "remaining_g": 100})
            app.last_import = ac.parse_3mf(sample_3mf(10), "job.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "1"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "swap-test"}})

            app.assign_spool({"slot": "1", "spool_id": green["id"]})
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "swap-test"}})

            spools = {spool["id"]: spool for spool in app.public_state()["inventory"]["spools"]}
            self.assertEqual(90, spools[red_id]["remaining_g"])
            self.assertEqual(100, spools[green["id"]]["remaining_g"])

    def test_rfid_sync_recognises_the_same_spool_after_it_returns_to_ams(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            report = {
                "print": {
                    "ams": {"ams": [{"tray": [{
                        "id": "0", "tag_uid": "A1B2C3D4E5F6", "tray_info_idx": "GFA12",
                        "tray_type": "PLA", "tray_color": "FF6A13FF",
                    }]}]}
                }
            }
            app.on_message(report)
            first = app.state["spools"]["1"]
            first_id = first["spool_id"]
            self.assertEqual("PLA", next(
                x["material"] for x in app.public_state()["inventory"]["spools"] if x["id"] == first_id
            ))
            self.assertEqual("Orange", next(
                x["color"] for x in app.public_state()["inventory"]["spools"] if x["id"] == first_id
            ))
            self.assertEqual("PLA Orange", next(
                x["name"] for x in app.public_state()["inventory"]["spools"] if x["id"] == first_id
            ))
            self.assertIn("RFID synchronisé", app.state["printer"]["rfid_status"])

            app.update_spools({"1": {"remaining_g": 382}})
            app.assign_spool({"slot": "", "spool_id": first_id})
            app.on_message(report)
            self.assertEqual(first_id, app.state["spools"]["1"]["spool_id"])
            self.assertEqual(382, app.state["spools"]["1"]["remaining_g"])

    def test_multifilament_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app = ac.Companion(path)
            app.last_import = ac.parse_3mf(sample_3mf(10.5, 4.25), "multi.gcode.3mf")
            app.arm({"plate": "1", "mappings": [
                {"filament_id": "1", "slot": "1"},
                {"filament_id": "2", "slot": "4"},
            ]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "45"}})
            # Simulate Companion being restarted while the printer is running.
            app.state["bridge"].update({
                "mapping_confirmation_required": True,
                "mapping_conflict": [{"filament_id": "1"}],
                "status": "Correspondance AMS modifiée — confirmation requise",
            })
            app.save()
            restarted = ac.Companion(path)
            restarted.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "45"}})
            self.assertFalse(restarted.state["bridge"]["mapping_confirmation_required"])
            self.assertEqual([], restarted.state["bridge"]["mapping_conflict"])
            self.assertEqual("Impression en cours, suivi filament actif", restarted.state["bridge"]["status"])
            restarted.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "45"}})
            self.assertEqual(989.5, restarted.state["spools"]["1"]["remaining_g"])
            self.assertEqual(995.75, restarted.state["spools"]["4"]["remaining_g"])
            # A repeated terminal frame after another restart remains idempotent.
            again = ac.Companion(path)
            again.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "45"}})
            self.assertEqual(989.5, again.state["spools"]["1"]["remaining_g"])

    def test_new_task_replaces_stale_active_job_without_deduction(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.last_import = ac.parse_3mf(sample_3mf(40), "old.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "1"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "old-task"}})

            parsed = ac.parse_3mf(sample_3mf(6), "new.gcode.3mf")
            app.on_studio_archive(Path(tmp) / "new.3mf", parsed)
            app.on_mqtt_message("device/SERIAL/request", {"print": {
                "ams_mapping": [0], "param": "Metadata/plate_1.gcode"
            }})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "new-task"}})

            self.assertEqual("new-task", app.state["active_job"]["task_id"])
            self.assertEqual("REMPLACÉ", app.state["history"][0]["result"])
            self.assertFalse(app.state["history"][0]["deducted"])
            self.assertEqual(1000, app.state["spools"]["1"]["remaining_g"])
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "new-task"}})
            self.assertEqual(994, app.state["spools"]["1"]["remaining_g"])

    def test_terminal_state_for_another_task_does_not_debit_active_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.last_import = ac.parse_3mf(sample_3mf(40), "current.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "1"}]})
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "current-task"}})

            # A delayed FINISH frame for the previous task must not settle the
            # active task or reduce its spool level.
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "previous-task"}})
            self.assertEqual("current-task", app.state["active_job"]["task_id"])
            self.assertEqual(1000, app.state["spools"]["1"]["remaining_g"])
            self.assertEqual([], app.state["history"])

            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "current-task"}})
            self.assertEqual(960, app.state["spools"]["1"]["remaining_g"])

    def test_progress_is_kept_within_zero_and_one_hundred(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.on_message({"print": {"gcode_state": "RUNNING", "mc_percent": "125"}})
            self.assertEqual(100, app.state["printer"]["progress"])
            app.on_message({"print": {"gcode_state": "RUNNING", "mc_percent": -8}})
            self.assertEqual(0, app.state["printer"]["progress"])

    def test_bridge_recovers_studio_archive_and_uses_saved_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bamboo_model"
            metadata = root / "job#123" / "Metadata"
            metadata.mkdir(parents=True)
            app = ac.Companion(Path(tmp) / "state.json", [root])
            app.bridge.stable_seconds = 0
            archive = metadata / ".123.0.3mf"
            archive.write_bytes(sample_3mf(10.5, 4.25))
            app.bridge.scan_once()
            app.bridge.scan_once()
            self.assertIsNotNone(app.auto_import)
            self.assertIsNotNone(app.state["armed_job"])
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "auto-1"}})
            self.assertEqual(["1", "2"], [line["slot"] for line in app.state["active_job"]["lines"]])
            self.assertEqual("Correspondance enregistrée", app.state["active_job"]["mapping_source"])
            self.assertEqual("Impression en cours, suivi filament actif", app.state["bridge"]["status"])
            self.assertFalse(app.state["bridge"]["mapping_confirmation_required"])
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "auto-1"}})
            self.assertEqual(989.5, app.state["spools"]["1"]["remaining_g"])
            self.assertEqual(995.75, app.state["spools"]["2"]["remaining_g"])

    def test_bridge_uses_ams_mapping_from_studio_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json", [Path(tmp) / "watch"])
            parsed = ac.parse_3mf(sample_3mf(12, 3), "automatic.gcode.3mf")
            app.on_studio_archive(Path(tmp) / "automatic.3mf", parsed)
            app.on_mqtt_message("device/SERIAL/request", {"print": {
                "command": "project_file",
                "ams_mapping": [2, 0],
                "param": "Metadata/plate_1.gcode",
                "subtask_name": "Bicolore",
            }})
            self.assertIsNone(app.state["armed_job"])
            self.assertTrue(app.state["bridge"]["mapping_confirmation_required"])
            self.assertEqual("Correspondance AMS modifiée — confirmation requise", app.state["bridge"]["status"])
            app.confirm_auto_import()
            armed = app.state["armed_job"]
            self.assertEqual(["3", "1"], [line["slot"] for line in armed["lines"]])
            self.assertEqual("Commande Bambu Studio", armed["mapping_source"])
            self.assertEqual("Bicolore", armed["file"])
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "mapped-1"}})
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "mapped-1"}})
            self.assertEqual(988, app.state["spools"]["3"]["remaining_g"])
            self.assertEqual(997, app.state["spools"]["1"]["remaining_g"])

    def test_bridge_can_keep_saved_mapping_after_a_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json", [Path(tmp) / "watch"])
            parsed = ac.parse_3mf(sample_3mf(12), "conflict.gcode.3mf")
            app.on_studio_archive(Path(tmp) / "conflict.3mf", parsed)
            app.on_mqtt_message("device/SERIAL/request", {"print": {
                "ams_mapping": [2], "param": "Metadata/plate_1.gcode",
            }})
            self.assertIsNone(app.state["armed_job"])
            app.use_saved_mapping_for_auto_import()
            self.assertFalse(app.state["bridge"]["mapping_confirmation_required"])
            self.assertEqual("Correspondance enregistrée", app.state["armed_job"]["mapping_source"])
            self.assertEqual("1", app.state["armed_job"]["lines"][0]["slot"])

    def test_bridge_auto_arms_when_bambu_mapping_matches_saved_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json", [Path(tmp) / "watch"])
            parsed = ac.parse_3mf(sample_3mf(12), "matching.gcode.3mf")
            app.on_studio_archive(Path(tmp) / "matching.3mf", parsed)
            app.on_mqtt_message("device/SERIAL/request", {"print": {
                "ams_mapping": [0], "param": "Metadata/plate_1.gcode",
            }})
            self.assertFalse(app.state["bridge"]["mapping_confirmation_required"])
            self.assertEqual("Travail armé automatiquement (Commande Bambu Studio)", app.state["bridge"]["status"])
            self.assertEqual("1", app.state["armed_job"]["lines"][0]["slot"])

    def test_bridge_auto_arm_keeps_the_selected_plate_object_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json", [Path(tmp) / "watch"])
            parsed = ac.parse_3mf(sample_mapped_3mf(), "bridge-mapped.gcode.3mf")
            expected_map = parsed["plates"][0]["object_map"]
            parsed["object_map"] = {"status": "unavailable", "objects": []}
            app.on_studio_archive(Path(tmp) / "bridge-mapped.3mf", parsed)
            app.on_mqtt_message("device/SERIAL/request", {"print": {
                "ams_mapping": [0], "param": "Metadata/plate_1.gcode",
            }})
            self.assertEqual(expected_map, app.state["armed_job"]["object_map"])

    def test_bridge_request_can_arrive_before_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json", [Path(tmp) / "watch"])
            app.configure_bridge({"default_mapping": {"1": "4", "2": "2"}})
            app.on_mqtt_message("device/SERIAL/request", {"print": {
                "ams_mapping": "[3,1]", "param": "Metadata/plate_1.gcode"
            }})
            parsed = ac.parse_3mf(sample_3mf(8, 2), "later.gcode.3mf")
            app.on_studio_archive(Path(tmp) / "later.3mf", parsed)
            self.assertEqual(["4", "2"], [line["slot"] for line in app.state["armed_job"]["lines"]])

    def test_bridge_does_not_replace_manual_job_or_choose_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bamboo_model"
            metadata = root / "job#123" / "Metadata"
            metadata.mkdir(parents=True)
            app = ac.Companion(Path(tmp) / "state.json", [root])
            app.bridge.stable_seconds = 0
            app.last_import = ac.parse_3mf(sample_3mf(7), "manual.gcode.3mf")
            app.arm({"plate": "1", "mappings": [{"filament_id": "1", "slot": "4"}]})
            old = metadata / "old.3mf"
            old.write_bytes(sample_3mf(99))
            old_time = app.bridge.started_at - 60
            os.utime(old, (old_time, old_time))
            newest = metadata / "new.3mf"
            newest.write_bytes(sample_3mf(2))
            app.bridge.scan_once()
            app.bridge.scan_once()
            self.assertEqual("manual.gcode.3mf", app.state["armed_job"]["file"])
            self.assertEqual("new.3mf", app.auto_import["filename"])
            self.assertEqual("Fichier détecté, travail manuel conservé", app.state["bridge"]["status"])

    def test_bridge_waits_for_complete_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bamboo_model"
            metadata = root / "job#123" / "Metadata"
            metadata.mkdir(parents=True)
            app = ac.Companion(Path(tmp) / "state.json", [root])
            app.bridge.stable_seconds = 0
            archive = metadata / "writing.3mf"
            archive.write_bytes(b"not complete")
            app.bridge.scan_once()
            app.bridge.scan_once()
            self.assertIsNone(app.auto_import)
            time.sleep(0.002)
            archive.write_bytes(sample_3mf(6))
            app.bridge.scan_once()
            app.bridge.scan_once()
            self.assertEqual("writing.3mf", app.auto_import["filename"])

    def test_bridge_never_falls_back_to_an_older_recent_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bamboo_model"
            metadata = root / "job#123" / "Metadata"
            metadata.mkdir(parents=True)
            app = ac.Companion(Path(tmp) / "state.json", [root])
            app.bridge.stable_seconds = 0
            older = metadata / "older.3mf"
            older.write_bytes(sample_3mf(90))
            time.sleep(0.002)
            newest = metadata / "newest.3mf"
            newest.write_bytes(sample_3mf(5))
            app.bridge.scan_once()
            app.bridge.scan_once()
            app.bridge.scan_once()
            self.assertEqual("newest.3mf", app.auto_import["filename"])
            self.assertEqual(5, app.auto_import["plates"][0]["filaments"][0]["used_g"])

    def test_bridge_ignores_root_project_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bamboo_model" / "job#123"
            metadata = root / "Metadata"
            metadata.mkdir(parents=True)
            app = ac.Companion(Path(tmp) / "state.json", [root.parent])
            app.bridge.stable_seconds = 0
            print_package = metadata / ".123.0.3mf"
            print_package.write_bytes(sample_3mf(8))
            time.sleep(0.002)
            project_backup = root / ".3mf"
            project_backup.write_bytes(sample_3mf(99))

            self.assertEqual([print_package], app.bridge.candidates())
            app.bridge.scan_once()
            app.bridge.scan_once()
            self.assertEqual(".123.0.3mf", app.auto_import["filename"])
            self.assertEqual(8, app.auto_import["plates"][0]["filaments"][0]["used_g"])

    def test_finish_consumes_auto_import_and_does_not_rearm(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            first = ac.parse_3mf(sample_3mf(9), "print.3mf")
            app.on_studio_archive(Path(tmp) / "Metadata" / "print.3mf", first)
            app.confirm_auto_import()
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "task-1"}})
            self.assertIsNotNone(app.state["active_job"])

            # Reproduce beta.2: another archive appears while the task runs.
            backup = ac.parse_3mf(sample_3mf(90), "backup.3mf")
            app.on_studio_archive(Path(tmp) / ".3mf", backup)
            self.assertIsNotNone(app.auto_import)
            app.on_message({"print": {"gcode_state": "FINISH", "subtask_id": "task-1"}})
            app.bridge_tick()

            self.assertEqual(991, app.state["spools"]["1"]["remaining_g"])
            self.assertIsNone(app.auto_import)
            self.assertIsNone(app.pending_request)
            self.assertIsNone(app.state["armed_job"])
            self.assertIn("Impression terminée", app.state["bridge"]["status"])

    def test_bridge_expires_unconfirmed_import_instead_of_arming_a_later_print(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.on_studio_archive(Path(tmp) / "Metadata" / "old.3mf", ac.parse_3mf(sample_3mf(9)))
            app.auto_import["detected_epoch"] -= ac.MAX_AUTO_IMPORT_AGE_SECONDS + 1
            app.bridge_tick()
            app.on_message({"print": {"gcode_state": "RUNNING", "subtask_id": "unrelated"}})
            self.assertIsNone(app.auto_import)
            self.assertIsNone(app.state["armed_job"])
            self.assertIsNone(app.state["active_job"])
            self.assertIn("en attente du prochain travail", app.state["bridge"]["status"])

    def test_bridge_recovers_after_an_expired_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.on_studio_archive(Path(tmp) / "Metadata" / "old.3mf", ac.parse_3mf(sample_3mf(9)))
            app.auto_import["detected_epoch"] -= ac.MAX_AUTO_IMPORT_AGE_SECONDS + 1
            app.bridge_tick()

            fresh = ac.parse_3mf(sample_3mf(4), "fresh.3mf")
            app.on_studio_archive(Path(tmp) / "Metadata" / "fresh.3mf", fresh)
            self.assertEqual("fresh.3mf", app.auto_import["filename"])
            self.assertIn("automatiquement", app.state["bridge"]["status"])
            self.assertEqual("fresh.3mf", app.state["armed_job"]["file"])

    def test_recent_print_command_can_use_a_prepared_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.on_studio_archive(Path(tmp) / "Metadata" / "prepared.3mf", ac.parse_3mf(sample_3mf(4)))
            app.auto_import["detected_epoch"] -= 30 * 60
            app.on_mqtt_message("device/SERIAL/request", {"print": {
                "ams_mapping": [0], "param": "Metadata/plate_1.gcode"
            }})
            self.assertEqual("Commande Bambu Studio", app.state["armed_job"]["mapping_source"])

    def test_recent_archive_is_restored_after_the_source_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            archive = Path(tmp) / "prepared.3mf"
            archive.write_bytes(sample_3mf(6))
            app = ac.Companion(state_path)
            app.on_studio_archive(archive, ac.parse_3mf_path(archive))
            archive.unlink()

            restarted = ac.Companion(state_path)
            self.assertIsNotNone(restarted.auto_import)
            self.assertIsNotNone(restarted.state["armed_job"])

    def test_startup_clears_legacy_auto_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            app = ac.Companion(state_path)
            parsed = ac.parse_3mf(sample_3mf(7), "legacy.3mf")
            app.on_studio_archive(Path(tmp) / "Metadata" / "legacy.3mf", parsed)
            with app.lock:
                app._try_auto_arm_locked(force_fallback=True)
                app.state["armed_job"].pop("armed_epoch")
                app.save()

            restarted = ac.Companion(state_path)
            self.assertIsNotNone(restarted.state["armed_job"])
            self.assertEqual("legacy.3mf", restarted.state["armed_job"]["file"])
            self.assertEqual(1000, restarted.state["spools"]["1"]["remaining_g"])

    def test_http_interface_and_state_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ac.Companion(Path(tmp) / "state.json")
            app.state["active_job"] = {
                "token": "http-job", "object_map": ac.parse_3mf(sample_mapped_3mf())["object_map"],
            }
            app.state["printer"].update({"connected": True, "state": "RUNNING"})
            mqtt_stub = ManualMQTTStub()
            app.mqtt = mqtt_stub
            proposal = None
            for index in range(3):
                observed = app.observe_plate_guardian({
                    "object_id": "944", "object_label": "Piece test", "confidence": 0.95,
                    "source": "http-test", "frame_sha256": f"{index + 10:064x}",
                })
                proposal = observed["proposal"] or proposal
            self.assertIsNotNone(proposal)
            server = ac.ThreadingHTTPServer(("127.0.0.1", 0), ac.Handler)
            server.app = app
            server.api_token = "test-session-token"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                html = urllib.request.urlopen(base + "/", timeout=2).read().decode()
                headers = {"X-AMS-Token": server.api_token, "Content-Type": "application/json"}
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(base + "/api/state", timeout=2)
                self.assertEqual(403, rejected.exception.code)
                with self.assertRaises(urllib.error.HTTPError) as rejected_origin:
                    urllib.request.urlopen(urllib.request.Request(
                        base + "/api/state", headers={**headers, "Origin": "https://attacker.invalid"}), timeout=2)
                self.assertEqual(403, rejected_origin.exception.code)
                state = json.loads(urllib.request.urlopen(urllib.request.Request(
                    base + "/api/state", headers=headers), timeout=2).read())
                self.assertIn("AMS Lite Companion", html)
                self.assertIn("Arrêter Companion", html)
                self.assertIn("Passerelle Bambu Studio", html)
                self.assertIn("Cartographie G-code", html)
                self.assertIn("executeManualExclusion", html)
                self.assertIn("Gestionnaire de bobines", html)
                self.assertIn("catalogView=", html)
                self.assertIn("catalog-table", html)
                self.assertIn("catalogLoaded", html)
                self.assertIn('onchange="formDirty=true"', html)
                self.assertIn("body.embedded", html)
                self.assertIn("manual-card", html)
                self.assertIn("embedded=new URLSearchParams", html)
                self.assertIn(server.api_token, html)
                self.assertEqual(1000, state["spools"]["1"]["remaining_g"])
                prepare_request = urllib.request.Request(
                    base + f"/api/manual-exclusions/proposals/{proposal['id']}/prepare",
                    data=b"{}", method="POST", headers=headers,
                )
                prepared = json.loads(urllib.request.urlopen(prepare_request, timeout=2).read())
                self.assertEqual([944], prepared["instruction"]["print"]["obj_list"])
                self.assertEqual("prepared_manually", prepared["status"])
                execute_request = urllib.request.Request(
                    base + f"/api/manual-exclusions/proposals/{proposal['id']}/execute",
                    data=b'{"confirmed":true}', method="POST", headers=headers,
                )
                executed = json.loads(urllib.request.urlopen(execute_request, timeout=2).read())
                self.assertEqual("published", executed["transport"]["status"])
                self.assertEqual([944], mqtt_stub.sent[0]["print"]["obj_list"])
                report = json.loads(urllib.request.urlopen(urllib.request.Request(
                    base + "/api/report.json", headers=headers), timeout=2).read())
                self.assertEqual(1, report["schema_version"])
                self.assertEqual("3.0.4", report["application"]["version"])
                self.assertNotIn("access_code", json.dumps(report))
                self.assertIn("Poste de supervision", html)
                self.assertIn("Historique Vision et rapports", html)
                snapshot_request = urllib.request.Request(
                    base + "/api/reports/snapshot", data=b"{}", method="POST", headers=headers,
                )
                snapshot = json.loads(urllib.request.urlopen(snapshot_request, timeout=2).read())
                self.assertEqual("manual", snapshot["reason"])
                reports = json.loads(urllib.request.urlopen(urllib.request.Request(
                    base + "/api/reports", headers=headers), timeout=2).read())
                self.assertIn(snapshot["id"], [item["id"] for item in reports["reports"]])
                archived = json.loads(urllib.request.urlopen(urllib.request.Request(
                    base + f"/api/reports/{snapshot['id']}.json", headers=headers), timeout=2).read())
                self.assertIn("supervision", archived)
                bridge_request = urllib.request.Request(
                    base + "/api/bridge",
                    data=json.dumps({"enabled": True, "fallback_enabled": True,
                                     "default_mapping": {"1": "3"}}).encode(),
                    method="POST",
                    headers=headers,
                )
                bridge_result = json.loads(urllib.request.urlopen(bridge_request, timeout=2).read())
                self.assertTrue(bridge_result["ok"])
                self.assertEqual("3", app.state["bridge"]["default_mapping"]["1"])
                new_spool_request = urllib.request.Request(
                    base + "/api/inventory/spools",
                    data=json.dumps({"name": "PLA rouge", "initial_g": 1000, "remaining_g": 382}).encode(),
                    method="POST",
                    headers=headers,
                )
                new_spool = json.loads(urllib.request.urlopen(new_spool_request, timeout=2).read())
                self.assertEqual("PLA rouge", new_spool["name"])
                assign_request = urllib.request.Request(
                    base + "/api/inventory/assign",
                    data=json.dumps({"slot": "1", "spool_id": new_spool["id"]}).encode(),
                    method="POST",
                    headers=headers,
                )
                assign_result = json.loads(urllib.request.urlopen(assign_request, timeout=2).read())
                self.assertTrue(assign_result["ok"])
                update_request = urllib.request.Request(
                    base + f"/api/inventory/spools/{new_spool['id']}",
                    data=json.dumps({"name": "PLA rouge", "material": "PLA", "remaining_g": 381.5}).encode(),
                    method="POST",
                    headers=headers,
                )
                updated_spool = json.loads(urllib.request.urlopen(update_request, timeout=2).read())
                self.assertEqual("PLA", updated_spool["material"])
                self.assertEqual(381.5, updated_spool["remaining_g"])
                spool_history = json.loads(urllib.request.urlopen(urllib.request.Request(
                    base + f"/api/inventory/spools/{new_spool['id']}/history", headers=headers), timeout=2
                ).read())
                self.assertEqual(new_spool["id"], spool_history["spool"]["id"])
                self.assertIn("assign", [event["type"] for event in spool_history["events"]])
                state = json.loads(urllib.request.urlopen(urllib.request.Request(
                    base + "/api/state", headers=headers), timeout=2).read())
                self.assertEqual("PLA rouge", state["spools"]["1"]["name"])
                self.assertEqual(381.5, state["spools"]["1"]["remaining_g"])
                self.assertEqual("1", next(
                    spool["slot"] for spool in state["inventory"]["spools"] if spool["id"] == new_spool["id"]
                ))
                archive_request = urllib.request.Request(
                    base + f"/api/inventory/spools/{new_spool['id']}/archive",
                    data=b"{}",
                    method="POST",
                    headers=headers,
                )
                archived = json.loads(urllib.request.urlopen(archive_request, timeout=2).read())
                self.assertTrue(archived["ok"])
                self.assertIn("historique", archived["message"])
                state = json.loads(urllib.request.urlopen(urllib.request.Request(
                    base + "/api/state", headers=headers), timeout=2).read())
                self.assertIsNone(state["spools"]["1"]["spool_id"])
                self.assertNotIn(new_spool["id"], [spool["id"] for spool in state["inventory"]["spools"]])
                request = urllib.request.Request(base + "/api/shutdown", data=b"{}", method="POST", headers=headers)
                result = json.loads(urllib.request.urlopen(request, timeout=2).read())
                self.assertTrue(result["ok"])
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
            finally:
                # /api/shutdown has already stopped serve_forever in the normal
                # path above. Calling shutdown() a second time after the server
                # thread has exited blocks on Python's shutdown event.
                if thread.is_alive():
                    server.shutdown()
                thread.join(timeout=2)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
