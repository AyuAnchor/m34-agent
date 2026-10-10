#!/usr/bin/env python3
"""Telegram bridge: the owner's messages become tasks for the phone agent."""
import html
import json
import logging
import logging.handlers
import os
import queue
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from agent import Agent, Router
from commands import QuickCommands, format_telegram, parse_duration, plain
from device import Device, adb, getprop, is_temporary, ready_to_send
from net import ConnectionPool
from store import PACIFIC, Memory, Schedules, Shortcuts, Usage, read_json, write_atomically
from updater import current_version, self_update
from watch import SOUNDS, MotionWatch, SoundWatch

HOME = Path.home()
REPO_DIR = Path(__file__).resolve().parent.parent  # the git checkout the bot runs from
CONFIG_PATH = HOME / ".config/tg-bridge/config.json"
STATE_DIR = HOME / ".cache/tg-bridge"
HISTORY_PATH = STATE_DIR / "history.json"
MAX_HISTORY = 20
MAX_MESSAGE = 4000
APPROVAL_TIMEOUT_S = 15 * 60
PROGRESS_LINES = 8
OFFLINE_REPORT_S = 10 * 60  # while Telegram is unreachable, log a reminder this often, not every retry
OUTAGE_NOTICE_S = 10 * 60  # tell the owner once when the main models have been failing this long
PROGRESS_EDIT_S = 2.0
DRAFT_EVERY_S = 0.6  # how often a reply being written is redrawn; each redraw is one API call
SCHEDULER_TICK_S = 20
WARM_EVERY_S = 120  # keep a fresh connection to Telegram and each AI provider ready
ADB_GRACE_S = 6 * 60  # after a reboot, adb on 5555 normally returns within ~3 minutes
WIRELESS_GUARD_S = 60  # how often to check that wireless debugging is on and trusted
BATTERY_CHECK_S = 120
BATTERY_LOW = 15  # alert below this %
BATTERY_REARM = 20  # ...and alert again only after it has been back at or above this %
OWNER_SCREEN_PATH = Path("/tmp/owner-screen.jpg")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # Telegram bot upload limit
UPLOAD_KINDS = {  # file suffix -> (Bot API method, form field)
    **dict.fromkeys((".jpg", ".jpeg", ".png", ".webp"), ("sendPhoto", "photo")),
    **dict.fromkeys((".mp4", ".mov", ".3gp", ".mkv"), ("sendVideo", "video")),
    **dict.fromkeys((".m4a", ".mp3", ".aac", ".wav", ".ogg"), ("sendAudio", "audio")),
}
BUILT_IN_COMMANDS = [  # (command, description) shown in /help and Telegram's command menu
    ("status", "What I'm doing right now"),
    ("screen", "Screenshot of the phone, right now"),
    ("memory", "What I remember (/forget N deletes note N)"),
    ("schedules", "Scheduled tasks (/unschedule N cancels one)"),
    ("shortcuts", "Saved shortcuts (/delshortcut N deletes one)"),
    ("usage", "AI requests today vs the free daily limits"),
    ("watch", "Message me if anything moves: /watch 10m [front|back] [people]"),
    ("unwatch", "Stop the camera watch"),
    ("listen", f"Message me if I hear a sound: /listen 8h [{'|'.join(SOUNDS)}]"),
    ("unlisten", "Stop the sound watch"),
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


def split_message(text: str, limit: int) -> list[str]:
    """Split a message to fit Telegram's size cap, breaking at line boundaries and never inside a
    <pre> block (which would break the HTML). Over-long single blocks are hard-split as a last resort."""
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
        boundary = secrets.token_hex(16)

        def field(name: str, value: str) -> bytes:
            return f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()

        body = (
            field("chat_id", str(chat_id))
            + field("caption", caption[:1000])
            + f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; filename="{path.name}"\r\n'
              f"Content-Type: application/octet-stream\r\n\r\n".encode()
            + path.read_bytes()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        self.post(method, body, f"multipart/form-data; boundary={boundary}", 300)


class Draft:
    """A reply shown while the AI writes it (Telegram's sendMessageDraft). Its own thread sends only the
    newest text, so the model's stream never waits on Telegram."""

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
        """Called before the real reply goes out, so a late redraw can't land after it."""
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
        self.tasks: queue.Queue[tuple[str, float]] = queue.Queue()  # (task, when the owner sent it)
        self.busy = False
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
        self.device_lock = threading.Lock()  # one user of the screen at a time: a task, a quick command or a watch
        send_file = lambda path, caption: self.tg.send_file(self.owner, path, caption)
        self.watch = MotionWatch(self.device, self.device_lock, self.notify, send_file)
        self.listener = SoundWatch(self.device, self.notify, send_file)
        self.memory = Memory()
        self.usage = Usage()
        self.schedules = Schedules(getprop("persist.sys.timezone") or "UTC")
        self.shortcuts = Shortcuts()
        self.agent = Agent(
            Router(self.config.get("providers", {}), usage=self.usage),
            self.device,
            self.memory,
            self.schedules,
            self.shortcuts,
            self.watch,
            self.listener,
            self.device_lock,
            ask_owner=self.ask_owner,
            send_file=send_file,
            notify=self.notify,
            on_step=self.show_typing,
            on_action=self.on_action,
            on_text=self.show_draft,
            cancelled=self.cancel.is_set,
        )

    def send_reply(self, text: str) -> None:
        """Send an AI reply with Markdown rendered as Telegram HTML, falling back to plain text if
        Telegram rejects the formatting."""
        try:
            self.tg.send(self.owner, format_telegram(text), parse_mode="HTML")
        except Exception as error:
            log.warning(f"formatted reply rejected, sending plain: {error}")
            self.tg.send(self.owner, text)

    def notify(self, text: str) -> bool:
        """Best effort, returns whether it was delivered: background loops call this, and a failed send
        (e.g. while Wi-Fi reconnects) must not kill them."""
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
        """The first screen read starts the phone-side uiautomator2 server (~2 s). Pay that at startup
        instead of in the owner's first task."""
        try:
            self.device.read_elements()
        except Exception as error:  # adb may not be back yet after a reboot; the first task will retry
            log.warning(f"screen reader warm-up failed: {error}")

    def keep_warm(self) -> None:
        while True:
            for pool in (self.tg.pool, *self.agent.router.pools.values()):
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
                self.tg.send(self.owner, "Busy: a task or motion watch is using the phone. Wait, or /stop it.")
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
        offline_since = last_report = 0.0
        while True:
            try:
                updates = self.tg.call(
                    "getUpdates",
                    http_timeout=70,
                    offset=offset,
                    timeout=50,
                    allowed_updates=["message", "callback_query"],
                )
            except Exception as error:  # the phone's network drops; keep retrying, but log it sparingly
                now = time.time()
                if not offline_since:
                    offline_since = last_report = now
                    log.warning(f"poll failed: {error}")
                elif now - last_report >= OFFLINE_REPORT_S:
                    last_report = now
                    log.warning(f"still offline after {(now - offline_since) / 60:.0f} min: {error}")
                time.sleep(5)
                continue
            if offline_since:
                log.info(f"back online after {(time.time() - offline_since) / 60:.1f} min")
                offline_since = 0.0
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
        text = (message.get("text") or "").strip()
        user_id = message.get("from", {}).get("id")
        if not text or user_id is None or message.get("chat", {}).get("type") != "private":
            return
        if self.owner is None:
            self.try_pair(user_id, text)
        elif user_id != self.owner:
            log.warning(f"ignored message from user {user_id}")
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
        elif text.split()[0] == "/watch":
            self.tg.send(self.owner, self.start_watch(text.partition(" ")[2]))
        elif text == "/unwatch":
            self.tg.send(self.owner, self.watch.stop())
        elif text.split()[0] == "/listen":
            self.tg.send(self.owner, self.start_listening(text.partition(" ")[2]))
        elif text == "/unlisten":
            self.tg.send(self.owner, self.listener.stop())
        elif text == "/stop":
            self.stop()
        elif text == "/update":
            self.update()
        elif text.startswith("/") and (name := text[1:].split()[0].split("@")[0].lower()) in self.quick.table:
            self.run_quick(name, text.partition(" ")[2].strip())
        elif text.startswith("/"):  # a typo'd command shouldn't become an AI task
            self.tg.send(self.owner, "Unknown command. Send /help for the list.")
        else:
            if self.busy or not self.tasks.empty():
                self.tg.send(self.owner, "Still working on the previous task. Queued this one.")
            self.tasks.put((text, message.get("date") or time.time()))

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
        watching = "".join(f"\n{w.name.capitalize()}: {w.summary}." for w in (self.watch, self.listener) if w.active)
        if not self.busy:
            return (f"Idle. {queued} queued." if queued else "Idle.") + watching
        last = self.actions[-1] if self.actions else "thinking"
        waiting = "\nWaiting for your approval." if self.approvals else ""
        return (
            f"Working on: {self.task[:300]}\n"
            f"Running {self.elapsed()}, {len(self.actions)} steps, model {self.model or 'starting'}{self.fallback_note()}.\n"
            f"Last action: {last[:200]}{waiting}\n"
            f"Queued: {queued}{watching}"
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
        """Posts and edits the progress message in order on its own thread, so the agent never waits on
        Telegram between actions."""
        while True:
            job = self.progress_jobs.get()
            try:
                job()
            except Exception as error:  # unchanged text or a network blip; the next edit catches up
                log.debug(f"progress update skipped: {error}")

    def post_progress(self) -> None:
        """The live progress message appears with the first action, so plain chat gets its reply sooner."""
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
        """Model switches only show in the progress message, but a long outage is worth one message:
        the fallback models are weaker, so a long task may go worse than usual."""
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
            if self.busy or self.watch.active or self.listener.active:
                self.tg.send(self.owner, "Busy (a task or watch is running). Send /update again when idle.")
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
                os._exit(0)  # runit starts the service again, now running the new code

        threading.Thread(target=run, daemon=True).start()

    def start_watch(self, args: str) -> str:
        words = args.lower().split()
        duration = next((w for w in words if w not in ("front", "back", "rear", "people")), "5m")
        seconds = parse_duration(duration, 60)
        if not seconds:
            return "Usage: /watch 10m [front|back] [people]  (90s, 10m, 1h; a plain number means minutes)"
        return self.watch.start(seconds, front="front" in words, people="people" in words)

    def start_listening(self, args: str) -> str:
        words = args.lower().split()
        sound = next((w for w in words if w in SOUNDS), "cry")
        seconds = parse_duration(next((w for w in words if w not in SOUNDS), "1h"), 60)
        if not seconds:
            return f"Usage: /listen 8h [{'|'.join(SOUNDS)}]  (30m, 8h; a plain number means minutes)"
        return self.listener.start(seconds, sound)

    def stop(self) -> None:
        for watch in (self.watch, self.listener):
            if watch.active:
                watch.stop()
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

    def handle_callback(self, query: dict[str, Any]) -> None:
        if query.get("from", {}).get("id") != self.owner:
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
            task, sent_at = self.tasks.get()
            self.busy = True
            self.cancel.clear()
            self.task, self.started, self.actions, self.model = task, time.time(), [], ""
            log.info(f"task: {task[:200]} (reached the bot ~{self.started - sent_at:.0f}s after sending)")
            self.draft = Draft(self.tg, self.owner)
            try:
                history = load_json(HISTORY_PATH, [])
                reply = self.agent.run(history, task)
                self.draft.close()
                history += [{"role": "user", "content": task}, {"role": "assistant", "content": reply}]
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
                used = f"{counts.get(model.label, [0, 0])[0]}/{model.daily}"
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
        """Message the owner once when the battery drops below BATTERY_LOW, and when it has recovered."""
        alerted = False
        while True:
            time.sleep(BATTERY_CHECK_S)
            try:
                status = self.device.battery_status()
            except Exception:
                log.exception("battery check failed")
                continue
            if status is None or self.owner is None:
                continue
            level, charging = status
            # Only marked as sent once delivered, so an alert that hit a network drop is retried next check.
            if level < BATTERY_LOW and not alerted:
                state = "charging" if charging else "not charging; is the charger unplugged or the power off?"
                alerted = self.notify(f"Battery low: {level}% ({state}). I'll stop working when it runs out.")
                log.info(f"low battery alert at {level}% ({'sent' if alerted else 'not delivered, will retry'})")
            elif level >= BATTERY_REARM and alerted:
                alerted = not self.notify(f"Battery back up to {level}%.")

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
        """Keep Wi-Fi on, and wireless debugging on and trusted on whatever Wi-Fi the phone is on, so a
        reboot there can recover by itself (Android asks per network, and nobody can answer at boot)."""
        while True:
            time.sleep(WIRELESS_GUARD_S)
            try:  # needs no screen, so it doesn't wait for the device lock
                if note := self.device.ensure_wifi():
                    log.warning(note)
                    self.notify(note)
            except Exception:
                log.exception("Wi-Fi guard failed")
            if not self.device_lock.acquire(blocking=False):
                continue  # a task or command is using the screen; try again next round
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
                    self.tasks.put((f"[Scheduled #{item['id']}] {item['task']}", time.time()))
            except Exception:
                log.exception("scheduler failed")


def log_thread_error(args: threading.ExceptHookArgs) -> None:
    """An uncaught error in a background thread otherwise goes to stderr, which runit discards."""
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
