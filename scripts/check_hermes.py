"""Install/load this commit in a disposable real Hermes profile; never open audio."""
from __future__ import annotations

import importlib
import inspect
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="hermes-plugin-smoke-") as tmp:
        home = Path(tmp)
        os.environ["HERMES_HOME"] = str(home)
        bundled = home / "empty-bundled"
        bundled.mkdir()
        os.environ["HERMES_BUNDLED_PLUGINS"] = str(bundled)
        os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = "0"
        os.environ.pop("HERMES_SAFE_MODE", None)
        (home / "config.yaml").write_text(
            "plugins:\n  enabled: []\n  entries:\n    discord-native-live:\n"
            "      allow_gateway_injection: true\n", encoding="utf-8"
        )
        from hermes_cli.plugins_cmd import cmd_install
        cmd_install(ROOT.as_uri(), enable=True, force=True)
        installed = home / "plugins" / "discord-native-live"
        assert (installed / "plugin.yaml").is_file()
        assert (installed / "pyproject.toml").is_file()
        assert (installed / "live.py").read_bytes() == (ROOT / "live.py").read_bytes()

        from hermes_cli.plugins import PluginManager
        manager = PluginManager()
        with patch("aiohttp.ClientSession", side_effect=AssertionError("Loading must not start voice")):
            manager.discover_and_load()
        rows = [r for r in manager.list_plugins() if r["name"] == "discord-native-live"]
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["enabled"] and not row["error"], row
        assert row["tools"] == 1 and row["hooks"] == 1 and row["commands"] == 1, row
        module = manager._plugins[row["key"]].module
        runtime = importlib.import_module(module.__name__ + ".native")
        transport = importlib.import_module(module.__name__ + ".live")
        audio = importlib.import_module(module.__name__ + ".audio")

        from tools import voice_live
        assert voice_live.build_session_config()["delegation"]["type"] == "client"
        from plugins.platforms.discord.adapter import DiscordAdapter, VoiceReceiver
        for name in ("join_voice_channel", "leave_voice_channel", "play_in_voice_channel", "get_user_voice_channel"):
            assert callable(getattr(DiscordAdapter, name))
        assert "source" in inspect.signature(DiscordAdapter.join_voice_channel).parameters
        from gateway.run import GatewayRunner
        assert "source" in inspect.signature(GatewayRunner._run_agent).parameters
        from gateway.session_identity import replace_source
        from gateway.session import SessionSource, build_session_key
        from gateway.config import Platform
        source = SessionSource(platform=Platform.DISCORD, chat_id="100", user_id="42", chat_type="channel", scope_id="1")
        a = replace_source(source, chat_id="201", thread_id="201", parent_chat_id="100", chat_type="thread")
        b = replace_source(source, chat_id="202", thread_id="202", parent_chat_id="100", chat_type="thread")
        assert build_session_key(a) != build_session_key(b)
        assert runtime.TaskOwner.from_source(a) == runtime.TaskOwner.from_source(b)
        assert list(transport.append_chunks("loader check")) == ["loader check"]

        # Exercise the real receiver's buffer/flush contract without sockets or codecs.
        receiver = VoiceReceiver(None, allowed_user_ids={"42"})
        capture = audio.Capture(42)
        old_buffers = receiver._buffers
        tap = audio.ReceiverTap(receiver, capture)
        receiver.map_ssrc(7, 42)
        receiver._buffers[7].extend(audio.SILENCE)
        assert capture.queue.qsize() == 1
        assert receiver.check_silence() == [] and receiver.flush_pending() == []
        tap.close()
        assert receiver._buffers is old_buffers
        manager.unload()
        print(json.dumps({"native_install": "passed", "native_load": "passed", "receiver_contract": "passed", "registration": row, "live_audio": "not tested"}, indent=2))


if __name__ == "__main__":
    main()
