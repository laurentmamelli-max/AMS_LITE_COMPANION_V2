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


if __name__ == "__main__":
    unittest.main()
