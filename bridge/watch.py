"""Background watches that alert the owner: movement or a person on camera, and sounds on the microphone.

Android blocks background camera access, so frames come from the camera app's own video recording. The
microphone and the camera can record at the same time, so a camera watch and a sound watch can run together.
"""
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from device import AUDIO_DIR, Device, compress_video, cut_audio, frame_at, motion_moments, shell, start_recorder, stop_recorder

if TYPE_CHECKING:
    from detect import PersonDetector, SoundDetector

log = logging.getLogger("watch")

CHUNK_S = 15  # clip length; a ~7 s gap follows each clip while the camera restarts
MIN_CHUNK_S = 3
ALERT_GAP_S = 60  # at most one alert per minute while something keeps moving
SNIPPET_S = 6  # length of the clip sent with an alert
SNIPPET_LEAD_S = 2  # it starts this long before the first movement
MAX_WATCH_S = 2 * 3600
PERSON_FRAMES = 3  # frames checked for a person per clip; each costs ~0.4 s
SEGMENT_S = 1800  # one recording per half hour; starting the next leaves a ~4 s gap
POLL_S = 5  # how often the growing recording is checked
SOUND_HITS = 2  # windows within SOUND_SPAN_S that must hear it, so a single bang or cough doesn't count
SOUND_SPAN_S = 10
SOUND_LEAD_S = 3  # an alert's recording starts this long before the sound was first heard
SOUND_ALERT_GAP_S = 120
LIVE_FORMAT = ["-e", "opus", "-b", "32", "-r", "48000", "-c", "1"]  # Ogg can be read while it's being written
MAX_LISTEN_S = 12 * 3600  # a night; the microphone alone barely warms the phone
SOUNDS = {  # name -> (what the owner is told, YAMNet classes that count as it)
    "cry": ("a baby crying", ("Crying, sobbing", "Baby cry, infant cry")),
    "scream": ("screaming", ("Screaming",)),
    "dog": ("a dog barking", ("Dog", "Bark")),
    "alarm": ("an alarm", ("Alarm", "Smoke detector, smoke alarm", "Fire alarm")),
    "glass": ("glass breaking", ("Glass", "Shatter")),
    "door": ("the doorbell or a knock", ("Doorbell", "Ding-dong", "Knock")),
}
MISSING_MODELS = (
    "On-device recognition isn't installed. On the phone, in Debian, run: "
    "~/venv/bin/pip install ai-edge-litert"
)


def describe(seconds: int) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    return f"{seconds // 60} min" if seconds >= 60 and seconds % 60 == 0 else f"{seconds}s"


def recognition_installed() -> bool:
    try:
        import detect  # noqa: F401  needs numpy and ai-edge-litert, which the rest of the bridge doesn't
    except ImportError:
        return False
    return True


class Watch:
    """One background watch at a time per kind; started and stopped by the owner or the AI."""

    name = "watch"

    def __init__(self, device: Device, send_text: Callable[[str], None], send_file: Callable[[Path, str], None]) -> None:
        self.device = device
        self.send_text = send_text
        self.send_file = send_file
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.summary = ""

    @property
    def active(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def begin(self, summary: str, target: Callable[..., None], *args: object) -> None:
        self.summary = summary
        self.stopping.clear()
        self.thread = threading.Thread(target=self.guarded, args=(target, *args), daemon=True)
        self.thread.start()

    def guarded(self, target: Callable[..., None], *args: object) -> None:
        try:
            target(*args)
        except Exception as error:
            log.exception(f"{self.name} failed")
            self.send_text(f"The {self.name} stopped: {error}")

    def stop(self) -> str:
        if not self.active:
            return f"No {self.name} is running."
        self.stopping.set()
        return f"Stopping the {self.name} after the current recording."


class MotionWatch(Watch):
    name = "camera watch"

    def __init__(self, device: Device, lock: threading.Lock, send_text: Callable[[str], None],
                 send_file: Callable[[Path, str], None]) -> None:
        super().__init__(device, send_text, send_file)
        self.lock = lock  # held while recording: the camera and screen can't be shared

    def start(self, seconds: int, front: bool, people: bool = False) -> str:
        if self.active:
            return f"Already {self.summary}. Send /unwatch to stop it first."
        if people and not recognition_installed():
            return MISSING_MODELS
        seconds = max(MIN_CHUNK_S, min(seconds, MAX_WATCH_S))
        camera = "front" if front else "back"
        target = "someone entering" if people else "movement"
        self.begin(f"watching for {target} with the {camera} camera for {describe(seconds)}",
                   self.run, seconds, front, camera, people)
        return f"Started {self.summary}. I'll message you with a clip if I see it."

    def run(self, seconds: int, front: bool, camera: str, people: bool) -> None:
        detector = None
        if people:
            from detect import PersonDetector
            detector = PersonDetector()
        clips, alerts, last_alert, present = 0, 0, 0.0, False
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
                trigger = moments[0] if moments else None
                if detector:
                    # Alert when someone appears, not every minute while they're in the room.
                    seen = self.person_moment(detector, local, moments or [length / 2])
                    trigger = seen if seen is not None and moments and not present else None
                    present = seen is not None
                log.info(f"clip {clips}: {length}s, motion at {moments[:5]}, person present: {present}")
                if trigger is not None and time.time() - last_alert >= ALERT_GAP_S:
                    alerts, last_alert = alerts + 1, time.time()
                    what = "Someone entered" if detector else "Motion detected"
                    threading.Thread(target=self.alert, args=(local, trigger, f"{what} ({camera} camera)"), daemon=True).start()
                else:
                    local.unlink(missing_ok=True)
        finally:
            shell("input keyevent KEYCODE_HOME")
        result = f"{alerts} alert(s)" if alerts else "nothing seen"
        self.send_text(f"Camera watch finished: {clips} clip(s) checked, {result}.")

    @staticmethod
    def person_moment(detector: "PersonDetector", clip: Path, moments: list[float]) -> float | None:
        """The first of a few spread-out moments where a person is in the picture."""
        step = max(1, len(moments) // PERSON_FRAMES)
        return next((m for m in moments[::step][:PERSON_FRAMES] if detector.seen(frame_at(clip, m))), None)

    def alert(self, clip: Path, moment: float, message: str) -> None:
        """Send a short, small snippet around the moment; a full clip can take minutes to upload."""
        snippet = None
        try:
            snippet = compress_video(clip, start=max(0.0, moment - SNIPPET_LEAD_S), duration=SNIPPET_S, width=640, crf=30)
            self.send_text(f"{message}.")
            self.send_file(snippet, message)
        except Exception:
            log.exception("camera alert failed")
            self.send_text(f"{message}, but sending the clip failed.")
        finally:
            clip.unlink(missing_ok=True)
            if snippet:
                snippet.unlink(missing_ok=True)


class SoundWatch(Watch):
    """Listens with the microphone only: no camera or screen, so tasks keep using the phone meanwhile.
    One recording runs per segment and is checked every few seconds while it grows."""

    name = "sound watch"

    def __init__(self, device: Device, send_text: Callable[[str], None], send_file: Callable[[Path, str], None]) -> None:
        super().__init__(device, send_text, send_file)
        self.segment: Path | None = None
        self.heard_s = 0.0  # seconds of the segment checked so far...
        self.heard_at = 0.0  # ...as of this time
        self.alerts, self.last_alert = 0, 0.0

    def start(self, seconds: int, sound: str) -> str:
        if self.active:
            return f"Already {self.summary}. Send /unlisten to stop it first."
        if sound not in SOUNDS:
            return f"Unknown sound '{sound}'. I can listen for: {', '.join(SOUNDS)}."
        if not recognition_installed():
            return MISSING_MODELS
        seconds = max(MIN_CHUNK_S, min(seconds, MAX_LISTEN_S))
        self.begin(f"listening for {SOUNDS[sound][0]} for {describe(seconds)}", self.run, seconds, sound)
        return f"Started {self.summary}. I'll message you with a recording if I hear it."

    def run(self, seconds: int, sound: str) -> None:
        from detect import SoundDetector
        heard, classes = SOUNDS[sound]
        detector = SoundDetector(classes)
        self.alerts, self.last_alert, listened = 0, 0.0, 0.0
        end = time.time() + seconds
        self.device.live_audio = self.clip
        try:
            while not self.stopping.is_set() and (left := int(end - time.time())) >= MIN_CHUNK_S:
                listened += self.listen(detector, min(SEGMENT_S, left), heard)
        finally:
            self.device.live_audio = None
            self.segment = None
        result = f"{self.alerts} alert(s)" if self.alerts else "nothing heard"
        listened_text = f"{listened / 60:.0f} min" if listened >= 60 else f"{listened:.0f}s"
        self.send_text(f"Sound watch finished: {listened_text} listened, {result}.")

    def listen(self, detector: "SoundDetector", length: int, heard: str) -> float:
        """Record one segment, checking it as it grows; return the seconds checked."""
        from detect import SAMPLE_RATE, decode
        path = AUDIO_DIR / f"listen_{time.strftime('%Y%m%d_%H%M%S')}.ogg"
        hits: list[float] = []
        used = 0  # samples checked; the rest of the last window waits for the next round
        with self.device.mic_lock:
            try:
                start_recorder(path, length, LIVE_FORMAT)
            except RuntimeError:
                stop_recorder()  # one may be left running by a bridge that restarted mid-watch
                start_recorder(path, length, LIVE_FORMAT)
            self.segment, self.heard_s, self.heard_at = path, 0.0, time.time()
            deadline = time.time() + length + 2 * POLL_S
            try:
                while not self.stopping.wait(POLL_S) and self.heard_s < length - 1 and time.time() < deadline:
                    try:
                        pcm = decode(path, used / SAMPLE_RATE)
                    except subprocess.CalledProcessError:
                        continue  # nothing complete to read yet
                    found, consumed = detector.heard(pcm)
                    hits += [used / SAMPLE_RATE + f for f in found]
                    self.heard_s, self.heard_at = (used + len(pcm)) / SAMPLE_RATE, time.time()
                    used += consumed
                    hits = [h for h in hits if h >= self.heard_s - SOUND_SPAN_S]
                    if len(hits) >= SOUND_HITS and time.time() - self.last_alert >= SOUND_ALERT_GAP_S:
                        log.info(f"heard {heard} at {hits} s of {path.name}")
                        self.alerts, self.last_alert = self.alerts + 1, time.time()
                        start = max(0.0, hits[0] - SOUND_LEAD_S)
                        clip = cut_audio(path, start, self.heard_s - start)
                        threading.Thread(target=self.alert, args=(clip, f"Heard {heard}"), daemon=True).start()
                        hits = []
            finally:
                if self.heard_s < length - 1:
                    stop_recorder()
                path.unlink(missing_ok=True)
        return self.heard_s

    def clip(self, seconds: int) -> Path:
        """Serves record_audio while listening: the next seconds of the live recording."""
        path = self.segment
        if path is None:
            raise RuntimeError("The sound watch is starting its recording; try again in a few seconds.")
        start = self.heard_s + time.time() - self.heard_at
        time.sleep(seconds + 1)  # the recorder writes about a second behind
        if path != self.segment or not path.exists():
            raise RuntimeError("The sound watch restarted its recording meanwhile; try again.")
        return cut_audio(path, start, seconds)

    def alert(self, audio: Path, message: str) -> None:
        try:
            self.send_text(f"{message}.")
            self.send_file(audio, message)
        except Exception:
            log.exception("sound alert failed")
            self.send_text(f"{message}, but sending the recording failed.")
        finally:
            audio.unlink(missing_ok=True)
