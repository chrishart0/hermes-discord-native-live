"""Regressions for task/turn identity and voice resource ownership."""
from __future__ import annotations

import asyncio
import dataclasses
import sys
from types import SimpleNamespace

import pytest

from conftest import Adapter, Event, Gateway, Source
from discord_native_live.audio import ReceiverTap, Capture
from discord_native_live.live import LiveConnection
from discord_native_live.native import HermesTaskBridge, TaskOwner, current_work
from discord_native_live.plugin import DiscordLivePlugin
from discord_native_live.session import DiscordVoiceSession


async def drain(adapter):
    if adapter._background_tasks:
        await asyncio.wait_for(asyncio.gather(*list(adapter._background_tasks)), 2)


async def test_capacity_counts_admission_once(host):
    host.voice.max_jobs = 2
    store = host.gateway.async_session_store
    original = store.get_or_create_session
    blocked, release = asyncio.Event(), asyncio.Event()

    async def slow_get(source):
        if not blocked.is_set():
            blocked.set()
            await release.wait()
        return await original(source)

    store.get_or_create_session = slow_get
    first = asyncio.create_task(host.native.submit(host.voice, "a", "A", ""))
    await asyncio.wait_for(blocked.wait(), 1)
    try:
        second = await host.native.submit(host.voice, "b", "B", "")
        assert second.delegation_id == "b"
        assert host.native.pending_admissions == 0
    finally:
        release.set()
        await first
        await drain(host.adapter)


async def test_continuation_delivered_after_initial_acknowledgement(host):
    task = await host.native.submit(host.voice, "a", "Long task", "")
    await drain(host.adapter)
    await host.gateway._run_agent("Actual result", "", [], task.source, task.session_key)
    await host.adapter.on_processing_complete(Event("Actual result", task.source), "success")
    await host.adapter.on_processing_complete(Event("Actual result", task.source), "success")
    assert [(update.delegation_id, update.text) for update in host.notices] == [
        ("a", "answer:Long task"), ("a", "answer:Actual result")
    ]
    assert [update.update_id for update in host.notices] == [
        f"{task.thread_id}:1", f"{task.thread_id}:2"
    ]


async def test_queued_completion_is_an_immutable_snapshot(host):
    task = await host.native.submit(host.voice, "a", "Original request", "")
    await drain(host.adapter)
    original_update = host.notices[0]
    await host.gateway._run_agent("Later continuation", "", [], task.source, task.session_key)
    await host.adapter.on_processing_complete(Event("Later continuation", task.source), "success")
    assert original_update.text == "answer:Original request"
    assert host.notices[1].text == "answer:Later continuation"
    with pytest.raises(dataclasses.FrozenInstanceError):
        original_update.text = "overwritten"


async def test_idle_parent_with_live_subagent_still_occupies_capacity(host, monkeypatch):
    task = await host.native.submit(host.voice, "a", "A", "")
    await drain(host.adapter)
    host.voice.max_jobs = 2
    registry = sys.modules["tools.async_delegation"]
    monkeypatch.setattr(registry, "has_live_for_session", lambda session_key: session_key == task.session_key)
    inventory = host.native.inventory(task.owner)
    assert inventory[0]["last_turn"] == "idle"
    assert inventory[0]["background_work"] is True
    gate = host.gateway.barriers["B"] = asyncio.Event()
    await host.native.submit(host.voice, "b", "B", "")
    try:
        with pytest.raises(RuntimeError, match="capacity"):
            await host.native.submit(host.voice, "c", "C", "")
    finally:
        gate.set()
        await drain(host.adapter)


async def test_finished_turn_does_not_disable_explicit_task_controls(host):
    task = await host.native.submit(host.voice, "a", "A", "")
    await drain(host.adapter)
    events = []

    async def record(event):
        events.append(event)

    host.adapter.handle_message = record
    await host.native.control(task.owner, "cancel", task.thread_id)
    assert events[0].text == "/stop"
    assert events[0].source.thread_id == task.thread_id
    assert task.stop_requested


async def test_second_adapter_preserves_its_owner_through_first_wrapper(host):
    gateway = Gateway()
    observed = []

    async def run(message, context_prompt, history, source, session_id, **kwargs):
        observed.append(current_work.get())
        return {"final_response": "done"}

    gateway._run_agent = run
    first = HermesTaskBridge(gateway, Adapter(gateway))
    second = HermesTaskBridge(gateway, Adapter(gateway))
    first.attach(lambda _: False)
    second.attach(lambda _: False)
    voice = SimpleNamespace(
        source=Source(profile="beta"), channel=host.voice.channel,
        max_jobs=4, closing=False, publish=lambda _: None,
    )
    try:
        task = await second.submit(voice, "second", "test", "")
        await drain(second.adapter)
        assert observed == [(second, task.owner)]
        assert current_work.get() is None
    finally:
        second.unload()
        first.unload()


async def test_detaching_voice_keeps_native_work_but_drops_speech(host):
    gate = host.gateway.barriers["A"] = asyncio.Event()
    task = await host.native.submit(host.voice, "a", "A", "")
    await asyncio.sleep(0)
    host.native.detach_voice(host.voice.publish)
    assert task.publish is None and host.native.active(task)
    gate.set()
    await drain(host.adapter)
    assert not host.notices
    assert host.adapter.text_results == [(task.thread_id, "answer:A")]
    # Closed-call history may be reclaimed; live-call idle tasks may not.
    host.native._prune()
    assert not host.native.tasks


async def test_status_does_not_install_observers(host):
    host.native.unload()
    adapter = host.adapter
    adapter._voice_clients = {}
    original = host.gateway._run_agent
    plugin = DiscordLivePlugin(SimpleNamespace())
    reply = plugin.capture_command(event=Event("/live-discord status"), gateway=host.gateway)
    status = await plugin.command(reply["text"].split()[1])
    assert '"connected": false' in status
    assert not plugin.bridges
    assert host.gateway._run_agent is original


async def test_provider_can_speak_multiple_updates_for_one_delegation():
    sent = []
    live = LiveConnection(lambda *_: None, lambda _: None)

    async def send_json(event):
        sent.append(event)

    live.ws = SimpleNamespace(closed=False, send_json=send_json)
    await live.speak("a", "I started the task.")
    await live.speak("a", "Here is the result.")
    assert [event["content"] for event in sent] == ["I started the task.", "Here is the result."]
    assert [event["delegation_id"] for event in sent] == ["a", "a"]


@pytest.mark.parametrize("boundary", ["settings", "connect", "start-event"])
async def test_close_cancels_startup_and_closes_every_created_resource(monkeypatch, boundary):
    import discord_native_live.live as module

    blocked = asyncio.Event()
    resources = []
    never = asyncio.Event()

    async def settings(_):
        if boundary == "settings":
            blocked.set()
            await never.wait()
        return ({"model": "test", "audio": {"output": {"voice": "test"}}},
                "dummy", "wss://example.invalid/live/sessions")

    class Socket:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await never.wait()
            raise StopAsyncIteration

        async def send_json(self, event):
            if event["type"] == "session.start" and boundary == "start-event":
                blocked.set()
                await never.wait()
            if event["type"] == "session.close":
                live.provider_closed = True
                live.finalized.set()

        async def close(self):
            self.closed = True

    class HTTP:
        def __init__(self, **kwargs):
            self.closed = False
            self.socket = None
            resources.append(self)

        async def ws_connect(self, *args, **kwargs):
            if boundary == "connect":
                blocked.set()
                await never.wait()
            self.socket = Socket()
            return self.socket

        async def close(self):
            self.closed = True

    monkeypatch.setattr(module.asyncio, "to_thread", settings)
    monkeypatch.setattr(module.aiohttp, "ClientSession", HTTP)
    live = LiveConnection(lambda *_: None, lambda _: None)
    starting = asyncio.create_task(live.start())
    await asyncio.wait_for(blocked.wait(), 1)
    await asyncio.wait_for(live.close(), 1)
    await asyncio.gather(starting, return_exceptions=True)
    assert live.closing and live.closed
    assert all(http.closed and (http.socket is None or http.socket.closed) for http in resources)
    assert live.reader is None or live.reader.done()


async def test_closed_connection_cannot_be_started(monkeypatch):
    import discord_native_live.live as module

    def no_network(**kwargs):
        pytest.fail("closed connection allocated a new client")

    monkeypatch.setattr(module.aiohttp, "ClientSession", no_network)
    live = LiveConnection(lambda *_: None, lambda _: None)
    await live.close()
    with pytest.raises(RuntimeError, match="only be started once"):
        await live.start()


async def test_outer_session_close_cancels_provider_start(host):
    plugin = SimpleNamespace(
        ctx=SimpleNamespace(get_config=lambda name, default=None: default), sessions={}
    )
    session = DiscordVoiceSession(plugin, host.native, Event("join"), host.voice.channel, object())
    host.adapter._voice_clients = {}
    blocked, cancelled = asyncio.Event(), asyncio.Event()

    async def waiting_start():
        blocked.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    session.live.start = waiting_start
    opening = asyncio.create_task(session.start())
    await asyncio.wait_for(blocked.wait(), 1)
    await asyncio.wait_for(session.close(), 1)
    await asyncio.gather(opening, return_exceptions=True)
    assert session.closed and cancelled.is_set()
    assert session.voice_client is None


async def test_closed_session_rejects_late_delegations(host):
    scheduled = []
    plugin = SimpleNamespace(
        ctx=SimpleNamespace(get_config=lambda name, default=None: default), sessions={},
        spawn=lambda *args: scheduled.append(args),
    )
    session = DiscordVoiceSession(plugin, host.native, Event("join"), host.voice.channel, object())
    session.closing = True
    session.delegate("late", "More work", "")
    assert not scheduled


def test_receiver_tap_does_not_overwrite_new_owner():
    import threading

    original_map = lambda *_: None
    receiver = SimpleNamespace(_lock=threading.Lock(), _buffers={}, map_ssrc=original_map)
    tap = ReceiverTap(receiver, Capture(42))
    new_buffers, new_map = {}, lambda *_: None
    receiver._buffers = new_buffers
    receiver.map_ssrc = new_map
    tap.close()
    assert receiver._buffers is new_buffers and receiver.map_ssrc is new_map


async def test_slot_is_held_until_native_completion_callback(host):
    gate = host.gateway.barriers['A'] = asyncio.Event()
    task = await host.native.submit(host.voice, 'a', 'A', '')
    await asyncio.sleep(0)
    host.native._record_turn(task, 'First reply', 'idle')
    assert host.native.active(task)
    gate.set()
    await drain(host.adapter)
    assert not host.native.active(task)


async def test_leaving_during_dispatch_does_not_admit_new_work(host):
    store = host.gateway.async_session_store
    original = store.set_model_override

    async def leave_during_setup(*args):
        host.native.detach_voice(host.voice.publish)
        await original(*args)

    store.set_model_override = leave_during_setup
    with pytest.raises(RuntimeError, match='Voice ended'):
        await host.native.submit(host.voice, 'a', 'A', '')
    assert host.gateway.calls == []
