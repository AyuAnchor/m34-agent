"""Voice notes to text with Groq's Whisper, and text to voice messages with Gemini's text-to-speech."""
import base64
import json
import re
import subprocess
from pathlib import Path

from net import ConnectionPool, multipart

HOST = "api.groq.com"
PATH = "/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3"
TIMEOUT_S = 60

GEMINI_HOST = "generativelanguage.googleapis.com"
# Fastest first; both are on the free tier and speak Hindi.
VOICE_MODELS = ("gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts")
VOICE = "Kore"
SAMPLE_RATE = 24000  # streamed audio is raw 16-bit mono PCM at this rate
MAX_SPOKEN_CHARS = 3000  # ~3-4 minutes of speech
# Strips markdown but keeps link text and plain [brackets]: Gemini's speech models read those as vocal cues.
MARKDOWN = re.compile(r"\[([^\]]*)\]\([^)]*\)|[*_`#~]+")


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


class Voice:
    """Speech in Telegram's voice message format (Ogg Opus), encoded by ffmpeg while it streams in."""

    def __init__(self, keys: list[str], pool: ConnectionPool | None = None) -> None:
        self.keys = keys
        self.pool = pool or ConnectionPool(GEMINI_HOST)  # share the chat models' warm connection when given

    def __call__(self, text: str, style: str, out: Path) -> float:
        """Write `text` spoken to `out` (.opus) and return its length in seconds, trying each model and key in turn."""
        errors = []
        text = MARKDOWN.sub(lambda match: match[1] or "", text)[:MAX_SPOKEN_CHARS]
        for model in VOICE_MODELS:
            for number, key in enumerate(self.keys, 1):
                try:
                    return self.synthesize(model, key, text, style, out)
                except RuntimeError as error:
                    errors.append(f"{model} key {number}: {error}")
        raise RuntimeError("; ".join(errors) or "no Gemini key")

    def synthesize(self, model: str, key: str, text: str, style: str, out: Path) -> float:
        part: dict = {"text": text}
        if style:
            part["speech_metadata"] = {"style": style}
        body = json.dumps({
            "contents": [{"role": "user", "parts": [part]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {"voice": VOICE}}},
        }).encode()
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
        encoder = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
             "-c:a", "libopus", "-b:a", "32k", "-application", "voip", str(out)],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert encoder.stdin
        written = 0

        def on_line(line: bytes) -> None:
            nonlocal written
            if not line.startswith(b"data:"):
                return
            for candidate in json.loads(line[5:]).get("candidates", []):
                for piece in candidate.get("content", {}).get("parts", []):
                    if "inlineData" in piece:
                        audio = base64.b64decode(piece["inlineData"]["data"])
                        encoder.stdin.write(audio)
                        written += len(audio)

        try:
            status, _, data = self.pool.request(
                f"/v1beta/models/{model}:streamGenerateContent?alt=sse", body, headers, TIMEOUT_S, on_line
            )
        except Exception as error:
            encoder.kill()
            encoder.wait()
            raise RuntimeError(f"{type(error).__name__}: {error}") from error
        _, encoder_errors = encoder.communicate(timeout=30)
        if status != 200:
            raise RuntimeError(f"HTTP {status}: {data[:200].decode(errors='replace')}")
        if encoder.returncode or not written:
            raise RuntimeError(f"no audio ({encoder_errors.decode(errors='replace')[:200] or 'empty answer'})")
        return written / (2 * SAMPLE_RATE)
