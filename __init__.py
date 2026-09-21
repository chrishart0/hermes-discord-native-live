"""Hermes plugin entry point; importing it does not open audio or network connections."""


def register(ctx):
    from .plugin import Plugin

    plugin = Plugin(ctx)
    ctx.register_hook("pre_gateway_dispatch", plugin.capture_command)
    ctx.register_command(
        "live-discord", plugin.command,
        description="Native GPT-Live in Discord (cloud audio; independent Hermes work)",
        args_hint="join | leave | status | cancel <thread-id> | steer <thread-id> <text>",
    )
    ctx.register_tool(
        name="discord_live_work", toolset="discord", schema=plugin.work_schema(),
        handler=plugin.work_tool, description="Control this speaker's existing live-voice work",
    )
    ctx.on_unload(plugin.unload)
