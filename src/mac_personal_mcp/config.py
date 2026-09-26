"""Opt-in switches for tools that change state or send data off the machine.

Everything defaults to off. Read-only tools are always available.
"""

import os
from pathlib import Path


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# Notes/Contacts writes and local alias edits
ENABLE_WRITES = _flag("MAC_MCP_ENABLE_WRITES")

# imessage_send. Kept separate from ENABLE_WRITES because sending is the
# channel a prompt-injected agent would use to exfiltrate data.
ENABLE_SEND = _flag("MAC_MCP_ENABLE_SEND")

# By default imessage_send only reaches handles you already have a
# conversation with. Set this to allow brand-new recipients.
ALLOW_NEW_RECIPIENTS = _flag("MAC_MCP_ALLOW_NEW_RECIPIENTS")

# shortcuts_list / shortcuts_run / shortcuts_create. Shortcuts can do almost
# anything on the Mac, so running them is equivalent to arbitrary automation.
ENABLE_SHORTCUTS = _flag("MAC_MCP_ENABLE_SHORTCUTS")

# User data (handle aliases) lives outside the repo so it can never be committed.
CONFIG_DIR = Path(
    os.environ.get("MAC_MCP_CONFIG_DIR", str(Path.home() / ".config" / "mac-personal-mcp"))
)
ALIASES_FILE = CONFIG_DIR / "aliases.json"
