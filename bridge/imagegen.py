"""Pictures from a description with FLUX.1 [dev] on NVIDIA's free API (the key the chat models use)."""
import base64
import json
import secrets
from pathlib import Path

from net import ConnectionPool

HOST = "ai.api.nvidia.com"
PATH = "/v1/genai/black-forest-labs/flux.1-dev"
STEPS = 28
TIMEOUT_S = 120
SHAPES = {"square": (1024, 1024), "portrait": (768, 1344), "landscape": (1344, 768)}  # sizes FLUX.1 [dev] takes


class ImageMaker:
    def __init__(self, key: str) -> None:
        self.key = key
        self.pool = ConnectionPool(HOST)

    def __call__(self, prompt: str, shape: str, out: Path) -> None:
        """Write a JPEG of `prompt` to `out`."""
        width, height = SHAPES.get(shape, SHAPES["square"])
        body = json.dumps({
            "prompt": prompt, "width": width, "height": height, "steps": STEPS, "seed": secrets.randbelow(2**31),
        }).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {self.key}"}
        status, _, data = self.pool.request(PATH, body, headers, TIMEOUT_S)
        if status != 200:
            raise RuntimeError(f"image generation failed (HTTP {status}): {data[:200].decode(errors='replace')}")
        artifact = (json.loads(data).get("artifacts") or [{}])[0]
        if artifact.get("finishReason") != "SUCCESS" or not artifact.get("base64"):
            raise RuntimeError(f"no image ({artifact.get('finishReason') or 'empty answer'}): the description may have been filtered")
        out.write_bytes(base64.b64decode(artifact["base64"]))
