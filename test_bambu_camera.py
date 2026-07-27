import hashlib
import struct
import unittest

from bambu_camera import (
    AUTH_COMMAND,
    CAMERA_PORT,
    FRAME_HEADER_SUFFIX,
    CameraError,
    build_auth_packet,
    parse_frame_length,
    verify_certificate,
)


class BambuCameraTests(unittest.TestCase):
    def test_auth_packet_has_the_expected_fixed_size_and_fields(self):
        packet = build_auth_packet("12345678")
        self.assertEqual(80, len(packet))
        self.assertEqual((0x40, AUTH_COMMAND, 0, 0), struct.unpack("<IIII", packet[:16]))
        self.assertEqual(b"bblp\0\0\0\0", packet[16:24])
        self.assertEqual(b"12345678", packet[48:56])

    def test_frame_header_requires_the_known_camera_signature(self):
        header = struct.pack("<I", 1234) + FRAME_HEADER_SUFFIX
        self.assertEqual(1234, parse_frame_length(header))
        with self.assertRaisesRegex(CameraError, "inconnue"):
            parse_frame_length(b"\0" * 16)

    def test_unpinned_or_changed_certificate_is_refused(self):
        certificate = b"test certificate"
        fingerprint = hashlib.sha256(certificate).hexdigest()
        self.assertEqual(fingerprint, verify_certificate(fingerprint, certificate))
        with self.assertRaisesRegex(CameraError, "approuvé"):
            verify_certificate("", certificate)
        with self.assertRaisesRegex(CameraError, "a changé"):
            verify_certificate("0" * 64, certificate)

    def test_camera_port_is_not_a_printer_control_port(self):
        self.assertEqual(6000, CAMERA_PORT)


if __name__ == "__main__":
    unittest.main()
