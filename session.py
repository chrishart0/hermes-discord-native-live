"""One operator's audio lifetime; native Hermes work has a separate lifetime."""
from __future__ import annotations

import asyncio
import time
import logging

log = logging.getLogger(__name__)

from .audio import Audio, Capture, ReceiverSink
from .live import Live


class Session:
    def __init__(self, plugin, native, event, channel, voice_channel):
        self.plugin, self.native = plugin, native
        self.source, self.channel, self.voice_channel = event.source, channel, voice_channel
        self.guild_id = int(self.source.scope_id or self.source.guild_id)
        with native.gateway._profile_scope_for_source(self.source):
            self.max_jobs = int(plugin.ctx.get_config("max_jobs", 4))
        if not 2 <= self.max_jobs <= 16:
            raise ValueError("discord-native-live max_jobs must be between 2 and 16")
        self.audio = Audio()
        self.capture = Capture(int(self.source.user_id))
        self.live = Live(self.delegate, self.output)
        self.vc = self.receiver = self.player = self.loop_task = None
        self.sink = None
        self.original_buffers = self.original_map = self.map_callback = None
        self.closed = False
        self.last_voice = time.monotonic()
        self.close_lock = asyncio.Lock()

    async def start(self):
        adapter = self.native.adapter
        if adapter._voice_clients.get(self.guild_id):
            raise RuntimeError("Leave the existing voice connection first with /voice leave")
        try:
            # Connect Live before opening Discord, so the old batch listener has
            # no provider-handshake interval in which to submit an unwanted turn.
            with self.native.gateway._profile_scope_for_source(self.source):
                await self.live.start()
                joined = await adapter.join_voice_channel(
                    self.voice_channel, text_channel_id=self.channel.id, source=self.source.to_dict())
            if not joined:
                raise RuntimeError("Hermes could not join this Discord voice channel")
            self.vc = adapter._voice_clients[self.guild_id]
            self.receiver = adapter._voice_receivers.get(self.guild_id)
            if (self.receiver is None or not callable(getattr(self.receiver, "map_ssrc", None))
                    or not hasattr(self.receiver, "_buffers") or not hasattr(self.receiver, "_lock")):
                raise RuntimeError("Hermes did not provide a compatible voice receiver")
            listener = adapter._voice_listen_tasks.pop(self.guild_id, None)
            if listener:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
            receiver = self.receiver
            self.original_map = receiver.map_ssrc

            def map_speaker(ssrc, user_id):
                self.original_map(ssrc, user_id)
                self.capture.map_speaker(ssrc, user_id)

            self.map_callback = map_speaker
            receiver.map_ssrc = map_speaker
            # Existing maps might include Hermes's sole-member inference. Only
            # subsequent genuine SPEAKING opcodes are admitted to cloud audio.
            self.sink = ReceiverSink(self.capture)
            with receiver._lock:
                self.original_buffers = receiver._buffers
                receiver._buffers = self.sink
                self.original_buffers.clear()
            self.vc.stop()
            adapter._voice_mixers.pop(self.guild_id, None)
            self.player = self.audio.source()
            self.vc.play(self.player)
            self.loop_task = self.plugin.spawn(self.stream(), "audio")
        except BaseException:
            await self.close()
            raise

    def output(self, pcm):
        self.audio.output(pcm)
        if pcm:
            self.last_voice = time.monotonic()

    def delegate(self, delegation_id, prompt, context):
        if not self.closed:
            self.plugin.spawn(self.dispatch(delegation_id, prompt, context), "dispatch")

    async def dispatch(self, delegation_id, prompt, context):
        try:
            if self.closed:
                return
            job = await self.native.submit(self, delegation_id, prompt, context)
            if not self.closed:
                await self.live.append(f"Hermes accepted task {job.id}: {prompt[:120]}. It is not finished.", delegation_id)
        except Exception as exc:
            if not self.closed:
                # Locally generated errors only; avoid vendor request bodies.
                text = str(exc) if isinstance(exc, (ValueError, PermissionError, RuntimeError)) else "Task submission failed; check the associated text channel."
                await self.live.result(delegation_id, text[:500])

    async def stream(self):
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        checked = 0.0
        try:
            while not self.closed and not self.live.finalized.is_set():
                now = loop.time()
                if now - deadline > 0.25:
                    raise RuntimeError("Voice event loop fell behind; no queued audio was replayed")
                await asyncio.sleep(max(0.0, deadline - now))
                pcm, voiced = self.audio.input(self.capture.read())
                if voiced:
                    self.last_voice = time.monotonic()
                if now - checked >= 1:
                    checked = now
                    if (not self.vc.is_connected() or self.vc.channel.id != self.voice_channel.id
                            or self.native.adapter._voice_clients.get(self.guild_id) is not self.vc):
                        break
                    if not self.native.authorized(self.source, voice=True):
                        raise PermissionError("Voice operator authorization ended")
                    member = self.voice_channel.guild.get_member(int(self.source.user_id))
                    if not member or not member.voice or member.voice.channel.id != self.voice_channel.id:
                        break
                    timeout = self.native.adapter._voice_timeout_limit()
                    if timeout > 0 and time.monotonic() - self.last_voice >= timeout:
                        break
                    if time.monotonic() - self.last_voice < 2:
                        self.native.adapter._reset_voice_timeout(self.guild_id)
                await self.live.audio(pcm)
                deadline += 0.02
        except asyncio.CancelledError:
            raise
        except Exception:
            self.live.error = "Live audio stopped. Existing Hermes tasks continue in their text threads."
        finally:
            await self.close()

    async def close(self):
        async with self.close_lock:
            if self.closed:
                return
            self.closed = True
            self.capture.close()
            self.audio.close()
            adapter = self.native.adapter
            try:
                if self.receiver:
                    with self.receiver._lock:
                        if self.sink is not None and self.receiver._buffers is self.sink:
                            self.receiver._buffers = self.original_buffers
                    if self.map_callback is not None and self.receiver.map_ssrc is self.map_callback:
                        self.receiver.map_ssrc = self.original_map
                if self.vc and adapter._voice_clients.get(self.guild_id) is self.vc:
                    await asyncio.wait_for(adapter.leave_voice_channel(self.guild_id), 5)
            except Exception as exc:
                # Closing Discord must never prevent closing the billed provider.
                log.warning("Discord audio cleanup failed (%s)", type(exc).__name__)
            finally:
                try:
                    # No job cancellation: the native adapter owns submitted work.
                    await self.live.close()
                finally:
                    if self.loop_task and self.loop_task is not asyncio.current_task():
                        self.loop_task.cancel()
                    if self.plugin.sessions.get(self.key) is self:
                        self.plugin.sessions.pop(self.key, None)
            if self.live.error:
                try:
                    await asyncio.wait_for(adapter.send(
                        str(self.channel.id),
                        self.live.error + " Existing work remains in its task threads."), 5)
                except Exception:
                    log.warning("Could not deliver live-voice error notice")

    @property
    def key(self):
        return (id(self.native.adapter), self.guild_id)
