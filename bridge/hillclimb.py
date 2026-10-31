"""Hill Climb Racing autopilot: drives the car from the live screen and taps through the menus between runs.

Runs on the phone through scrcpy (see scrcpy.py): ~30 frames a second in, touches out within a few ms.
The car is found by its red body; its tilt (the body's long axis) decides the pedals. In the air, gas
turns the nose up and brake turns it down, so the same rule also lands jumps.

Menus are only tapped when the screen matches a saved picture of that exact button (hillclimb_ui/),
and only while the game is the app in front. Anything else (offers, ads, store pages) counts as unknown
and is never tapped: a looser colour check once took a "Starter pack" offer for the menu and opened
Google Play's payment page. Positions were measured on the Galaxy M34 (2340x1080, landscape) with a
640x294 video, which the saved pictures need.
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np
from PIL import Image

from device import shell
from scrcpy import DOWN, UP, Screen

log = logging.getLogger("hillclimb")

PACKAGE = "com.fingersoft.hillclimb"
VIDEO_MAX_SIZE = 640  # the saved button pictures are this scale
FRAME_SIZE = (640, 294)
UI_DIR = Path(__file__).parent / "hillclimb_ui"
# Saved picture name -> its top-left corner in the 640x294 frame.
TEMPLATES = {
    "menu_start": (432, 182),  # with the Tune tab open: START gets a white frame
    "menu_start_stage": (432, 182),  # with the Stage tab open
    "start_regular": (268, 249),
    "second_chance": (232, 32),
    "touch_to_continue": (338, 177),  # blinks; matches in its bright phase
    "pause": (582, 10),
    "paused": (296, 24),  # the pause menu's title: going home mid-run pauses the game
    "gas_label": (548, 255),  # labels, not the pedals: their holes show the scenery behind
    "brake_label": (48, 255),
}
TEMPLATE_SHIFT = 2  # pixels; the video scaler can place an edge a pixel or so off
TEMPLATE_MATCH = 15  # mean grey difference; the same screen scores 0-10, any other 27 or more
GAS_FINGER, BRAKE_FINGER, TAP_FINGER = 1, 2, 3
# Points as fractions of the frame (x, y).
GAS = (0.887, 0.816)
BRAKE = (0.109, 0.816)
CLOSE_SECOND_CHANCE = (0.672, 0.129)  # the X; the big green button would play an ad
TOUCH_TO_CONTINUE = (0.656, 0.850)
START_REGULAR = (0.484, 0.881)
MENU_START = (0.728, 0.673)
RESUME = (0.5, 0.279)
# Car tracking and driving.
SEARCH_AREA = (0.0, 0.15, 0.5, 0.68)  # the camera keeps the car in the left half, between HUD and pedals
TRACK_WINDOW = (0.11, 0.15)  # half width and half height around the last position
MIN_CAR_PIXELS = 80
MAX_GAP = 3  # pixels; the body has thin dark outlines that shouldn't split it
MAX_TURN_PER_FRAME = 35  # degrees; a bigger jump is a misread, not the car
MERGED_GROWTH = 1.2  # this many times the car's usual pixel count means something red touches it
MAX_REJECTED = 6  # misreads in a row; past this the new reading is the car (it really turned, or the old one was wrong)
LOOKAHEAD_S = 0.25  # react to where the tilt is heading (covers screen-to-touch latency)
COAST_ABOVE = 30  # nose-up degrees: let go of the gas
BRAKE_ABOVE = 50  # nose-up degrees: brake to turn the nose back down
MENU_CHECK_S = 0.5
MENU_SETTLE_S = 1.5  # after a menu tap, let the next screen come in before judging it
TAP_HOLD_S = 0.08
STUCK_S = 20  # an unknown screen this long stops the autopilot
WAKE_EVERY_S = 3


def grey(frame: np.ndarray) -> np.ndarray:
    """Luma with the same weights as PIL's "L" mode, which the saved pictures use."""
    return (frame[..., 0] * 0.299 + frame[..., 1] * 0.587 + frame[..., 2] * 0.114).astype(np.int16)


def load_templates() -> dict[str, np.ndarray]:
    return {name: np.array(Image.open(UI_DIR / f"{name}.png").convert("L"), dtype=np.int16) for name in TEMPLATES}


PICTURES = load_templates()


def shows(frame_grey: np.ndarray, name: str) -> bool:
    """Whether the frame shows the saved picture `name` at its place (within a pixel or two)."""
    picture, (x, y) = PICTURES[name], TEMPLATES[name]
    height, width = picture.shape
    best = min(
        np.abs(frame_grey[y + dy: y + dy + height, x + dx: x + dx + width] - picture).mean()
        for dy in range(-TEMPLATE_SHIFT, TEMPLATE_SHIFT + 1)
        for dx in range(-TEMPLATE_SHIFT, TEMPLATE_SHIFT + 1)
    )
    return best < TEMPLATE_MATCH


def game_in_front() -> bool:
    return f" {PACKAGE}/" in shell("dumpsys activity activities | grep -m1 topResumedActivity")


class View(str, Enum):
    DRIVING = "driving"
    SECOND_CHANCE = "second chance"  # revive offer after a run; reviving means watching an ad
    RESULTS = "results"  # "Out of fuel" / "Driver down", touch to continue
    RAIN_POPUP = "rain popup"  # "Heavy rain mode" offer before a run
    MENU = "menu"
    PAUSED = "paused"
    UNKNOWN = "unknown"


def view_of(frame: np.ndarray) -> View:
    if frame.shape[1::-1] != FRAME_SIZE:
        return View.UNKNOWN  # portrait (another app in front) or a scale the pictures don't fit
    frame_grey = grey(frame)
    # A pressed pedal shrinks, so one unpressed label is enough.
    if shows(frame_grey, "pause") and (shows(frame_grey, "gas_label") or shows(frame_grey, "brake_label")):
        return View.DRIVING
    if shows(frame_grey, "second_chance"):
        return View.SECOND_CHANCE
    if shows(frame_grey, "touch_to_continue"):
        return View.RESULTS
    if shows(frame_grey, "start_regular"):
        return View.RAIN_POPUP
    if shows(frame_grey, "menu_start") or shows(frame_grey, "menu_start_stage"):
        return View.MENU
    if shows(frame_grey, "paused"):
        return View.PAUSED
    return View.UNKNOWN


def next_tap(view: View) -> tuple[float, float] | None:
    """Where to tap to get from a menu screen towards the next run."""
    if view == View.RESULTS:
        return TOUCH_TO_CONTINUE
    if view == View.SECOND_CHANCE:
        return CLOSE_SECOND_CHANCE
    if view == View.RAIN_POPUP:
        return START_REGULAR
    if view == View.MENU:
        return MENU_START
    if view == View.PAUSED:
        return RESUME
    return None


def red_mask(frame: np.ndarray) -> np.ndarray:
    r, g, b = (frame[..., i].astype(np.int16) for i in range(3))
    return (r > 110) & (r > 2 * g + 20) & (r > 2 * b + 20)


def biggest_run(counts: np.ndarray) -> slice:
    """The stretch of non-empty rows or columns holding the most pixels (gaps up to MAX_GAP are bridged)."""
    best, best_total, start, total, gap = slice(0, 0), 0, None, 0, 0
    for index, count in enumerate([*counts, 0] + [0] * MAX_GAP):
        if count:
            start = index if start is None else start
            total, gap = total + count, 0
        elif start is not None:
            gap += 1
            if gap > MAX_GAP:
                if total > best_total:
                    best, best_total = slice(start, index - gap + 1), total
                start, total, gap = None, 0, 0
    return best


def largest_blob(mask: np.ndarray) -> np.ndarray:
    """Keep only the biggest group of pixels: a fuel can near the car is a separate, smaller group."""
    columns = biggest_run(mask.sum(axis=0))
    kept = np.zeros_like(mask)
    kept[:, columns] = mask[:, columns]
    rows = biggest_run(kept.sum(axis=1))
    blob = np.zeros_like(mask)
    blob[rows] = kept[rows]
    return blob


@dataclass
class Pilot:
    """Tracks the car's tilt from frame to frame and chooses the pedals."""

    position: tuple[float, float] | None = None
    angle: float = 0.0
    pixels: float = 0.0  # the car's usual size in pixels, smoothed
    history: list[tuple[float, float]] = field(default_factory=list)  # (time, angle)
    rejected: int = 0

    def car(self, frame: np.ndarray) -> tuple[float, float, float, int] | None:
        """(x, y, tilt in degrees with nose up positive, pixel count) of the car body, or None."""
        h, w = frame.shape[:2]
        mask = red_mask(frame)
        keep = np.zeros_like(mask)
        x0, y0, x1, y1 = SEARCH_AREA
        if self.position:
            px, py = self.position
            x0, x1 = max(x0, px / w - TRACK_WINDOW[0]), min(x1, px / w + TRACK_WINDOW[0])
            y0, y1 = max(y0, py / h - TRACK_WINDOW[1]), min(y1, py / h + TRACK_WINDOW[1])
        keep[int(y0 * h): int(y1 * h), int(x0 * w): int(x1 * w)] = True
        ys, xs = np.nonzero(largest_blob(mask & keep))
        if len(xs) < MIN_CAR_PIXELS:
            return None
        x, y = xs.mean(), ys.mean()
        _, vectors = np.linalg.eigh(np.cov(np.stack([xs - x, ys - y])))
        vx, vy = vectors[:, 1]
        axis = float(np.degrees(np.arctan2(-vy, vx)))
        # The long axis has no front or back: pick the reading closest to the last one, so a car
        # past vertical reads as 100 degrees, not -80.
        angle = min((axis, axis + 180, axis - 180), key=lambda a: abs(a - self.angle))
        return float(x), float(y), angle, len(xs)

    def update(self, frame: np.ndarray, now: float) -> tuple[bool, bool]:
        """(gas, brake) for this frame."""
        found = self.car(frame)
        if found is None:
            self.position = None  # search the whole area again next frame
            return True, False
        x, y, angle, pixels = found
        # A fuel can touching the car merges with it for a few frames (same red); its extra pixels
        # give it away, since the car's own size only changes slowly as the camera zooms.
        merged = self.pixels and pixels > self.pixels * MERGED_GROWTH
        misread = self.history and (merged or abs(angle - self.angle) > MAX_TURN_PER_FRAME)
        if misread and self.rejected < MAX_REJECTED:
            self.rejected += 1
            return self.pedals(self.angle)
        if misread:
            self.history = []
        self.rejected = 0
        self.pixels = pixels if not self.pixels or merged else self.pixels * 0.8 + pixels * 0.2
        self.position, self.angle = (x, y), angle
        self.history = [(t, a) for t, a in self.history if now - t < 0.3] + [(now, angle)]
        first_time, first_angle = self.history[0]
        rate = (angle - first_angle) / (now - first_time) if now > first_time else 0.0
        return self.pedals(angle + rate * LOOKAHEAD_S)

    @staticmethod
    def pedals(tilt: float) -> tuple[bool, bool]:
        if tilt > BRAKE_ABOVE:
            return False, True
        if tilt > COAST_ABOVE:
            return False, False
        return True, False

    def reset(self) -> None:
        self.position, self.angle, self.pixels, self.history, self.rejected = None, 0.0, 0.0, [], 0


class Autopilot:
    """Plays runs until stopped or out of time. One run = start, drive, results screen."""

    def __init__(self, screen: Screen, width: int, height: int) -> None:
        self.screen = screen
        self.width, self.height = width, height  # the phone's screen in landscape
        self.pilot = Pilot()
        self.pressed = {GAS_FINGER: False, BRAKE_FINGER: False}
        self.runs = 0
        self.driving_s = 0.0

    def point(self, spot: tuple[float, float]) -> tuple[float, float]:
        return spot[0] * self.width, spot[1] * self.height

    def press(self, finger: int, spot: tuple[float, float], down: bool) -> None:
        if self.pressed[finger] != down:
            self.screen.touch(finger, DOWN if down else UP, *self.point(spot))
            self.pressed[finger] = down

    def release(self) -> None:
        self.press(GAS_FINGER, GAS, False)
        self.press(BRAKE_FINGER, BRAKE, False)

    def tap(self, spot: tuple[float, float]) -> None:
        x, y = self.point(spot)
        self.screen.touch(TAP_FINGER, DOWN, x, y)
        time.sleep(TAP_HOLD_S)
        self.screen.touch(TAP_FINGER, UP, x, y)

    def portrait(self) -> bool:
        width, height = self.screen.size
        return width < height

    def drive(self, stop: threading.Event, deadline: float) -> None:
        """Hold the pedals frame by frame until the run ends."""
        self.pilot.reset()
        count, started = self.screen.frame_count, time.time()
        while not stop.is_set() and time.time() < deadline:
            frame = self.screen.wait_frame(after=count, timeout=2)
            if frame is None or self.portrait() or view_of(frame) != View.DRIVING:
                break
            count, now = self.screen.frame_count, time.time()
            gas, brake = self.pilot.update(frame, now)
            self.press(GAS_FINGER, GAS, gas)
            self.press(BRAKE_FINGER, BRAKE, brake)
        self.release()
        self.driving_s += time.time() - started

    def play(self, stop: threading.Event, seconds: float) -> str:
        """Play until `stop` is set or time runs out; returns a summary, or why it gave up."""
        deadline = time.time() + seconds
        unknown_since: float | None = None
        last_wake, drove = 0.0, False
        while not stop.is_set() and time.time() < deadline:
            frame = self.screen.wait_frame(after=self.screen.frame_count, timeout=MENU_CHECK_S)
            if frame is None:
                if self.screen.closed.is_set():
                    return self.summary("the screen stream stopped")
                frame = self.screen.frame
            if self.portrait():
                return self.summary("something else came to the front (the screen turned portrait), so I stopped")
            view = view_of(frame)
            if view == View.RESULTS and drove:  # the blinking text can match several times per screen
                drove = False
                self.runs += 1
                log.info(f"run {self.runs} finished")
            if view == View.DRIVING:
                unknown_since = None
                self.drive(stop, deadline)
                drove = True
            elif spot := next_tap(view):
                unknown_since = None
                if not game_in_front():
                    return self.summary("Hill Climb Racing is no longer the app in front, so I stopped")
                # The check above takes a moment; only tap if the same screen is still showing.
                fresh = self.screen.wait_frame(after=self.screen.frame_count, timeout=MENU_CHECK_S)
                if fresh is not None and not self.portrait() and view_of(fresh) == view:
                    self.tap(spot)
                    time.sleep(MENU_SETTLE_S)
            else:
                now = time.time()
                if now - last_wake > WAKE_EVERY_S:
                    self.screen.wake()  # in case the screen went to sleep; does nothing when it's on
                    last_wake = now
                unknown_since = unknown_since or now
                if now - unknown_since > STUCK_S:
                    return self.summary("stuck on a screen I don't know (an ad or a new popup?)")
        return self.summary("stopped" if stop.is_set() else "time is up")

    def summary(self, reason: str) -> str:
        self.release()
        minutes = self.driving_s / 60
        return f"Hill Climb Racing: {reason}. {self.runs} run(s) finished, {minutes:.1f} min of driving."
