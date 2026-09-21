# Hermes Discord Native Live

[![Tests](https://github.com/chrishart0/hermes-discord-native-live/actions/workflows/tests.yml/badge.svg)](https://github.com/chrishart0/hermes-discord-native-live/actions/workflows/tests.yml)

Talk to Hermes in Discord while it works. This community plugin uses native GPT-Live settings and the existing Discord bot. Each backend request gets a normal Hermes task thread, so another request need not wait for it.

**Experimental, v0.1.2.** Real Discord/GPT-Live microphone testing is still required. Automated tests cover task coordination, audio plumbing and pinned-host compatibility, not conversational quality. This is not an official Nous Research integration.

## Install

Run on the Hermes gateway host:

```bash
hermes plugins install chrishart0/hermes-discord-native-live --no-enable
hermes plugins enable discord-native-live
```

Review the custom-source/security-scan notice. Hermes installs dependencies from `pyproject.toml`; do not replace Hermes or install another bot. In the profile that owns Discord, merge into `config.yaml`:

```yaml
plugins:
  entries:
    discord-native-live:
      allow_gateway_injection: true
      settings:
        max_jobs: 4

# Keep your existing native voice settings when already configured.
voice:
  gpt_live:
    model: gpt-live-1
    voice: marin
```

Use the profile's native OpenAI credential setting (`OPENAI_API_KEY` in `.env`, or `voice.gpt_live.api_key`). Enable the native `discord` toolset for spoken task controls. Changing `voice.voice_chat_mode` is unnecessary.

```bash
hermes gateway restart
```

For a named profile, use `hermes -p YOUR_PROFILE ...` and edit that profile's configuration. The plugin ID is **`discord-native-live`**, not the repository name.

### Requirements and privacy

- Python 3.11–3.13, a recent Hermes gateway with native `tools.voice_live`, a working Discord voice adapter and at least two allowed concurrent sessions. This plugin uses **private instance hooks**; a version number alone is not a compatibility guarantee. See [the pinned host and integration boundaries](docs/COMPATIBILITY.md).
- Bot permissions: Create Public Threads, Send Messages in Threads, View Channel, Connect and Speak. Start from an ordinary server text channel, not a DM, forum or nested thread.
- OpenAI GPT-Live access. **Cloud audio** uses the native configured endpoint and is billed separately from backend work, including idle call time where applicable. This does not use local GPU STT/TTS or promise subscription passthrough.

**Use headphones and a restricted parent text channel.** Task threads are public within that channel's visibility. Only the initiating operator's identified audio is forwarded. No acoustic echo cancellation or multi-speaker meeting mode is included. Do not post credentials or private transcripts in issues.

## Use

Join a voice channel, then run these in the associated text channel:

```text
/live-discord join
/live-discord status
/live-discord steer <task-thread-id> Focus on the failing tests
/live-discord cancel <task-thread-id>
/live-discord leave
```

Leave any ordinary bot voice connection with `/voice leave` first. Speak after the Live join confirmation so Discord can establish your speaker identity.

**Interrupting speech does not cancel work.** Ending voice leaves submitted tasks in their native text threads. Cancellation targets an explicit thread ID. Approvals remain in the task thread using Hermes's normal controls.

A turn ending is not a claim that the whole request finished. Status reports the last turn state and whether Hermes reports live background delegations. Initial acknowledgements and later continuation results can both be spoken, associated with the original request. Full output remains in text; each voice update is limited to 1,600 characters.

The native inactivity timeout is reused. Operator departure or revoked authorization ends voice. Results from an old call are not replayed into a new one. Restarting the gateway can interrupt its tasks; leaving voice alone does not.

## Update or disable

End voice before updating or disabling:

```bash
hermes plugins update discord-native-live
hermes gateway restart

hermes plugins disable discord-native-live
hermes gateway restart
```

From the old fork branch, back up local edits and reinstall with `hermes plugins install chrishart0/hermes-discord-native-live --force --no-enable`. Review the notices and enable it again. Profile settings retain the same plugin ID; do not merge the old branch into Hermes.

## Code map

| Module | Responsibility |
| --- | --- |
| `plugin.py` | Commands, authorization context and call ownership |
| `session.py` | One Discord voice call; routes immutable updates to speech |
| `native.py` | Native task admission, controls and per-turn result observation |
| `live.py` | GPT-Live connection and provider events |
| `audio.py` | PCM conversion, buffering and the scoped receiver compatibility tap |

Hermes owns task execution, persistence and approvals. The plugin keeps only request correlation and voice state. It neither instantiates another agent nor changes Desktop. Tasks inherit recent spoken context and the selected parent model, not every setting or the parent's entire text history. Concurrent edits to one repository still need normal worktree isolation.

## Test and contribute

```bash
python -m pip install '.[test]'
python -m pytest -q
HERMES_SOURCE=/path/to/installed/hermes-agent python -m pytest -q
```

CI installs and loads this plugin using the real Hermes installer and PluginManager. Its host job imports actual admission functions and tests the real background-delegation registry. Model execution, Discord I/O and completion-drain routing remain controlled substitutes. No automated test opens a microphone or calls a paid provider.

Use the [manual acceptance checklist](docs/VALIDATION.md) before relying on voice. Report exact versions, reproduction steps and redacted errors. See [contributing](CONTRIBUTING.md), [security](SECURITY.md) and [changelog](CHANGELOG.md).

MIT. Upstream test excerpts retain their [attribution](THIRD_PARTY_NOTICES.md).
