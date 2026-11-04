"""Free OpenAI-compatible model APIs: the model list, and a router that falls back and hedges between them."""
import json
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from net import ConnectionPool
from store import PACIFIC, STATE_DIR, Usage, read_json, write_atomically
from tools import SCREEN_STEP_TOOLS, TOOLS
log = logging.getLogger("models")


ENDPOINTS = {  # provider -> (host, path) of its OpenAI-compatible chat endpoint
    "gemini": ("generativelanguage.googleapis.com", "/v1beta/openai/chat/completions"),
    "groq": ("api.groq.com", "/openai/v1/chat/completions"),
    "nvidia": ("integrate.api.nvidia.com", "/v1/chat/completions"),
    "ovh": ("oai.endpoints.kepler.ai.cloud.ovh.net", "/v1/chat/completions"),
}
# Used without a key unless the config sets the provider to false; OVHcloud allows 2 requests a minute per model and IP.
KEYLESS = {"ovh"}
COOLDOWNS_PATH = STATE_DIR / "cooldowns.json"
CHOSEN_MODEL_PATH = STATE_DIR / "model.json"  # the owner's pick from /model; none means automatic
# Daily quotas often free up before the documented reset (Groq's window rolls), so a full model is retried after this.
DAILY_RECHECK_S = 3600
MAX_TROUBLE_WAIT_S = 900  # server errors and network failures back off 1, 2, 4... minutes, up to this
TROUBLE_STALE_S = 900  # a problem not seen again for this long counts as over
PROVIDER_NAMES = {"gemini": "Google", "groq": "Groq", "nvidia": "NVIDIA", "ovh": "OVHcloud"}
# An overloaded provider may still answer, but only after minutes.
MODEL_TIMEOUT_S = 40  # give up and try the next model; healthy answers take under ~15 s
SLOW_ANSWER_S = 25  # an answer slower than this sets the model aside for a while
SLOW_COOLDOWN_S = 300
MAX_SLOW_WAIT_S = 3600
# No answer after this long: also ask the next model and take whichever answers first. Most answers take under ~5 s.
HEDGE_S = 5
MAX_PARALLEL = 3
SHORT_LIMIT_WAIT_S = 20  # every model cooling down, but one is back within this: wait rather than give up
OPENAI_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name"}


def seconds_until_quota_reset() -> float:
    """Gemini's daily quotas reset at midnight Pacific time (plus a minute of margin)."""
    now = datetime.now(PACIFIC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp() - now.timestamp() + 60  # timestamps, so a DST change in between counts


def reported_quota(headers: Any) -> tuple[int, int] | None:
    """(daily limit, left) from Groq's headers, which count its rolling 24-hour window."""
    try:
        return int(headers["x-ratelimit-limit-requests"]), int(headers["x-ratelimit-remaining-requests"])
    except (KeyError, TypeError, ValueError):
        return None


def retry_after(headers: Any, detail: str) -> float | None:
    """How long the provider asked us to wait, if it said."""
    if headers and headers.get("retry-after"):
        try:
            return float(headers["retry-after"])
        except ValueError:
            pass
    if match := re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', detail):
        return float(match[1])
    if match := re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", detail):
        return int(match[1] or 0) * 60 + float(match[2])
    return None


@dataclass
class Model:
    provider: str
    name: str
    vision: bool
    effort: str | None = None  # reasoning_effort; "low" keeps thinking models quick
    fast: int = 0  # rank among the quick models tried first for a task's first step (1 = first); 0 = not quick
    daily: int = 0  # free-tier requests per day (for /usage; the provider enforces it)
    cooldown_until: float = 0.0
    slow_until: float = 0.0  # answering slowly lately: asked only after the other models
    slow_strikes: int = 0
    strikes: int = 0
    out_of_quota: bool = False  # the last refusal was for the daily quota
    reported: tuple[int, int] | None = None  # (daily limit, left) as the provider last reported them
    latency: float = 0.0  # seconds the last request took

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.name}"

    @property
    def short(self) -> str:
        """Display name: "gemini/gemini-flash-lite-latest" -> "flash-lite"."""
        return self.name.split("/")[-1].removeprefix("gemini-").removesuffix("-latest").removesuffix("-preview")

    @property
    def family(self) -> str:
        """The API this model is served by: "gemini#2" (a second account's key) is still "gemini"."""
        return self.provider.split("#")[0]

    @property
    def title(self) -> str:
        """Name for the owner, e.g. on the /model buttons."""
        return TITLES.get(self.name, self.short)


# Each Gemini model is tried on every configured Gemini key ("gemini", "gemini#2") before the next model.
MODELS = [
    Model("gemini", "gemini-3-flash-preview", vision=True, effort="low", daily=20),
    Model("gemini#2", "gemini-3-flash-preview", vision=True, effort="low", daily=20),
    Model("gemini", "gemini-3.6-flash", vision=True, effort="low", daily=20),
    Model("gemini#2", "gemini-3.6-flash", vision=True, effort="low", daily=20),
    Model("gemini", "gemini-flash-lite-latest", vision=True, daily=500),
    Model("gemini#2", "gemini-flash-lite-latest", vision=True, daily=500),
    Model("nvidia", "nvidia/nemotron-3.5-super-vl-120b-a12b", vision=True, daily=10000),  # NVIDIA's 10K/day is per account
    Model("gemini", "gemini-3.1-flash-lite", vision=True, daily=500),
    Model("gemini#2", "gemini-3.1-flash-lite", vision=True, daily=500),
    Model("ovh", "Qwen3.8-27B", vision=True, effort="low"),
    Model("ovh", "Qwen3.6-27B", vision=True, effort="low"),
    Model("groq", "qwen/qwen3.8-27b", vision=True, fast=2, daily=1000),
    Model("groq", "openai/gpt-oss-120b", vision=False, effort="low", fast=1, daily=1000),
]
TITLES = {
    "gemini-3-flash-preview": "Gemini 3 Flash",
    "gemini-3.6-flash": "Gemini 3.6 Flash",
    "nvidia/nemotron-3.5-super-vl-120b-a12b": "Nemotron 3.5 Super (NVIDIA)",
    "gemini-flash-lite-latest": "Gemini 3.5 Flash Lite",
    "gemini-3.1-flash-lite": "Gemini 3.1 Flash Lite",
    "qwen/qwen3.8-27b": "Qwen 3.8 27B (Groq)",
    "Qwen3.8-27B": "Qwen 3.8 27B (OVHcloud)",
    "Qwen3.6-27B": "Qwen 3.6 27B (OVHcloud)",
    "openai/gpt-oss-120b": "GPT-OSS 120B (Groq, can't see images)",
}


class NoModelAvailable(Exception):
    pass


class ProviderError(Exception):
    def __init__(self, status: int, headers: Any, detail: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status, self.headers, self.detail = status, headers, detail


class StreamedReply:
    """Rebuilds a chat message from a server-sent events stream; text goes to on_text until a tool call shows up."""

    def __init__(self, on_text: Callable[[str], None]) -> None:
        self.on_text = on_text
        self.content = ""
        self.calls: list[dict[str, Any]] = []
        self.deadline = time.time() + MODEL_TIMEOUT_S

    def feed(self, line: bytes) -> None:
        if time.time() > self.deadline:  # the socket timeout only covers the wait for each piece
            raise TimeoutError("answer still streaming")
        line = line.strip()
        if not line.startswith(b"data:") or line == b"data: [DONE]":
            return
        chunk = json.loads(line[5:])
        if "error" in chunk:
            raise ProviderError(502, {}, f"error mid-answer: {json.dumps(chunk)[:300]}")
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            for part in delta.get("tool_calls") or []:
                self.add_call(part)
            if text := delta.get("content"):
                self.content += text
                if not self.calls:
                    self.on_text(self.content)

    def add_call(self, part: dict[str, Any]) -> None:
        """Groq sends a call in pieces tagged with its index; Gemini sends each call whole, untagged."""
        index = part.get("index", len(self.calls) if "id" in part else max(len(self.calls) - 1, 0))
        while len(self.calls) <= index:
            self.calls.append({"type": "function", "function": {"name": "", "arguments": ""}})
        call = self.calls[index]
        for key, value in part.items():
            if key == "function":
                for field in ("name", "arguments"):
                    call["function"][field] += value.get(field) or ""
            elif key != "index":
                call[key] = value

    def message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.calls:
            message["tool_calls"] = self.calls
        return message


class TextGate:
    """Thread-safe: passes on streamed text only from the first model to write, and nothing after close()."""

    def __init__(self, on_text: Callable[[str], None]) -> None:
        self.on_text = on_text
        self.owner: str | None = None
        self.closed = False
        self.lock = threading.Lock()

    def writer(self, model: Model) -> Callable[[str], None]:
        def write(text: str) -> None:
            with self.lock:
                if self.closed or self.owner not in (None, model.label):
                    return
                self.owner = model.label
            self.on_text(text)

        return write

    def release(self, model: Model) -> None:
        """A model that failed mid-answer gives the draft up to the others."""
        with self.lock:
            if self.owner == model.label:
                self.owner = None

    def close(self) -> None:
        with self.lock:
            self.closed = True


class Router:
    """Sends each request to the best model that isn't cooling down after a limit or error."""

    def __init__(self, keys: dict[str, str], usage: Usage | None = None) -> None:
        self.keys = keys
        self.usage = usage
        self.models = [model for model in MODELS if keys.get(model.provider, model.provider in KEYLESS)]
        families = {model.family for model in self.models}
        self.pools = {family: ConnectionPool(host) for family, (host, _) in ENDPOINTS.items() if family in families}
        self.last_failure = ""
        # Why requests are going to a fallback model, for the progress message; "" when they aren't.
        self.note = ""
        self.note_since = 0.0
        # Models failing with server or network errors: label -> (first failure, latest failure).
        self.trouble: dict[str, tuple[float, float]] = {}
        self.load_cooldowns()
        saved = read_json(CHOSEN_MODEL_PATH, {}).get("model", "")
        self.chosen = saved if any(m.name == saved for m in self.models) else ""

    def choices(self) -> list[Model]:
        """One entry per model, whichever keys it's on."""
        return list({m.name: m for m in self.models}.values())

    def choose(self, name: str) -> None:
        """Ask this model first from now on ("" = automatic) and save the pick; others take over when it fails."""
        self.chosen = name if any(m.name == name for m in self.models) else ""
        write_atomically(CHOSEN_MODEL_PATH, json.dumps({"model": self.chosen}))

    def load_cooldowns(self) -> None:
        """Restore saved cooldowns, capped at DAILY_RECHECK_S from now."""
        saved = read_json(COOLDOWNS_PATH, {})
        latest = time.time() + DAILY_RECHECK_S
        for model in self.models:
            entry = saved.get(model.label)
            if isinstance(entry, dict):
                model.out_of_quota = bool(entry.get("full"))
                model.cooldown_until = max(model.cooldown_until, min(entry.get("until", 0.0), latest))

    def save_cooldowns(self) -> None:
        now = time.time()
        paused = {m.label: {"until": m.cooldown_until, "full": m.out_of_quota} for m in self.models if m.cooldown_until > now}
        write_atomically(COOLDOWNS_PATH, json.dumps(paused))

    def order(self, fast_first: bool, prefer: str, vision: bool) -> tuple[list[Model], str]:
        """(models to ask, in order; the name of the one asked first by request)."""
        chosen = "" if prefer else self.chosen
        first = prefer or chosen
        models = [m for m in self.models if m.vision] if vision else self.models
        order = sorted(models, key=lambda m: (m.fast == 0, m.fast)) if fast_first and not chosen else models
        return sorted(order, key=lambda m: m.name != first), first

    def complete(
        self,
        messages: list[dict[str, Any]],
        fast_first: bool = False,
        on_text: Callable[[str], None] | None = None,
        tools: bool = True,
        prefer: str = "",
        vision: bool = False,
    ) -> tuple[dict[str, Any], Model]:
        """Ask the best available model; returns (message, model) or raises NoModelAvailable.

        After HEDGE_S with no answer the next model is asked too and the first answer wins. fast_first puts
        the quick models first; prefer names a model to try first (otherwise the owner's /model pick); vision
        asks only models that see images; on_text receives a text answer as it streams."""
        order, first = self.order(fast_first, prefer, vision)
        running: dict[str, tuple[Model, float]] = {}  # label -> (model, when it was asked)
        outcomes: queue.Queue[tuple[Model, dict[str, Any] | None]] = queue.Queue()
        gate = TextGate(on_text) if on_text else None
        failed = skipped = waited = False

        def start() -> bool:
            """Ask the next ready model; False if none. Skips models already thinking on another key, prefers
            another provider (slowness and 503s hit a whole provider), and asks slow models last unless named."""
            nonlocal skipped
            now = time.time()
            busy = {model.name for model, _ in running.values()}
            families = {model.family for model, _ in running.values()}
            ready = [m for m in order if m.cooldown_until <= now and m.name not in busy]
            ready.sort(key=lambda m: (m.slow_until > now and m.name != first, m.family in families))
            if not ready:
                return False
            model = ready[0]
            skipped = skipped or (not running and model is not order[0])
            running[model.label] = (model, now)
            threading.Thread(target=self.attempt, args=(model, messages, gate, outcomes, tools), daemon=True).start()
            return True

        start()
        try:
            while True:
                if not running:
                    wait = min((m.cooldown_until for m in order), default=float("inf")) - time.time()
                    if waited or wait > SHORT_LIMIT_WAIT_S:
                        break
                    log.info(f"every model is cooling down; waiting {wait:.0f}s for the first to be back")
                    time.sleep(max(0.0, wait))
                    waited = True
                    if not start():
                        break
                try:
                    model, message = outcomes.get(timeout=HEDGE_S if len(running) < MAX_PARALLEL else None)
                except queue.Empty:
                    if start():
                        labels = list(running)
                        log.info(f"no answer from {labels[0]} in {HEDGE_S}s; also asking {labels[-1]}")
                    continue
                _, asked = running.pop(model.label)
                if message is None:  # attempt() already put it on a cooldown
                    failed = True
                    start()
                    continue
                for slower, since in running.values():  # asked earlier and still thinking
                    if since < asked:
                        self.set_aside(slower, f"lost to {model.label}")
                        failed = True
                if model.latency > SLOW_ANSWER_S:
                    self.set_aside(model, f"took {model.latency:.0f}s")
                    failed = True
                else:
                    model.slow_strikes = 0
                return self.succeeded(model, message, failed, skipped, fast_first)
        finally:
            if gate:
                gate.close()
        raise NoModelAvailable("All models are rate-limited or failing right now. Try again later.")

    def attempt(
        self, model: Model, messages: list[dict[str, Any]], gate: TextGate | None, outcomes: queue.Queue, tools: bool
    ) -> None:
        """Runs on its own thread; puts (model, answer) on outcomes, or (model, None) after a cooldown is set."""
        message = None
        try:
            try:
                message = self.call(model, messages, gate.writer(model) if gate else None, tools)
            except ProviderError as error:
                if error.status != 400 or not model.effort:
                    raise
                log.warning(f"{model.label} rejected reasoning_effort={model.effort}; sending without it")
                model.effort = None
                message = self.call(model, messages, gate.writer(model) if gate else None, tools)
        except ProviderError as error:
            self.penalize(model, error.status, error.detail, retry_after(error.headers, error.detail))
        except TimeoutError as error:
            self.penalize(model, 504, f"no answer within {MODEL_TIMEOUT_S}s: {error}", None)
        except (OSError, ValueError) as error:
            self.penalize(model, 0, str(error), None)
        except Exception as error:
            log.exception(f"{model.label} request failed")
            self.penalize(model, 0, str(error), None)
        finally:
            if gate and message is None:
                gate.release(model)
            outcomes.put((model, message))

    def set_aside(self, model: Model, why: str) -> None:
        """Ask a slow model after the others for SLOW_COOLDOWN_S, doubling while it stays slow."""
        model.slow_strikes += 1
        wait = min(SLOW_COOLDOWN_S * 2 ** (model.slow_strikes - 1), MAX_SLOW_WAIT_S)
        model.slow_until = time.time() + wait
        self.last_failure = f"{model.short} slow at {PROVIDER_NAMES.get(model.family, model.family)}"
        log.warning(f"{model.label} {why}; setting it aside for {wait}s")

    def succeeded(
        self, model: Model, message: dict[str, Any], failed: bool, skipped: bool, fast_first: bool
    ) -> tuple[dict[str, Any], Model]:
        """Clear the model's strikes and update the fallback note shown in progress messages."""
        model.strikes = 0
        model.out_of_quota = False
        self.trouble.pop(model.label, None)
        if failed:
            if not self.note:
                self.note_since = time.time()
            self.note = self.last_failure
        elif not skipped and not fast_first:  # the first-choice model answered: back to normal
            self.note = ""
            self.trouble.clear()
        return message, model

    def outage_since(self) -> float | None:
        """When the current run of server or network failures began, or None if there isn't one."""
        now = time.time()
        ongoing = [first for first, last in self.trouble.values() if now - last < TROUBLE_STALE_S]
        return min(ongoing) if ongoing else None

    def call(
        self, model: Model, messages: list[dict[str, Any]], on_text: Callable[[str], None] | None = None, tools: bool = True
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model.name, "messages": prepare(model, messages)}
        if tools:
            pictured = model.vision and any(isinstance(m.get("content"), list) for m in messages)
            body["tools"] = SCREEN_STEP_TOOLS if pictured and model.family == "groq" else TOOLS
        streamed = StreamedReply(on_text) if on_text else None
        if streamed:
            body["stream"] = True
        if model.effort:
            body["reasoning_effort"] = model.effort
        headers = {"Content-Type": "application/json", "User-Agent": "m34-agent/1.0"}  # Groq's CDN rejects Python's default agent
        if isinstance(key := self.keys.get(model.provider), str) and key:
            headers["Authorization"] = f"Bearer {key}"
        _, path = ENDPOINTS[model.family]
        start = time.time()
        status, response_headers, data = self.pools[model.family].request(
            path, json.dumps(body).encode(), headers, MODEL_TIMEOUT_S, streamed.feed if streamed else None
        )
        model.latency = time.time() - start
        log.info(f"{model.label}: HTTP {status} in {model.latency:.1f}s")
        model.reported = reported_quota(response_headers) or model.reported
        if self.usage and status in (200, 429):
            self.usage.record(model.label, ok=status == 200)
        if status != 200:
            raise ProviderError(status, response_headers, data.decode(errors="replace"))
        if streamed:
            message = streamed.message()
            if not message["content"] and not message.get("tool_calls"):
                raise ProviderError(502, response_headers, "empty streamed answer")
            return message
        choices = json.loads(data).get("choices") or []
        if not choices or "message" not in choices[0]:  # e.g. a filtered reply: treat like a server hiccup
            raise ProviderError(502, response_headers, f"no answer in response: {data[:300]!r}")
        return choices[0]["message"]

    def penalize(self, model: Model, code: int, detail: str, hint: float | None) -> None:
        provider = PROVIDER_NAMES.get(model.family, model.family)
        model.strikes += 1
        if code == 429:
            if "perday" in detail.lower().replace(" ", ""):
                model.out_of_quota = True
                if match := re.search(r'"quotaValue":\s*"(\d+)"', detail):
                    model.daily = int(match[1])
                wait = min(seconds_until_quota_reset(), DAILY_RECHECK_S)
                self.last_failure = f"{model.short} used up today's quota"
            else:
                wait = hint + 2 if hint else min(60 * 2 ** (model.strikes - 1), 3600)
                self.last_failure = f"{model.short} hit its rate limit"
        elif code == 0 or code >= 500:
            # Busy spells last minutes, so back off exponentially.
            wait = min(60 * 2 ** (model.strikes - 1), MAX_TROUBLE_WAIT_S)
            if code == 0:
                self.last_failure = f"can't reach {provider}"
            elif code == 503:
                self.last_failure = f"{model.short} busy at {provider}"
            elif code == 504:
                self.last_failure = f"{model.short} too slow at {provider}"
            else:
                self.last_failure = f"{model.short}: server error at {provider}"
            first, _ = self.trouble.get(model.label, (time.time(), 0.0))
            self.trouble[model.label] = (first, time.time())
        else:  # often our request's fault rather than the model's, so keep it short
            wait = 120
            self.last_failure = f"{model.short} rejected the request"
        model.cooldown_until = time.time() + wait
        self.save_cooldowns()
        log.warning(f"{model.label} failed ({code}), cooling down {wait:.0f}s: {' '.join(detail.split())[:300]}")


def with_thought_signature(call: dict[str, Any]) -> dict[str, Any]:
    """Gemini rejects tool calls without a thought signature (other models' calls lack one); adds Google's placeholder."""
    if call.get("extra_content", {}).get("google", {}).get("thought_signature"):
        return call
    return {**call, "extra_content": {"google": {"thought_signature": "skip_thought_signature_validator"}}}


def prepare(model: Model, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt the shared history to one provider: strip vendor fields and, if needed, images."""
    prepared = []
    for message in messages:
        if model.family == "gemini" and message.get("tool_calls"):
            message = {**message, "tool_calls": [with_thought_signature(call) for call in message["tool_calls"]]}
        if model.family != "gemini":
            message = {key: value for key, value in message.items() if key in OPENAI_KEYS}
            if message.get("tool_calls"):
                message["tool_calls"] = [
                    {"id": call["id"], "type": "function", "function": call["function"]}
                    for call in message["tool_calls"]
                ]
        if isinstance(message.get("content"), list) and not model.vision:
            text = " ".join(part.get("text", "") for part in message["content"] if part.get("type") == "text")
            message = {**message, "content": f"{text} [image omitted: this model reads the element list instead]"}
        prepared.append(message)
    return prepared
