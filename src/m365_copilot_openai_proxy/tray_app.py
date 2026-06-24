"""GlobalProtect-style system-tray app for the proxy.

A tray icon + a small CustomTkinter window (Status / Logs / Settings tabs) with a
Connected/Disconnected toggle. Starting/stopping reuses the `serve` internals (token capture,
auto-refresh, client wiring). With the windowed build there is no console, so stdout/logging is
redirected into the in-app Logs tab.

Update is opt-in: on launch it reads the release update.json manifest and, if newer, shows a pop-up.
On accept, an installed build downloads the signed Inno installer and runs it silently (in-place
upgrade); a portable single-file swaps itself on restart — the manifest names both URLs explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from importlib import resources
from pathlib import Path

import httpx

from m365_copilot_openai_proxy import __version__ as APP_VERSION

REPO = "MassimilianoPili/m365-copilot-proxy"

# Palette
BG = "#16161A"
CARD = "#1F1F26"
ACCENT = "#6E56CF"
ACCENT_HOVER = "#7C66DB"
GREEN = "#30A46C"
RED = "#E5484D"
RED_HOVER = "#EC5D62"
MUTED = "#8B8B95"
TEXT = "#ECECEE"

_GUI_SETTINGS = Path.home() / ".m365-copilot-openai-proxy" / "gui-settings.json"
_DEFAULT_SETTINGS = {
    "port": 8000,
    "work_grounding": False,  # web grounding (better for coding agents)
    "temporary_chat": True,  # disableMemory=1 (no history/memories)
    "configure_clients": True,  # wire Claude Code + VS Code while running
    "launch_edge": True,  # open the debug Edge window for token capture/refresh
    "auto_refresh": True,
    "auto_connect": False,  # do NOT connect automatically on launch (user connects explicitly)
    "persist_default": True,  # reuse one substrate conversation per client chat
    "ws_reuse": False,  # keep one WebSocket alive per session (experimental)
    "passthrough_claude": True,  # forward non-m365 models straight to the real Anthropic API
    "anthropic_key": "",  # optional API-key override for passthrough (empty -> OAuth file)
    "hide_on_token_success": True,  # automatically close/hide debug browser on token capture
}


def _pkg_file(name: str) -> str:
    return str(resources.files(__package__).joinpath(name))


def _hide_console() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def load_settings() -> dict:
    try:
        return {
            **_DEFAULT_SETTINGS,
            **json.loads(_GUI_SETTINGS.read_text(encoding="utf-8")),
        }
    except Exception:
        return dict(_DEFAULT_SETTINGS)


def save_settings(s: dict) -> None:
    try:
        _GUI_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        _GUI_SETTINGS.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"  ! could not save settings: {exc}")


def _tray_image(connected: bool):
    """The tray icon with a green (connected) / red (disconnected) status badge in the corner."""
    from PIL import Image, ImageDraw

    base = Image.open(_pkg_file("icon.png")).convert("RGBA").resize((64, 64))
    draw = ImageDraw.Draw(base)
    r = 24
    color = (46, 204, 113, 255) if connected else (231, 76, 60, 255)
    draw.ellipse(
        [64 - r, 64 - r, 63, 63], fill=color, outline=(22, 22, 26, 255), width=3
    )
    return base


# --- proxy lifecycle ---------------------------------------------------------------------------


class ProxyController:
    def __init__(self, settings: dict):
        self.settings = settings
        self.host = "127.0.0.1"
        self.cdp_port = 9222
        self.port = int(settings.get("port", 8000))
        self._server = None
        self._thread = None
        self._stop_refresh = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self.browser_running = False

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            import uvicorn

            from .app import create_app
from .config import write_config_value
            from .cli import (
                _auto_refresh_loop,
                _configure_clients,
                _launch_debug_edge,
                _needs_substrate_token,
                _read_token,
                _startup_capture_loop,
            )

            s = self.settings
            write_config_value("work_grounding", "true" if s.get("work_grounding") else "false")
            write_config_value("disable_memory", "true" if s.get("temporary_chat", True) else "false")
            write_config_value("persist_default", "true" if s.get("persist_default", True) else "false")
            write_config_value("ws_reuse", "true" if s.get("ws_reuse", False) else "false")
            write_config_value("anthropic_passthrough", "true" if s.get("passthrough_claude", False) else "false")
            write_config_value("hide_on_token_success", "true" if s.get("hide_on_token_success", True) else "false")
            write_config_value("anthropic_key", (s.get("anthropic_key") or "").strip())
            self.port = int(s.get("port", 8000))

            print(f"Starting proxy on {self.base_url} ...")
            self._server = uvicorn.Server(
                uvicorn.Config(
                    create_app(),
                    host=self.host,
                    port=self.port,
                    log_level="info",
                    access_log=False,
                    log_config=None,  # use root logger -> Logs tab
                )
            )
            self._thread = threading.Thread(target=self._server.run, daemon=True)
            self._thread.start()
            for _ in range(100):
                if self._server.started:
                    break
                time.sleep(0.05)
            needs_token = _needs_substrate_token(_read_token())
            if s.get("launch_edge", True) and (
                needs_token or s.get("auto_refresh", True)
            ):
                try:
                    _launch_debug_edge(self.cdp_port)
                except Exception as exc:
                    print(f"  ! could not launch Edge: {exc}")
            if needs_token:
                threading.Thread(
                    target=_startup_capture_loop, args=(self.cdp_port, 180), daemon=True
                ).start()
            if s.get("auto_refresh", True):
                self._stop_refresh = threading.Event()
                threading.Thread(
                    target=_auto_refresh_loop,
                    args=(self.cdp_port, 900, 60, self._stop_refresh),
                    daemon=True,
                ).start()
            if s.get("configure_clients", True):
                _configure_clients(undo=False, base_url=self.base_url)
            self._running = True
            print("Proxy started.")

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            from .cli import _configure_clients

            print("Stopping proxy ...")
            self._stop_refresh.set()
            if self._server is not None:
                self._server.should_exit = True
            if self._thread is not None:
                self._thread.join(timeout=5)
            if self.settings.get("configure_clients", True):
                _configure_clients(undo=True, base_url=self.base_url)
            self._running = False
            print("Proxy stopped.")

    def reconnect(self) -> None:
        """Stop then start, so edited settings/env take effect without a manual disconnect+connect."""
        print("Reloading proxy connection ...")
        self.stop()
        self.start()

    def token_info(self) -> str:
        from .cli import _is_substrate_token, _read_token, _seconds_remaining

        tok = _read_token()
        if not tok or not _is_substrate_token(tok):
            return "no token"
        sec = _seconds_remaining(tok)
        return f"valid ({sec // 60}m left)" if sec > 0 else "expired"


# --- update ------------------------------------------------------------------------------------


def _version_tuple(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def _current_exe() -> Path | None:
    return Path(sys.executable) if getattr(sys, "frozen", False) else None


def _is_installed() -> bool:
    """True if this build runs from the per-user install dir (Inno installer) rather than as the
    portable single-file. Drives whether updates upgrade in place via the installer or swap the exe."""
    base = os.environ.get("LOCALAPPDATA")
    exe = _current_exe()
    if not base or exe is None:
        return False
    try:
        return exe.resolve().is_relative_to(
            (Path(base) / "Programs" / "M365CopilotProxy").resolve()
        )
    except Exception:
        return False


def _download(url: str, dest: Path) -> bool:
    try:
        with httpx.stream("GET", url, timeout=180, follow_redirects=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return True
    except Exception:
        return False


# Stable "latest release" download URL for the update manifest — no GitHub API / asset-name parsing.
# The manifest carries explicit, named download URLs per build kind:
#   {"version": "0.2.0", "portable": "<url to portable .exe>", "installer": "<url to Setup .exe>"}
_UPDATE_MANIFEST_URL = f"https://github.com/{REPO}/releases/latest/download/update.json"


def check_update() -> tuple[str, str] | None:
    try:
        r = httpx.get(_UPDATE_MANIFEST_URL, timeout=10, follow_redirects=True)
        r.raise_for_status()
        manifest = r.json()
        version = str(manifest.get("version", ""))
        if _version_tuple(version) <= _version_tuple(APP_VERSION):
            return None
        # The running build already knows its kind (install path); pick that key explicitly.
        url = manifest.get("installer" if _is_installed() else "portable")
        if url:
            return version, url
    except Exception:
        return None
    return None


def apply_update(asset_url: str) -> bool:
    """Installed build -> run the Inno installer silently (upgrade in place). Portable build ->
    swap the single .exe on restart. The caller quits the app afterwards in both cases."""
    if _is_installed():
        dest = Path(tempfile.gettempdir()) / "M365CopilotProxy-Setup.exe"
        if not _download(asset_url, dest):
            return False
        try:
            flags = (
                0x00000008 if os.name == "nt" else 0
            )  # DETACHED_PROCESS — survive our quit
            subprocess.Popen(
                [str(dest), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                creationflags=flags,
                close_fds=True,
            )
        except Exception:
            return False
        return True

    # Portable: download the new single-file next to the running exe, then swap-on-exit via a .bat.
    exe = _current_exe()
    if exe is None:
        return False
    new = exe.with_name(exe.stem + ".new.exe")
    if not _download(asset_url, new):
        return False
    bat = exe.with_name(exe.stem + ".update.bat")
    pid = os.getpid()
    bat.write_text(
        "@echo off\r\n:wait\r\n"
        f'tasklist /FI "PID eq {pid}" | find "{pid}" >nul && (timeout /t 1 >nul & goto wait)\r\n'
        f'move /y "{new}" "{exe}" >nul\r\nstart "" "{exe}"\r\ndel "%~f0"\r\n',
        encoding="utf-8",
    )
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x00000008)
    return True


# --- in-GUI log capture ------------------------------------------------------------------------


class _GuiLog:
    """File-like object that funnels writes into the Logs textbox (from any thread)."""

    encoding = "utf-8"

    def __init__(self, app, textbox):
        self.app, self.textbox = app, textbox
        self._buf: list[str] = []
        self._lock = threading.Lock()

    def isatty(self) -> bool:
        return False

    def fileno(self):
        raise OSError("GuiLog has no fileno")

    def writable(self) -> bool:
        return True

    def write(self, s: str):
        if not s:
            return
        with self._lock:
            self._buf.append(s)
        try:
            self.app.after(0, self._flush)
        except Exception:
            pass

    def _flush(self):
        with self._lock:
            data = "".join(self._buf)
            self._buf.clear()
        if not data:
            return
        self.textbox.configure(state="normal")
        self.textbox.insert("end", data)
        self.textbox.see("end")
        self.textbox.configure(state="disabled")

    def flush(self):
        pass


# --- GUI ---------------------------------------------------------------------------------------

_singleton_handle = None


def _acquire_singleton() -> bool:
    """Single-instance guard (cross-platform): False if another tray instance already holds it.
    The handle is kept on a module global so the OS releases it only when this process exits."""
    global _singleton_handle
    if os.name == "nt":
        try:
            import ctypes

            _singleton_handle = ctypes.windll.kernel32.CreateMutexW(
                None, False, "M365CopilotProxy.singleton"
            )
            return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
        except Exception:
            return True
    # POSIX: a non-blocking exclusive flock on a per-user lock file, held for the process lifetime.
    try:
        import fcntl

        lock = Path.home() / ".m365-copilot-openai-proxy" / "tray.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        f = open(lock, "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _singleton_handle = f  # keep the fd open so the lock survives
        return True
    except OSError:
        return False  # already locked by another instance
    except Exception:
        return True


def run_tray() -> None:
    _hide_console()
    if not _acquire_singleton():
        print("M365 Copilot Proxy is already running; exiting this instance.")
        return
    import customtkinter as ctk
    import pystray
    from PIL import Image

    ctk.set_appearance_mode("dark")
    settings = load_settings()
    controller = ProxyController(settings)

    def check_browser_status():
        from .cli import _edge_debug_tabs

        while True:
            try:
                tabs = _edge_debug_tabs(controller.cdp_port)
                controller.browser_running = tabs is not None
            except Exception:
                controller.browser_running = False
            time.sleep(1.5)

    threading.Thread(target=check_browser_status, daemon=True).start()

    app = ctk.CTk()
    app.title("M365 Copilot Proxy")
    app.resizable(False, False)
    app.configure(fg_color=BG)
    try:
        app.iconbitmap(_pkg_file("icon.ico"))
    except Exception:
        pass
    W, H = 430, 650
    sw, sh = app.winfo_screenwidth(), app.winfo_screenheight()
    app.geometry(f"{W}x{H}+{max(0, sw - W - 16)}+{max(0, sh - H - 60)}")

    logo_img = ctk.CTkImage(Image.open(_pkg_file("icon.png")), size=(44, 44))

    # Header
    header = ctk.CTkFrame(app, fg_color="transparent")
    header.pack(pady=(16, 4))
    ctk.CTkLabel(header, image=logo_img, text="").pack(side="left", padx=(0, 10))
    titlebox = ctk.CTkFrame(header, fg_color="transparent")
    titlebox.pack(side="left")
    ctk.CTkLabel(
        titlebox,
        text="M365 Copilot Proxy",
        font=ctk.CTkFont(size=16, weight="bold"),
        text_color=TEXT,
    ).pack(anchor="w")
    ctk.CTkLabel(
        titlebox, text=f"v{APP_VERSION}", font=ctk.CTkFont(size=11), text_color=MUTED
    ).pack(anchor="w")

    tabs = ctk.CTkTabview(
        app,
        fg_color=CARD,
        segmented_button_selected_color=ACCENT,
        segmented_button_selected_hover_color=ACCENT_HOVER,
    )
    tabs.pack(padx=14, pady=(8, 4), fill="both", expand=True)
    t_status = tabs.add("Status")
    t_logs = tabs.add("Logs")
    t_settings = tabs.add("Settings")
    t_passthrough = tabs.add("Passthrough")

    # ---- Status tab ----
    dot = ctk.CTkFrame(t_status, width=16, height=16, corner_radius=8, fg_color=MUTED)
    dot.pack(pady=(26, 8))
    dot.pack_propagate(False)
    status_lbl = ctk.CTkLabel(
        t_status,
        text="Disconnected",
        font=ctk.CTkFont(size=24, weight="bold"),
        text_color=TEXT,
    )
    status_lbl.pack()
    sub_lbl = ctk.CTkLabel(
        t_status,
        text="The proxy is not running",
        font=ctk.CTkFont(size=12),
        text_color=MUTED,
    )
    sub_lbl.pack(pady=(2, 18))

    rows = ctk.CTkFrame(t_status, fg_color="transparent")
    rows.pack(padx=18, fill="x")

    def _row(label: str):
        line = ctk.CTkFrame(rows, fg_color="transparent")
        line.pack(fill="x", pady=3)
        ctk.CTkLabel(
            line, text=label, font=ctk.CTkFont(size=12), text_color=MUTED
        ).pack(side="left")
        val = ctk.CTkLabel(
            line, text="-", font=ctk.CTkFont(size=12, weight="bold"), text_color=TEXT
        )
        val.pack(side="right")
        return val

    endpoint_val = _row("Endpoint")
    token_val = _row("Token")

    hint_lbl = ctk.CTkLabel(
        t_status,
        text="",
        font=ctk.CTkFont(size=11),
        text_color=ACCENT,
        wraplength=330,
        justify="center",
    )
    hint_lbl.pack(padx=18, pady=(14, 0))

    update_btn = ctk.CTkButton(
        t_status,
        text="",
        height=34,
        corner_radius=17,
        font=ctk.CTkFont(size=12, weight="bold"),
        fg_color=GREEN,
        hover_color="#3CB179",
    )
    pending_update: dict[str, str] = {}

    # Reload = reconnect the proxy (apply edited settings). Shown only while connected (in refresh()).
    reload_btn = ctk.CTkButton(
        t_status,
        text="↻ Reload (reconnect)",
        height=34,
        corner_radius=17,
        font=ctk.CTkFont(size=12, weight="bold"),
        fg_color=CARD,
        hover_color="#2A2A33",
    )

    def toggle_browser():
        from .cli import _launch_debug_edge

        browser_btn.configure(state="disabled")
        if controller.browser_running:

            def work():
                try:
                    import asyncio
                    from .cli import _cdp_close_browser

                    asyncio.run(_cdp_close_browser(controller.cdp_port))
                except Exception:
                    pass
                finally:
                    app.after(0, lambda: browser_btn.configure(state="normal"))

            threading.Thread(target=work, daemon=True).start()
        else:

            def work():
                try:
                    _launch_debug_edge(controller.cdp_port)
                except Exception:
                    pass
                finally:
                    app.after(0, lambda: browser_btn.configure(state="normal"))

            threading.Thread(target=work, daemon=True).start()

    browser_btn = ctk.CTkButton(
        t_status,
        text="Show Browser",
        height=34,
        corner_radius=17,
        font=ctk.CTkFont(size=12, weight="bold"),
        fg_color=CARD,
        hover_color="#2A2A33",
        command=toggle_browser,
    )

    toggle_btn = ctk.CTkButton(
        t_status,
        text="Connect",
        height=46,
        corner_radius=23,
        font=ctk.CTkFont(size=15, weight="bold"),
        fg_color=ACCENT,
        hover_color=ACCENT_HOVER,
    )
    toggle_btn.pack(padx=18, pady=(22, 8), fill="x", side="bottom")
    browser_btn.pack(padx=18, pady=(0, 4), fill="x", side="bottom", before=toggle_btn)

    # ---- Logs tab ----
    log_box = ctk.CTkTextbox(
        t_logs,
        fg_color="#101014",
        text_color="#C9C9D0",
        font=ctk.CTkFont(family="Consolas", size=11),
        wrap="word",
    )
    log_box.pack(padx=8, pady=(8, 4), fill="both", expand=True)
    log_box.configure(state="disabled")
    ctk.CTkButton(
        t_logs,
        text="Clear",
        height=28,
        width=80,
        fg_color=CARD,
        hover_color="#2A2A33",
        command=lambda: (
            log_box.configure(state="normal"),
            log_box.delete("1.0", "end"),
            log_box.configure(state="disabled"),
        ),
    ).pack(pady=(0, 8))

    # ---- Settings tab ----
    vars_: dict[str, object] = {}
    sform = ctk.CTkScrollableFrame(t_settings, fg_color="transparent")
    sform.pack(fill="both", expand=True, padx=4, pady=4)

    port_var = ctk.StringVar(value=str(settings["port"]))
    pr = ctk.CTkFrame(sform, fg_color="transparent")
    pr.pack(fill="x", pady=6)
    ctk.CTkLabel(pr, text="Port", font=ctk.CTkFont(size=12), text_color=TEXT).pack(
        side="left"
    )
    ctk.CTkEntry(pr, textvariable=port_var, width=90).pack(side="right")
    vars_["port"] = port_var

    def _switch(parent, key: str, label: str):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=6)
        ctk.CTkLabel(row, text=label, font=ctk.CTkFont(size=12), text_color=TEXT).pack(
            side="left"
        )
        sw = ctk.CTkSwitch(row, text="", progress_color=ACCENT)
        sw.pack(side="right")
        sw.select() if settings.get(key) else sw.deselect()
        vars_[key] = sw

    _switch(sform, "auto_connect", "Auto-connect on launch")
    _switch(sform, "temporary_chat", "Temporary chat (no history)")
    _switch(sform, "work_grounding", "Work grounding (enterprise)")
    _switch(sform, "configure_clients", "Auto-configure Claude Code + VS Code")
    _switch(sform, "launch_edge", "Open debug Edge for token")
    _switch(sform, "auto_refresh", "Auto-refresh token")
    _switch(sform, "persist_default", "Reuse one conversation per chat")
    _switch(sform, "ws_reuse", "Keep WebSocket alive (experimental)")
    _switch(sform, "hide_on_token_success", "Hide browser on token success")

    def reconnect_async():
        if controller.running:
            threading.Thread(target=controller.reconnect, daemon=True).start()

    reload_btn.configure(command=reconnect_async)

    # Manual client wiring (Claude Code global env + VS Code custom model): apply / remove on demand.
    cfg_row = ctk.CTkFrame(sform, fg_color="transparent")
    cfg_row.pack(fill="x", pady=(14, 6))
    ctk.CTkLabel(
        cfg_row, text="Global client config", font=ctk.CTkFont(size=12), text_color=TEXT
    ).pack(side="left")

    def _client_config(undo: bool):
        from .cli import _configure_clients

        def work():
            try:
                _configure_clients(undo=undo, base_url=controller.base_url)
            except Exception as exc:
                print(
                    f"  ! client config {'remove' if undo else 'apply'} failed: {exc}"
                )

        threading.Thread(target=work, daemon=True).start()

    ctk.CTkButton(
        cfg_row,
        text="Remove",
        width=72,
        height=28,
        corner_radius=14,
        fg_color="#2A2A33",
        hover_color=RED,
        command=lambda: _client_config(True),
    ).pack(side="right", padx=(6, 0))
    ctk.CTkButton(
        cfg_row,
        text="Apply",
        width=72,
        height=28,
        corner_radius=14,
        fg_color=ACCENT,
        hover_color=ACCENT_HOVER,
        command=lambda: _client_config(False),
    ).pack(side="right")

    save_hint = ctk.CTkLabel(
        t_settings, text="", font=ctk.CTkFont(size=11), text_color=GREEN
    )
    save_hint.pack()

    def save_clicked():
        try:
            settings["port"] = int(port_var.get())
        except ValueError:
            pass
        for key in (
            "auto_connect",
            "temporary_chat",
            "work_grounding",
            "configure_clients",
            "launch_edge",
            "auto_refresh",
            "persist_default",
            "ws_reuse",
            "hide_on_token_success",
        ):
            settings[key] = bool(vars_[key].get())  # type: ignore[attr-defined]
        save_settings(settings)
        if controller.running:
            save_hint.configure(text="Saved — reconnecting…")
            reconnect_async()
        else:
            save_hint.configure(text="Saved.")
        app.after(2500, lambda: save_hint.configure(text=""))

    ctk.CTkButton(
        t_settings,
        text="Save",
        height=36,
        corner_radius=18,
        fg_color=ACCENT,
        hover_color=ACCENT_HOVER,
        command=save_clicked,
    ).pack(padx=8, pady=8, fill="x", side="bottom")

    # ---- Passthrough tab ----
    pform = ctk.CTkFrame(t_passthrough, fg_color="transparent")
    pform.pack(fill="both", expand=True, padx=10, pady=10)
    ctk.CTkLabel(
        pform,
        text="Models that aren't ours (non m365-*) are forwarded straight to the real Anthropic\n"
        "API. Default credential = your Claude Code login (free, uses your subscription).",
        font=ctk.CTkFont(size=11),
        text_color=MUTED,
        justify="left",
        wraplength=360,
    ).pack(anchor="w", pady=(0, 10))
    _switch(pform, "passthrough_claude", "Forward non-m365 models to Claude")

    # The Claude Code login file is the single source of truth for the passthrough token.
    _creds_file = Path.home() / ".claude" / ".credentials.json"

    def _read_creds() -> dict:
        try:
            return json.loads(_creds_file.read_text("utf-8"))
        except Exception:
            return {}

    def _oauth_status() -> str:
        oauth = _read_creds().get("claudeAiOauth", {})
        if not oauth.get("accessToken"):
            return "Claude Code OAuth: no token in ~/.claude/.credentials.json"
        exp = int(oauth.get("expiresAt") or 0)
        if exp:
            left = int((exp / 1000 - time.time()) / 60)
            return (
                f"Claude Code OAuth: valid ({left}m left)"
                if left > 0
                else "Claude Code OAuth: expired (auto-refresh on use)"
            )
        return "Claude Code OAuth: valid"

    cred_hint = ctk.CTkLabel(
        pform,
        text=_oauth_status(),
        font=ctk.CTkFont(size=11),
        text_color=GREEN,
        justify="left",
        wraplength=360,
    )
    cred_hint.pack(anchor="w", pady=(8, 4))

    # Single token field: shows the token from the JSON; editing + Apply overwrites the JSON.
    hdr = ctk.CTkFrame(pform, fg_color="transparent")
    hdr.pack(fill="x", pady=(12, 2))
    ctk.CTkLabel(
        hdr,
        text="Token (source: ~/.claude/.credentials.json)",
        font=ctk.CTkFont(size=12),
        text_color=TEXT,
    ).pack(side="left")
    _reveal = [False]

    def _toggle_reveal():
        _reveal[0] = not _reveal[0]
        tok_entry.configure(show="" if _reveal[0] else "•")
        reveal_btn.configure(text="Hide" if _reveal[0] else "Show")

    def _copy_token():
        t = (tok_var.get() or "").strip()
        if t:
            app.clipboard_clear()
            app.clipboard_append(t)

    reveal_btn = ctk.CTkButton(
        hdr,
        text="Show",
        width=58,
        height=24,
        corner_radius=12,
        fg_color=CARD,
        hover_color="#2A2A33",
        command=_toggle_reveal,
    )
    reveal_btn.pack(side="right")
    ctk.CTkButton(
        hdr,
        text="Copy",
        width=58,
        height=24,
        corner_radius=12,
        fg_color=CARD,
        hover_color="#2A2A33",
        command=_copy_token,
    ).pack(side="right", padx=(0, 6))
    tok_var = ctk.StringVar(
        value=(_read_creds().get("claudeAiOauth", {}).get("accessToken", "") or "")
    )
    tok_entry = ctk.CTkEntry(pform, textvariable=tok_var, show="•")
    tok_entry.pack(fill="x")

    pt_hint = ctk.CTkLabel(
        pform,
        text="",
        font=ctk.CTkFont(size=11),
        text_color=GREEN,
        justify="left",
        wraplength=360,
    )
    pt_hint.pack(anchor="w", pady=(6, 0))

    def save_passthrough():
        settings["passthrough_claude"] = bool(vars_["passthrough_claude"].get())  # type: ignore[attr-defined]
        save_settings(settings)
        # Write the (possibly edited) token back to the JSON — the source of truth — preserving siblings.
        new_tok = (tok_var.get() or "").strip()
        raw = _read_creds()
        cur = (raw.get("claudeAiOauth") or {}).get("accessToken", "")
        msg = "Saved."
        if new_tok and new_tok != cur:
            oauth = raw.get("claudeAiOauth") or {}
            oauth["accessToken"] = new_tok
            raw["claudeAiOauth"] = oauth
            try:
                _creds_file.write_text(json.dumps(raw), "utf-8")
                msg = "✓ token written to ~/.claude/.credentials.json"
            except Exception as exc:
                msg = f"! could not write token: {exc}"
        if controller.running:
            reconnect_async()
        cred_hint.configure(text=_oauth_status())
        pt_hint.configure(text=msg)
        app.after(3000, lambda: pt_hint.configure(text=""))

    ctk.CTkButton(
        t_passthrough,
        text="Apply",
        height=36,
        corner_radius=18,
        fg_color=ACCENT,
        hover_color=ACCENT_HOVER,
        command=save_passthrough,
    ).pack(padx=8, pady=8, fill="x", side="bottom")

    footer = ctk.CTkFrame(app, fg_color="transparent")
    footer.pack(side="bottom", fill="x", padx=16, pady=(0, 10))
    ctk.CTkLabel(
        footer, text="X = hide to tray", font=ctk.CTkFont(size=10), text_color=MUTED
    ).pack(side="left")
    quit_button = ctk.CTkButton(
        footer,
        text="Quit",
        width=76,
        height=28,
        corner_radius=14,
        fg_color="#2A2A33",
        hover_color=RED,
        text_color=TEXT,
        font=ctk.CTkFont(size=12, weight="bold"),
    )
    quit_button.pack(side="right")

    # --- behaviour ---
    _tray_state: list[object] = [
        None
    ]  # last status pushed to the tray badge (avoid redundant redraws)

    def refresh():
        on = controller.running
        dot.configure(fg_color=GREEN if on else MUTED)
        status_lbl.configure(text="Connected" if on else "Disconnected")
        sub_lbl.configure(
            text=controller.base_url if on else "The proxy is not running"
        )
        endpoint_val.configure(text=controller.base_url)
        token_val.configure(text=controller.token_info() if on else "-")
        hint_lbl.configure(
            text="↻ Reload VS Code (Ctrl+Shift+P → Reload Window) to load the M365 model"
            if on and settings.get("configure_clients", True)
            else ""
        )
        if browser_btn.cget("state") == "normal":
            browser_btn.configure(
                text="Hide Browser" if controller.browser_running else "Show Browser"
            )
        if on:
            reload_btn.pack(
                padx=18, pady=(0, 4), fill="x", side="bottom", before=browser_btn
            )
        else:
            reload_btn.pack_forget()
        toggle_btn.configure(
            text="Disconnect" if on else "Connect",
            fg_color=RED if on else ACCENT,
            hover_color=RED_HOVER if on else ACCENT_HOVER,
        )
        try:
            icon.title = f"M365 Copilot Proxy - {'Connected' if on else 'Disconnected'}"
            if _tray_state[0] != on:
                _tray_state[0] = on
                icon.icon = _tray_image(on)  # green/red status badge on the tray icon
        except Exception:
            pass
        try:  # keep the passthrough OAuth status live
            cred_hint.configure(text=_oauth_status())
        except Exception:
            pass
        app.after(1500, refresh)

    def toggle():
        toggle_btn.configure(
            state="disabled",
            text="Disconnecting..." if controller.running else "Connecting...",
        )

        def work():
            try:
                controller.stop() if controller.running else controller.start()
            finally:
                app.after(0, lambda: toggle_btn.configure(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    toggle_btn.configure(command=toggle)

    def do_update():
        url = pending_update.get("url")
        if not url:
            return
        update_btn.configure(state="disabled", text="Downloading update...")

        def work():
            ok = apply_update(url)
            app.after(
                0,
                quit_app
                if ok
                else lambda: update_btn.configure(
                    state="normal", text="Update failed - retry"
                ),
            )

        threading.Thread(target=work, daemon=True).start()

    def show_update(tag: str, url: str):
        pending_update.update({"tag": tag, "url": url})
        update_btn.configure(text=f"Update available: {tag}", command=do_update)
        update_btn.pack(
            padx=18, pady=(0, 4), fill="x", side="bottom", before=toggle_btn
        )
        win = ctk.CTkToplevel(app)
        win.title("Update available")
        win.geometry("320x180")
        win.configure(fg_color=BG)
        win.transient(app)
        win.grab_set()
        ctk.CTkLabel(
            win,
            text="Update available",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=TEXT,
        ).pack(pady=(22, 4))
        ctk.CTkLabel(
            win,
            text=f"{tag} is available (you have v{APP_VERSION}).",
            font=ctk.CTkFont(size=12),
            text_color=MUTED,
        ).pack()
        b = ctk.CTkFrame(win, fg_color="transparent")
        b.pack(pady=20)
        ctk.CTkButton(
            b,
            text="Later",
            width=110,
            fg_color=CARD,
            hover_color="#2A2A33",
            command=win.destroy,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            b,
            text="Update & restart",
            width=150,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=lambda: (win.destroy(), do_update()),
        ).pack(side="left", padx=6)

    # --- redirect stdout/logging into the Logs tab (windowed build has no console) ---
    gui_log = _GuiLog(app, log_box)
    sys.stdout = gui_log  # type: ignore[assignment]
    sys.stderr = gui_log  # type: ignore[assignment]
    _h = logging.StreamHandler(gui_log)
    _h.setFormatter(logging.Formatter("%(levelname)s  %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(_h)
    root_logger.setLevel(logging.INFO)

    # --- tray ---
    tray_image = _tray_image(controller.running)

    def quit_app(*_):
        try:
            controller.stop()
        finally:
            try:
                icon.stop()
            except Exception:
                pass
            app.after(0, app.destroy)

    quit_button.configure(command=quit_app)

    menu = pystray.Menu(
        pystray.MenuItem(
            "Open",
            lambda i, it: app.after(0, lambda: (app.deiconify(), app.lift())),
            default=True,
        ),
        pystray.MenuItem(
            lambda it: "Disconnect" if controller.running else "Connect",
            lambda i, it: app.after(0, toggle),
        ),
        pystray.MenuItem("Quit", lambda i, it: app.after(0, quit_app)),
    )
    icon = pystray.Icon("m365-copilot-proxy", tray_image, "M365 Copilot Proxy", menu)
    threading.Thread(target=icon.run, daemon=True).start()

    app.protocol("WM_DELETE_WINDOW", app.withdraw)

    if settings.get("auto_connect", True):
        threading.Thread(target=controller.start, daemon=True).start()

    def update_check_async():
        res = check_update()
        if res:
            app.after(0, lambda: show_update(res[0], res[1]))

    threading.Thread(target=update_check_async, daemon=True).start()

    refresh()
    # Best-effort cleanup (undo client wiring + stop the server) on ANY interpreter exit —
    # normal quit, unhandled exception, or sys.exit. Cannot catch SIGKILL / taskkill /F.
    import atexit

    def _cleanup_on_exit():
        try:
            if controller.running:
                controller.stop()
        except Exception:
            pass

    atexit.register(_cleanup_on_exit)
    try:
        app.mainloop()
    except KeyboardInterrupt:
        quit_app()
    except BaseException:
        _cleanup_on_exit()  # crash in the GUI loop: still undo before propagating
        raise
