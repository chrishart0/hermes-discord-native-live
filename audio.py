"""Bounded PCM plumbing. Discord owns codecs, encryption, pacing and its socket."""
from __future__ import annotations

import queue
import threading
from collections.abc import MutableMapping

import numpy as np
import soxr

DISCORD_BYTES = 3840  # 20ms, 48kHz, stereo, signed little-endian int16
SILENCE = bytes(DISCORD_BYTES)


class Capture:
    """Called under Hermes's receiver lock; never waits on an event loop/network."""

    def __init__(self, operator: int):
        self.operator = operator
        self.verified: dict[int, int] = {}
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=50)
        self.enabled = True
        self.overflow = False
        self.lock = threading.Lock()

    def map_speaker(self, ssrc: int, user_id: int) -> None:
        with self.lock:
            self.verified[ssrc] = user_id

    def put(self, ssrc: int, pcm: bytes) -> None:
        with self.lock:
            if not self.enabled or self.verified.get(ssrc) != self.operator:
                return
        # Opus decodes whole PCM frames. Refuse unexpected framing rather than splice it.
        if len(pcm) % DISCORD_BYTES:
            self.overflow = True
            return
        for offset in range(0, len(pcm), DISCORD_BYTES):
            try:
                self.queue.put_nowait(bytes(pcm[offset:offset + DISCORD_BYTES]))
            except queue.Full:
                self.overflow = True
                return

    def read(self) -> bytes:
        if self.overflow:
            raise RuntimeError("Discord capture fell behind; restart live voice")
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return SILENCE  # preserve real-time gaps, not concatenated utterances

    def close(self) -> None:
        with self.lock:
            self.enabled = False
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break


class _FrameSink:
    def __init__(self, capture: Capture, ssrc: int):
        self.capture, self.ssrc = capture, ssrc

    def extend(self, pcm: bytes) -> None:
        self.capture.put(self.ssrc, pcm)


class ReceiverSink(MutableMapping):
    """A scoped replacement for VoiceReceiver._buffers, not its packet decoder.

    The native receiver calls _buffers[ssrc].extend(decoded_pcm). Iteration stays
    empty: its batch/silence/flush paths have no utterances to transcribe. Unknown
    identities are never inferred. No subclass or copy of the crypto/Opus code.
    """

    def __init__(self, capture: Capture):
        self.capture = capture

    def __getitem__(self, ssrc):
        return _FrameSink(self.capture, ssrc)

    def __setitem__(self, key, value):
        raise RuntimeError("Hermes VoiceReceiver buffer contract changed")

    def __delitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


class Audio:
    def __init__(self):
        self.input_resampler = soxr.ResampleStream(48000, 24000, 1, dtype="int16")
        self.output_resampler = soxr.ResampleStream(24000, 48000, 1, dtype="int16")
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.closed = False
        self.output_tail = b""
        self.speaking_frames = 0
        self.quiet_frames = 0
        self.user_speaking = False

    def input(self, pcm: bytes) -> tuple[bytes, bool]:
        stereo = np.frombuffer(pcm, dtype="<i2").reshape(-1, 2)
        mono = (stereo.astype(np.int32).sum(axis=1) // 2).astype(np.int16)
        # A small energy gate only drains LOCAL playback. The Live model owns
        # conversational endpointing. Headphones are required; this is not AEC.
        voiced = bool(np.sqrt(np.mean(mono.astype(np.float32) ** 2)) > 600)
        self.speaking_frames = self.speaking_frames + 1 if voiced else 0
        self.quiet_frames = 0 if voiced else self.quiet_frames + 1
        if self.speaking_frames >= 4:
            self.user_speaking = True
            self.interrupt()
        if self.quiet_frames >= 8:
            self.user_speaking = False
        converted = self.input_resampler.resample_chunk(mono)
        return converted.astype("<i2", copy=False).tobytes(), voiced

    def output(self, pcm: bytes) -> None:
        pcm = self.output_tail + pcm
        complete = len(pcm) - len(pcm) % 2
        self.output_tail = pcm[complete:]
        if not complete:
            return
        samples = np.frombuffer(pcm[:complete], dtype="<i2")
        samples = self.output_resampler.resample_chunk(samples)
        stereo = np.repeat(samples[:, None], 2, axis=1).astype("<i2").tobytes()
        with self.lock:
            if self.closed or self.user_speaking:
                return
            if len(self.buffer) + len(stereo) > DISCORD_BYTES * 250:
                raise RuntimeError("Live playback exceeded the five-second buffer")
            self.buffer.extend(stereo)

    def read(self) -> bytes:
        with self.lock:
            if self.closed:
                return b""
            frame = bytes(self.buffer[:DISCORD_BYTES])
            del self.buffer[:DISCORD_BYTES]
        return frame.ljust(DISCORD_BYTES, b"\0")

    def interrupt(self) -> None:
        with self.lock:
            self.buffer.clear()

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.buffer.clear()

    def source(self):
        import discord
        audio = self

        class LiveSource(discord.AudioSource):
            def read(self):
                return audio.read()

            def is_opus(self):
                return False

            def cleanup(self):
                audio.close()

        return LiveSource()
