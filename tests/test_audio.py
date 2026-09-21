import threading

import numpy as np
import pytest

from discord_native_live.audio import DuplexAudio, Capture, DISCORD_BYTES, ReceiverSink, SILENCE


def test_unknown_other_speaker_never_reaches_capture_queue():
    capture = Capture(42)
    sink = ReceiverSink(capture)
    sink[1].extend(SILENCE)
    capture.map_speaker(1, 99)
    sink[1].extend(SILENCE)
    assert capture.queue.empty()
    capture.map_speaker(1, 42)
    sink[1].extend(SILENCE)
    assert capture.queue.qsize() == 1
    assert list(sink.items()) == []


def test_capture_from_real_other_thread_and_silence_gaps():
    capture = Capture(42)
    capture.map_speaker(7, 42)
    pcm = b"\x01\0" * (DISCORD_BYTES // 2)
    thread = threading.Thread(target=lambda: capture.put(7, pcm))
    thread.start()
    thread.join()
    assert capture.read() == pcm
    assert capture.read() == SILENCE


def test_capture_bounded_overflow_is_visible():
    capture = Capture(42)
    capture.map_speaker(1, 42)
    for _ in range(51):
        capture.put(1, SILENCE)
    assert capture.queue.qsize() == 50
    with pytest.raises(RuntimeError, match="fell behind"):
        capture.read()


def test_capture_close_stops_forwarding():
    capture = Capture(42)
    capture.map_speaker(1, 42)
    capture.close()
    capture.put(1, SILENCE)
    assert capture.queue.empty()


def test_resampling_is_stateful_and_rate_correct():
    audio = DuplexAudio()
    total = 0
    for _ in range(100):
        data, voiced = audio.input(SILENCE)
        assert not voiced
        total += len(data)
    # Some converter delay is held at the end. Never feed 48k bytes labeled 24k.
    assert 94000 < total <= 96000


def test_output_is_48k_stereo_twenty_millisecond_frames():
    audio = DuplexAudio()
    samples = np.ones(24000, dtype=np.int16) * 1000
    audio.output(samples.astype("<i2").tobytes())
    frames = [audio.read() for _ in range(40)]
    assert all(len(frame) == DISCORD_BYTES for frame in frames)
    for frame in frames[2:]:
        stereo = np.frombuffer(frame, dtype="<i2").reshape(-1, 2)
        assert np.array_equal(stereo[:, 0], stereo[:, 1])


def test_sustained_barge_in_clears_playback_not_work():
    audio = DuplexAudio()
    audio.output((np.ones(24000, dtype=np.int16) * 1000).tobytes())
    voiced = (np.ones(DISCORD_BYTES // 2, dtype=np.int16) * 2000).tobytes()
    for _ in range(4):
        audio.input(voiced)
    assert audio.user_speaking
    assert audio.read() == SILENCE
    # Output arriving while the operator speaks is drained locally too.
    audio.output(voiced)
    assert audio.read() == SILENCE
    for _ in range(8):
        audio.input(SILENCE)
    assert not audio.user_speaking


def test_playback_close_returns_eof():
    audio = DuplexAudio()
    audio.close()
    assert audio.read() == b""


def test_oversized_playback_is_bounded():
    audio = DuplexAudio()
    with pytest.raises(RuntimeError, match="five-second"):
        audio.output(np.zeros(24000 * 6, dtype="<i2").tobytes())
