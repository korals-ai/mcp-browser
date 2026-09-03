"""Pin the tool-selection guidance in browser_open's description.

This is the model's ONLY signal (independent of persona, which some
providers don't set at all) to prefer WebSearch/WebFetch or a purpose-built
data tool over spinning up a full co-browsing session, and to know that this
browser can't reach every site (platform-infra network/region limits). A
capability sentence like this is a claim with a shelf life — pin it so a
future edit that drops the guidance fails loudly instead of silently.
"""

import asyncio
import re

from src.server import mcp


def _tool_description(name: str) -> str:
    tools = asyncio.run(mcp.list_tools())
    for tool in tools:
        if tool.name == name:
            # Docstrings wrap across lines; a model reads that as plain
            # prose, so collapse whitespace before matching a phrase rather
            # than pinning to one exact line-wrapped literal.
            return re.sub(r"\s+", " ", tool.description or "")
    raise AssertionError(f"no tool named {name!r} registered")


def test_browser_open_tells_the_model_to_prefer_native_web_tools_first() -> None:
    description = _tool_description("browser_open")
    assert "WebSearch" in description
    assert "WebFetch" in description


def test_browser_open_warns_it_cannot_reach_every_site() -> None:
    description = _tool_description("browser_open")
    assert "cannot reach every site" in description
