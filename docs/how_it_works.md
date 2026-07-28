# How It Works

The technical side: how audio moves through the system, and why several parts are built the way they are rather than the obvious way.

---

## The basic pipeline

1. The ReSpeaker microphone captures audio, read in 100ms chunks
2. Chunks stream continuously to OpenAI's realtime translation API over a WebSocket
3. The model detects the source language, transcribes it, and returns English audio and text
4. Returned audio is buffered, filtered, then written to PipeWire
5. PipeWire plays it out through the Bluetooth adapter to the earbuds

See `hardware/system_architecture.png` for the diagram.

The interesting parts are not the pipeline itself — that is fairly standard. They are the things that had to be added to make it work reliably on constrained hardware, over a flaky radio, with a model that does not always behave predictably.

---

## Threading model

Five threads run concurrently:

| Thread | Job |
|--------|-----|
| Main | Reads WebSocket events, drives the filter state machine |
| Microphone | Reads mic audio, uploads it, maintains a rolling audio buffer |
| Playback | Writes translated audio to PipeWire |
| Bluetooth watchdog | Independently monitors and recovers the Bluetooth connection |
| Fallback watchdog | Handles short-utterance translation and phrase timeouts |

Network calls are deliberately kept off the main thread. An early version made the fallback's API calls inline, which stalled event processing and made the whole system lag.

---

## Latency control

### The audio buffer drops rather than queues

Translated audio arrives from the model at slightly faster than real time during continuous speech. Playing it at exactly 1.0× speed means the backlog only grows — in testing, latency crept to about 30 seconds during long monologues.

Two mechanisms prevent this:

**Playback runs at 1.03× speed.** Three percent faster is inaudible, but it drains roughly a second of backlog every thirty seconds. This is load-bearing, not a tweak.

**The buffer has a hard cap.** Past `MAX_BACKLOG_SECONDS`, it discards the *oldest* queued audio and drains all the way down to a low-water mark. Words are lost when this fires, so it is a last resort — sized large enough to be rare, small enough that latency stays usable.

An earlier version of that cap contained a subtle bug worth mentioning: it stopped dropping as soon as it was back under the cap, which left the buffer permanently pinned *at* the cap, shaving one small chunk per write forever. Audibly, this was constant cutting in and out — with the full cap's worth of hidden latency underneath. Draining to a low-water mark instead of the cap fixed it.

---

## Same-language filtering

The translation model sometimes echoes speech back unchanged instead of staying silent when the input is already English. Filtering that out turned out to be the hardest part of the project.

### Comparing transcripts

The system receives two text streams: the source-language transcript and the translated transcript. If they are nearly identical, the "translation" is an echo.

The obvious approach — comparing the two full strings with a similarity ratio — **does not work**, and the reason is not obvious. The standard ratio is `2 × matches / (len(A) + len(B))`, which penalizes length differences directly. The two transcripts stream at very different speeds, so at any given moment one is usually well ahead of the other. A perfect echo scored as "completely different" simply because a 3-character output was being compared against a 17-character input.

The fix is to compare **equal-length prefixes** of the two texts. This single change is what made the filter work at all; no amount of threshold tuning fixed it beforehand.

### Holding audio before playing it

A decision made after audio has already played is useless. So each phrase's audio is held in memory until either a decision is reached or a timeout expires.

That timeout is a genuine tradeoff. Too short and English leaks through before its transcript arrives; too long and everything feels sluggish. Three seconds was the tuned compromise — 2.5 leaked the opening words of English phrases, 4.0 pushed total latency to around ten seconds.

### Rolling mid-phrase detection

A per-phrase decision assumes phrases have boundaries. In practice, continuous talking runs an entire session together as one phrase — one log showed a single phrase lasting two minutes. A decision made in its first seconds then governed everything afterward, so English spoken mid-stream played straight through.

A second layer continuously compares the newest input text against the newest output text, muting when the output starts tracking the input verbatim and unmuting when they diverge again. The mute and unmute thresholds are deliberately different, giving it hysteresis so it does not flap back and forth.

### Delayed release

Because the transcripts the detector reads lag the audio by a couple of seconds, deciding a chunk's fate the moment it arrives made every late realization irreversible — late mutes had already leaked the echo, and late unmutes had already discarded the start of a genuine translation.

Post-decision audio therefore waits in a release queue for a couple of seconds, and each chunk's fate is decided when it is released rather than when it arrives. This means a decision made now applies to the audio it was actually about.

### Phrases end on silence, not on the "done" event

The API documents a phrase-completion event. In practice, across entire sessions of successful translation, that event **never fired once**. Because phrase state only reset on that event, the first phrase's decision stayed locked in for the whole session and the filter effectively ran once and never again.

Phrases are now also finished after sustained silence from the model's output stream. This was the single largest cause of English playing through.

---

## Single-word translation

The streaming model needs enough audio before it commits to translating. A lone word often produces nothing at all — not a limitation that can be configured away.

A separate fallback pipeline handles this, with two arms:

**Text arm.** If a source transcript arrived but the model never produced meaningful output, that text is sent through a one-shot chat completion for translation, then spoken with text-to-speech. Unlike the streaming model, a one-shot call handles single words fine, because it is not waiting to decide whether enough was said.

**Audio arm.** Isolated single words frequently produce *no* streaming transcript either, so the text arm never arms. The microphone worker keeps a rolling buffer of raw audio and tracks when speech energy was last heard; if speech happened but nothing came back, the held audio itself is sent for one-shot transcription and continues down the same path.

Two details that took iteration:

- **"Did output arrive" is a useless signal.** The model emits a little audio for nearly every utterance, including words it declines to translate. One logged session showed this suppressing the fallback for literally every held word. Only a substantial amount of *non-silent* output counts as a real response.
- **Do not prompt the transcription model.** An early version passed a hint list of example words to improve accuracy on short clips. On near-silent audio the model hallucinated those examples back verbatim, and the device spoke phrases the user never said. Padding the clip with a little silence improves accuracy without that risk.

---

## Push-to-translate

Automatic single-word detection depends on finding a clean silence gap around the word. In a real room with a TV on, that gap rarely exists.

A short button press solves it directly: the mic buffer clears, the next stretch of speech is captured until trailing silence, and it is transcribed and translated straight away — bypassing every automatic heuristic.

---

## Recovery

The device is meant to run headless with no screen, so it has to recover from failures on its own.

### Bluetooth

Two independent mechanisms:

- The playback thread detects write failures **and stalls** — a stalled pipe does not always raise an error, and PipeWire will keep accepting writes into its own buffer even when the underlying device is gone
- A watchdog thread separately polls the actual connection state on a timer, catching drops that no write has failed on yet

Recovery escalates rather than retrying the same thing forever (one logged stretch showed 43 identical failed attempts over eleven minutes). Every third consecutive failure restarts the Bluetooth service; every sixth also unbinds and rebinds the adapter's USB port, which is the only software-level reset that revives an RTL8761B the kernel's own resets could not.

### Microphone

The ReSpeaker occasionally wedges on stream reopen. The worker reopens the stream in place rather than bringing the whole system down, escalating to a USB hub bind cycle after repeated failures. Distinguishing "died after running a while" from "died instantly on open" matters here — an earlier version counted every reopen attempt the same way, so a start-then-instantly-die loop churned forever without ever reaching its escalation.

### Startup

Boot ordering is not guaranteed. `bluetoothd` frequently starts before the USB adapter has finished enumerating, leaving it running with no controller at all — while `rfkill` and `systemd` both report everything as healthy. The startup sequence waits for an actual controller to appear, escalating through service restarts and USB resets until one does.

---

## Status LED design

The LED is the only output on a device with no screen, so what it shows had to be chosen carefully.

An early version lit magenta whenever the microphone heard sound. In a real room that is nearly constant, so the LED sat purple permanently and conveyed nothing. There is deliberately **no** "heard something" color now.

The hardware also constrains the palette: on this LED, cyan renders indistinguishably from green, and yellow appears as a muddy lime. Cyan was originally used for the Bluetooth wait state and looked exactly like the "running" state, which made a stuck device look like a working one. Both colors are now avoided entirely.

Current states:

| Color | Meaning |
|-------|---------|
| Blinking blue | Waiting for internet |
| Blinking magenta | Waiting for a Bluetooth controller |
| Solid white (startup) | Connecting to earbuds |
| Solid green | Listening |
| Solid white (running) | Capturing after a button press |
| Solid blue | Working on a translation |
| Two red blinks | Heard you, but nothing worth translating |

---

## Known limitations

- **Bluetooth adapter stability.** The RTL8761B crashes under sustained load. The software recovers, but recovery takes seconds during which audio is lost.
- **Latency.** Typically a few seconds, varying by language. Some of that is the model, some is the deliberate hold used by the language filter.
- **Cognate false positives.** A foreign phrase that opens with a word resembling its English translation can rarely be muted incorrectly. The debug log records every decision, so this is at least visible when it happens.
- **Requires internet.** All translation is cloud-based. A phone hotspot works, but there is no offline mode.
