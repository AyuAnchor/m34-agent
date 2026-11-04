# M34 Agent

Turn a spare Android phone into a 24/7 AI agent you control from Telegram.
You send a message like *"open Chrome and send me a screenshot"*, and the phone does it and replies.

- Works with a **broken touchscreen**: everything runs through adb.
- **Reliable phone control**: the AI sees numbered boxes on every button and taps by number.
- **Memory** across days and **scheduled tasks** ("every morning at 9, send me the battery level").
- Uses **free AI** (Gemini, Groq) and switches models automatically when one hits its limit.
- **Only you** can talk to it. Risky actions ask you first with Approve / Deny buttons.
- **Survives reboots** without a cable.

Tested on Galaxy M34, Android 16 (One UI 8.5), Termux 0.118.3, Shizuku 13.6.0.

---

## How it works

```
 You (Telegram app)
       │  "open chrome and send me a screenshot"
       ▼
 Telegram servers  ◄──── bridge.py asks "any new messages?" (long polling, outbound only)
                                │
                                ▼
                       agent.py: the agent loop
                         1. send the chat + tool list to the AI
                         2. AI picks a tool (tap, type, screenshot, ...)
                         3. safety check → run it → add the result to the chat
                         4. repeat until the AI gives a final answer
                                │                          ▲
                                ▼                          │
                         phone (adb) ─► Android        Gemini / Groq
                                │
                                ▼
 You  ◄─── final reply + progress updates + screenshots
```

### The parts

| Part | Where it runs | Job |
|---|---|---|
| **Termux** | Android app | Linux environment on the phone |
| **Debian (proot)** | Inside Termux | Normal Linux userland for Python (bridge runs in `~/venv`) |
| **uiautomator2** | Phone, started by the agent | Persistent automation server for fast screen reading |
| **bridge.py** | Debian, user `agent` | Talks to Telegram, checks it's you, queues tasks, approvals, progress, runs schedules |
| **agent.py** | Debian, user `agent` | Agent loop, model fallback, tools, safety rules |
| **device.py** | Debian, user `agent` | Phone actions: numbered screen elements, taps, typing, apps, camera, files |
| **store.py** | Debian, user `agent` | Memory notes and scheduled tasks (small JSON files) |
| **commands.py** | Debian, user `agent` | Instant `/commands` that call device actions directly, no AI |
| **speech.py** | Debian, user `agent` | Voice notes to text (Groq Whisper), text to voice messages (Gemini) |
| **imagegen.py** | Debian, user `agent` | Pictures from a description (FLUX.1 [dev] on NVIDIA) |
| **phone** | Debian | `adb` pointed at the phone itself (`127.0.0.1:5555`) |
| **runit** | Termux | Keeps the bridge running, restarts it if it crashes |
| **Termux:Boot** | Android app | Starts everything after a reboot |
| **Shizuku** | Android app | Turns Wireless debugging back on after a reboot |

### Why adb?

Apps on Android are sandboxed. adb runs as the `shell` user, which can tap, type, take
screenshots and open apps. The phone's own adb client connects to the phone's own adb
service, so the agent controls the phone like a computer would, without touching the screen.

### What the AI can do

| Tool | What it does |
|---|---|
| `look` | Numbered list of on-screen elements + a screenshot with the numbers drawn on |
| `tap` | Tap by visible text, by element number, or a point on a 0-1000 scale; optional long press |
| `type_text` | Type into the focused field, any language and emoji; optionally replace what's there and press Enter |
| `key`, `scroll` | Press back/home/enter/media play-pause/next/previous/..., scroll up/down/left/right |
| `open_app` | Open an app by name ("camera", "chrome", "Hill Climb Racing"); only apps with a home-screen icon |
| `open_url` | Open a link in the app that handles it (YouTube, Play Store, Maps), else Chrome |
| `wait` | Pause up to 2 minutes |
| `take_photo`, `record_video` | Use the camera app (front or back), returns the file path |
| `record_audio` | Record from the microphone only (Termux:API), up to 2 minutes |
| `set_alarm`, `set_timer` | Direct to the clock app (needs a clock app installed) |
| `set_volume`, `set_brightness` | Direct actions, no tapping through settings |
| `open_settings` | Jump straight to a Settings page (display, sound, battery, apps, one app's info page...) instead of tapping through menus |
| `phone_status` | Volumes, brightness, screen timeout, Wi-Fi, airplane mode, Bluetooth, do not disturb, battery, storage, RAM and chipset in one step |
| `speak` | Say something out loud through the phone's speaker |
| `send_voice` | Send you a voice message of any text (Gemini text-to-speech), for "answer in voice" |
| `generate_image` | Make a new picture from a description (FLUX.1 on NVIDIA), send it to you and keep a copy on the phone |
| `search_contacts` | Find contacts by name or number |
| `make_call`, `send_sms`, `send_email` | Always ask you first; calls/SMS need a SIM and airplane mode off |
| `find_media`, `send_file`, `send_screenshot` | Find photos/videos, send any file or the screen to you |
| `watch_motion`, `listen`, `stop_watch` | Camera watch (any movement, or only someone entering) and sound watch (a baby crying, a dog, an alarm...) in the background (see below) |
| `remember`, `forget` | Long-term memory notes |
| `schedule_task`, `list_schedules`, `cancel_schedule` | Run tasks later, once or on repeat |
| `web_search`, `fetch_url` | Search the web (DuckDuckGo) and read pages as text, without the screen (~1 s) |
| `phone`, `shell` | Raw adb command / Linux command (for anything else) |
| `ask_owner` | Ask you a yes/no question with Approve / Deny buttons (e.g. before signing in) |

### Why numbered elements?

Guessing pixel positions from a screenshot is unreliable. `look` reads Android's own list of
buttons and fields, draws a numbered box on each (the "Set-of-Mark" technique), and the AI says
"tap 13" instead of "tap 840, 835". If something has no number (a game, a camera preview), it
taps on a 0-1000 scale, which is how Gemini models naturally describe positions.

After `tap`, `type_text`, `scroll`, `key`, `open_app`, `open_settings` and `open_url` the new screen comes back
automatically, so the AI doesn't spend a separate round trip on `look` (about half the AI calls).
The screen is read only once it's stable (two identical reads in a row, ~250 ms each), so a page that's still
animating isn't mistaken for "my tap did nothing". A screen with almost no labels (a game, the camera) is
read once: it goes by its screenshot anyway. When the AI sends several actions at once, the
screen is read once, after the last one, and a tap by text waits up to 1.5 s for its text to appear.

Each AI step takes a few seconds (~1.2 s on the phone to tap, read and capture, then the model's answer),
and some things close by themselves in that time: menus, popups, a game's panel for the building just
tapped. The AI is told to make a tap and its follow-up in one reply (they run ~0.5 s apart; a follow-up by
x/y waits 0.3 s more for the panel to slide in). After a tap the new screen is compared with the last two
(element lists, plus 32x32 grey thumbnails of the screenshots when there are any), and the AI is told when
the tap changed nothing or the screen went back to how it was before its previous action, instead of
repeating the tap. Seen in Clash of Clans: the AI tapped a building and its panel's Upgrade button by turns,
the panel closing in between, until the loop check below stopped it.

Those actions were all planned on one screen, so the batch is checked as it runs:

- A tap by number after an earlier action finds its element again (same label and id) and taps it where
  it is now, or refuses if it's gone, instead of hitting whatever moved into its place.
- Opening an app, link or Settings page, or a failed screen action, skips the rest of the batch and
  shows the new screen.

`open_url` waits for the page instead of a fixed 2 s pause (~1-1.6 s for an uncached page, all of it
read). While loading, Chrome shows an empty "Web View" for 0.5-1.5 s, which would otherwise pass as a
stable screen; a page that stays like that gets a screenshot. Search links for YouTube, the Play Store and
Maps open straight in the app (one step instead of tapping, typing and submitting: ~7 s instead of ~11 s
for a YouTube search). The package is always named, since with two handlers Android shows an "Open with"
chooser.

Plain ASCII is typed with adb's `input text` (~0.3 s). Other text (Hindi, emoji...) is pasted into the
focused field and the field is read back (~1.5 s); if it doesn't hold the text (Chrome's address bar
drops a pasted leading space), the AI is told what it shows. Password fields only take ASCII, so a
password never lands in the keyboard's clipboard history.

That automatic screen comes with a screenshot only when the element list is too sparse to go by
(fewer than 3 labels, or mostly unlabeled: games, the camera, icon-only buttons). A screenshot
about doubles the AI's answer time (Flash-Lite: ~2 s instead of ~0.9 s), and most screens read
fine as a list. An explicit `look` always includes it, so the AI can still see photos, colours or layout.
The AI's copy is half the width and height (540x1170, ~40 KB instead of ~115 KB), with the numbers drawn
big enough to stay readable; Nemotron and Flash Lite picked the same numbered box at both sizes in testing.
The copy sent to you with `/screen` stays full size.

The list leaves out what's on every screen (status bar icons, the Back/Home/Recents buttons, the edge
panel handle; the `key` tool presses those). A tap by text on a screen with no
readable text says so ("tap it by x/y on the screenshot") instead of only "not found". Once a screenshot or
your photo is part of a task, only models that see images are asked, so a text-only backup can't act blind.
Failed actions and "the screen didn't change" notes are logged, so a loop can be traced afterwards and folds each settings row's title and switch into one
line with its state (`Eye comfort shield unchecked`). On Settings pages that cuts the list by ~60%.

Older screen lists are compacted to just their text ("earlier screen showed: Android version; 16"):
the AI keeps the facts it read while the re-sent conversation stays small.

If the same action comes up 3 times in the last 6 steps (repeats or back-and-forth loops), the AI
is told it's going in circles and to change approach. Taps less than ~25 apart on the 0-1000 scale
count as the same tap. If it starts repeating again after that warning, the task stops and says
which action it kept repeating.

Before the reply to a task that used the screen goes out, a second model checks it: it gets the
request, the actions taken, the reply and a fresh read of the screen, and answers OK or names a claim
that no action did and the screen doesn't show ("searched for lofi hindi songs" after only opening
YouTube). The agent is then told to finish the task or say what didn't happen; a second failed check
lets the reply through. On a screen the element list can't describe (a game, the camera) the check gets
the screenshot too and only models that see images are asked; before, a text-only fallback judged an
empty list and called a game screen black. The check costs ~0.8 s and uses no tools, so the request stays small. Gemini 3.5
Flash Lite does it: it was right on all 18 test cases (true and false claims, both keys). gpt-oss missed a
claimed settings change, and Qwen called a results page that was still loading a failed search. Runs
whose last check failed aren't saved as shortcuts.

A reply that claims something was done ("I've sent you a voice note", "I've tapped Deploy troops") when no
tool ran for the request is caught without a model: the claim is dropped and the agent is told nothing was
done yet, once. Seen when a quick model saw "I've sent it" earlier in the conversation and simply said it
again, and when it copied the bot's own "Stopped: I kept repeating..." notice.

### Fast screen reading

Android's built-in `uiautomator dump` starts a new process on every call (~3 s). The agent instead
uses [uiautomator2](https://github.com/openatx/uiautomator2), which keeps a small automation server
running on the phone: a full `look` (elements + marked screenshot) takes **~0.5 s**. Taps and
swipes go through the same server (~0.13 s, against ~0.3 s for `input`, which starts a new process
each time). If the server dies (reboot, adb restart) it is restarted automatically, and the old
methods are the fallback.

### Memory and schedules

- **Memory**: when you tell it a lasting fact or preference, or it learns a trick that works on
  this phone, it saves a short note. All notes go into every task's instructions. You get a
  "Remembered #N" message each time, so nothing is saved silently.
- **Schedules**: "every day at 9 send me the battery level" becomes a scheduled task. Creating one
  needs your **Approve** (it will run unattended later). Times use the phone's time zone.
  Missed runs (phone off) are skipped, not run in a burst.

### Safety rules

| Type | Examples | What happens |
|---|---|---|
| Routine | look, tap, type, keys, open app, camera, audio, alarm, volume, links, speak, send to you, memory | Runs right away |
| Everything else | calls, SMS, email, other adb commands, any `shell` command, creating a schedule | Asks you: **Approve / Deny** (15 min timeout = deny) |
| Lockout risk | Wi-Fi off, debugging off, reboot, touching Termux or Shizuku | Always blocked |

**Signing in:** the AI may sign in to apps and sites with the Google account already on the phone
("Continue with Google"), but asks you first. It never types passwords or one-time codes (they
would go to the AI provider): if a login needs one, it stops and you type it yourself via scrcpy.
Banking, payment and money apps are off limits.

The AI is also told to treat screen text, web pages and notifications as untrusted data,
never as instructions. Only your Telegram messages count.

### Model fallback

Models are tried in this order (edit `MODELS` in `agent.py`). An optional second Gemini key
(`"gemini#2"` in the config) gets its own row for each Gemini model, tried right after the first
key. Note that Google's API terms forbid circumventing usage limits, which extra accounts or
projects used only to multiply free quota can count as; the risk is Google suspending the keys.

1. `gemini-3-flash-preview` (sees images)
2. `gemini-3.6-flash` (sees images)
3. `gemini-flash-lite-latest` (sees images)
4. NVIDIA `nvidia/nemotron-3.5-super-vl-120b-a12b` (sees images)
5. `gemini-3.1-flash-lite` (sees images)
6. OVHcloud `Qwen3.8-27B` (sees images)
7. OVHcloud `Qwen3.6-27B` (sees images)
8. NVIDIA `mistralai/mistral-large-4-1t-a52b` (sees images)
9. Groq `qwen/qwen3.8-27b` (sees images)
10. Groq `openai/gpt-oss-120b` (text only, uses `ui_dump`)

The order comes from the log of 30 October to 4 November (195 tasks, 1,245 agent steps). Flash Lite
decided 43% of the steps and answered 99% of its requests (median 2.3 s, 9 in 10 within 5.1 s), using
~117 of its 500 a day; Nemotron answered 95% (median 2.7 s, 9 in 10 within 9.4 s, 14 timeouts), so it
comes right after, as the backup from another provider. `gemini-flash-latest` (Gemini 3.8 Flash) was
dropped: it answered 9-21% of requests, took a median 14-21 s when it did, and its 37 timeouts were
most of the time lost to failed requests.

NVIDIA's models are used only when `"nvidia"` is in the config. Nemotron answered agent steps in ~2 s in
testing (November 2026), as fast as Flash Lite, with a much larger daily allowance. Other NVIDIA models
that see images (DeepSeek V4.1 Flash, Gemma 4) often took over 40 s, so they aren't used. NVIDIA calls
its free API a trial for prototyping and may throttle it when busy.

OVHcloud's AI Endpoints serve Qwen anonymously: no account and no key, 2 requests a minute per model and
IP address (set `"ovh": false` in the config to leave them out). Qwen3.8-27B scores 82% on AndroidWorld,
the phone-control benchmark; both models got all 8 test steps right (timer, tapping an icon on a
screenshot) in 2-7 s with low thinking (default thinking took up to 36 s). The 2-a-minute limit makes
them backups, not main models: when they're used up the router waits out OVHcloud's retry hint and
the others carry on.

Each model has its own daily quota, so a model is listed once under one name: Google's deprecated
IDs (`gemini-3.7-flash`, `gemini-3.5-flash`) and the `-latest` aliases share quota with the model
they point to. Gemma 4 on the Gemini API returned server errors or timed out in testing (November
2026), so it isn't used.

**Fast first step.** A task's first step goes to the quickest models (Groq `gpt-oss-120b`, then
Groq `qwen`). Plain chat ("hi", a quick question) ends right there in under a second. If the quick model refuses
something, the same step goes to the main models instead (they follow the bot's own rules on what's
allowed, e.g. approved sign-ins). If the task
needs the phone, the following steps go to the smartest available model (Gemini first). Mixing
models mid-task is safe: tool calls made by another model get the placeholder "thought signature"
Gemini requires.

When a model hits a limit or its provider is busy, the next one continues the same task. Instead of
a message per switch, the progress message shows the reason, e.g. `flash-lite (flash busy at Google
since 00:48)`, and you get one message only if the main models keep failing for 10 minutes. A rate
limit follows the provider's own "retry in N seconds" hint when it gives one, so the best model
comes back quickly. Server errors and network failures back off 1, 2, 4... minutes (up to 15), so a
busy spell doesn't bounce the task between models. A used-up **daily** quota is retried hourly.

**Slow answers.** Most answers take 1-5 s, but an overloaded model sometimes sends nothing until the
40 s timeout. So when a model hasn't answered within 5 s, another model is asked too and the first
answer wins, up to 3 at once. That backup comes from another provider when one is free, since
overload and 503s hit all of a provider's models together (usually NVIDIA's Nemotron backs up Gemini;
Groq takes 7,000 input tokens a minute per model, so it fits one or two steps a minute at most). A step
with a screenshot came to 7,024 tokens, ~3,600 of them the tool list, so Qwen on Groq answered "request
too large" every time; on such steps it now gets only the screen tools (~800 tokens) and answers in ~0.6 s.
Qwen3.8-27B scores 82% on AndroidWorld, the phone-control benchmark, one of the best open models. A model that loses this race, or takes over 25 s,
is asked only after the others for 5 minutes, doubling while it stays slow (up to an hour). If every
model is briefly rate-limited (back within 20 s), the bot waits rather than giving up.

**Choosing a model.** `/model` shows a button per model (marked "out for today" or "paused" when it
can't answer now) plus Auto, the order above. A tapped model is asked first for every step of later
tasks, the first step too, even after it was slow; the rest still take over when it's out of quota or
fails, and a backup still joins after 5 s. The choice is kept in `model.json` across restarts.

### Speed

| What | Typical time |
|---|---|
| Simple AI reply ("hi") | ~1 s |
| One agent step (AI decides + action + new screen) | ~3-4 s |
| "Model and Android version from Settings" | 7 AI calls, ~40 s |
| Reading the screen (`look`) | ~0.5 s |
| Instant `/commands` | ~0.5-1.5 s |

What makes it fast:
- **Reused connections** (`net.py`): Telegram and AI calls share open HTTPS connections.
  A new one costs 1-6 s on a mobile hotspot; a reused one ~0.2 s. A background thread
  refreshes one connection per service every 2 minutes so the first message after a quiet
  spell is fast too.
- **IPv4 first**: some hotspots silently drop IPv6, which stalled new connections ~35 s.
- **DNS**: lookups are cached for 5 minutes in the bot, and Debian's resolver retries after 1 s
  instead of 5 s (`debian/resolv.conf`). A single lost DNS packet on a hotspot used to add 5 s
  to a reply. (Groq drops idle connections within ~30 s, so its reconnects rely on the cache.)
- **Remembered rate limits**: cooldowns are saved in `cooldowns.json`, so a restart doesn't
  retry models that are out of quota.
- **Less thinking**: models that support it get `reasoning_effort: low`.
- **Fast screen reading** with uiautomator2 (see above).
- **Opened apps are read once loaded**: `open_app` waits (up to 4 s) for the new app's elements.
  YouTube shows an empty window for ~1-2 s, which the screen read took as settled, and the AI then
  spent a step waiting 5 s ("open yt" took 17 s; now ~1.6 s to a readable screen).

### Web reading

`web_search` (DuckDuckGo's HTML page, no key) and `fetch_url` (a page's visible text, max ~6000
characters) answer things like "weather in Delhi" or "summarise this link" in a few seconds instead of
opening Chrome and reading the screen. Only public internet addresses are fetched, redirects included:
the phone itself, the home network and Tailscale addresses are refused, so a web page can't trick the
agent into calling services on them (e.g. the UI automation server). Code: `bridge/web.py`.

### Health alerts

Every 2 minutes the bot checks the battery (adb, or Termux:API if adb isn't up). Below 15% it sends one
Telegram alert (with whether it's charging); once it's back to 20% it says so and re-arms. Change
`BATTERY_LOW` / `BATTERY_REARM` in `bridge.py`.

### Motion watch

`/watch 10m front` (or "tell me if anything moves in the next 10 minutes") records 15 s clips with
the camera app, pulls 2 frames per second at 160x90 grey, and compares each frame with the previous
one. If more than 2% of the picture changes by a noticeable amount, you get a message and the clip
(at most one alert per minute). The clip sent is a 6 s, 640 px snippet starting 2 s before the
movement (~0.1-1 MB; a full clip can take minutes to upload from the phone). Clips are deleted right
after they're checked, so a long watch uses almost no storage (an hour of camera video would be
~7-8 GB). There's a ~7 s gap between clips while the camera restarts; the screen stays on with the
camera open while watching. The watch only holds the phone while recording: chat is answered
immediately, and an AI task that needs the screen runs between clips (the watch pauses meanwhile).
Maximum watch: 2 hours (continuous recording heats the phone).
Android blocks background camera access, which is why it uses the camera app instead of capturing
silently. Tune `MOTION_PIXEL_DIFF` / `MOTION_SHARE` in `device.py` if you get false alarms.

**Only people:** `/watch 1h people` (or "tell me if anyone enters the room") alerts only when a person
appears, not on light changes, curtains or pets. Each clip's moving frames (or one frame, when nothing
moved) go through EfficientDet-Lite0 on the phone (~65 ms per frame). You're told once when someone
enters, not every minute while they stay; after they leave, the next entry alerts again.

### Sound watch

`/listen 8h cry` (or "tell me if the baby cries tonight") listens with the microphone only, so the
camera and screen stay free for other tasks and a camera watch can run at the same time. One Opus
recording runs per half hour and is checked every 5 s while it's written, by YAMNet on the phone
(~14 ms per second of audio). When the sound is heard in 2 half-second windows within 10 s, you get
a message and the recording from 3 s before it (at most one alert per 2 minutes). Sounds: `cry`,
`scream`, `dog`, `alarm`, `glass`, `door` (doorbell or knock); add more in `SOUNDS` in `watch.py`
using [YAMNet's class names](https://github.com/tensorflow/models/blob/master/research/audioset/yamnet/yamnet_class_map.csv).
Maximum: 12 hours. While listening, `/audio` and the AI's `record_audio` are cut from the live
recording. Nothing is kept: each half-hour recording is deleted once checked.

Both smart watches run fully offline on the phone. They need `ai-edge-litert` in the venv (see
setup); the two models (~9 MB) are downloaded on first use and checked against pinned hashes.

### Playing Hill Climb Racing

`/play 30m` opens Hill Climb Racing and plays it by itself: it drives each run, closes the "Second
chance" popup (reviving would play an ad), taps through the results and starts the next run, then
messages a summary. `/stop` ends it early. It holds the screen the whole time, so tasks and commands
wait or report busy. Maximum: 3 hours.

It runs fully on the phone through the scrcpy server, not adb screenshots: ~30 frames a second at
640x294 (decoded by ffmpeg, the newest frame ~20 ms old) and multi-touch injected over a socket, so it
can hold the pedals down. The car is found by its red body; its tilt (the long axis of the red pixels)
decides the pedals: gas, let go above 30° nose-up, brake above 50°, looking 0.25 s ahead.

Menus are tapped only when the screen matches a saved picture of that exact button (`hillclimb_ui/`)
and the game is the app in front. Anything else (offers, ads, store pages) is never tapped; after 20 s
on one it stops and tells you. If another app comes to the front (the screen turns portrait), it
stops at once. The pictures and positions are for a 2340x1080 screen; on another phone the screens
won't match and it stops without tapping.

### Free tier limits (October 2026)

| Model | Per minute | Tokens / min | Per day |
|---|---|---|---|
| Gemini 3 Flash (`gemini-3-flash-preview`) | 5 | 250K | 20 |
| Gemini 3.6 Flash (`gemini-3.6-flash`) | 5 | 250K | 20 |
| Gemini 3.5 Flash Lite (`gemini-flash-lite-latest`) | 15 | 250K | 500 |
| Gemini 3.1 Flash Lite (`gemini-3.1-flash-lite`) | 15 | 250K | 500 |
| NVIDIA (all models together) | up to 40 | - | ~10,000 |
| OVHcloud `Qwen3.8-27B`, `Qwen3.6-27B` (no key) | 2 each, per IP address | - | - |
| Groq `openai/gpt-oss-120b` | ~30 | 8K | 1,000 |
| Groq `qwen/qwen3.8-27b` | ~30 | 8K (7K input) | 1,000 |
| Groq `whisper-large-v3` (voice notes) | 20 | - | 2,000 (8 h of audio) |
| Gemini 3.8 Flash Lite / Flash TTS (voice replies) | free; limits not published, see AI Studio | - | - |
| NVIDIA FLUX.1 [dev] (pictures) | free with the NVIDIA key; no separate image limit published | - | - |

Every agent step is one request, so the two Gemini Flash models cover only a few phone tasks a
day; the Flash Lite models do most of the work. Gemini limits are per Google Cloud project and reset at
midnight Pacific time, though quota has come back sooner in practice, so a model that runs out
is tried again every hour. Groq's daily limit is a rolling 24-hour window that it reports in
response headers, and `/usage` shows those numbers as Groq gives them. Check Gemini's at
aistudio.google.com/rate-limit. Free tiers change, so treat these numbers as a snapshot.

### Shortcuts

When a task finishes without going in circles, the steps that worked are saved, by button text
rather than number (numbers change): `open_app name=settings → tap "About phone" → ...`.
Requests need at least two keywords, so chat like "hi" never becomes a shortcut, and runs longer than
8 steps aren't saved (they usually wandered). File paths are saved as "the file from the previous
step". When a similar request comes in (at least half its keywords in common), the AI gets those steps
as a hint, so it skips the exploring but still checks each screen. A shorter route for the same
request replaces the old one.

When a request has **exactly the same keywords** as a saved one, the route is **replayed directly**,
without asking the AI at each step (about 1.3 s saved per step, and fewer free requests used). The AI
then looks at the final screen once and replies, or carries on if something differs. Replay stops at
the first step that fails and hands over to the AI. A similar but different request ("record 10
seconds" vs a saved "record 5 seconds") only gets the hint, never a replay. Routes with a tap by
coordinates aren't replayed, and shortcuts saved before this feature become replayable the next time
that request finishes. Calls, SMS, email, shell commands, contacts, memory and schedules
are never saved. `/shortcuts` lists them, `/delshortcut N` deletes one.

### After a reboot

Wireless debugging is trusted per Wi-Fi network: on a new one Android asks "Allow wireless debugging
on this network?", and nobody can answer that during a reboot. So while the phone is running, the bot
checks every minute that wireless debugging is on; on a new network it answers that prompt itself
("Always allow on this network" + Allow) and tells you. Only that exact prompt is answered, never
"Allow USB debugging?". Any network the phone has been on while running is therefore ready for a
reboot. If screen control still isn't back 6 minutes after a start, the bot messages you.

The same check turns **Wi-Fi** back on if it's off (with no SIM it's the phone's only connection, so
a mistaken tap on its switch would otherwise cut the bot off), and tells you once it's back. It uses
adb on the phone itself, which doesn't need a network. If that ever fails, plug in USB and run
`adb shell cmd wifi set-wifi-enabled enabled`.

**Changing Wi-Fi:** keep a phone hotspot saved on the M34 as a rescue network. If the home Wi-Fi
changes, turn the hotspot on near the phone; the bot comes online through it, then send
`/wifi "New network" "password"`. The new network gets trusted automatically. The one case that
still needs hands: the phone powered on somewhere with no saved network at all.


```
Phone boots
 ├─ Shizuku starts itself and turns Wireless debugging on
 └─ Termux:Boot runs
     ├─ 00-server       wake lock + start runit services (sshd, tg-bridge)
     └─ 10-adb-restore  find the Wireless debugging port, switch adb to 5555,
                        restart Shizuku → `phone` works again
```

---

## Folder contents

```
m34-agent/
├── bridge/
│   ├── bridge.py              Telegram bot, owner check, queue, approvals, scheduler, commands
│   ├── agent.py               agent loop, tool list, model router, safety rules
│   ├── device.py              phone actions: numbered elements, taps, apps, camera, files
│   ├── commands.py            instant /commands that skip the AI
│   ├── net.py                 shared HTTPS connection pool (IPv4 first, kept warm)
│   ├── speech.py              voice notes to text (Groq Whisper), voice replies (Gemini)
│   ├── imagegen.py            pictures from a description (FLUX.1 on NVIDIA)
│   ├── store.py               memory notes, scheduled tasks, shortcuts
│   ├── watch.py               camera watch (clips + frame comparison) and sound watch
│   ├── detect.py              on-device sound and person recognition (YAMNet, EfficientDet)
│   ├── game.py                /play: runs the Hill Climb Racing autopilot in the background
│   ├── hillclimb.py           the autopilot: reads the screen, drives, taps through menus
│   ├── hillclimb_ui/          saved button pictures the autopilot recognises screens by
│   ├── scrcpy.py              live screen frames and touch input through the scrcpy server
│   ├── web.py                 web search + page reading (public addresses only)
│   └── config.example.json    bot token + API keys template
├── debian/
│   ├── phone                  adb wrapper for Debian (/usr/local/bin/phone)
│   └── resolv.conf            DNS settings with 1s retries (/etc/resolv.conf)
├── termux/
│   ├── bin/phone              same wrapper for Termux ($PREFIX/bin/phone)
│   ├── boot/00-server         Termux:Boot: wake lock + services
│   ├── boot/10-adb-restore    Termux:Boot: bring adb back to port 5555
│   └── service/tg-bridge/run  runit service that runs the bridge
└── mac/
    └── m34                    optional: reconnect + open scrcpy from your computer
```

---

## Setup on a new phone

You need a computer with `adb` (Android platform-tools) and the phone on the same Wi-Fi.
Commands marked **[computer]** run on your computer; **[termux]** run in the Termux app.

### 1. Get keys (all free)

- **Telegram bot**: message `@BotFather` → `/newbot` → copy the token.
  Optional: `/setjoingroups` → Disable.
- **Gemini**: aistudio.google.com → Get API key → Create. Don't enable billing.
- **Groq**: console.groq.com → API Keys → Create. Copy it right away (shown once).
- **NVIDIA** (optional): build.nvidia.com → sign in (Google works) → verify a phone number by SMS →
  API Keys → Generate API Key. Shown once; it starts with `nvapi-`.

### 2. Prepare the phone

Settings → About phone → tap **Build number** 7 times. Then in Developer options turn on
**USB debugging** and **Wireless debugging**. Connect the phone by USB and accept the prompt.

**[computer]** useful 24/7 settings:

```sh
adb shell settings put global ota_disable_automatic_update 1  # no surprise OS updates
adb shell settings put global window_animation_scale 0       # no animations: screens settle faster
adb shell settings put global transition_animation_scale 0
adb shell settings put global animator_duration_scale 0
# Screen off after 2 min idle instead of always on (AMOLED burn-in, heat). Tasks, quick commands, the
# camera and the game autopilot wake it themselves (~1.4 s to wake and read). There must be no lock screen.
adb shell settings put global stay_on_while_plugged_in 0
adb shell settings put system screen_off_timeout 120000
# Android 12+ kills an app's child processes when it thinks there are too many ("signal 9" in Termux);
# Termux runs ~15 normally, more during watches. The first line stops Google's servers resetting the rest.
adb shell device_config set_sync_disabled_for_tests persistent
adb shell device_config put activity_manager max_phantom_processes 2147483647
adb shell settings put global settings_enable_monitor_phantom_procs false
adb shell settings put secure edge_enable 0                   # Samsung's edge panel handle on the right
adb shell settings put global heads_up_notifications_enabled 0  # no pop-up notifications over the screen
```

Then disable apps the bot doesn't use (undo any with `adb shell pm enable <package>`). Samsung Game
Booster floats a button over every game whose touch lock swallows all taps (the AI turned it on once);
the rest are unused helpers taking memory or popping up. Keep the Samsung keyboard, Knox, Game Optimizing
Service, Find My Mobile, Maps and YouTube.

```sh
for p in com.samsung.android.game.gametools \
  com.facebook.appmanager com.facebook.services com.facebook.system \
  com.microsoft.appmanager com.samsung.android.mdx com.samsung.android.mdx.kit \
  com.samsung.android.smartsuggestions com.samsung.android.rubin.app \
  com.samsung.android.bixbyvision.framework com.samsung.android.visionintelligence \
  com.samsung.android.aremoji com.samsung.android.stickercenter com.samsung.android.app.camera.sticker.facearavatar.preload \
  com.samsung.android.kidsinstaller com.samsung.android.app.parentalcare \
  com.samsung.android.samsungpass com.samsung.android.samsungpassautofill com.samsung.android.spayfw \
  com.sec.android.app.samsungapps com.sec.android.app.billing com.sec.android.easyMover.Agent \
  com.samsung.android.forest com.samsung.android.scloud com.samsung.android.app.dressroom \
  com.samsung.android.service.peoplestripe com.samsung.android.app.taskedge com.samsung.android.app.clipboardedge \
  com.samsung.android.app.watchmanagerstub com.google.android.apps.tachyon com.google.android.projection.gearhead \
  com.google.ar.core com.google.android.feedback com.google.android.apps.accessibility.voiceaccess \
  com.google.android.googlequicksearchbox; do adb shell pm disable-user --user 0 $p; done
adb shell pm uninstall --user 0 com.google.android.apps.bard  # Gemini can't be disabled; undo: pm install-existing
adb shell pm disable-user --user 0 com.aura.oobe.samsung      # AppCloud (ironSource): app-install ads, auto-installs
# No notifications from apps whose notifications only clutter the screen (promos, game reminders).
# Undo one: adb shell appops set --user 0 <package> POST_NOTIFICATION allow
for p in com.android.vending com.google.android.youtube com.google.android.apps.youtube.music \
  com.google.android.apps.photos com.google.android.apps.maps com.google.android.keep com.google.android.apps.docs \
  com.supercell.clashofclans com.fingersoft.hillclimb; do
  adb shell pm revoke $p android.permission.POST_NOTIFICATIONS; adb shell appops set --user 0 $p POST_NOTIFICATION ignore
done
```

Notifications stay on for Android, the clock (alarms), Phone, Messages, Termux, Shizuku and Tailscale.
Samsung's memory saver can't be muted on its own: it runs as the system user, so blocking it would mute
Android's own notifications too.

Also turn off Google Play Games' Sidekick tab, which sits over games: Play Store → profile picture →
Settings → General → Play Games Sidekick → Show Play Games Sidekick off. Check that Settings → Device care →
Auto optimisation → Auto restart is off, so the phone doesn't reboot on its own.

Also turn on **Battery protection** (stop charging at 80%) in Settings → Battery.
A phone kept at 100% for months can swell.

### 3. Install the apps

Download from GitHub releases (they must all come from GitHub, same signing key):
[Termux](https://github.com/termux/termux-app/releases),
[Termux:Boot](https://github.com/termux/termux-boot/releases),
[Shizuku](https://github.com/RikkaApps/Shizuku/releases).

**[computer]**

```sh
adb install -g termux-app_*arm64-v8a.apk
adb install -g termux-boot-app_*.apk
adb install shizuku-*.apk

for p in com.termux com.termux.boot moe.shizuku.privileged.api; do
  adb shell dumpsys deviceidle whitelist +$p
  adb shell cmd appops set $p RUN_ANY_IN_BACKGROUND allow
done
adb shell cmd appops set com.termux MANAGE_EXTERNAL_STORAGE allow
adb shell pm grant moe.shizuku.privileged.api android.permission.WRITE_SECURE_SETTINGS

adb shell monkey -p com.termux.boot -c android.intent.category.LAUNCHER 1   # open once to register
adb push m34-agent /sdcard/m34-agent
adb tcpip 5555                                                             # adb on a fixed port
```

### 4. Set up Shizuku (reboot recovery)

Open Shizuku → **Pairing** → Developer options → Wireless debugging →
**Pair device with pairing code**. Type the 6-digit code into Shizuku's notification.
Then make sure Shizuku's **Start on boot** switch is on.

### 5. Set up Termux

Open Termux, wait for the first-run setup, then **[termux]**:

```sh
pkg install -y openssh termux-services android-tools nmap proot-distro
SRC=/sdcard/m34-agent

cp $SRC/termux/bin/phone $PREFIX/bin/phone && chmod 700 $PREFIX/bin/phone
mkdir -p ~/.termux/boot && cp $SRC/termux/boot/* ~/.termux/boot/ && chmod 700 ~/.termux/boot/*

phone shell id   # an "Allow USB debugging?" prompt appears: tick "Always allow", tap Allow
```

No touchscreen? Use a mouse through `scrcpy`, or a USB-OTG mouse, for any on-screen prompt.

Close and reopen Termux once so the service manager starts.

For audio, contacts and SMS (Termux:API from GitHub releases too), **[computer]**:

```sh
adb install -g termux-api-app_*.apk
adb shell pm grant com.termux.api android.permission.RECORD_AUDIO
adb shell pm grant com.termux.api android.permission.READ_CONTACTS
adb shell pm grant com.termux.api android.permission.SEND_SMS   # only if the phone has a SIM
```

and **[termux]** `pkg install -y termux-api`.

### 6. Set up Debian and the bridge

**[termux]**

```sh
proot-distro install debian
proot-distro login debian -- bash -c \
  "apt update && apt install -y python3 python3-pil python3-venv adb ffmpeg curl && useradd -m -s /bin/bash agent"
proot-distro login debian --user agent -- bash -c \
  "python3 -m venv --system-site-packages ~/venv && ~/venv/bin/pip install uiautomator2 ai-edge-litert numpy"
proot-distro login debian --user agent -- bash -c \
  "mkdir -p ~/.cache/tg-bridge && curl -fL -o ~/.cache/tg-bridge/scrcpy-server \
   https://github.com/Genymobile/scrcpy/releases/download/v4.1/scrcpy-server-v4.1"   # only for /play

ROOTFS=$PREFIX/var/lib/proot-distro/containers/debian/rootfs
[ -d "$ROOTFS" ] || ROOTFS=$PREFIX/var/lib/proot-distro/installed-rootfs/debian   # older proot-distro

mkdir -p $ROOTFS/home/agent/.android $ROOTFS/home/agent/bridge $ROOTFS/home/agent/agent \
         $ROOTFS/home/agent/.config/tg-bridge
cp ~/.android/adbkey ~/.android/adbkey.pub $ROOTFS/home/agent/.android/   # reuse the approved adb key
cp $SRC/debian/phone $ROOTFS/usr/local/bin/phone && chmod 755 $ROOTFS/usr/local/bin/phone
cp $SRC/debian/resolv.conf $ROOTFS/etc/resolv.conf                          # 1s DNS retries
cp -r $SRC/bridge/*.py $SRC/bridge/hillclimb_ui $ROOTFS/home/agent/bridge/   # or clone the repo: see "Updating the code"
cp $SRC/bridge/config.example.json $ROOTFS/home/agent/.config/tg-bridge/config.json
proot-distro login debian -- bash -c \
  "chown -R agent:agent /home/agent && chmod 600 /home/agent/.config/tg-bridge/config.json /home/agent/.android/adbkey"
```

Put your real keys in the config:

```sh
nano $ROOTFS/home/agent/.config/tg-bridge/config.json
```

Device-specific: the first line of `SYSTEM_PROMPT` in `agent.py` names the phone and screen size.
Change it to yours (`adb shell wm size`).

### 7. Start the bot

**[termux]**

```sh
mkdir -p $PREFIX/var/service/tg-bridge
cp $SRC/termux/service/tg-bridge/run $PREFIX/var/service/tg-bridge/run
chmod 700 $PREFIX/var/service/tg-bridge/run
sv-enable tg-bridge

sleep 10; cat $ROOTFS/home/agent/.cache/tg-bridge/pair_code
```

In Telegram, open your bot and send `/pair <code>`. It replies "Paired". From now on it
ignores everyone else.

### 8. Test a reboot

Reboot the phone and wait about 3 minutes. `/status` in Telegram should answer.

---

## Using it

### Instant commands (no AI)

Common actions run directly, in about 1-3 seconds, and use no AI quota. They also appear in
Telegram's "/" menu.

| Command | Does |
|---|---|
| `/open youtube` | Open an app |
| `/url example.com` | Open a link |
| `/photo [front]` | Take a photo and send it |
| `/video 10 [front]` | Record a video (max 30 s) and send it (compressed first if over 20 MB) |
| `/audio 10` | Record audio (max 2 min) and send it |
| `/alarm 7:30 [label]` | Set an alarm |
| `/timer 5m [label]` | Start a timer (`90s`, `5m`, `1h30m`; a plain number means minutes) |
| `/volume 50 [media\|ring\|alarm\|notification]` | Set volume |
| `/brightness 40` | Set brightness |
| `/say hello` | Speak out loud |
| `/tap Settings` | Tap something by its text |
| `/type hello` | Type into the focused field |
| `/home`, `/back` | Keys |
| `/battery`, `/info` | Battery; model, Wi-Fi, IP, storage, uptime |
| `/wifi "Name" "password"` | Join a Wi-Fi network (quotes needed if there are spaces) |
| `/screenoff`, `/screenon` | Turn the screen off / on (tasks wake it again by themselves) |

Commands that change the phone wait their turn: while an AI task or a watch clip is using the
phone they reply "Busy", so nothing fights over the screen. `/battery`, `/info`, `/screen` and `/status` always work.

Photos take ~7.5 s: the camera opens in ~1 s and needs ~5 s to save a photo (Auto HDR is switched
off in the camera's settings, which saved ~1 s; it merges several frames per shot). Audio adds ~5 s
(Termux:API startup). `/say` replies at once and speaks in the background.

### AI tasks and control

| Message | Result |
|---|---|
| any task in plain words | the agent does it and replies |
| a voice note | transcribed (any language), shown back as "Heard: ...", then run as a task |
| "...answer in voice" | the reply comes as a voice message too |
| "draw / generate a picture of ..." | a new AI image, sent as a photo and kept in Pictures/AI |
| a photo, video or file | saved on the phone in `Download/Telegram`; its caption, if any, becomes a task about it |
| `/status` | current task, time, steps, model, queue |
| `/screen` | instant screenshot, no AI involved |
| `/memory`, `/forget N` | see memory notes, delete note N |
| `/schedules`, `/unschedule N` | see scheduled tasks, cancel task N |
| `/shortcuts`, `/delshortcut N` | see saved shortcuts, delete shortcut N |
| `/usage` | Table of AI requests per model against the free daily limits (Groq's own numbers), and which models are paused |
| `/model` | buttons to pick the AI model for tasks, or Auto (the default) |
| `/watch 10m [front\|back] [people]`, `/unwatch` | message me with a clip if anything moves (or only when someone enters); stop watching |
| `/listen 8h [cry\|scream\|dog\|alarm\|glass\|door]`, `/unlisten` | message me with a recording when I hear the sound (default `cry`); stop listening |
| `/play 30m` | play Hill Climb Racing by itself, then message a summary; `/stop` ends it |
| `/stop` | stop the task, watches and game, and clear the queue |
| `/new` | forget the conversation (memory notes stay) |

Examples: *"record a 10 second video from the front camera and send it"*,
*"remember that my name is Ayush"*, *"every day at 9:00 send me the battery level"*.

**Voice notes** go to Groq's `whisper-large-v3` with the Groq key the models already use (~0.3-0.5 s for
a short note). It was as fast as the turbo model in testing, and heard "सात बजे" as "7 बजे" where turbo
heard "साथ बजे" ("with o'clock"). The transcript is sent back so you can see what it understood.

**Voice replies.** Ask for one ("send me a voice note saying good morning in Hindi", "how much battery is
left? answer in voice") and the answer comes back as a Telegram voice message. The AI's `send_voice` tool
uses Gemini's free text-to-speech (`gemini-3.8-flash-lite-tts`, then `gemini-3.8-flash-tts`, each on every
Gemini key) with the voice Kore, in 100+ languages. The audio is streamed into ffmpeg as it's generated,
so a 7 s message is ready in ~3 s; generating first and converting after took ~6 s (ffmpeg alone needs
~1 s to start in Debian).

**Pictures.** "Generate a photo of a dog" or "make a wallpaper of mountains at sunset" makes a new image
with FLUX.1 [dev] on NVIDIA's free API (the NVIDIA key the chat models use; ~8 s for 1024x1024, ~15 s for
the whole task), sends it to you as a photo and keeps a copy in `/sdcard/Pictures/AI` (so "set it as the
wallpaper" works after). Code: `bridge/imagegen.py`. Gemini's image models have no free tier (429 on the
first request), and NVIDIA's faster FLUX.1 [schnell] endpoint didn't answer within 3 minutes when tested.
FLUX.1 [dev]'s licence covers personal, non-commercial use.

**Files** you send (up to 20 MB, Telegram's limit for bots) land in `/sdcard/Download/Telegram`, renamed
if the name is taken, and photos and videos are indexed so Gallery shows them. With a caption ("set this as
the wallpaper") the caption becomes a task that knows where the file is, and a photo or image is also shown
to the AI, shrunk to 1280 px on its longest side, for every step of the task (screenshots are dropped as
newer ones arrive; your photo isn't). Without one you get "Saved on the phone: ...", and your next task is
told about the file. Photos of an album arrive together and are saved one at a time, so two with the same
name both keep theirs (the second gets `_1`). Text sent as a voice reply loses Markdown marks first, so
`**bold**` isn't read out.

Tasks that use the phone show one live **progress message** that updates with every step. It
appears with the first action, so plain chat ("hi") just gets the reply, about 0.4 s sooner.

Text replies **stream** as the AI writes them: the bridge asks the models for a streamed answer and
shows it growing in the chat (Telegram's `sendMessageDraft`, redrawn at most every 0.6 s), then
sends the finished reply as a normal message. Text a model writes before an action is cleared.

Replies are formatted: the AI's Markdown is rendered as Telegram HTML (bold, italics, `code`,
links, bullet points), tables and code blocks become aligned monospace blocks, and headings become
bold. If Telegram rejects the formatting the reply is resent as plain text.

The AI is given the phone's fixed specs (model, Android and One UI version, chipset, total RAM and
storage, screen size) in its prompt, so it answers questions about the phone from fact instead of
guessing. Live values (free space, battery) are still read with phone_status.

### Logs

```sh
# [termux]
tail -f $PREFIX/var/lib/proot-distro/containers/debian/rootfs/home/agent/.cache/tg-bridge/bridge.log
```

### Restart the bot

```sh
# [termux]
sv restart tg-bridge
```

---

## Updating the code (GitHub)

The bot runs straight from a git checkout on the phone (`/home/agent/m34-agent` inside Debian).

```
computer: edit -> commit -> git push  ──►  GitHub (private repo)  ──►  Telegram: /update
```

`/update` fetches, fast-forwards, checks that every `bridge/*.py` compiles, then restarts into the new
code and replies with the commits it pulled. If the new code doesn't compile it stays on the old
commit. It refuses while a task or watch is running. The phone pulls with a **read-only deploy
key**, so it can't change the repository or reach your other repos.

Setup on the phone (Debian, user `agent`):

```sh
apt install -y git openssh-client                      # as root
ssh-keygen -t ed25519 -N "" -f ~/.ssh/m34_deploy -C m34-deploy
# add ~/.ssh/m34_deploy.pub as a read-only deploy key: repo Settings -> Deploy keys
#   (or from the computer: gh repo deploy-key add m34_deploy.pub --repo <you>/m34-agent --title m34)
printf 'Host github.com\n  IdentityFile ~/.ssh/m34_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
git clone git@github.com:<you>/m34-agent.git ~/m34-agent
```

Files outside `bridge/` (Termux boot scripts, the service script, `phone`) aren't touched by `/update`;
copy them by hand when they change.

## Optional: reach the phone from anywhere (Tailscale)

[Tailscale](https://tailscale.com) puts the computer and the phone on a private, encrypted network,
so SSH, adb and scrcpy work from any network with one fixed address. Nothing is exposed to the
internet.

1. Install Tailscale on the computer and sign in.
2. Lock the phone down so it can be reached but can't reach your other devices. In the admin
   console, Access controls:
   ```json
   {
     "tagOwners": { "tag:agent": ["autogroup:admin"] },
     "grants": [
       { "src": ["autogroup:member"], "dst": ["autogroup:member"], "ip": ["*"] },
       { "src": ["autogroup:member"], "dst": ["tag:agent"], "ip": ["*"] }
     ]
   }
   ```
3. Generate an auth key with the tag `tag:agent` (tagged devices also don't expire).
4. On the phone: install Tailscale from the Play Store, open Settings, Accounts, menu,
   "Use an auth key", paste it, Add account. Then **[computer]**:
   ```sh
   adb shell dumpsys deviceidle whitelist +com.tailscale.ipn
   adb shell cmd appops set com.tailscale.ipn RUN_ANY_IN_BACKGROUND allow
   adb shell settings put secure always_on_vpn_app com.tailscale.ipn   # reconnects on its own
   adb shell settings put secure always_on_vpn_lockdown 0
   ```
5. Use the phone's Tailscale address (`100.x.y.z`) as `HostName` in `~/.ssh/config` and as `M34_IP`
   in `mac/m34`. Keep a second SSH entry with the home Wi-Fi address as a fallback.

**Which path is used.** `ssh m34` and `m34` always go to the Tailscale address; Tailscale picks the
route by itself:

| Where the computer and phone are | Path | Speed in testing |
|---|---|---|
| Same network (e.g. both on home Wi-Fi) | Tailscale, **direct** over the local network | ~30 ms |
| Different networks (e.g. computer on a mobile hotspot) | Tailscale, through a **relay** server (a hotspot's NAT blocks a direct path) | ~130-150 ms, ~3 Mbps |

Both are encrypted end to end. On a relay, lower scrcpy's quality: `M34_BITRATE=2M M34_MAX_SIZE=1024 m34`.
Check the current path with `tailscale status` (`direct <ip>` or `relay "<city>"`).

**Automatic fallback.** If the phone's Tailscale address doesn't answer (for example Tailscale is
turned off on the computer), `ssh m34` and `m34` switch to the home Wi-Fi address by themselves. That
only works on the home Wi-Fi, and costs ~2 s for the failed check. In `~/.ssh/config`, placed before
`Host m34` (ssh keeps the first `HostName` it sees):

```
Match originalhost m34 !exec "nc -z -G 2 <tailscale-ip> 8022 >/dev/null 2>&1"
    HostName <home-wifi-ip>
```

`mac/m34` does the same (`TAILSCALE_IP`, then `HOME_IP`; `M34_IP=` still overrides both).
`ssh m34lan` always skips Tailscale.

## Optional: control from your computer

- **Mirror the screen**: `scrcpy --tcpip=<phone-ip>:5555`
- **SSH into Termux**: run `sshd` in Termux, add your public key to `~/.ssh/authorized_keys`,
  then `ssh -p 8022 <phone-ip>`
- **mac/m34**: reconnects adb on port 5555 (via USB or Wireless debugging if needed) and opens
  scrcpy. Set `M34_IP`, `M34_SERIAL` and the scrcpy path at the top to match your setup.
  `m34 --watch` keeps reconnecting.
- **Static IP**: Wi-Fi → network settings → IP settings → Static, so the address never changes.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `409 Conflict` in the log | Two bridges are running. `sv restart tg-bridge` (the run script kills strays) |
| "busy at Google" in the progress message | Google's servers are overloaded (HTTP 503). Usually passes within minutes |
| "used up today's quota" | Free daily limits; Gemini resets at midnight Pacific time |
| Camera tasks fail | The camera tools use the volume key as shutter (Samsung default). Check the camera app's "shooting methods" setting |
| Time zone error at start | Old zone name from Android; add it to `LEGACY_ZONES` in `store.py` |
| "All models are rate-limited" | Wait, or add more providers/keys |
| Termux suddenly stopped | Revoking any permission from Termux or Termux:API force-stops Termux (they share a process). Open Termux once, or reboot |
| Bot says it "can't control the screen" after a reboot | The phone is on a Wi-Fi network it hasn't used before; Android asks "Allow wireless debugging on this network?" and nobody answers. Plug in USB, `adb tcpip 5555`, then allow it once with "Always allow on this network" |
| `phone` fails after reboot | Check `~/.adb-restore.log` in Termux. Make sure Shizuku "Start on boot" is on |
| Slow on a mobile hotspot | First connection can take a few seconds; later ones reuse a shared pool |
| Everything a bit slow | Close scrcpy when not watching: its video encoding uses a lot of the phone's CPU |
| Where did the time go? | The log has `reached the bot ~Ns after sending`, `<model>: HTTP 200 in Ns` per AI call, and `replied Ns after starting` per task |
| Groq returns 403 / 1010 | Requests need a User-Agent header (already set in `agent.py`) |

---

## Good to know

- **Saved state survives crashes**: memory, schedules, shortcuts, history, usage, cooldowns and the
  config are written to a temporary file and swapped in, so power loss mid-save keeps the old
  version. A file that's damaged anyway is kept aside as `<name>.damaged-<time>` and logged.

- **Privacy**: free Gemini may use prompts (including screenshots) to improve Google's models.
  Use a throwaway Google account on the phone and keep personal apps off it.
- **Keys are passwords**: if they leak, regenerate them (AI Studio, Groq console, `/revoke` in BotFather).
- **Free tier limits** change often. Check the provider dashboards if something stops working.
- **Port 5555** is unencrypted adb. Keep it on your home network; never port-forward it.
