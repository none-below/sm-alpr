#!/usr/bin/env python3
"""Regression tests for the live-meeting transcriber's two lossy spots.

Both bugs these pin were found live, mid-council-meeting on 2026-09-22, and both
fail *silently* — the transcript simply lacks words, which is indistinguishable
from nobody having spoken.

1. `select()` — whisper can end a window early without erroring. Under a pure
   midpoint-ownership rule the dropped audio is discarded twice: by the window
   that owned it and did not emit it, and by the next window for not owning it.
   That is how "okay, seeing none" — the answer to whether a council member
   pulled an item from the consent calendar — went missing.

2. `slice_wav()` — capture runs are laid out on one meeting timeline. A restart
   that blindly appended to a single file would duplicate the DVR backfill and
   shift every later timestamp.
"""

import json
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import meeting_live_transcribe as mlt


# ----------------------------------------------------------------- select()

# Window 70 from the real meeting: owns [2100, 2130), transcribed [2095, 2140).
WIN_START, OWNED_START, OWNED_END = 2095.0, 2100.0, 2130.0


def rel(*spans):
    """Absolute (start, end, text) -> window-relative, as whisper reports them."""
    return [(s - WIN_START, e - WIN_START, t) for s, e, t in spans]


def test_midpoint_ownership_drops_lookahead_into_next_window():
    segs = rel(
        (2098.0, 2104.0, "spans the left edge, midpoint inside"),
        (2110.0, 2116.0, "squarely owned"),
        (2128.0, 2136.0, "midpoint past owned_end - belongs to the next window"),
    )
    picked, emitted_to, recovered = mlt.select(segs, WIN_START, OWNED_START, OWNED_END, 0.0)
    assert [t for _, t in picked] == [
        "spans the left edge, midpoint inside",
        "squarely owned",
    ]
    assert emitted_to == 2116.0
    assert recovered == 0.0


def test_already_emitted_audio_is_not_repeated():
    """The overlap exists for context, not for duplication."""
    segs = rel((2096.0, 2099.0, "previous window already emitted this"))
    picked, emitted_to, _ = mlt.select(
        segs, WIN_START, OWNED_START, OWNED_END, emitted_to=2100.0
    )
    assert picked == []
    assert emitted_to == 2100.0


def test_backfills_what_a_truncated_predecessor_dropped():
    """The live failure: window 70 stopped at 2125, window 71 must recover 2125-2132.

    Under midpoint ownership alone this segment is rejected by window 71 (its
    midpoint, 2128.5, sits in window 70's territory) and the words are lost.
    """
    nxt_start, nxt_owned_start, nxt_owned_end = 2125.0, 2130.0, 2160.0
    segs = [(2125.0 - nxt_start, 2132.0 - nxt_start, "okay seeing none")]
    picked, emitted_to, recovered = mlt.select(
        segs, nxt_start, nxt_owned_start, nxt_owned_end, emitted_to=2125.0
    )
    assert [t for _, t in picked] == ["okay seeing none"]
    assert emitted_to == 2132.0
    assert recovered == 5.0  # 2125 -> 2130, the part the predecessor owed


def test_backfill_does_not_rewind_the_transcript():
    """A recovered line is stamped from where we left off, so stamps stay ordered."""
    segs = [(0.0, 7.0, "recovered")]
    picked, _, _ = mlt.select(segs, 2125.0, 2130.0, 2160.0, emitted_to=2127.0)
    assert picked[0][0] == 2127.0


# --------------------------------------------------------------- slice_wav()

TONE = b"\x01\x02"  # one non-silent 16-bit sample


def write_run(path: Path, seconds: float) -> None:
    path.write_bytes(TONE * int(seconds * mlt.SAMPLE_RATE))


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        return wf.readframes(wf.getnframes())


def test_restart_gap_is_padded_so_offsets_still_mean_clock_time(tmp_path):
    """Capture died at 10s and resumed at 25s: the hole must occupy 15s of timeline.

    If the missing audio simply closed up, every word after it would be stamped
    15 seconds early for the rest of the meeting.
    """
    write_run(tmp_path / "audio.000.raw", 10)
    write_run(tmp_path / "audio.001.raw", 10)
    runs = [{"file": "audio.000.raw", "start": 0.0}, {"file": "audio.001.raw", "start": 25.0}]

    dest = tmp_path / "w.wav"
    mlt.slice_wav(tmp_path, runs, 0.0, 35.0, dest)
    data = read_wav(dest)

    assert len(data) == int(35 * mlt.BYTES_PER_SEC)
    assert data[: int(10 * mlt.BYTES_PER_SEC)] == TONE * (10 * mlt.SAMPLE_RATE)
    assert set(data[int(10 * mlt.BYTES_PER_SEC) : int(25 * mlt.BYTES_PER_SEC)]) == {0}
    assert data[int(25 * mlt.BYTES_PER_SEC) :] == TONE * (10 * mlt.SAMPLE_RATE)


def test_overlapping_restart_is_trimmed_not_duplicated(tmp_path):
    """A restart that re-backfills the DVR window overlaps; it must not double up."""
    write_run(tmp_path / "audio.000.raw", 30)
    write_run(tmp_path / "audio.001.raw", 30)
    runs = [{"file": "audio.000.raw", "start": 0.0}, {"file": "audio.001.raw", "start": 20.0}]

    dest = tmp_path / "w.wav"
    mlt.slice_wav(tmp_path, runs, 0.0, 50.0, dest)

    assert len(read_wav(dest)) == int(50 * mlt.BYTES_PER_SEC)
    assert mlt.available(tmp_path, runs) == 50.0


def test_slice_is_sample_aligned(tmp_path):
    """Odd byte offsets would swap the halves of a 16-bit sample into noise."""
    write_run(tmp_path / "audio.000.raw", 4)
    runs = [{"file": "audio.000.raw", "start": 0.0}]

    dest = tmp_path / "w.wav"
    mlt.slice_wav(tmp_path, runs, 0.5000001, 2.0, dest)
    assert read_wav(dest).startswith(TONE)


def test_available_ignores_a_run_whose_file_is_missing(tmp_path):
    write_run(tmp_path / "audio.000.raw", 12)
    runs = [{"file": "audio.000.raw", "start": 0.0}, {"file": "gone.raw", "start": 40.0}]
    assert mlt.available(tmp_path, runs) == 12.0


# ------------------------------------------------------------------- filler


def test_filler_over_dead_air_is_dropped_but_real_speech_is_kept():
    assert mlt.is_filler("Thank you.")
    assert mlt.is_filler("  you  ")
    assert mlt.is_filler("[Applause]")
    assert not mlt.is_filler("Thank you, City Clerk.")
    assert not mlt.is_filler("okay seeing none")


def test_runs_manifest_roundtrips(tmp_path):
    runs = [{"file": "audio.000.raw", "start": 0.0}]
    (tmp_path / "runs.json").write_text(json.dumps(runs))
    assert mlt.load_runs(tmp_path) == runs
    assert mlt.load_runs(tmp_path / "nope") == []
