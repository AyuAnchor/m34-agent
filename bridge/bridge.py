#!/usr/bin/env python3
"""Telegram bridge: the owner's messages become tasks for the phone agent."""
import base64
import html
import io
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps

from agent import Agent, Router
from commands import QuickCommands, format_telegram, plain
from device import Device, adb, getprop, is_temporary, ready_to_send
from imagegen import ImageMaker
from net import ConnectionPool, multipart
from speech import Transcriber, Voice
from store import PACIFIC, Memory, Schedules, Shortcuts, Usage, read_json, write_atomically
from updater import current_version, self_update
HOME = Path.home()
REPO_DIR = Path(__file__).resolve().parent.parent  # the git checkout the bot runs from
CONFIG_PATH = HOME / ".config/tg-bridge/config.json"
STATE_DIR = HOME / ".cache/tg-bridge"
HISTORY_PATH = STATE_DIR / "history.json"
MAX_HISTORY = 20
MAX_MESSAGE = 4000
APPROVAL_TIMEOUT_S = 15 * 60
PROGRESS_LINES = 8
OUTAGE_NOTICE_S = 10 * 60  # tell the owner once when the main models have been failing this long
PROGRESS_EDIT_S = 2.0
DRAFT_EVERY_S = 0.6  # min gap between draft redraws; each is one API call
SCHEDULER_TICK_S = 20
WARM_EVERY_S = 120  # keep a fresh connection to Telegram and each AI provider ready
ADB_GRACE_S = 6 * 60  # how long after a start to wait for adb before telling the owner
WIRELESS_GUARD_S = 60  # how often to check that wireless debugging is on and trusted
BATTERY_CHECK_S = 120
BATTERY_LOW = 15  # alert below this %
BATTERY_REARM = 20  # ...and alert again only after it has been back at or above this %
BATTERY_ALERTED_PATH = STATE_DIR / "battery_alerted"
OWNER_SCREEN_PATH = Path("/tmp/owner-screen.jpg")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # Telegram bot upload limit
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # bots can't download bigger files from Telegram
RECEIVED_DIR = STATE_DIR / "received"  # files from the owner, on their way to the phone
ATTACHMENTS = ("voice", "photo", "video", "video_note", "audio", "document")  # "photo" is a list of sizes
VIEWABLE_IMAGES = ("image/jpeg", "image/png", "image/webp")  # sent with a request, the AI sees these too
MAX_VIEWED_BYTES = 4 * 1024 * 1024
VIEWED_PX = 1280  # longest side of the AI's copy of a photo; it's re-sent with every step
FILE_NAMES = {"photo": "photo_{:%Y%m%d_%H%M%S}.jpg", "video": "video_{:%Y%m%d_%H%M%S}.mp4",
              "video_note": "video_{:%Y%m%d_%H%M%S}.mp4", "audio": "audio_{:%Y%m%d_%H%M%S}.mp3",
              "document": "file_{:%Y%m%d_%H%M%S}"}
UPLOAD_KINDS = {  # file suffix -> (Bot API method, form field)
    **dict.fromkeys((".jpg", ".jpeg", ".png", ".webp"), ("sendPhoto", "photo")),
    **dict.fromkeys((".mp4", ".mov", ".3gp", ".mkv"), ("sendVideo", "video")),
    **dict.fromkeys((".m4a", ".mp3", ".aac", ".wav", ".ogg"), ("sendAudio", "audio")),
    ".opus": ("sendVoice", "voice"),  # Ogg Opus shows as a voice message
}
BUILT_IN_COMMANDS = [  # (command, description) shown in /help and Telegram's command menu
    ("status", "What I'm doing right now"),
    ("screen", "Screenshot of the phone, right now"),
    ("memory", "What I remember (/forget N deletes note N)"),
    ("schedules", "Scheduled tasks (/unschedule N cancels one)"),
    ("shortcuts", "Saved shortcuts (/delshortcut N deletes one)"),
    ("usage", "AI requests today vs the free daily limits"),
    ("model", "Choose the AI model for tasks, or Auto"),
    ("new", "Forget the conversation and start fresh"),
    ("stop", "Stop the current task and clear the queue"),
    ("update", "Pull the latest code from GitHub and restart"),
]

STATE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(STATE_DIR / "bridge.log", maxBytes=1_000_000, backupCount=2),
    ],
)
log = logging.getLogger("bridge")


def load_json(path: Path, default: Any) -> Any:
    return read_json(path, default)


def save_json(path: Path, data: Any) -> None:
    write_atomically(path, json.dumps(data, indent=2))


def viewable(data: bytes) -> str:
    """A photo as a data URL for the AI, at most VIEWED_PX on its longest side and turned upright."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    image.thumbnail((VIEWED_PX, VIEWED_PX))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    return f"data:image/jpeg;base64,{base64.b64encode(buffer.getvalue()).decode()}"


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


class Bridge:
    def __init__(self) -> None:
        self.config = load_json(CONFIG_PATH, {})
        self.tg = Telegram(self.config["token"])
        self.owner: int | None = self.config.get("owner_id")
        self.pair_code = None if self.owner else secrets.token_hex(3)
        # (task, when the owner sent it, photos to show the AI as data URLs)
        self.tasks: queue.Queue[tuple[str, float, list[str]]] = queue.Queue()
        self.received: list[str] = []  # notes on files the owner sent without a request, for the next task
        providers = self.config.get("providers", {})
        self.transcribe = Transcriber(providers["groq"]) if providers.get("groq") else None
        self.busy = False
        self.offline_since = 0.0  # when Telegram stopped answering; 0 while online
        self.cancel = threading.Event()
        self.approvals: dict[str, tuple[threading.Event, list[bool]]] = {}
        self.task = ""
        self.started = 0.0
        self.actions: list[str] = []
        self.model = ""
        self.outage_noticed = 0.0  # start of the model outage the owner was last told about
        self.progress_id: int | None = None  # set and cleared only by the progress sender thread
        self.last_edit = 0.0
        self.progress_jobs: queue.Queue[Callable[[], None]] = queue.Queue()
        self.draft: Draft | None = None  # the reply being written, during a task
        self.device = Device()
        self.quick = QuickCommands(self.device)
        self.device_lock = threading.Lock()  # one user of the screen at a time: a task or a quick command
        send_file = lambda path, caption: self.tg.send_file(self.owner, path, caption)
        self.memory = Memory()
        self.usage = Usage()
        self.schedules = Schedules(getprop("persist.sys.timezone") or "UTC")
        self.shortcuts = Shortcuts()
        router = Router(providers, usage=self.usage)
        gemini_keys = [key for name, key in providers.items() if name.split("#")[0] == "gemini" and key]
        self.agent = Agent(
            router,
            self.device,
            self.memory,
            self.schedules,
            self.shortcuts,
            self.device_lock,
            ask_owner=self.ask_owner,
            send_file=send_file,
            notify=self.notify,
            on_step=self.show_typing,
            on_action=self.on_action,
            on_text=self.show_draft,
            cancelled=self.cancel.is_set,
            voice=Voice(gemini_keys, router.pools.get("gemini")) if gemini_keys else None,
            make_image=ImageMaker(providers["nvidia"]) if providers.get("nvidia") else None,
        )

    def send_reply(self, text: str) -> None:
        """Send an AI reply as Telegram HTML, or as plain text if Telegram rejects the formatting."""
        try:
            self.tg.send(self.owner, format_telegram(text), parse_mode="HTML")
        except Exception as error:
            log.warning(f"formatted reply rejected, sending plain: {error}")
            self.tg.send(self.owner, text)

    def notify(self, text: str) -> bool:
        """Best-effort message to the owner; returns whether it was delivered. Never raises."""
        if not self.owner:
            return False
        try:
            self.tg.send(self.owner, text)
            return True
        except Exception as error:
            log.warning(f"could not send notification: {error}")
            return False

    def show_typing(self) -> None:
        """Telegram's "typing..." indicator, sent in the background so it never delays a step."""

        def send() -> None:
            try:
                self.tg.call("sendChatAction", chat_id=self.owner, action="typing")
            except Exception as error:
                log.debug(f"typing indicator skipped: {error}")

        threading.Thread(target=send, daemon=True).start()

    def warm_screen_reader(self) -> None:
        """Start the phone-side uiautomator2 server now, so the first task doesn't wait for it."""
        try:
            self.device.read_elements()
        except Exception as error:  # adb may not be back yet after a reboot; the first task will retry
            log.warning(f"screen reader warm-up failed: {error}")

    def keep_warm(self) -> None:
        while True:
            transcriber = [self.transcribe.pool] if self.transcribe else []
            for pool in (self.tg.pool, *self.agent.router.pools.values(), *transcriber):
                pool.refresh()
            time.sleep(WARM_EVERY_S)

    def help_text(self) -> str:
        quick = "\n".join(f"{c.usage} - {c.description}" for c in self.quick.table.values())
        built_in = "\n".join(f"/{name} - {description}" for name, description in BUILT_IN_COMMANDS)
        return (
            "Send me a task in plain words and I'll do it on the phone.\n\n"
            f"Instant commands (no AI):\n{quick}\n\nControl:\n{built_in}"
        )

    def register_commands(self) -> None:
        """Fill Telegram's "/" menu."""
        quick = [(name, c.description) for name, c in self.quick.table.items()]
        commands = [{"command": name, "description": text} for name, text in BUILT_IN_COMMANDS + quick]
        self.tg.call("setMyCommands", commands=commands)

    def run_quick(self, name: str, args: str) -> None:
        command = self.quick.table[name]

        def run() -> None:
            if command.changes_device and not self.device_lock.acquire(blocking=False):
                self.tg.send(self.owner, "Busy: a task is using the phone. Wait, or /stop it.")
                return
            start = time.time()
            try:
                if command.changes_device and name != "screenoff":
                    self.device.wake()
                reply = command.run(args)
                if reply.file:
                    sendable = ready_to_send(reply.file)
                    try:
                        self.tg.send_file(self.owner, sendable, "")
                    finally:
                        for temporary in {reply.file, sendable}:
                            if is_temporary(temporary):
                                temporary.unlink(missing_ok=True)
                if reply.text:
                    self.tg.send(self.owner, plain(reply.text))
                log.info(f"/{name} done in {time.time() - start:.1f}s")
            except Exception as error:
                log.exception(f"/{name} failed")
                self.notify(f"/{name} failed: {error}")
            finally:
                if command.changes_device:
                    self.device_lock.release()

        threading.Thread(target=run, daemon=True).start()

    def poll(self) -> None:
        offset = 0
        while True:
            try:
                updates = self.tg.call(
                    "getUpdates",
                    http_timeout=70,
                    offset=offset,
                    timeout=50,
                    allowed_updates=["message", "callback_query"],
                )
            except Exception as error:  # the phone's network drops; retry, logging once per outage
                if not self.offline_since:
                    self.offline_since = time.time()
                    log.warning(f"poll failed: {error}")
                time.sleep(5)
                continue
            if self.offline_since:
                log.info(f"back online after {(time.time() - self.offline_since) / 60:.1f} min")
                self.offline_since = 0.0
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle(update)
                except Exception:
                    log.exception("failed to handle update")

    def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        text = (message.get("text") or message.get("caption") or "").strip()
        kind = next((kind for kind in ATTACHMENTS if message.get(kind)), None)
        user_id = message.get("from", {}).get("id")
        if not (text or kind) or user_id is None or message.get("chat", {}).get("type") != "private":
            return
        if self.owner is None:
            self.try_pair(user_id, text)
        elif user_id != self.owner:
            log.warning(f"ignored message from user {user_id}")
        elif kind:
            threading.Thread(target=self.receive, args=(message, kind, text), daemon=True).start()
        elif text in ("/start", "/help"):
            self.tg.send(self.owner, self.help_text())
        elif text == "/status":
            start = time.time()
            self.tg.send(self.owner, self.status())
            log.info(f"/status answered in {time.time() - start:.1f}s")
        elif text == "/screen":
            self.send_screen()
        elif text == "/memory":
            self.tg.send(self.owner, self.memory.listing() or "I don't remember anything yet.")
        elif text == "/schedules":
            self.tg.send(self.owner, self.schedules.listing() or "No scheduled tasks.")
        elif text == "/usage":
            self.tg.send(self.owner, self.usage_report(), parse_mode="HTML")
        elif text == "/model":
            self.tg.send(self.owner, self.model_choice(), reply_markup={"inline_keyboard": self.model_buttons()})
        elif text == "/shortcuts":
            self.tg.send(self.owner, self.shortcuts.listing() or "No shortcuts saved yet.")
        elif text.split()[0] in ("/forget", "/unschedule", "/delshortcut"):
            command, _, number = text.partition(" ")
            store = {"/forget": self.memory, "/unschedule": self.schedules, "/delshortcut": self.shortcuts}[command]
            removed = number.strip().isdigit() and store.remove(int(number))
            self.tg.send(self.owner, "Deleted." if removed else "No such number. Check /memory, /schedules or /shortcuts.")
        elif text == "/new":
            HISTORY_PATH.unlink(missing_ok=True)
            self.tg.send(self.owner, "Conversation cleared.")
        elif text == "/stop":
            self.stop()
        elif text == "/update":
            self.update()
        elif text.startswith("/") and (name := text[1:].split()[0].split("@")[0].lower()) in self.quick.table:
            self.run_quick(name, text.partition(" ")[2].strip())
        elif text.startswith("/"):  # a typo'd command shouldn't become an AI task
            self.tg.send(self.owner, "Unknown command. Send /help for the list.")
        else:
            self.queue_task(text, message.get("date") or time.time())

    def queue_task(self, text: str, sent_at: float, images: list[str] | None = None) -> None:
        if self.busy or not self.tasks.empty():
            self.tg.send(self.owner, "Still working on the previous task. Queued this one.")
        self.tasks.put((text, sent_at, images or []))

    def receive(self, message: dict[str, Any], kind: str, caption: str) -> None:
        """A voice note becomes a task; any other file goes to the phone, and its caption (if any) becomes
        a task about it. Slow: run on its own thread."""
        sent_at = message.get("date") or time.time()
        file = message[kind][-1] if kind == "photo" else message[kind]  # the largest photo size comes last
        try:
            if file.get("file_size", 0) > MAX_DOWNLOAD_BYTES:
                self.tg.send(self.owner, "That's over 20 MB, the most a Telegram bot can download.")
                return
            if kind == "voice":
                if self.transcribe is None:
                    self.tg.send(self.owner, "Voice notes need a Groq API key (providers.groq in the config).")
                    return
                start = time.time()
                audio = self.tg.download(file["file_id"])
                downloaded = time.time()
                heard = self.transcribe(audio, "voice.ogg")
                log.info(f"voice note ({file.get('duration', '?')}s) downloaded in {downloaded - start:.1f}s, "
                         f"transcribed in {time.time() - downloaded:.1f}s")
                if not heard:
                    self.tg.send(self.owner, "I couldn't make out any words in that voice note.")
                    return
                self.tg.send(self.owner, f"Heard: {heard}")
                self.queue_task(heard, sent_at)
                return
            data = self.tg.download(file["file_id"])
            name = file.get("file_name") or FILE_NAMES[kind].format(datetime.fromtimestamp(sent_at, self.schedules.tz))
            name = re.sub(r"[^\w.\- ]+", "_", Path(name).name).strip() or "file"
            # Album photos arrive together, often with the same name: one folder each.
            staging = RECEIVED_DIR / secrets.token_hex(4)
            staging.mkdir(parents=True)
            local = staging / name
            local.write_bytes(data)
            try:
                path = self.device.receive(local)
            finally:
                local.unlink(missing_ok=True)
                staging.rmdir()
            note = f"(The owner sent a {kind.replace('_', ' ')}, saved on the phone at {path}.)"
            log.info(f"received a {kind} ({len(data) // 1024} KB) -> {path}")
            if caption:
                mime = "image/jpeg" if kind == "photo" else file.get("mime_type", "")
                seen = mime in VIEWABLE_IMAGES and len(data) <= MAX_VIEWED_BYTES
                images = [viewable(data)] if seen else []
                self.queue_task(f"{caption}\n\n{note}", sent_at, images)
            else:
                self.received.append(note)
                self.tg.send(self.owner, f"Saved on the phone: {path}")
        except Exception as error:
            log.exception(f"couldn't receive a {kind}")
            self.notify(f"Couldn't receive that {kind.replace('_', ' ')}: {error}")

    def try_pair(self, user_id: int, text: str) -> None:
        if text != f"/pair {self.pair_code}":
            return
        self.owner = user_id
        self.config["owner_id"] = user_id
        save_json(CONFIG_PATH, self.config)
        self.pair_code = None
        (STATE_DIR / "pair_code").unlink(missing_ok=True)
        log.info(f"paired with user {user_id}")
        self.tg.send(user_id, f"Paired. Only you can control this phone now.\n\n{self.help_text()}")

    def elapsed(self) -> str:
        seconds = int(time.time() - self.started)
        return f"{seconds // 60}m {seconds % 60}s"

    def status(self) -> str:
        queued = self.tasks.qsize()
        if not self.busy:
            return f"Idle. {queued} queued." if queued else "Idle."
        last = self.actions[-1] if self.actions else "thinking"
        waiting = "\nWaiting for your approval." if self.approvals else ""
        return (
            f"Working on: {self.task[:300]}\n"
            f"Running {self.elapsed()}, {len(self.actions)} steps, model {self.model or 'starting'}{self.fallback_note()}.\n"
            f"Last action: {last[:200]}{waiting}\n"
            f"Queued: {queued}"
        )

    def send_screen(self) -> None:
        def capture() -> None:
            start = time.time()
            try:
                self.device.snapshot(OWNER_SCREEN_PATH)
                self.tg.send_file(self.owner, OWNER_SCREEN_PATH, "Current screen")
                log.info(f"/screen sent in {time.time() - start:.1f}s")
            except Exception as error:
                self.notify(f"Screenshot failed: {error}")

        threading.Thread(target=capture, daemon=True).start()

    def send_progress(self) -> None:
        """Run progress posts and edits in order on one thread, so the agent never waits on Telegram."""
        while True:
            job = self.progress_jobs.get()
            try:
                job()
            except Exception as error:  # unchanged text or a network blip; the next edit catches up
                log.debug(f"progress update skipped: {error}")

    def post_progress(self) -> None:
        """Queue posting the live progress message (on a task's first action; plain chat gets none)."""
        text = f"Working: {self.task[:200]}"

        def post() -> None:
            self.progress_id = self.tg.call("sendMessage", chat_id=self.owner, text=text)["message_id"]

        self.progress_jobs.put(post)

    def show_draft(self, text: str) -> None:
        if self.draft:
            self.draft.update(text)

    def on_action(self, model: str, action: str) -> None:
        self.show_draft("")  # any text shown so far was a remark before this action, not the reply
        self.model = model
        self.actions.append(action)
        if len(self.actions) == 1:
            self.post_progress()
        if time.time() - self.last_edit >= PROGRESS_EDIT_S:
            self.update_progress("Working")
        self.report_long_outage()

    def report_long_outage(self) -> None:
        """Tell the owner once per outage when the main models have been failing for OUTAGE_NOTICE_S."""
        router = self.agent.router
        since = router.outage_since()
        if since is None or since == self.outage_noticed or time.time() - since < OUTAGE_NOTICE_S:
            return
        self.outage_noticed = since
        self.notify(
            f"The main AI models have had problems for {int(time.time() - since) // 60} min "
            f"({router.note or router.last_failure}). Carrying on with {self.model}, which may be less reliable."
        )

    def fallback_note(self) -> str:
        router = self.agent.router
        if not router.note:
            return ""
        since = datetime.fromtimestamp(router.note_since, self.schedules.tz).strftime("%H:%M")
        return f" ({router.note} since {since})"

    def update_progress(self, headline: str) -> None:
        if not self.actions:  # plain chat: no progress message was posted
            return
        steps = self.actions[-PROGRESS_LINES:]
        first = len(self.actions) - len(steps) + 1
        lines = "\n".join(f"{first + i}. {step[:120]}" for i, step in enumerate(steps))
        text = (
            f"{headline}: {self.task[:200]}\n"
            f"{self.elapsed()} · {len(self.actions)} steps · {self.model or 'starting'}{self.fallback_note()}\n\n{lines}"
        )
        self.last_edit = time.time()

        def edit() -> None:
            if self.progress_id is not None:
                self.tg.call("editMessageText", chat_id=self.owner, message_id=self.progress_id, text=text.strip())

        self.progress_jobs.put(edit)

    def update(self) -> None:
        def run() -> None:
            if self.busy:
                self.tg.send(self.owner, "Busy with a task. Send /update again when it's done.")
                return
            try:
                result = self_update(REPO_DIR)
            except Exception as error:
                log.exception("update failed")
                self.notify(f"Update failed: {error}")
                return
            log.info(result.message.splitlines()[0])
            try:
                self.tg.send(self.owner, result.message)
            except Exception:  # the new code is already in place, so restart into it regardless
                log.exception("could not send the update reply")
            if result.restart:
                log.info("exiting to restart into the updated code")
                os._exit(0)  # runit restarts the service

        threading.Thread(target=run, daemon=True).start()

    def stop(self) -> None:
        while not self.tasks.empty():
            self.tasks.get_nowait()
        if self.busy:
            self.cancel.set()
            for event, _ in list(self.approvals.values()):
                event.set()
        self.tg.send(self.owner, "Stopping.")

    def ask_owner(self, action: str) -> bool:
        req_id = secrets.token_hex(8)
        event, verdict = threading.Event(), [False]
        self.approvals[req_id] = (event, verdict)
        buttons = [[
            {"text": "Approve", "callback_data": f"ok:{req_id}"},
            {"text": "Deny", "callback_data": f"no:{req_id}"},
        ]]
        try:
            self.tg.send(self.owner, f"Allow this?\n{action[:1500]}", reply_markup={"inline_keyboard": buttons})
            if not event.wait(APPROVAL_TIMEOUT_S):
                self.tg.send(self.owner, "No answer in 15 minutes, so I skipped it.")
            return verdict[0]
        finally:
            self.approvals.pop(req_id, None)

    def model_choice(self) -> str:
        router = self.agent.router
        if not router.chosen:
            return ("AI model for tasks: Auto. The quick models take the first step, then the best available "
                    "model, with backups when one is slow or out of quota.")
        title = next(m.title for m in router.models if m.name == router.chosen)
        return f"AI model for tasks: {title}. When it's out of quota or failing, the others take over as in Auto."

    def model_buttons(self) -> list[list[dict[str, str]]]:
        router, now = self.agent.router, time.time()
        rows = [[{"text": f"{'✓ ' if not router.chosen else ''}Auto", "callback_data": "model:"}]]
        for model in router.choices():
            keys = [m for m in router.models if m.name == model.name]
            state = ""
            if all(m.cooldown_until > now for m in keys):
                state = " (out for today)" if all(m.out_of_quota for m in keys) else " (paused)"
            mark = "✓ " if model.name == router.chosen else ""
            rows.append([{"text": f"{mark}{model.title}{state}", "callback_data": f"model:{model.name}"}])
        return rows

    def handle_callback(self, query: dict[str, Any]) -> None:
        if query.get("from", {}).get("id") != self.owner:
            return
        if (query.get("data") or "").startswith("model:"):
            self.agent.router.choose(query["data"].removeprefix("model:"))
            log.info(f"model chosen: {self.agent.router.chosen or 'auto'}")
            self.tg.call("answerCallbackQuery", callback_query_id=query["id"], text="Saved")
            if message := query.get("message"):
                self.tg.call("editMessageText", chat_id=message["chat"]["id"], message_id=message["message_id"],
                             text=self.model_choice())
            return
        choice, _, req_id = (query.get("data") or "").partition(":")
        pending = self.approvals.get(req_id)
        if pending and choice in ("ok", "no") and not pending[0].is_set():
            pending[1][0] = choice == "ok"
            pending[0].set()
            label = "Approved" if choice == "ok" else "Denied"
        else:
            label = "Expired"
        self.tg.call("answerCallbackQuery", callback_query_id=query["id"], text=label)
        message = query.get("message")
        if message:
            self.tg.call(
                "editMessageText",
                chat_id=message["chat"]["id"],
                message_id=message["message_id"],
                text=f"{message.get('text', '')}\n\n{label}",
            )

    def work(self) -> None:
        while True:
            task, sent_at, images = self.tasks.get()
            self.busy = True
            self.cancel.clear()
            self.task, self.started, self.actions, self.model = task, time.time(), [], ""
            log.info(f"task: {task[:200]} (reached the bot ~{self.started - sent_at:.0f}s after sending)")
            self.draft = Draft(self.tg, self.owner)
            try:
                history = load_json(HISTORY_PATH, [])
                notes, self.received = self.received, []
                prompt = "\n".join([*notes, task])
                reply = self.agent.run(history, prompt, images)
                self.draft.close()
                history += [{"role": "user", "content": prompt}, {"role": "assistant", "content": reply}]
                save_json(HISTORY_PATH, history[-MAX_HISTORY:])
                self.send_reply(reply)
                log.info(f"replied {time.time() - self.started:.1f}s after starting, {len(self.actions)} actions")
            except Exception as error:
                log.exception("task failed")
                self.notify(f"Task failed: {error}")
            finally:
                self.draft.close()
                self.draft = None
                self.update_progress("Stopped" if self.cancel.is_set() else "Finished")
                self.progress_jobs.put(lambda: setattr(self, "progress_id", None))  # after the final edit
                self.busy = False

    def usage_report(self) -> str:
        """A monospace table: requests used against each model's daily limit, and whether it can be used now."""
        now = time.time()
        tz = self.schedules.tz
        reset = (datetime.now(PACIFIC) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        counts = self.usage.today()
        models = self.agent.router.models
        several_keys = {model.family for model in models if "#" in model.provider}
        rows = [("Model", "Used", "Now")]
        for model in models:
            name = model.short
            if model.family in several_keys:
                name += f" #{model.provider.partition('#')[2] or 1}"
            if model.reported:
                limit, left = model.reported
                used = f"{limit - left}/{limit}"
            else:
                count = counts.get(model.label, [0, 0])[0]
                used = f"{count}/{model.daily}" if model.daily else str(count)
            if model.cooldown_until <= now:
                state = "ready"
            else:
                until = datetime.fromtimestamp(model.cooldown_until, tz).strftime("%H:%M")
                state = f"full, {until}" if model.out_of_quota else f"wait {until}"
            rows.append((name, used, state))
        widths = [max(len(row[column]) for row in rows) for column in range(2)]
        table = "\n".join(f"{name:<{widths[0]}}  {used:>{widths[1]}}  {state}" for name, used, state in rows)
        notes = [
            f"Gemini: counted by this bot since its daily reset at {reset.astimezone(tz):%H:%M}.",
            "Groq: as Groq reports it, over the last 24 hours.",
            "full = daily quota used up; the time is when it is tried again.",
        ]
        return f"<b>AI usage</b>\n<pre>{html.escape(table)}</pre>\n" + "\n".join(notes)

    def monitor_battery(self) -> None:
        """Alert the owner once below BATTERY_LOW and once on recovery. The alerted flag is kept on disk
        so a restart doesn't repeat it."""
        alerted = BATTERY_ALERTED_PATH.exists()
        while True:
            time.sleep(BATTERY_CHECK_S)
            try:
                status = self.device.battery_status()
            except Exception:
                log.exception("battery check failed")
                continue
            if status is None or self.owner is None or self.offline_since:
                continue
            level, charging = status
            # Marked sent only once delivered, so an undelivered alert is retried.
            if level < BATTERY_LOW and not alerted:
                state = "charging" if charging else "not charging; is the charger unplugged or the power off?"
                alerted = self.notify(f"Battery low: {level}% ({state}). I'll stop working when it runs out.")
                log.info(f"low battery alert at {level}% ({'sent' if alerted else 'not delivered, will retry'})")
                if alerted:
                    BATTERY_ALERTED_PATH.touch()
            elif level >= BATTERY_REARM and alerted:
                alerted = not self.notify(f"Battery back up to {level}%.")
                if not alerted:
                    BATTERY_ALERTED_PATH.unlink(missing_ok=True)

    def check_phone_control(self) -> None:
        """After a start (usually a reboot), tell the owner if screen control doesn't come back."""
        deadline = time.time() + ADB_GRACE_S
        while time.time() < deadline:
            if adb("get-state").stdout.decode().strip() == "device":
                return
            time.sleep(15)
        if self.owner:
            self.notify(
                "I restarted but can't control the screen yet (adb didn't come back). Chat still works. "
                "Most likely the phone is on a Wi-Fi network it hasn't used before, where Android asks "
                "\"Allow wireless debugging on this network?\". Fix: plug the phone into your computer and run "
                "`adb tcpip 5555`, then allow wireless debugging once on that network."
            )

    def guard_wireless_debugging(self) -> None:
        """Keep Wi-Fi on, and wireless debugging on and trusted on the current network, so a reboot can
        recover unattended (Android asks per network)."""
        while True:
            time.sleep(WIRELESS_GUARD_S)
            try:  # needs no screen, so it doesn't wait for the device lock
                if note := self.device.ensure_wifi():
                    log.warning(note)
                    self.notify(note)
            except Exception:
                log.exception("Wi-Fi guard failed")
            if not self.device_lock.acquire(blocking=False):
                continue
            try:
                if note := self.device.ensure_wireless_debugging():
                    log.info(note)
                    self.notify(note)
            except Exception:
                log.exception("wireless debugging guard failed")
            finally:
                self.device_lock.release()

    def run_schedules(self) -> None:
        while True:
            time.sleep(SCHEDULER_TICK_S)
            if self.owner is None:
                continue
            try:
                for item in self.schedules.pop_due():
                    log.info(f"scheduled task #{item['id']} is due")
                    self.tasks.put((f"[Scheduled #{item['id']}] {item['task']}", time.time(), []))
            except Exception:
                log.exception("scheduler failed")


def log_thread_error(args: threading.ExceptHookArgs) -> None:
    """Log uncaught background-thread errors; runit discards stderr."""
    name = args.thread.name if args.thread else "?"
    log.error(f"thread {name} crashed", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def main() -> None:
    threading.excepthook = log_thread_error
    try:
        log.info(f"starting, code version {current_version(REPO_DIR)}")
    except Exception:
        log.info("starting (not running from a git checkout)")
    bridge = Bridge()
    if bridge.pair_code:
        (STATE_DIR / "pair_code").write_text(bridge.pair_code)
        log.info(f"not paired yet: send '/pair {bridge.pair_code}' to the bot")
    try:
        bridge.register_commands()
    except Exception as error:  # cosmetic; the bot works without the menu
        log.warning(f"could not register the command menu: {error}")
    for target in (
        bridge.work, bridge.run_schedules, bridge.keep_warm, bridge.check_phone_control,
        bridge.guard_wireless_debugging, bridge.monitor_battery, bridge.send_progress, bridge.warm_screen_reader,
    ):
        threading.Thread(target=target, daemon=True).start()
    bridge.poll()


if __name__ == "__main__":
    main()
