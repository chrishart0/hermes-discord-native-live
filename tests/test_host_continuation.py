"""Real Hermes registry/admission; fake model, Discord and completion-drain routing."""
from __future__ import annotations

import asyncio
import os
import sys
import threading

import pytest

if os.environ.get("HERMES_SOURCE"):
    from tools import async_delegation as registry
    from tools.process_registry import process_registry

pytestmark = pytest.mark.skipif(not os.environ.get("HERMES_SOURCE"), reason="needs installed Hermes checkout")


async def test_native_background_registry_returns_a_later_voice_update(host, monkeypatch, tmp_path):
    from conftest import Event
    from discord_native_live.native import HermesTaskBridge

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "tools.async_delegation", registry)
    registry._reset_for_tests()
    release = threading.Event()
    host.native.unload()

    def worker():
        if not release.wait(10):
            raise RuntimeError("test failed to release worker")
        return {"summary": "Completed A"}

    async def model(message, context_prompt, history, source, session_id, **kwargs):
        if message == "A":
            handle = registry.dispatch_async_delegation(
                goal="A", context=None, toolsets=None, role="leaf", model="test",
                session_key=session_id, parent_session_id=session_id, runner=worker,
            )
            assert handle["status"] == "dispatched"
            return {"final_response": "A is running"}
        return {"final_response": message}

    host.gateway._run_agent = model
    bridge = HermesTaskBridge(host.gateway, host.adapter)
    bridge.attach(lambda _: False)
    host.voice.bridge = bridge
    try:
        task = await bridge.submit(host.voice, "voice-a", "A", "")
        await asyncio.gather(*list(host.adapter._background_tasks))
        assert task.turn_status == "idle" and bridge.background_active(task)
        await bridge.submit(host.voice, "voice-b", "B", "")
        await asyncio.gather(*list(host.adapter._background_tasks))
        assert [(u.delegation_id, u.text) for u in host.notices] == [
            ("voice-a", "A is running"), ("voice-b", "B")
        ]
        assert bridge.background_active(task)  # B did not wait for or cancel A.
        release.set()
        event = await asyncio.to_thread(process_registry.completion_queue.get, True, 5)
        assert event["session_key"] == task.session_key
        assert event["summary"] == "Completed A"
        # Model the gateway's later-message routing, but use the real native admission method.
        await host.adapter.handle_message(Event(event["summary"], task.source, allow_gateway_control=False))
        await asyncio.gather(*list(host.adapter._background_tasks))
        assert [(u.delegation_id, u.text) for u in host.notices] == [
            ("voice-a", "A is running"), ("voice-b", "B"), ("voice-a", "Completed A")
        ]
        assert host.notices[0].update_id != host.notices[-1].update_id
    finally:
        release.set()
        bridge.unload()
        registry._reset_for_tests()
