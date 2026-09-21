"""Primary GPT-Live WebSocket, sharing Hermes's native settings and persona."""
from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from urllib.parse import urlsplit, urlunsplit

import aiohttp


def append_chunks(text: str):
    """<=400 UTF-8 bytes also bounds byte-tokenizers below the 500-token cap.

    Unlike the Desktop character estimate, this holds for non-English text too.
    """
    chunk, size = [], 0
    for char in text:
        n = len(char.encode("utf-8"))
        if size + n > 400:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(char)
        size += n
    if chunk:
        yield "".join(chunk)


def native_settings():
    # Narrow, version-checked private seam; do not clone credential or persona policy.
    from tools import voice_live

    config = voice_live.build_session_config()
    key, base = voice_live._resolve_credentials(voice_live._live_section())
    if not key:
        raise RuntimeError("Set OPENAI_API_KEY or native voice.gpt_live.api_key on this profile")
    url = urlsplit(base)
    if url.scheme != "https" or not url.netloc or url.query or url.fragment or url.username:
        raise RuntimeError("Native voice.gpt_live.base_url must be an HTTPS API base URL")
    endpoint = urlunsplit(("wss", url.netloc, url.path.rstrip("/") + "/live/sessions", "", ""))
    config["audio"]["format"] = {"type": "audio/pcm", "rate": 24000}
    return config, key, endpoint


class Live:
    def __init__(self, on_delegation, on_audio):
        self.on_delegation, self.on_audio = on_delegation, on_audio
        self.http = self.ws = self.reader = None
        self.started = asyncio.Event()
        self.finalized = asyncio.Event()
        self.transcript = deque(maxlen=80)
        self.pending_user = ""
        self.seen: set[str] = set()
        self.sent: set[str] = set()
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.provider_closed = False
        self.close_lock = asyncio.Lock()
        self.error: str | None = None
        self.usage = None
        self.model = self.voice = None

    async def start(self):
        config, key, endpoint = await asyncio.to_thread(native_settings)
        self.model = config["model"]
        self.voice = config["audio"]["output"]["voice"]
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=15))
        try:
            self.ws = await self.http.ws_connect(
                endpoint, headers={"Authorization": f"Bearer {key}"},
                heartbeat=20, max_msg_size=2 * 1024 * 1024,
            )
            self.reader = asyncio.create_task(self.receive(), name="discord-live:receive")
            await self.send({"type": "session.start", "session": config})
            await asyncio.wait_for(self.started.wait(), 20)
            if self.error or self.finalized.is_set():
                raise RuntimeError(self.error or "Live session ended before audio started")
        except BaseException:
            await self.close()
            raise

    async def send(self, event: dict):
        async with self.send_lock:
            if self.closed or self.ws is None or self.ws.closed:
                raise RuntimeError("Live connection is closed")
            await asyncio.wait_for(self.ws.send_json(event), 5)

    async def audio(self, pcm: bytes):
        if pcm:
            await self.send({"type": "session.input_audio.append", "audio": base64.b64encode(pcm).decode("ascii")})

    async def append(self, text: str, delegation_id: str | None = None, *, spoken=False):
        for part in append_chunks(text):
            await self.send({
                "type": "session.commentary.append" if spoken else "session.thinking.append",
                "delegation_id": delegation_id, "content": part,
            })

    async def result(self, delegation_id: str, text: str):
        if self.closed or delegation_id in self.sent:
            return
        # Per-connection dedup only. On any partial send failure close the voice
        # rather than replay an answer whose audible extent cannot be known.
        self.sent.add(delegation_id)
        await self.append(text[:1600], delegation_id, spoken=True)

    async def event(self, event: dict):
        kind = event.get("type")
        if kind == "session.started":
            self.started.set()
        elif kind == "session.closed":
            self.provider_closed = True
            self.usage = event.get("usage")
            self.finalized.set()
        elif kind == "error":
            # Do not log vendor text: it can reflect secrets or request content.
            code = (event.get("error") or {}).get("code")
            self.error = "GPT-Live rejected an event" + (f" ({code})" if isinstance(code, str) and code.isidentifier() else "")
            self.started.set()
            self.finalized.set()
        elif kind == "session.output_audio.delta":
            self.on_audio(base64.b64decode(event["delta"], validate=True))
        elif kind in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            role = "User" if kind == "session.input_transcript.delta" else "Voice assistant"
            delta = event.get("delta", "")
            if isinstance(delta, str) and delta:
                self.transcript.append((role, delta[:6000]))
                if role == "User":
                    self.pending_user = (self.pending_user + delta)[-4000:]
        elif kind == "session.delegation.created":
            delegation = event.get("delegation") or {}
            key = delegation.get("id")
            if delegation.get("target") != "client" or not isinstance(key, str) or not key or key in self.seen:
                return
            if len(self.seen) >= 512:
                raise RuntimeError("Live conversation reached 512 requests; rejoin to continue")
            self.seen.add(key)
            # Text is not in the delegation event. Group streaming transcript
            # deltas, retaining the latest user utterance as the persisted prompt.
            turns = []
            for role, text in self.transcript:
                if turns and turns[-1][0] == role:
                    turns[-1][1] += text
                else:
                    turns.append([role, text])
            # Consecutive user utterances need not have assistant text between
            # them. Do not dispatch A again as part of independent question B.
            prompt, self.pending_user = self.pending_user.strip(), ""
            context = "\n".join(f"{role}: {text}" for role, text in turns)[-6000:]
            if prompt:
                # Callback schedules work; never await the agent on this loop.
                self.on_delegation(key, prompt[-4000:], context)
            else:
                await self.append("No user transcript was available. Ask the user to repeat the request.", key, spoken=True)

    async def receive(self):
        try:
            async for message in self.ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self.event(json.loads(message.data))
                elif message.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError("Live transport failed")
                if self.finalized.is_set():
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error = self.error or "Live transport or protocol failed; text tasks remain available"
        finally:
            if not self.provider_closed and not self.closed:
                self.error = self.error or "Live transport ended without a final provider usage receipt"
            self.started.set()
            self.finalized.set()

    async def close(self):
        async with self.close_lock:
            if self.closed:
                return
            try:
                if self.ws is not None and not self.ws.closed and not self.finalized.is_set():
                    try:
                        await self.send({"type": "session.close"})
                        await asyncio.wait_for(self.finalized.wait(), 15)
                    except (TimeoutError, RuntimeError, aiohttp.ClientError):
                        self.error = self.error or "Live final usage was not confirmed"
                if self.ws is not None and not self.provider_closed:
                    self.error = self.error or "Live final usage was not confirmed"
            finally:
                self.closed = True
                try:
                    if self.ws is not None:
                        await asyncio.wait_for(self.ws.close(), 5)
                finally:
                    if self.reader and self.reader is not asyncio.current_task():
                        self.reader.cancel()
                        await asyncio.gather(self.reader, return_exceptions=True)
                    if self.http is not None:
                        await self.http.close()
