import unittest

from gcode_mapper import map_gcode_objects, object_map_summary


class GCodeMapperTests(unittest.TestCase):
    def test_maps_explicit_objects_and_xy_bounds(self):
        gcode = """; OBJECT: Cube gauche
G1 X10.0 Y20.0 E1
G1 X30 Y22 E2
; STOP_PRINTING_OBJECT: Cube gauche
; PRINTING_OBJECT: Tour
G0 X50 Y60
G1 X55 Y70 E1
; END_OBJECT
"""
        objects = map_gcode_objects(gcode)
        self.assertEqual(2, len(objects))
        self.assertEqual("Cube gauche", objects[0]["label"])
        self.assertEqual({"min_x": 10.0, "max_x": 30.0, "min_y": 20.0, "max_y": 22.0}, objects[0]["bounds_xy"])
        self.assertEqual({"min_x": 50.0, "max_x": 55.0, "min_y": 60.0, "max_y": 70.0}, objects[1]["bounds_xy"])
        self.assertEqual("mapped", object_map_summary(objects)["status"])

    def test_does_not_invent_objects_without_markers(self):
        objects = map_gcode_objects("G1 X10 Y20 E1\nG1 X30 Y40 E2\n")
        self.assertEqual([], objects)
        self.assertEqual("unavailable", object_map_summary(objects)["status"])

    def test_aggregates_the_repeated_bambu_studio_object_markers(self):
        gcode = """; OBJECT_ID: 944
; start printing object, unique label id: 944
G1 X10 Y20 E1
G1 X15 Y24 E2
; stop printing object, unique label id: 944
; start printing object, unique label id: 1670
G1 X50 Y60 E1
; stop printing object, unique label id: 1670
; start printing object, unique label id: 944
G1 X11 Y19 E1
G1 X16 Y25 E2
; stop printing object, unique label id: 944
"""
        objects = map_gcode_objects(gcode)
        by_id = {item["id"]: item for item in objects}
        self.assertEqual({"944", "1670"}, set(by_id))
        self.assertEqual("Objet Bambu #944", by_id["944"]["label"])
        self.assertEqual(2, by_id["944"]["segment_count"])
        self.assertEqual(
            {"min_x": 10.0, "max_x": 16.0, "min_y": 19.0, "max_y": 25.0},
            by_id["944"]["bounds_xy"],
        )
        self.assertEqual(2, len(by_id["944"]["line_ranges"]))

    def test_keeps_a_true_segment_count_when_ranges_are_capped(self):
        gcode = """; start printing object, unique label id: 1
G1 X1 Y1
; stop printing object, unique label id: 1
; start printing object, unique label id: 1
G1 X2 Y2
; stop printing object, unique label id: 1
; start printing object, unique label id: 1
G1 X3 Y3
; stop printing object, unique label id: 1
"""
        item = map_gcode_objects(gcode, max_ranges_per_object=2)[0]
        self.assertEqual(3, item["segment_count"])
        self.assertEqual(2, len(item["line_ranges"]))
        self.assertTrue(item["line_ranges_truncated"])


if __name__ == "__main__":
    unittest.main()
