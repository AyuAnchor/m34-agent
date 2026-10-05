"""Long-term memory notes and scheduled tasks, kept in small JSON files."""
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

STATE_DIR = Path.home() / ".cache/tg-bridge"
# Android still reports some old zone names that Debian only ships in its optional legacy package.
LEGACY_ZONES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Rangoon": "Asia/Yangon",
    "Europe/Kiev": "Europe/Kyiv",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
}
MAX_MEMORIES = 100
MIN_INTERVAL_MIN = 5
INTERVALS = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(weeks=1)}
RELATIVE = re.compile(r"in (\d+) (minute|hour|day)s?")
EVERY = re.compile(r"every (\d+) minutes?")
MAX_SHORTCUTS = 50
SHORTCUT_MATCH = 0.5  # share of keywords two requests must have in common
MIN_SHORTCUT_KEYWORDS = 2  # "hi" or "thanks" is chat, never a shortcut
STOPWORDS = {
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "me", "my", "please", "can", "you",
    "could", "would", "i", "it", "is", "this", "that", "with", "from", "then", "now", "what", "s", "tell",
}


log = logging.getLogger("store")


def write_atomically(path: Path, text: str) -> None:
    """Write to a temporary file, flush it to storage, then swap it in: a crash or power loss mid-write
    leaves the previous version intact instead of a half-written file."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(temp, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any) -> Any:
    """Load a JSON file. A damaged one is moved aside and logged rather than read as empty, which the
    next save would otherwise make permanent."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        damaged = path.with_name(f"{path.name}.damaged-{datetime.now():%Y%m%d-%H%M%S}")
        path.replace(damaged)
        log.error(f"{path.name} was damaged; kept it as {damaged.name} and started a new one")
        return default


class JsonList:
    def __init__(self, name: str) -> None:
        self.path = STATE_DIR / name
        self.lock = threading.Lock()

    def load(self) -> list[dict[str, Any]]:
        return read_json(self.path, [])

    def save(self, items: list[dict[str, Any]]) -> None:
        write_atomically(self.path, json.dumps(items, indent=2, ensure_ascii=False))


def next_id(items: list[dict[str, Any]]) -> int:
    return max((item["id"] for item in items), default=0) + 1


class Memory:
    """Facts and preferences the agent keeps across conversations."""

    def __init__(self) -> None:
        self.store = JsonList("memory.json")

    def add(self, text: str) -> dict[str, Any]:
        with self.store.lock:
            items = self.store.load()
            item = {"id": next_id(items), "text": text.strip()[:500], "saved": datetime.now().isoformat(timespec="minutes")}
            items = (items + [item])[-MAX_MEMORIES:]
            self.store.save(items)
        return item

    def remove(self, item_id: int) -> bool:
        with self.store.lock:
            items = self.store.load()
            kept = [item for item in items if item["id"] != item_id]
            self.store.save(kept)
        return len(kept) != len(items)

    def all(self) -> list[dict[str, Any]]:
        return self.store.load()

    def listing(self) -> str:
        return "\n".join(f"{item['id']}. {item['text']}" for item in self.all())


PACIFIC = ZoneInfo("America/Los_Angeles")
USAGE_DAYS_KEPT = 7


class Usage:
    """AI requests per model per day. Days follow Pacific time, when Gemini's daily quotas reset."""

    def __init__(self) -> None:
        self.path = STATE_DIR / "usage.json"
        self.lock = threading.Lock()

    def load(self) -> dict[str, dict[str, list[int]]]:
        return read_json(self.path, {})

    def record(self, label: str, ok: bool) -> None:
        """Count one answered request (ok) or one refused for rate limits."""
        day = datetime.now(PACIFIC).date().isoformat()
        with self.lock:
            data = self.load()
            counts = data.setdefault(day, {}).setdefault(label, [0, 0])
            counts[0 if ok else 1] += 1
            for old_day in sorted(data)[:-USAGE_DAYS_KEPT]:
                del data[old_day]
            write_atomically(self.path, json.dumps(data))

    def today(self) -> dict[str, list[int]]:
        return self.load().get(datetime.now(PACIFIC).date().isoformat(), {})


def keywords(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in STOPWORDS}


class Shortcuts:
    """Steps from tasks that worked, offered as a hint when a similar request comes in."""

    def __init__(self) -> None:
        self.store = JsonList("shortcuts.json")

    def match(self, task: str) -> dict[str, Any] | None:
        words = keywords(task)
        if len(words) < MIN_SHORTCUT_KEYWORDS:
            return None
        best, best_score = None, 0.0
        for item in self.store.load():
            other = keywords(item["task"])
            score = len(words & other) / len(words | other) if words and other else 0.0
            if score > best_score:
                best, best_score = item, score
        return best if best_score >= SHORTCUT_MATCH else None

    @staticmethod
    def same_request(item: dict[str, Any], task: str) -> bool:
        """Exactly the same keywords: only then is a saved route safe to replay without the AI, since
        "record 10 seconds" must not replay "record 5 seconds"."""
        return keywords(item["task"]) == keywords(task)

    def save(self, task: str, steps: list[str], calls: list[dict[str, Any]]) -> None:
        """Keep one shortcut per request; a shorter route for the same request replaces the old one.
        steps describe the route for the AI; calls are the same steps as tool calls, for replaying."""
        words = keywords(task)
        if len(words) < MIN_SHORTCUT_KEYWORDS:
            return
        saved = datetime.now().isoformat(timespec="minutes")
        with self.store.lock:
            items = self.store.load()
            for item in items:
                if keywords(item["task"]) == words:
                    if len(steps) <= len(item["steps"]) or not item.get("calls"):  # older saves can't replay
                        item.update(task=task, steps=steps, calls=calls, saved=saved)
                        self.store.save(items)
                    return
            item = {"id": next_id(items), "task": task, "steps": steps, "calls": calls, "saved": saved}
            self.store.save((items + [item])[-MAX_SHORTCUTS:])

    def remove(self, item_id: int) -> bool:
        with self.store.lock:
            items = self.store.load()
            kept = [item for item in items if item["id"] != item_id]
            self.store.save(kept)
        return len(kept) != len(items)

    def listing(self) -> str:
        return "\n".join(f"#{item['id']} {item['task']} ({len(item['steps'])} steps)" for item in self.store.load())


def load_zone(name: str) -> ZoneInfo:
    for candidate in (name, LEGACY_ZONES.get(name, "")):
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def interval_of(repeat: str) -> timedelta | None:
    if repeat in INTERVALS:
        return INTERVALS[repeat]
    match = EVERY.fullmatch(repeat)
    return timedelta(minutes=max(int(match[1]), MIN_INTERVAL_MIN)) if match else None


class Schedules:
    """Tasks that run later, once or on repeat, in the phone's time zone."""

    def __init__(self, timezone: str) -> None:
        self.tz = load_zone(timezone)
        self.store = JsonList("schedules.json")

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def parse_when(self, when: str) -> datetime:
        when = when.strip().lower()
        now = self.now()
        if match := RELATIVE.fullmatch(when):
            amount, unit = int(match[1]), match[2]
            return now + timedelta(**{f"{unit}s": amount})
        for pattern in ("%Y-%m-%d %H:%M", "%H:%M"):
            try:
                parsed = datetime.strptime(when, pattern)
            except ValueError:
                continue
            if pattern == "%H:%M":
                run = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
                return run if run > now else run + timedelta(days=1)
            return parsed.replace(tzinfo=self.tz)
        raise ValueError("Use 'HH:MM', 'YYYY-MM-DD HH:MM' or 'in N minutes/hours/days'.")

    @staticmethod
    def check_repeat(repeat: str) -> str:
        repeat = repeat.strip().lower() or "once"
        if repeat != "once" and interval_of(repeat) is None:
            raise ValueError("repeat must be once, hourly, daily, weekly or 'every N minutes'.")
        return repeat

    def add(self, task: str, run_at: datetime, repeat: str) -> dict[str, Any]:
        """Store exactly the time that was shown for approval. If it passed while waiting, it runs at the
        next scheduler tick."""
        repeat = self.check_repeat(repeat)
        with self.store.lock:
            items = self.store.load()
            item = {"id": next_id(items), "task": task.strip(), "next_run": run_at.isoformat(), "repeat": repeat}
            self.store.save(items + [item])
        return item

    def remove(self, item_id: int) -> bool:
        with self.store.lock:
            items = self.store.load()
            kept = [item for item in items if item["id"] != item_id]
            self.store.save(kept)
        return len(kept) != len(items)

    def all(self) -> list[dict[str, Any]]:
        return self.store.load()

    def listing(self) -> str:
        lines = []
        for item in self.all():
            run_at = datetime.fromisoformat(item["next_run"]).astimezone(self.tz).strftime("%a %d %b %H:%M")
            lines.append(f"#{item['id']} {run_at} ({item['repeat']}): {item['task']}")
        return "\n".join(lines)

    def pop_due(self) -> list[dict[str, Any]]:
        """Return tasks that are due and move repeating ones to their next future time (no catch-up runs)."""
        now = self.now()
        with self.store.lock:
            items, due, kept = self.store.load(), [], []
            for item in items:
                # Back into the zone itself, not the fixed offset that was saved, so "daily 07:00" stays
                # 07:00 across daylight-saving changes and follows the phone if its time zone changes.
                run_at = datetime.fromisoformat(item["next_run"]).astimezone(self.tz)
                if run_at > now:
                    kept.append(item)
                    continue
                due.append(item)
                interval = interval_of(item["repeat"])
                if interval:
                    while run_at <= now:
                        run_at += interval
                    kept.append({**item, "next_run": run_at.isoformat()})
            if due:
                self.store.save(kept)
        return due
