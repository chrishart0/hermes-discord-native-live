import asyncio
import threading
import types

import pytest

from conftest import Event
from discord_native_live.live import Live
from discord_native_live.session import Session


class Context:
    def get_config(self, name, default=None):
        return default


class Socket:
    closed = False

    def __init__(self, events=(), close_error=False):
        self.events, self.sent, self.close_error = list(events), [], close_error

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)

    async def send_json(self, event):
        self.sent.append(event)

    async def close(self):
        self.closed = True
        if self.close_error:
            raise RuntimeError('test socket cleanup error')


async def test_eof_is_not_a_provider_finalization_receipt():
    live = Live(lambda *_: None, lambda _: None)
    live.ws = Socket()
    await live.receive()
    assert live.finalized.is_set() and not live.provider_closed
    assert live.usage is None and 'receipt' in live.error
    await live.close()
    assert live.closed and live.ws.closed


async def test_provider_closed_receipt_is_distinct_from_transport_eof():
    live = Live(lambda *_: None, lambda _: None)
    live.ws = Socket()
    await live.event({'type': 'session.closed', 'usage': {'seconds': 20}})
    await live.close()
    assert live.provider_closed and live.usage == {'seconds': 20}
    assert live.error is None


async def test_http_closes_even_when_socket_close_raises():
    live = Live(lambda *_: None, lambda _: None)
    live.ws = Socket(close_error=True)
    live.finalized.set()
    http = types.SimpleNamespace(closed=False)
    async def close_http():
        http.closed = True
    http.close = close_http
    live.http = http
    with pytest.raises(RuntimeError):
        await live.close()
    assert http.closed


async def test_discord_failure_does_not_skip_billed_provider_close(host):
    plugin = types.SimpleNamespace(ctx=Context(), sessions={})
    session = Session(plugin, host.native, Event('join'), host.voice.channel, object())
    session.vc = object()
    host.adapter._voice_clients = {session.guild_id: session.vc}
    called = []
    async def failing_leave(_):
        raise RuntimeError('Discord failed')
    async def live_close():
        called.append('provider closed')
    host.adapter.leave_voice_channel = failing_leave
    session.live.close = live_close
    plugin.sessions[session.key] = session
    await session.close()
    assert called == ['provider closed']
    assert session.closed and session.key not in plugin.sessions


async def test_cleanup_preserves_newer_connection_owner(host):
    plugin = types.SimpleNamespace(ctx=Context(), sessions={})
    session = Session(plugin, host.native, Event('join'), host.voice.channel, object())
    session.vc = object()
    newer_vc, newer_session = object(), object()
    host.adapter._voice_clients = {session.guild_id: newer_vc}
    plugin.sessions[session.key] = newer_session
    calls = []
    async def leave(_):
        calls.append('leave')
    async def close_live():
        calls.append('live')
    host.adapter.leave_voice_channel = leave
    session.live.close = close_live
    await session.close()
    assert calls == ['live']
    assert plugin.sessions[session.key] is newer_session


async def test_session_close_does_not_cancel_native_a(host):
    gate = host.gateway.barriers['A'] = asyncio.Event()
    plugin = types.SimpleNamespace(ctx=Context(), sessions={})
    session = Session(plugin, host.native, Event('join'), host.voice.channel, object())
    host.adapter._voice_clients = {}
    await host.native.submit(host.voice, 'a', 'A', '')
    await asyncio.sleep(0)
    tasks = list(host.adapter._background_tasks)
    await session.close()
    assert tasks and all(not t.done() for t in tasks)
    gate.set()
    await asyncio.gather(*tasks)
    assert host.adapter.text_results[0][1] == 'answer:A'


async def test_cleanup_restores_only_owned_receiver_hooks(host):
    plugin = types.SimpleNamespace(ctx=Context(), sessions={})
    session = Session(plugin, host.native, Event('join'), host.voice.channel, object())
    original_map = lambda *_: None
    session.original_map = original_map
    session.original_buffers = {}
    session.sink = object()
    session.map_callback = lambda *_: None
    receiver = types.SimpleNamespace(_lock=threading.Lock(), _buffers=session.sink,
                                    map_ssrc=session.map_callback)
    session.receiver = receiver
    host.adapter._voice_clients = {}
    await session.close()
    assert receiver._buffers is session.original_buffers
    assert receiver.map_ssrc is original_map
