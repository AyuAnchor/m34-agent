"""Voice notes to text with Groq's Whisper (free tier: 20 a minute, 2,000 a day, 8 hours of audio a day)."""
import json

from net import ConnectionPool, multipart

HOST = "api.groq.com"
PATH = "/openai/v1/audio/transcriptions"
# Same speed as the turbo model here (~0.3 s) and same limits, but it heard "सात बजे" as "7 बजे" where turbo
# heard "साथ बजे" ("with o'clock").
MODEL = "whisper-large-v3"
TIMEOUT_S = 60


class Transcriber:
    def __init__(self, key: str) -> None:
        self.key = key
        self.pool = ConnectionPool(HOST)

    def __call__(self, audio: bytes, filename: str) -> str:
        """The spoken words, in whatever language they were said."""
        body, content_type = multipart({"model": MODEL, "response_format": "json"}, "file", filename, audio)
        headers = {
            "Content-Type": content_type,
            "Authorization": f"Bearer {self.key}",
            "User-Agent": "m34-agent/1.0",  # Groq's CDN rejects Python's default agent
        }
        status, _, data = self.pool.request(PATH, body, headers, TIMEOUT_S)
        if status != 200:
            raise RuntimeError(f"transcription failed (HTTP {status}): {data[:200].decode(errors='replace')}")
        return json.loads(data)["text"].strip()
