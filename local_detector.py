# -*- coding: utf-8 -*-
"""Local, read-only 3D-print fault classifier.

The optional model is Yodazon/3DPrintFailureType (MIT), pinned by the
installer.  It is deliberately loaded from the private Companion data folder,
never from the network at inference time.  This module has no Bambu protocol
or printer-control code.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


MODEL_FILENAME = "CNNModelV0_2.pth"
MODEL_SHA256 = "2ba203900ffb0b173d6f90fcf01a1fdcde0d6b96cefcf3b1c1c230a34ee9c705"
MODEL_REVISION = "38d7ffcc6104aa28250e615492238ac90ba3ce80"
MODEL_NAME = "Yodazon/3DPrintFailureType"
MODEL_LICENSE = "MIT"
LABELS = ("healthy", "spaghetti", "stringing", "extrusion_anomaly")
_MODEL_CACHE: dict[str, tuple[Any, Any]] = {}


class DetectorUnavailable(RuntimeError):
    """The optional local inference runtime has not been installed."""


def runtime_directory(app_dir: Path) -> Path:
    return app_dir / "detector-runtime"


def model_path(app_dir: Path) -> Path:
    return runtime_directory(app_dir) / "models" / MODEL_FILENAME


def _model_valid(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 200 * 1024 * 1024:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == MODEL_SHA256


def _model_present(path: Path) -> bool:
    """Cheap status probe; the installer already validates the full digest."""
    return path.is_file() and path.stat().st_size >= 200 * 1024 * 1024


def status(app_dir: Path) -> dict[str, Any]:
    runtime = runtime_directory(app_dir)
    model = model_path(app_dir)
    # Do not import PyTorch here: this method runs while Companion is idle and
    # must not reserve hundreds of megabytes merely to update the UI.
    torch_ready = (runtime / "torch" / "__init__.py").is_file()
    present = _model_present(model)
    ready = torch_ready and present
    if ready:
        message = "Détecteur local prêt : alerte après 3 captures concordantes"
    elif model.exists() and not present:
        message = "Fichier modèle invalide : réinstalle le détecteur local"
    elif not torch_ready:
        message = "Détecteur IA non installé sur ce Mac"
    else:
        message = "Modèle IA local absent"
    return {
        "ready": ready, "message": message, "runtime_ready": torch_ready,
        "model_ready": present, "model": MODEL_NAME, "revision": MODEL_REVISION,
        "license": MODEL_LICENSE, "labels": list(LABELS),
    }


def _torch_and_model(app_dir: Path) -> tuple[Any, Any]:
    cached = _MODEL_CACHE.get(str(app_dir))
    if cached is not None:
        return cached
    details = status(app_dir)
    if not details["ready"]:
        raise DetectorUnavailable(str(details["message"]))
    runtime = runtime_directory(app_dir)
    if str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    import torch  # type: ignore[import-not-found]
    import torch.nn as nn  # type: ignore[import-not-found]
    if not _model_valid(model_path(app_dir)):
        raise DetectorUnavailable("Empreinte du modèle local invalide : réinstalle le détecteur")

    class PrintFailureNet(nn.Module):
        """Architecture matching the published state dictionary exactly."""

        def __init__(self) -> None:
            super().__init__()
            self.layer1 = nn.Sequential(nn.Conv2d(3, 96, 11, stride=4), nn.BatchNorm2d(96), nn.ReLU(), nn.MaxPool2d(3, stride=2))
            self.layer2 = nn.Sequential(nn.Conv2d(96, 256, 5, padding=2), nn.BatchNorm2d(256), nn.ReLU(), nn.MaxPool2d(3, stride=2))
            self.layer3 = nn.Sequential(nn.Conv2d(256, 384, 3, padding=1), nn.BatchNorm2d(384), nn.ReLU())
            self.layer4 = nn.Sequential(nn.Conv2d(384, 384, 3, padding=1), nn.BatchNorm2d(384), nn.ReLU())
            self.layer5 = nn.Sequential(nn.Conv2d(384, 256, 3, padding=1), nn.BatchNorm2d(256), nn.ReLU(), nn.MaxPool2d(3, stride=2))
            self.fc = nn.Sequential(nn.Dropout(), nn.Linear(256 * 6 * 6, 4096), nn.ReLU())
            self.fc1 = nn.Sequential(nn.Dropout(), nn.Linear(4096, 4096), nn.ReLU())
            self.fc2 = nn.Sequential(nn.Linear(4096, 4))

        def forward(self, value: Any) -> Any:
            value = self.layer1(value)
            value = self.layer2(value)
            value = self.layer3(value)
            value = self.layer4(value)
            value = self.layer5(value)
            value = value.reshape(value.shape[0], -1)
            return self.fc2(self.fc1(self.fc(value)))

    network = PrintFailureNet()
    try:
        state = torch.load(model_path(app_dir), map_location="cpu", weights_only=True)
    except TypeError:  # torch < 2.0
        state = torch.load(model_path(app_dir), map_location="cpu")
    if not isinstance(state, dict):
        raise DetectorUnavailable("Le modèle local ne contient pas de poids exploitables")
    network.load_state_dict(state, strict=True)
    network.eval()
    _MODEL_CACHE[str(app_dir)] = (torch, network)
    return torch, network


def _tensor_for_image(image_path: Path, torch: Any) -> Any:
    try:
        from PIL import Image, ImageOps
        import numpy
    except ImportError as exc:
        raise DetectorUnavailable("Pillow ou NumPy sont indisponibles") from exc
    with Image.open(image_path) as original:
        image = ImageOps.fit(original.convert("RGB"), (227, 227), method=Image.Resampling.BILINEAR)
        pixels = numpy.asarray(image, dtype=numpy.float32) / 255.0
    pixels = (pixels - 0.5) / 0.5
    return torch.from_numpy(pixels.transpose(2, 0, 1)).unsqueeze(0)


def _classify_in_process(image_path: Path, app_dir: Path) -> dict[str, Any]:
    """Classify in the short-lived worker process."""
    if not image_path.is_file():
        raise FileNotFoundError("Capture Vision introuvable")
    torch, network = _torch_and_model(app_dir)
    with torch.inference_mode():
        probabilities = torch.softmax(network(_tensor_for_image(image_path, torch)), dim=1)[0].tolist()
    scores = {label: round(float(probability), 5) for label, probability in zip(LABELS, probabilities)}
    label = max(scores, key=scores.get)
    confidence = scores[label]
    return {
        "label": label, "confidence": confidence, "scores": scores,
        "model": MODEL_NAME, "revision": MODEL_REVISION, "license": MODEL_LICENSE,
    }


def classify(image_path: Path, app_dir: Path) -> dict[str, Any]:
    """Classify in a local worker that exits and returns its memory to macOS."""
    command = [sys.executable, str(Path(__file__)), "--classify", str(image_path), str(app_dir)]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=75)
    except subprocess.TimeoutExpired as exc:
        raise DetectorUnavailable("Analyse IA expirée après 75 secondes") from exc
    if completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip() or "Analyse IA locale impossible"
        raise DetectorUnavailable(message[-600:])
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DetectorUnavailable("Réponse du détecteur local invalide") from exc
    if not isinstance(result, dict):
        raise DetectorUnavailable("Réponse du détecteur local invalide")
    return result


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] != "--classify":
        raise SystemExit("usage: local_detector.py --classify IMAGE APP_DATA_DIR")
    try:
        result = _classify_in_process(Path(sys.argv[2]), Path(sys.argv[3]))
    except Exception as exc:  # report only a bounded message to the parent
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
