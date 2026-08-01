# -*- coding: utf-8 -*-
"""Read-only local camera client for Bambu A1/P1 MJPEG streams.

The protocol is community-documented rather than an official public API.  This
client deliberately requires a pinned TLS certificate before it accepts frames
and it never sends a printer-control command.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import socket
import ssl
import struct
from typing import Final


CAMERA_PORT: Final = 6000
AUTH_USER: Final = b"bblp"
AUTH_COMMAND: Final = 0x3000
FRAME_HEADER_SUFFIX: Final = bytes((0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0))
MAX_JPEG_BYTES: Final = 5 * 1024 * 1024


class CameraError(ConnectionError):
    """A safe camera failure that must never turn into printer control."""


@dataclass(frozen=True)
class CameraFrame:
    jpeg: bytes
    sha256: str
    certificate_sha256: str


def build_auth_packet(access_code: str) -> bytes:
    """Build the 80-byte read-only camera authentication packet."""
    try:
        password = access_code.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CameraError("Le code d'accès caméra doit être ASCII") from exc
    if not password or len(password) > 32:
        raise CameraError("Le code d'accès caméra doit contenir entre 1 et 32 caractères")
    return (
        struct.pack("<IIII", 0x40, AUTH_COMMAND, 0, 0)
        + AUTH_USER.ljust(32, b"\0")
        + password.ljust(32, b"\0")
    )


def parse_frame_length(header: bytes) -> int:
    """Validate a 16-byte camera header and return its JPEG payload length."""
    if len(header) != 16:
        raise CameraError("En-tête caméra incomplet")
    length = struct.unpack("<I", header[:4])[0]
    if header[4:] != FRAME_HEADER_SUFFIX:
        raise CameraError("Trame caméra inconnue")
    if not 4 <= length <= MAX_JPEG_BYTES:
        raise CameraError("Taille d'image caméra invalide")
    return length


def verify_certificate(expected_sha256: str, certificate: bytes) -> str:
    """Return the certificate fingerprint, refusing unpinned connections."""
    fingerprint = hashlib.sha256(certificate).hexdigest()
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise CameraError("Le certificat caméra doit être approuvé avant d'activer la surveillance")
    if not hmac.compare_digest(expected, fingerprint):
        raise CameraError("Le certificat de la caméra a changé ; connexion refusée")
    return fingerprint


def discover_certificate_sha256(host: str, *, timeout_seconds: float = 5.0) -> str:
    """Read only the camera certificate fingerprint; no camera command is sent."""
    host = str(host or "").strip()
    if not host:
        raise CameraError("Adresse de caméra absente")
    raw = socket.create_connection((host, CAMERA_PORT), timeout=timeout_seconds)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with raw, context.wrap_socket(raw, server_hostname=host) as connection:
            certificate = connection.getpeercert(binary_form=True)
            if not certificate:
                raise CameraError("Certificat caméra absent")
            return hashlib.sha256(certificate).hexdigest()
    except (OSError, ssl.SSLError) as exc:
        raise CameraError("Connexion caméra impossible") from exc


def _read_exact(connection: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise CameraError("Flux caméra interrompu")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def capture_jpeg(
    host: str,
    access_code: str,
    certificate_sha256: str,
    *,
    timeout_seconds: float = 5.0,
    max_packets: int = 12,
) -> CameraFrame:
    """Read one JPEG after a pinned TLS handshake on the local camera port."""
    host = str(host or "").strip()
    if not host:
        raise CameraError("Adresse de caméra absente")
    if max_packets < 1:
        raise CameraError("max_packets doit être positif")
    raw = socket.create_connection((host, CAMERA_PORT), timeout=timeout_seconds)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Bambu cameras use a local, self-signed certificate.  It is checked by
    # fingerprint immediately after the handshake instead of being trusted.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with raw, context.wrap_socket(raw, server_hostname=host) as connection:
            connection.settimeout(timeout_seconds)
            certificate = connection.getpeercert(binary_form=True)
            if not certificate:
                raise CameraError("Certificat caméra absent")
            fingerprint = verify_certificate(certificate_sha256, certificate)
            connection.sendall(build_auth_packet(access_code))
            for _ in range(max_packets):
                length = parse_frame_length(_read_exact(connection, 16))
                jpeg = _read_exact(connection, length)
                if jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9":
                    return CameraFrame(
                        jpeg=jpeg,
                        sha256=hashlib.sha256(jpeg).hexdigest(),
                        certificate_sha256=fingerprint,
                    )
    except (OSError, ssl.SSLError) as exc:
        raise CameraError("Connexion caméra impossible") from exc
    raise CameraError("Aucune image JPEG valide reçue de la caméra")
