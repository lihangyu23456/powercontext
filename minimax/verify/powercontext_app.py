#!/usr/bin/env python3
"""Start a local PowerContext server for the MiniMax assistant verification."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "/home/hsx/work/powercontext/src")

from powercontext.server.factory import create_server_app  # noqa: E402
from powercontext.server.settings import ServerSettings  # noqa: E402

settings = ServerSettings()
print(f"database: {settings.database}", flush=True)
print(f"mcp path: {settings.mcp.path}", flush=True)
app = create_server_app(settings=settings)
