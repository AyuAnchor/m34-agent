"""Quick commands: common actions that run instantly, without the AI."""
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from device import MAX_AUDIO_S, MAX_VIDEO_S, Device, getprop, shell

DURATION = re.compile(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?")
CLOCK_TIME = re.compile(r"(\d{1,2})[:.](\d{2})\s*(.*)")
AI_HINT = re.compile(r"\s*[^.]*\b(look|send_file)\b[^.]*\.", re.IGNORECASE)


def plain(text: str) -> str:
    """Drop the hints meant for the AI ("Call look to see the page.") from a device result."""
    return AI_HINT.sub("", text).strip()


TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
HEADER = re.compile(r"^\s*(#{1,6})\s+(.*)$")
BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    """Markdown emphasis in one already-escaped line to Telegram HTML."""
    codes: list[str] = []

    def stash(match: re.Match) -> str:
        codes.append(f"<code>{match.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _table(header: list[str], body: list[list[str]]) -> str:
    """A markdown pipe table as an aligned monospace block (Telegram has no table markup)."""
    rows = [[re.sub(r"[*_`]", "", cell) for cell in row] for row in [header, *body]]
    columns = max(len(row) for row in rows)
    rows = [row + [""] * (columns - len(row)) for row in rows]
    widths = [max(len(row[c]) for row in rows) for c in range(columns)]
    line = lambda row: " | ".join(row[c].ljust(widths[c]) for c in range(columns))
    separator = "-+-".join("-" * widths[c] for c in range(columns))
    body_lines = "\n".join(line(row) for row in rows[1:])
    return "<pre>" + _escape(f"{line(rows[0])}\n{separator}\n{body_lines}") + "</pre>"


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def format_telegram(md: str) -> str:
    """Render the AI's Markdown as Telegram HTML: bold, italic, code, links, bullets, and tables and
    code blocks as monospace. Telegram has no headers or tables, so headers become bold and tables
    aligned <pre> blocks."""
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            out.append("<pre>" + _escape("\n".join(block)) + "</pre>")
        elif "|" in line and i + 1 < len(lines) and "|" in lines[i + 1] and TABLE_SEP.match(lines[i + 1]):
            header = _cells(line)
            i += 2
            body = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                body.append(_cells(lines[i]))
                i += 1
            out.append(_table(header, body))
        elif header := HEADER.match(line):
            out.append("<b>" + _inline(_escape(header.group(2).strip())) + "</b>")
            i += 1
        elif bullet := BULLET.match(line):
            out.append(f"{bullet.group(1)}• " + _inline(_escape(bullet.group(2))))
            i += 1
        else:
            out.append(_inline(_escape(line)))
            i += 1
    return "\n".join(out)


@dataclass
class Reply:
    text: str = ""
    file: Path | None = None


@dataclass
class Command:
    run: Callable[[str], Reply]
    usage: str
    description: str
    changes_device: bool = True


def parse_duration(text: str, plain_unit_s: int) -> int | None:
    """Seconds from '90s', '5m', '1h30m', or a plain number counted in plain_unit_s."""
    text = text.strip().lower()
    if text.isdigit():
        return int(text) * plain_unit_s
    match = DURATION.fullmatch(text)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(group or 0) for group in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def split_first(args: str) -> tuple[str, str]:
    first, _, rest = args.strip().partition(" ")
    return first, rest.strip()


class QuickCommands:
    def __init__(self, device: Device) -> None:
        self.device = device
        self.table: dict[str, Command] = {
            "open": Command(self.open, "/open youtube", "Open an app"),
            "url": Command(self.url, "/url example.com", "Open a link"),
            "photo": Command(self.photo, "/photo [front]", "Take a photo and send it"),
            "video": Command(self.video, "/video 10 [front]", f"Record a video (max {MAX_VIDEO_S}s) and send it"),
            "audio": Command(self.audio, "/audio 10", f"Record audio (max {MAX_AUDIO_S}s) and send it"),
            "alarm": Command(self.alarm, "/alarm 7:30 [label]", "Set an alarm"),
            "timer": Command(self.timer, "/timer 5m [label]", "Start a timer"),
            "volume": Command(self.volume, "/volume 50 [media|ring|alarm|notification]", "Set volume"),
            "brightness": Command(self.brightness, "/brightness 40", "Set screen brightness"),
            "say": Command(self.say, "/say hello", "Speak out loud on the phone"),
            "tap": Command(self.tap, "/tap Settings", "Tap something by its text"),
            "type": Command(self.type, "/type hello", "Type into the focused field"),
            "screenoff": Command(lambda _: Reply(self.device.screen_off()), "/screenoff", "Turn the screen off"),
            "screenon": Command(lambda _: Reply(self.device.screen_on()), "/screenon", "Turn the screen on"),
            "home": Command(lambda _: Reply(self.device.key("home")), "/home", "Go to the home screen"),
            "back": Command(lambda _: Reply(self.device.key("back")), "/back", "Press back"),
            "battery": Command(lambda _: Reply(self.device.battery()), "/battery", "Battery level", changes_device=False),
            "info": Command(self.info, "/info", "Phone status", changes_device=False),
            "wifi": Command(self.wifi, '/wifi "Network name" "password"', "Join a Wi-Fi network"),
        }

    def usage(self, name: str) -> Reply:
        return Reply(f"Usage: {self.table[name].usage}")

    def open(self, args: str) -> Reply:
        return Reply(self.device.open_app(args)) if args else self.usage("open")

    def url(self, args: str) -> Reply:
        if not args:
            return self.usage("url")
        return Reply(self.device.open_url(args if "://" in args else f"https://{args}"))

    def photo(self, args: str) -> Reply:
        path = self.device.capture(video=False, seconds=0, front="front" in args.lower())
        return Reply(file=self.device.fetch(path))

    def video(self, args: str) -> Reply:
        first, rest = split_first(args)
        seconds = parse_duration(first, 1) if first and first.lower() != "front" else 10
        if not seconds:
            return self.usage("video")
        front = "front" in args.lower()
        return Reply(file=self.device.fetch(self.device.capture(video=True, seconds=min(seconds, MAX_VIDEO_S), front=front)))

    def audio(self, args: str) -> Reply:
        seconds = parse_duration(args, 1) if args else 10
        return Reply(file=self.device.record_audio_file(min(seconds, MAX_AUDIO_S))) if seconds else self.usage("audio")

    def alarm(self, args: str) -> Reply:
        match = CLOCK_TIME.fullmatch(args.strip())
        if not match or int(match[1]) > 23 or int(match[2]) > 59:
            return self.usage("alarm")
        return Reply(self.device.set_alarm(int(match[1]), int(match[2]), match[3]))

    def timer(self, args: str) -> Reply:
        first, label = split_first(args)
        seconds = parse_duration(first, 60) if first else None
        return Reply(self.device.set_timer(seconds, label)) if seconds else self.usage("timer")

    def volume(self, args: str) -> Reply:
        first, stream = split_first(args)
        if not first.isdigit():
            return self.usage("volume")
        return Reply(self.device.set_volume(int(first), stream.lower() or "media"))

    def brightness(self, args: str) -> Reply:
        return Reply(self.device.set_brightness(int(args))) if args.strip().isdigit() else self.usage("brightness")

    def say(self, args: str) -> Reply:
        return Reply(self.device.speak(args)) if args else self.usage("say")

    def tap(self, args: str) -> Reply:
        return Reply(self.device.tap(None, None, None, None, args)) if args else self.usage("tap")

    def type(self, args: str) -> Reply:
        return Reply(self.device.type_text(args, submit=False)) if args else self.usage("type")

    def wifi(self, args: str) -> Reply:
        try:
            parts = shlex.split(args)
        except ValueError:
            parts = []
        if not 1 <= len(parts) <= 2:
            return self.usage("wifi")
        return Reply(self.device.join_wifi(parts[0], parts[1] if len(parts) == 2 else ""))

    def info(self, _: str) -> Reply:
        model = getprop("ro.product.model")
        android = getprop("ro.build.version.release")
        wifi = re.search(r'connected to "([^"]*)"', shell("cmd wifi status"))
        ip = re.search(r"inet ([\d.]+)", shell("ip -4 addr show wlan0"))
        storage = shell("df -h /data | tail -1").split()
        uptime = shell("uptime").split(",")[0].split("up", 1)[-1].strip()
        lines = [
            f"{model}, Android {android}",
            self.device.battery(),
            f"Wi-Fi: {wifi[1] if wifi else 'not connected'}" + (f" ({ip[1]})" if ip else ""),
            f"Storage: {storage[2]} used of {storage[1]} ({storage[4]})" if len(storage) >= 5 else "Storage: ?",
            f"Up for {uptime}",
        ]
        return Reply("\n".join(lines))
