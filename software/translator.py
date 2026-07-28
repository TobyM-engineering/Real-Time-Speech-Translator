"""
Real-time speech translator for a headless Raspberry Pi.

Listens to a room through a USB microphone (a ReSpeaker Lite),
streams the audio to OpenAI's realtime translation API, and plays
the English translation into Bluetooth earbuds (or wired headphones
in TRANSLATOR_WIRED=1 mode). Includes same-language filtering so
English speech isn't parroted back, a fallback pipeline for single
words the streaming model won't translate, a push-to-translate
button, and an RGB status LED.

Configuration lives in a .env file next to this script (API key,
Bluetooth device address) and in the SETTINGS block below. See the
README for hardware wiring and setup instructions.
"""

from __future__ import annotations

import array
import base64
import difflib
import io
import json
import math
import os
import queue
import re
import select
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from collections import deque
from datetime import datetime

import websocket
from dotenv import load_dotenv
from gpiozero import Button, RGBLED

# Loaded at import time so the SETTINGS constants below can read
# values from .env. (main() calls load_dotenv() again; harmless.)
load_dotenv()


# ============================================================
# SETTINGS
# ============================================================

OPENAI_URL = (
    "wss://api.openai.com/v1/realtime/translations"
    "?model=gpt-realtime-translate"
)

TARGET_LANGUAGE = "en"

# Your Bluetooth earbuds' MAC address. Set AIRPODS_MAC in the .env
# file next to this script (pair the earbuds once with
# `bluetoothctl`, then find the address via `bluetoothctl devices`
# or `bluetoothctl scan on`). AIRPODS_NAME is the display name as
# it appears in `wpctl status`, used to find the audio sink.
AIRPODS_MAC = os.getenv("AIRPODS_MAC", "XX:XX:XX:XX:XX:XX")
AIRPODS_NAME = os.getenv("AIRPODS_NAME", "AirPods Pro")

# Wired test mode (TRANSLATOR_WIRED=1): skip every Bluetooth step and
# play through the current default PipeWire sink instead -- e.g.
# headphones plugged into the ReSpeaker Lite's jack. Used to isolate
# pipeline problems from the flaky Bluetooth dongle.
WIRED_OUTPUT = os.getenv("TRANSLATOR_WIRED") == "1"

SAMPLE_RATE = 24_000
CHANNELS = 1
BYTES_PER_SAMPLE = 2

# NEW: a dead microphone stream is reopened in place rather than
# shutting the whole translator down -- the common cause is a
# restart race (the previous instance's arecord still holding the
# device for a moment) or the ReSpeaker's USB clock interface
# wedging on stream reopen (err -71 / "cannot get clock validity"
# in dmesg, arecord failing to install hw params). Plain reopens
# are tried first; if those fail, the parent USB hub gets a bind
# cycle, which is the only software recovery that has revived a
# wedged ReSpeaker (a hard wedge still needs a physical replug).
MICROPHONE_REOPEN_ATTEMPTS = 5
MICROPHONE_REOPEN_DELAY_SECONDS = 2.0
MICROPHONE_REOPEN_USB_RESET_AT = 2
MICROPHONE_HUB_USB_PORT = "1-1"

MICROPHONE_CHUNK_MS = 100
MICROPHONE_CHUNK_BYTES = (
    SAMPLE_RATE
    * CHANNELS
    * BYTES_PER_SAMPLE
    * MICROPHONE_CHUNK_MS
    // 1000
)

PLAYBACK_LATENCY_MS = 150

# 1.03 is deliberate and load-bearing: during nonstop talking the
# model produces audio at (slightly over) real time, and playing at
# exactly 1.0 means backlog only ever grows -- latency crept to ~30s
# in live testing. 3% faster drains roughly a second of backlog per
# half minute while being inaudible. (The choppiness once blamed on
# this speedup was actually the pinned-at-cap buffer bug below.)
PLAYBACK_SPEED = 1.03
PLAYBACK_RATE = round(SAMPLE_RATE * PLAYBACK_SPEED)

# The cap bounds worst-case latency during nonstop talking (the
# backlog drains naturally at every pause in speech, so it only
# matters for minutes-long monologues). When tripped it discards the
# OLDEST queued audio -- words are lost -- so it is a last resort:
# big enough to be rare, small enough that latency stays usable.
MAX_BACKLOG_SECONDS = 10.0
CATCH_UP_TO_SECONDS = 1.5

PLAYBACK_WRITE_TIMEOUT_SECONDS = 0.75

STARTUP_RETRY_ATTEMPTS = 3
STARTUP_RETRY_DELAY_SECONDS = 3
RECONNECT_RETRY_ATTEMPTS = 3
RECONNECT_RETRY_DELAY_SECONDS = 2

# NEW: this used to be 1 second, applied to the whole websocket
# connection (both receiving translations AND sending microphone
# audio, since they share one socket). Any WiFi hiccup longer than
# 1 second was being treated as a fatal error, killing the whole
# session. 10 seconds gives real breathing room while still letting
# Ctrl+C be noticed reasonably quickly.
WEBSOCKET_IDLE_TIMEOUT_SECONDS = 10
WEBSOCKET_CONNECT_TIMEOUT_SECONDS = 30

# Same-language suppression. The translation model sometimes echoes
# speech back instead of staying silent when the spoken language
# already matches the target language. We detect this ourselves by
# comparing the source-language transcript (from gpt-realtime-whisper)
# against the translated transcript for the current phrase, and mute
# that phrase's audio if they're nearly identical.
#
# To make this correct rather than a race against timing, each
# phrase's audio is held in memory until either a same/different
# -language decision can be made, or the phrase ends (in which case
# we fail open and play it, so we never silently eat a real
# translation). This means phrases where the source-language
# transcript arrives slowly will play a bit later than instant
# streaming would -- if that latency ever feels worse than the
# occasional English echo, set this to False to go back to
# instant, unfiltered streaming.
SAME_LANGUAGE_FILTER_ENABLED = True
SAME_LANGUAGE_SIMILARITY_THRESHOLD = 0.60
MIN_CHARACTERS_BEFORE_COMPARING = 8

# Speed optimization: most phrases are genuine translations, and
# translated text diverges from the original almost immediately
# for any real language pair. So we check early, with very little
# text, whether things already look clearly different -- if so we
# release the audio right away instead of waiting for the fuller
# comparison. The slower, more careful comparison above is only
# needed to decide the rarer case (muting an English echo), where
# being wrong actually costs something.
EARLY_RELEASE_MIN_CHARACTERS = 6
EARLY_RELEASE_MAX_SIMILARITY = 0.35

# All similarity checks compare equal-length prefixes of the two
# transcripts. The raw SequenceMatcher ratio is 2*matches/(lenA+lenB),
# so it directly penalizes a length difference -- and the two
# transcripts stream at very different speeds, meaning a perfect
# English echo scored as "different" whenever one transcript was
# ahead of the other (a 3-char output against a 17-char input can
# never reach 0.35). That length mismatch is why the filter never
# muted anything, regardless of threshold tuning.

# If no same/different decision has been made by the time this much
# audio is held for the current phrase, fail open and play it. This
# bounds worst-case latency when the source transcript is very late,
# at the cost of letting a rare very-late-transcript echo through.
# 3.0 is the tuned compromise between 2.5 (leaked the first words
# of English phrases before their transcript arrived) and 4.0
# (which pushed total latency to ~10s). The rolling release queue
# catches echo retroactively after this point, so this only guards
# the leading edge of a phrase.
PENDING_HOLD_MAX_SECONDS = 3.0
PENDING_HOLD_MAX_BYTES = int(
    SAMPLE_RATE
    * CHANNELS
    * BYTES_PER_SAMPLE
    * PENDING_HOLD_MAX_SECONDS
)

# NEW: for short phrases that end before either transcript reaches
# MIN_CHARACTERS_BEFORE_COMPARING (very common in normal back-and-forth
# conversation -- "Very well", "Thank you", etc.), we used to just give
# up and play the audio. Now we get one last look with whatever text
# exists, using a stricter threshold than the normal cautious path
# since there's less text to be confident in.
FINAL_CHANCE_MIN_CHARACTERS = 3
FINAL_CHANCE_SIMILARITY_THRESHOLD = 0.75

# NEW: rolling mid-phrase echo detection. The per-phrase decision
# above only works when speech has pauses in it -- live testing
# showed continuous talking runs an entire session together as ONE
# phrase (a 2-minute phrase in one log), so a one-shot decision made
# in its first seconds governed everything after it, and English
# spoken mid-stream played right through. This layer keeps comparing
# the newest input text against the newest output text after the
# initial decision, muting when the output starts tracking the input
# verbatim (an echo) and unmuting when they diverge again (real
# translation resumed). Asymmetric thresholds give it hysteresis so
# it doesn't flap. It can only stop FUTURE audio -- a few seconds of
# an echo's start still get through before there's enough text to
# call it; fully suppressing phrase-initial English is the held-
# audio path's job.
ROLLING_ECHO_TAIL_CHARS = 60
ROLLING_ECHO_WINDOW_CHARS = 200
ROLLING_ECHO_MIN_TAIL_CHARS = 20
ROLLING_ECHO_MUTE_SCORE = 0.72
ROLLING_ECHO_UNMUTE_SCORE = 0.45

# NEW: how long post-decision audio is held before its play/drop
# fate is decided. The transcripts the echo detector reads lag the
# audio by a couple of seconds, so deciding a chunk's fate the
# moment it arrives meant every late realization was irreversible:
# late mutes had already leaked the echo, and late unmutes had
# already discarded the start of a genuine translation (heard as
# whole Spanish sentences going unanswered). Holding audio this
# long lets a decision made now apply to the audio it was actually
# about. Costs this much extra latency on everything played.
ROLLING_HOLD_SECONDS = 2.0

# NEW: the phrase-end event this code listens for
# (session.output_transcript.done) has never once appeared in the
# debug log despite whole sessions of translation -- so phrase state
# is also finished and reset after this much silence from the model's
# output stream. Without this, the first phrase's same/different
# decision stayed locked for the entire session and the filter never
# ran again (the reason English kept playing through).
PHRASE_SILENCE_RESET_SECONDS = 1.5

# NEW: fallback pipeline for short phrases the streaming translation
# model never responds to at all (its known "needs enough audio to
# commit" limitation). Rather than guessing at audio, this reuses the
# source-language TEXT we already get from Whisper (which handles
# short utterances fine), sends it through a plain one-shot text
# translation call, then speaks the result with text-to-speech.
#
# This is a genuinely separate, less battle-tested path than the rest
# of the file -- if it ever misbehaves, set FALLBACK_ENABLED to False
# to disable it without touching anything else.
FALLBACK_ENABLED = True
FALLBACK_SILENCE_TIMEOUT_SECONDS = 1.8
FALLBACK_MIN_INPUT_CHARACTERS = 2
FALLBACK_CHECK_INTERVAL_SECONDS = 1.5
FALLBACK_TRANSLATE_MODEL = "gpt-4o-mini"
FALLBACK_TTS_MODEL = "gpt-4o-mini-tts"
FALLBACK_TTS_VOICE = "alloy"
FALLBACK_REQUEST_TIMEOUT_SECONDS = 15.0

# NEW: audio arm of the fallback. Isolated single words often produce
# NO streaming transcript at all (source='' in the log), so the text
# fallback above never even arms. The microphone worker keeps a short
# rolling buffer of raw mic audio and notes when speech energy was
# last heard; if speech happened but neither the realtime model nor
# the streaming transcript produced anything, the held audio itself
# is sent to a one-shot transcription call, then continues down the
# same translate + speak path as the text arm.
FALLBACK_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"

# RMS level (16-bit samples, full scale 32767) above which a mic
# chunk counts as speech. Measured ambient room level is ~330. If
# single words are still missed, lower this; if the fallback fires
# on room noise, raise it. The audio fallback logs the transcription
# it heard, so misfires are visible either way.
SPEECH_RMS_THRESHOLD = 450

MIC_ROLLING_BUFFER_SECONDS = 12.0
MIC_PREROLL_SECONDS = 0.5

FALLBACK_MIN_AUDIO_BYTES = int(
    SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * 0.3
)

# NEW: the realtime model emits SOME output audio for nearly every
# utterance -- even single words it declines to translate -- so
# "did any output arrive" proved useless as a did-it-respond
# signal: one logged session showed it suppressing the audio
# fallback for literally every held word. A response only counts
# if enough NON-SILENT output audio arrived since the current
# utterance began; anything less and the fallback still runs.
FALLBACK_MEANINGFUL_OUTPUT_SECONDS = 0.6
FALLBACK_MEANINGFUL_OUTPUT_BYTES = int(
    SAMPLE_RATE
    * CHANNELS
    * BYTES_PER_SAMPLE
    * FALLBACK_MEANINGFUL_OUTPUT_SECONDS
)
OUTPUT_LOUD_RMS_THRESHOLD = 200

# NEW: LED feedback, deliberately sparse: green = listening, white
# = button capture ("say your word now"), blue = working on an
# answer, two quick red blinks = heard you but decided not to
# answer (nothing usable, or it was already English). There is NO
# "heard sound" color on purpose -- a first version lit magenta on
# any mic energy, which in a real room is nearly constant, so the
# LED sat purple and conveyed nothing. Also: this LED renders cyan
# indistinguishably from green and yellow as a muddy lime --
# neither may be used for status colors on this hardware.
LED_LISTENING_COLOR = (0, 1, 0)
LED_WORKING_COLOR = (0, 0, 1)
LED_CAPTURE_COLOR = (1, 1, 1)
LED_DECLINED_COLOR = (1, 0, 0)

# NEW: short-press button capture -- push-to-translate. A press
# arms a deliberate capture window: the mic buffer is cleared, the
# next stretch of speech (up to the max, or until this much
# trailing silence) is transcribed and translated directly,
# bypassing every automatic heuristic. Exists because ambient
# audio (a TV in the room) keeps the automatic single-word path
# from ever seeing a clean silence gap.
BUTTON_CAPTURE_MAX_SECONDS = 6.0
BUTTON_CAPTURE_SILENCE_SECONDS = 1.0

# NEW: the automatic audio arm only chases SHORT bursts of speech.
# Longer continuous sound is a conversation, a TV, or music -- the
# realtime model's territory -- and transcribing rolling chunks of
# it burned API calls on noise and kept the status LED stuck in
# "working".
FALLBACK_MAX_UTTERANCE_SECONDS = 3.5

LOG_PATH = os.path.expanduser("~/translator_debug.log")

# NEW: physical status LED and button.
# LED colors double as a status display for a headless device with no
# screen. Startup: blue-blinking = waiting for internet,
# magenta-blinking = waiting for a Bluetooth controller (was cyan,
# which reads as green on this LED and looked like a crash loop),
# white = internet found, connecting Bluetooth, red-blinking = a
# startup step failed and we're retrying. Running: green =
# listening, white = button capture, blue = working on an answer,
# two quick red blinks = heard you but not answering (nothing
# usable, or it was already English).
BUTTON_PIN = 17
LED_RED_PIN = 27
LED_GREEN_PIN = 22
LED_BLUE_PIN = 23

# Set to False if your RGB LED is common-anode rather than common-cathode.
LED_ACTIVE_HIGH = True

# Holding the button this long triggers a full clean restart of the
# translator (via systemd), useful if Bluetooth ever gets stuck, without
# needing a keyboard or screen.
BUTTON_RESTART_HOLD_SECONDS = 3

# What we check to confirm there's a real, usable internet connection
# (not just an associated WiFi network -- hotspots can connect at the
# link layer but still have no working internet for a few seconds).
CONNECTIVITY_CHECK_HOST = ("api.openai.com", 443)
CONNECTIVITY_CHECK_TIMEOUT_SECONDS = 3

# The dongle's USB port, for escalating recovery: when the RTL8761B
# wedges hard enough that the kernel's own automatic resets fail
# ("Read reg16 failed (-110)" repeating in dmesg), an unbind/rebind
# of the port re-enumerates it cleanly. Verified to revive a dongle
# the kernel could not. Update this if the dongle moves ports.
BLUETOOTH_DONGLE_USB_PORT = "1-1.2"
BLUETOOTH_RECOVERY_RETRY_SECONDS = 30


# ============================================================
# LOGGING
# ============================================================

_log_lock = threading.Lock()


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"[{timestamp}] {message}"

    with _log_lock:
        try:
            with open(LOG_PATH, "a") as log_file:
                log_file.write(line + "\n")
        except Exception:
            pass

    print(line, flush=True)


def retry(
    label: str,
    attempts: int,
    delay_seconds: float,
    func,
):
    """Run func(), retrying on exception instead of failing once."""

    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as error:
            last_error = error
            log(
                f"{label} failed on attempt {attempt}/{attempts}: "
                f"{error}"
            )

            if attempt < attempts:
                time.sleep(delay_seconds)

    raise RuntimeError(
        f"{label} failed after {attempts} attempts: {last_error}"
    )


# ============================================================
# STATUS LED AND CONNECTIVITY
# ============================================================

def has_internet() -> bool:
    """
    Checks for a real, working internet connection -- not just an
    associated WiFi network. Hotspots can connect at the link layer
    for a few seconds before DNS/routing is actually ready, so we
    check by trying to reach the one host we actually need.
    """

    try:
        with socket.create_connection(
            CONNECTIVITY_CHECK_HOST,
            timeout=CONNECTIVITY_CHECK_TIMEOUT_SECONDS,
        ):
            return True
    except OSError:
        return False


def wait_for_internet(led: RGBLED) -> None:
    """Blinks the LED blue until a real internet connection is found."""

    log("Waiting for internet connection...")

    blink_on = True

    while not has_internet():
        led.color = (0, 0, 1) if blink_on else (0, 0, 0)
        blink_on = not blink_on
        time.sleep(0.5)

    led.color = (0, 0, 1)
    log("Internet connection confirmed.")


def has_bluetooth_controller() -> bool:
    """
    Checks whether BlueZ has actually registered a working Bluetooth
    controller yet. On boot, bluetoothd can start before the USB
    dongle has finished enumerating, leaving it running with no
    controller at all -- rfkill and systemd can both look perfectly
    healthy while this is happening.
    """

    try:
        result = subprocess.run(
            ["bluetoothctl", "list"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def find_usb_port_by_vendor(vendor_id: str) -> str | None:
    """
    Locates a USB device's current port ("1-1.1") by vendor id.
    Ports shift when devices get replugged into different sockets,
    and a reset aimed at a stale hardcoded port resets nothing --
    exactly what happened when the Bluetooth dongle moved from
    1-1.2 to 1-1.1.
    """

    import glob

    for vendor_file in glob.glob(
        "/sys/bus/usb/devices/*/idVendor"
    ):
        try:
            with open(vendor_file) as handle:
                if handle.read().strip() == vendor_id:
                    return os.path.basename(
                        os.path.dirname(vendor_file)
                    )
        except OSError:
            continue

    return None


BLUETOOTH_DONGLE_VENDOR_ID = "2357"  # TP-Link


def reset_bluetooth_dongle_usb() -> None:
    """
    Unbind/rebind the dongle's USB port -- a deeper reset than the
    kernel's automatic device resets, for when the RTL8761B has
    wedged hard enough that those fail.
    """

    port = (
        find_usb_port_by_vendor(BLUETOOTH_DONGLE_VENDOR_ID)
        or BLUETOOTH_DONGLE_USB_PORT
    )

    log(
        "Resetting the Bluetooth dongle at the USB level "
        f"(port {port})..."
    )

    for action in ("unbind", "bind"):
        subprocess.run(
            ["sudo", "tee", f"/sys/bus/usb/drivers/usb/{action}"],
            input=port,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        time.sleep(2)


def reset_microphone_usb() -> None:
    """
    Bind-cycles the hub the ReSpeaker sits behind. Also briefly
    resets everything else on that hub (the Ethernet adapter, and
    the Bluetooth dongle when present) -- acceptable, since without
    a working microphone the translator is dead anyway.
    """

    log("Resetting the USB hub to revive the microphone...")

    for action in ("unbind", "bind"):
        subprocess.run(
            ["sudo", "tee", f"/sys/bus/usb/drivers/usb/{action}"],
            input=MICROPHONE_HUB_USB_PORT,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
        time.sleep(3)

    # Give the sound card time to re-register before arecord tries.
    time.sleep(5)


def recent_playback_interface_failure(
    window_seconds: float = 45.0,
) -> bool:
    """
    True if the kernel logged a USB audio interface failure in the
    last window_seconds -- the signature of the ReSpeaker's
    playback side coming up wedged ("usb_set_interface failed" for
    interface 1:1). PipeWire keeps accepting audio into a wedged
    sink without complaint, so this is the only way to notice
    before the user does.
    """

    try:
        kernel_log = subprocess.check_output(
            ["dmesg"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )

        with open("/proc/uptime") as uptime_file:
            uptime = float(uptime_file.read().split()[0])

    except Exception:
        return False

    cutoff = uptime - window_seconds

    for line in kernel_log.splitlines():
        if "usb_set_interface failed" not in line:
            continue

        match = re.match(r"\[\s*(\d+\.\d+)\]", line)

        if match and float(match.group(1)) >= cutoff:
            return True

    return False


def wait_for_bluetooth_controller(led: RGBLED) -> None:
    """
    Blinks the LED magenta until BlueZ actually has a controller.
    Escalates through recovery steps while waiting, rather than the
    old behavior of one bluetooth-service restart and then blinking
    forever: first a bluetooth service restart (the fix for the boot
    race where the dongle enumerates after bluetoothd started), then
    a USB-level reset of the dongle itself (the fix for the RTL8761B
    wedging so hard the kernel's own resets fail), alternating every
    BLUETOOTH_RECOVERY_RETRY_SECONDS until the controller appears.
    """

    log("Waiting for a Bluetooth controller...")

    # A leftover rfkill soft-block leaves the controller visible
    # but unpowerable ("adapter-not-powered" on every connect);
    # clearing it is harmless when there is no block.
    subprocess.run(
        ["sudo", "rfkill", "unblock", "bluetooth"],
        check=False,
        timeout=10,
    )
    subprocess.run(
        ["bluetoothctl", "power", "on"],
        capture_output=True,
        check=False,
        timeout=10,
    )

    blink_on = True
    waited_seconds = 0.0
    next_recovery_at = 10.0
    recovery_attempts = 0

    while not has_bluetooth_controller():
        led.color = (1, 0, 1) if blink_on else (0, 0, 0)
        blink_on = not blink_on
        time.sleep(0.5)
        waited_seconds += 0.5

        if waited_seconds >= next_recovery_at:
            recovery_attempts += 1
            next_recovery_at = (
                waited_seconds + BLUETOOTH_RECOVERY_RETRY_SECONDS
            )

            if recovery_attempts % 2 == 0:
                reset_bluetooth_dongle_usb()

            log(
                "No Bluetooth controller yet; restarting the "
                f"bluetooth service (attempt {recovery_attempts})."
            )
            subprocess.run(
                ["sudo", "systemctl", "restart", "bluetooth"],
                check=False,
            )

    led.color = (1, 0, 1)
    log("Bluetooth controller confirmed.")


# ============================================================
# SHORT-PHRASE FALLBACK (single words, very short utterances)
# ============================================================

def _call_openai_json(
    path: str,
    payload: dict,
    api_key: str,
) -> dict:
    request = urllib.request.Request(
        f"https://api.openai.com/v1/{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(
        request, timeout=FALLBACK_REQUEST_TIMEOUT_SECONDS
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def _call_openai_binary(
    path: str,
    payload: dict,
    api_key: str,
) -> bytes:
    request = urllib.request.Request(
        f"https://api.openai.com/v1/{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(
        request, timeout=FALLBACK_REQUEST_TIMEOUT_SECONDS
    ) as response:
        return response.read()


def translate_short_phrase(text: str, api_key: str) -> str:
    """
    One-shot text translation for a short phrase, using a plain chat
    completion instead of the streaming realtime model. Unlike the
    realtime model, this handles single words and short phrases
    reliably, since it isn't waiting to decide whether "enough" was
    said. If the text is already English, it's asked to return it
    unchanged, so the caller can detect that by comparing the two.
    """

    payload = {
        "model": FALLBACK_TRANSLATE_MODEL,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Translate the following into natural, "
                    "conversational English. If it is already "
                    "English, repeat it back unchanged. Reply "
                    "with only the translation and nothing "
                    f"else.\n\nText: {text}"
                ),
            }
        ],
        "max_tokens": 60,
        "temperature": 0,
    }

    result = _call_openai_json("chat/completions", payload, api_key)
    return result["choices"][0]["message"]["content"].strip()


def synthesize_speech(text: str, api_key: str) -> bytes:
    """
    Converts text to speech via OpenAI's TTS endpoint, requesting raw
    PCM output. This assumes that format matches the same 24kHz,
    16-bit, mono layout used everywhere else in this file (the same
    format the realtime API itself uses) -- if playback ever sounds
    pitched or sped up specifically for fallback phrases, that
    assumption is the first thing to check.
    """

    payload = {
        "model": FALLBACK_TTS_MODEL,
        "voice": FALLBACK_TTS_VOICE,
        "input": text,
        "response_format": "pcm",
    }

    return _call_openai_binary("audio/speech", payload, api_key)


def transcribe_held_audio(pcm_audio: bytes, api_key: str) -> str:
    """
    One-shot transcription of raw PCM mic audio, used only by the
    audio arm of the fallback (single words that never produce a
    streaming transcript). The endpoint wants multipart/form-data,
    which urllib doesn't build for us, so the body is assembled by
    hand.
    """

    # Very short clips ("bien" came back as 'bn') transcribe far
    # better with a little silence padding around them. Deliberately
    # NO prompt: a hint list of example words ("Buenos días...") got
    # hallucinated back verbatim on near-silent audio and spoken to
    # the user as a phrase they never said.
    padding = b"\x00" * int(
        SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * 0.35
    )

    wav_file = io.BytesIO()

    with wave.open(wav_file, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(BYTES_PER_SAMPLE)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(padding + pcm_audio + padding)

    boundary = uuid.uuid4().hex

    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="model"\r\n\r\n',
            FALLBACK_TRANSCRIBE_MODEL.encode(),
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; '
            b'filename="phrase.wav"\r\n'
            b"Content-Type: audio/wav\r\n\r\n",
            wav_file.getvalue(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )

    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": (
                f"multipart/form-data; boundary={boundary}"
            ),
        },
        method="POST",
    )

    with urllib.request.urlopen(
        request, timeout=FALLBACK_REQUEST_TIMEOUT_SECONDS
    ) as response:
        result = json.loads(response.read().decode("utf-8"))

    return result.get("text", "").strip()


def restart_service() -> None:
    """
    Triggered by a long button press. Asks systemd to cleanly restart
    the whole translator, useful if Bluetooth ever gets stuck, without
    needing a keyboard or screen.
    """

    log("Button held: restarting translator service.")

    subprocess.run(
        ["systemctl", "--user", "restart", "translator.service"],
        check=False,
    )


# ============================================================
# TRANSLATED-AUDIO BUFFER
# ============================================================

class AudioBuffer:
    """Stores translated audio without allowing a huge delay."""

    def __init__(self) -> None:
        self._chunks: deque[bytes] = deque()
        self._total_bytes = 0
        self._closed = False
        self._condition = threading.Condition()

        bytes_per_second = (
            SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
        )

        self._maximum_bytes = int(
            bytes_per_second * MAX_BACKLOG_SECONDS
        )

        self._catch_up_bytes = int(
            bytes_per_second * CATCH_UP_TO_SECONDS
        )

    def put(self, audio: bytes) -> None:
        if not audio:
            return

        dropped_bytes = 0

        with self._condition:
            if self._closed:
                return

            self._chunks.append(audio)
            self._total_bytes += len(audio)

            # Once over the cap, drain all the way down to the
            # catch-up target in one skip. The old loop stopped as
            # soon as it was back under the cap, so the buffer sat
            # pinned AT the cap shaving one tiny chunk per put()
            # forever -- heard as constant cutting in and out, with
            # the whole cap's worth of hidden latency on top.
            if self._total_bytes > self._maximum_bytes:
                while (
                    self._chunks
                    and self._total_bytes > self._catch_up_bytes
                ):
                    removed = self._chunks.popleft()
                    self._total_bytes -= len(removed)
                    dropped_bytes += len(removed)

            self._condition.notify()

        if dropped_bytes:
            dropped_seconds = dropped_bytes / (
                SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
            )

            log(
                f"Playback stalled: skipped {dropped_seconds:.2f}s "
                "of delayed audio to catch up (backlog exceeded "
                f"{MAX_BACKLOG_SECONDS}s)."
            )

    def get(self, timeout: float = 0.5) -> bytes | None:
        deadline = time.monotonic() + timeout

        with self._condition:
            while not self._chunks:
                if self._closed:
                    return None

                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    raise queue.Empty

                self._condition.wait(remaining)

            audio = self._chunks.popleft()
            self._total_bytes -= len(audio)
            return audio

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def backlog_seconds(self) -> float:
        with self._condition:
            return self._total_bytes / (
                SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
            )


# ============================================================
# MICROPHONE AND AIRPODS SETUP
# ============================================================

def find_respeaker() -> str:
    output = subprocess.check_output(
        ["arecord", "-l"],
        text=True,
        stderr=subprocess.STDOUT,
    )

    for line in output.splitlines():
        if "ReSpeaker Lite" not in line:
            continue

        match = re.search(
            r"card\s+(\d+):.*device\s+(\d+):",
            line,
            flags=re.IGNORECASE,
        )

        if match:
            return f"plughw:{match.group(1)},{match.group(2)}"

    raise RuntimeError(
        "ReSpeaker Lite was not found. Check its USB connection."
    )


def connect_airpods() -> int:
    subprocess.run(
        ["bluetoothctl", "power", "on"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )

    subprocess.run(
        ["bluetoothctl", "trust", AIRPODS_MAC],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )

    subprocess.run(
        ["bluetoothctl", "connect", AIRPODS_MAC],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )

    deadline = time.monotonic() + 10

    while time.monotonic() < deadline:
        status = subprocess.check_output(
            ["wpctl", "status"],
            text=True,
            stderr=subprocess.DEVNULL,
        )

        in_sinks = False

        for line in status.splitlines():
            if "Sinks:" in line:
                in_sinks = True
                continue

            if in_sinks and "Sources:" in line:
                break

            if in_sinks and AIRPODS_NAME in line:
                match = re.search(
                    r"(\d+)\.\s+.*AirPods",
                    line,
                    flags=re.IGNORECASE,
                )

                if match:
                    sink_id = int(match.group(1))

                    subprocess.run(
                        ["wpctl", "set-default", str(sink_id)],
                        check=False,
                    )

                    subprocess.run(
                        [
                            "wpctl",
                            "set-volume",
                            str(sink_id),
                            "0.60",
                        ],
                        check=False,
                    )

                    return sink_id

        time.sleep(0.5)

    raise RuntimeError(
        "The AirPods are paired, but their speaker output "
        "did not appear."
    )


def start_airpods_player() -> subprocess.Popen:
    command = [
        "pw-cat",
        "--playback",
        "--raw",
        "--rate",
        str(PLAYBACK_RATE),
        "--format",
        "s16",
        "--channels",
        str(CHANNELS),
        "--latency",
        f"{PLAYBACK_LATENCY_MS}ms",
        "-",
    ]

    player = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    time.sleep(0.6)

    if player.poll() is not None:
        error_message = ""

        if player.stderr is not None:
            error_message = player.stderr.read().decode(
                errors="replace"
            )

        raise RuntimeError(
            "Could not open the AirPods audio stream:\n"
            + error_message
        )

    if player.stdin is None:
        player.terminate()
        raise RuntimeError("The AirPods audio pipe did not open.")

    wakeup_samples = int(PLAYBACK_RATE * 0.05)
    player.stdin.write(b"\x00\x00" * wakeup_samples)

    return player


def stop_player(player: subprocess.Popen | None) -> None:
    if player is None:
        return

    try:
        if player.stdin:
            player.stdin.close()
    except Exception:
        pass

    try:
        player.wait(timeout=2)
    except subprocess.TimeoutExpired:
        player.terminate()


def start_microphone(device: str) -> subprocess.Popen:
    microphone = subprocess.Popen(
        [
            "arecord",
            "-q",
            "-D",
            device,
            "-t",
            "raw",
            "-f",
            "S16_LE",
            "-r",
            str(SAMPLE_RATE),
            "-c",
            str(CHANNELS),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    if microphone.stdout is None:
        microphone.terminate()
        raise RuntimeError("The microphone stream did not open.")

    return microphone


# ============================================================
# AUDIO THREADS
# ============================================================

# NEW: how often to actively check that the AirPods are still
# connected at the Bluetooth level. The existing reconnect logic
# below only reacts when *writing* audio fails -- but a Bluetooth
# drop doesn't always show up that way immediately (PipeWire can
# keep accepting writes into its own buffer for a while even if
# the underlying device is gone). This watchdog catches that gap.
BLUETOOTH_WATCHDOG_INTERVAL_SECONDS = 5


def is_airpods_connected() -> bool:
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", AIRPODS_MAC],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return "Connected: yes" in result.stdout
    except Exception:
        return False


def bluetooth_watchdog_worker(stop_event: threading.Event) -> None:
    """
    Independently checks the actual Bluetooth connection state on a
    timer, and reconnects if it's dropped -- even if nothing has
    tried to write audio recently, or a write hasn't yet failed.

    Escalates after repeated failures instead of retrying the same
    thing forever (one logged stretch showed 43 identical failures
    over 11 minutes): every 3rd consecutive failure restarts the
    bluetooth service, every 6th also resets the dongle's USB port.
    """

    consecutive_failures = 0

    while not stop_event.is_set():
        if stop_event.wait(BLUETOOTH_WATCHDOG_INTERVAL_SECONDS):
            break

        if is_airpods_connected():
            consecutive_failures = 0
            continue

        log("Watchdog: AirPods appear disconnected. Reconnecting...")

        try:
            connect_airpods()
            log("Watchdog: AirPods reconnected.")
            consecutive_failures = 0
        except Exception as error:
            consecutive_failures += 1
            log(f"Watchdog: reconnect attempt failed: {error}")

            if consecutive_failures % 3 == 0:
                if consecutive_failures % 6 == 0:
                    reset_bluetooth_dongle_usb()

                log(
                    "Watchdog: escalating -- restarting the "
                    "bluetooth service."
                )
                subprocess.run(
                    ["sudo", "systemctl", "restart", "bluetooth"],
                    check=False,
                )


def fallback_watchdog_worker(
    event_handler: TranslationEventHandler,
    audio_buffer: AudioBuffer,
    api_key: str,
    stop_event: threading.Event,
) -> None:
    """
    Checks every FALLBACK_CHECK_INTERVAL_SECONDS for a short phrase
    the realtime model never responded to. Runs on its own thread
    specifically so the network calls it makes never delay the main
    event loop.
    """

    while not stop_event.is_set():
        if stop_event.wait(FALLBACK_CHECK_INTERVAL_SECONDS):
            break

        event_handler.maybe_finish_phrase_after_silence(audio_buffer)
        event_handler.maybe_run_fallback_translation(
            audio_buffer, api_key
        )


def microphone_worker(
    ws: websocket.WebSocket,
    microphone: subprocess.Popen,
    stop_event: threading.Event,
    send_lock: threading.Lock,
    event_handler: TranslationEventHandler,
    device: str,
) -> None:
    if microphone.stdout is None:
        stop_event.set()
        return

    # Counts streams that died within seconds of opening. A reopen
    # that immediately dies again must escalate rather than reset
    # the clock -- an earlier version counted each reopen attempt
    # separately, so a start-then-instantly-die loop churned
    # forever without ever reaching its USB-reset escalation.
    quick_deaths = 0
    opened_at = time.monotonic()

    while not stop_event.is_set():
        audio = microphone.stdout.read(MICROPHONE_CHUNK_BYTES)

        if not audio:
            if stop_event.is_set():
                return

            if time.monotonic() - opened_at < 5.0:
                quick_deaths += 1
            else:
                quick_deaths = 1

            log(
                "The microphone stream ended; reopening it "
                f"(rapid failure {quick_deaths})..."
            )
            microphone.poll()

            if quick_deaths >= MICROPHONE_REOPEN_ATTEMPTS:
                # Let systemd restart the whole translator; startup
                # has its own, stronger recovery escalation.
                log(
                    "The microphone keeps dying instantly; "
                    "stopping so the service restarts cleanly."
                )
                stop_event.set()
                return

            if quick_deaths == MICROPHONE_REOPEN_USB_RESET_AT:
                reset_microphone_usb()

            time.sleep(MICROPHONE_REOPEN_DELAY_SECONDS)

            try:
                if quick_deaths >= MICROPHONE_REOPEN_USB_RESET_AT:
                    # Re-enumeration can change the card number.
                    device = find_respeaker()

                microphone = start_microphone(device)
                opened_at = time.monotonic()
                log("Microphone reopened.")

            except Exception as error:
                # Keep looping: the dead handle's read returns
                # empty immediately, so the counter keeps climbing
                # toward escalation and the stop above. Timestamp
                # the attempt so those instant re-deaths still
                # count as rapid.
                opened_at = time.monotonic()
                log(f"Microphone reopen failed: {error}")

            continue

        # Fed even if the upload below fails -- the audio was still
        # heard, and the fallback may be the only path left for it.
        event_handler.note_mic_audio(audio)

        event = {
            "type": "session.input_audio_buffer.append",
            "audio": base64.b64encode(audio).decode("ascii"),
        }

        try:
            with send_lock:
                ws.send(json.dumps(event))

        except websocket.WebSocketTimeoutException:
            # A brief network stall, not a dead connection. Skip
            # this one chunk of audio (roughly 100ms) and keep
            # going, rather than tearing down the whole session
            # over a momentary hiccup.
            log(
                "Microphone upload stalled briefly; "
                "skipping one audio chunk."
            )
            continue

        except (BrokenPipeError, ConnectionResetError, OSError) as error:
            if not stop_event.is_set():
                log(f"Microphone upload stopped: {error}")

            stop_event.set()
            return

        except Exception as error:
            if not stop_event.is_set():
                log(f"Microphone upload stopped: {error}")

            stop_event.set()
            return


def _write_with_timeout(
    player: subprocess.Popen,
    audio: bytes,
    timeout: float = PLAYBACK_WRITE_TIMEOUT_SECONDS,
) -> None:
    if player.stdin is None:
        raise BrokenPipeError("Player has no stdin.")

    _, writable, _ = select.select(
        [], [player.stdin.fileno()], [], timeout
    )

    if not writable:
        raise BrokenPipeError(
            "Write to AirPods pipe stalled "
            f"(no progress within {timeout}s)."
        )

    player.stdin.write(audio)


def playback_worker(
    audio_buffer: AudioBuffer,
    player: subprocess.Popen,
    stop_event: threading.Event,
) -> None:
    current_player = player

    while not stop_event.is_set():
        try:
            audio = audio_buffer.get(timeout=0.5)

        except queue.Empty:
            continue

        if audio is None:
            break

        try:
            _write_with_timeout(current_player, audio)

        except (BrokenPipeError, OSError) as error:
            log(f"AirPods stream dropped ({error}). Reconnecting...")

            stop_player(current_player)

            try:
                def _reconnect():
                    if not WIRED_OUTPUT:
                        connect_airpods()
                    return start_airpods_player()

                current_player = retry(
                    "AirPods reconnect",
                    RECONNECT_RETRY_ATTEMPTS,
                    RECONNECT_RETRY_DELAY_SECONDS,
                    _reconnect,
                )

                _write_with_timeout(current_player, audio)
                log("AirPods reconnected.")

            except Exception as error:
                log(f"AirPods reconnection failed: {error}")

    stop_player(current_player)


# ============================================================
# OPENAI SESSION SETUP
# ============================================================

def configure_session(
    ws: websocket.WebSocket,
    send_lock: threading.Lock,
) -> None:
    """
    Set the target language and turn on source-language
    transcription. Source transcripts are used only to detect
    and mute same-language echo -- they are not shown on screen.
    """

    event = {
        "type": "session.update",
        "session": {
            "audio": {
                "input": {
                    "transcription": {
                        "model": "gpt-realtime-whisper",
                    },
                },
                "output": {
                    "language": TARGET_LANGUAGE,
                },
            }
        },
    }

    with send_lock:
        ws.send(json.dumps(event))

    while True:
        raw_message = ws.recv()

        if not raw_message:
            raise RuntimeError(
                "OpenAI disconnected during setup."
            )

        response = json.loads(raw_message)

        if response.get("type") == "error":
            raise RuntimeError(
                response.get("error", {}).get(
                    "message",
                    "OpenAI setup failed.",
                )
            )

        if response.get("type") != "session.updated":
            continue

        session = response.get("session", {})
        audio = session.get("audio", {})
        language = audio.get("output", {}).get("language")
        transcription = audio.get("input", {}).get(
            "transcription"
        )

        log(
            "Session configured. output.language="
            f"{language!r} input.transcription={transcription!r}"
        )

        if language == TARGET_LANGUAGE:
            log("Target language confirmed: English")
            return


# ============================================================
# EVENT HANDLING (STATEFUL, PER-PHRASE)
# ============================================================

def prefix_similarity(text_a: str, text_b: str) -> float:
    """
    Similarity of the two texts truncated to equal length. The raw
    SequenceMatcher ratio penalizes a length difference directly,
    and the two transcripts stream at very different speeds -- so
    comparing full texts made a perfect echo look "different"
    whenever one transcript was ahead of the other.
    """

    length = min(len(text_a), len(text_b))

    return difflib.SequenceMatcher(
        None, text_a[:length], text_b[:length]
    ).ratio()


def chunk_rms(chunk: bytes) -> float:
    """
    Root-mean-square level of a chunk of 16-bit mono PCM. Done by
    hand because audioop was removed in Python 3.13; a 100ms chunk
    is only 2400 samples, which is cheap even on this Pi.
    """

    samples = array.array("h")
    samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])

    if not samples:
        return 0.0

    total = 0

    for sample in samples:
        total += sample * sample

    return math.sqrt(total / len(samples))


def shorten_for_log(text: str, limit: int = 120) -> str:
    text = text.strip()

    if len(text) <= limit:
        return text

    return text[:limit] + "..."


class TranslationEventHandler:
    """
    Tracks the current phrase's source and translated transcript
    text so we can detect same-language echo (e.g. English spoken
    when the target is already English) and mute just that phrase's
    audio.

    Each phrase's audio is held in memory rather than played
    immediately, so the mute decision can't "lose the race" against
    audio that already streamed out. If a decision can't be made
    before the phrase ends (source transcript never arrived, or
    arrived too late), we fail open and play the audio -- a real
    translation should never be silently dropped just because the
    source-language transcript was slow.
    """

    def __init__(self, led: RGBLED | None = None) -> None:
        self._led = led
        self._input_transcript = ""
        self._output_transcript = ""
        self._decision_made = False
        self._suppress_current_phrase = False
        self._pending_audio_chunks: list[bytes] = []
        self._pending_bytes = 0
        self._last_output_activity: float | None = None

        # Every distinct event type is logged once per session, so
        # the log reveals what this API actually sends -- needed
        # because at least one expected event name has proven wrong.
        self._seen_event_types: set[str] = set()

        # Deliberately separate from the fields above. The existing
        # same-language logic resets on output_transcript.done -- but
        # the whole point of this fallback is to catch phrases where
        # that event may never fire at all. Keeping its own copy of
        # the source text and its own reset triggers means a bug here
        # can't corrupt the comparison the working filter depends on.
        self._fallback_lock = threading.Lock()
        self._fallback_input_text = ""
        self._fallback_last_activity_time: float | None = None
        self._fallback_output_arrived = False
        self._fallback_last_output_time: float | None = None
        self._fallback_in_progress = False
        self._loud_output_bytes_since_speech = 0

        # Audio arm of the fallback: a rolling buffer of raw mic
        # audio plus speech-energy timestamps, fed by the microphone
        # worker via note_mic_audio(). Shares _fallback_lock.
        self._mic_chunks: deque[bytes] = deque()
        self._mic_bytes = 0
        self._speech_active_since: float | None = None
        self._speech_last_loud_time: float | None = None

        # Post-decision output audio waits here for
        # ROLLING_HOLD_SECONDS; each chunk's play/drop fate is
        # decided at release time, not arrival time, so the (always
        # lagging) echo detector can act on the audio it was
        # actually judging. Entries are (arrival_time, chunk).
        self._release_queue: deque[tuple[float, bytes]] = deque()

    def _set_led(self, color: tuple[int, int, int]) -> None:
        if self._led is None:
            return

        try:
            self._led.color = color
        except Exception:
            pass

    def _blink_declined(self) -> None:
        """Two quick red blinks: heard you, not going to answer."""

        for _ in range(2):
            self._set_led(LED_DECLINED_COLOR)
            time.sleep(0.18)
            self._set_led(LED_LISTENING_COLOR)
            time.sleep(0.12)

    def _reset_fallback_state_locked(self) -> None:
        """Caller must hold _fallback_lock."""

        self._fallback_input_text = ""
        self._fallback_last_activity_time = None
        self._fallback_output_arrived = False
        self._mic_chunks.clear()
        self._mic_bytes = 0
        self._speech_active_since = None
        self._speech_last_loud_time = None
        self._loud_output_bytes_since_speech = 0
        self._set_led(LED_LISTENING_COLOR)

    def note_mic_audio(self, chunk: bytes) -> None:
        """
        Called by the microphone worker for every chunk it uploads.
        Keeps a short rolling buffer of raw audio and notes when
        speech energy was last heard, so the fallback can transcribe
        the audio itself when no streaming transcript ever arrives
        (the isolated-single-word case).
        """

        rms = chunk_rms(chunk)
        loud = rms >= SPEECH_RMS_THRESHOLD
        now = time.monotonic()

        bytes_per_second = (
            SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
        )
        max_bytes = int(
            bytes_per_second * MIC_ROLLING_BUFFER_SECONDS
        )
        preroll_bytes = int(
            bytes_per_second * MIC_PREROLL_SECONDS
        )

        with self._fallback_lock:
            self._mic_chunks.append(chunk)
            self._mic_bytes += len(chunk)

            while self._mic_chunks and self._mic_bytes > max_bytes:
                removed = self._mic_chunks.popleft()
                self._mic_bytes -= len(removed)

            if loud:
                if self._speech_active_since is None:
                    # A fresh utterance is starting: trim the buffer
                    # to a short preroll so older audio (already
                    # handled, or just room noise) can't get
                    # transcribed along with it.
                    while (
                        len(self._mic_chunks) > 1
                        and self._mic_bytes
                        > preroll_bytes + len(chunk)
                    ):
                        removed = self._mic_chunks.popleft()
                        self._mic_bytes -= len(removed)

                    self._speech_active_since = now
                    self._loud_output_bytes_since_speech = 0
                    log(
                        "Fallback: speech energy heard "
                        f"(RMS {rms:.0f}); holding mic audio."
                    )

                self._speech_last_loud_time = now

    def _reset_for_next_phrase(self) -> None:
        self._input_transcript = ""
        self._output_transcript = ""
        self._decision_made = False
        self._suppress_current_phrase = False
        self._pending_audio_chunks = []
        self._pending_bytes = 0
        self._release_queue.clear()

    def _maybe_decide_same_language(self) -> None:
        if self._decision_made:
            return

        input_text = self._input_transcript.strip().lower()
        output_text = self._output_transcript.strip().lower()

        # Fast path: with just a handful of characters from each
        # side, text from two different languages almost always
        # looks obviously different already. If so, release the
        # audio immediately rather than waiting for more text --
        # this is the common case (a real translation).
        if (
            len(output_text) >= EARLY_RELEASE_MIN_CHARACTERS
            and len(input_text) >= EARLY_RELEASE_MIN_CHARACTERS
        ):
            quick_similarity = prefix_similarity(
                input_text, output_text
            )

            if quick_similarity < EARLY_RELEASE_MAX_SIMILARITY:
                self._decision_made = True
                self._suppress_current_phrase = False
                return

        # Cautious path: only used to decide whether to mute this
        # phrase as an English echo. Requires more text before
        # committing, since this is the decision where being wrong
        # actually costs something (dropping a real translation).
        if (
            len(output_text) < MIN_CHARACTERS_BEFORE_COMPARING
            or len(input_text) < MIN_CHARACTERS_BEFORE_COMPARING
        ):
            return

        similarity = prefix_similarity(input_text, output_text)

        self._decision_made = True

        if similarity >= SAME_LANGUAGE_SIMILARITY_THRESHOLD:
            self._suppress_current_phrase = True
            log(
                "Same-language speech detected "
                f"(similarity {similarity:.2f}); muting playback "
                "for this phrase."
            )

    def _rolling_echo_score(self) -> float:
        """
        How closely the newest output text tracks the newest input
        text: the fraction of the input's tail that appears (in
        order) in the output's tail window. Near 1.0 for an echo,
        low for a genuine translation. -1.0 when there isn't enough
        text yet to say anything.
        """

        tail = (
            self._input_transcript.strip().lower()
            [-ROLLING_ECHO_TAIL_CHARS:]
        )
        window = (
            self._output_transcript.strip().lower()
            [-ROLLING_ECHO_WINDOW_CHARS:]
        )

        if (
            len(tail) < ROLLING_ECHO_MIN_TAIL_CHARS
            or len(window) < ROLLING_ECHO_MIN_TAIL_CHARS
        ):
            return -1.0

        matcher = difflib.SequenceMatcher(None, tail, window)

        matched = sum(
            block.size for block in matcher.get_matching_blocks()
        )

        return matched / len(tail)

    def _update_rolling_echo(self) -> None:
        """
        Runs after the initial phrase decision, on every transcript
        delta. Flips the mute both ways mid-phrase: English starting
        mid-stream gets cut off once there's enough text to see the
        echo, and a mute lifts again the moment real translation
        resumes -- so a wrong mute can never eat more than the
        stretch that actually looked like an echo.
        """

        if not self._decision_made:
            return

        score = self._rolling_echo_score()

        if score < 0:
            return

        if (
            not self._suppress_current_phrase
            and score >= ROLLING_ECHO_MUTE_SCORE
        ):
            self._suppress_current_phrase = True
            log(
                f"Echo mid-phrase (score {score:.2f}); muting "
                "until the output diverges from the input again."
            )
        elif (
            self._suppress_current_phrase
            and score <= ROLLING_ECHO_UNMUTE_SCORE
        ):
            self._suppress_current_phrase = False
            log(
                f"Output diverged from input (score {score:.2f}); "
                "unmuting."
            )

    def _final_chance_decision(self) -> None:
        """
        Called only when a phrase is ending with no decision ever
        made -- usually because it was too short to reach the normal
        comparison threshold. Rather than always defaulting to "play
        it", this takes one last look with whatever text exists,
        using a stricter similarity requirement since there's less
        text to be confident in.
        """

        input_text = self._input_transcript.strip().lower()
        output_text = self._output_transcript.strip().lower()

        if (
            len(input_text) < FINAL_CHANCE_MIN_CHARACTERS
            or len(output_text) < FINAL_CHANCE_MIN_CHARACTERS
        ):
            return

        similarity = prefix_similarity(input_text, output_text)

        if similarity >= FINAL_CHANCE_SIMILARITY_THRESHOLD:
            self._suppress_current_phrase = True
            log(
                "Same-language speech detected on final check "
                f"(similarity {similarity:.2f}); muting playback "
                "for this short phrase."
            )

    def maybe_run_fallback_translation(
        self,
        audio_buffer: AudioBuffer,
        api_key: str,
    ) -> None:
        """
        Called periodically from a dedicated watchdog thread, never
        from the main event loop, so a slow network call here can
        never delay processing of the next real event. Fires only
        when: some source text arrived, nothing has gone silent for
        a while, no output ever arrived from the realtime model
        (meaning it silently declined to respond -- exactly the
        single-word limitation this is meant to catch), and we're
        not already mid-attempt for this same phrase.
        """

        if not FALLBACK_ENABLED:
            return

        now = time.monotonic()

        with self._fallback_lock:
            if self._fallback_in_progress:
                return

            text_arm_ready = (
                self._fallback_last_activity_time is not None
                and now - self._fallback_last_activity_time
                >= FALLBACK_SILENCE_TIMEOUT_SECONDS
            )

            audio_arm_ready = (
                self._speech_last_loud_time is not None
                and now - self._speech_last_loud_time
                >= FALLBACK_SILENCE_TIMEOUT_SECONDS
            )

            if not text_arm_ready and not audio_arm_ready:
                return

            if self._fallback_output_arrived:
                # Output arriving is NOT the same as the model
                # answering: it emits a little audio for nearly
                # every utterance, including words it declines to
                # translate (which suppressed the fallback for
                # literally every held word in one logged session).
                # Only a substantial amount of non-silent output
                # since this utterance began counts as a real
                # answer. Speech spoken after the model's last
                # output is likewise still unanswered.
                answered = (
                    self._loud_output_bytes_since_speech
                    >= FALLBACK_MEANINGFUL_OUTPUT_BYTES
                    and not (
                        self._fallback_last_output_time is not None
                        and self._speech_active_since is not None
                        and self._speech_active_since
                        > self._fallback_last_output_time
                    )
                )

                if self._speech_active_since is None or answered:
                    if self._speech_active_since is not None:
                        log(
                            "Fallback: model answered this speech "
                            "with real audio; clearing held mic "
                            "audio."
                        )

                    self._reset_fallback_state_locked()
                    return

                # Negligible output: clear the flag and fall
                # through, so the audio arm can still fire in this
                # same pass -- returning here let a model that
                # dribbles quiet audio re-set the flag before every
                # check and starve the fallback forever.
                self._fallback_input_text = ""
                self._fallback_last_activity_time = None
                self._fallback_output_arrived = False
                log(
                    "Fallback: model output since this speech "
                    "was negligible; keeping held mic audio."
                )

            text = self._fallback_input_text.strip()
            held_audio = b""

            if len(text) >= FALLBACK_MIN_INPUT_CHARACTERS:
                if not text_arm_ready:
                    # Transcript text is still trickling in; let
                    # its own silence timer make the call.
                    return
            elif audio_arm_ready:
                utterance_seconds = (
                    (self._speech_last_loud_time or 0.0)
                    - (self._speech_active_since or 0.0)
                )

                if utterance_seconds > FALLBACK_MAX_UTTERANCE_SECONDS:
                    # Long continuous sound, not an isolated word.
                    self._reset_fallback_state_locked()
                    return

                # No usable transcript, but a short burst of speech
                # was heard and has gone quiet: the isolated-single-
                # word case. Transcribe the held audio itself.
                held_audio = b"".join(self._mic_chunks)

                if len(held_audio) < FALLBACK_MIN_AUDIO_BYTES:
                    self._reset_fallback_state_locked()
                    return
            else:
                # A scrap of transcript too short to use, and no
                # speech energy pending: nothing worth chasing.
                self._reset_fallback_state_locked()
                return

            self._fallback_in_progress = True

        self._set_led(LED_WORKING_COLOR)

        try:
            if held_audio:
                held_seconds = len(held_audio) / (
                    SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
                )
                text = transcribe_held_audio(held_audio, api_key)
                log(
                    f"Audio fallback: {held_seconds:.1f}s of held "
                    f"mic audio transcribed as '{text}'"
                )

                # Silent decline: the automatic path never blinks
                # red -- that signal is reserved for words the user
                # deliberately captured with the button, where "not
                # answering" is information they're waiting on.
                if len(text) < FALLBACK_MIN_INPUT_CHARACTERS:
                    return

            translated = translate_short_phrase(text, api_key)

            similarity = difflib.SequenceMatcher(
                None,
                text.strip().lower(),
                translated.strip().lower(),
            ).ratio()

            if similarity >= SAME_LANGUAGE_SIMILARITY_THRESHOLD:
                log(
                    f"Short phrase '{text}' looked like English "
                    "already (fallback path); not repeating it."
                )
            else:
                log(
                    f"Short-phrase fallback: '{text}' -> "
                    f"'{translated}'"
                )
                audio = synthesize_speech(translated, api_key)
                audio_buffer.put(audio)
                print(translated, flush=True)

        except Exception as error:
            log(f"Short-phrase fallback failed: {error}")

        finally:
            with self._fallback_lock:
                self._reset_fallback_state_locked()
                self._fallback_in_progress = False

    def _drain_release_queue(
        self,
        audio_buffer: AudioBuffer,
        force: bool = False,
    ) -> None:
        """
        Plays (or drops) every held chunk whose hold time is up,
        using the suppress state as it stands NOW -- which, thanks
        to the hold, is a judgement made with the transcripts that
        correspond to this audio. force=True releases everything
        immediately (end of phrase).
        """

        now = time.monotonic()

        while self._release_queue:
            arrival, chunk = self._release_queue[0]

            if (
                not force
                and now - arrival < ROLLING_HOLD_SECONDS
            ):
                break

            self._release_queue.popleft()

            if not self._suppress_current_phrase:
                audio_buffer.put(chunk)

    def force_capture_translation(
        self,
        audio_buffer: AudioBuffer,
        api_key: str,
    ) -> None:
        """
        Button short press: push-to-translate. Clears the mic
        buffer, captures only what is said next (until trailing
        silence or the max window), then transcribes, translates,
        and speaks it directly -- no automatic heuristics involved,
        so it works even with a TV talking in the background.
        Runs on its own thread from the button callback.
        """

        with self._fallback_lock:
            if self._fallback_in_progress:
                return

            self._fallback_in_progress = True
            self._mic_chunks.clear()
            self._mic_bytes = 0
            self._speech_active_since = None
            self._speech_last_loud_time = None

        log("Button pressed: capturing speech to translate.")
        self._set_led(LED_CAPTURE_COLOR)

        try:
            deadline = (
                time.monotonic() + BUTTON_CAPTURE_MAX_SECONDS
            )

            while time.monotonic() < deadline:
                time.sleep(0.2)

                with self._fallback_lock:
                    heard = self._speech_active_since is not None
                    last_loud = self._speech_last_loud_time

                if (
                    heard
                    and last_loud is not None
                    and time.monotonic() - last_loud
                    >= BUTTON_CAPTURE_SILENCE_SECONDS
                ):
                    break

            with self._fallback_lock:
                held_audio = b"".join(self._mic_chunks)

            if len(held_audio) < FALLBACK_MIN_AUDIO_BYTES:
                log("Button capture heard nothing.")
                self._blink_declined()
                return

            self._set_led(LED_WORKING_COLOR)
            text = transcribe_held_audio(held_audio, api_key)
            log(f"Button capture transcribed as '{text}'")

            if len(text) < FALLBACK_MIN_INPUT_CHARACTERS:
                self._blink_declined()
                return

            translated = translate_short_phrase(text, api_key)

            similarity = difflib.SequenceMatcher(
                None,
                text.strip().lower(),
                translated.strip().lower(),
            ).ratio()

            if similarity >= SAME_LANGUAGE_SIMILARITY_THRESHOLD:
                log(
                    f"Button capture '{text}' was already "
                    "English; not repeating it."
                )
                self._blink_declined()
                return

            log(f"Button translation: '{text}' -> '{translated}'")
            audio = synthesize_speech(translated, api_key)
            audio_buffer.put(audio)
            print(translated, flush=True)

        except Exception as error:
            log(f"Button capture failed: {error}")

        finally:
            with self._fallback_lock:
                self._reset_fallback_state_locked()
                self._fallback_in_progress = False

    def _flush_pending_audio(self, audio_buffer: AudioBuffer) -> None:
        # Released via the rolling hold queue with a backdated
        # arrival: these chunks already waited out the phrase-start
        # hold, so they play on the next drain rather than waiting
        # ROLLING_HOLD_SECONDS again.
        release_at = time.monotonic() - ROLLING_HOLD_SECONDS

        if not self._suppress_current_phrase:
            for chunk in self._pending_audio_chunks:
                self._release_queue.append((release_at, chunk))

        self._pending_audio_chunks = []
        self._pending_bytes = 0
        self._drain_release_queue(audio_buffer)

    def _finish_phrase(
        self,
        audio_buffer: AudioBuffer,
        reason: str,
    ) -> None:
        if SAME_LANGUAGE_FILTER_ENABLED and not self._decision_made:
            self._final_chance_decision()

            if not self._suppress_current_phrase:
                log(
                    "No confident same-language match for this "
                    "short phrase; playing it."
                )

            self._flush_pending_audio(audio_buffer)

        # Release whatever the rolling hold still has, under the
        # phrase's final verdict, before state resets.
        self._drain_release_queue(audio_buffer, force=True)

        # Transcript text otherwise only reaches the volatile
        # journal, so nothing survives a reboot to debug filter
        # decisions against.
        log(
            f"Phrase done ({reason}). "
            f"source={shorten_for_log(self._input_transcript)!r} "
            f"translation="
            f"{shorten_for_log(self._output_transcript)!r} "
            f"muted={self._suppress_current_phrase}"
        )

        self._reset_for_next_phrase()
        self._last_output_activity = None

    def maybe_finish_phrase_after_silence(
        self,
        audio_buffer: AudioBuffer,
    ) -> None:
        """
        Safety net for phrase endings, called from the watchdog
        thread. The done event this code listens for has never been
        observed, so sustained silence from the model's output is
        treated as the end of the phrase. Runs on a different thread
        than handle(), but individual mutations are GIL-atomic and
        the 1.5s silence threshold means real overlap with a
        still-streaming phrase is effectively impossible; worst case
        a chunk lands in the next phrase's pending audio and plays
        slightly late.
        """

        # Timer-driven drain: when output deltas stop arriving,
        # nothing else would release the last held chunks.
        self._drain_release_queue(audio_buffer)

        if self._last_output_activity is None:
            return

        elapsed = time.monotonic() - self._last_output_activity

        if elapsed < PHRASE_SILENCE_RESET_SECONDS:
            return

        self._finish_phrase(audio_buffer, reason="silence")

    def handle(
        self,
        event: dict,
        audio_buffer: AudioBuffer,
    ) -> bool:
        """Returns True if the session has closed."""

        event_type = event.get("type", "")

        if event_type not in self._seen_event_types:
            self._seen_event_types.add(event_type)
            log(f"First event of this type this session: {event_type}")

        if event_type == "session.output_audio.delta":
            encoded_audio = event.get("delta", "")

            if not encoded_audio:
                return False

            self._last_output_activity = time.monotonic()

            with self._fallback_lock:
                self._fallback_output_arrived = True
                self._fallback_last_output_time = time.monotonic()

            chunk = base64.b64decode(encoded_audio)

            if chunk_rms(chunk) >= OUTPUT_LOUD_RMS_THRESHOLD:
                with self._fallback_lock:
                    self._loud_output_bytes_since_speech += len(
                        chunk
                    )

            if not SAME_LANGUAGE_FILTER_ENABLED:
                audio_buffer.put(chunk)
                return False

            if self._decision_made:
                self._release_queue.append(
                    (time.monotonic(), chunk)
                )
                self._drain_release_queue(audio_buffer)
            else:
                self._pending_audio_chunks.append(chunk)
                self._pending_bytes += len(chunk)

                if self._pending_bytes >= PENDING_HOLD_MAX_BYTES:
                    self._decision_made = True
                    self._suppress_current_phrase = False
                    log(
                        "No same-language decision within "
                        f"{PENDING_HOLD_MAX_SECONDS}s of held audio "
                        "(source transcript is late); playing this "
                        "phrase."
                    )
                    self._flush_pending_audio(audio_buffer)

        elif event_type == "session.input_transcript.delta":
            delta = event.get("delta", "")
            self._input_transcript += delta

            with self._fallback_lock:
                self._fallback_input_text += delta
                self._fallback_last_activity_time = time.monotonic()

            if SAME_LANGUAGE_FILTER_ENABLED:
                self._maybe_decide_same_language()

                if self._decision_made:
                    self._flush_pending_audio(audio_buffer)
                    self._update_rolling_echo()

        elif event_type == "session.output_transcript.delta":
            delta = event.get("delta", "")
            self._output_transcript += delta
            print(delta, end="", flush=True)
            self._last_output_activity = time.monotonic()

            with self._fallback_lock:
                self._fallback_output_arrived = True
                self._fallback_last_output_time = time.monotonic()

            if SAME_LANGUAGE_FILTER_ENABLED:
                self._maybe_decide_same_language()

                if self._decision_made:
                    self._flush_pending_audio(audio_buffer)
                    self._update_rolling_echo()

        elif event_type == "session.output_transcript.done":
            print(flush=True)
            self._finish_phrase(audio_buffer, reason="done event")

        elif event_type == "error":
            log(
                "OpenAI error: "
                + event.get("error", {}).get(
                    "message",
                    "Unknown error.",
                )
            )

        elif event_type == "session.closed":
            log("Translation session closed.")
            return True

        return False


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY was not found in .env."
        )

    log("=== Translator starting ===")

    led = RGBLED(
        red=LED_RED_PIN,
        green=LED_GREEN_PIN,
        blue=LED_BLUE_PIN,
        active_high=LED_ACTIVE_HIGH,
    )
    button = Button(
        BUTTON_PIN,
        pull_up=True,
        bounce_time=0.05,
        hold_time=BUTTON_RESTART_HOLD_SECONDS,
        hold_repeat=False,
    )
    button.when_held = restart_service

    wait_for_internet(led)

    if not WIRED_OUTPUT:
        wait_for_bluetooth_controller(led)

    try:
        print("Checking ReSpeaker...")

        try:
            microphone_device = retry(
                "ReSpeaker detection",
                STARTUP_RETRY_ATTEMPTS,
                STARTUP_RETRY_DELAY_SECONDS,
                find_respeaker,
            )
        except Exception:
            # A soft-wedged ReSpeaker sometimes revives from a hub
            # bind cycle -- and a freshly replugged one often does
            # not register on the bus until one happens. Escalate
            # once, then run a final round of attempts. Combined
            # with Restart=always, the recovery loop for a hard
            # wedge is now just: human replugs the cable, service
            # finds it within a cycle.
            reset_microphone_usb()
            microphone_device = retry(
                "ReSpeaker detection (after USB reset)",
                STARTUP_RETRY_ATTEMPTS,
                STARTUP_RETRY_DELAY_SECONDS,
                find_respeaker,
            )

        print(f"ReSpeaker found: {microphone_device}")

        if WIRED_OUTPUT:
            print("Wired mode: using default PipeWire sink.")
        else:
            print("Connecting AirPods...")
            led.color = (1, 1, 1)
            sink_id = retry(
                "AirPods connect",
                STARTUP_RETRY_ATTEMPTS,
                STARTUP_RETRY_DELAY_SECONDS,
                connect_airpods,
            )
            print(f"AirPods ready: PipeWire sink {sink_id}")

        print("Opening audio stream...")
        player = retry(
            "AirPods audio stream open",
            STARTUP_RETRY_ATTEMPTS,
            STARTUP_RETRY_DELAY_SECONDS,
            start_airpods_player,
        )
        print("AirPods audio stream ready.")

        if WIRED_OUTPUT:
            # Opening the stream activates the sink; give the
            # kernel a moment to log a failure if the ReSpeaker's
            # playback side came up wedged, then self-heal once.
            time.sleep(1.5)

            if recent_playback_interface_failure():
                log(
                    "Playback interface came up wedged; resetting "
                    "the USB hub and reopening audio..."
                )
                stop_player(player)
                reset_microphone_usb()
                microphone_device = retry(
                    "ReSpeaker detection",
                    STARTUP_RETRY_ATTEMPTS,
                    STARTUP_RETRY_DELAY_SECONDS,
                    find_respeaker,
                )
                player = retry(
                    "audio stream reopen",
                    STARTUP_RETRY_ATTEMPTS,
                    STARTUP_RETRY_DELAY_SECONDS,
                    start_airpods_player,
                )
    except Exception:
        led.color = (1, 0, 0)
        raise

    audio_buffer = AudioBuffer()
    stop_event = threading.Event()
    playback_stop = threading.Event()
    send_lock = threading.Lock()
    event_handler = TranslationEventHandler(led)

    # gpiozero 2.0 removed Button.was_held, so the hold is tracked
    # here: a long press restarts (and must not ALSO trigger a
    # capture when the button is finally released).
    button_was_held = {"value": False}

    def on_button_held() -> None:
        button_was_held["value"] = True
        restart_service()

    def on_button_release() -> None:
        if button_was_held["value"]:
            button_was_held["value"] = False
            return

        threading.Thread(
            target=event_handler.force_capture_translation,
            args=(audio_buffer, api_key),
            daemon=True,
        ).start()

    button.when_held = on_button_held
    button.when_released = on_button_release

    ws: websocket.WebSocket | None = None
    microphone: subprocess.Popen | None = None
    microphone_thread: threading.Thread | None = None
    output_thread: threading.Thread | None = None

    session_started = False
    session_closed = False

    try:
        print("Connecting to OpenAI...")

        ws = websocket.create_connection(
            OPENAI_URL,
            header=[
                f"Authorization: Bearer {api_key}",
                "OpenAI-Safety-Identifier: translator-pi-user",
            ],
            timeout=WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
            enable_multithread=True,
            sockopt=[
                (
                    socket.IPPROTO_TCP,
                    socket.TCP_NODELAY,
                    1,
                )
            ],
        )

        first_message = ws.recv()

        if not first_message:
            raise RuntimeError(
                "OpenAI closed the connection immediately."
            )

        first_event = json.loads(first_message)

        if first_event.get("type") == "error":
            raise RuntimeError(
                first_event.get("error", {}).get(
                    "message",
                    "OpenAI connection failed.",
                )
            )

        if first_event.get("type") != "session.created":
            raise RuntimeError(
                "Unexpected first event: "
                f"{first_event.get('type')}"
            )

        session_started = True
        print("Connected to the translation service.")

        configure_session(ws, send_lock)

        output_thread = threading.Thread(
            target=playback_worker,
            args=(
                audio_buffer,
                player,
                playback_stop,
            ),
            daemon=True,
        )
        output_thread.start()

        microphone = start_microphone(microphone_device)

        microphone_thread = threading.Thread(
            target=microphone_worker,
            args=(
                ws,
                microphone,
                stop_event,
                send_lock,
                event_handler,
                microphone_device,
            ),
            daemon=True,
        )
        microphone_thread.start()

        if not WIRED_OUTPUT:
            watchdog_thread = threading.Thread(
                target=bluetooth_watchdog_worker,
                args=(stop_event,),
                daemon=True,
            )
            watchdog_thread.start()

        # Always started, even with the fallback disabled: this
        # thread also finishes phrases by output silence, which the
        # same-language filter depends on (the API's phrase-end
        # event has never been observed to fire).
        fallback_thread = threading.Thread(
            target=fallback_watchdog_worker,
            args=(event_handler, audio_buffer, api_key, stop_event),
            daemon=True,
        )
        fallback_thread.start()

        ws.settimeout(WEBSOCKET_IDLE_TIMEOUT_SECONDS)

        led.color = (0, 1, 0)

        print()
        print("Listening now.")
        print("Speak or play a foreign language near the ReSpeaker.")
        print("Press Ctrl+C to stop.")
        print()
        print("English translation:")

        while not stop_event.is_set():
            try:
                raw_message = ws.recv()

            except websocket.WebSocketTimeoutException:
                continue

            if not raw_message:
                break

            event = json.loads(raw_message)

            if event_handler.handle(event, audio_buffer):
                session_closed = True
                break

    except KeyboardInterrupt:
        print(
            "\n\nStopping and finishing the current phrase..."
        )

    finally:
        stop_event.set()

        if microphone is not None:
            microphone.terminate()

            try:
                microphone.wait(timeout=2)
            except subprocess.TimeoutExpired:
                microphone.kill()

        if microphone_thread is not None:
            microphone_thread.join(timeout=2)

        if (
            ws is not None
            and session_started
            and not session_closed
        ):
            try:
                with send_lock:
                    ws.send(
                        json.dumps({"type": "session.close"})
                    )

                deadline = time.monotonic() + 8
                ws.settimeout(1)

                while time.monotonic() < deadline:
                    try:
                        raw_message = ws.recv()

                    except websocket.WebSocketTimeoutException:
                        continue

                    if not raw_message:
                        break

                    event = json.loads(raw_message)

                    if event_handler.handle(event, audio_buffer):
                        session_closed = True
                        break

            except Exception as error:
                log(f"Could not finish cleanly: {error}")

        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

        audio_buffer.close()

        deadline = time.monotonic() + 8

        while (
            audio_buffer.backlog_seconds() > 0.05
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)

        playback_stop.set()

        if output_thread is not None:
            output_thread.join(timeout=3)

        led.off()
        led.close()
        button.close()

        log("Translator stopped.")


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        log(f"Translator failed: {error}")
