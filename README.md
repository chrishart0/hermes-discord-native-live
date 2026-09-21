# Hermes Discord Native Live

[![Tests](https://github.com/chrishart0/hermes-discord-native-live/actions/workflows/tests.yml/badge.svg)](https://github.com/chrishart0/hermes-discord-native-live/actions/workflows/tests.yml)

**Talk to Hermes in Discord while it works.** This standalone plugin connects Hermes's native GPT-Live settings to its existing Discord bot. Long-running work goes through normal Hermes sessions, so another question does not have to wait for the first task to finish.

**Experimental, v0.1.1.** Published for hands-on testing, not as a production-ready voice release. Automated tests cover packaging, native plugin loading and the task/audio logic; they do not replace a real Discord microphone test. See [validation and the live-test checklist](docs/VALIDATION.md). This is a community plugin, not an official Nous Research integration.

## Install

On the machine running your Hermes gateway, use the normal plugin installer:

```bash
hermes plugins install chrishart0/hermes-discord-native-live --no-enable
hermes plugins enable discord-native-live
```

Review the custom-source/security-scan notice before approving installation. Dependencies are declared in `pyproject.toml`; Hermes installs them into its own environment and re-applies them after updates. You do not need to clone or replace Hermes, run Desktop, or install a second Discord bot.

In the **same Hermes profile** that owns the Discord bot, merge this into `config.yaml`. Keep your other plugin entries and existing voice settings:

```yaml
plugins:
  entries:
    discord-native-live:
      allow_gateway_injection: true
      settings:
        max_jobs: 4

# Reuses the native Desktop GPT-Live configuration.
voice:
  gpt_live:
    model: gpt-live-1
    voice: marin
```

Configure an OpenAI API key through your existing native voice credential configuration (`OPENAI_API_KEY` in that profile's `.env`, or native `voice.gpt_live.api_key`). Do not put real keys in a public issue or commit. `voice.voice_chat_mode` does not need to change. Enable the native `discord` toolset for spoken work-status, steering and cancellation.

```bash
hermes gateway restart
```

For a named profile, run the install/enable/restart commands with `hermes -p YOUR_PROFILE ...` and edit that profile's configuration. The plugin manifest name is **`discord-native-live`**, not the GitHub repository name.

### Requirements

A recent Hermes gateway with native `tools.voice_live` and a working local Discord voice adapter; Python 3.11–3.13; OpenAI GPT-Live access; and at least two allowed native concurrent sessions. Version 0.21.3 alone is not a full compatibility guarantee: this integration uses private instance hooks. See [compatibility](docs/COMPATIBILITY.md) for the inspected commit and exact assumptions.

The bot needs **Create Public Threads**, **Send Messages in Threads**, and its usual text/voice **View Channel**, **Connect** and **Speak** permissions. Start from an ordinary server text channel, not a DM, forum or nested thread.

**Cloud audio and costs:** this mode streams your speech to the native configured OpenAI endpoint. It does not use your local GPU STT/TTS or promise subscription passthrough. Voice is billed separately from Hermes's backend work; idle call time can count. End the call when finished.

**Use headphones and a restricted parent text channel.** One initiating operator is supported. Task threads are public within the parent channel's visibility; they are not private DMs. Unknown/other speakers are not forwarded to the voice service. No acoustic echo canceller or multi-person meeting mode is included.

## Use

Join a voice channel, then type in its associated text channel:

```text
/live-discord join
/live-discord status
/live-discord steer <task-thread-id> Focus on the failing tests
/live-discord cancel <task-thread-id>
/live-discord leave
```

Leave any existing ordinary bot voice connection with `/voice leave` first. Start speaking after the successful Live join message so Discord can establish your speaker identity. The first substantive request creates a real task thread. You can keep talking and make another request while the original work continues.

Interrupting speech does **not** cancel work. `/live-discord leave` closes audio but leaves Hermes tasks running in their text threads. Use an explicit cancellation and exact task-thread ID to stop work. Approvals stay in those threads using Hermes's normal controls.

The native voice inactivity timeout is reused. Voice disconnects on operator departure or revoked authorization. There is no automatic reconnect or replay into a later call: results remain available as text.

## Update or remove

```bash
hermes plugins update discord-native-live
hermes gateway restart

# To disable it without deleting your configuration:
hermes plugins disable discord-native-live
hermes gateway restart
```

Use `/live-discord leave` before updating or disabling an active call. Existing jobs belong to the gateway; restarting that gateway can interrupt them. Ending voice alone does not.

**Migrating from the old fork branch:** end voice and back up any local plugin edits first. The installer will see the old directory as already installed. Reinstall from this repository with `hermes plugins install chrishart0/hermes-discord-native-live --force --no-enable`, review its notices, and enable it again. Existing profile settings use the same plugin ID. Do not merge the old plugin branch into Hermes.

## How it stays small

```text
Discord audio <-> GPT-Live using native Hermes settings
                           |
                           +--> native task thread A: long work
                           +--> native task thread B: another question
                           |
                           <--- result correlated to each original request
```

The plugin reuses Discord's receiver/decoder and Hermes's admission, model resolution, approvals and text delivery. Each request uses a distinct native session key, not another turn queued behind a busy session. There is no new scheduler, job database, agent subprocess, browser, Desktop fork, or provider framework.

Recent voice context and the parent's selected model are passed to each task. This is not a clone of the parent's entire text history or every session-specific setting. Full output stays in the thread; up to 1,600 characters of the final response are sent to the voice model. Concurrent edits to the same repository still need your normal workspace/worktree isolation.

## Testing and feedback

```bash
python -m pip install '.[test]'
python -m pytest -q
# Also use admission/key methods from an actual Hermes source checkout:
HERMES_SOURCE=/path/to/hermes-agent python -m pytest -q
```

CI additionally runs the **real Hermes installer and PluginManager** in a temporary profile against a pinned upstream checkout. It does not connect to Discord or paid voice services. The focused concurrency harness still mocks network/model/persistence and lower agent execution; it is not an end-to-end production test.

For your first live test, use the [acceptance checklist](docs/VALIDATION.md). Report issues with plugin/Hermes versions, OS, the triggering steps, and redacted errors—never credentials or private audio. See [contributing](CONTRIBUTING.md), [security](SECURITY.md), and [changelog](CHANGELOG.md).

MIT. Upstream test excerpts retain their attribution in [third-party notices](THIRD_PARTY_NOTICES.md).
