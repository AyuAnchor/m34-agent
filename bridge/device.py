"""Phone control over adb: numbered screen elements, taps, typing, apps, camera and files."""
import base64
import io
import json
import logging
import re
import shlex
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageChops, ImageDraw, ImageFont

try:  # persistent UI automation server: ~0.3s per screen read instead of ~3s for `uiautomator dump`
    import uiautomator2
except ImportError:
    uiautomator2 = None

log = logging.getLogger("device")

ADB_TARGET = "127.0.0.1:5555"  # same device the `phone` wrapper talks to
# adb's own connection errors only, so a command's own failure output never causes a repeat run.
ADB_DISCONNECTED = re.compile(
    r"^(?:adb: )?(?:error: )?(?:device\b.*\b(?:not found|offline)|no devices|closed)", re.MULTILINE
)
# With Android animations off (see README), a short pause lets the tap register before the stability check.
ACTION_PAUSE_S = 0.3
SCREEN_PATH = Path("/tmp/agent-screen.jpg")
MEDIA_DIR = Path("/tmp/agent-media")
CAMERA_DIR = "/sdcard/DCIM/Camera"
SCREENSHOT_DIR = "/sdcard/DCIM/Screenshots"
MAX_ELEMENTS = 80
WIRELESS_DEBUG_PROMPT = "Allow wireless debugging on this network?"
STABLE_TIMEOUT_S = 2.5
MAX_VIDEO_S = 30
MAX_AUDIO_S = 120
SAVE_WAIT_S = 12
# Termux:API commands; Termux's files are visible inside Debian at the same paths.
TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
MIC_RECORDER = f"{TERMUX_BIN}/termux-microphone-record"
AUDIO_DIR = Path("/data/data/com.termux/files/home/.agent-audio")
VOLUME_STREAMS = {"media": 3, "ring": 2, "alarm": 4, "notification": 5}
DUMPSYS_STREAMS = {"media": "MUSIC", "ring": "RING", "alarm": "ALARM", "notification": "NOTIFICATION"}
STATUS_SCRIPT = (
    "dumpsys audio | grep -E '^- STREAM_(MUSIC|RING|ALARM|NOTIFICATION):|^   (Max|streamVolume):';"
    "echo ringer=$(dumpsys audio | grep -m1 'mode (external)' | sed 's/.*= //');"
    "echo brightness=$(settings get system screen_brightness);"
    "echo auto_brightness=$(settings get system screen_brightness_mode);"
    "echo timeout=$(settings get system screen_off_timeout);"
    "echo airplane=$(settings get global airplane_mode_on);"
    "echo bluetooth=$(settings get global bluetooth_on);"
    "echo zen=$(settings get global zen_mode);"
    "echo rotate=$(settings get system accelerometer_rotation);"
    "echo night=$(cmd uimode night);"
    "cmd wifi status | grep -m1 'connected to';"
    "dumpsys battery | grep -E '^  (level|status):'"
)
TOGGLES = {  # setting -> (command to turn it on, command to turn it off)
    "auto_rotate": ("settings put system accelerometer_rotation 1", "settings put system accelerometer_rotation 0"),
    "dark_mode": ("cmd uimode night yes", "cmd uimode night no"),
    "bluetooth": ("cmd bluetooth_manager enable", "cmd bluetooth_manager disable"),
    "do_not_disturb": ("cmd notification set_dnd on", "cmd notification set_dnd off"),
}
LARGE_VIDEO_BYTES = 20 * 1024 * 1024  # compress videos above this before sending
MOTION_FPS = 2
MOTION_SIZE = (160, 90)
MOTION_PIXEL_DIFF = 25  # a pixel "changed" if its brightness moves by more than this (0-255)
MOTION_SHARE = 0.02  # motion if more than 2% of the picture changed since the previous frame
MOTION_SKIP_S = 1.0  # ignore the first second while the camera settles its exposure
PHONE_NUMBER = re.compile(r"\+?[\d\s()-]{3,20}")
LAUNCHER = "android.intent.category.LAUNCHER"
# Apps whose package name doesn't contain their everyday name.
APP_ALIASES = {"playstore": "com.android.vending", "files": "com.sec.android.app.myfiles"}
ALARM_EXTRA = "android.intent.extra.alarm"
KEYS = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "enter": "KEYCODE_ENTER",
    "recents": "KEYCODE_APP_SWITCH",
    "wakeup": "KEYCODE_WAKEUP",
    "delete": "KEYCODE_DEL",
    "tab": "KEYCODE_TAB",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
}
MEDIA_EXTENSIONS = {
    "photo": (".jpg", ".jpeg", ".png", ".heic", ".webp"),
    "video": (".mp4", ".mov", ".3gp", ".mkv"),
}
SWITCH_CAMERA = re.compile(r"switch to (front|rear|back) camera", re.IGNORECASE)
MARK_COLOR = (255, 0, 180)


def adb(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
    """Run adb against this phone. Calls adb directly (~100 ms) and only reconnects when a command fails,
    instead of checking the connection before every command (~90 ms extra each)."""
    command = ["adb", "-s", ADB_TARGET, *args]
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    if result.returncode != 0 and ADB_DISCONNECTED.search(result.stderr.decode(errors="replace")):
        subprocess.run(["adb", "connect", ADB_TARGET], capture_output=True, timeout=30)
        result = subprocess.run(command, capture_output=True, timeout=timeout)
    return result


def shell(command: str, timeout: int = 60) -> str:
    result = adb("shell", command, timeout=timeout)
    return (result.stdout + result.stderr).decode(errors="replace").strip()


def getprop(name: str) -> str:
    """Read a system property directly. Works right after boot, before adb is back on port 5555."""
    result = subprocess.run(["/system/bin/getprop", name], capture_output=True, text=True, timeout=10)
    return result.stdout.strip()


def termux(tool: str, *args: str, timeout: int = 60) -> str:
    result = subprocess.run([f"{TERMUX_BIN}/{tool}", *args], capture_output=True, text=True, timeout=timeout)
    return (result.stdout + result.stderr).strip()


def start_activity(action: str, extras: str = "", data: str | None = None) -> str:
    target = f" -d {shlex.quote(data)}" if data else ""
    return shell(f"am start -a {action}{target}{extras}")


def handles(action: str, data: str | None = None) -> bool:
    target = f" -d {shlex.quote(data)}" if data else ""
    return "No activity found" not in shell(f"cmd package resolve-activity --brief -a {action}{target}")


def is_temporary(path: Path) -> bool:
    """Files the agent created only to send (pulled copies, audio recordings)."""
    return path.parent in (MEDIA_DIR, AUDIO_DIR)


def phone_line_problem() -> str | None:
    """Why calls and SMS can't work right now, if they can't."""
    if all(state.strip() in ("ABSENT", "") for state in shell("getprop gsm.sim.state").split(",")):
        return "There is no SIM card in the phone, so calls and SMS are not possible."
    if shell("settings get global airplane_mode_on") == "1":
        return "Airplane mode is on, so calls and SMS are not possible. Ask the owner to turn it off."
    return None


def compress_video(source: Path, start: float = 0, duration: float | None = None, width: int = 960,
                   crf: int = 28, audio: bool = True) -> Path:
    """A smaller copy for sending: uploads from the phone can be slow, and bots can't send over 50 MB.
    Takes ~0.5 s per second of video on the phone."""
    target = source.with_name(f"{source.stem}_small.mp4")
    trim = ["-ss", f"{start:.1f}", *(["-t", f"{duration:.1f}"] if duration else [])]
    sound = ["-c:a", "aac", "-b:a", "64k"] if audio else ["-an"]
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", *trim, "-i", str(source), "-vf", f"scale={width}:-2",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), *sound, "-movflags", "+faststart", str(target)],
        check=True, timeout=600,
    )
    return target


def ready_to_send(path: Path) -> Path:
    """Big videos get a compressed copy; everything else is sent as is."""
    if path.suffix.lower() in MEDIA_EXTENSIONS["video"] and path.stat().st_size > LARGE_VIDEO_BYTES:
        return compress_video(path)
    return path


def motion_moments(video: Path) -> list[float]:
    """Seconds into a clip where a noticeable share of the picture changed since the previous frame."""
    width, height = MOTION_SIZE
    frames = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(video),
         "-vf", f"fps={MOTION_FPS},scale={width}:{height},format=gray", "-f", "rawvideo", "-"],
        capture_output=True, timeout=180,
    ).stdout
    size = width * height
    moments, previous = [], None
    for index in range(len(frames) // size):
        frame = Image.frombytes("L", MOTION_SIZE, frames[index * size:(index + 1) * size])
        seconds = index / MOTION_FPS
        if previous is not None and seconds >= MOTION_SKIP_S:
            changed = ImageChops.difference(frame, previous).point(lambda v: 255 if v > MOTION_PIXEL_DIFF else 0)
            if changed.histogram()[255] / size > MOTION_SHARE:
                moments.append(seconds)
        previous = frame
    return moments


def to_data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=60)
    return f"data:image/jpeg;base64,{base64.b64encode(buffer.getvalue()).decode()}"


@dataclass
class Element:
    label: str
    resource: str
    box: tuple[int, int, int, int]
    flags: str

    @property
    def center(self) -> tuple[int, int]:
        return (self.box[0] + self.box[2]) // 2, (self.box[1] + self.box[3]) // 2


def node_label(node: ET.Element) -> str:
    """A node's own text, or the first text inside it (clickable rows often hold their label in a child)."""
    for child in node.iter("node"):
        label = (child.get("text") or child.get("content-desc") or "").strip()
        if label:
            return label[:80]
    return ""


class Device:
    """The phone. Tap numbers refer to the elements from the latest look()."""

    def __init__(self) -> None:
        self.elements: list[Element] = []
        self.automator = None
        self.apps: dict[str, str] = {}  # package -> its launcher component
        match = re.search(r"(\d+)x(\d+)", shell("wm size"))
        self.width, self.height = (int(match[1]), int(match[2])) if match else (1080, 2340)

    def fast(self):
        """The uiautomator2 connection, or None if unavailable. Reconnects lazily after a failure."""
        if self.automator is None and uiautomator2 is not None:
            self.automator = uiautomator2.connect(ADB_TARGET)
        return self.automator

    def dump_xml(self) -> str:
        try:
            if automator := self.fast():
                return automator.dump_hierarchy()
        except Exception as error:  # server died (e.g. adb restarted); fall back and reconnect next time
            log.warning(f"fast screen read failed, using uiautomator dump: {error}")
            self.automator = None
        adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
        return adb("exec-out", "cat", "/sdcard/ui.xml").stdout.decode(errors="replace")

    def read_elements(self) -> list[Element]:
        xml = self.dump_xml()
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []
        elements, seen = [], set()
        for node in root.iter("node"):
            interactive = any(node.get(flag) == "true" for flag in ("clickable", "long-clickable", "checkable"))
            own_label = (node.get("text") or node.get("content-desc") or "").strip()
            if not interactive and not own_label:
                continue
            bounds = [int(n) for n in re.findall(r"\d+", node.get("bounds", ""))]
            if len(bounds) != 4 or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                continue  # hidden or zero-size
            label = own_label or node_label(node)
            key = (tuple(bounds), label)
            if key in seen:
                continue
            seen.add(key)
            flags = " ".join(f for f in ("checked", "selected", "focused") if node.get(f) == "true")
            resource = (node.get("resource-id") or "").rsplit("/", 1)[-1]
            elements.append(Element(label, resource, tuple(bounds), flags))
        return elements[:MAX_ELEMENTS]

    def snapshot(self, path: Path = SCREEN_PATH) -> Image.Image:
        """Capture the screen and save a clean JPEG copy for sending."""
        image = None
        try:
            if automator := self.fast():
                image = automator.screenshot()
        except Exception as error:
            log.warning(f"fast screenshot failed, using screencap: {error}")
            self.automator = None
        if image is None:
            image = Image.open(io.BytesIO(adb("exec-out", "screencap", "-p").stdout))
        image = image.convert("RGB")
        self.width, self.height = image.size  # authoritative, and follows rotation
        image.save(path, "JPEG", quality=75)
        return image

    def read_stable_elements(self) -> list[Element]:
        """Read the screen until two reads in a row match, so a page transition isn't caught halfway."""
        elements = self.read_elements()
        deadline = time.time() + STABLE_TIMEOUT_S
        while time.time() < deadline:  # a read takes ~250 ms, which is gap enough between the two
            again = self.read_elements()
            if again == elements:
                break
            elements = again
        return elements

    def look(self, with_image: bool) -> tuple[str, str | None]:
        """Number the on-screen elements; return the list and, for vision models, a marked screenshot."""
        self.elements = self.read_stable_elements()
        image = self.snapshot()
        lines = [
            f"[{n}] {e.label or '(no label)'}{f' #{e.resource}' if e.resource else ''} {e.flags}".rstrip()
            for n, e in enumerate(self.elements, 1)
        ]
        listing = "\n".join(lines) or "(no elements found: tap with x/y on the 0-1000 scale)"
        text = f"Elements on screen (tap by number):\n{listing}"
        return text, to_data_url(self.mark(image)) if with_image else None

    def mark(self, image: Image.Image) -> Image.Image:
        """Draw each element's number on the screenshot (Set-of-Mark), skipping full-screen containers."""
        marked = image.copy()
        draw = ImageDraw.Draw(marked)
        font = ImageFont.load_default(size=30)
        screen_area = self.width * self.height
        for number, element in enumerate(self.elements, 1):
            x1, y1, x2, y2 = element.box
            if (x2 - x1) * (y2 - y1) > screen_area * 0.5:
                continue
            draw.rectangle(element.box, outline=MARK_COLOR, width=3)
            tag = str(number)
            width = draw.textlength(tag, font=font) + 10
            draw.rectangle((x1, y1, x1 + width, y1 + 36), fill=MARK_COLOR)
            draw.text((x1 + 5, y1 + 2), tag, fill="white", font=font)
        return marked

    def find_text(self, text: str) -> int | None:
        """Number of the element whose label matches text exactly, else contains it as whole words
        (so "Stop" never matches "Stopwatch")."""
        self.elements = self.read_elements()
        wanted = text.strip().lower()
        whole_words = re.compile(rf"(?<!\w){re.escape(wanted)}(?!\w)")
        labels = [e.label.lower() for e in self.elements]
        for matches in (
            lambda label: label == wanted,
            lambda label: whole_words.search(label) is not None,
        ):
            for number, label in enumerate(labels, 1):
                if label and matches(label):
                    return number
        return None

    def tap(
        self, element: int | None, x: float | None, y: float | None, hold_ms: int | None, text: str | None = None
    ) -> str:
        if text:
            element = self.find_text(text)
            if element is None:
                return f"No element with text {text!r} on screen. Call look to see what is there."
        if element:
            if not 1 <= element <= len(self.elements):
                return f"There is no element {element}. Call look to refresh the numbers."
            target = self.elements[element - 1]
            px, py = target.center
            what = f"[{element}] {target.label or '(no label)'}"
        elif x is not None and y is not None:
            px, py = round(x / 1000 * self.width), round(y / 1000 * self.height)
            what = f"point ({x:.0f},{y:.0f}) on the 0-1000 scale"
        else:
            return "Give an element number, or x and y on the 0-1000 scale."
        if hold_ms:
            shell(f"input swipe {px} {py} {px} {py} {int(hold_ms)}")
        else:
            shell(f"input tap {px} {py}")
        time.sleep(ACTION_PAUSE_S)
        return f"{'Long-pressed' if hold_ms else 'Tapped'} {what}."

    def type_text(self, text: str, submit: bool) -> str:
        if not text.isascii():
            return "Only plain ASCII text can be typed with adb."
        escaped = text.replace("'", "'\\''").replace(" ", "%s")
        shell(f"input text '{escaped}'")
        if submit:
            shell("input keyevent KEYCODE_ENTER")
        return f"Typed {text!r}{' and pressed Enter' if submit else ''}."

    def scroll(self, direction: str) -> str:
        cx, top, bottom = self.width // 2, int(self.height * 0.3), int(self.height * 0.7)
        left, right, cy = int(self.width * 0.2), int(self.width * 0.8), self.height // 2
        moves = {  # finger moves opposite to the content you want to reveal
            "down": (cx, bottom, cx, top),
            "up": (cx, top, cx, bottom),
            "right": (right, cy, left, cy),
            "left": (left, cy, right, cy),
        }
        if direction not in moves:
            return "Direction must be up, down, left or right."
        shell("input swipe {} {} {} {} 400".format(*moves[direction]))
        time.sleep(ACTION_PAUSE_S)
        return f"Scrolled {direction}."

    def wifi_network(self) -> str | None:
        """Name of the Wi-Fi network the phone is on, or None."""
        match = re.search(r'connected to "([^"]*)"', shell("cmd wifi status"))
        return match[1] if match else None

    def ensure_wireless_debugging(self) -> str | None:
        """Keep Wireless debugging on, approving Android's "Allow wireless debugging on this network?"
        prompt on a new network. Recovery after a reboot needs it, and nobody can tap that prompt then.
        Only that exact prompt is answered, never "Allow USB debugging?". Returns a note if it acted."""
        if shell("settings get global adb_wifi_enabled") == "1":
            return None
        network = self.wifi_network()
        if network is None:
            return None  # wireless debugging needs Wi-Fi; nothing to do on mobile data or offline
        self.wake()
        shell("settings put global adb_wifi_enabled 1")
        time.sleep(2)
        if shell("settings get global adb_wifi_enabled") == "1":
            return None  # this network was already trusted
        elements = self.read_elements()
        if not any(e.label == WIRELESS_DEBUG_PROMPT for e in elements):
            return f"Couldn't turn on wireless debugging on {network!r} (no prompt appeared)."
        for label in ("Always allow on this network", "Allow"):
            target = next((e for e in elements if e.label == label), None)
            if target is None:
                return f"Couldn't find {label!r} on the wireless debugging prompt for {network!r}."
            shell("input tap {} {}".format(*target.center))
            time.sleep(1)
        time.sleep(2)
        if shell("settings get global adb_wifi_enabled") != "1":
            return f"Tried to trust {network!r} for wireless debugging, but it didn't stick."
        return f"Trusted the new Wi-Fi {network!r} for wireless debugging, so I can recover after a reboot there."

    def join_wifi(self, name: str, password: str) -> str:
        security = f"wpa2 {shlex.quote(password)}" if password else "open"
        output = shell(f"cmd wifi connect-network {shlex.quote(name)} {security}")
        for _ in range(15):
            time.sleep(2)
            if self.wifi_network() == name:
                return f"Connected to {name!r}."
        return f"Couldn't connect to {name!r} (wrong password or out of range?). {output}".strip()

    def is_awake(self) -> bool:
        return "mWakefulness=Awake" in shell("dumpsys power | grep mWakefulness=")

    def wake(self) -> None:
        """Turn the screen on if it's off; reading and tapping the screen need it on."""
        if not self.is_awake():
            shell("input keyevent KEYCODE_WAKEUP")
            time.sleep(0.5)

    def screen_off(self) -> str:
        shell("input keyevent KEYCODE_SLEEP")
        return "Screen off."

    def screen_on(self) -> str:
        shell("input keyevent KEYCODE_WAKEUP")
        return "Screen on."

    def key(self, name: str) -> str:
        code = KEYS.get(name.lower())
        if not code:
            return f"Unknown key. Use one of: {', '.join(KEYS)}."
        shell(f"input keyevent {code}")
        time.sleep(ACTION_PAUSE_S)
        return f"Pressed {name}."

    def launchable(self, refresh: bool = False) -> dict[str, str]:
        """Packages with a home-screen icon and their launcher component, shortest name first; cached
        (refreshed when a lookup misses, e.g. after an install)."""
        if refresh or not self.apps:
            output = shell(f"cmd package query-activities --brief -a android.intent.action.MAIN -c {LAUNCHER}")
            components = {line.strip().split("/")[0]: line.strip() for line in output.splitlines() if "/" in line}
            self.apps = dict(sorted(components.items(), key=lambda item: len(item[0])))
        return self.apps

    def open_app(self, name: str) -> str:
        query = re.sub(r"\s+", "", name.lower())
        package = APP_ALIASES.get(query)
        if package is None:
            for refresh in (False, True):
                matches = [p for p in self.launchable(refresh) if query in p.lower()]
                if matches:
                    package = matches[0]  # shortest name is usually the main app
                    break
        if package is None:
            return f"No app with a home-screen icon matches {name!r}. Try another name."
        # am start is ~0.4 s quicker than monkey, and the look that follows waits for the app to finish opening.
        # monkey stays as the fallback for packages that aren't in the launcher list.
        component = self.launchable().get(package)
        # The same intent and flags a launcher uses, so an app already open comes back where it was.
        output = shell(f"am start -a android.intent.action.MAIN -c {LAUNCHER} -f 0x10200000 -n {shlex.quote(component)}") if component else ""
        if not component or "Error" in output:
            shell(f"monkey -p {package} -c {LAUNCHER} 1")
            time.sleep(1.5)
        return f"Opened {package}."

    def newest_camera_file(self) -> str:
        return shell(f"ls -t {CAMERA_DIR} 2>/dev/null | head -1")

    def face_camera(self, front: bool) -> None:
        """The camera app ignores facing hints in intents and remembers the last lens, so flip it via its button."""
        for element in self.read_elements():
            match = SWITCH_CAMERA.search(element.label)
            if match and (match[1].lower() == "front") == front:
                shell("input tap {} {}".format(*element.center))
                time.sleep(2)
                return

    def capture(self, video: bool, seconds: int, front: bool) -> str:
        """Use the camera app with the volume key as shutter; return the new file's path on the phone."""
        before = self.newest_camera_file()
        shell("input keyevent KEYCODE_WAKEUP")
        shell(f"am start -a android.media.action.{'VIDEO_CAMERA' if video else 'STILL_IMAGE_CAMERA'}")
        time.sleep(3)
        self.face_camera(front)
        shell("input keyevent KEYCODE_VOLUME_DOWN")
        if video:
            time.sleep(seconds)
            shell("input keyevent KEYCODE_VOLUME_DOWN")
        extensions = MEDIA_EXTENSIONS["video" if video else "photo"]
        for _ in range(SAVE_WAIT_S):  # the file appears only after the camera finishes processing
            time.sleep(1)
            newest = self.newest_camera_file()
            if newest and newest != before and newest.lower().endswith(extensions):
                return f"{CAMERA_DIR}/{newest}"
        raise RuntimeError("Capture failed: no new file appeared. Call look to see what the camera app is showing.")

    def record_video(self, seconds: int, front: bool) -> str:
        seconds = max(1, min(seconds, MAX_VIDEO_S))
        path = self.capture(video=True, seconds=seconds, front=front)
        return f"Saved {seconds}s video: {path}. Use send_file to send it."

    def take_photo(self, front: bool) -> str:
        return f"Saved photo: {self.capture(video=False, seconds=0, front=front)}. Use send_file to send it."

    def record_audio_file(self, seconds: int) -> Path:
        """Record from the microphone only (no camera) using Termux:API; return the local file."""
        AUDIO_DIR.mkdir(exist_ok=True)
        path = AUDIO_DIR / f"audio_{time.strftime('%Y%m%d_%H%M%S')}.m4a"
        options = ["-l", str(seconds), "-e", "aac", "-b", "96", "-r", "44100"]
        started = subprocess.run([MIC_RECORDER, "-f", str(path), *options], capture_output=True, text=True, timeout=30)
        if "Recording started" not in started.stdout:
            raise RuntimeError(f"Could not start recording: {(started.stdout + started.stderr).strip()}")
        time.sleep(seconds)
        # The recorder stops itself at the limit. An .m4a is complete once its index ("moov") is written
        # at the end, so wait for that instead of making a 4s stop call.
        for _ in range(20):
            if path.exists() and b"moov" in path.read_bytes():
                return path
            time.sleep(0.5)
        raise RuntimeError("Recording failed: no complete audio file was written.")

    def record_audio(self, seconds: int) -> str:
        seconds = max(1, min(seconds, MAX_AUDIO_S))
        return f"Saved {seconds}s audio: {self.record_audio_file(seconds)}. Use send_file to send it."

    def battery_status(self) -> tuple[int, bool] | None:
        """(level %, charging or full). Uses adb, or Termux:API if adb isn't available (e.g. just after a reboot)."""
        info = dict(re.findall(r"^\s+([\w ]+): (.+)$", shell("dumpsys battery"), re.MULTILINE))
        if info.get("level", "").isdigit():
            return int(info["level"]), info.get("status") in ("2", "5")
        try:
            data = json.loads(termux("termux-battery-status"))
            return int(data["percentage"]), data["status"] in ("CHARGING", "FULL")
        except (ValueError, KeyError, subprocess.TimeoutExpired):
            return None

    def battery(self) -> str:
        info = dict(re.findall(r"^\s+([\w ]+): (.+)$", shell("dumpsys battery"), re.MULTILINE))
        charging = "charging" if info.get("status") == "2" else "not charging"
        if info.get("status") == "5":
            charging = "full"
        temperature = int(info.get("temperature", "0")) / 10
        return f"Battery {info.get('level', '?')}%, {charging}, {temperature:.1f}°C"

    def set_alarm(self, hour: int, minute: int, label: str) -> str:
        if not handles("android.intent.action.SET_ALARM"):
            return "No clock app is installed, so alarms can't be set. A clock app (e.g. Google Clock) must be installed first."
        extras = (
            f" --ei {ALARM_EXTRA}.HOUR {hour} --ei {ALARM_EXTRA}.MINUTES {minute}"
            f" --es {ALARM_EXTRA}.MESSAGE {shlex.quote(label or 'Alarm')} --ez {ALARM_EXTRA}.SKIP_UI true"
        )
        start_activity("android.intent.action.SET_ALARM", extras)
        return f"Alarm set for {hour:02d}:{minute:02d}."

    def set_timer(self, seconds: int, label: str) -> str:
        if not handles("android.intent.action.SET_TIMER"):
            return "No clock app is installed, so timers can't be set. A clock app (e.g. Google Clock) must be installed first."
        extras = (
            f" --ei {ALARM_EXTRA}.LENGTH {seconds}"
            f" --es {ALARM_EXTRA}.MESSAGE {shlex.quote(label or 'Timer')} --ez {ALARM_EXTRA}.SKIP_UI true"
        )
        start_activity("android.intent.action.SET_TIMER", extras)
        return f"Timer started for {seconds}s."

    def open_url(self, url: str) -> str:
        if urlparse(url).scheme not in ("http", "https"):
            return "Only http and https links can be opened."
        start_activity("android.intent.action.VIEW", data=url)
        time.sleep(2)
        return f"Opened {url}."

    def status(self) -> str:
        """Volumes, brightness, display, connectivity and battery in one adb call (~0.7 s), so the AI doesn't dig
        through dumpsys output over many steps."""
        output = shell(STATUS_SCRIPT)
        values = dict(re.findall(r"^(\w+)=(.*)$", output, re.MULTILINE))
        volumes = []
        for name, stream in DUMPSYS_STREAMS.items():
            block = re.search(rf"^- STREAM_{stream}:\n((?:   .*\n?)*)", output, re.MULTILINE)
            level = re.search(r"streamVolume:(\d+)", block[1]) if block else None
            top = re.search(r"Max: (\d+)", block[1]) if block else None
            if level and top:
                volumes.append(f"{name} {round(100 * int(level[1]) / int(top[1]))}%")
        brightness, timeout = values.get("brightness", ""), values.get("timeout", "")
        wifi = re.search(r'connected to "([^"]*)"', output)
        level = re.search(r"^  level: (\d+)", output, re.MULTILINE)
        charging = re.search(r"^  status: ([25])$", output, re.MULTILINE)

        def on(key: str) -> str:
            return "on" if values.get(key) == "1" else "off"

        return "\n".join([
            f"Volume: {', '.join(volumes) or 'unknown'}; ringer {values.get('ringer', '?').lower()}",
            f"Brightness: {round(100 * int(brightness) / 255) if brightness.isdigit() else '?'}%"
            f"{' (auto)' if values.get('auto_brightness') == '1' else ''}",
            f"Screen timeout: {int(timeout) // 1000 if timeout.isdigit() else '?'} s; auto-rotate: {on('rotate')}; "
            f"dark mode: {'on' if values.get('night', '').endswith('yes') else 'off'}",
            f"Wi-Fi: {wifi[1] if wifi else 'not connected'}",
            f"Airplane mode: {on('airplane')}; Bluetooth: {on('bluetooth')}; "
            f"Do not disturb: {'off' if values.get('zen') in ('0', None) else 'on'}",
            f"Battery: {f'{level[1]}%, ' + ('charging' if charging else 'not charging') if level else 'unknown'}",
        ])

    def toggle(self, setting: str, on: bool) -> str:
        commands = TOGGLES.get(setting)
        if commands is None:
            return f"Setting must be one of: {', '.join(TOGGLES)}."
        shell(commands[0 if on else 1])
        return f"{setting.replace('_', ' ').capitalize()} turned {'on' if on else 'off'}."

    def set_volume(self, percent: int, stream: str) -> str:
        stream_id = VOLUME_STREAMS.get(stream)
        if stream_id is None:
            return f"Stream must be one of: {', '.join(VOLUME_STREAMS)}."
        current = shell(f"cmd media_session volume --stream {stream_id} --get")
        match = re.search(r"range \[(\d+)\.\.(\d+)\]", current)
        low, high = (int(match[1]), int(match[2])) if match else (0, 15)
        level = round(low + (high - low) * max(0, min(percent, 100)) / 100)
        shell(f"cmd media_session volume --stream {stream_id} --set {level}")
        return f"{stream.capitalize()} volume set to {percent}% ({level}/{high})."

    def set_brightness(self, percent: int) -> str:
        value = max(1, round(255 * max(0, min(percent, 100)) / 100))
        shell("settings put system screen_brightness_mode 0")
        shell(f"settings put system screen_brightness {value}")
        return f"Brightness set to {percent}%."

    def speak(self, text: str) -> str:
        # Termux:API takes ~5s to start; speak in the background instead of making the caller wait.
        threading.Thread(target=termux, args=("termux-tts-speak", text), kwargs={"timeout": 120}, daemon=True).start()
        return f"Speaking: {text!r}"

    def search_contacts(self, query: str) -> str:
        try:
            contacts = json.loads(termux("termux-contact-list") or "[]")
        except json.JSONDecodeError:
            return "Could not read contacts. Termux:API may need the contacts permission."
        wanted = query.lower()
        found = [c for c in contacts if wanted in c.get("name", "").lower() or wanted in c.get("number", "")]
        return "\n".join(f"{c.get('name')}: {c.get('number')}" for c in found[:10]) or f"No contact matches {query!r}."

    def make_call(self, number: str) -> str:
        if problem := phone_line_problem():
            return problem
        if not PHONE_NUMBER.fullmatch(number.strip()):
            return "That doesn't look like a phone number."
        start_activity("android.intent.action.CALL", data=f"tel:{number.strip()}")
        return f"Calling {number}."

    def send_sms(self, number: str, text: str) -> str:
        if problem := phone_line_problem():
            return problem
        if not PHONE_NUMBER.fullmatch(number.strip()):
            return "That doesn't look like a phone number."
        output = termux("termux-sms-send", "-n", number.strip(), text)
        return f"SMS sent to {number}." + (f" ({output})" if output else "")

    def compose_email(self, to: str, subject: str, body: str) -> str:
        data = f"mailto:{to.strip()}"
        if not handles("android.intent.action.SENDTO", data):
            return "No email app can send mail on this phone."
        extras = f" --es android.intent.extra.SUBJECT {shlex.quote(subject)} --es android.intent.extra.TEXT {shlex.quote(body)}"
        start_activity("android.intent.action.SENDTO", extras, data)
        time.sleep(3)
        return "The email is written in the mail app. Call look, then tap its Send button to send it."

    def find_media(self, kind: str, count: int) -> str:
        extensions = MEDIA_EXTENSIONS.get(kind, MEDIA_EXTENSIONS["photo"] + MEDIA_EXTENSIONS["video"])
        found = []
        for folder in (CAMERA_DIR, SCREENSHOT_DIR):
            for name in shell(f"ls -t {folder} 2>/dev/null").splitlines():
                if name.lower().endswith(extensions):
                    found.append(f"{folder}/{name}")
        found.sort(key=lambda path: path.rsplit("/", 1)[-1], reverse=True)  # names start with the date
        return "\n".join(found[: max(1, min(count, 20))]) or f"No {kind} files found."

    def fetch(self, path: str) -> Path:
        """Return a local copy of a file, pulling it from the phone's storage if needed."""
        if not path.startswith(("/sdcard", "/storage")):
            return Path(path)
        MEDIA_DIR.mkdir(exist_ok=True)
        local = MEDIA_DIR / Path(path).name
        result = adb("pull", path, str(local), timeout=300)
        if result.returncode != 0:
            raise FileNotFoundError(result.stderr.decode(errors="replace").strip() or path)
        return local
