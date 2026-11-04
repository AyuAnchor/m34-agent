"""Playing Hill Climb Racing in the background (the autopilot is in hillclimb.py).

The autopilot needs numpy and the scrcpy server, so it's imported only when a game starts.
"""
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from device import Device, shell
from watch import Watch, describe

log = logging.getLogger("game")

MIN_PLAY_S = 60
MAX_PLAY_S = 3 * 3600  # a long session drains the battery
DEVICE_WAIT_S = 60  # a task or command finishing up
LAUNCH_WAIT_S = 20
LAUNCH_POLL_S = 1


def missing_parts() -> str | None:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return "Playing needs numpy. On the phone, in Debian, run: ~/venv/bin/pip install numpy"
    from scrcpy import LOCAL_SERVER, SERVER_VERSION
    if not LOCAL_SERVER.exists():
        return f"Playing needs the scrcpy server v{SERVER_VERSION} at {LOCAL_SERVER} on the phone. See the README."
    return None


class Game(Watch):
    """One game session at a time. It holds the screen throughout, so tasks and commands wait or report busy."""

    name = "game"

    def __init__(self, device: Device, lock: threading.Lock, send_text: Callable[[str], None],
                 send_file: Callable[[Path, str], None]) -> None:
        super().__init__(device, send_text, send_file)
        self.lock = lock

    def start(self, seconds: int) -> str:
        if self.active:
            return f"Already {self.summary}. Send /stop to end it."
        if problem := missing_parts():
            return problem
        from hillclimb import PACKAGE
        if PACKAGE not in shell(f"pm list packages {PACKAGE}"):
            return "Hill Climb Racing isn't installed. Install it from the Play Store first."
        seconds = max(MIN_PLAY_S, min(seconds, MAX_PLAY_S))
        self.begin(f"playing Hill Climb Racing for {describe(seconds)}", self.run, seconds)
        return f"Started {self.summary}. I'll message you when I stop; /stop ends it early."

    def stop(self) -> str:
        if not self.active:
            return "No game is running."
        self.stopping.set()
        return "Stopping the game."

    def run(self, seconds: int) -> None:
        from hillclimb import PACKAGE, VIDEO_MAX_SIZE, Autopilot, game_in_front
        from scrcpy import Screen
        if not self.lock.acquire(timeout=DEVICE_WAIT_S):
            self.send_text("The game didn't start: a task or watch is using the phone.")
            return
        screen = Screen(max_size=VIDEO_MAX_SIZE)
        try:
            self.device.wake()
            shell(f"monkey -p {PACKAGE} -c android.intent.category.LAUNCHER 1")
            deadline = time.time() + LAUNCH_WAIT_S
            while not game_in_front():
                if time.time() > deadline or self.stopping.wait(LAUNCH_POLL_S):
                    self.send_text("The game didn't start: Hill Climb Racing didn't come to the front.")
                    return
            width, height = max(self.device.width, self.device.height), min(self.device.width, self.device.height)
            screen.start(width, height)
            result = Autopilot(screen, width, height).play(self.stopping, seconds)
        finally:
            screen.close()
            shell("input keyevent KEYCODE_HOME")  # pauses the game, so a stopped run doesn't roll on
            self.lock.release()
        log.info(result)
        self.send_text(result)
