"""On-device recognition with LiteRT: YAMNet for sounds (AudioSet's 521 classes) and EfficientDet-Lite0 for people.

Needs numpy and ai-edge-litert, so it's imported only when a smart watch starts. The models are downloaded once
and checked against pinned hashes. Measured on the phone: ~14 ms per second of audio, ~65 ms per frame.
"""
import hashlib
import io
import logging
import subprocess
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from ai_edge_litert.interpreter import Interpreter
from PIL import Image

from store import STATE_DIR

log = logging.getLogger("detect")

MODEL_DIR = STATE_DIR / "models"
THREADS = 2  # Termux gets 3 cores; leave one for the bridge and ffmpeg
SAMPLE_RATE = 16000
WINDOW = 15600  # YAMNet hears 0.975 s at a time
HOP = SAMPLE_RATE // 2
SOUND_SCORE = 0.3  # tested on ESC-50 cries: real crying scores 0.5-0.9, a fussing, babbling baby stays under 0.2
PERSON_CLASS = 0  # COCO
PERSON_SCORE = 0.5


@dataclass(frozen=True)
class Model:
    file: str
    url: str
    sha256: str  # of the .tflite itself, even when it comes inside an archive
    archive_member: str = ""


YAMNET = Model(
    "yamnet.tflite",
    "https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite",
    "4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf",
)
# MediaPipe's own EfficientDet build leaves box decoding to MediaPipe; this one has it built in.
EFFICIENTDET = Model(
    "efficientdet_lite0.tflite",
    "https://www.kaggle.com/api/v1/models/tensorflow/efficientdet/tfLite/lite0-detection-metadata/1/download",
    "2e04c53bfeac0ac2a30c057c7e2a777594ce39baaac35a92f74fb1e8c4fc4e0b",
    archive_member="1.tflite",
)


def model_path(model: Model) -> Path:
    path = MODEL_DIR / model.file
    if path.exists():
        return path
    log.info(f"downloading {model.file}")
    request = urllib.request.Request(model.url, headers={"User-Agent": "m34-agent"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if model.archive_member:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            data = archive.extractfile(model.archive_member).read()
    if hashlib.sha256(data).hexdigest() != model.sha256:
        raise RuntimeError(f"{model.file} download didn't match its pinned hash")
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    partial.rename(path)
    return path


def decode(audio: Path, start: float = 0) -> np.ndarray:
    """Mono samples from start to the end; works on an Ogg file that is still being recorded."""
    return np.frombuffer(subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", f"{start:.4f}", "-i", str(audio),
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
        capture_output=True, check=True, timeout=60,
    ).stdout, np.float32)


def interpreter(model: Model) -> Interpreter:
    loaded = Interpreter(str(model_path(model)), num_threads=THREADS)
    loaded.allocate_tensors()
    return loaded


class SoundDetector:
    def __init__(self, class_names: tuple[str, ...]) -> None:
        self.model = interpreter(YAMNET)
        with zipfile.ZipFile(model_path(YAMNET)) as bundle:  # the .tflite carries its label list
            labels = bundle.read("yamnet_label_list.txt").decode().splitlines()
        self.classes = [labels.index(name) for name in class_names]
        self.input = self.model.get_input_details()[0]["index"]
        self.output = self.model.get_output_details()[0]["index"]

    def heard(self, pcm: np.ndarray) -> tuple[list[float], int]:
        """Seconds into pcm where the sound is heard, and how many samples were used up: the
        last partial window is left for the next call, so a stream can be checked piece by piece."""
        found, start = [], 0
        while start + WINDOW <= len(pcm):
            self.model.set_tensor(self.input, pcm[start:start + WINDOW])
            self.model.invoke()
            if self.model.get_tensor(self.output)[0][self.classes].max() >= SOUND_SCORE:
                found.append(start / SAMPLE_RATE)
            start += HOP
        return found, start


class PersonDetector:
    def __init__(self) -> None:
        self.model = interpreter(EFFICIENTDET)
        details = self.model.get_input_details()[0]
        self.input = details["index"]
        self.size = (int(details["shape"][2]), int(details["shape"][1]))
        # The outputs' names don't say their role; in this model :0 is the count, :1 scores, :2 classes, :3 boxes.
        outputs = {o["name"]: o["index"] for o in self.model.get_output_details()}
        self.scores, self.classes = outputs["StatefulPartitionedCall:1"], outputs["StatefulPartitionedCall:2"]

    def seen(self, image: Image.Image) -> bool:
        pixels = np.asarray(image.convert("RGB").resize(self.size), np.uint8)[None]
        self.model.set_tensor(self.input, pixels)
        self.model.invoke()
        classes = self.model.get_tensor(self.classes)[0]
        scores = self.model.get_tensor(self.scores)[0]
        return bool(((classes == PERSON_CLASS) & (scores >= PERSON_SCORE)).any())
