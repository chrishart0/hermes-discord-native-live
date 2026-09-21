# Changelog

## 0.1.2 — 2026-09-21

- Separate request identity, per-turn status and immutable voice updates; later background results are no longer suppressed by an initial acknowledgement.
- Cancel and await pending startup before closing provider or Discord resources.
- Count admission reservations once and retain native background-work status.
- Name the task bridge, voice session and Live connection explicitly; isolate the receiver tap and make status commands read-only.
- Keep existing shared-gateway context protection, native approvals and text delivery.
- Add lifecycle/continuation regressions and pinned-host checks using actual admission functions and the background-delegation registry.

Still experimental; real Discord/GPT-Live microphone testing is required.

## 0.1.1 — 2026-09-21

- Publish as `chrishart0/hermes-discord-native-live`, a standalone public repository.
- Declare dependencies in `pyproject.toml` for Hermes's installer and updater.
- Add manifest configuration metadata, public installation/migration instructions, contribution guidance, issue templates and a manual acceptance checklist.
- Add pinned-host installer/loader checks alongside the offline regression suite.
- Preserve recognized task context when multiple adapter observers share a gateway.

Experimental: a real Discord/GPT-Live microphone acceptance test is still required.

## 0.1.0

Initial prototype on a dedicated branch of the owner's Hermes fork. No longer the recommended distribution source.
