# Security and privacy

This is an experimental, in-process Hermes plugin. Enabling it grants code execution with the gateway's privileges; it is not sandboxed. It uses documented plugin hooks and some private instance-level integration points, listed in `docs/COMPATIBILITY.md`.

Audio from the initiating authorized Discord user is forwarded to the configured cloud Live provider. Model work can use the same tools and credentials as the associated Hermes profile. Task threads inherit their parent text channel's visibility. Use a restricted channel, headphones and normal native approval controls.

Never include API keys, tokens, customer data, raw private audio or unredacted transcripts in public issues. For a security problem, initially report only a non-sensitive description or contact the repository owner privately; do not publish exploit details or secrets. The repository does not promise a response-time SLA.

The plugin does not automatically reconnect or replay old spoken results. A failed shutdown may leave final provider usage unconfirmed; check the provider's usage record when diagnosing billing. Native gateway restarts have their own task-lifecycle behavior.
