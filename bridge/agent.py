"""The tool-using phone agent: runs a task step by step, checks the result, and replays saved routes."""
import json
import logging
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from device import GENERATED_DIR, MEDIA_DIR, SCREEN_PATH, Device, adb, is_temporary, ready_to_send
from models import Model, NoModelAvailable, Router
import web
from store import Memory, Schedules, Shortcuts
from tools import DONE_CHECK_PROMPT, MAX_WAIT_S, SYSTEM_PROMPT
log = logging.getLogger("agent")

WORKSPACE_DIR = "/home/agent/agent"
MAX_STEPS = 40
MAX_TOOL_OUTPUT = 6000
REPEAT_LIMIT = 3
LOOP_WINDOW = 6
SAME_SPOT = 25  # taps closer than about this on the 0-1000 scale count as repeats
# These take the phone (device lock) for the rest of the task; other tools never wait for a quick command.
SCREEN_TOOLS = {
    "look", "tap", "type_text", "scroll", "key", "open_app", "open_settings", "open_url", "take_photo", "record_video",
    "record_audio", "send_screenshot", "phone",
}
DEVICE_WAIT_S = 60  # a quick command finishes well within this
SHOWS_SCREEN_AFTER = {"tap", "type_text", "scroll", "key", "open_app", "open_settings", "open_url"}
NAVIGATION = {"open_app", "open_settings", "open_url"}
SCREEN_MARKER = "Elements on screen"
ELEMENT_LINE = re.compile(r"^\[\d+\] (.*?)(?: #\S+)?(?: (?:checked|unchecked|selected|focused)\b.*)?$")
MAX_OLD_SCREEN = 1500
REFUSAL = re.compile(r"\b(I(?:'|’)?m sorry|I can(?:'|’)?t|I cannot|I(?:'|’)?m (?:not able|unable)|I won(?:'|’)?t)\b", re.IGNORECASE)
# A reply claiming an action was done; also matches a copy of the bot's own "Stopped: I kept repeating" notice.
CLAIMED = re.compile(
    r"\b(?:I(?:'|’)?ve|I have|I)(?: just| now| already)? (?:sent|recorded|opened|turned|started|played|called|saved"
    r"|taken|took|scheduled|changed|set|deleted|installed|downloaded|tapped|pressed|typed|entered|clicked|selected"
    r"|generated|created|drawn"
    r"|deployed|attacked|closed|cleared|paused|stopped|searched|followed|finished|completed|done)\b"
    r"|\b(?:has|have) been (?:sent|set|opened|saved|turned|tapped|deployed|done|completed)\b"
    r"|^Stopped: I kept repeating",
    re.IGNORECASE,
)
# Added to a tap's result when the screen after it matches the one before it, or the one before the previous action.
SCREEN_NOTES = {
    "same": " The screen is the same as before this tap: it did nothing, or what it opened closed again before "
    "the screen was read. Don't just tap it again: if it opens something that closes by itself, put the next "
    "tap in the same reply.",
    "back": " The screen went back to how it was before your previous action: what that opened closed by itself "
    "before this tap landed (each of your steps takes a few seconds). Do both taps in one reply.",
}
NOTHING_DONE = (
    "No tool has been used for this request, so nothing was done just now. If the owner asked for an action, "
    "do it with the tools; if they're asking about earlier work, answer that without claiming anything new."
)
SCHEDULED_PREFIX = re.compile(r"^\[Scheduled #\d+\]\s*")
OWNER_PHOTO = "The owner's photo:"
TAPPED = re.compile(r'^(?:Tapped|Long-pressed) (?:\[\d+\] (.+?)|"(.+?)")\.$', re.MULTILINE)
# Never saved into shortcuts: read-only, and anything sensitive or personal.
SHORTCUT_SKIP = {
    "look", "phone_status", "list_schedules", "remember", "schedule_task", "cancel_schedule", "shell",
    "ask_owner", "send_voice", "generate_image",
}
MAX_SHORTCUT_STEPS = 8  # longer runs aren't worth saving
MAX_DONE_CHECKS = 2  # after a second failed check the reply goes out anyway (not saved as a route)
DONE_CHECKER = "gemini-flash-lite-latest"
PREVIOUS_FILE = "<the file from the previous step>"
SAVED_FILE = re.compile(r"(/(?:sdcard|storage|data)/\S+?\.\w+)(?=\.?(?:\s|$))")
FAILED_RESULTS = (
    "Tool error", "No element", "There is no element", "Blocked", "The owner", "Unknown tool",
    "Invalid JSON", "Capture failed", "Could not", "Timed out", "Cancelled", "No app", "Give an element",
)

# Raw adb commands that run without asking: swipes (drags no tool does) and read-only lookups.
AUTO_PHONE = re.compile(
    r"^shell (input swipe .+"
    r"|pm list packages( -3)?( [\w.]+)?|dumpsys [\w.]+|getprop( [\w.]+)?|wm size"
    r"|settings get (system|global|secure) [\w.]+|ls( -\w+)? /sdcard/[\w./-]*)$"
)
# Refused in the phone tool: the dedicated tools do these and also return the new screen and catch repeats.
TOOL_COMMANDS = re.compile(r"""^shell\s+["']?(input\s+(tap|text|keyevent)|monkey)\b""")
SHELL_METACHARS = re.compile(r"[;&|`$<>\\\n()]")
# Commands that only read; run without owner approval when there are no shell metacharacters.
READ_ONLY_CMDS = frozenset(
    "ls cat head tail df du free pwd wc grep egrep fgrep stat file find uname whoami date uptime ps "
    "env printenv which type hostname id tree realpath readlink dirname basename uniq cut".split()
)
DESTRUCTIVE_FLAGS = re.compile(r"(?<!\S)-(?:delete|exec\w*)(?!\S)")  # find's write flags
# Actions that would cut off remote access. Blocked outright, even with owner approval.
LOCKOUT = re.compile(
    r"\btcpip\b|\badb_wifi|\badb_enabled|\breboot\b|\bshutdown\b|svc (wifi|data|usb)|airplane_mode"
    r"|cmd wifi (forget|disconnect|set-wifi-enabled)|settings put global wifi"
    r"|(uninstall|force-stop|disable(-user)?|clear|suspend)\b.*(com\.termux|moe\.shizuku)",
    re.IGNORECASE,
)



def as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_bool(value: Any) -> bool:
    return value is True or str(value).lower() in ("true", "1", "yes")


def read_only_shell(command: str) -> bool:
    """A single plain command that only reads: safe to run without owner approval."""
    if SHELL_METACHARS.search(command) or DESTRUCTIVE_FLAGS.search(command):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return bool(argv) and argv[0] in READ_ONLY_CMDS






def compact_screen(listing: str) -> str:
    """An old element list reduced to its unique labels; numbers, ids and flags are stale anyway."""
    labels = [ELEMENT_LINE.sub(r"\1", line).strip() for line in listing.splitlines()[1:]]
    seen: set[str] = set()
    unique = [label for label in labels if label and label != "(no label)" and not (label in seen or seen.add(label))]
    return f"[earlier screen showed: {'; '.join(unique)[:MAX_OLD_SCREEN]}]"


def drop_old_screen_lists(messages: list[dict[str, Any]], before: int) -> None:
    """Compact element lists in tool results before index `before`; the conversation is re-sent every step."""
    for message in messages[:before]:
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, str) and SCREEN_MARKER in content:
            action, _, listing = content.partition(SCREEN_MARKER)
            message["content"] = f"{action.strip()} {compact_screen(SCREEN_MARKER + listing)}".strip()


def action_key(call: dict[str, Any]) -> str:
    """Key for spotting repeated calls: ignores argument order and treats nearby taps as the same spot."""
    name = call["function"]["name"]
    try:
        args = json.loads(call["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):
        args = {"args": args}
    if name == "tap":
        args = {k: round(v / SAME_SPOT) if k in ("x", "y") and isinstance(v, (int, float)) else v for k, v in args.items()}
    return json.dumps([name, args], sort_keys=True)


def is_refusal(message: dict[str, Any]) -> bool:
    return not message.get("tool_calls") and bool(REFUSAL.search(message.get("content") or ""))


def shortcut_step(call: dict[str, Any], result: str) -> tuple[str, dict[str, Any] | None] | None:
    """(description, call to replay or None) for a shortcut step, or None for failed, read-only or sensitive
    steps. Taps replay by visible text, since element numbers change; taps by coordinates can't replay."""
    name = call["function"]["name"]
    if name in SHORTCUT_SKIP or result.startswith(FAILED_RESULTS):
        return None
    if name == "tap" and (match := TAPPED.match(result)):
        label = match[1] or match[2]
        return f'tap "{label}"', {"name": "tap", "args": {"text": label}}
    try:
        args = json.loads(call["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        return None
    # A file path from one run means nothing in the next; a replay uses the newest file instead.
    args = {key: PREVIOUS_FILE if key == "path" else value for key, value in args.items() if value not in (None, "")}
    detail = " ".join(f"{key}={value}" for key, value in args.items())
    return f"{name} {detail}".strip(), None if name == "tap" else {"name": name, "args": args}


def drop_old_images(messages: list[dict[str, Any]]) -> None:
    """Replace every image message with a placeholder, except the owner's photo."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and content[0].get("text") != OWNER_PHOTO:
            message["content"] = "[earlier screenshot removed]"


# Stands in for a model while a saved route replays: names the steps in logs and progress, and skips
# screenshots until the AI looks at the result.
REPLAY_MODEL = Model("replay", "saved-route", vision=False)


class Agent:
    def __init__(
        self,
        router: Router,
        device: Device,
        memory: Memory,
        schedules: Schedules,
        shortcuts: Shortcuts,
        device_lock: threading.Lock,
        ask_owner: Callable[[str], bool],
        send_file: Callable[[Any, str], None],
        notify: Callable[[str], None],
        on_step: Callable[[], None],
        on_action: Callable[[str, str], None],
        on_text: Callable[[str], None],
        cancelled: Callable[[], bool],
        voice: Callable[[str, str, Path], float] | None = None,
        make_image: Callable[[str, str, Path], None] | None = None,
    ) -> None:
        self.router = router
        self.device = device
        self.memory = memory
        self.schedules = schedules
        self.shortcuts = shortcuts
        self.device_lock = device_lock
        self.holding = False
        self.ask_owner = ask_owner
        self.send_file = send_file
        self.notify = notify
        self.on_step = on_step
        self.on_action = on_action
        self.on_text = on_text
        self.cancelled = cancelled
        self.voice = voice
        self.make_image = make_image
        self.handlers = self.tool_handlers()

    def tool_handlers(self) -> dict[str, Callable[[dict[str, Any], Model], str | tuple[str, str | None]]]:
        """Tool name -> function of (arguments, model) that runs it."""
        return {
            "look": lambda a, m: self.device.look(picture=True),
            "tap": lambda a, m: self.device.tap(
                as_int(a.get("element")),
                as_float(a.get("x")),
                as_float(a.get("y")),
                as_int(a.get("hold_ms")),
                str(a.get("text") or "") or None,
            ),
            "set_alarm": lambda a, m: self.device.set_alarm(
                as_int(a.get("hour")) or 0, as_int(a.get("minute")) or 0, str(a.get("label", ""))
            ),
            "set_timer": lambda a, m: self.device.set_timer(as_int(a.get("seconds")) or 60, str(a.get("label", ""))),
            "open_url": lambda a, m: self.device.open_url(str(a.get("url", "")), as_bool(a.get("browser"))),
            "set_volume": lambda a, m: self.device.set_volume(
                as_int(a.get("percent")) or 0, str(a.get("stream") or "media")
            ),
            "set_brightness": lambda a, m: self.device.set_brightness(as_int(a.get("percent")) or 0),
            "phone_status": lambda a, m: self.device.status(),
            "open_settings": lambda a, m: self.device.open_settings(str(a.get("page", "")), str(a.get("app") or "")),
            "speak": lambda a, m: self.device.speak(str(a.get("text", ""))),
            "send_voice": lambda a, m: self.send_voice(str(a.get("text", "")), str(a.get("style") or "")),
            "generate_image": lambda a, m: self.generate_image(str(a.get("prompt", "")), str(a.get("shape") or "square")),
            "type_text": lambda a, m: self.device.type_text(
                str(a.get("text", "")), as_bool(a.get("submit")), as_bool(a.get("clear"))
            ),
            "scroll": lambda a, m: self.device.scroll(str(a.get("direction", ""))),
            "key": lambda a, m: self.device.key(str(a.get("name", ""))),
            "open_app": lambda a, m: self.device.open_app(str(a.get("name", ""))),
            "wait": lambda a, m: self.wait(as_int(a.get("seconds")) or 1),
            "take_photo": lambda a, m: self.device.take_photo(as_bool(a.get("front"))),
            "record_video": lambda a, m: self.device.record_video(as_int(a.get("seconds")) or 5, as_bool(a.get("front"))),
            "web_search": lambda a, m: web.search(str(a.get("query", ""))),
            "fetch_url": lambda a, m: web.fetch_text(str(a.get("url", ""))),
            "ask_owner": lambda a, m: (
                "The owner approved." if self.ask_owner(str(a.get("question", ""))) else "The owner said no; don't do it."
            ),
            "record_audio": lambda a, m: self.device.record_audio(as_int(a.get("seconds")) or 10),
            "find_media": lambda a, m: self.device.find_media(str(a.get("kind", "any")), as_int(a.get("count")) or 5),
            "send_file": lambda a, m: self.send(str(a.get("path", "")), str(a.get("caption", ""))),
            "send_screenshot": lambda a, m: self.send_screenshot(str(a.get("caption", ""))),
            "phone": lambda a, m: self.phone(str(a.get("args", ""))),
            "shell": lambda a, m: self.shell(str(a.get("command", ""))),
            "remember": lambda a, m: self.remember(str(a.get("text", ""))),
            "schedule_task": lambda a, m: self.schedule(
                str(a.get("task", "")), str(a.get("when", "")), str(a.get("repeat", "once"))
            ),
            "list_schedules": lambda a, m: self.schedules.listing() or "No scheduled tasks.",
            "cancel_schedule": lambda a, m: self.cancel_schedule(as_int(a.get("id"))),
        }

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(
            specs=self.device.specs(),
            now=self.schedules.now().strftime("%A %d %B %Y, %H:%M"),
            timezone=self.schedules.tz.key,
            memory=self.memory.listing() or "(nothing yet)",
        )

    def run(self, history: list[dict[str, Any]], task: str, images: list[str] | None = None) -> str:
        try:
            return self.steps(history, task, images or [])
        finally:
            if self.holding:
                self.holding = False
                self.device_lock.release()

    def check_done(self, request: str, reply: str, done_steps: list[tuple[str, dict[str, Any] | None]]) -> str | None:
        """Have a quick model check the reply against the actions and screen; returns what's missing, or None."""
        if not self.claim_device():
            return None
        screen, image = self.device.look()  # an image only when the list can't describe the screen (games...)
        actions = "\n".join(f"- {text}" for text, _ in done_steps) or "(none)"
        text = f"Request: {request}\n\nActions taken:\n{actions}\n\nReply: {reply}\n\n{screen}"
        content: str | list[dict[str, Any]] = text
        if image:
            content = [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": image}}]
        messages = [{"role": "system", "content": DONE_CHECK_PROMPT}, {"role": "user", "content": content}]
        try:
            answer, model = self.router.complete(
                messages, fast_first=True, tools=False, prefer=DONE_CHECKER, vision=bool(image)
            )
        except NoModelAvailable:
            return None
        verdict = (answer.get("content") or "").strip()
        log.info(f"done check by {model.label}: {verdict[:200]}")
        if answer.get("tool_calls") or not verdict.upper().startswith("WRONG"):
            return None
        return verdict.partition(":")[2].strip() or "the screen doesn't match the reply"

    def claim_device(self) -> bool:
        """Take the phone for the rest of this task, waiting briefly for a quick command to finish."""
        if not self.holding:
            if not self.device_lock.acquire(timeout=DEVICE_WAIT_S):
                return False
            self.holding = True
            self.device.wake()  # the screen may be off (/screenoff); reading and tapping need it on
        return True

    def opening(self, history: list[dict[str, Any]], task: str, request: str, images: list[str],
                shortcut: dict[str, Any] | None) -> list[dict[str, Any]]:
        """The conversation a task starts with; a saved route that can't be replayed is offered as a hint."""
        prompt = task
        if shortcut:
            steps = "\n".join(f"{n}. {step}" for n, step in enumerate(shortcut["steps"], 1))
            prompt = (
                f"{task}\n\n(Hint: a similar earlier request, \"{shortcut['task']}\", worked with these steps:\n"
                f"{steps}\nReuse them if they fit, but still check each new screen.)"
            )
            log.info(f"offering shortcut #{shortcut['id']} for: {request[:80]}")
        messages = [{"role": "system", "content": self.system_prompt()}, *history, {"role": "user", "content": prompt}]
        if images:  # separate from the request, so dropping old images later keeps the request
            parts = [{"type": "image_url", "image_url": {"url": url}} for url in images]
            messages.append({"role": "user", "content": [{"type": "text", "text": OWNER_PHOTO}, *parts]})
        return messages

    def next_message(self, messages: list[dict[str, Any]], first_step: bool, images: list[str]) -> tuple[dict[str, Any], Model]:
        # The quick first-step models can't see images; once any image is in the conversation, only models
        # that see images are asked.
        fast = first_step and not images
        pictured = any(isinstance(m.get("content"), list) for m in messages)
        message, model = self.router.complete(messages, fast_first=fast, on_text=self.on_text, vision=pictured)
        if first_step and model.fast and is_refusal(message):
            # Quick models refuse some things the main models handle within the rules; let those decide.
            log.info(f"{model.label} refused; asking the main models instead")
            message, model = self.router.complete(messages, on_text=self.on_text, vision=pictured)
        return message, model

    def steps(self, history: list[dict[str, Any]], task: str, images: list[str]) -> str:
        request = SCHEDULED_PREFIX.sub("", task)
        shortcut = self.shortcuts.match(request)
        replayable = shortcut and shortcut.get("calls") and self.shortcuts.same_request(shortcut, request)
        messages = self.opening(history, task, request, images, None if replayable else shortcut)
        first_new = len(messages) - (2 if images else 1)
        recent: list[str] = []
        done_steps: list[tuple[str, dict[str, Any] | None]] = []
        went_in_circles = False
        checks = 0
        doubtful = False  # the last check said the screen doesn't show the task done
        nudged = False
        self.device.looks = []  # screens from an earlier task say nothing about this one's taps
        if replayable:
            done_steps = self.replay(shortcut, messages)
        for step in range(MAX_STEPS):
            if self.cancelled():
                return "Stopped."
            self.on_step()
            try:
                message, model = self.next_message(messages, step == 0, images)
            except NoModelAvailable as error:
                return str(error)
            messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                reply = (message.get("content") or "").strip() or "(no reply)"
                used = [call["function"]["name"] for earlier in messages[first_new:] for call in earlier.get("tool_calls") or []]
                if not used and not done_steps and not nudged and CLAIMED.search(reply):
                    nudged = True
                    log.info(f"{model.label} claimed an action without using a tool; asking again")
                    messages.pop()  # left in, the model repeats the false claim
                    messages.append({"role": "user", "content": NOTHING_DONE})
                    continue
                moved = any(name in SHOWS_SCREEN_AFTER for name in used)
                if moved and checks < MAX_DONE_CHECKS and not self.cancelled():
                    checks += 1
                    problem = self.check_done(request, reply, done_steps)
                    doubtful = problem is not None
                    if doubtful and checks < MAX_DONE_CHECKS:
                        messages.append({
                            "role": "user",
                            "content": f"Before you reply: a check of the current screen says this isn't done ({problem}). "
                            "Look at the screen, then finish the task, or tell the owner plainly what didn't happen.",
                        })
                        continue
                if done_steps and len(done_steps) <= MAX_SHORTCUT_STEPS and not went_in_circles and not doubtful:
                    # Replayable only if every step is, so a replay never skips a step it can't redo.
                    calls = [replay for _, replay in done_steps]
                    self.shortcuts.save(request, [text for text, _ in done_steps], calls if all(calls) else [])
                return reply
            signature = json.dumps([action_key(c) for c in calls])
            recent = (recent + [signature])[-LOOP_WINDOW:]
            self.run_batch(calls, model, messages, done_steps)
            # Catches both repeats (A A A) and back-and-forth loops (A B A B A).
            looping = recent.count(signature) >= REPEAT_LIMIT
            if looping and not any(c["function"]["name"] in ("look", "wait") for c in calls):
                if went_in_circles:  # warned once already and still repeating: more steps won't help
                    action = calls[-1]["function"]
                    return (
                        f"Stopped: I kept repeating the same action ({action['name']} {action.get('arguments') or ''}) "
                        "without getting anywhere. Tell me more exactly what to tap, or try it another way."
                    )
                messages.append({
                    "role": "user",
                    "content": "You are going in circles, repeating the same actions without progress. Stop: "
                    "read the current screen carefully, then try a different approach, or tell the owner what "
                    "is blocking you.",
                })
                recent = []
                went_in_circles = True
        return "Stopped: too many steps without finishing. Try breaking the task into smaller ones."

    def run_batch(self, calls: list[dict[str, Any]], model: Model, messages: list[dict[str, Any]],
                  done_steps: list[tuple[str, dict[str, Any] | None]]) -> None:
        """Run one reply's tool calls, adding their results and the new screen to messages."""
        images, first_result = [], len(messages)
        last_move = max((n for n, c in enumerate(calls) if c["function"]["name"] in SHOWS_SCREEN_AFTER), default=-1)
        skip = ""
        for number, call in enumerate(calls):
            name = call["function"]["name"]
            if skip and name in SHOWS_SCREEN_AFTER:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": skip})
                continue
            navigates = name in NAVIGATION and number < last_move
            result, image = self.execute(call, model, show_screen=number == last_move or navigates)
            failed = name in SHOWS_SCREEN_AFTER and result.startswith(FAILED_RESULTS)
            if number < last_move and not skip and (navigates or failed):
                # The rest of the batch was planned on the screen before this call.
                skip = "Skipped: the screen changed or an earlier action failed. Check the new screen first."
                if failed and self.claim_device():
                    screen, image = self.device.look()
                    result = f"{result}\n\n{screen}"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            if image:
                images.append(image)
            if described := shortcut_step(call, result):
                done_steps.append(described)
        if any(SCREEN_MARKER in m["content"] for m in messages[first_result:]):
            drop_old_screen_lists(messages, first_result)
        if images:
            drop_old_images(messages)
            parts = [{"type": "image_url", "image_url": {"url": url}} for url in images]
            messages.append({"role": "user", "content": [{"type": "text", "text": "Current screen:"}, *parts]})

    def replay(self, shortcut: dict[str, Any], messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any] | None]]:
        """Run a saved route without asking the AI, stopping at the first failed step; returns the steps done.
        Appends the calls, results and current screen to messages as if the AI had made them."""
        log.info(f"replaying shortcut #{shortcut['id']} ({len(shortcut['calls'])} steps)")
        calls, results, done = [], [], []
        last_file = None
        for number, saved in enumerate(shortcut["calls"]):
            if self.cancelled():
                break
            args = dict(saved["args"])
            if args.get("path") == PREVIOUS_FILE:
                if last_file is None:
                    break
                args["path"] = last_file
            call = {"id": f"replay-{number}", "type": "function", "function": {"name": saved["name"], "arguments": json.dumps(args)}}
            result, _ = self.execute(call, REPLAY_MODEL, show_screen=False)
            calls.append(call)
            results.append(result)
            if result.startswith(FAILED_RESULTS):
                log.info(f"replay stopped at step {number + 1}: {result[:120]}")
                break
            if match := SAVED_FILE.search(result):
                last_file = match[1]
            if described := shortcut_step(call, result):
                done.append(described)
        if not calls:
            return done
        messages.append({"role": "assistant", "content": None, "tool_calls": calls})
        messages.extend({"role": "tool", "tool_call_id": c["id"], "content": r} for c, r in zip(calls, results))
        drop_old_screen_lists(messages, len(messages))
        screen, image = self.device.look() if self.claim_device() else ("(screen unavailable)", None)
        total = len(shortcut["calls"])
        outcome = "All of them ran." if len(done) == total else f"It stopped after {len(calls)} of {total} steps."
        note = (
            f"These steps were replayed automatically from a route that worked before for this same request. {outcome} "
            "Check the current screen: if the task is done, reply to the owner; otherwise carry on from here."
        )
        messages.append({"role": "user", "content": f"{note}\n\n{screen}"})
        if image:  # separate, like other screenshots, so dropping it later keeps the note and screen list
            messages.append({"role": "user", "content": [{"type": "text", "text": "Current screen:"}, {"type": "image_url", "image_url": {"url": image}}]})
        return done

    def execute(self, call: dict[str, Any], model: Model, show_screen: bool = True) -> tuple[str, str | None]:
        """Run one tool call; returns (result, screenshot or None). show_screen appends the new screen after
        a screen-changing tool."""
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            return "Invalid JSON arguments.", None
        log.info(f"{model.label} -> {name} {args}")
        detail = " ".join(str(value) for value in args.values() if value not in (None, ""))
        self.on_action(model.short, f"{name} {detail}".strip())
        handler = self.handlers.get(name)
        if handler is None:
            return f"Unknown tool: {name}", None
        if self.cancelled():
            return "Cancelled by the owner.", None
        if name in SCREEN_TOOLS and not self.claim_device():
            return "The phone is busy right now (a command is running). Tell the owner.", None
        try:
            result = handler(args, model)
        except subprocess.TimeoutExpired:
            return "Timed out.", None
        except Exception as error:
            log.exception(f"tool {name} failed")
            return f"Tool error: {error}", None
        if isinstance(result, tuple):
            return result
        result = str(result)[-MAX_TOOL_OUTPUT:]
        if result.startswith(FAILED_RESULTS):
            log.info(f"{name} failed: {result[:120]}")
        if show_screen and name in SHOWS_SCREEN_AFTER:
            screen, image = self.device.look()
            note = SCREEN_NOTES.get(self.device.screen_note, "") if name == "tap" else ""
            if note:
                log.info(f"{name}: screen {self.device.screen_note} after it")
            return f"{result}{note}\n\n{screen}", image
        return result, None

    def wait(self, seconds: int) -> str:
        end = time.time() + max(1, min(seconds, MAX_WAIT_S))
        while time.time() < end and not self.cancelled():
            time.sleep(1)
        return f"Waited {seconds}s."

    def send(self, path: str, caption: str) -> str:
        local = self.device.fetch(path)
        sendable = ready_to_send(local)
        try:
            self.send_file(sendable, caption)
        finally:
            for temporary in {local, sendable}:
                if is_temporary(temporary):
                    temporary.unlink(missing_ok=True)
        return "Sent."

    def send_voice(self, text: str, style: str) -> str:
        if not self.voice:
            return "Voice messages need a Gemini API key (providers.gemini in the config)."
        if not text.strip():
            return "Nothing to say."
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        out = MEDIA_DIR / f"voice-{time.time_ns()}.opus"
        try:
            seconds = self.voice(text, style, out)
            self.send_file(out, "")
        finally:
            out.unlink(missing_ok=True)
        return f"Sent a {seconds:.0f}s voice message."

    def generate_image(self, prompt: str, shape: str) -> str:
        if not self.make_image:
            return "Making images needs an NVIDIA API key (providers.nvidia in the config)."
        if not prompt.strip():
            return "Describe the picture to make."
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        out = MEDIA_DIR / f"ai_{datetime.now(self.schedules.tz):%Y%m%d_%H%M%S}.jpg"
        try:
            self.make_image(prompt, shape, out)
            self.send_file(out, "")
            saved = self.device.receive(out, GENERATED_DIR)
        finally:
            out.unlink(missing_ok=True)
        return f"Made the picture and sent it to the owner; a copy is on the phone at {saved}."

    def send_screenshot(self, caption: str) -> str:
        self.device.snapshot()
        self.send_file(SCREEN_PATH, caption)
        return "Sent."

    def phone(self, args: str) -> str:
        if LOCKOUT.search(args):
            return "Blocked: this could cut off remote access to the phone."
        if TOOL_COMMANDS.match(args.strip()):
            return ("Not run: use the tap tool (x/y on the 0-1000 scale for any point), key, type_text or open_app "
                    "instead. They return the new screen.")
        try:
            argv = shlex.split(args)
        except ValueError as error:
            return f"Could not parse arguments: {error}"
        body = args[6:] if args.startswith("shell ") else ""
        routine = (AUTO_PHONE.match(args) or read_only_shell(body)) and not SHELL_METACHARS.search(args)
        if not routine and not self.ask_owner(f"phone {args}"):
            return "The owner denied this action."
        result = adb(*argv)
        output = (result.stdout + result.stderr).decode(errors="replace").strip()
        return output or f"Done (exit {result.returncode})."

    def shell(self, command: str) -> str:
        if LOCKOUT.search(command):
            return "Blocked: this could cut off remote access to the phone."
        if not read_only_shell(command) and not self.ask_owner(f"shell: {command}"):
            return "The owner denied this action."
        result = subprocess.run(
            ["bash", "-lc", command], cwd=WORKSPACE_DIR, capture_output=True, text=True, timeout=300
        )
        return (result.stdout + result.stderr).strip() or f"Done (exit {result.returncode})."

    def remember(self, text: str) -> str:
        if not text.strip():
            return "Nothing to remember."
        item = self.memory.add(text)
        self.notify(f"Remembered #{item['id']}: {item['text']}")
        return f"Saved as note {item['id']}."

    def schedule(self, task: str, when: str, repeat: str) -> str:
        try:  # validate everything before asking, and store exactly the time the owner approved
            run_at = self.schedules.parse_when(when)
            repeat = self.schedules.check_repeat(repeat)
        except ValueError as error:
            return str(error)
        summary = f"Schedule this task?\n{task}\nFirst run: {run_at:%a %d %b %H:%M}, repeat: {repeat}"
        if not self.ask_owner(summary):
            return "The owner did not approve the schedule."
        item = self.schedules.add(task, run_at, repeat)
        late = " Its first run time passed while waiting for approval, so it runs now." if run_at <= self.schedules.now() else ""
        return f"Scheduled as #{item['id']}.{late}"

    def cancel_schedule(self, item_id: int | None) -> str:
        return f"Cancelled schedule {item_id}." if item_id and self.schedules.remove(item_id) else "No such schedule."
