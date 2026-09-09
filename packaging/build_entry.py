"""PyInstaller entry point.

Bakes the runtime config the launcher .bat used to set, straight into the .exe, so the
standalone binary behaves the same without any external env/.env setup. `setdefault` means a
real environment variable still overrides these if the user sets one.
"""
import os

os.environ.setdefault("M365_TIME_ZONE", "Europe/Rome")
os.environ.setdefault("M365_WORK_GROUNDING", "false")  # web grounding; work grounding derails coding agents
os.environ.setdefault("M365_DEBUG", "1")

from copilot_proxy_server.cli import main

if __name__ == "__main__":
    main()
