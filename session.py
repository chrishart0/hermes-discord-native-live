"""One Discord voice call. Closing audio does not cancel native task threads."""
from __future__ import annotations

import asyncio
import logging
import time

from .audio import Capture, DuplexAudio, ReceiverTap
from .live import LiveConnection
from .native import TaskUpdate

logger = logging.getLogger(__name__)


class DiscordVoiceSession:
    def __init__(self, plugin, bridge, event, channel, voice_channel):
        self.plugin = plugin
        self.bridge = bridge
        self.source = event.source
        self.channel = channel
        self.voice_channel = voice_channel
        self.guild_id = int(self.source.scope_id or self.source.guild_id)
        with bridge.gateway._profile_scope_for_source(self.source):
            self.max_jobs = int(plugin.ctx.get_config("max_jobs", 4))
        if not 2 <= self.max_jobs <= 16:
            raise ValueError("discord-native-live max_jobs must be between 2 and 16")
        self.audio = DuplexAudio()
        self.capture = Capture(int(self.source.user_id))
        self.live = LiveConnection(self.delegate, self.output)
        self.voice_client = None
        self.tap: ReceiverTap | None = None
        self.stream_task: asyncio.Task | None = None
        self._opening: asyncio.Task | None = None
        self.closing = False
        self.closed = False
        self.last_voice = time.monotonic()
        self.close_lock = asyncio.Lock()

    @property
    def key(self) -> tuple[int, int]:
        return id(self.bridge.adapter), self.guild_id

    async def start(self) -> None:
        if self.closing or self._opening is not None:
            raise RuntimeError("A Discord voice call can only be started once")
        if self.bridge.adapter._voice_clients.get(self.guild_id):
            raise RuntimeError("Leave the existing voice connection first with /voice leave")
        self._opening = asyncio.create_task(self._open(), name="discord-live:join")
        try:
            await self._opening
        except BaseException:
            await self.close()
            raise

    async def _open(self) -> None:
        adapter = self.bridge.adapter
        # Establish the provider first; do not leave batch STT running during its handshake.
        with self.bridge.gateway._profile_scope_for_source(self.source):
            await self.live.start()
            joined = await adapter.join_voice_channel(
                self.voice_channel, text_channel_id=self.channel.id, source=self.source.to_dict()
            )
        if not joined:
            raise RuntimeError("Hermes could not join this Discord voice channel")
        self.voice_client = adapter._voice_clients[self.guild_id]
        receiver = adapter._voice_receivers.get(self.guild_id)
        if (receiver is None or not callable(getattr(receiver, "map_ssrc", None))
                or not hasattr(receiver, "_buffers") or not hasattr(receiver, "_lock")):
            raise RuntimeError("Hermes did not provide a compatible voice receiver")
        listener = adapter._voice_listen_tasks.pop(self.guild_id, None)
        if listener is not None:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        # Existing maps may contain native sole-member inference. Admit only new SPEAKING events.
        self.tap = ReceiverTap(receiver, self.capture)
        self.voice_client.stop()
        adapter._voice_mixers.pop(self.guild_id, None)
        self.voice_client.play(self.audio.source())
        self.stream_task = self.plugin.spawn(self.stream(), "audio")

    def output(self, pcm: bytes) -> None:
        self.audio.output(pcm)
        if pcm:
            self.last_voice = time.monotonic()

    def delegate(self, delegation_id: str, prompt: str, context: str) -> None:
        if not self.closing:
            self.plugin.spawn(self.dispatch(delegation_id, prompt, context), "dispatch")

    async def dispatch(self, delegation_id: str, prompt: str, context: str) -> None:
        if self.closing:
            return
        try:
            task = await self.bridge.submit(self, delegation_id, prompt, context)
            if not self.closing:
                await self.live.append(
                    f"Hermes accepted task {task.thread_id}: {prompt[:120]}. It is not finished.",
                    delegation_id,
                )
        except Exception as exc:
            if not self.closing:
                text = str(exc) if isinstance(exc, (ValueError, PermissionError, RuntimeError)) else (
                    "Task submission failed; check the associated text channel."
                )
                await self.live.speak(delegation_id, text[:500])

    def publish(self, update: TaskUpdate) -> None:
        if not self.closing:
            self.plugin.spawn(self.deliver(update), "result")

    async def deliver(self, update: TaskUpdate) -> None:
        if self.closing:
            return
        text = update.text or "This Hermes turn ended without a spoken reply; check its text thread."
        if update.turn_status in {"failed", "interrupted"}:
            text = f"The Hermes turn {update.turn_status}. " + text
        try:
            await self.live.speak(
                update.delegation_id, f"For your request {update.request[:100]!r}: {text}"
            )
        except Exception:
            # Partial speech cannot be replayed reliably; keep the native text result instead.
            self.live.error = f"Voice delivery failed for task {update.thread_id}; check its text thread."
            await self.close()

    def _still_connected(self) -> bool:
        adapter = self.bridge.adapter
        client = self.voice_client
        if (not client.is_connected() or client.channel.id != self.voice_channel.id
                or adapter._voice_clients.get(self.guild_id) is not client):
            return False
        if not self.bridge.authorized(self.source, voice=True):
            return False
        member = self.voice_channel.guild.get_member(int(self.source.user_id))
        return bool(member and member.voice and member.voice.channel
                    and member.voice.channel.id == self.voice_channel.id)

    async def stream(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        checked = 0.0
        try:
            while not self.closing and not self.live.finalized.is_set():
                now = loop.time()
                if now - deadline > 0.25:
                    raise RuntimeError("Voice event loop fell behind")
                await asyncio.sleep(max(0.0, deadline - now))
                pcm, voiced = self.audio.input(self.capture.read())
                if voiced:
                    self.last_voice = time.monotonic()
                if now - checked >= 1:
                    checked = now
                    if not self._still_connected():
                        break
                    timeout = self.bridge.adapter._voice_timeout_limit()
                    if timeout > 0 and time.monotonic() - self.last_voice >= timeout:
                        break
                    if time.monotonic() - self.last_voice < 2:
                        self.bridge.adapter._reset_voice_timeout(self.guild_id)
                await self.live.audio(pcm)
                deadline += 0.02
        except asyncio.CancelledError:
            raise
        except Exception:
            self.live.error = "Live audio stopped; native task threads remain available."
        finally:
            if not self.closing:
                await self.close()

    async def close(self) -> None:
        self.closing = True
        self.capture.close()
        self.audio.close()
        self.bridge.detach_voice(self.publish)
        opening = self._opening
        if opening is not None and not opening.done():
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
        async with self.close_lock:
            if self.closed:
                return
            adapter = self.bridge.adapter
            try:
                if self.tap is not None:
                    self.tap.close()
                if self.voice_client and adapter._voice_clients.get(self.guild_id) is self.voice_client:
                    await asyncio.wait_for(adapter.leave_voice_channel(self.guild_id), 5)
            except Exception as exc:
                logger.warning("Discord voice cleanup failed (%s)", type(exc).__name__)
            finally:
                try:
                    await self.live.close()
                finally:
                    self.closed = True
                    if self.stream_task and self.stream_task is not asyncio.current_task():
                        self.stream_task.cancel()
                        await asyncio.gather(self.stream_task, return_exceptions=True)
                    if self.plugin.sessions.get(self.key) is self:
                        self.plugin.sessions.pop(self.key)
            if self.live.error:
                try:
                    await asyncio.wait_for(adapter.send(
                        str(self.channel.id), self.live.error + " Existing work stays in its task threads."
                    ), 5)
                except Exception:
                    logger.warning("Could not deliver the voice-close notice to channel %s", self.channel.id)
