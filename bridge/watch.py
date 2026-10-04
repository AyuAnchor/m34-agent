"""Motion watch: record short clips with the camera app, compare frames, and alert the owner on movement.

Android blocks background camera access, so frames come from the camera app's own video recording.
"""
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from device import Device, compress_video, motion_moments, shell

log = logging.getLogger("watch")

CHUNK_S = 15  # clip length; a ~7 s gap follows each clip while the camera restarts
MIN_CHUNK_S = 3
ALERT_GAP_S = 60  # at most one alert per minute while something keeps moving
SNIPPET_S = 6  # length of the clip sent with an alert
SNIPPET_LEAD_S = 2  # it starts this long before the first movement
MAX_WATCH_S = 2 * 3600


def describe(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds >= 60 and seconds % 60 == 0 else f"{seconds}s"


class MotionWatch:
    def __init__(
        self,
        device: Device,
        lock: threading.Lock,
        send_text: Callable[[str], None],
        send_file: Callable[[Path, str], None],
    ) -> None:
        self.device = device
        self.lock = lock  # held while recording: the camera and screen can't be shared
        self.send_text = send_text
        self.send_file = send_file
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.summary = ""

    @property
    def active(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, seconds: int, front: bool) -> str:
        if self.active:
            return f"Already {self.summary}. Send /unwatch to stop it first."
        seconds = max(MIN_CHUNK_S, min(seconds, MAX_WATCH_S))
        camera = "front" if front else "back"
        self.summary = f"watching with the {camera} camera for {describe(seconds)}"
        self.stopping.clear()
        self.thread = threading.Thread(target=self.run, args=(seconds, front, camera), daemon=True)
        self.thread.start()
        return f"Started {self.summary}. I'll message you with a clip if anything moves."

    def stop(self) -> str:
        if not self.active:
            return "Not watching."
        self.stopping.set()
        return "Stopping the motion watch after the current clip."

    def run(self, seconds: int, front: bool, camera: str) -> None:
        clips, alerts, last_alert = 0, 0, 0.0
        end = time.time() + seconds
        try:
            while not self.stopping.is_set():
                length = int(min(CHUNK_S, end - time.time()))
                if length < MIN_CHUNK_S:
                    break
                with self.lock:  # only while recording; between clips an AI task or command can use the phone
                    phone_path = self.device.capture(video=True, seconds=length, front=front)
                    local = self.device.fetch(phone_path)
                    shell(f"rm {phone_path}")  # clips only live as long as it takes to check them
                clips += 1
                moments = motion_moments(local)
                log.info(f"clip {clips}: {length}s, motion at {moments[:5]}")
                if moments and time.time() - last_alert >= ALERT_GAP_S:
                    alerts, last_alert = alerts + 1, time.time()
                    threading.Thread(target=self.alert, args=(local, moments[0], camera), daemon=True).start()
                else:
                    local.unlink(missing_ok=True)
        except Exception as error:
            log.exception("motion watch failed")
            self.send_text(f"Motion watch stopped: {error}")
            return
        finally:
            shell("input keyevent KEYCODE_HOME")
        result = f"{alerts} alert(s)" if alerts else "no motion detected"
        self.send_text(f"Motion watch finished: {clips} clip(s) checked, {result}.")

    def alert(self, clip: Path, moment: float, camera: str) -> None:
        """Send a short, small snippet around the movement; a full clip can take minutes to upload."""
        snippet = None
        try:
            snippet = compress_video(clip, start=max(0.0, moment - SNIPPET_LEAD_S), duration=SNIPPET_S, width=640, crf=30)
            self.send_text(f"Motion detected ({camera} camera).")
            self.send_file(snippet, f"Motion ({camera} camera)")
        except Exception:
            log.exception("motion alert failed")
            self.send_text(f"Motion detected ({camera} camera), but sending the clip failed.")
        finally:
            clip.unlink(missing_ok=True)
            if snippet:
                snippet.unlink(missing_ok=True)
