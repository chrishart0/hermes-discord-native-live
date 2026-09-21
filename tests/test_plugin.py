import dataclasses
import types

import pytest

from conftest import Event, Source
from discord_native_live.plugin import Plugin


class Context:
    def get_config(self, key, default=None):
        return default


def test_command_capture_only_records_until_native_auth_dispatch(host):
    plugin = Plugin(Context())
    event = Event("/live-discord join")
    result = plugin.capture_command(event=event, gateway=host.gateway)
    assert result["action"] == "rewrite"
    assert len(plugin.tickets) == 1
    assert plugin.sessions == {} and plugin.tasks == set()
    assert not host.gateway.calls


async def test_command_tickets_are_single_use_and_not_global_last_sender(host):
    plugin = Plugin(Context())
    a = plugin.capture_command(event=Event("/live-discord status"), gateway=host.gateway)
    b = plugin.capture_command(event=Event("/live-discord status", Source(user_id="99")), gateway=host.gateway)
    assert a["text"] != b["text"]
    tickets = list(plugin.tickets.values())
    assert [r[1].source.user_id for r in tickets] == ["42", "99"]
    assert "no authenticated" in await plugin.command("made-up-ticket")


def test_capture_declines_internal_or_noncontrol_voice_input(host):
    plugin = Plugin(Context())
    for event in [Event("/live-discord join", internal=True), Event("/live-discord join", allow_gateway_control=False)]:
        assert plugin.capture_command(event=event, gateway=host.gateway) is None
    assert not plugin.tickets


def test_pending_command_tickets_are_bounded(host):
    plugin = Plugin(Context())
    for _ in range(200):
        plugin.capture_command(event=Event("/live-discord status"), gateway=host.gateway)
    assert len(plugin.tickets) == 128


def test_work_tool_denies_nonvoice_execution():
    plugin = Plugin(Context())
    assert "error" in plugin.work_tool({"action": "cancel", "thread_id": "201"})


def test_runtime_does_not_create_agents_or_subprocesses():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        calls = [node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
        assert "AIAgent" not in calls
        assert "subprocess" not in path.read_text()
