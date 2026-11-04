"""Telegram Bot API client, and the reply draft shown while the AI writes."""
import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Any

from net import ConnectionPool, multipart
log = logging.getLogger("telegram")

MAX_MESSAGE = 4000
DRAFT_EVERY_S = 0.6  # min gap between draft redraws; each is one API call
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # Telegram bot upload limit
UPLOAD_KINDS = {  # file suffix -> (Bot API method, form field)
    **dict.fromkeys((".jpg", ".jpeg", ".png", ".webp"), ("sendPhoto", "photo")),
    **dict.fromkeys((".mp4", ".mov", ".3gp", ".mkv"), ("sendVideo", "video")),
    **dict.fromkeys((".m4a", ".mp3", ".aac", ".wav", ".ogg"), ("sendAudio", "audio")),
    ".opus": ("sendVoice", "voice"),  # Ogg Opus shows as a voice message
}


def split_message(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most limit, at line breaks and never inside a <pre> block.
    A single block over the limit is hard-split."""
    if len(text) <= limit:
        return [text]
    blocks, buffer, inside = [], [], False
    for line in text.split("\n"):
        buffer.append(line)
        inside = (inside or "<pre>" in line) and "</pre>" not in line
        if not inside:
            blocks.append("\n".join(buffer))
            buffer = []
    if buffer:
        blocks.append("\n".join(buffer))
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks += [block[i:i + limit] for i in range(0, len(block), limit)]
        elif not current:
            current = block
        elif len(current) + 1 + len(block) <= limit:
            current += "\n" + block
        else:
            chunks.append(current)
            current = block
    if current:
        chunks.append(current)
    return chunks


class Telegram:
    """Bot API client over a shared pool of open connections."""

    def __init__(self, token: str) -> None:
        self.path = f"/bot{token}"
        self.pool = ConnectionPool("api.telegram.org")

    def post(self, method: str, body: bytes, content_type: str, http_timeout: float) -> Any:
        _, _, data = self.pool.request(f"{self.path}/{method}", body, {"Content-Type": content_type}, http_timeout)
        payload = json.loads(data)
        if not payload.get("ok"):
            raise RuntimeError(f"{method} failed: {payload.get('description')}")
        return payload["result"]

    def call(self, method: str, http_timeout: float = 30, **params: Any) -> Any:
        return self.post(method, json.dumps(params).encode(), "application/json", http_timeout)

    def send(self, chat_id: int, text: str, **extra: Any) -> None:
        for chunk in split_message(text or "(empty reply)", MAX_MESSAGE):
            self.call("sendMessage", chat_id=chat_id, text=chunk, **extra)

    def send_file(self, chat_id: int, path: Path, caption: str) -> None:
        """Upload a photo, video or any other file (as a document)."""
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError(f"{path.name} is over Telegram's 50 MB bot upload limit.")
        method, field_name = UPLOAD_KINDS.get(path.suffix.lower(), ("sendDocument", "document"))
        fields = {"chat_id": str(chat_id), "caption": caption[:1000]}
        body, content_type = multipart(fields, field_name, path.name, path.read_bytes())
        self.post(method, body, content_type, 300)

    def download(self, file_id: str) -> bytes:
        file_path = self.call("getFile", file_id=file_id)["file_path"]
        status, _, data = self.pool.request(f"/file{self.path}/{file_path}", None, {}, 300, method="GET")
        if status != 200:
            raise RuntimeError(f"download failed (HTTP {status})")
        return data


class Draft:
    """A reply shown while the AI writes it (sendMessageDraft). Its own thread sends only the newest text,
    so the model's stream never waits on Telegram."""

    def __init__(self, tg: Telegram, chat_id: int) -> None:
        self.tg, self.chat_id = tg, chat_id
        self.draft_id = secrets.randbelow(2**31 - 1) + 1
        self.text = self.shown = ""
        self.changed = threading.Event()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.send_latest, daemon=True)
        self.thread.start()

    def update(self, text: str) -> None:
        self.text = text[:MAX_MESSAGE]
        self.changed.set()

    def close(self) -> None:
        """Call before sending the real reply, so a late redraw can't land after it."""
        self.closed.set()
        self.changed.set()
        self.thread.join(timeout=10)

    def send_latest(self) -> None:
        while self.changed.wait() and not self.closed.is_set():
            self.changed.clear()
            text = self.text
            if text == self.shown:
                continue
            try:
                self.tg.call("sendMessageDraft", http_timeout=10, chat_id=self.chat_id, draft_id=self.draft_id, text=text)
                self.shown = text
            except Exception as error:
                log.debug(f"draft update skipped: {error}")
            if self.closed.wait(DRAFT_EVERY_S):
                return
