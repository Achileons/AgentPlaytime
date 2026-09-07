"""Shared logical tool names and detector metadata.

The tracker persists these names, so detector modules should import them from
one place instead of each maintaining a subtly different spelling.
"""

from __future__ import annotations

from typing import Final


CHATGPT: Final = "ChatGPT"
CLAUDE: Final = "Claude"
CLAUDE_CODE: Final = "Claude Code"
CODEX: Final = "Codex"
CURSOR: Final = "Cursor"

SUPPORTED_TOOLS: Final[tuple[str, ...]] = (
    CHATGPT,
    CLAUDE,
    CLAUDE_CODE,
    CODEX,
    CURSOR,
)

