# Public-package verification — 2026-09-21

## Executed checks

The [publication workflow](https://github.com/chrishart0/hermes-discord-native-live/actions/runs/35637873480) created, tested, and published plugin commit **`93825c8dc91064be71ba4b88f50c15e23c6a0091`** (v0.1.1).

The workflow definition was triggered at migration commit `2f5ea81b440a169f442307f3fd33ff8c0a9b7dbd`; the job created the candidate commit above **before** executing these checks and pushed that same candidate only after they passed. Its logs record both identities.

| Check | Observed result |
| --- | --- |
| Plugin build on Python 3.13.15 / Ubuntu | Wheel built successfully |
| Offline regression suite | 52 passed |
| Actual Hermes plugin installer in a temporary profile | Installed the manifest-named plugin and declared Python dependencies |
| Actual Hermes `PluginManager` | Loaded without error: 1 tool, 1 hook, 1 command |
| Native configuration helpers and required adapter signatures | Passed |
| Regression suite using methods extracted from the pinned Hermes checkout | 52 passed |
| Default-branch publication and read-back | Published commit matches `93825c8dc91064be71ba4b88f50c15e23c6a0091` |

Hermes checkout: **`4b8a8134009a8727a289bcabeb0019fedd353128`**, reporting 0.21.3. Discord dependency used for the host check: **discord.py 2.7.1**.

Normal subsequent commits run [the Tests workflow](https://github.com/chrishart0/hermes-discord-native-live/actions/workflows/tests.yml): offline checks on Python 3.11 and 3.13, plus the real installer/loader check on the pinned host. Check each run's exact commit and conclusion; this receipt is not a claim that every later commit has passed.

## What these checks do not establish

The installer and loader were real. The task regression harness still mocks network, models, persistence, and lower agent execution; loading upstream admission/key methods does not make it a full end-to-end gateway test.

No real Discord voice connection, microphone conversation, paid GPT-Live call, or live tool-approval/cancellation test was performed. The public package remains **experimental and ready for hands-on testing**, not production-certified.

Use [the live acceptance issue](https://github.com/chrishart0/hermes-discord-native-live/issues/1) and [the detailed checklist](VALIDATION.md) to record those remaining checks. Do not mark them complete based on this automated receipt.
