#!/usr/bin/env python3
"""Live-transcribe a city meeting stream so you can prep public comment in real time.

Two halves that run independently:

  capture     yt-dlp resolves the YouTube live HLS manifest; ffmpeg writes
              headerless mono 16k PCM. Gov streams are ``playlist_type/DVR`` with
              a ~3600s window, so ``-live_start_index 0`` backfills up to an hour
              before you started — you do not have to launch this on time.

  transcribe  slices overlapping windows out of that PCM and runs whisper over
              each, appending to a rolling transcript stamped in real
              wall-clock time. mlx-whisper on Apple Silicon (~13x realtime on an
              M-series); faster-whisper on CPU anywhere else (``--engine
              faster``, the default off a Mac — e.g. a cloud session). Either
              closes a cold-start backlog and then sits at the live edge,
              roughly 20-50s behind the room.

Run both at once with ``watch`` (the normal case), or separately if capture is
already running. ``--once`` flushes the tail after capture stops.

Three things that are easy to get wrong, all learned the hard way live:

* **Anchor.** ``#EXT-X-PROGRAM-DATE-TIME`` at ``#EXT-X-MEDIA-SEQUENCE:0`` is the
  true stream start. Guessing it put every timestamp 39 minutes out.
* **Windows overlap, ownership does not.** Window n transcribes
  ``[n*stride-pad_before, (n+1)*stride+pad_after)`` but owns only
  ``[n*stride, (n+1)*stride)``; see ``select()``. Whisper *silently* truncates
  long chunks — on 120s segments it dropped 67 seconds mid-sentence once and an
  entire chunk another time — so windows are short and each backfills whatever
  its predecessor failed to emit.
* **Restarts do not append.** Each capture run is its own file with a recorded
  offset on the meeting timeline (``runs.json``); overlap is trimmed and a hole
  is padded with silence, so a byte offset always means the same clock time.

``--alert`` takes regexes; matching lines go to stderr and ``alerts.txt`` so you
can tail one small file instead of the whole transcript.

Audio only: a slide the speaker never reads aloud is invisible here.

Examples
--------
    # follow tonight's council meeting, alert on the consent item you care about
    scripts/meeting_live_transcribe.py watch --channel UCtQV2ZVAgoV6smi6kWfQU8A \
        --out ~/meetings/2026-09-22 \
        --alert 'item (thirteen|13)' --alert 'consent' --alert 'pull(ed)? .*calendar'

    # from a Linux/cloud box before the meeting starts: wait up to 2h for the
    # channel to go live, then run faster-whisper on CPU
    scripts/meeting_live_transcribe.py watch --channel UCtQV2ZVAgoV6smi6kWfQU8A \
        --out /tmp/meeting --engine faster --wait-live 120

    # rehearse on a past meeting: replay 20 min of the recording at 2x through
    # the same capture -> overlapping-window path (stamps are replay wall-clock)
    scripts/meeting_live_transcribe.py watch --video <VOD id> --out /tmp/rehearsal \
        --engine faster --replay 2 --start 2:48:00 --duration 1200

    # transcribe only, against a capture another process is writing
    scripts/meeting_live_transcribe.py transcribe --out ~/meetings/2026-09-22
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")

# mlx-whisper only runs on Apple Silicon; faster-whisper (CTranslate2, CPU int8)
# runs anywhere, which is what a cloud session gets.
DEFAULT_MODELS = {
    "mlx": "mlx-community/whisper-large-v3-turbo",
    "faster": "large-v3-turbo",
}
SEGMENT_SECONDS = 120

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2  # s16le mono

# Overlapping-window defaults: transcribe 45s every 30s. Each window owns only its
# middle 30s, so nothing is emitted twice; the padding exists so whisper always has
# real audio on both sides of a boundary instead of a hard cut mid-sentence.
STRIDE = 30.0
PAD_BEFORE = 5.0
PAD_AFTER = 10.0

# A live capture that stops growing for this long is wedged, not quiet.
STALL_AFTER = 90.0

# Lines whisper emits over dead air. Matched against the whole stripped line,
# case-insensitively, after punctuation is trimmed.
FILLER = {
    "thank you",
    "thanks for watching",
    "you",
    "bye",
    "applause",
    "music",
    "silence",
    "[music]",
    "[applause]",
    "♪",
}


def log(msg: str) -> None:
    print(f"[{datetime.now(PACIFIC):%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def fmt(anchor: "datetime | None", offset: float) -> str:
    """Timeline offset as wall-clock when anchored, else elapsed time."""
    if anchor:
        return (anchor + timedelta(seconds=offset)).strftime("%H:%M:%S")
    s = int(offset)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------- capture


def resolve_live_video(channel: str) -> tuple[str, str]:
    """Return (video_id, title) for a channel's current live stream."""
    url = f"https://www.youtube.com/channel/{channel}/live"
    html = subprocess.run(
        ["curl", "-s", "--max-time", "30", "-A", "Mozilla/5.0", url],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    vid = re.search(r'"videoId":"([\w-]{11})"', html)
    title = re.search(r'"title":"((?:[^"\\]|\\.)*)","lengthSeconds"', html)
    if not vid:
        raise SystemExit(f"no video id found at {url} — channel may not be live")
    if '"isLive":true' not in html:
        log("warning: page does not report isLive=true; recording anyway")
    return vid.group(1), (title.group(1) if title else "(unknown)")


def yt_dlp() -> list[str]:
    return ["yt-dlp"] if shutil.which("yt-dlp") else ["uvx", "yt-dlp"]


def default_engine() -> str:
    return "mlx" if sys.platform == "darwin" and platform.machine() == "arm64" else "faster"


def wait_for_channel(channel: str, deadline: float, poll: float = 30.0) -> tuple[str, str]:
    """Poll a channel until it has a live (or scheduled) stream to point at.

    Before a meeting the ``/live`` page either has no video at all or shows the
    *scheduled* stream; ``hls_url`` then waits for that one to actually start.
    """
    while True:
        try:
            return resolve_live_video(channel)
        except (SystemExit, subprocess.CalledProcessError) as exc:
            if time.time() > deadline:
                raise SystemExit(f"channel {channel} never showed a stream: {exc}")
            log(f"no stream on the channel yet ({exc}); retrying in {poll:.0f}s")
            time.sleep(poll)


def hls_url(video_id: str, wait: bool = False) -> str:
    # A scheduled stream that has not started makes plain `-g` fail with "This
    # live event will begin in N minutes"; --wait-for-video polls until it starts.
    cmd = yt_dlp() + (["--wait-for-video", "30"] if wait else [])
    out = subprocess.run(
        cmd + ["-f", "bestaudio", "-g", f"https://www.youtube.com/watch?v={video_id}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    line = out.splitlines()[-1] if out else ""
    if not line.startswith("http"):
        raise SystemExit(f"yt-dlp returned no stream url for {video_id}")
    return line


def playlist_start(url: str) -> datetime | None:
    """Wall-clock start of the DVR window, from the playlist's first
    ``EXT-X-PROGRAM-DATE-TIME``.

    Only trustworthy as the anchor for segment 0 when ffmpeg was given
    ``-live_start_index 0`` *and* the window still reaches media-sequence 0 —
    i.e. you started capturing within the DVR window (~1h) of stream start.
    """
    try:
        body = subprocess.run(
            ["curl", "-s", "--max-time", "25", url], capture_output=True, text=True, check=True
        ).stdout
    except subprocess.CalledProcessError:
        return None
    seq = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", body)
    pdt = re.search(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)", body)
    if not pdt:
        return None
    start = datetime.fromisoformat(pdt.group(1)).astimezone(PACIFIC)
    if seq and seq.group(1) != "0":
        log(f"warning: DVR window starts at media-sequence {seq.group(1)}, not 0 —"
            " anchor is the window start, not the stream start")
    return start


def load_runs(out: Path) -> list[dict]:
    f = out / "runs.json"
    return json.loads(f.read_text()) if f.exists() else []


def start_capture(
    out: Path, video_id: str, seconds: int, from_start: bool, segments: bool,
    wait: bool = False, replay: dict | None = None,
) -> subprocess.Popen:
    segdir = out / "seg"
    segdir.mkdir(parents=True, exist_ok=True)
    url = hls_url(video_id, wait=wait)
    (out / "hls_url.txt").write_text(url + "\n")

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning"]
    if replay:
        # A finished recording, fed at a fixed multiple of realtime so the window
        # loop sees audio arrive the way it would live. No DVR playlist, so no
        # program-date-time: stamps are the replay's own wall-clock.
        window_start = datetime.now(PACIFIC)
        cmd += ["-readrate", str(replay["speed"])]
        if replay["start"]:
            cmd += ["-ss", str(replay["start"])]
        if replay["duration"]:
            cmd += ["-t", str(replay["duration"])]
    elif from_start:
        window_start = playlist_start(url)
        # Backfill the whole DVR window rather than joining at the live edge.
        cmd += ["-live_start_index", "0"]
    else:
        window_start = datetime.now(PACIFIC)
    cmd += ["-i", url, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le"]
    logf = open(out / "ffmpeg.log", "ab")
    if segments:
        cmd += [
            "-f", "segment", "-segment_time", str(seconds), "-reset_timestamps", "1",
            str(segdir / "seg_%04d.wav"),
        ]
        proc = subprocess.Popen(cmd, stdout=logf, stderr=logf)
        log(f"capture started (pid {proc.pid}) -> {segdir}")
        return proc

    # Headerless PCM: no size fields to finalise, so the reader can slice any byte
    # range while ffmpeg is still writing. Each capture *run* gets its own file and
    # a recorded start offset on the meeting timeline. Appending a restart to one
    # shared file would silently duplicate the DVR backfill and shift every
    # subsequent timestamp, so runs stay separate and the reader stitches them.
    runs = load_runs(out)
    anchor_file = out / "anchor.txt"
    if runs and anchor_file.exists():
        anchor = datetime.fromisoformat(anchor_file.read_text().strip())
        offset = (window_start - anchor).total_seconds() if window_start else None
        if offset is None:
            raise SystemExit("restarting needs a playlist date-time to place the new run")
        prev = runs[-1]
        prev_end = prev["start"] + (out / prev["file"]).stat().st_size / BYTES_PER_SEC
        if offset > prev_end:
            log(f"restart leaves a {offset - prev_end:.0f}s hole at "
                f"{(anchor + timedelta(seconds=prev_end)):%H:%M:%S} — that audio is gone")
        else:
            log(f"restart overlaps {prev_end - offset:.0f}s of existing capture; trimmed on read")
    else:
        if window_start is None:
            raise SystemExit("could not read EXT-X-PROGRAM-DATE-TIME; cannot anchor the capture")
        anchor, offset = window_start, 0.0
        anchor_file.write_text(anchor.isoformat() + "\n")
        log(f"anchor (stream start) = {anchor:%Y-%m-%d %H:%M:%S %Z}")

    name = f"audio.{len(runs):03d}.raw"
    runs.append({"file": name, "start": offset})
    (out / "runs.json").write_text(json.dumps(runs, indent=1))
    cmd += ["-f", "s16le", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=open(out / name, "wb"), stderr=logf)
    log(f"capture started (pid {proc.pid}) -> {name} at offset {offset:.0f}s")
    return proc


# ------------------------------------------------------------- transcribe


ENGINE = "mlx"
_FASTER: dict = {}


def transcribe_file(wav: Path, model: str) -> list[tuple[float, float, str]]:
    """Run whisper over one wav; return (start, end, text) triples."""
    if ENGINE == "faster":
        return transcribe_faster(wav, model)
    with_json = wav.with_suffix(".json")
    subprocess.run(
        [
            "uvx", "--from", "mlx-whisper", "mlx_whisper",
            "--model", model,
            "--language", "en",
            "--output-format", "json",
            "--output-dir", str(wav.parent),
            "--verbose", "False",
            str(wav),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    if not with_json.exists():
        return []
    data = json.loads(with_json.read_text())
    return [
        (float(s["start"]), float(s["end"]), s["text"].strip())
        for s in data.get("segments", [])
        if s.get("text", "").strip()
    ]


def transcribe_faster(wav: Path, model: str) -> list[tuple[float, float, str]]:
    """faster-whisper on CPU. The model loads once and stays resident — reloading
    a large model per 45s window would cost more than transcribing it."""
    if model not in _FASTER:
        from faster_whisper import WhisperModel

        t = time.time()
        _FASTER[model] = WhisperModel(
            model, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4
        )
        log(f"loaded {model} in {time.time() - t:.0f}s")
    segs, _info = _FASTER[model].transcribe(
        str(wav), language="en", beam_size=1, condition_on_previous_text=False
    )
    return [(float(s.start), float(s.end), s.text.strip()) for s in segs if s.text.strip()]


def available(out: Path, runs: list[dict]) -> float:
    """Seconds of meeting timeline covered by the capture runs so far."""
    end = 0.0
    for r in runs:
        try:
            size = (out / r["file"]).stat().st_size
        except FileNotFoundError:
            continue
        end = max(end, r["start"] + size / BYTES_PER_SEC)
    return end


def slice_wav(out: Path, runs: list[dict], start: float, end: float, dest: Path) -> None:
    """Cut [start, end) of the meeting timeline out of the capture runs into a WAV.

    Runs are laid out on one timeline; a later run that overlaps an earlier one is
    trimmed, and a hole between runs (a capture that died and was restarted late)
    is filled with silence so every byte offset still means the same wall-clock time.
    """
    pos = start
    chunks: list[bytes] = []
    for r in sorted(runs, key=lambda r: r["start"]):
        path = out / r["file"]
        if not path.exists():
            continue
        r_start = r["start"]
        r_end = r_start + path.stat().st_size / BYTES_PER_SEC
        if r_end <= pos or r_start >= end:
            continue
        if r_start > pos:  # hole before this run
            chunks.append(b"\0" * (int((r_start - pos) * BYTES_PER_SEC) & ~1))
            pos = r_start
        take_to = min(end, r_end)
        with path.open("rb") as fh:
            fh.seek(int((pos - r_start) * BYTES_PER_SEC) & ~1)
            chunks.append(fh.read(int((take_to - pos) * BYTES_PER_SEC)))
        pos = take_to
        if pos >= end:
            break
    with wave.open(str(dest), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(chunks))


def select(
    segs: list[tuple[float, float, str]],
    win_start: float,
    owned_start: float,
    owned_end: float,
    emitted_to: float,
) -> tuple[list[tuple[float, str]], float, float]:
    """Pick which of a window's whisper segments this window should emit.

    Ownership is by midpoint, so the overlap between windows never duplicates text.
    But a window must also backfill whatever its predecessor failed to emit: whisper
    can end a window early without erroring, and under a pure midpoint rule that
    audio is discarded twice — once by the window that owned it and did not emit it,
    once by this window for not owning it. Observed live: the sentence "okay, seeing
    none" disappeared that way.

    Returns (emitted, new_emitted_to, recovered_seconds).
    """
    picked: list[tuple[float, str]] = []
    recovered = 0.0
    for start, end, text in segs:
        abs_start, abs_end = win_start + start, win_start + end
        mid = (abs_start + abs_end) / 2
        if mid >= owned_end or abs_end <= emitted_to + 0.5:
            continue
        if mid < owned_start:
            recovered = max(recovered, min(owned_start, abs_end) - abs_start)
        picked.append((max(abs_start, emitted_to), text))
        emitted_to = max(emitted_to, abs_end)
    return picked, emitted_to, recovered


def windows(
    out: Path,
    model: str,
    stride: float,
    pad_before: float,
    pad_after: float,
    anchor: datetime | None,
    alerts: list[re.Pattern],
    drop_filler: bool,
    poll: float,
    once: bool,
    stall_after: float = 0.0,
    on_stall: "Callable[[], None] | None" = None,
) -> None:
    """Transcribe overlapping windows of the growing raw capture.

    Window n covers ``[n*stride - pad_before, (n+1)*stride + pad_after)`` but only
    *owns* ``[n*stride, (n+1)*stride)``. A whisper segment is emitted by the window
    that owns its midpoint, so the overlap never duplicates text — it exists purely
    to give whisper real audio either side of the boundary, which is what stops
    sentences being cut in half. Cost is ``(stride+pad_before+pad_after)/stride``
    times more audio decoded; at ~14x realtime that is free.
    """
    transcript = out / "transcript.txt"
    statefile = out / "state.json"
    tmp = out / "window.wav"
    n = 0
    emitted_to = 0.0
    if statefile.exists():
        st = json.loads(statefile.read_text())
        n = int(st.get("next_window", 0))
        emitted_to = float(st.get("emitted_to", 0.0))

    tf = transcript.open("a", encoding="utf-8")
    af = (out / "alerts.txt").open("a", encoding="utf-8")
    grew_at, last_avail, stalled = time.time(), -1.0, False

    while True:
        runs = load_runs(out)
        if not runs:
            if once:
                break
            time.sleep(poll)
            continue
        avail = available(out, runs)
        if avail > last_avail:
            last_avail, grew_at, stalled = avail, time.time(), False
        owned_start = n * stride
        owned_end = owned_start + stride
        win_start = max(0.0, owned_start - pad_before)
        win_end = owned_end + pad_after

        final = False
        if avail < win_end:
            # Not enough audio yet. In a live run just wait — never emit a partial
            # window, because advancing past it would skip the audio the capture
            # has not written yet. `--once` is the explicit "capture is over, flush
            # the tail" pass, and it resumes from state.json at exactly this window.
            if not once:
                # A dead capture looks exactly like a quiet room: the transcript
                # simply stops. ffmpeg does not necessarily exit — it can sit in a
                # "Connection reset by peer" retry loop forever — so watch the
                # audio actually growing, not the process being alive.
                idle = time.time() - grew_at
                if stall_after and idle > stall_after and not stalled:
                    stalled = True
                    log(f"CAPTURE STALLED — no new audio for {idle:.0f}s "
                        f"(transcript ends at {fmt(anchor, avail)})")
                    if on_stall:
                        if on_stall() == "done":
                            once = True  # capture finished: flush the tail, then stop
                            continue
                        grew_at = time.time()
                time.sleep(poll)
                continue
            if avail <= owned_start + 1:
                break
            win_end, final = avail, True

        slice_wav(out, runs, win_start, win_end, tmp)
        try:
            segs = transcribe_file(tmp, model)
        except Exception as exc:  # noqa: BLE001 — one bad window must not end a live run
            log(f"whisper failed on window {n}: {exc}")
            n += 1
            continue

        picked, emitted_to, gap = select(segs, win_start, owned_start, owned_end, emitted_to)
        kept = 0
        for offset, text in picked:
            if drop_filler and is_filler(text):
                continue
            line = f"[{fmt(anchor, offset)}] {text}"
            tf.write(line + "\n")
            kept += 1
            for pat in alerts:
                if pat.search(text):
                    hit = f"*** {pat.pattern} *** {line}"
                    af.write(hit + "\n")
                    af.flush()
                    log(hit)
                    break
        # Whisper dropping the tail of a window is silent, and silence is
        # indistinguishable from nobody speaking. Say so in the log.
        shortfall = owned_end - emitted_to
        if not final and shortfall > 2.0:
            log(f"window {n} ends {shortfall:.0f}s short — next window will backfill")
        tf.flush()
        n += 1
        statefile.write_text(json.dumps({"next_window": n, "emitted_to": emitted_to}))
        recovered = f", recovered {gap:.0f}s" if gap > 1.0 else ""
        log(f"window {n - 1} [{owned_start:.0f}-{min(owned_end, win_end):.0f}s] -> {kept} lines{recovered}")
        if final:
            break


def is_filler(text: str) -> bool:
    bare = re.sub(r"[^\w♪\[\] ]", "", text).strip().lower()
    return bare in FILLER or bare == ""


def segment_is_complete(path: Path, settle: float = 2.0) -> bool:
    """A segment ffmpeg still has open keeps growing; wait for a stable size."""
    try:
        first = path.stat().st_size
    except FileNotFoundError:
        return False
    time.sleep(settle)
    try:
        return path.stat().st_size == first and first > 0
    except FileNotFoundError:
        return False


def run_transcribe(
    out: Path,
    model: str,
    seconds: int,
    anchor: datetime | None,
    alerts: list[re.Pattern],
    drop_filler: bool,
    poll: float,
    once: bool,
) -> None:
    segdir = out / "seg"
    transcript = out / "transcript.txt"
    alertfile = out / "alerts.txt"
    statefile = out / "state.json"
    done: set[str] = set()
    if statefile.exists():
        done = set(json.loads(statefile.read_text()).get("done", []))

    tf = transcript.open("a", encoding="utf-8")
    af = alertfile.open("a", encoding="utf-8")

    while True:
        pending = sorted(p for p in segdir.glob("seg_*.wav") if p.name not in done)
        # The highest-numbered segment is the one ffmpeg is still writing.
        progressed = False
        for wav in pending:
            if not segment_is_complete(wav):
                continue
            idx = int(wav.stem.split("_")[1])
            base = idx * seconds
            try:
                segs = transcribe_file(wav, model)
            except subprocess.CalledProcessError as exc:
                log(f"whisper failed on {wav.name}: {exc}")
                continue
            for start, _end, text in segs:
                if drop_filler and is_filler(text):
                    continue
                offset = base + start
                if anchor:
                    stamp = (anchor + timedelta(seconds=offset)).strftime("%H:%M:%S")
                else:
                    stamp = f"{int(offset) // 3600:02d}:{int(offset) % 3600 // 60:02d}:{int(offset) % 60:02d}"
                line = f"[{stamp}] {text}"
                tf.write(line + "\n")
                for pat in alerts:
                    if pat.search(text):
                        hit = f"*** {pat.pattern} *** {line}"
                        af.write(hit + "\n")
                        af.flush()
                        log(hit)
                        break
            tf.flush()
            done.add(wav.name)
            statefile.write_text(json.dumps({"done": sorted(done)}))
            progressed = True
            log(f"{wav.name} -> {len(segs)} segments ({len(done)} done)")
        if once and not pending:
            break
        if not progressed:
            time.sleep(poll)


# ------------------------------------------------------------------ cli


def parse_anchor(value: str | None, out: Path) -> datetime | None:
    """Anchor segment 0 to a wall-clock time, so stamps match the agenda."""
    if value is None:
        cached = out / "anchor.txt"
        if cached.exists():
            return datetime.fromisoformat(cached.read_text().strip())
        return None
    if value == "auto":
        url_file = out / "hls_url.txt"
        anchor = playlist_start(url_file.read_text().strip()) if url_file.exists() else None
        if anchor is None:
            raise SystemExit("could not read EXT-X-PROGRAM-DATE-TIME; pass --anchor explicitly")
    else:
        anchor = datetime.fromisoformat(value)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=PACIFIC)
    (out / "anchor.txt").write_text(anchor.isoformat() + "\n")
    return anchor


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["watch", "capture", "transcribe"])
    ap.add_argument("--out", required=True, type=Path, help="working directory for this meeting")
    ap.add_argument("--channel", help="YouTube channel id to resolve the current live stream from")
    ap.add_argument("--video", help="YouTube video id (skips channel lookup)")
    ap.add_argument("--engine", choices=["mlx", "faster"], default=default_engine(),
                    help="mlx-whisper (Apple Silicon) or faster-whisper (CPU, anywhere); "
                         "default: mlx on an arm64 Mac, else faster")
    ap.add_argument("--model", help="whisper model (default depends on --engine: "
                    + ", ".join(f"{k}={v}" for k, v in DEFAULT_MODELS.items()) + ")")
    ap.add_argument("--wait-live", type=float, default=0.0, metavar="MINUTES",
                    help="start before the meeting: wait up to this long for the channel's "
                         "stream to go live instead of exiting")
    ap.add_argument("--stride", type=float, default=STRIDE,
                    help=f"seconds of new audio per window (default {STRIDE:g})")
    ap.add_argument("--pad-before", type=float, default=PAD_BEFORE)
    ap.add_argument("--pad-after", type=float, default=PAD_AFTER,
                    help="lookahead past the window; adds directly to latency")
    ap.add_argument("--segments", action="store_true",
                    help="legacy: non-overlapping segment files instead of overlapping windows")
    ap.add_argument("--segment-seconds", type=int, default=SEGMENT_SECONDS,
                    help="segment length for --segments")
    ap.add_argument("--anchor", help="wall-clock time of offset 0 (ISO, or 'auto')")
    ap.add_argument("--alert", action="append", default=[], help="regex; repeatable")
    ap.add_argument("--no-drop-filler", dest="drop_filler", action="store_false")
    ap.add_argument("--live-edge", dest="from_start", action="store_false",
                    help="join at the live edge instead of backfilling the DVR window")
    ap.add_argument("--poll", type=float, default=10.0)
    ap.add_argument("--stall-after", type=float, default=STALL_AFTER,
                    help="seconds without new audio before declaring the capture "
                         f"stalled; `watch` then restarts it (default {STALL_AFTER:g}, 0 disables)")
    ap.add_argument("--once", action="store_true", help="transcribe what exists, then exit")
    ap.add_argument("--replay", type=float, metavar="SPEED",
                    help="rehearsal: --video is a finished recording; feed it at SPEED x "
                         "realtime through the live path")
    ap.add_argument("--start", default="", help="with --replay: offset into the recording "
                    "(seconds or HH:MM:SS)")
    ap.add_argument("--duration", default="", help="with --replay: seconds of recording to feed")
    args = ap.parse_args()

    global ENGINE
    ENGINE = args.engine
    args.model = args.model or DEFAULT_MODELS[args.engine]
    needed = ["ffmpeg"]
    if args.engine == "mlx" or not shutil.which("yt-dlp"):
        needed.append("uvx")
    for tool in needed:
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found on PATH")

    args.out.mkdir(parents=True, exist_ok=True)
    alerts = [re.compile(p, re.I) for p in args.alert]

    cap: subprocess.Popen | None = None
    wait = args.wait_live > 0
    replay = None
    if args.replay:
        if not args.video:
            raise SystemExit("--replay needs --video (a finished recording)")
        replay = {"speed": args.replay, "start": args.start, "duration": args.duration}
        args.from_start = False
    if args.mode in ("watch", "capture"):
        video = args.video
        if not video:
            if not args.channel:
                raise SystemExit("--video or --channel required for capture")
            if wait:
                video, title = wait_for_channel(args.channel, time.time() + args.wait_live * 60)
            else:
                video, title = resolve_live_video(args.channel)
            log(f"stream: {title} ({video})")
        cap = start_capture(
            args.out, video, args.segment_seconds, args.from_start, args.segments,
            wait=wait, replay=replay,
        )
        (args.out / "video_id.txt").write_text(video + "\n")

    if args.mode == "capture":
        try:
            cap.wait()
        except KeyboardInterrupt:
            cap.send_signal(signal.SIGINT)
        return 0

    # After capture, so `watch` picks up the anchor start_capture just wrote.
    anchor = parse_anchor(args.anchor, args.out)

    def restart_capture() -> str | None:
        """Kill a wedged ffmpeg and start a fresh run.

        The new run backfills the DVR window, so a stall caught inside the hour
        loses nothing at all — which is the whole point of reacting automatically
        rather than waiting for someone to notice the transcript went quiet.
        """
        nonlocal cap
        if replay:
            # A replay that stops growing has reached the end of what it was fed.
            return "done" if cap and cap.poll() is not None else None
        if cap and cap.poll() is None:
            cap.kill()
            cap.wait(timeout=10)
        cap = start_capture(
            args.out, video, args.segment_seconds, args.from_start, args.segments
        )

    try:
        if args.segments:
            run_transcribe(
                args.out, args.model, args.segment_seconds, anchor,
                alerts, args.drop_filler, args.poll, args.once,
            )
        else:
            windows(
                args.out, args.model, args.stride, args.pad_before, args.pad_after,
                anchor, alerts, args.drop_filler, args.poll, args.once,
                stall_after=args.stall_after,
                on_stall=restart_capture if args.mode == "watch" else None,
            )
    except KeyboardInterrupt:
        pass
    finally:
        if cap and cap.poll() is None:
            cap.send_signal(signal.SIGINT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
