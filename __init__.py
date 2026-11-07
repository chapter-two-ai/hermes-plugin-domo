"""Domo: chief-of-staff tools and the Domo app's backend, as one Hermes plugin."""

from __future__ import annotations

from . import tools
from .tools import PROMPT, bound_tools


def register(ctx):
    # register() runs once per profile scope; pin this scope's home so tool calls on worker
    # threads write to the right profile's store.
    for name, schema, handler, emoji in bound_tools(tools.home_resolver()):
        ctx.register_tool(name=name, toolset="domo", schema=schema, handler=handler,
                          description=schema["description"], emoji=emoji)
    ctx.register_system_prompt_section("domo.chief-of-staff", PROMPT, max_chars=1200)
