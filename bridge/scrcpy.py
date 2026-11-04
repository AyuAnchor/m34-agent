"""Live screen frames and multi-touch input through the scrcpy server, for games.

The server streams H.264 (decoded here by ffmpeg) and takes touch events over a socket. Protocol: scrcpy 4.1, doc/develop.md.
"""
import logging
import secrets
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from device import ADB_TARGET, adb

log = logging.getLogger("scrcpy")

SERVER_VERSION = "4.1"  # must match the jar exactly; the server refuses any other client version
LOCAL_SERVER = Path.home() / ".cache/tg-bridge/scrcpy-server"
PHONE_SERVER = "/data/local/tmp/scrcpy-server-bot.jar"
DEVICE_NAME_BYTES = 64
CONNECT_TRIES = 100
KEY_EVENT, TOUCH_EVENT = 0, 2  # SC_CONTROL_MSG_TYPE_INJECT_KEYCODE, _INJECT_TOUCH_EVENT
DOWN, UP = 0, 1  # AMOTION_EVENT_ACTION_* (key events use the same values)
FULL_PRESSURE = 0xFFFF
KEYCODE_WAKEUP = 224  # wakes and undims the screen; does nothing else when it's already on


class ScrcpyError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def read_exactly(conn: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("scrcpy server closed the stream")
        data += chunk
    return bytes(data)


class Screen:
    """The phone's screen as numpy RGB frames, plus touch input in screen pixels."""

    def __init__(self, max_size: int = 640, max_fps: int = 30, bit_rate: int = 4_000_000) -> None:
        self.options = {"max_size": max_size, "max_fps": max_fps, "video_bit_rate": bit_rate}
        self.scid = f"{secrets.randbits(31):08x}"
        self.port = free_port()
        self.server: subprocess.Popen[bytes] | None = None
        self.decoder: subprocess.Popen[bytes] | None = None
        self.video: socket.socket | None = None
        self.control: socket.socket | None = None
        self.control_lock = threading.Lock()
        self.frame_lock = threading.Condition()
        self.frame: np.ndarray | None = None
        self.frame_count = 0
        self.size = (0, 0)  # video width, height; touches are sent in this space
        self.screen_size = (0, 0)  # the phone's real pixels, in the current orientation
        self.closed = threading.Event()

    def start(self, screen_width: int, screen_height: int) -> None:
        self.screen_size = (screen_width, screen_height)
        if not LOCAL_SERVER.exists():
            raise ScrcpyError(f"scrcpy server missing: put scrcpy-server v{SERVER_VERSION} at {LOCAL_SERVER}")
        if adb("push", str(LOCAL_SERVER), PHONE_SERVER).returncode != 0:
            raise ScrcpyError("could not copy the scrcpy server to the phone")
        if adb("forward", f"tcp:{self.port}", f"localabstract:scrcpy_{self.scid}").returncode != 0:
            raise ScrcpyError("adb forward failed")
        options = " ".join(f"{key}={value}" for key, value in self.options.items())
        self.server = subprocess.Popen(
            ["adb", "-s", ADB_TARGET, "shell",
             f"CLASSPATH={PHONE_SERVER} app_process / com.genymobile.scrcpy.Server {SERVER_VERSION} "
             f"scid={self.scid} log_level=warn tunnel_forward=true audio=false control=true "
             f"clipboard_autosync=false power_off_on_close=false {options}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.video = self.connect_first()
        self.control = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.control.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        read_exactly(self.video, DEVICE_NAME_BYTES)
        codec = read_exactly(self.video, 4)
        if codec != b"h264":
            raise ScrcpyError(f"unexpected video codec {codec!r}")
        self.video.settimeout(None)
        threading.Thread(target=self.read_video, daemon=True).start()
        if self.wait_frame(timeout=10) is None:
            raise ScrcpyError("no video frames from the scrcpy server")

    def connect_first(self) -> socket.socket:
        """The adb forward accepts connections before the server listens; the dummy byte proves it's up."""
        for _ in range(CONNECT_TRIES):
            if self.server and self.server.poll() is not None:
                raise ScrcpyError(f"scrcpy server exited: {self.server.stderr.read().decode(errors='replace')[-500:]}")
            try:
                conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                if conn.recv(1) == b"\x00":
                    return conn
                conn.close()
            except OSError:
                pass
            time.sleep(0.1)
        raise ScrcpyError("could not connect to the scrcpy server")

    def read_video(self) -> None:
        """Split the stream into packets and feed them to ffmpeg; a session packet starts a new decoder."""
        try:
            while not self.closed.is_set():
                header = read_exactly(self.video, 12)
                if header[0] & 0x80:  # session packet: a new capture size (start, or rotation)
                    width, height = struct.unpack(">II", header[4:])
                    self.start_decoder(width, height)
                    continue
                (size,) = struct.unpack(">I", header[8:])
                packet = read_exactly(self.video, size)
                if self.decoder and self.decoder.stdin:
                    self.decoder.stdin.write(packet)
                    self.decoder.stdin.flush()
        except (OSError, ValueError, ConnectionError) as error:
            if not self.closed.is_set():
                log.warning(f"video stream ended: {error}")
        finally:
            self.closed.set()
            with self.frame_lock:
                self.frame_lock.notify_all()

    def start_decoder(self, width: int, height: int) -> None:
        if self.decoder:
            self.decoder.kill()
        self.size = (width, height)
        self.decoder = subprocess.Popen(
            # No "-fflags nobuffer": with it this ffmpeg build decodes nothing from a raw H.264 pipe.
            ["ffmpeg", "-loglevel", "error", "-flags", "low_delay", "-probesize", "32", "-analyzeduration", "0",
             "-threads", "1", "-f", "h264", "-i", "pipe:0",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-flush_packets", "1", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        threading.Thread(target=self.read_frames, args=(self.decoder, width, height), daemon=True).start()

    def read_frames(self, decoder: subprocess.Popen[bytes], width: int, height: int) -> None:
        frame_bytes = width * height * 3
        while decoder.stdout and not self.closed.is_set():
            data = decoder.stdout.read(frame_bytes)
            if len(data) < frame_bytes:
                return
            with self.frame_lock:
                self.frame = np.frombuffer(data, np.uint8).reshape(height, width, 3)
                self.frame_count += 1
                self.frame_lock.notify_all()

    def wait_frame(self, after: int = 0, timeout: float = 1.0) -> np.ndarray | None:
        """The newest frame once there is one newer than frame number `after`."""
        deadline = time.time() + timeout
        with self.frame_lock:
            while self.frame_count <= after and not self.closed.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self.frame_lock.wait(remaining)
            return self.frame

    def touch(self, finger: int, action: int, x: float, y: float) -> None:
        """Press, move or lift a finger at (x, y) in the phone's screen pixels. Each finger id is separate."""
        width, height = self.size
        video_x = round(x * width / self.screen_size[0])
        video_y = round(y * height / self.screen_size[1])
        pressure = 0 if action == UP else FULL_PRESSURE
        self.send(struct.pack(">BBQiiHHHII", TOUCH_EVENT, action, finger, video_x, video_y, width, height, pressure, 0, 0))

    def wake(self) -> None:
        """Undim the screen, which Android dims before sleeping when nothing is touched."""
        for action in (DOWN, UP):
            self.send(struct.pack(">BBIII", KEY_EVENT, action, KEYCODE_WAKEUP, 0, 0))

    def send(self, message: bytes) -> None:
        with self.control_lock:
            if self.control:
                self.control.sendall(message)

    def close(self) -> None:
        self.closed.set()
        for conn in (self.control, self.video):
            if conn:
                conn.close()
        for process in (self.decoder, self.server):
            if process and process.poll() is None:
                process.kill()
        adb("forward", "--remove", f"tcp:{self.port}")
