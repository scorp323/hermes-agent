"""Opus delegation plugin — bundled.

Registers the `opus_delegate` tool into its own `opus_delegate` toolset. It lets a
Hermes agent (e.g. the gpt-5.5 primary) hand a task to Claude Code running
Opus 4.8 on this machine — either for a read-only fresh perspective or, with
approval, for full coding/ops work with file-edit + shell access.

See plugins/opus_delegate/tools.py for the implementation and the safety
model (perspective = read-only; work = approval-gated, fail-closed).

NOTE: registering the tool does NOT expose it to any agent. It becomes
callable only once the `opus_delegate` toolset is added to a profile/platform
toolset. Do not add this to Hermes core tools; per-profile scoping is the
safety boundary.
"""
from __future__ import annotations

from plugins.opus_delegate.tools import (
    OPUS_DELEGATE_SCHEMA,
    _check_claude_cli,
    opus_delegate,
)


def register(ctx) -> None:
    """Register the opus_delegate tool. Called once by the plugin loader."""
    ctx.register_tool(
        name="opus_delegate",
        toolset="opus_delegate",
        schema=OPUS_DELEGATE_SCHEMA,
        handler=opus_delegate,
        check_fn=_check_claude_cli,
        description=OPUS_DELEGATE_SCHEMA["description"],
        emoji="🧠",
    )
