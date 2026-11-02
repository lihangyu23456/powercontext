#!/usr/bin/env python3
"""Print the local PowerContext server settings that the verification needs."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.environ.get("MINIMAX_MCP_POWERCONTEXT_SRC", "/home/hsx/work/powercontext/src"))

from powercontext.server.settings import ServerSettings  # noqa: E402

settings = ServerSettings()
print("host:", getattr(settings, "host", None))
print("port:", getattr(settings, "port", None))
print("database:", settings.database)
print("mcp path:", settings.mcp.path)
print("inference:", getattr(settings, "inference", None))
