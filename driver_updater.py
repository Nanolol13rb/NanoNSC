# -*- coding: utf-8 -*-
"""
System Updater & Cleaner  -  Version 2.0
==========================================

Desktop-Systemwerkzeug mit den Bereichen Treiber, Dateien, Autostart,
Speicheranalyse, Systemmonitor, Netzwerkdiagnose, Übersicht und
Wiederherstellung. Treiber-Updates stammen aus dem Microsoft Update Catalog;
Dateibereinigung und Autostart-Verwaltung erfordern eine bewusste Auswahl.

Nur Standardbibliothek. Start:  python driver_updater.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import html
import json
import ntpath
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import socket
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import maintenance
import usage_tracker

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows import support
    winreg = None

try:
    import winsound
except ImportError:  # pragma: no cover - non-Windows import support
    winsound = None

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

APP_NAME = "System Updater & Cleaner"
APP_VERSION = "2.0"
APP_DIR = Path(os.environ.get("APPDATA") or str(Path.home())) / "SystemUpdaterCleaner"
CONFIG_FILE = APP_DIR / "config.json"
HISTORY_FILE = APP_DIR / "maintenance-history.json"
USAGE_FILE = APP_DIR / "app-usage.json"
USAGE_CONTROL_FILE = APP_DIR / "app-usage-control.json"
USAGE_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
USAGE_AUTOSTART_NAME = "Nano System Control App-Nutzung"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

IS_WINDOWS = os.name == "nt"
NO_CONSOLE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)


def _write_usage_command(command: str) -> None:
    if command not in ("start", "pause", "reset"):
        raise ValueError("Ungültiger Befehl für die Nutzungsaufzeichnung.")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(APP_DIR),
                prefix="app-usage-command-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"command": command}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(USAGE_CONTROL_FILE)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _set_usage_autostart(enabled: bool) -> None:
    if winreg is None:
        raise RuntimeError("Hintergrundstart ist nur unter Windows verfügbar.")
    with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, USAGE_AUTOSTART_KEY, 0,
            winreg.KEY_SET_VALUE) as key:
        if enabled:
            if getattr(sys, "frozen", False):
                command = '"%s" --background-tracker' % sys.executable
            else:
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                executable = str(pythonw if pythonw.is_file() else Path(sys.executable))
                command = '"%s" "%s" --background-tracker' % (
                    executable, Path(__file__).resolve())
            winreg.SetValueEx(key, USAGE_AUTOSTART_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, USAGE_AUTOSTART_NAME)
            except FileNotFoundError:
                pass


def _launch_background_usage_tracker() -> None:
    if not IS_WINDOWS:
        raise RuntimeError("Hintergrundaufzeichnung wird nur unter Windows unterstützt.")
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--background-tracker"]
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        executable = str(pythonw if pythonw.is_file() else Path(sys.executable))
        command = [executable, str(Path(__file__).resolve()), "--background-tracker"]
    subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True,
        creationflags=DETACHED_PROCESS | NO_CONSOLE_WINDOW,
        cwd=str(Path(__file__).resolve().parent),
    )


def _apply_usage_command(tracker: usage_tracker.UsageTracker) -> bool:
    if not USAGE_CONTROL_FILE.exists():
        return False
    try:
        payload = json.loads(USAGE_CONTROL_FILE.read_text(encoding="utf-8"))
    finally:
        USAGE_CONTROL_FILE.unlink(missing_ok=True)
    if not isinstance(payload, dict):
        raise ValueError("Befehl für die Nutzungsaufzeichnung ist ungültig.")
    command = payload.get("command")
    if command == "start":
        tracker.start()
    elif command == "pause":
        tracker.pause()
    elif command == "reset":
        tracker.reset()
    else:
        raise ValueError("Unbekannter Hintergrund-Aufzeichnungsbefehl.")
    return True


def _run_background_usage_tracker() -> int:
    if not IS_WINDOWS:
        return 1
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    mutex = kernel32.CreateMutexW(None, False, "Local\\NanoSystemControlUsageTracker")
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(mutex)
        return 0

    tracker: Optional[usage_tracker.UsageTracker] = None
    idle_since: Optional[float] = None
    try:
        tracker = usage_tracker.UsageTracker(USAGE_FILE)
        if tracker.enabled:
            tracker.start()
        while True:
            if _apply_usage_command(tracker) and tracker.enabled:
                idle_since = None
            if tracker.enabled:
                idle_since = None
            else:
                if idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= 10:
                    return 0
            time.sleep(1)
    finally:
        if tracker is not None and tracker.enabled:
            tracker.shutdown()
        kernel32.CloseHandle(mutex)


# ---------------------------------------------------------------------------
# Farbpalette
# ---------------------------------------------------------------------------
class T:
    bg = "#0e1422"
    card = "#151e2e"
    card_hi = "#1d2a3d"
    line = "#304056"
    text = "#f2f5fa"
    dim = "#bbc6d5"
    faint = "#91a0b4"
    accent = "#67d8c2"
    accent_hover = "#86e8d4"
    card_hover = "#253650"
    accent_soft = "#163b38"
    accent_2 = "#b5a7ff"
    ok = "#61d6a4"
    warn = "#f0c46e"
    err = "#f18491"
    danger = "#713a4a"
    danger_text = "#ffe9ec"
    danger_border = "#985264"
    sel = "#244653"
    stripe = "#192436"


DARK_PALETTE = {
    "bg": "#0e1422", "card": "#151e2e", "card_hi": "#1d2a3d", "line": "#304056",
    "text": "#f2f5fa", "dim": "#bbc6d5", "faint": "#91a0b4", "accent": "#67d8c2",
    "accent_soft": "#163b38", "accent_2": "#b5a7ff", "ok": "#61d6a4",
    "warn": "#f0c46e", "err": "#f18491", "danger": "#713a4a",
    "danger_text": "#ffe9ec", "danger_border": "#985264",
    "sel": "#244653", "stripe": "#192436", "accent_hover": "#86e8d4",
    "card_hover": "#253650",
}
LIGHT_PALETTE = {
    "bg": "#f3f6fa", "card": "#ffffff", "card_hi": "#e8eef5", "line": "#cbd5e1",
    "text": "#182334", "dim": "#45566d", "faint": "#65758a", "accent": "#087f72",
    "accent_soft": "#d8f1ec", "accent_2": "#6554b8", "ok": "#08774d",
    "warn": "#8b5b00", "err": "#b3263e", "danger": "#fde7ea",
    "danger_text": "#8e1930", "danger_border": "#d99aa5",
    "sel": "#d2e8f0", "stripe": "#f0f4f8", "accent_hover": "#b5e5dc",
    "card_hover": "#d8e1eb",
}
THEME_PALETTES = {
    "dark": DARK_PALETTE,
    "light": LIGHT_PALETTE,
    "violet": {
        **DARK_PALETTE, "bg": "#151020", "card": "#211831", "card_hi": "#302243",
        "line": "#49335f", "accent": "#c19aff", "accent_soft": "#38264f",
        "accent_2": "#ff9ed2", "ok": "#87dfbd", "sel": "#48335f",
        "stripe": "#281d38", "accent_hover": "#d4b8ff", "card_hover": "#3b2b53",
    },
    "rose": {
        **DARK_PALETTE, "bg": "#1b1018", "card": "#281721", "card_hi": "#3a222f",
        "line": "#563342", "accent": "#ff8cab", "accent_soft": "#482332",
        "accent_2": "#ffc078", "ok": "#8fe0b4", "sel": "#563044",
        "stripe": "#301c28", "accent_hover": "#ffa9bf", "card_hover": "#482b3a",
    },
    "sunset": {
        **DARK_PALETTE, "bg": "#20130f", "card": "#2d1b15", "card_hi": "#42271c",
        "line": "#62402a", "accent": "#ff9b54", "accent_soft": "#4d2d1d",
        "accent_2": "#ffd166", "ok": "#9edb91", "sel": "#59361f",
        "stripe": "#352018", "accent_hover": "#ffb276", "card_hover": "#523221",
    },
    "amber": {
        **DARK_PALETTE, "bg": "#1c190e", "card": "#292414", "card_hi": "#39321b",
        "line": "#574a29", "accent": "#e9c46a", "accent_soft": "#463a1b",
        "accent_2": "#f4a261", "ok": "#a6d189", "sel": "#514323",
        "stripe": "#302a17", "accent_hover": "#f3d58b", "card_hover": "#494027",
    },
    "forest": {
        **DARK_PALETTE, "bg": "#101a15", "card": "#17251c", "card_hi": "#223529",
        "line": "#344d3b", "accent": "#8ecf78", "accent_soft": "#243d27",
        "accent_2": "#d1b37c", "ok": "#9ad68a", "sel": "#304b34",
        "stripe": "#1c2c21", "accent_hover": "#a8df95", "card_hover": "#2b4131",
    },
    "ocean": {
        **DARK_PALETTE, "bg": "#0c1720", "card": "#13232e", "card_hi": "#1d3442",
        "line": "#2e4b5c", "accent": "#55c7e8", "accent_soft": "#153949",
        "accent_2": "#8eb8ff", "ok": "#78d8bb", "sel": "#1f4354",
        "stripe": "#172a36", "accent_hover": "#80d7ee", "card_hover": "#294352",
    },
    "crimson": {
        **DARK_PALETTE, "bg": "#1c1012", "card": "#291719", "card_hi": "#3a2023",
        "line": "#5a3034", "accent": "#ff6b6b", "accent_soft": "#4a2025",
        "accent_2": "#ffad66", "ok": "#91d39b", "sel": "#54272d",
        "stripe": "#301a1d", "accent_hover": "#ff9292", "card_hover": "#48292c",
    },
    "mono": {
        **DARK_PALETTE, "bg": "#141414", "card": "#202020", "card_hi": "#2d2d2d",
        "line": "#454545", "accent": "#e1e1e1", "accent_soft": "#353535",
        "accent_2": "#b8b8b8", "ok": "#b2d8b2", "sel": "#414141",
        "stripe": "#262626", "accent_hover": "#ffffff", "card_hover": "#393939",
    },
}
THEME_LABELS = {
    "dark": "🌙 Dunkel",
    "light": "☀️ Hell",
    "violet": "💜 Violett",
    "rose": "🌸 Rosa",
    "sunset": "🌅 Sonnenuntergang",
    "amber": "🟡 Bernstein",
    "forest": "🌲 Wald",
    "ocean": "🌊 Ozean",
    "crimson": "🔴 Rubinrot",
    "mono": "⚫ Monochrom",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LEVELS = {"info": T.dim, "ok": T.ok, "warn": T.warn, "err": T.err, "cmd": T.accent_2}


class Logger:
    """Thread-sicherer Logger, der in die Konsole der GUI schreibt."""

    def __init__(self) -> None:
        self._cb: Optional[Callable[[str, str], None]] = None
        self._lock = threading.Lock()
        self.lines: List[Tuple[str, str]] = []

    def bind(self, cb: Optional[Callable[[str, str], None]]) -> None:
        self._cb = cb

    def _emit(self, level: str, msg: str) -> None:
        with self._lock:
            self.lines.append((level, msg))
            if len(self.lines) > 2000:
                del self.lines[:-2000]
        if self._cb:
            try:
                self._cb(level, msg)
            except Exception:
                pass

    def info(self, m: str) -> None:
        self._emit("info", m)

    def ok(self, m: str) -> None:
        self._emit("ok", m)

    def warn(self, m: str) -> None:
        self._emit("warn", m)

    def err(self, m: str) -> None:
        self._emit("err", m)

    def cmd(self, m: str) -> None:
        self._emit("cmd", m)


LOG = Logger()


# ---------------------------------------------------------------------------
# System-Hilfsfunktionen
# ---------------------------------------------------------------------------
def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def os_release(build: int) -> str:
    """Windows-Build -> Feature-Release (fuer die Katalog-Suche)."""
    if build >= 26200:
        return "25H2"
    if build >= 26100:
        return "24H2"
    if build >= 22631:
        return "23H2"
    if build >= 22621:
        return "22H2"
    if build >= 22000:
        return "21H2"
    return "21H2"


def run_ps(script: str, timeout: int = 90) -> Tuple[int, str, str]:
    """Fuehrt ein PowerShell-Skript aus und liefert (code, stdout, stderr)."""
    wrapped = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$ErrorActionPreference='Continue'; " + script
    )
    try:
        p = subprocess.run(
            ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", wrapped],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=NO_CONSOLE_WINDOW,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "Zeitueberschreitung"
    except Exception as exc:  # pragma: no cover
        return 1, "", str(exc)


# --- Elevation -------------------------------------------------------------
class _SHELLEXECUTEINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", wintypes.LPVOID),
        ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_ERROR_CANCELLED = 1223
_INFINITE = 0xFFFFFFFF


def run_elevated(ps_path: Path, timeout: int = 1800) -> int:
    """Startet ein .ps1 elevated (UAC) und wartet. -1 = vom Benutzer abgebrochen."""
    if not IS_WINDOWS:
        return 1
    info = _SHELLEXECUTEINFO()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = (
        "-NoProfile -ExecutionPolicy Bypass -File \"%s\"" % str(ps_path)
    )
    info.nShow = 0
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFO)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    ok = shell32.ShellExecuteExW(ctypes.byref(info))
    if not ok:
        err = ctypes.get_last_error()
        if err == _ERROR_CANCELLED:
            return -1
        LOG.err("Erhoehte Installation konnte nicht gestartet werden: %s" %
                ctypes.FormatError(err).strip())
        return 1
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    wait_ms = max(1, min(timeout, 0xFFFFFFFE // 1000)) * 1000
    wait_result = kernel32.WaitForSingleObject(info.hProcess, wait_ms)
    if wait_result == 0x00000102:  # WAIT_TIMEOUT: never interrupt an active driver install
        LOG.warn("Treiberinstallation dauert laenger als %d Sekunden; warte weiter." % timeout)
        wait_result = kernel32.WaitForSingleObject(info.hProcess, _INFINITE)
    if wait_result == 0xFFFFFFFF:  # WAIT_FAILED
        err = ctypes.get_last_error()
        kernel32.CloseHandle(info.hProcess)
        raise OSError(err, "Auf den Installationsprozess konnte nicht gewartet werden.")
    code = wintypes.DWORD()
    if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
        err = ctypes.get_last_error()
        kernel32.CloseHandle(info.hProcess)
        raise OSError(err, "Exit-Code des Installationsprozesses konnte nicht gelesen werden.")
    kernel32.CloseHandle(info.hProcess)
    return int(code.value)


# --- Papierkorb ------------------------------------------------------------
_FOF_ALLOWUNDO = 0x0040
_FOF_NOCONFIRMATION = 0x0010
_FOF_SILENT = 0x0004
_FOF_NOERRORUI = 0x0400
_FOF_NOCONFIRMMKDIR = 0x0200


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_int), ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID), ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def send_to_recycle(paths: Sequence[str]) -> Tuple[int, List[str]]:
    """Verschiebt Dateien in den Papierkorb. Gibt (Anzahl, Fehler) zurueck."""
    if not paths:
        return 0, []
    if any("\0" in str(p) for p in paths):
        raise ValueError("Dateipfade duerfen kein Nullzeichen enthalten.")
    original = [str(p) for p in paths]
    buf = "\0".join(original) + "\0\0"
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 0x0003  # FO_DELETE
    op.pFrom = buf
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI | _FOF_NOCONFIRMMKDIR
    op.lpszProgressTitle = "In den Papierkorb verschieben"
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    failed = [path for path in original if os.path.lexists(path)]
    if rc != 0 and not failed:
        failed = original
    return len(original) - len(failed), failed


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def http_get(url: str, data: Optional[bytes] = None, timeout: int = 60,
             cancel_event: Optional[threading.Event] = None) -> bytes:
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept-Language", "en-US,en;q=0.9")
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Treiberprüfung wurde abgebrochen.")
        if cancel_event:
            chunks = []
            while True:
                if cancel_event.is_set():
                    raise OperationCancelled("Treiberprüfung wurde abgebrochen.")
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
        else:
            raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return raw


def http_download(url: str, dest: Path, timeout: int = 300,
                  cancel_event: Optional[threading.Event] = None) -> None:
    """Streamt einen Download in eine temporaere Datei und ersetzt das Ziel atomar."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept-Language", "en-US,en;q=0.9")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Treiberprüfung wurde abgebrochen.")
        if urllib.parse.urlparse(response.geturl()).scheme.lower() != "https":
            raise ValueError("Der Download wurde auf eine unsichere HTTP-Adresse umgeleitet.")
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=str(dest.parent), prefix=dest.name + ".", suffix=".part",
                delete=False) as tmp:
            temporary = Path(tmp.name)
            try:
                if cancel_event:
                    while True:
                        if cancel_event.is_set():
                            raise OperationCancelled("Treiberprüfung wurde abgebrochen.")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        tmp.write(chunk)
                else:
                    shutil.copyfileobj(response, tmp, length=1024 * 1024)
                tmp.flush()
                os.fsync(tmp.fileno())
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
    try:
        temporary.replace(dest)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Microsoft Update Catalog
# ---------------------------------------------------------------------------
@dataclass
class CatalogEntry:
    update_id: str
    title: str
    version: str
    date: str
    product: str
    classification: str


_GUID_RE = re.compile(
    r"goToDetails\(\s*(?:'|&#39;|&apos;|\")\s*"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*(?:'|&#39;|&apos;|\")\s*\)"
)
_FILE_RE = re.compile(r"\.files\[(\d+)\]\.fileName\s*=\s*'([^']+)'")
_URL_RE = re.compile(r"\.files\[(\d+)\]\.url\s*=\s*'(https://[^']+)'")
_UPDATE_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class CatalogClient:
    BASE = "https://www.catalog.update.microsoft.com"

    def search(self, query: str,
               cancel_event: Optional[threading.Event] = None) -> List[CatalogEntry]:
        url = "%s/Search.aspx?q=%s&sortOrder=datePublished" % (self.BASE, urllib.parse.quote(query))
        page = http_get(url, timeout=15, cancel_event=cancel_event).decode("utf-8", "replace")
        if "We did not find any results" in page:
            return []
        found: Dict[str, CatalogEntry] = {}
        for row in re.split(r"(?i)<tr\b", page):
            m = _GUID_RE.search(row)
            if not m:
                continue
            uid = m.group(1)
            if uid in found:
                continue
            entry = self._parse_row(row)
            if entry:
                entry.update_id = uid
                found[uid] = entry
        return list(found.values())

    @staticmethod
    def _parse_row(row: str) -> Optional[CatalogEntry]:
        """Wertet eine Tabellenzeile des Katalogs aus.

        Spalten: 1 Titel, 2 Produkt, 3 Klassifikation, 4 Datum, 5 Version.
        """
        cells = re.findall(r"(?is)<td\b[^>]*>(.*?)</td>", row)
        if len(cells) < 4:
            return None

        def text_of(cell: str) -> str:
            t = re.sub(r"(?is)<[^>]+>", " ", cell)
            return re.sub(r"\s+", " ", html.unescape(t)).strip()

        title_raw = text_of(cells[1])
        if not title_raw:
            return None
        product = text_of(cells[2]) if len(cells) > 2 else ""
        classification = text_of(cells[3]) if len(cells) > 3 else ""
        date = text_of(cells[4]) if len(cells) > 4 else ""
        version = text_of(cells[5]) if len(cells) > 5 else ""

        m = re.match(r"^(?P<name>.*?)\s*Driver Update\s*\((?P<ver>[^()]+)\)\s*$", title_raw, re.I)
        if m:
            title = m.group("name").strip()
            version = version or m.group("ver").strip()
        else:
            title = title_raw
        if not title:
            return None
        version = re.sub(r"^version\s+", "", version.strip(), flags=re.I)
        if not version or version.lower() in ("n/a", "na", "-"):
            # aeltere Katalogeintraege nennen die Version nur im Titel
            found = re.findall(r"\d+(?:\.\d+){1,4}", title)
            version = found[-1] if found else ""
        return CatalogEntry("", title, version, date, product, classification)

    def resolve_files(self, update_id: str,
                      cancel_event: Optional[threading.Event] = None) -> List[Tuple[str, str]]:
        if not _UPDATE_ID_RE.fullmatch(update_id):
            raise ValueError("Ungueltige Update-ID.")
        ids = '[{"size":0,"languages":"","uidInfo":"%s","updateID":"%s"}]' % (update_id, update_id)
        body = urllib.parse.urlencode({
            "updateIDs": ids, "sku": "null", "isEnterprise": "false", "serverSelection": "2",
            "downloadMethod": "2", "includeAllLanguages": "true", "errorCount": "0",
            "cp39": "true", "servicePackID": "1", "uniqueProductId": "null",
        }).encode()
        page = http_get(
            "%s/DownloadDialog.aspx" % self.BASE, data=body, timeout=20,
            cancel_event=cancel_event,
        ).decode("utf-8", "replace")
        names: Dict[int, str] = {}
        urls: Dict[int, str] = {}
        for m in _FILE_RE.finditer(page):
            names[int(m.group(1))] = html.unescape(m.group(2))
        for m in _URL_RE.finditer(page):
            urls[int(m.group(1))] = html.unescape(m.group(2))
        return [(names[i], urls[i]) for i in sorted(urls) if i in names]

    def download(self, update_id: str, dest: Path,
                 cancel_event: Optional[threading.Event] = None) -> List[Path]:
        dest.mkdir(parents=True, exist_ok=True)
        out: List[Path] = []
        for name, url in self.resolve_files(update_id, cancel_event=cancel_event):
            if cancel_event and cancel_event.is_set():
                raise OperationCancelled("Treiberprüfung wurde abgebrochen.")
            filename = ntpath.basename(name.replace("/", "\\"))
            if not filename or filename in (".", "..") or "\0" in filename:
                raise ValueError("Ungueltiger Paketdateiname im Katalog.")
            target = dest / filename
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme.lower() != "https" or not parsed.hostname:
                raise ValueError("Der Katalog lieferte eine unsichere Download-Adresse.")
            if not target.is_file() or target.stat().st_size == 0:
                http_download(url, target, timeout=30, cancel_event=cancel_event)
            out.append(target)
        return out


# ---------------------------------------------------------------------------
# Installierte Treiber
# ---------------------------------------------------------------------------
@dataclass
class InstalledDriver:
    name: str
    device_id: str
    inf: str
    version: str
    date: str
    manufacturer: str
    device_class: str
    hardware_ids: List[str] = field(default_factory=list)
    problem: int = 0

    @property
    def is_vendor(self) -> bool:
        """Echter Herstellertreiber (nicht Windows-Inbox / Software)."""
        if not self.name:
            return False
        cls = (self.device_class or "").strip().lower()
        if cls in ("softwarecomponent", "unknown", ""):
            return False
        if self.problem == 45:  # Phantomgeraet
            return False
        mfr = (self.manufacturer or "").strip()
        if not mfr:
            return False
        norm = mfr.lower().strip("()").strip()
        for junk in ("microsoft", "standard", "unknown", "compatibility publisher",
                     "ibm", "usb input device", "firmware", "system devices"):
            if norm.startswith(junk):
                return False
        if self.name.lower().startswith(("generic ", "standard ", "microsoft ")):
            return False
        if norm.startswith("generic "):
            return False
        return True


_SCAN_PS = r"""
$ErrorActionPreference='Stop'
try {
  $hw=@{}
  Get-CimInstance Win32_PnPEntity | ForEach-Object { $hw[$_.DeviceID] = @($_.HardwareID) }
  $out = [System.Collections.Generic.List[object]]::new()
  Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceName } | ForEach-Object {
    $ids = @($hw[$_.DeviceID])
    $out.Add([pscustomobject]@{
      Name=$_.DeviceName; Id=$_.DeviceID; Inf=$_.InfName; Ver=$_.DriverVersion
      Date=$(if($_.DriverDate){$_.DriverDate.ToString('yyyy-MM-dd')}else{''})
      Mfr=$_.Manufacturer; Cls=$_.DeviceClass; Hw=$ids; Prob=$_.ConfigManagerErrorCode
    })
  }
  ConvertTo-Json -InputObject @($out.ToArray()) -Compress -Depth 4
} catch {
  [Console]::Error.WriteLine($_.Exception.Message)
  exit 1
}
"""


def scan_installed() -> List[InstalledDriver]:
    code, out, err = run_ps(_SCAN_PS, timeout=180)
    if code != 0:
        raise RuntimeError(err.strip() or "PowerShell konnte die Treiber nicht abfragen.")
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Die Treiberabfrage lieferte ungueltige Daten.") from exc
    if isinstance(data, dict):
        data = [data]
    result: List[InstalledDriver] = []
    for d in data:
        hw = d.get("Hw") or []
        if isinstance(hw, str):
            hw = [hw]
        result.append(InstalledDriver(
            name=d.get("Name") or "", device_id=d.get("Id") or "", inf=d.get("Inf") or "",
            version=d.get("Ver") or "", date=d.get("Date") or "",
            manufacturer=d.get("Mfr") or "", device_class=d.get("Cls") or "",
            hardware_ids=[str(x) for x in hw if x], problem=int(d.get("Prob") or 0),
        ))
    return result


def os_build() -> int:
    if IS_WINDOWS:
        try:
            return int(sys.getwindowsversion().build)
        except (AttributeError, OSError) as exc:
            LOG.warn("Windows-Build konnte nicht direkt gelesen werden: %s" % exc)
    code, out, _ = run_ps("[System.Environment]::OSVersion.Version.Build", timeout=30)
    try:
        if code != 0:
            raise ValueError("PowerShell lieferte Exit-Code %d." % code)
        return int(out.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        LOG.warn("Windows-Build nicht ermittelbar; verwende konservativen Standard: %s" % exc)
        return 26100


# ---------------------------------------------------------------------------
# Versionslogik
# ---------------------------------------------------------------------------
def version_key(v: str) -> Tuple[int, ...]:
    if not v:
        return ()
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts[:5]) if parts else ()


def is_newer(candidate: str, current: str) -> bool:
    a, b = version_key(candidate), version_key(current)
    if not a:
        return False
    if not b:
        return True
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b


def hw_id_core(hwid: str) -> str:
    """Kanonischer Kern einer Hardware-ID, z. B. PCI\\VEN_8086&DEV_2723.

    Windows-Hardware-IDs haben die Form
    PCI\\VEN_8086&DEV_2723&SUBSYS_00748086&REV_01\\<Instanzpfad>.
    Fuer den INF-Abgleich ist nur VEN/DEV bzw. VID/PID relevant.
    """
    hwid = (hwid or "").strip().replace(" ", "")
    if "\\" not in hwid:
        return ""
    prefix, rest = hwid.split("\\", 1)
    fields = [f for f in rest.split("&") if f]
    keys = [f.split("_", 1)[0].upper() for f in fields]
    if "VEN" in keys and "DEV" in keys:
        want = ("VEN", "DEV")
    elif "VID" in keys and "PID" in keys:
        want = ("VID", "PID")
    else:
        want = None
    if want:
        picked: List[str] = []
        for f, k in zip(fields, keys):
            if k in want and k not in [x.split("_", 1)[0].upper() for x in picked]:
                picked.append(f)
            if len(picked) == 2:
                break
        if len(picked) == 2:
            return prefix.upper() + "\\" + "&".join(picked)
    keep = [f for f in fields if not f.upper().startswith(("REV_", "MI_"))]
    if not keep:
        return ""
    return prefix.upper() + "\\" + "&".join(keep)


def hw_id_subsys(hwid: str) -> str:
    """SUBSYS_-Kennung einer Hardware-ID (leer, wenn keine vorhanden)."""
    for f in (hwid or "").split("\\")[-1].split("&"):
        if f.upper().startswith("SUBSYS_"):
            return f.upper()
    return ""


def hwid_compatible(device_id: str, inf_id: str) -> bool:
    """True, wenn eine INF-ID zum Geraet passt (Kern gleich, SUBSYS ohne Konflikt)."""
    dc, ic = hw_id_core(device_id), hw_id_core(inf_id)
    if not dc or not ic or dc != ic:
        return False
    ds, isb = hw_id_subsys(device_id), hw_id_subsys(inf_id)
    if ds and isb and ds != isb:
        return False
    return True


def hw_id_cores(hwid: str) -> List[str]:
    """Alle Pruefvarianten einer Hardware-ID (Kompatibilitaet mit aelteren INFs)."""
    core = hw_id_core(hwid)
    return [core] if core else []


_INF_HWID_SCAN = re.compile(
    r"(?<![A-Za-z0-9_.\\])(?P<bus>[A-Z][A-Z0-9_]{1,15})\\[A-Za-z0-9_&._#-]{2,}(?:&[A-Za-z0-9_&._#-]+)*")

_KNOWN_BUSES = {
    "PCI", "USB", "ACPI", "HID", "BTHENUM", "SCSI", "SD", "DISK", "HDC", "MODEM",
    "ROOT", "UEFI", "FOLD", "LPT", "PCMCIA", "PLUGPLAY", "SW", "I8042", "XUSB",
    "SMBIOS", "BIOS", "MMDEV", "WPD", "PCIIDE", "INTERNET", "MEDIA",
}


def read_text_auto(path: Path) -> str:
    """Liest eine Datei und beruecksichtigt BOM/UTF-16 (entpackte INFs sind oft UTF-16)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def inf_hardware_ids(text: str) -> List[str]:
    """Extrahiert die Hardware-IDs aus einer INF-Datei.

    Sie stehen je nach Hersteller links vom Gleichheitszeichen
    ("PCI\\VEN_8086&DEV_2723 = Install") oder rechts davon
    ("%iBT_USB% = ibtusb, USB\\VID_8087&PID_0029&REV_0001").
    Registry-Schluessel (HKLM,\\SYSTEM\\...) werden dadurch nicht erfasst.
    """
    out: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        for m in _INF_HWID_SCAN.finditer(line):
            key = m.group(0)
            bus = m.group("bus")
            if bus not in _KNOWN_BUSES and "&DEV_" not in key and "&PID_" not in key:
                continue
            if key not in out:
                out.append(key)
    return out


# ---------------------------------------------------------------------------
# Update-Engine
# ---------------------------------------------------------------------------
_NOISE_TOKENS = {
    "driver", "drivers", "device", "devices", "software", "for", "windows", "version",
    "update", "system", "controller", "adapter", "and", "the", "with", "desktop",
    "r", "tm", "inc", "corporation", "corp", "ltd", "gmbh", "co", "usa", "class",
    "package", "kit", "suite", "installation", "utility", "wimax", "networking",
}

# Titel, die auf Firmware-/BIOS-Aktionen hindeuten, werden nie angeboten.
_RISKY_TITLE_TOKENS = ("firmware", "bios", "uefi", "flash", "recovery", "capsule")

# Geraeteklasse -> Stichwort in der Katalog-Klassifikation ("Drivers (Networking)")
_CLASS_HINTS = {
    "net": "networking", "media": "sound", "display": "display", "bluetooth": "other hardware",
    "usb": "other hardware", "hidclass": "other hardware", "keyboard": "other hardware",
    "modem": "other hardware", "system": "system", "scsiadapter": "storage",
    "diskdrive": "storage", "hdc": "storage", "ide": "storage", "sd": "storage",
}

# Woerter, die fuer die Unterscheidung des Geraets nichts aussagen
_WEAK_TOKENS = {
    "controller", "family", "driver", "device", "system", "software", "update", "interface",
    "plug", "gaming", "onboard", "integrated", "dedicated", "card", "bus", "mini", "pcie",
    "high", "speed", "plus", "series", "chipset", "computer", "pc", "corp", "gmbh",
}


def _clean_name(name: str) -> str:
    name = re.sub(r"\(R\)|\(TM\)|\(r\)|\(tm\)|™|®", " ", name or "")
    name = re.sub(r"\bfor Windows.*$", " ", name, flags=re.I)
    name = re.sub(r"\bversion\s+\d[\w.]*", " ", name, flags=re.I)
    name = re.sub(r"\b\d+\.\d+[\w.]*\b", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _words(text: str) -> List[str]:
    """Woerter inkl. Bindestrichen (z. B. Wi-Fi, 2.5GbE), ohne reine Zahlen."""
    out: List[str] = []
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]*", text or ""):
        w = w.strip(".-+")
        if not w or w.replace(".", "").isdigit():
            continue
        out.append(w)
    return out


def _norm_tokens(text: str) -> List[str]:
    """Kleingeschriebene Vergleichswoerter (Bindestriche getrennt)."""
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t]


def _vendor_token(dev: InstalledDriver) -> str:
    mfr = re.sub(r"\(R\)|\(TM\)|Corporation|Inc\.|Ltd\.|GmbH|Co\.|or its affiliates", "",
                 dev.manufacturer or "", flags=re.I).strip()
    toks = _words(mfr)
    return toks[0] if toks else ""


def _device_tokens(dev: InstalledDriver) -> List[str]:
    toks = _words(_clean_name(dev.name))
    return [t for t in toks if t.lower() not in _NOISE_TOKENS]


def _strong_tokens(dev: InstalledDriver) -> List[str]:
    """Kennzeichnende Worte des Geraetennamens (z. B. AX200, Bluetooth, GeForce)."""
    vendor = _vendor_token(dev).lower()
    out: List[str] = []
    for t in _words(_clean_name(dev.name)):
        low = t.lower()
        if low in _NOISE_TOKENS or low in _WEAK_TOKENS or low == vendor:
            continue
        if len(t) < 3:
            continue
        out.append(t)
    return out


def _safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    """Entpackt ZIP-Dateien ohne Traversalpfade oder symbolische Links."""
    root = destination.resolve()
    for item in archive.infolist():
        name = item.filename.replace("\\", "/")
        is_dir = item.is_dir()
        normalized = name.rstrip("/") if is_dir else name
        parts = normalized.split("/")
        if (not normalized or name.startswith("/") or any(part in ("", ".", "..") for part in parts)
                or any(":" in part for part in parts)):
            raise ValueError("Das ZIP-Archiv enthaelt einen unsicheren Pfad.")
        if stat.S_ISLNK(item.external_attr >> 16):
            raise ValueError("Symbolische Links in ZIP-Archiven werden nicht entpackt.")
        target = root.joinpath(*parts)
        resolved = target.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("Ein ZIP-Eintrag verlaesst den Zielordner.") from exc
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError("Ein ZIP-Eintrag wuerde einen symbolischen Link ueberschreiben.")
        with archive.open(item) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)


@dataclass
class PreparedUpdate:
    driver: InstalledDriver
    entry: CatalogEntry
    inf_path: Path
    new_version: str


class UpdateEngine:
    def __init__(self, catalog: CatalogClient, workdir: Path) -> None:
        self.catalog = catalog
        self.workdir = workdir
        self.release = os_release(os_build())
        self._package_locks: Dict[str, threading.Lock] = {}
        self._package_locks_guard = threading.Lock()

    def _package_lock(self, update_id: str) -> threading.Lock:
        with self._package_locks_guard:
            return self._package_locks.setdefault(update_id, threading.Lock())

    # -- Katalogsuche ------------------------------------------------------
    def build_query(self, dev: InstalledDriver) -> str:
        """Katalog-Suchbegriff: Hersteller + die aussagekraeftigsten Geraetewoerter."""
        vendor = _vendor_token(dev)
        name_toks = [t for t in _device_tokens(dev) if t.lower() != vendor.lower()]
        toks: List[str] = []
        for t in [vendor] + name_toks[:3]:
            if t and t.lower() not in [x.lower() for x in toks]:
                toks.append(t)
        return " ".join(toks)

    def query_variants(self, dev: InstalledDriver) -> List[str]:
        """Mehrere Suchbegriffe, vom spezifischsten zum allgemeinsten."""
        out: List[str] = []
        vendor = _vendor_token(dev)
        name_toks = [t for t in _device_tokens(dev) if t.lower() != vendor.lower()]
        for words in (name_toks[:3], name_toks[:2], name_toks[:1]):
            toks: List[str] = []
            for t in ([vendor] if vendor else []) + list(words):
                if t and t.lower() not in [x.lower() for x in toks]:
                    toks.append(t)
            q = " ".join(toks)
            if q and q not in out:
                out.append(q)
        return out

    def _title_tier(self, dev: InstalledDriver, entry: CatalogEntry) -> int:
        """Wie gut passt der Katalogeintrag zum Geraet? 2 = exakt, 1 = Hersteller."""
        title = (entry.title or "").lower()
        if not title:
            return 0
        strong = {t.lower() for t in _strong_tokens(dev)}
        title_words = set(_norm_tokens(entry.title))
        if strong & title_words:
            return 2
        vendor = _vendor_token(dev).lower()
        if vendor and vendor in title:
            return 1
        return 0

    def _class_score(self, dev: InstalledDriver, entry: CatalogEntry) -> int:
        """Passt die Katalog-Klassifikation zur Geraeteklasse?"""
        hint = _CLASS_HINTS.get((dev.device_class or "").strip().lower())
        if not hint:
            return 0
        cls = (entry.classification or "").lower()
        return 1 if hint in cls else 0

    def candidates(self, dev: InstalledDriver,
                   cancel_event: Optional[threading.Event] = None) -> List[CatalogEntry]:
        seen: Dict[str, CatalogEntry] = {}
        errors: List[Exception] = []
        queries = self.query_variants(dev)
        if not queries:
            raise RuntimeError("Fuer dieses Geraet konnte kein Suchbegriff erstellt werden.")
        for query in queries:
            if cancel_event and cancel_event.is_set():
                raise OperationCancelled("Treiberprüfung wurde abgebrochen.")
            try:
                if cancel_event:
                    entries = self.catalog.search(query, cancel_event=cancel_event)
                else:
                    entries = self.catalog.search(query)
            except OperationCancelled:
                raise
            except Exception as exc:
                errors.append(exc)
                continue
            if cancel_event and cancel_event.is_set():
                raise OperationCancelled("Treiberprüfung wurde abgebrochen.")
            for e in entries:
                seen.setdefault(e.update_id, e)
            if len(self._rank(dev, seen.values())) >= 3:
                break
        ranked = self._rank(dev, seen.values())
        if errors and not ranked:
            raise RuntimeError("Fuer %s konnte kein verlaesslicher Katalogstatus ermittelt werden: %s" %
                               (dev.name[:40], errors[-1])) from errors[-1]
        if errors:
            LOG.warn("Ein Teil der Katalogabfragen fuer %s ist fehlgeschlagen." % dev.name[:40])
        return ranked[:6]

    def _rank(self, dev: InstalledDriver, entries) -> List[CatalogEntry]:
        """Filtert und sortiert die Katalogeintraege: passend, aktuell, passendes Release."""
        release = self.release.upper()
        out: List[CatalogEntry] = []
        for e in entries:
            title = (e.title or "").lower()
            if any(t in title for t in _RISKY_TITLE_TOKENS):
                continue
            classification = (e.classification or "").lower()
            if classification:
                if not classification.startswith("drivers"):
                    continue
            elif "driver" not in title:
                continue
            tier = self._title_tier(dev, e)
            if tier == 0:
                continue
            if not is_newer(e.version, dev.version):
                continue
            product = e.product or ""
            if product and not ("Windows 11" in product or "Windows 10" in product):
                continue
            out.append(e)
        out.sort(key=lambda e: (self._title_tier(dev, e),
                                self._class_score(dev, e),
                                release in (e.product or "").upper(),
                                version_key(e.version)), reverse=True)
        best_tier = max([self._title_tier(dev, e) for e in out], default=0)
        out = [e for e in out if self._title_tier(dev, e) == best_tier]
        # dasselbe Paket erscheint je Betriebssystem-Version mehrfach -> entdoppeln
        unique: List[CatalogEntry] = []
        seen_keys = set()
        for e in out:
            key = (e.title.lower(), e.version)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            unique.append(e)
        return unique

    # -- Paket vorbereiten -------------------------------------------------
    def prepare(self, dev: InstalledDriver, entry: CatalogEntry,
                cancel_event: Optional[threading.Event] = None) -> Optional[PreparedUpdate]:
        with self._package_lock(entry.update_id):
            try:
                return self._prepare_locked(dev, entry, cancel_event)
            except OperationCancelled:
                raise
            except Exception as exc:
                LOG.warn("Paket konnte nicht geprueft werden (%s): %s" % (dev.name[:40], exc))
                raise

    def _prepare_locked(self, dev: InstalledDriver,
                        entry: CatalogEntry,
                        cancel_event: Optional[threading.Event] = None
                        ) -> Optional[PreparedUpdate]:
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Treiberprüfung wurde abgebrochen.")
        package_dir = self.workdir / entry.update_id
        files = self.catalog.download(
            entry.update_id, package_dir, cancel_event=cancel_event
        )
        if not files:
            raise RuntimeError("Der Update-Katalog hat keine Paketdateien geliefert.")
        package_dir.mkdir(parents=True, exist_ok=True)
        extract_dir = Path(tempfile.mkdtemp(prefix="extract-", dir=str(package_dir)))
        extracted = False
        for package in files:
            if cancel_event and cancel_event.is_set():
                raise OperationCancelled("Treiberprüfung wurde abgebrochen.")
            suffix = package.suffix.lower()
            if suffix == ".cab":
                result = subprocess.run(
                    ["expand.exe", "-F:*", str(package), str(extract_dir)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=180, creationflags=NO_CONSOLE_WINDOW,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "").strip()
                    raise RuntimeError("CAB konnte nicht entpackt werden: %s" % detail)
                extracted = True
            elif suffix == ".zip":
                with zipfile.ZipFile(package) as archive:
                    _safe_extract_zip(archive, extract_dir)
                extracted = True
        if not extracted:
            raise RuntimeError("Das Update-Paket enthaelt kein unterstuetztes CAB- oder ZIP-Archiv.")
        inf = self._match_inf(dev, extract_dir)
        if inf is None:
            return None
        new_version = self._inf_version(inf) or entry.version
        if not is_newer(new_version, dev.version):
            LOG.warn("    INF %s ist nicht neuer als installiert (%s)" % (new_version, dev.version))
            return None
        return PreparedUpdate(dev, entry, inf, new_version)

    @staticmethod
    def _inf_version(inf: Path) -> str:
        text = read_text_auto(inf)
        m = re.search(r"(?im)^\s*DriverVer\s*=\s*[^,]+,\s*([0-9][0-9.]*)", text)
        return m.group(1).rstrip(".") if m else ""

    def _match_inf(self, dev: InstalledDriver, root: Path) -> Optional[Path]:
        """Findet die INF im entpackten Paket, die zur Hardware-ID des Geraets passt."""
        dev_ids = [h for h in dev.hardware_ids if h]
        if not dev_ids:
            return None
        cores = {hw_id_core(h) for h in dev_ids}
        cores.discard("")
        if not cores:
            return None
        best: Optional[Path] = None
        best_ver: Tuple[int, ...] = ()
        best_exact = False
        for inf in sorted(root.rglob("*.inf")):
            text = read_text_auto(inf)
            inf_ids = inf_hardware_ids(text)
            if not inf_ids:
                continue
            exact = False
            ok = False
            for iid in inf_ids:
                icore = hw_id_core(iid)
                if not icore or icore not in cores:
                    continue
                if not any(hwid_compatible(iid, h) for h in dev_ids):
                    continue
                ok = True
                if hw_id_subsys(iid) and hw_id_subsys(iid) in {
                        hw_id_subsys(h) for h in dev_ids}:
                    exact = True
            if not ok:
                continue
            ver = version_key(self._inf_version(inf))
            if best is None or (exact, ver) > (best_exact, best_ver):
                best, best_ver, best_exact = inf, ver, exact
        return best


# ---------------------------------------------------------------------------
# Elevated Installation
# ---------------------------------------------------------------------------
_INSTALL_PS = """$ErrorActionPreference='Stop'
chcp 65001 > $null
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$log = '%LOG%'
$payload = '%PAYLOAD%'
function W($m) { Add-Content -LiteralPath $log -Value $m -Encoding UTF8 }
W '=== Administrator-Treiberinstallation ==='
if (-not (Get-Command pnputil.exe -ErrorAction SilentlyContinue)) {
    W 'pnputil.exe wurde nicht gefunden.'
    exit 1
}
try {
    $data = @(Get-Content -LiteralPath $payload -Raw | ConvertFrom-Json)
} catch {
    W ('Payload konnte nicht gelesen werden: ' + $_.Exception.Message)
    exit 1
}
$failed = $false
foreach ($item in $data) {
    W ''
    W ('>> ' + $item.name + '  [' + $item.version + ']')
    W ('   INF: ' + $item.inf)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = & pnputil.exe /add-driver $item.inf /install 2>&1 | Out-String
    $code = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    foreach ($line in ($out -split "`r?`n")) {
        $t = $line.Trim()
        if ($t) { W ('   ' + $t) }
    }
    if ($code -ne 0) {
        $failed = $true
        W ('   Fehlercode: ' + $code)
    }
}
W ''
W '=== Installation abgeschlossen ==='
if ($failed) { exit 1 }
exit 0
"""


def install_prepared(items: Sequence[PreparedUpdate], workdir: Path) -> int:
    """Installiert die vorbereiteten INFs elevated. Gibt Exit-Code zurueck."""
    if not items:
        return 0
    workdir.mkdir(parents=True, exist_ok=True)
    job_dir = Path(tempfile.mkdtemp(prefix="install-", dir=str(workdir)))
    payload = job_dir / "payload.json"
    log = job_dir / "install.log"
    payload.write_text(json.dumps(
        [{"name": it.driver.name, "version": it.new_version, "inf": str(it.inf_path)} for it in items],
        ensure_ascii=False,
    ), encoding="utf-8-sig")
    if log.exists():
        log.unlink()
    ps = job_dir / "install.ps1"
    ps.write_text(
        _INSTALL_PS.replace("%LOG%", str(log).replace("'", "''"))
                     .replace("%PAYLOAD%", str(payload).replace("'", "''")),
        encoding="utf-8-sig",
    )
    code = run_elevated(ps)
    if log.exists():
        try:
            for line in log.read_text(encoding="utf-8-sig", errors="replace").splitlines()[-80:]:
                if line.strip():
                    LOG.info(line)
            LOG.info("Installationsprotokoll: %s" % log)
        except OSError as exc:
            LOG.warn("Installationsprotokoll konnte nicht gelesen werden: %s" % exc)
    return code


# ---------------------------------------------------------------------------
# Datei-Bereinigung
# ---------------------------------------------------------------------------
@dataclass
class FileEntry:
    path: str
    size: int
    reason: str
    group: int = -1
    keep: bool = False


class OperationCancelled(Exception):
    pass


DEFAULT_SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata", "$recycle.bin",
    "system volume information", "appdata", "node_modules", ".git", "venv", ".venv",
    "__pycache__", ".cache", "recovery",
}


def _protected_scan_root(folder: Path) -> Optional[Path]:
    protected = (
        "WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)",
        "ProgramData",
    )
    for variable in protected:
        value = os.environ.get(variable)
        if not value:
            continue
        try:
            root = Path(value).resolve()
            if folder == root or root in folder.parents:
                return root
        except OSError:
            continue
    return None


class FileCleaner:
    def scan(self, folder: Path, find_dupes: bool = True, max_age_days: int = 0,
             min_size_kb: int = 0, extensions: str = "", skip_system: bool = True,
             progress: Optional[Callable[[int, str], None]] = None,
             cancel_event: Optional[threading.Event] = None) -> List[FileEntry]:
        if not folder.exists() or not folder.is_dir():
            raise ValueError("Der ausgewaehlte Pfad ist kein gueltiger Ordner.")
        if max_age_days < 0 or min_size_kb < 0:
            raise ValueError("Alter und Mindestgroesse muessen 0 oder groesser sein.")
        folder = folder.resolve()
        protected = _protected_scan_root(folder)
        if protected is not None:
            raise ValueError("Systemordner koennen nicht bereinigt werden: %s" % protected)
        exts = {e.strip().lower() for e in extensions.replace(";", ",").split(",") if e.strip()}
        exts = {e if e.startswith(".") else "." + e for e in exts}
        min_bytes = max(0, min_size_kb) * 1024
        now = time.time()
        by_size: Dict[int, List[str]] = {}
        old_files: List[str] = []
        total = 0

        def walk_error(exc: OSError) -> None:
            LOG.warn("Ordner konnte nicht gelesen werden: %s" % exc)

        for root, dirs, files in os.walk(folder, onerror=walk_error):
            if cancel_event and cancel_event.is_set():
                raise OperationCancelled("Datei-Scan wurde abgebrochen.")
            dirs[:] = [
                d for d in dirs
                if not os.path.islink(os.path.join(root, d))
                and not (skip_system and d.lower() in DEFAULT_SKIP_DIRS)
            ]
            for name in files:
                if cancel_event and cancel_event.is_set():
                    raise OperationCancelled("Datei-Scan wurde abgebrochen.")
                p = os.path.join(root, name)
                try:
                    st = os.stat(p, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    continue
                total += 1
                if progress and total % 250 == 0:
                    progress(total, name)
                if exts and os.path.splitext(name)[1].lower() not in exts:
                    continue
                if st.st_size < min_bytes:
                    continue
                if max_age_days and (now - st.st_mtime) > max_age_days * 86400:
                    old_files.append(p)
                if find_dupes:
                    by_size.setdefault(st.st_size, []).append(p)

        entries: List[FileEntry] = []

        # Duplikate: erst nach Groesse gruppieren, dann voller SHA-256
        if find_dupes:
            gid = 0
            hashed = 0
            for size, paths in by_size.items():
                if len(paths) < 2:
                    continue
                buckets: Dict[str, List[str]] = {}
                for p in paths:
                    if cancel_event and cancel_event.is_set():
                        raise OperationCancelled("Datei-Scan wurde abgebrochen.")
                    hashed += 1
                    if progress and hashed % 25 == 0:
                        progress(total + hashed, "Pruefe Hashes: " + os.path.basename(p))
                    h = sha256_file(p, cancel_event=cancel_event)
                    if h:
                        buckets.setdefault(h, []).append(p)
                for _h, group in buckets.items():
                    if len(group) < 2:
                        continue
                    gid += 1
                    def modified(path: str) -> float:
                        try:
                            return os.stat(path, follow_symlinks=False).st_mtime
                        except OSError:
                            return 0.0

                    group.sort(key=lambda p: (-modified(p), p.lower()))
                    for i, p in enumerate(group):
                        entries.append(FileEntry(p, size, "Duplikat", gid, keep=(i == 0)))

        duplicate_paths = {e.path for e in entries}
        for p in old_files:
            if p in duplicate_paths:
                continue
            try:
                size = os.stat(p).st_size
            except OSError:
                size = 0
            entries.append(FileEntry(p, size, "Aelter als %d Tage" % max_age_days, keep=True))

        return entries


def sha256_file(path: str, chunk: int = 1 << 20,
                cancel_event: Optional[threading.Event] = None) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                if cancel_event and cancel_event.is_set():
                    raise OperationCancelled("Hash-Prüfung wurde abgebrochen.")
                b = fh.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except OSError:
        return None


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return str(n)


def show_action_error(title: str, action: str, exc: Exception,
                      next_step: str, parent: Optional[tk.Misc] = None) -> None:
    messagebox.showerror(
        title,
        "%s ist fehlgeschlagen.\n\nDetails: %s\n\nNächster Schritt: %s" %
        (action, exc, next_step),
        parent=parent,
    )


def _run_ps_json(script: str, timeout: int = 30):
    code, out, err = run_ps(script, timeout=timeout)
    if code != 0:
        raise RuntimeError(err.strip() or "PowerShell ist mit Code %d fehlgeschlagen." % code)
    try:
        return json.loads(out) if out.strip() else None
    except json.JSONDecodeError as exc:
        raise RuntimeError("PowerShell lieferte ungueltige JSON-Daten.") from exc


def load_overview_data() -> dict:
    data = _run_ps_json(r"""
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$load = Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average
$drives = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" |
  ForEach-Object { [pscustomobject]@{ Name=$_.DeviceID; Size=[double]$_.Size; Free=[double]$_.FreeSpace } })
[pscustomobject]@{
  OS=$os.Caption; Version=$os.Version; Build=$os.BuildNumber
  CPU=$cpu.Name; CpuLoad=[double]$load.Average
  TotalMemory=[double]$os.TotalVisibleMemorySize*1KB
  FreeMemory=[double]$os.FreePhysicalMemory*1KB; Drives=$drives
} | ConvertTo-Json -Compress -Depth 4
""")
    if not isinstance(data, dict):
        raise RuntimeError("Systeminformationen konnten nicht ermittelt werden.")
    return data


def load_monitor_data() -> dict:
    data = _run_ps_json(r"""
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average
$processes = @(Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 15 |
  ForEach-Object { [pscustomobject]@{ Name=$_.ProcessName; PID=$_.Id; Memory=[double]$_.WorkingSet64 } })
$drives = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" |
  ForEach-Object { [pscustomobject]@{ Name=$_.DeviceID; Size=[double]$_.Size; Free=[double]$_.FreeSpace } })
[pscustomobject]@{
  CPU=[double]$cpu.Average; TotalMemory=[double]$os.TotalVisibleMemorySize*1KB
  FreeMemory=[double]$os.FreePhysicalMemory*1KB; Processes=$processes; Drives=$drives
} | ConvertTo-Json -Compress -Depth 4
""", timeout=45)
    if not isinstance(data, dict):
        raise RuntimeError("Systemauslastung konnte nicht ermittelt werden.")
    return data


def analyze_storage(folder: Path,
                    progress: Optional[Callable[[int, str], None]] = None,
                    cancel_event: Optional[threading.Event] = None) -> Tuple[List[Tuple[str, int, int]], int]:
    if not folder.exists() or not folder.is_dir():
        raise ValueError("Der ausgewählte Speicherpfad ist kein Ordner.")
    root = folder.resolve()
    totals: Dict[str, List[int]] = {}
    overall = 0
    count = 0
    def onerror(exc: OSError) -> None:
        LOG.warn("Speicheranalyse: Ordner nicht lesbar: %s" % exc)
    for current, dirs, files in os.walk(root, onerror=onerror):
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Speicheranalyse wurde abgebrochen.")
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(current, name))]
        for name in files:
            if cancel_event and cancel_event.is_set():
                raise OperationCancelled("Speicheranalyse wurde abgebrochen.")
            path = os.path.join(current, name)
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            relative = Path(path).relative_to(root)
            category = relative.parts[0] if len(relative.parts) > 1 else "(Ordnerwurzel)"
            extension = Path(name).suffix.lower() or "(ohne Endung)"
            for key in ("Ordner: " + category, "Typ: " + extension):
                totals.setdefault(key, [0, 0])
                totals[key][0] += info.st_size
                totals[key][1] += 1
            overall += info.st_size
            count += 1
            if progress and count % 100 == 0:
                progress(count, name)
    rows = [(name, values[0], values[1]) for name, values in totals.items()]
    rows.sort(key=lambda item: item[1], reverse=True)
    return rows, overall


def get_network_data() -> List[dict]:
    items = _run_ps_json(r"""
$items = @(Get-CimInstance Win32_NetworkAdapterConfiguration -Filter "IPEnabled=True" |
  ForEach-Object {
    [pscustomobject]@{
      Name=$_.Description; IPv4=@($_.IPAddress | Where-Object { $_ -match '^\d{1,3}(\.\d{1,3}){3}$' })
      Gateway=@($_.DefaultIPGateway); DNS=@($_.DNSServerSearchOrder)
      DHCP=$_.DHCPEnabled
    }
  })
ConvertTo-Json -InputObject $items -Compress -Depth 4
""", timeout=30)
    if items is None:
        return []
    if isinstance(items, dict):
        return [items]
    if not isinstance(items, list):
        raise RuntimeError("Netzwerkdaten haben ein ungültiges Format.")
    return items


def check_network_connection(host: str = "www.catalog.update.microsoft.com",
                             timeout: float = 4.0) -> Tuple[bool, str]:
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True, "HTTPS-Verbindung zu %s erfolgreich." % host
    except OSError as exc:
        return False, "Verbindung zu %s fehlgeschlagen: %s" % (host, exc)


STARTUP_RUN_KEYS = (
    r"Software\Microsoft\Windows\CurrentVersion\Run",
    r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
)
DISABLED_STARTUP_KEY = r"Software\SystemUpdaterCleaner\DisabledStartup"
STARTUP_FOLDER = Path(os.environ.get("APPDATA", "")) / (
    r"Microsoft\Windows\Start Menu\Programs\Startup"
)
DISABLED_STARTUP_FOLDER = APP_DIR / "DisabledStartupFiles"


def get_startup_items() -> List[dict]:
    if winreg is None:
        raise RuntimeError("Autostart-Verwaltung ist nur unter Windows verfügbar.")
    rows: List[dict] = []
    try:
        disabled_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, DISABLED_STARTUP_KEY, 0,
                                      winreg.KEY_READ)
    except FileNotFoundError:
        disabled_key = None
    if disabled_key is not None:
        with disabled_key as key:
            index = 0
            while True:
                try:
                    name, data, kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                try:
                    value = json.loads(data)
                except (TypeError, json.JSONDecodeError):
                    value = {"data": str(data), "kind": kind, "key": STARTUP_RUN_KEYS[0]}
                if not isinstance(value, dict):
                    value = {"data": str(data), "kind": kind, "key": STARTUP_RUN_KEYS[0]}
                rows.append({"name": name, "command": value.get("data", ""),
                             "enabled": False, "source": "Benutzer-Registry",
                             "writable": True, "registry_name": name})

    for key_path in STARTUP_RUN_KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                index = 0
                while True:
                    try:
                        name, command, _kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    rows.append({"name": name, "command": str(command), "enabled": True,
                                 "source": "Benutzer-Registry",
                                 "writable": key_path == STARTUP_RUN_KEYS[0],
                                 "registry_name": name, "registry_key": key_path})
        except OSError:
            continue

    for folder, enabled in ((STARTUP_FOLDER, True), (DISABLED_STARTUP_FOLDER, False)):
        if folder.is_dir():
            for path in folder.iterdir():
                if path.is_file():
                    rows.append({"name": path.name, "command": str(path), "enabled": enabled,
                                 "source": "Benutzer-Startordner", "writable": True,
                                 "file_path": str(path)})
    rows.sort(key=lambda row: (not row["enabled"], row["name"].lower()))
    return rows


def set_startup_enabled(item: dict, enabled: bool) -> None:
    if item.get("source") == "Benutzer-Startordner" and item.get("writable"):
        source = Path(str(item.get("file_path") or ""))
        expected = DISABLED_STARTUP_FOLDER if enabled else STARTUP_FOLDER
        destination_root = STARTUP_FOLDER if enabled else DISABLED_STARTUP_FOLDER
        if not source.is_file() or source.parent.resolve() != expected.resolve():
            raise ValueError("Der ausgewählte Startordner-Eintrag ist ungültig.")
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / str(item.get("name") or "")
        if destination.exists():
            raise FileExistsError("Am Ziel existiert bereits eine Datei mit diesem Namen.")
        source.rename(destination)
        return
    if winreg is None or item.get("source") != "Benutzer-Registry" or not item.get("writable"):
        raise ValueError("Dieser Autostart-Eintrag kann hier nicht sicher geändert werden.")
    name = str(item.get("registry_name") or "")
    if not name:
        raise ValueError("Der Registry-Name des Autostart-Eintrags fehlt.")
    if enabled:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DISABLED_STARTUP_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as backup:
            encoded, _ = winreg.QueryValueEx(backup, name)
            value = json.loads(encoded)
        key_path = value.get("key")
        if key_path not in STARTUP_RUN_KEYS:
            raise ValueError("Der gespeicherte Autostart-Zielschlüssel ist ungültig.")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, int(value["kind"]), value["data"])
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DISABLED_STARTUP_KEY, 0,
                            winreg.KEY_SET_VALUE) as backup:
            winreg.DeleteValue(backup, name)
    else:
        key_path = item.get("registry_key")
        if key_path not in STARTUP_RUN_KEYS:
            raise ValueError("Der Registry-Zielschlüssel ist ungültig.")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            command, kind = winreg.QueryValueEx(key, name)
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, DISABLED_STARTUP_KEY, 0,
                                    winreg.KEY_SET_VALUE) as backup:
                winreg.SetValueEx(
                    backup, name, 0, winreg.REG_SZ,
                    json.dumps({"data": command, "kind": kind, "key": key_path}),
                )
            winreg.DeleteValue(key, name)


def export_installed_drivers(destination: Path,
                             cancel_event: Optional[threading.Event] = None) -> Tuple[int, str]:
    return export_drivers_to_folder(destination, cancel_event)


def export_drivers_to_folder(destination: Path,
                             cancel_event: Optional[threading.Event] = None) -> Tuple[int, str]:
    destination.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        ["pnputil.exe", "/export-driver", "*", str(destination)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", creationflags=NO_CONSOLE_WINDOW,
    )
    while process.poll() is None:
        if cancel_event and cancel_event.wait(0.2):
            process.terminate()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
            raise OperationCancelled("Treiberexport wurde abgebrochen.")
        time.sleep(0.05)
    output, _ = process.communicate()
    return process.returncode, (output or "").strip()


def restore_driver_backup(folder: Path,
                          cancel_event: Optional[threading.Event] = None) -> Tuple[int, str]:
    root = folder.resolve()
    infs: List[Path] = []
    for path in root.rglob("*.inf"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            path.resolve(strict=True).relative_to(root)
            infs.append(path)
        except (OSError, ValueError):
            continue
    if not infs:
        raise ValueError("Im ausgewählten Ordner wurden keine INF-Treiberdateien gefunden.")
    if cancel_event and cancel_event.is_set():
        raise OperationCancelled("Treiberwiederherstellung wurde abgebrochen.")
    workdir = Path(tempfile.mkdtemp(prefix="SystemUpdaterCleaner-restore-drivers-"))
    script = workdir / "restore-drivers.ps1"
    inf_list = ",\n".join(
        "  '%s'" % str(path).replace("'", "''") for path in infs
    )
    try:
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "$infs = @(\n%s\n)\n"
            "if (-not $infs) { throw 'Keine INF-Dateien gefunden.' }\n"
            "$needsRestart = $false\n"
            "foreach ($inf in $infs) {\n"
            "  if (-not (Test-Path -LiteralPath $inf -PathType Leaf)) { throw ('Datei fehlt: ' + $inf) }\n"
            "  Write-Output ('Installiere ' + $inf)\n"
            "  & pnputil.exe /add-driver $inf /install\n"
            "  if ($LASTEXITCODE -eq 3010) { $needsRestart = $true }\n"
            "  elseif ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
            "}\n"
            "if ($needsRestart) { exit 3010 }\n"
            % inf_list,
            encoding="utf-8-sig",
        )
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Treiberwiederherstellung wurde abgebrochen.")
        code = run_elevated(script, timeout=900)
        if code == 3010:
            return code, "Treiber wiederhergestellt; Windows muss neu gestartet werden."
        return code, "Treiberwiederherstellung abgeschlossen." if code == 0 else (
            "Treiberwiederherstellung beendet (Code %d)." % code
        )
    finally:
        try:
            script.unlink(missing_ok=True)
            workdir.rmdir()
        except OSError as exc:
            LOG.warn("Temporäre Treiberwiederherstellungsdatei blieb zurück: %s" % exc)


def build_diagnostic_report(sections: dict) -> str:
    lines = [
        "SYSTEM UPDATER & CLEANER – DIAGNOSEBERICHT",
        "Erstellt: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "Dieser Bericht wurde lokal erstellt. Er wurde nicht hochgeladen.",
        "",
    ]
    overview = sections.get("overview")
    if isinstance(overview, dict):
        lines.extend(["SYSTEM", "------",
                      "Windows: %s (Build %s)" %
                      (overview.get("OS", "Unbekannt"), overview.get("Build", "?")),
                      "Version: %s" % overview.get("Version", "?"),
                      "Prozessor: %s" % overview.get("CPU", "Unbekannt"),
                      "CPU-Auslastung zum Zeitpunkt der Abfrage: %s%%" %
                      overview.get("CpuLoad", "?"),
                      "RAM frei: %s / %s" %
                      (human_size(int(overview.get("FreeMemory") or 0)),
                       human_size(int(overview.get("TotalMemory") or 0)))])
        drives = overview.get("Drives") or []
        if isinstance(drives, dict):
            drives = [drives]
        lines.append("Laufwerke:")
        for drive in drives:
            lines.append("  %s: %s frei / %s" % (
                drive.get("Name", "?"), human_size(int(drive.get("Free") or 0)),
                human_size(int(drive.get("Size") or 0))))
        lines.append("")
    drivers = sections.get("drivers")
    if isinstance(drivers, list):
        lines.extend(["TREIBER", "-------",
                      "Anzahl erfasster Treiber: %d" % len(drivers)])
        for driver in drivers:
            lines.append("  %s | %s | %s | %s" % (
                driver.name, driver.manufacturer, driver.version, driver.device_class))
        lines.append("")
    network = sections.get("network")
    if isinstance(network, list):
        lines.extend(["NETZWERK", "--------"])
        for item in network:
            lines.append("  %s | IP: %s | Gateway: %s" % (
                item.get("Name", "?"), ", ".join(item.get("IPv4") or []),
                ", ".join(item.get("Gateway") or [])))
        lines.append("")
    startup = sections.get("startup")
    if isinstance(startup, list):
        lines.extend(["AUTOSTART", "---------"])
        for item in startup:
            lines.append("  %s | %s | %s" % (
                item.get("name", "?"),
                "Aktiv" if item.get("enabled") else "Deaktiviert",
                item.get("source", "?")))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def create_restore_point() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="SystemUpdaterCleaner-restore-"))
    script = workdir / "restore-point.ps1"
    try:
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "try {\n"
            "  Checkpoint-Computer -Description 'System Updater Cleaner' "
            "-RestorePointType 'MODIFY_SETTINGS'\n"
            "  exit 0\n"
            "} catch {\n"
            "  [Console]::Error.WriteLine($_.Exception.Message)\n"
            "  exit 1\n"
            "}\n",
            encoding="utf-8-sig",
        )
        return run_elevated(script, timeout=180)
    finally:
        try:
            script.unlink(missing_ok=True)
            workdir.rmdir()
        except OSError as exc:
            LOG.warn("Temporärer Wiederherstellungspunkt-Aufruf blieb zurück: %s" % exc)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def pick_font(root: tk.Misc, *candidates: str) -> str:
    available = set(tkfont.families(root))
    for c in candidates:
        if c in available:
            return c
    return "TkDefaultFont"


class LogPanel(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "App") -> None:
        super().__init__(master, style="Card.TFrame")
        self.app = app
        self.text = tk.Text(self, height=9, wrap="none", bd=0, highlightthickness=0,
                             bg=T.card, fg=T.dim, insertbackground=T.accent,
                             font=(app.mono, 9), padx=10, pady=8, state="disabled")
        vs = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=vs.set)
        self.text.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=8)
        vs.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=8)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        for lvl, col in LEVELS.items():
            self.text.tag_configure(lvl, foreground=col)
        self.text.tag_configure("ts", foreground=T.faint)

    def write(self, level: str, msg: str) -> None:
        self.text.configure(state="normal")
        stamp = time.strftime("%H:%M:%S")
        self.text.insert("end", stamp + "  ", "ts")
        self.text.insert("end", msg + "\n", level)
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > 501:
            self.text.delete("1.0", "%d.0" % (lines - 500))
        self.text.see("end")
        self.text.configure(state="disabled")


class Tooltip:
    def __init__(self, widget: tk.Misc, text: str) -> None:
        self.widget = widget
        self.text = text
        self.window: Optional[tk.Toplevel] = None
        self.after_id: Optional[str] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._hide()
        self.after_id = self.widget.after(650, self._show)

    def _show(self) -> None:
        self.after_id = None
        if not self.widget.winfo_exists():
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.configure(bg=T.line)
        label = tk.Label(self.window, text=self.text, bg=T.card_hi, fg=T.text,
                         padx=9, pady=6, font=(self.widget.winfo_toplevel().ui, 9))
        label.pack(padx=1, pady=1)
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.window.geometry("+%d+%d" % (x, y))

    def _hide(self, _event=None) -> None:
        if self.after_id:
            try:
                self.widget.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None
        if self.window:
            self.window.destroy()
            self.window = None


def configure_tree_table(parent: ttk.Frame, tree: ttk.Treeview) -> None:
    vertical = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
    horizontal = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
    vertical.grid(row=0, column=1, sticky="ns", padx=(0, 6), pady=8)
    horizontal.grid(row=1, column=0, sticky="ew", padx=(8, 10), pady=(0, 6))
    parent.grid_rowconfigure(0, weight=1)
    parent.grid_columnconfigure(0, weight=1)


class ToolsView(ttk.Frame):
    PAGES = {
        "overview": ("Übersicht", "Systemstatus auf einen Blick"),
        "startup": ("Autostart", "Programme beim Windows-Start verwalten"),
        "storage": ("Speicher", "Ordner und Dateitypen analysieren"),
        "monitor": ("Monitor", "Aktuelle Systemauslastung und Prozesse"),
        "network": ("Netzwerk", "Adapter und Verbindung diagnostizieren"),
        "recovery": ("Wiederherstellung", "Treiber sichern und Schutz vorbereiten"),
        "history": ("Verlauf", "Lokale Wartungsaktivitäten ansehen"),
        "apps": ("Apps", "Nutzungszeit aktiver Apps lokal erfassen"),
    }

    def __init__(self, master: tk.Misc, app: "App") -> None:
        super().__init__(master, style="Bg.TFrame")
        self.app = app
        self.frames: Dict[str, ttk.Frame] = {}
        self.pending_refresh = set()
        self._build_pages()

    def refresh_pending(self) -> None:
        page = getattr(self.app, "current_view", None)
        callbacks = {
            "overview": self.refresh_overview,
            "startup": self.refresh_startup,
            "network": self.refresh_network,
        }
        if page in self.pending_refresh and page in callbacks:
            self.pending_refresh.discard(page)
            self.app.after_idle(callbacks[page])

    def show(self, page: str) -> None:
        for view in self.frames.values():
            view.pack_forget()
        self.frames[page].pack(fill="both", expand=True)
        if page == "overview":
            self.refresh_overview()
        elif page == "startup":
            self.refresh_startup()
        elif page == "network":
            self.refresh_network()
        elif page == "history":
            self.refresh_history()
        elif page == "apps":
            self.refresh_app_usage()

    def _page(self, key: str) -> ttk.Frame:
        frame = ttk.Frame(self, style="Bg.TFrame")
        self.frames[key] = frame
        return frame

    def _build_pages(self) -> None:
        self._build_overview()
        self._build_startup()
        self._build_storage()
        self._build_monitor()
        self._build_network()
        self._build_recovery()
        self._build_history()
        self._build_apps_usage()

    def _build_overview(self) -> None:
        frame = self._page("overview")
        bar = ttk.Frame(frame, style="Bg.TFrame")
        bar.pack(fill="x", padx=24, pady=(14, 12))
        ttk.Button(bar, text="↻  Aktualisieren", style="Accent.TButton",
                   command=self.refresh_overview).pack(side="left")
        self.overview_status = ttk.Label(bar, text="Noch nicht aktualisiert",
                                         style="Muted.TLabel")
        self.overview_status.pack(side="left", padx=12)
        cards = ttk.Frame(frame, style="Bg.TFrame")
        cards.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        cards.grid_columnconfigure(0, weight=1, uniform="overview")
        cards.grid_columnconfigure(1, weight=1, uniform="overview")
        self.overview_labels: Dict[str, ttk.Label] = {}
        cards.grid_rowconfigure(2, weight=1)
        for key, title, icon, row, column in (
            ("os", "Windows", "🪟", 0, 0),
            ("cpu", "Prozessor", "🧠", 0, 1),
            ("memory", "Arbeitsspeicher", "💾", 1, 0),
            ("drives", "Laufwerke", "🗄️", 1, 1),
            ("scans", "Letzte Scans", "🕘", 2, 0),
        ):
            group = ttk.Frame(cards, style="Card.TFrame", padding=(16, 13))
            group.grid(row=row, column=column, columnspan=2 if key == "scans" else 1,
                       sticky="nsew", padx=(0, 8) if column == 0 else (8, 0),
                       pady=(0, 12))
            ttk.Label(group, text="%s  %s" % (icon, title),
                      style="H2.TLabel").pack(anchor="w")
            label = ttk.Label(group, text="Wird geladen ...", style="Muted2.TLabel",
                              wraplength=400, justify="left")
            label.pack(anchor="w", pady=(8, 0))
            self.overview_labels[key] = label

    def refresh_overview(self) -> None:
        if self.app.busy:
            self.pending_refresh.add("overview")
            return
        self.pending_refresh.discard("overview")
        self.app.set_busy(True, "Systemstatus wird geladen ...")
        def work() -> None:
            try:
                data = load_overview_data()
            except Exception as exc:
                self.app._post_ui(self._overview_failed, exc)
                return
            self.app._post_ui(self._overview_loaded, data)
        threading.Thread(target=work, daemon=True).start()

    def _overview_failed(self, exc: Exception) -> None:
        self.app.set_busy(False)
        self.overview_status.config(text="Aktualisierung fehlgeschlagen")
        self.app._schedule_alert_refresh()
        show_action_error(APP_NAME, "Systemstatus laden", exc,
                          "Prüfe, ob Windows PowerShell und die CIM-Systemabfragen verfügbar sind.",
                          self.app)

    def _overview_loaded(self, data: dict) -> None:
        self.overview_labels["os"].config(text="%s · Version %s · Build %s" % (
            data.get("OS", "Windows"), data.get("Version", "?"), data.get("Build", "?")))
        cpu_load = float(data.get("CpuLoad") or 0)
        self.overview_labels["cpu"].config(
            text="%s\nAktuelle Auslastung: %.0f%%" %
            (data.get("CPU") or "Nicht verfügbar", cpu_load),
            foreground=T.warn if cpu_load >= 90 else T.dim)
        total = int(data.get("TotalMemory") or 0)
        free = int(data.get("FreeMemory") or 0)
        self.overview_labels["memory"].config(
            text="%s von %s frei" % (human_size(free), human_size(total)))
        warnings = maintenance.system_warnings(data)
        if "wenig freier RAM" in warnings:
            self.overview_labels["memory"].config(foreground=T.warn)
        else:
            self.overview_labels["memory"].config(foreground=T.dim)
        drives = data.get("Drives") or []
        if isinstance(drives, dict):
            drives = [drives]
        if "Speicherplatz knapp" in warnings:
            self.overview_labels["drives"].config(foreground=T.warn)
        else:
            self.overview_labels["drives"].config(foreground=T.dim)
        self.overview_labels["drives"].config(text="   ·   ".join(
            "%s  %s frei von %s" % (
                d.get("Name", "?"), human_size(int(d.get("Free") or 0)),
                human_size(int(d.get("Size") or 0))) for d in drives
        ) or "Keine Laufwerke gefunden")
        status = "⚠  Aufmerksamkeit: " + ", ".join(warnings) if warnings else "✓  System wirkt im Normalbereich"
        self.overview_status.config(
            text="%s  ·  %s" % (status, time.strftime("%H:%M")),
            foreground=T.warn if warnings else T.ok)
        self.update_scan_summary()
        newly_detected = set(warnings) - self.app._active_alerts
        self.app._active_alerts = set(warnings)
        self.app.set_busy(False)
        self.app._schedule_alert_refresh()
        if newly_detected and self.app.alerts_enabled:
            details = ", ".join(sorted(newly_detected))
            self.app.record_history("Systemwarnung", "Aufmerksamkeit", details)
            messagebox.showwarning(
                APP_NAME, "Neue Systemwarnung:\n\n%s\n\nÖffne die Übersicht für Details." %
                details, parent=self.app)

    def update_scan_summary(self) -> None:
        if "scans" in self.overview_labels:
            self.overview_labels["scans"].config(
                text="Treiber: %s    ·    Dateien: %s" %
                (self.app.last_driver_scan, self.app.last_file_scan))

    def _build_startup(self) -> None:
        frame = self._page("startup")
        bar = ttk.Frame(frame, style="Bg.TFrame")
        bar.pack(fill="x", padx=24, pady=(14, 12))
        ttk.Button(bar, text="↻  Aktualisieren", style="Ghost.TButton",
                   command=self.refresh_startup).pack(side="left")
        self.startup_toggle = ttk.Button(bar, text="⏸  Deaktivieren", style="Accent.TButton",
                                         command=self.toggle_startup, state="disabled")
        self.startup_toggle.pack(side="left", padx=8)
        ttk.Label(bar, text="Änderungen betreffen nur deinen Benutzer.",
                  style="Muted.TLabel").pack(side="left", padx=8)
        self.startup_search_var = tk.StringVar()
        self.startup_search = ttk.Entry(bar, textvariable=self.startup_search_var,
                                        style="Field.TEntry", width=28)
        self.startup_search.pack(side="right")
        self.startup_search.insert(0, "🔎  Einträge filtern")
        self.startup_search.configure(foreground=T.faint)
        self.startup_search.bind("<FocusIn>", self._startup_search_focus_in)
        self.startup_search.bind("<FocusOut>", self._startup_search_focus_out)
        self.startup_search.bind("<KeyRelease>", lambda _event: self._filter_startup())
        card = ttk.Frame(frame, style="Card.TFrame")
        card.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        table = ttk.Frame(card, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        cols = ("name", "state", "source", "command")
        self.startup_tree = ttk.Treeview(table, columns=cols, show="headings")
        for col, title, width in (("name", "Eintrag", 190), ("state", "Status", 100),
                                  ("source", "Quelle", 170), ("command", "Befehl / Pfad", 420)):
            self.startup_tree.heading(col, text=title)
            self.startup_tree.column(col, width=width, anchor="w")
        configure_tree_table(table, self.startup_tree)
        self.startup_tree.bind("<<TreeviewSelect>>",
                               lambda _event: self._startup_selection_changed())
        self.startup_items: List[dict] = []

    def refresh_startup(self) -> None:
        if self.app.busy:
            self.pending_refresh.add("startup")
            return
        self.pending_refresh.discard("startup")
        self.app.set_busy(True, "Autostart-Einträge werden geladen ...")
        def work() -> None:
            try:
                items = get_startup_items()
            except Exception as exc:
                self.app._post_ui(self._startup_failed, exc)
                return
            self.app._post_ui(self._startup_loaded, items)
        threading.Thread(target=work, daemon=True).start()

    def _startup_failed(self, exc: Exception) -> None:
        self.app.set_busy(False)
        show_action_error(APP_NAME, "Autostart-Liste laden", exc,
                          "Prüfe dein Benutzerprofil und die Berechtigungen für die Registry.",
                          self.app)

    def _startup_loaded(self, items: List[dict]) -> None:
        self.startup_items = items
        self._filter_startup()
        self._startup_selection_changed()
        self.app.set_busy(False)

    def _startup_search_focus_in(self, _event=None) -> None:
        if self.startup_search.get() == "🔎  Einträge filtern":
            self.startup_search.delete(0, "end")
            self.startup_search.configure(foreground=T.text)

    def _startup_search_focus_out(self, _event=None) -> None:
        if not self.startup_search.get().strip():
            self.startup_search.insert(0, "🔎  Einträge filtern")
            self.startup_search.configure(foreground=T.faint)
        self._filter_startup()

    def _filter_startup(self) -> None:
        query = self.startup_search.get().strip().lower()
        if query == "🔎  einträge filtern":
            query = ""
        selected = self.startup_tree.selection()
        self.startup_tree.delete(*self.startup_tree.get_children())
        for i, item in enumerate(self.startup_items):
            text = "%s %s %s %s" % (
                item["name"], item["source"],
                "aktiv" if item["enabled"] else "deaktiviert", item["command"])
            if not query or query in text.lower():
                self.startup_tree.insert("", "end", iid="s%d" % i, values=(
                    item["name"], "● Aktiv" if item["enabled"] else "○ Deaktiviert",
                    item["source"], item["command"]),
                    tags=("enabled" if item["enabled"] else "disabled",))
        self.startup_tree.tag_configure("enabled", foreground=T.ok)
        self.startup_tree.tag_configure("disabled", foreground=T.faint)
        if selected and self.startup_tree.exists(selected[0]):
            self.startup_tree.selection_set(selected)
        self._startup_selection_changed()

    def _startup_selection_changed(self) -> None:
        selected = self.startup_tree.selection()
        item = self.startup_items[int(selected[0][1:])] if selected else None
        can_change = bool(item and item.get("writable"))
        self.startup_toggle.config(
            state="normal" if can_change and not self.app.busy else "disabled",
            text=("⏸  Deaktivieren" if item["enabled"] else "▶  Wieder aktivieren")
            if can_change else "Deaktivieren")

    def toggle_startup(self) -> None:
        if self.app.busy:
            return
        selected = self.startup_tree.selection()
        if not selected:
            return
        item = self.startup_items[int(selected[0][1:])]
        action = "deaktivieren" if item["enabled"] else "wieder aktivieren"
        if not messagebox.askyesno(APP_NAME, "Autostart-Eintrag „%s“ wirklich %s?" %
                                   (item["name"], action)):
            return
        try:
            set_startup_enabled(item, not item["enabled"])
        except Exception as exc:
            show_action_error(APP_NAME, "Autostart-Eintrag ändern", exc,
                              "Prüfe, ob die Datei oder der Registry-Eintrag noch vorhanden ist.",
                              self.app)
            return
        self.app.record_history("Autostart", "Geändert", "%s: %s" % (item["name"], action))
        self.refresh_startup()

    def _build_storage(self) -> None:
        frame = self._page("storage")
        bar = ttk.Frame(frame, style="Bg.TFrame")
        bar.pack(fill="x", padx=24, pady=(14, 12))
        self.storage_path = tk.StringVar(value=str(Path.home()))
        ttk.Entry(bar, textvariable=self.storage_path, style="Field.TEntry").pack(
            side="left", fill="x", expand=True)
        ttk.Button(bar, text="📂  Ordner wählen", style="Ghost.TButton",
                   command=self.choose_storage_folder).pack(side="left", padx=6)
        ttk.Button(bar, text="🔎  Analysieren", style="Accent.TButton",
                   command=self.scan_storage).pack(side="left")
        self.storage_summary = ttk.Label(frame, text="Analyse ist schreibgeschützt.",
                                         style="Muted.TLabel")
        self.storage_summary.pack(anchor="w", padx=24, pady=(0, 10))
        card = ttk.Frame(frame, style="Card.TFrame")
        card.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        table = ttk.Frame(card, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        self.storage_tree = ttk.Treeview(table, columns=("category", "size", "files"),
                                         show="headings")
        for col, title, width in (("category", "Kategorie", 420),
                                  ("size", "Belegung", 150), ("files", "Dateien", 120)):
            self.storage_tree.heading(col, text=title)
            self.storage_tree.column(col, width=width, anchor="w")
        configure_tree_table(table, self.storage_tree)

    def choose_storage_folder(self) -> None:
        folder = filedialog.askdirectory(title="Ordner für Speicheranalyse wählen",
                                         initialdir=self.storage_path.get(), mustexist=True)
        if folder:
            self.storage_path.set(folder)

    def scan_storage(self) -> None:
        if self.app.busy:
            return
        folder = Path(self.storage_path.get()).expanduser()
        self.storage_tree.delete(*self.storage_tree.get_children())
        self.app.set_busy(True, "Speicher wird analysiert ...", cancellable=True)
        def work() -> None:
            try:
                def progress(count: int, name: str) -> None:
                    self.app.report_progress(count, None, "Analysiert: %d Dateien · %s" %
                                             (count, name[:45]))
                rows, total = analyze_storage(
                    folder, progress=progress, cancel_event=self.app.cancel_event
                )
            except OperationCancelled:
                self.app._post_ui(self._operation_cancelled, "Speicheranalyse abgebrochen.")
                return
            except Exception as exc:
                self.app._post_ui(self._storage_failed, exc)
                return
            self.app._post_ui(self._storage_loaded, rows, total)
        threading.Thread(target=work, daemon=True).start()

    def _operation_cancelled(self, message: str) -> None:
        self.app.set_busy(False, "Abgebrochen")
        LOG.warn(message)
        self.storage_summary.config(text=message)

    def _storage_failed(self, exc: Exception) -> None:
        self.app.set_busy(False)
        show_action_error(APP_NAME, "Speicheranalyse", exc,
                          "Wähle einen erreichbaren Ordner, auf den du Lesezugriff hast.",
                          self.app)

    def _storage_loaded(self, rows: List[Tuple[str, int, int]], total: int) -> None:
        for name, size, count in rows:
            self.storage_tree.insert("", "end", values=(name, human_size(size), count))
        self.storage_summary.config(text="%s in den analysierten Dateien · schreibgeschützt" %
                                    human_size(total))
        self.app.set_busy(False)
        self.app.record_history("Speicheranalyse", "Abgeschlossen",
                                "%s · %d Kategorien" % (human_size(total), len(rows)))

    def _build_monitor(self) -> None:
        frame = self._page("monitor")
        bar = ttk.Frame(frame, style="Bg.TFrame")
        bar.pack(fill="x", padx=24, pady=(14, 12))
        ttk.Button(bar, text="↻  Jetzt aktualisieren", style="Accent.TButton",
                   command=self.refresh_monitor).pack(side="left")
        self.monitor_status = ttk.Label(bar, text="Manuelle Aktualisierung",
                                        style="Muted.TLabel")
        self.monitor_status.pack(side="left", padx=12)
        card = ttk.Frame(frame, style="Card.TFrame")
        card.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        self.monitor_summary = ttk.Label(card, text="Noch nicht aktualisiert",
                                         style="Muted2.TLabel")
        self.monitor_summary.pack(anchor="w", padx=12, pady=12)
        table = ttk.Frame(card, style="Card.TFrame")
        table.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.monitor_tree = ttk.Treeview(table, columns=("name", "pid", "memory"),
                                         show="headings")
        for col, title, width in (("name", "Prozess", 280), ("pid", "PID", 100),
                                  ("memory", "Arbeitsspeicher", 180)):
            self.monitor_tree.heading(col, text=title)
            self.monitor_tree.column(col, width=width, anchor="w")
        configure_tree_table(table, self.monitor_tree)

    def refresh_monitor(self) -> None:
        if self.app.busy:
            return
        self.app.set_busy(True, "Systemmonitor wird aktualisiert ...")
        def work() -> None:
            try:
                data = load_monitor_data()
            except Exception as exc:
                self.app._post_ui(self._monitor_failed, exc)
                return
            self.app._post_ui(self._monitor_loaded, data)
        threading.Thread(target=work, daemon=True).start()

    def _monitor_failed(self, exc: Exception) -> None:
        self.app.set_busy(False)
        show_action_error(APP_NAME, "Systemmonitor aktualisieren", exc,
                          "Prüfe, ob PowerShell und Windows-Systemabfragen verfügbar sind.",
                          self.app)

    def _monitor_loaded(self, data: dict) -> None:
        total, free = int(data.get("TotalMemory") or 0), int(data.get("FreeMemory") or 0)
        self.monitor_summary.config(text="CPU: %s%%    ·    RAM: %s / %s" % (
            round(float(data.get("CPU") or 0)), human_size(max(0, total - free)),
            human_size(total)))
        drives = data.get("Drives") or []
        if isinstance(drives, dict):
            drives = [drives]
        drive_summary = "   ·   ".join(
            "%s: %s frei" % (drive.get("Name", "?"),
                             human_size(int(drive.get("Free") or 0)))
            for drive in drives
        )
        if drive_summary:
            self.monitor_summary.config(
                text=self.monitor_summary.cget("text") + "    ·    " + drive_summary)
        self.monitor_tree.delete(*self.monitor_tree.get_children())
        processes = data.get("Processes") or []
        if isinstance(processes, dict):
            processes = [processes]
        for process in processes:
            self.monitor_tree.insert("", "end", values=(
                process.get("Name", "?"), process.get("PID", "?"),
                human_size(int(process.get("Memory") or 0))))
        self.monitor_status.config(text="Aktualisiert %s" % time.strftime("%H:%M:%S"))
        self.app.set_busy(False)

    def _build_network(self) -> None:
        frame = self._page("network")
        bar = ttk.Frame(frame, style="Bg.TFrame")
        bar.pack(fill="x", padx=24, pady=(14, 12))
        ttk.Button(bar, text="↻  Adapter aktualisieren", style="Ghost.TButton",
                   command=self.refresh_network).pack(side="left")
        ttk.Button(bar, text="🌐  Verbindung testen", style="Accent.TButton",
                   command=self.test_network).pack(side="left", padx=8)
        self.network_status = ttk.Label(bar, text="Noch nicht geprüft",
                                        style="Muted.TLabel", wraplength=400)
        self.network_status.pack(side="left", padx=8)
        card = ttk.Frame(frame, style="Card.TFrame")
        card.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        table = ttk.Frame(card, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        self.network_tree = ttk.Treeview(table, columns=("name", "ipv4", "gateway", "dns", "dhcp"),
                                         show="headings")
        for col, title, width in (("name", "Adapter", 230), ("ipv4", "IPv4", 160),
                                  ("gateway", "Gateway", 140), ("dns", "DNS", 180),
                                  ("dhcp", "DHCP", 80)):
            self.network_tree.heading(col, text=title)
            self.network_tree.column(col, width=width, anchor="w")
        configure_tree_table(table, self.network_tree)

    def refresh_network(self) -> None:
        if self.app.busy:
            self.pending_refresh.add("network")
            return
        self.pending_refresh.discard("network")
        self.app.set_busy(True, "Netzwerkadapter werden geladen ...")
        def work() -> None:
            try:
                items = get_network_data()
            except Exception as exc:
                self.app._post_ui(self._network_failed, exc)
                return
            self.app._post_ui(self._network_loaded, items)
        threading.Thread(target=work, daemon=True).start()

    def _network_failed(self, exc: Exception) -> None:
        self.app.set_busy(False)
        show_action_error(APP_NAME, "Netzwerkdaten laden", exc,
                          "Prüfe den Windows-Netzwerkdienst und versuche es erneut.", self.app)

    def _network_loaded(self, items: List[dict]) -> None:
        self.network_tree.delete(*self.network_tree.get_children())
        for item in items:
            self.network_tree.insert("", "end", values=(
                item.get("Name", "?"), ", ".join(item.get("IPv4") or []),
                ", ".join(item.get("Gateway") or []), ", ".join(item.get("DNS") or []),
                "Ja" if item.get("DHCP") else "Nein"))
        if not items:
            self.network_status.config(text="Keine aktiven Netzwerkadapter gefunden")
        self.app.set_busy(False)

    def test_network(self) -> None:
        if self.app.busy:
            return
        self.app.set_busy(True, "Internetverbindung wird getestet ...")
        def work() -> None:
            success, text = check_network_connection()
            self.app._post_ui(self._network_tested, success, text)
        threading.Thread(target=work, daemon=True).start()

    def _network_tested(self, success: bool, text: str) -> None:
        self.network_status.config(text=text, foreground=T.ok if success else T.warn)
        self.app.set_busy(False)
        self.app.record_history("Netzwerktest", "Erfolgreich" if success else "Fehlgeschlagen",
                                text[:300])

    def _build_recovery(self) -> None:
        frame = self._page("recovery")
        cards = ttk.Frame(frame, style="Bg.TFrame")
        cards.pack(fill="both", expand=True, padx=24, pady=(14, 8))
        cards.grid_columnconfigure(0, weight=1, uniform="recovery")
        cards.grid_columnconfigure(1, weight=1, uniform="recovery")
        cards.grid_rowconfigure(0, weight=1, uniform="recovery")
        cards.grid_rowconfigure(1, weight=1, uniform="recovery")
        card = ttk.Frame(cards, style="Card.TFrame", padding=14)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        ttk.Label(card, text="💽  Treiber-Backup", style="H2.TLabel").pack(anchor="w")
        ttk.Label(card, text="Installierte Treiberpakete in einen Ordner exportieren. "
                             "Der Export verändert keine Treiber.",
                  style="Muted2.TLabel", wraplength=340).pack(anchor="w", pady=(7, 12))
        ttk.Button(card, text="⬇  Treiber jetzt sichern", style="Accent.TButton",
                   command=self.backup_drivers).pack(anchor="w")
        restore_backup = ttk.Frame(cards, style="Card.TFrame", padding=14)
        restore_backup.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 6))
        ttk.Label(restore_backup, text="📦  Treiber aus Backup wiederherstellen",
                  style="H2.TLabel").pack(anchor="w")
        ttk.Label(restore_backup, text="Wähle den Ordner eines zuvor exportierten Backups. "
                                       "Windows fragt vor der Installation nach Administratorrechten.",
                  style="Muted2.TLabel", wraplength=340).pack(anchor="w", pady=(7, 12))
        ttk.Button(restore_backup, text="📂  Backup auswählen und wiederherstellen",
                   style="Ghost.TButton", command=self.restore_backup).pack(anchor="w")
        restore = ttk.Frame(cards, style="Card.TFrame", padding=14)
        restore.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(6, 0))
        ttk.Label(restore, text="🛡️  Wiederherstellungspunkt", style="H2.TLabel").pack(anchor="w")
        ttk.Label(restore, text="Windows erstellt einen Systemwiederherstellungspunkt. "
                                "Dafür ist eine UAC-Bestätigung nötig; Windows kann die "
                                "Erstellung bei deaktiviertem Systemschutz ablehnen.",
                  style="Muted2.TLabel", wraplength=340).pack(anchor="w", pady=(7, 12))
        ttk.Button(restore, text="＋  Wiederherstellungspunkt erstellen",
                   style="Ghost.TButton", command=self.make_restore_point).pack(anchor="w")
        report = ttk.Frame(cards, style="Card.TFrame", padding=14)
        report.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(6, 0))
        ttk.Label(report, text="📄  Diagnosebericht", style="H2.TLabel").pack(anchor="w")
        ttk.Label(report, text="Daten auswählen, vor dem Speichern prüfen und lokal als Textdatei ablegen.",
                  style="Muted2.TLabel", wraplength=340).pack(anchor="w", pady=(7, 12))
        ttk.Button(report, text="Bericht vorbereiten und ansehen",
                   style="Ghost.TButton", command=self.open_report_dialog).pack(anchor="w")
        self.recovery_status = ttk.Label(frame, text="", style="Muted.TLabel", wraplength=760)
        self.recovery_status.pack(anchor="w", padx=24, pady=8)
        ttk.Button(
            frame, text="🕘  Backups und Wartungsverlauf verwalten",
            style="Ghost.TButton", command=lambda: self.app.show_view("history"),
        ).pack(anchor="w", padx=24, pady=(0, 10))

    def _build_history(self) -> None:
        frame = self._page("history")
        bar = ttk.Frame(frame, style="Bg.TFrame")
        bar.pack(fill="x", padx=24, pady=(14, 12))
        ttk.Button(bar, text="↻  Aktualisieren", style="Accent.TButton",
                   command=self.refresh_history).pack(side="left")
        self.history_restore_button = ttk.Button(
            bar, text="📦  Ausgewähltes Backup prüfen",
            style="Ghost.TButton", command=self.restore_selected_backup,
            state="disabled",
        )
        self.history_restore_button.pack(side="left", padx=8)
        ttk.Button(bar, text="Verlauf löschen", style="Danger.TButton",
                   command=self.clear_history).pack(side="right")
        card = ttk.Frame(frame, style="Card.TFrame")
        card.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        table = ttk.Frame(card, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        self.history_tree = ttk.Treeview(
            table, columns=("time", "action", "result", "details"), show="headings",
            selectmode="browse",
        )
        for column, title, width in (
                ("time", "Zeit", 145), ("action", "Aktion", 180),
                ("result", "Ergebnis", 150), ("details", "Details / Backup-Ort", 430)):
            self.history_tree.heading(column, text=title)
            self.history_tree.column(column, width=width, anchor="w")
        configure_tree_table(table, self.history_tree)
        self.history_tree.bind(
            "<<TreeviewSelect>>", lambda _event: self._history_selection_changed())
        self.history_tree.bind("<Double-1>", lambda _event: self.restore_selected_backup())
        self.history_records: List[dict] = []

    def _build_apps_usage(self) -> None:
        frame = self._page("apps")
        bar = ttk.Frame(frame, style="Bg.TFrame")
        bar.pack(fill="x", padx=24, pady=(14, 10))
        self.usage_toggle_button = ttk.Button(
            bar, text="▶  Erfassung starten", style="Accent.TButton",
            command=self.toggle_app_usage,
        )
        self.usage_toggle_button.pack(side="left")
        ttk.Button(bar, text="↻  Aktualisieren", style="Ghost.TButton",
                   command=self.refresh_app_usage).pack(side="left", padx=8)
        ttk.Button(bar, text="Zurücksetzen", style="Danger.TButton",
                   command=self.reset_app_usage).pack(side="right")
        self.usage_status = ttk.Label(frame, text="", style="Muted.TLabel", wraplength=800)
        self.usage_status.pack(anchor="w", padx=24, pady=(0, 10))
        ttk.Label(
            frame,
            text="Datenschutz: Es werden nur Prozessnamen und aktive Vordergrundzeit "
                 "gespeichert – keine Fenstertitel, Inhalte oder Tastatureingaben. "
                 "Bei aktiver Erfassung läuft ein Hintergrundprozess auch nach dem "
                 "Schließen dieses Fensters; Zeit zwischen Messpunkten "
                 "ist auf maximal 7,5 Sekunden begrenzt.",
            style="Muted.TLabel", wraplength=820, justify="left",
        ).pack(anchor="w", padx=24, pady=(0, 10))
        card = ttk.Frame(frame, style="Card.TFrame")
        card.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        table = ttk.Frame(card, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        self.usage_tree = ttk.Treeview(
            table, columns=("application", "duration", "share"), show="headings")
        for column, title, width in (
                ("application", "App / Prozess", 380),
                ("duration", "Aktive Nutzungszeit", 200),
                ("share", "Anteil", 100)):
            self.usage_tree.heading(column, text=title)
            self.usage_tree.column(column, width=width, anchor="w")
        configure_tree_table(table, self.usage_tree)
        self._usage_refresh_id: Optional[str] = None

    def refresh_app_usage(self) -> None:
        try:
            tracker = usage_tracker.UsageTracker(USAGE_FILE)
            self.app.usage_tracker = tracker
            self.app.usage_error = ""
        except (OSError, ValueError) as exc:
            self.app.usage_error = str(exc)
            tracker = None
        self.usage_tree.delete(*self.usage_tree.get_children())
        if tracker is None:
            self.usage_status.config(
                text="Nutzungserfassung konnte nicht geladen werden: %s" %
                getattr(self.app, "usage_error", "unbekannter Fehler"))
            self.usage_toggle_button.config(text="▶  Erfassung starten")
            return
        snapshot = tracker.snapshot()
        started = snapshot["started_at"] or "noch nicht aktiviert"
        if snapshot["last_error"]:
            status = "Erfassung läuft seit %s · Messfehler: %s" % (
                started, snapshot["last_error"])
        elif snapshot["enabled"]:
            status = "Erfassung läuft seit %s · zeichnet auch nach dem Schließen im Hintergrund auf" % started
        else:
            status = "Erfassung pausiert · Start: %s" % started
        self.usage_status.config(text=status)
        self.usage_toggle_button.config(
            text="⏸  Erfassung pausieren" if snapshot["enabled"] else "▶  Erfassung starten")
        total = sum(seconds for _name, seconds in snapshot["apps"])
        for index, (name, seconds) in enumerate(snapshot["apps"]):
            share = (seconds * 100 / total) if total else 0
            self.usage_tree.insert(
                "", "end", iid="u%d" % index,
                values=(name, usage_tracker.format_duration(seconds), "%.1f%%" % share),
                tags=("odd",) if index % 2 else ("even",))
        self._schedule_usage_refresh(snapshot["enabled"])

    def _schedule_usage_refresh(self, enabled: bool) -> None:
        if self._usage_refresh_id:
            try:
                self.app.after_cancel(self._usage_refresh_id)
            except tk.TclError:
                pass
            self._usage_refresh_id = None
        if enabled and getattr(self.app, "current_view", None) == "apps":
            self._usage_refresh_id = self.app.after(5000, self.refresh_app_usage)

    def toggle_app_usage(self) -> None:
        tracker = getattr(self.app, "usage_tracker", None)
        if tracker is None:
            messagebox.showerror(
                APP_NAME, "Die lokale Nutzungsdatei konnte nicht geladen werden:\n\n%s" %
                getattr(self.app, "usage_error", "Unbekannter Fehler"), parent=self.app)
            return
        try:
            if tracker.enabled:
                _write_usage_command("pause")
                _launch_background_usage_tracker()
                _set_usage_autostart(False)
                self.app.record_history("App-Nutzung", "Pausiert")
            else:
                _write_usage_command("start")
                _set_usage_autostart(True)
                _launch_background_usage_tracker()
                self.app.record_history("App-Nutzung", "Erfassung gestartet")
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            show_action_error(APP_NAME, "App-Nutzungserfassung ändern", exc,
                              "Prüfe den Schreibzugriff auf den lokalen App-Datenordner "
                              "und die Windows-Autostart-Einstellungen.",
                              self.app)
            return
        self.usage_status.config(
            text="Hintergrundaufzeichnung wird gestartet …" if not tracker.enabled else
            "Aufzeichnung wird pausiert …")
        self.app.after(1500, self.refresh_app_usage)

    def reset_app_usage(self) -> None:
        if not messagebox.askyesno(
                APP_NAME, "Alle bisher erfassten App-Nutzungszeiten zurücksetzen? "
                "Die Erfassung bleibt danach im aktuellen Zustand.",
                parent=self.app):
            return
        tracker = getattr(self.app, "usage_tracker", None)
        if tracker is None:
            try:
                if USAGE_FILE.exists():
                    backup = USAGE_FILE.with_name(
                        "app-usage-corrupt-%d.json" % int(time.time()))
                    USAGE_FILE.replace(backup)
                self.app.usage_tracker = usage_tracker.UsageTracker(USAGE_FILE)
                self.app.usage_error = ""
                tracker = self.app.usage_tracker
            except OSError as exc:
                show_action_error(APP_NAME, "Nutzungsdaten zurücksetzen", exc,
                                  "Prüfe die Schreibrechte im lokalen App-Datenordner.",
                                  self.app)
                return
        try:
            if tracker.enabled:
                _write_usage_command("reset")
            else:
                tracker.reset()
        except OSError as exc:
            show_action_error(APP_NAME, "Nutzungsdaten zurücksetzen", exc,
                              "Prüfe die Schreibrechte im lokalen App-Datenordner.",
                              self.app)
            return
        self.app.record_history("App-Nutzung", "Zurückgesetzt")
        self.app.after(1000, self.refresh_app_usage)

    def refresh_history(self) -> None:
        try:
            self.history_records = maintenance.load_history(HISTORY_FILE)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            show_action_error(APP_NAME, "Wartungsverlauf laden", exc,
                              "Prüfe die lokale Verlaufsdatei; andere App-Funktionen bleiben verfügbar.",
                              self.app)
            return
        self.history_tree.delete(*self.history_tree.get_children())
        for index, record in enumerate(reversed(self.history_records)):
            details = record["details"]
            if record["action"] == "Treiber-Backup":
                try:
                    backup_data = json.loads(details)
                    if isinstance(backup_data, dict):
                        details = str(backup_data.get("path", details))
                except json.JSONDecodeError as exc:
                    LOG.warn("Backup-Verlaufseintrag konnte nicht gelesen werden: %s" % exc)
            self.history_tree.insert("", "end", iid="h%d" % index, values=(
                record["timestamp"], record["action"], record["result"],
                details),
                tags=("odd",) if index % 2 else ("even",))
        self._history_selection_changed()

    def _history_selection_changed(self) -> None:
        selected = self.history_tree.selection() if hasattr(self, "history_tree") else ()
        record = self.history_records[len(self.history_records) - 1 - int(selected[0][1:])] \
            if selected and self.history_records else None
        backup = False
        if record and record.get("action") == "Treiber-Backup" and record.get("result") == "Erfolgreich":
            try:
                backup_data = json.loads(record.get("details", ""))
                backup = isinstance(backup_data, dict) and isinstance(backup_data.get("path"), str)
            except json.JSONDecodeError as exc:
                LOG.warn("Backup-Verlaufseintrag konnte nicht geprüft werden: %s" % exc)
                backup = False
        self.history_restore_button.config(
            state="normal" if backup and not self.app.busy else "disabled")

    def restore_selected_backup(self) -> None:
        selected = self.history_tree.selection()
        if not selected:
            return
        record = self.history_records[len(self.history_records) - 1 - int(selected[0][1:])]
        if record.get("action") != "Treiber-Backup":
            return
        try:
            backup_data = json.loads(record.get("details", ""))
            folder = Path(backup_data["path"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            LOG.warn("Backup-Verweis im Wartungsverlauf ist ungültig: %s" % exc)
            messagebox.showerror(
                APP_NAME, "Der gespeicherte Backup-Verweis ist ungültig. "
                "Wähle den Backup-Ordner direkt im Wiederherstellungsbereich.",
                parent=self.app)
            return
        self._start_restore_preflight(folder)

    def clear_history(self) -> None:
        if not messagebox.askyesno(
                APP_NAME, "Den lokalen Wartungsverlauf einschließlich Backup-Verweisen löschen? "
                "Die gesicherten Treiberdateien bleiben erhalten."):
            return
        try:
            if HISTORY_FILE.exists():
                HISTORY_FILE.unlink()
        except OSError as exc:
            show_action_error(APP_NAME, "Wartungsverlauf löschen", exc,
                              "Prüfe die Schreibrechte für den lokalen App-Datenordner.", self.app)
            return
        self.app.record_history("Wartungsverlauf", "Geloescht", "")
        self.refresh_history()

    def backup_drivers(self) -> None:
        if self.app.busy:
            return
        parent = filedialog.askdirectory(title="Zielordner für Treiber-Backup wählen",
                                         mustexist=True)
        if not parent:
            return
        if not messagebox.askyesno(
                APP_NAME, "Ein neuer, datierter Backup-Ordner wird in „%s“ angelegt. "
                "Installierte Treiber jetzt exportieren?" % parent):
            return
        try:
            folder = str(maintenance.create_backup_directory(Path(parent)))
        except (OSError, ValueError) as exc:
            show_action_error(APP_NAME, "Backup-Ordner erstellen", exc,
                              "Wähle einen beschreibbaren Zielordner.", self.app)
            return
        self.app.set_busy(True, "Treiber werden exportiert ...", cancellable=True)
        def work() -> None:
            manifest_error = ""
            try:
                code, output = export_installed_drivers(Path(folder), self.app.cancel_event)
                if code == 0:
                    try:
                        maintenance.create_backup_manifest(
                            Path(folder), [driver.name for driver in self.app.drivers])
                    except Exception as exc:
                        manifest_error = str(exc)
            except OperationCancelled:
                self.app._post_ui(self._recovery_finished, False, "Treiberexport abgebrochen.")
                return
            except Exception as exc:
                self.app._post_ui(self._recovery_finished, False,
                                  "Treiber-Export fehlgeschlagen: %s" % exc)
                return
            if code == 0 and manifest_error:
                message = ("Treiber exportiert, aber Backup-Metadaten konnten nicht erstellt "
                           "werden: %s" % manifest_error)
            else:
                message = "Treiber-Export abgeschlossen." if code == 0 else (
                    "Treiber-Export fehlgeschlagen (Code %d): %s" % (code, output[-1200:]))
            self.app._post_ui(self._backup_export_finished, code == 0, message, folder)
        threading.Thread(target=work, daemon=True).start()

    def restore_backup(self) -> None:
        if self.app.busy:
            return
        folder = filedialog.askdirectory(title="Treiber-Backup-Ordner wählen", mustexist=True)
        if not folder:
            return
        self._start_restore_preflight(Path(folder))

    def _start_restore_preflight(self, folder: Path) -> None:
        if self.app.busy:
            return
        self.app.set_busy(True, "Backup-Dateien werden geprüft ...")
        def work() -> None:
            try:
                inspection = maintenance.inspect_driver_backup(folder)
            except Exception as exc:
                self.app._post_ui(self._backup_preflight_failed, exc)
                return
            self.app._post_ui(self._backup_preflight_ready, folder, inspection)
        threading.Thread(target=work, daemon=True).start()

    def _backup_preflight_failed(self, exc: Exception) -> None:
        self.app.set_busy(False)
        show_action_error(APP_NAME, "Treiber-Backup prüfen", exc,
                          "Wähle einen gültigen Backup-Ordner mit INF-Dateien.", self.app)

    def _backup_preflight_ready(self, folder: Path, inspection: dict) -> None:
        self.app.set_busy(False)
        if not inspection["valid"]:
            details = "\n".join(inspection["issues"][:8])
            messagebox.showerror(
                APP_NAME, "Das Backup ist beschädigt oder unvollständig und wird nicht "
                "installiert.\n\n%s" % details, parent=self.app)
            return
        drivers = inspection.get("drivers") or []
        description = (
            "Erstellt: %s\nDateien: %s · Größe: %s\nGekennzeichnete Geräte: %s" % (
                inspection["created"], inspection["file_count"],
                human_size(inspection["total_bytes"]),
                ", ".join(drivers[:8]) if drivers else "keine Geräteliste im Backup")
        )
        warning = ("\n\nDieses alte Backup enthält kein Prüfmanifest. Prüfe den Ordner "
                   "sorgfältig." if inspection["legacy"] else
                   "\n\nDateiliste, Dateigrößen und INF-Prüfsummen wurden geprüft.")
        if not messagebox.askyesno(
                APP_NAME,
                "%s\n\nTreiber aus „%s“ wirklich installieren? Das kann Gerätetreiber "
                "ändern und benötigt Administratorrechte.%s" %
                (description, folder, warning), parent=self.app):
            return
        self.app.set_busy(True, "Backup-Treiber werden wiederhergestellt ...")
        def work() -> None:
            try:
                inspection = maintenance.inspect_driver_backup(folder)
                if not inspection["valid"]:
                    raise ValueError("Das Backup hat sich seit der Prüfung verändert: %s" %
                                     "; ".join(inspection["issues"][:5]))
                code, message = restore_driver_backup(folder)
            except Exception as exc:
                self.app._post_ui(self._restore_backup_finished, False,
                                  "Treiberwiederherstellung fehlgeschlagen: %s" % exc,
                                  str(folder), "Fehlgeschlagen")
                return
            success = code in (0, 3010)
            result = "Erfolgreich" if success else (
                "Abgebrochen" if code == -1 else "Fehlgeschlagen")
            self.app._post_ui(self._restore_backup_finished, success, message,
                              str(folder), result)
        threading.Thread(target=work, daemon=True).start()

    def _backup_export_finished(self, success: bool, message: str, folder: str) -> None:
        self._recovery_finished(success, message)
        if success:
            if "Metadaten" in message:
                self.recovery_status.config(foreground=T.warn)
            self.app.record_history(
                "Treiber-Backup", "Erfolgreich",
                json.dumps({"path": str(Path(folder).resolve())}, ensure_ascii=False))
        else:
            self.app.record_history(
                "Treiber-Backup", "Fehlgeschlagen" if not success else "Metadaten unvollständig",
                str(folder))
        self.refresh_history_if_visible()

    def _restore_backup_finished(self, success: bool, message: str,
                                  folder: str, result: str) -> None:
        self._recovery_finished(success, message)
        self.app.record_history("Treiber-Wiederherstellung", result, folder)
        self.refresh_history_if_visible()

    def refresh_history_if_visible(self) -> None:
        if getattr(self.app, "current_view", None) == "history":
            self.refresh_history()

    def make_restore_point(self) -> None:
        if self.app.busy:
            return
        if not messagebox.askyesno(
            APP_NAME, "Windows um einen Wiederherstellungspunkt bitten? "
            "Windows kann dies ablehnen, wenn der Systemschutz ausgeschaltet ist."
        ):
            return
        self.app.set_busy(True, "Wiederherstellungspunkt wird vorbereitet ...")
        def work() -> None:
            try:
                code = create_restore_point()
            except Exception as exc:
                self.app._post_ui(self._recovery_finished, False, str(exc))
                return
            message = ("Wiederherstellungspunkt wurde erstellt." if code == 0 else
                       "UAC-Abfrage abgebrochen." if code == -1 else
                       "Wiederherstellungspunkt fehlgeschlagen (Code %d)." % code)
            self.app._post_ui(self._restore_point_finished, code == 0, message, code)
        threading.Thread(target=work, daemon=True).start()

    def _restore_point_finished(self, success: bool, message: str, code: int) -> None:
        self._recovery_finished(success, message)
        result = "Erfolgreich" if success else "Abgebrochen" if code == -1 else "Fehlgeschlagen"
        self.app.record_history("Wiederherstellungspunkt", result)

    def _recovery_finished(self, success: bool, message: str) -> None:
        self.recovery_status.config(text=message, foreground=T.ok if success else T.warn)
        self.app.set_busy(False)

    def open_report_dialog(self) -> None:
        dialog = tk.Toplevel(self.app)
        dialog.title("Diagnosebericht vorbereiten")
        dialog.configure(bg=T.bg)
        dialog.transient(self.app)
        dialog.geometry("760x620")
        dialog.minsize(600, 460)
        outer = ttk.Frame(dialog, style="Bg.TFrame", padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Diagnosebericht", style="H1.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Wähle Inhalte. Netzwerkadressen und Autostart-Namen können persönlich sein – "
                 "prüfe die Vorschau vor dem lokalen Speichern. Es wird nichts übertragen.",
            style="Muted.TLabel", wraplength=700,
        ).pack(anchor="w", pady=(6, 12))
        options = ttk.Frame(outer, style="Card.TFrame", padding=10)
        options.pack(fill="x")
        variables = {
            "overview": tk.BooleanVar(value=True),
            "drivers": tk.BooleanVar(value=False),
            "network": tk.BooleanVar(value=False),
            "startup": tk.BooleanVar(value=False),
        }
        for key, label in (
            ("overview", "🖥  System und Laufwerke"),
            ("drivers", "🚘  Installierte Treiber (ohne Hardware-IDs)"),
            ("network", "🌐  Netzwerkadapter und IP-Adressen"),
            ("startup", "🚀  Autostart-Namen und Status"),
        ):
            ttk.Checkbutton(options, text=label, variable=variables[key]).pack(anchor="w", pady=2)
        preview = tk.Text(outer, wrap="word", bg=T.card, fg=T.text,
                          insertbackground=T.accent, selectbackground=T.sel,
                          relief="flat", padx=12, pady=10, font=(self.app.ui, 9))
        preview.pack(fill="both", expand=True, pady=12)
        preview.insert("1.0", "Wähle die gewünschten Abschnitte und klicke auf „Vorschau erstellen“.")
        buttons = ttk.Frame(outer, style="Bg.TFrame")
        buttons.pack(fill="x")
        save_button = ttk.Button(buttons, text="💾  Lokal speichern", style="Accent.TButton",
                                 state="disabled")
        save_button.pack(side="right")
        ttk.Button(buttons, text="Vorschau erstellen", style="Ghost.TButton",
                   command=lambda: self._generate_report(dialog, variables, preview,
                                                        save_button)).pack(side="left")
        ttk.Button(buttons, text="Schließen", style="Ghost.TButton",
                   command=dialog.destroy).pack(side="right", padx=8)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def _generate_report(self, dialog: tk.Toplevel, variables: dict,
                         preview: tk.Text, save_button: ttk.Button) -> None:
        selected = [key for key, value in variables.items() if value.get()]
        if not selected:
            messagebox.showinfo(APP_NAME, "Wähle mindestens einen Abschnitt für den Bericht.",
                                parent=dialog)
            return
        self.app.set_busy(True, "Berichtsdaten werden gesammelt ...", cancellable=True)
        save_button.config(state="disabled")
        def work() -> None:
            sections = {}
            collectors = {
                "overview": load_overview_data,
                "drivers": lambda: [d for d in scan_installed() if d.is_vendor],
                "network": get_network_data,
                "startup": get_startup_items,
            }
            try:
                for index, key in enumerate(selected, 1):
                    if self.app.cancel_event.is_set():
                        raise OperationCancelled("Berichtserstellung wurde abgebrochen.")
                    self.app.report_progress(index - 1, len(selected),
                                             "Bericht: %s" % key)
                    sections[key] = collectors[key]()
                report_text = build_diagnostic_report(sections)
            except OperationCancelled as exc:
                self.app._post_ui(self._report_cancelled, dialog, str(exc))
                return
            except Exception as exc:
                self.app._post_ui(self._report_failed, dialog, exc)
                return
            self.app._post_ui(self._report_ready, dialog, preview, save_button, report_text)
        threading.Thread(target=work, daemon=True).start()

    def _report_cancelled(self, dialog: tk.Toplevel, message: str) -> None:
        self.app.set_busy(False, "Bericht abgebrochen")
        messagebox.showinfo(APP_NAME, message, parent=dialog)

    def _report_failed(self, dialog: tk.Toplevel, exc: Exception) -> None:
        self.app.set_busy(False)
        show_action_error(APP_NAME, "Diagnosebericht erstellen", exc,
                          "Prüfe die Berechtigungen und versuche es erneut.", dialog)

    def _report_ready(self, dialog: tk.Toplevel, preview: tk.Text,
                      save_button: ttk.Button, report_text: str) -> None:
        self.app.set_busy(False)
        preview.configure(state="normal")
        preview.delete("1.0", "end")
        preview.insert("1.0", report_text)
        preview.configure(state="disabled")
        save_button.configure(state="normal", command=lambda: self._save_report(
            dialog, report_text))

    def _save_report(self, dialog: tk.Toplevel, report_text: str) -> None:
        filename = filedialog.asksaveasfilename(
            parent=dialog, title="Diagnosebericht lokal speichern",
            defaultextension=".txt", filetypes=(("Textdatei", "*.txt"),),
        )
        if not filename:
            return
        try:
            Path(filename).write_text(report_text, encoding="utf-8")
        except OSError as exc:
            show_action_error(APP_NAME, "Diagnosebericht speichern", exc,
                              "Wähle einen beschreibbaren lokalen Ordner.", dialog)
            return
        self.app.record_history("Diagnosebericht", "Lokal gespeichert", Path(filename).name)
        self.recovery_status.config(text="Diagnosebericht lokal gespeichert: %s" % filename,
                                    foreground=T.ok)
        messagebox.showinfo(APP_NAME, "Diagnosebericht lokal gespeichert.", parent=dialog)


class DriversView(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "App") -> None:
        super().__init__(master, style="Bg.TFrame")
        self.app = app
        self.rows: Dict[str, dict] = {}
        self._display_order: List[str] = []
        self._sort_reverse: Dict[str, bool] = {}
        self._build()

    def _build(self) -> None:
        # Werkzeugleiste
        bar = ttk.Frame(self, style="Bg.TFrame")
        bar.pack(fill="x", padx=24, pady=(14, 14))

        self.search = tk.Entry(bar, bd=0, highlightthickness=1, highlightbackground=T.line,
                               highlightcolor=T.accent, bg=T.card_hi, fg=T.text,
                               insertbackground=T.accent, font=(self.app.ui, 10), relief="flat",
                               selectbackground=T.sel, selectforeground=T.text)
        self.search.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 4))
        self.search_placeholder = "Gerät, Hersteller oder Status suchen"
        self.search.insert(0, self.search_placeholder)
        self.search.configure(fg=T.faint)
        self.search.bind("<FocusIn>", self._search_focus_in)
        self.search.bind("<FocusOut>", self._search_focus_out)
        self.search.bind("<KeyRelease>", lambda e: self._filter())
        self.search.bind("<Escape>", lambda e: (self.search.delete(0, "end"), self._filter()))

        self.btn_scan = ttk.Button(bar, text="🔎  Updates suchen", style="Accent.TButton",
                                   command=self.app.scan_updates)
        self.btn_scan.pack(side="left", padx=(10, 0))
        self.btn_install = ttk.Button(bar, text="⬇  Installieren", style="Ghost.TButton",
                                      command=self.app.install_selected, state="disabled")
        self.btn_install.pack(side="left", padx=(6, 0))
        self.btn_clear = ttk.Button(bar, text="Auswahl leeren", style="Ghost.TButton",
                                    command=self._clear_selection)
        self.btn_clear.pack(side="left", padx=(6, 0))

        self.progress = ttk.Progressbar(bar, mode="determinate", style="Accent.Horizontal.TProgressbar",
                                        length=180)
        self.progress.pack(side="left", padx=(12, 0))
        self.count_lbl = ttk.Label(bar, text="", style="Muted.TLabel")
        self.count_lbl.pack(side="left", padx=(10, 0))

        # Tabelle
        card = ttk.Frame(self, style="Card.TFrame")
        card.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        cols = ("sel", "name", "ver", "neu", "datum", "mfr", "status")
        self.tree = ttk.Treeview(card, columns=cols, show="headings", selectmode="extended")
        heads = {
            "sel": ("", 34), "name": ("Gerät", 290), "ver": ("Installiert", 118),
            "neu": ("Verfuegbar", 118), "datum": ("Datum", 96), "mfr": ("Hersteller", 150),
            "status": ("Status", 132),
        }
        for c, (txt, w) in heads.items():
            self.tree.heading(c, text=txt, command=lambda cc=c: self._sort_by(cc))
            self.tree.column(c, width=w, anchor="w")
        self.tree.column("sel", anchor="center", stretch=False)
        for c in ("ver", "neu", "datum"):
            self.tree.column(c, anchor="center")
        vs = ttk.Scrollbar(card, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(card, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        vs.grid(row=0, column=1, sticky="ns", pady=8, padx=(0, 6))
        hs.grid(row=1, column=0, sticky="ew", padx=(8, 10), pady=(0, 6))
        card.grid_rowconfigure(0, weight=1)
        card.grid_columnconfigure(0, weight=1)

        self.tree.tag_configure("odd", background=T.stripe)
        self.tree.tag_configure("even", background=T.card)
        self.tree.tag_configure("upd", foreground=T.text)
        self.tree.tag_configure("cur", foreground=T.dim)
        self.tree.tag_configure("fail", foreground=T.warn)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", lambda e: self._on_click(e, toggle=True))
        self.tree.bind("<Control-a>", lambda e: self._select_all())

    # -- Interaktion --------------------------------------------------------
    def _search_focus_in(self, _event) -> None:
        if self.search.get() == self.search_placeholder:
            self.search.delete(0, "end")
            self.search.configure(fg=T.text)

    def _search_focus_out(self, _event) -> None:
        if not self.search.get().strip():
            self.search.insert(0, self.search_placeholder)
            self.search.configure(fg=T.faint)
            self._filter()

    def _on_click(self, event, toggle: bool = False) -> str:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return ""
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if row and col == "#1":
            if not toggle:
                self.toggle(row)
            return "break"
        if toggle and row:
            self.toggle(row)
            return "break"
        return ""

    def toggle(self, iid: str) -> None:
        if self.app.busy:
            return
        if not self.rows.get(iid, {}).get("update"):
            return
        vals = list(self.tree.item(iid, "values"))
        if not vals:
            return
        vals[0] = "☐" if vals[0] == "☑" else "☑"
        self.tree.item(iid, values=vals, tags=self._tags_for(iid))
        self.app.refresh_action_state()

    def _select_all(self) -> str:
        if self.app.busy:
            return "break"
        for iid in self.tree.get_children():
            if self.rows.get(iid, {}).get("update"):
                self.tree.item(iid, values=["☑"] + list(self.tree.item(iid, "values"))[1:],
                               tags=self._tags_for(iid))
        self.app.refresh_action_state()
        return "break"

    def _clear_selection(self) -> None:
        if self.app.busy:
            return
        for iid in self.rows:
            values = list(self.tree.item(iid, "values"))
            if values and self.rows[iid].get("update"):
                values[0] = "☐"
                self.tree.item(iid, values=values, tags=self._tags_for(iid))
        self.app.refresh_action_state()

    def _sort_by(self, col: str) -> None:
        order = {"name": 1, "ver": 2, "neu": 3, "datum": 4, "mfr": 5, "status": 6}.get(col, 1)
        reverse = self._sort_reverse.get(col, False)

        def key(iid: str):
            values = self.tree.item(iid, "values")
            value = str(values[order]) if values else ""
            if col in ("ver", "neu"):
                return version_key(value)
            return value.lower()

        self._display_order.sort(key=key, reverse=reverse)
        for index, iid in enumerate(self._display_order):
            self.rows[iid]["index"] = index
        self._sort_reverse[col] = not reverse
        self._filter()

    def _tags_for(self, iid: str) -> Tuple[str, ...]:
        info = self.rows.get(iid, {})
        index = info.get("index", 0)
        state = str(info.get("status", "")).lower()
        status_tag = "fail" if "fehlgeschlagen" in state else ("upd" if info.get("update") else "cur")
        return (status_tag,) + (
            ("odd",) if index % 2 else ("even",)
        )

    def _filter(self) -> None:
        raw_term = self.search.get().strip()
        term = "" if raw_term == self.search_placeholder else raw_term.lower()
        for iid in self._display_order:
            self.tree.detach(iid)
        for index, iid in enumerate(self._display_order):
            info = self.rows.get(iid, {})
            hay = "%s %s %s %s" % (
                info.get("name", ""), info.get("mfr", ""), info.get("status", ""),
                info.get("driver").version if info.get("driver") else "",
            )
            self.tree.item(iid, tags=("fail" if "fehlgeschlagen" in str(
                info.get("status", "")).lower() else
                ("upd" if info.get("update") else "cur"),) +
                (("odd",) if index % 2 else ("even",)))
            if not term or term in hay.lower():
                self.tree.move(iid, "", "end")

    # -- Daten --------------------------------------------------------------
    def set_rows(self, drivers: List[InstalledDriver], updates: Dict[str, PreparedUpdate],
                 statuses: Dict[str, str]) -> None:
        selected: Dict[str, bool] = {}
        for iid, info in self.rows.items():
            driver = info.get("driver")
            values = self.tree.item(iid, "values")
            if driver and values:
                selected[driver.device_id] = values[0] == "☑"
        if self.rows:
            self.tree.delete(*self.rows.keys())
        self.rows.clear()
        self._display_order = []
        for idx, d in enumerate(drivers):
            upd = updates.get(d.device_id)
            state = statuses.get(d.device_id, "Installiert")
            if upd:
                mark = "☑" if selected.get(d.device_id, True) else "☐"
                vals = (mark, d.name, d.version, upd.new_version, upd.entry.date or "-",
                        d.manufacturer, state)
            else:
                vals = ("☐", d.name, d.version, "-", d.date or "-", d.manufacturer, state)
            iid = "d%d" % idx
            self.rows[iid] = {"name": d.name, "mfr": d.manufacturer, "status": state,
                              "update": upd, "driver": d, "index": idx}
            self._display_order.append(iid)
            self.tree.insert("", "end", iid=iid, values=vals,
                             tags=self._tags_for(iid))
        self._filter()
        self.app.refresh_action_state()


class FilesView(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "App") -> None:
        super().__init__(master, style="Bg.TFrame")
        self.app = app
        self.entries: List[FileEntry] = []
        self._build()

    def _build(self) -> None:
        opts = ttk.Frame(self, style="Card.TFrame")
        opts.pack(fill="x", padx=24, pady=(14, 12))

        row1 = ttk.Frame(opts, style="Card.TFrame")
        row1.pack(fill="x", padx=17, pady=(13, 6))
        ttk.Button(row1, text="📂  Ordner wählen", style="Ghost.TButton",
                   command=self.choose_folder).pack(side="left")
        self.folder_lbl = ttk.Label(row1, text="Kein Ordner gewaehlt", style="Muted.TLabel")
        self.folder_lbl.pack(side="left", padx=10)

        row2 = ttk.Frame(opts, style="Card.TFrame")
        row2.pack(fill="x", padx=17, pady=(4, 5))
        self.chk_dupes_var = tk.BooleanVar(value=True)
        self.chk_dupes = tk.Checkbutton(row2, text="Duplikate finden", bg=T.card, fg=T.text,
                                        selectcolor=T.card_hi, activebackground=T.card,
                                        activeforeground=T.text, bd=0, highlightthickness=0,
                                        font=(self.app.ui, 10), variable=self.chk_dupes_var,
                                        onvalue=True, offvalue=False, anchor="w")
        self.chk_dupes.pack(side="left")

        ttk.Label(row2, text="Älter als (Tage)", style="Muted.TLabel").pack(side="left", padx=(20, 4))
        self.age_var = tk.StringVar(value="0")
        ttk.Entry(row2, textvariable=self.age_var, width=7, style="Field.TEntry").pack(side="left")
        ttk.Label(row2, text="Min. Größe (KB)", style="Muted.TLabel").pack(side="left", padx=(20, 4))
        self.size_var = tk.StringVar(value="0")
        ttk.Entry(row2, textvariable=self.size_var, width=7, style="Field.TEntry").pack(side="left")
        row3 = ttk.Frame(opts, style="Card.TFrame")
        row3.pack(fill="x", padx=17, pady=(3, 5))
        ttk.Label(row3, text="Dateitypen (z. B. .tmp, .log)", style="Muted2.TLabel").pack(side="left")
        self.ext_var = tk.StringVar(value="")
        ttk.Entry(row3, textvariable=self.ext_var, width=22, style="Field.TEntry").pack(side="left", padx=(8, 0))
        row4 = ttk.Frame(opts, style="Card.TFrame")
        row4.pack(fill="x", padx=17, pady=(3, 15))
        self.chk_skip_var = tk.BooleanVar(value=True)
        self.chk_skip = tk.Checkbutton(row4, text="Systemordner überspringen", bg=T.card,
                                       fg=T.text, selectcolor=T.card_hi,
                                       activebackground=T.card, activeforeground=T.text, bd=0,
                                       highlightthickness=0, font=(self.app.ui, 10),
                                       variable=self.chk_skip_var, onvalue=True, offvalue=False)
        self.chk_skip.pack(side="left")
        self.btn_scan = ttk.Button(row4, text="🔎  Ordner analysieren", style="Accent.TButton",
                                   command=self.start_scan)
        self.btn_scan.pack(side="left", padx=(16, 0))

        card = ttk.Frame(self, style="Card.TFrame")
        card.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        cols = ("sel", "path", "size", "reason")
        self.tree = ttk.Treeview(card, columns=cols, show="headings", selectmode="extended")
        for c, (txt, w) in {"sel": ("", 34), "path": ("Pfad", 620), "size": ("Größe", 100),
                            "reason": ("Grund", 190)}.items():
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w")
        self.tree.column("sel", anchor="center", stretch=False)
        self.tree.column("size", anchor="e")
        vs = ttk.Scrollbar(card, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(card, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        vs.grid(row=0, column=1, sticky="ns", pady=8, padx=(0, 6))
        hs.grid(row=1, column=0, sticky="ew", padx=(8, 10), pady=(0, 6))
        card.grid_rowconfigure(0, weight=1)
        card.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("odd", background=T.stripe)
        self.tree.tag_configure("even", background=T.card)
        self.tree.bind("<Button-1>", self._on_click)

        actions = ttk.Frame(self, style="Bg.TFrame")
        actions.pack(fill="x", padx=24, pady=(0, 18))
        self.btn_recycle = ttk.Button(actions, text="♻  In den Papierkorb", style="Accent.TButton",
                                      command=lambda: self.delete(False), state="disabled")
        self.btn_recycle.pack(side="left")
        self.btn_perm = ttk.Button(actions, text="🗑  Endgültig löschen", style="Danger.TButton",
                                   command=lambda: self.delete(True), state="disabled")
        self.btn_perm.pack(side="left", padx=8)
        self.btn_dupes = ttk.Button(actions, text="☑  Duplikate markieren", style="Ghost.TButton",
                                    command=self._select_duplicates)
        self.btn_dupes.pack(side="left", padx=(12, 0))
        self.btn_clear = ttk.Button(actions, text="Auswahl leeren", style="Ghost.TButton",
                                    command=self._clear_selection)
        self.btn_clear.pack(side="left", padx=6)
        self.sum_lbl = ttk.Label(actions, text="", style="Muted.TLabel")
        self.sum_lbl.pack(side="left", padx=10)

    def _on_click(self, event) -> str:
        if self.app.busy:
            return "break"
        if self.tree.identify_region(event.x, event.y) != "cell":
            return ""
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if row and col == "#1":
            vals = list(self.tree.item(row, "values"))
            vals[0] = "☐" if vals[0] == "☑" else "☑"
            self.tree.item(row, values=vals)
            self._update_summary()
            return "break"
        return ""

    def _select_duplicates(self) -> None:
        if self.app.busy:
            return
        for idx, entry in enumerate(self.entries):
            iid = "f%d" % idx
            if self.tree.exists(iid):
                vals = list(self.tree.item(iid, "values"))
                vals[0] = "☑" if entry.reason == "Duplikat" and not entry.keep else "☐"
                self.tree.item(iid, values=vals)
        self._update_summary()

    def _clear_selection(self) -> None:
        if self.app.busy:
            return
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, "values"))
            vals[0] = "☐"
            self.tree.item(iid, values=vals)
        self._update_summary()

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(
            title="Ordner fuer die Bereinigung waehlen",
            initialdir=getattr(self, "folder", str(Path.home())),
            mustexist=True,
        )
        if not folder:
            return
        self.folder = folder
        self.folder_lbl.config(text=folder, wraplength=720)
        LOG.info("Ordner gewaehlt: %s" % folder)

    def start_scan(self) -> None:
        folder = getattr(self, "folder", None)
        if not folder:
            messagebox.showinfo(APP_NAME, "Bitte zuerst einen Ordner waehlen.")
            return
        try:
            age = int(self.age_var.get() or 0)
            minsize = int(self.size_var.get() or 0)
        except ValueError:
            messagebox.showerror(APP_NAME, "Zahlenwerte sind ungueltig.")
            return
        if age < 0 or minsize < 0:
            messagebox.showerror(APP_NAME, "Alter und Mindestgroesse muessen 0 oder groesser sein.")
            return
        exts = self.ext_var.get()
        find_dupes = bool(self.chk_dupes_var.get())
        skip = bool(self.chk_skip_var.get())
        if not find_dupes and age == 0:
            messagebox.showinfo(APP_NAME, "Bitte Duplikatsuche aktivieren oder ein Mindestalter angeben.")
            return
        self.tree.delete(*self.tree.get_children())
        self.entries = []
        self.btn_recycle.config(state="disabled")
        self.btn_perm.config(state="disabled")
        self.app.set_busy(True, "Scanne Dateien ...", cancellable=True)

        def work() -> None:
            def prog(n: int, name: str) -> None:
                self.app.report_progress(n, None, "Scanne Datei %d · %s" %
                                         (n, name[:44]))
            try:
                res = FileCleaner().scan(
                    Path(folder), find_dupes, age, minsize, exts, skip, prog,
                    cancel_event=self.app.cancel_event,
                )
            except OperationCancelled:
                self.app._post_ui(self._scan_cancelled)
                return
            except Exception as exc:
                LOG.err("Scan fehlgeschlagen: %s" % exc)
                self.app._post_ui(self._scan_failed, exc)
                return
            self.app._post_ui(self._populate, res)

        threading.Thread(target=work, daemon=True).start()

    def _scan_cancelled(self) -> None:
        self.app.set_busy(False, "Scan abgebrochen")
        self.sum_lbl.config(text="Scan abgebrochen – keine Dateien wurden verändert.")
        LOG.warn("Datei-Scan abgebrochen.")

    def _scan_failed(self, exc: Exception) -> None:
        self.app.set_busy(False)
        show_action_error(APP_NAME, "Datei-Scan", exc,
                          "Prüfe den gewählten Ordner sowie die Lesezugriffsrechte.", self.app)

    def _populate(self, res: List[FileEntry]) -> None:
        self.entries = res
        self.tree.delete(*self.tree.get_children())
        for idx, e in enumerate(res):
            self.tree.insert("", "end", iid="f%d" % idx,
                             values=("☐" if e.keep else "☑", e.path, human_size(e.size), e.reason),
                             tags=("odd",) if idx % 2 else ("even",))
        self._update_summary()
        self.app.set_busy(False)
        self.app.record_scan("files", "%d Dateien gefunden" % len(res))
        LOG.ok("Scan fertig: %d Dateien" % len(res))

    def _update_summary(self) -> None:
        picked = [
            entry for idx, entry in enumerate(self.entries)
            if self.tree.exists("f%d" % idx)
            and self.tree.item("f%d" % idx, "values")[0] == "☑"
        ]
        total = sum(entry.size for entry in picked)
        self.sum_lbl.config(text="%d Dateien markiert - %s" % (len(picked), human_size(total)))
        state = "normal" if picked and not self.app.busy else "disabled"
        self.btn_recycle.config(state=state)
        self.btn_perm.config(state=state)

    def delete(self, permanent: bool) -> None:
        chosen: List[FileEntry] = []
        for iid in self.tree.get_children():
            if self.tree.item(iid, "values")[0] == "☑":
                chosen.append(self.entries[int(iid[1:])])
        if not chosen:
            return
        total = sum(e.size for e in chosen)
        preview = "\n".join("• %s" % e.path[:150] for e in chosen[:5])
        if len(chosen) > 5:
            preview += "\n• … und %d weitere Datei(en)" % (len(chosen) - 5)
        if permanent:
            prompt = ("%d Datei(en) · %s\n\n%s\n\nDiese Dateien werden endgültig gelöscht "
                      "und können nicht wiederhergestellt werden. Fortfahren?" %
                      (len(chosen), human_size(total), preview))
            if not messagebox.askyesno(APP_NAME, prompt, parent=self.app):
                return
        else:
            prompt = ("%d Datei(en) · %s\n\n%s\n\nDie Dateien werden in den Papierkorb "
                      "verschoben und können dort wiederhergestellt werden. Fortfahren?" %
                      (len(chosen), human_size(total), preview))
            if not messagebox.askyesno(APP_NAME, prompt, parent=self.app):
                return
        self.app.set_busy(True, "Ausgewählte Dateien werden geprüft ...")

        def work() -> None:
            try:
                ok, failed = maintenance.cleanup_files(
                    [e.path for e in chosen], Path(self.folder), permanent, send_to_recycle)
            except Exception as exc:
                failed = [e.path for e in chosen]
                ok = 0
                LOG.err("Dateiaktion fehlgeschlagen: %s" % exc)
            if ok:
                LOG.ok("%d Datei(en) %s" % (
                    ok, "endgültig gelöscht" if permanent else "in den Papierkorb verschoben"))
            for failed_path in failed[:10]:
                LOG.err("Datei konnte nicht sicher entfernt werden: %s" % failed_path)
            failed_paths = set(failed)
            remaining = [e for e in self.entries if e.path in failed_paths]
            self.app._post_ui(self._refresh_after_delete, remaining, permanent, ok, len(failed))

        threading.Thread(target=work, daemon=True).start()

    def _refresh_after_delete(self, remaining: List[FileEntry], permanent: bool,
                              succeeded: int, failed: int) -> None:
        self.app.set_busy(False)
        self.app.record_history(
            "Dateibereinigung",
            "Abgeschlossen" if failed == 0 else "Teilweise fehlgeschlagen",
            "%d entfernt · %d fehlgeschlagen · %s" %
            (succeeded, failed, "endgültig" if permanent else "Papierkorb"))
        self.tree.delete(*self.tree.get_children())
        self.entries = remaining
        for idx, entry in enumerate(remaining):
            self.tree.insert("", "end", iid="f%d" % idx,
                             values=("☑", entry.path, human_size(entry.size), entry.reason),
                             tags=("odd",) if idx % 2 else ("even",))
        self._update_summary()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("%s  %s" % (APP_NAME, APP_VERSION))
        self.geometry("1280x820")
        self.minsize(1040, 680)
        self.configure(bg=T.bg)

        self.ui = pick_font(self, "Segoe UI Variable Text", "Segoe UI", "Tahoma")
        self.title_font = pick_font(self, "Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI")
        self.mono = pick_font(self, "Cascadia Mono", "Consolas", "Courier New")
        self.emoji = pick_font(self, "Segoe UI Emoji", "Segoe UI Symbol", self.ui)
        self.theme_name = "dark"
        self.font_scale = 1.0
        self.start_view = "overview"
        self.beginner_mode = False
        self.alerts_enabled = False
        self._active_alerts = set()
        self._alert_refresh_id: Optional[str] = None
        self.usage_tracker: Optional[usage_tracker.UsageTracker] = None
        self.usage_error = ""
        self._base_font_specs: Dict[tk.Misc, Tuple[str, int, str, str]] = {}

        self._init_style()

        self.drivers: List[InstalledDriver] = []
        self.updates: Dict[str, PreparedUpdate] = {}
        self.statuses: Dict[str, str] = {}
        self.busy = False
        self.cancel_event = threading.Event()
        self.operation_started_at = 0.0
        self._scan_thread: Optional[threading.Thread] = None
        self.drivers_loaded = False
        self.startup_screen: Optional[tk.Frame] = None
        self.startup_progress: Optional[ttk.Progressbar] = None
        self.startup_animation_done = False
        self.startup_drivers_done = False
        self.last_driver_scan = "Noch nie"
        self.last_file_scan = "Noch nie"

        self._build_ui()
        LOG.bind(lambda lvl, m: self._post_ui(self.log_panel.write, lvl, m))
        self._init_usage_tracker()
        self._load_config()
        LOG.info("%s %s bereit." % (APP_NAME, APP_VERSION))
        LOG.info("Adminrechte: %s" % ("vorhanden" if is_admin() else "nicht vorhanden (Installation fragt nach)"))
        self._show_startup_screen()
        self.after(400, self.load_drivers)

    def _show_startup_screen(self) -> None:
        screen = tk.Frame(self, bg=T.bg)
        screen.place(relwidth=1, relheight=1)
        screen.lift()

        content = tk.Frame(
            screen, bg=T.card, padx=44, pady=38,
            highlightthickness=1, highlightbackground=T.line,
        )
        content.place(relx=0.5, rely=0.5, anchor="center")
        logo = tk.Canvas(
            content, width=360, height=110, bg=T.card,
            highlightthickness=0, bd=0,
        )
        logo.pack()
        self.startup_logo = logo
        self.startup_logo_text = logo.create_text(
            180, 52, text="N", fill=T.accent,
            font=(self.title_font, 54, "bold"),
        )
        self.startup_logo_glow = logo.create_text(
            180, 52, text="N", fill=T.accent_soft,
            font=(self.title_font, 60, "bold"),
        )
        logo.tag_lower(self.startup_logo_glow, self.startup_logo_text)
        self.startup_logo_beam = logo.create_rectangle(
            -40, 0, -12, 110, fill=T.accent, outline="",
        )
        logo.tag_lower(self.startup_logo_beam, self.startup_logo_glow)
        self.startup_logo_stage = 0
        self.after(40, self._animate_startup_logo)
        self.after(80, self._play_startup_sound)

        tk.Label(
            content, text="System wird vorbereitet", bg=T.card, fg=T.text,
            font=(self.title_font, 18, "bold"),
        ).pack(pady=(6, 6))
        tk.Label(
            content, text="Installierte Treiber werden eingelesen …",
            bg=T.card, fg=T.dim, font=(self.ui, 10),
        ).pack(pady=(0, 22))

        progress = ttk.Progressbar(
            content, mode="indeterminate", style="Accent.Horizontal.TProgressbar",
            length=300,
        )
        progress.pack(fill="x")
        progress.start(12)
        self.startup_screen = screen
        self.startup_progress = progress

    def _animate_startup_logo(self) -> None:
        if not self.startup_screen or not self.startup_logo:
            return
        logo = self.startup_logo

        def pulse(frame: int = 0) -> None:
            if not self.startup_screen or not self.startup_logo:
                return
            size = 54 + (12 if frame % 2 == 0 else 0)
            logo.itemconfigure(self.startup_logo_text, font=(self.title_font, size, "bold"))
            logo.itemconfigure(self.startup_logo_glow, font=(self.title_font, size + 6, "bold"))
            if frame < 7:
                self.after(90, lambda: pulse(frame + 1))
            else:
                reveal(0)

        def reveal(index: int) -> None:
            if not self.startup_screen or not self.startup_logo:
                return
            stages = ("N", "NA", "NAN", "NANO")
            text = stages[index]
            logo.itemconfigure(self.startup_logo_text, text=text, font=(self.title_font, 44, "bold"))
            logo.itemconfigure(self.startup_logo_glow, text=text, font=(self.title_font, 50, "bold"))
            self.startup_logo_stage = index
            if index < len(stages) - 1:
                self.after(190, lambda: reveal(index + 1))
            else:
                sweep(0)

        def sweep(frame: int) -> None:
            if not self.startup_screen or not self.startup_logo:
                return
            x = -40 + frame * 18
            logo.coords(self.startup_logo_beam, x, 0, x + 24, 110)
            if frame < 23:
                self.after(20, lambda: sweep(frame + 1))
            else:
                self.startup_animation_done = True
                self._finish_startup_screen()

        pulse()

    def _play_startup_sound(self) -> None:
        if winsound is None:
            return

        def play() -> None:
            try:
                for frequency, duration in ((440, 90), (660, 90), (880, 150)):
                    winsound.Beep(frequency, duration)
            except OSError as exc:
                LOG.warn("Startsound konnte nicht abgespielt werden: %s" % exc)

        threading.Thread(target=play, daemon=True).start()

    def _hide_startup_screen(self) -> None:
        self.startup_drivers_done = True
        self._finish_startup_screen()

    def _finish_startup_screen(self) -> None:
        if not self.startup_animation_done or not self.startup_drivers_done:
            return
        if self.startup_progress:
            self.startup_progress.stop()
            self.startup_progress = None
        if self.startup_screen:
            self.startup_screen.destroy()
            self.startup_screen = None

    def _init_usage_tracker(self) -> None:
        try:
            self.usage_tracker = usage_tracker.UsageTracker(USAGE_FILE)
        except (OSError, ValueError) as exc:
            self.usage_error = str(exc)
            LOG.err("App-Nutzungsdaten konnten nicht geladen werden: %s" % exc)
            return
        if self.usage_tracker.enabled:
            try:
                _set_usage_autostart(True)
                _launch_background_usage_tracker()
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                self.usage_error = str(exc)
                LOG.err("Hintergrundaufzeichnung konnte nicht gestartet werden: %s" % exc)
        else:
            try:
                _set_usage_autostart(False)
            except (OSError, RuntimeError) as exc:
                self.usage_error = str(exc)
                LOG.err("Hintergrund-Autostart konnte nicht deaktiviert werden: %s" % exc)

    # -- Styling ------------------------------------------------------------
    def _init_style(self) -> None:
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=T.bg, foreground=T.text,
                    font=(self.ui, max(8, round(10 * self.font_scale))))
        s.configure("Bg.TFrame", background=T.bg)
        s.configure("Card.TFrame", background=T.card, relief="solid", borderwidth=1,
                    bordercolor=T.line)
        s.configure("H1.TLabel", background=T.bg, foreground=T.text,
                    font=(self.title_font, max(16, round(20 * self.font_scale)), "bold"))
        s.configure("Muted.TLabel", background=T.bg, foreground=T.dim,
                    font=(self.ui, max(8, round(9 * self.font_scale))))
        s.configure("Muted2.TLabel", background=T.card, foreground=T.dim,
                    font=(self.ui, max(9, round(10 * self.font_scale))))
        s.configure("H2.TLabel", background=T.card, foreground=T.text,
                    font=(self.title_font, max(9, round(11 * self.font_scale)), "bold"))
        s.configure("Field.TEntry", fieldbackground=T.card_hi, foreground=T.text,
                    insertcolor=T.accent, borderwidth=1, relief="flat",
                    bordercolor=T.line, padding=(10, 8))
        s.configure("Accent.TButton", background=T.accent, foreground=T.bg, borderwidth=0,
                    focuscolor=T.accent, padding=(14, 9),
                    font=(self.ui, max(9, round(10 * self.font_scale)), "bold"))
        s.map("Accent.TButton", background=[("active", T.accent_hover), ("disabled", T.card_hi)],
              foreground=[("disabled", T.faint)])
        s.configure("Ghost.TButton", background=T.card_hi, foreground=T.text, borderwidth=1,
                    bordercolor=T.line, relief="solid", padding=(13, 8),
                    font=(self.ui, max(9, round(10 * self.font_scale)), "bold"))
        s.map("Ghost.TButton", background=[("active", T.card_hover)],
              foreground=[("disabled", T.faint)],
              bordercolor=[("!disabled", T.line)])
        s.configure("Danger.TButton", background=T.danger, foreground=T.danger_text,
                    borderwidth=1, bordercolor=T.danger_border, relief="solid",
                    padding=(13, 9),
                    font=(self.ui, max(9, round(10 * self.font_scale)), "bold"))
        s.map("Danger.TButton", background=[("active", T.err), ("disabled", T.card_hi)],
              foreground=[("disabled", T.faint)],
              bordercolor=[("!disabled", T.danger_border)])
        s.configure("Accent.Horizontal.TProgressbar", troughcolor=T.card_hi, background=T.accent,
                    borderwidth=0, thickness=8)
        s.configure("Treeview", background=T.card, fieldbackground=T.card, foreground=T.text,
                    borderwidth=0, rowheight=max(30, round(34 * self.font_scale)),
                    font=(self.ui, max(9, round(10 * self.font_scale))), padding=(5, 2))
        s.map("Treeview", background=[("selected", T.sel)], foreground=[("selected", T.text)])
        s.configure("Treeview.Heading", background=T.card_hi, foreground=T.dim,
                    borderwidth=0, relief="flat", padding=(10, 9),
                    font=(self.ui, max(8, round(9 * self.font_scale)), "bold"))
        s.map("Treeview.Heading", background=[("active", "#2b3b53")],
              foreground=[("active", T.text)])
        s.configure("TScrollbar", background=T.card_hi, troughcolor=T.card, borderwidth=0,
                    arrowcolor=T.dim, arrowsize=12)
        s.configure("TProgressbar", background=T.accent, troughcolor=T.card_hi, borderwidth=0)

    def apply_theme(self, name: str) -> None:
        palette = THEME_PALETTES.get(name)
        if palette is None:
            raise ValueError("Unbekanntes Farbschema: %s" % name)
        old = {key: getattr(T, key) for key in palette}
        for key, value in palette.items():
            setattr(T, key, value)
        self.theme_name = name
        LEVELS.update({"info": T.dim, "ok": T.ok, "warn": T.warn,
                       "err": T.err, "cmd": T.accent_2})
        self.configure(bg=T.bg)
        self._init_style()

        def recolor(widget: tk.Misc) -> None:
            options = {}
            for option in ("background", "foreground", "highlightbackground",
                           "highlightcolor", "insertbackground", "selectbackground",
                           "selectforeground", "activebackground", "activeforeground"):
                try:
                    current = widget.cget(option)
                except tk.TclError:
                    continue
                replacement = next((new for key, previous in old.items()
                                    for new in (palette[key],) if current == previous), None)
                if replacement:
                    options[option] = replacement
            if options:
                try:
                    widget.configure(**options)
                except tk.TclError:
                    pass
            if isinstance(widget, tk.Text):
                for level, color in LEVELS.items():
                    widget.tag_configure(level, foreground=color)
                widget.tag_configure("ts", foreground=T.faint)
            if isinstance(widget, ttk.Treeview):
                widget.tag_configure("odd", background=T.stripe)
                widget.tag_configure("even", background=T.card)
                widget.tag_configure("enabled", foreground=T.ok)
                widget.tag_configure("disabled", foreground=T.faint)
                widget.tag_configure("upd", foreground=T.text)
                widget.tag_configure("cur", foreground=T.dim)
                widget.tag_configure("fail", foreground=T.warn)
            for child in widget.winfo_children():
                recolor(child)
        recolor(self)
        if hasattr(self, "drivers_view"):
            self.drivers_view.tree.tag_configure("upd", foreground=T.text)
            self.drivers_view.tree.tag_configure("cur", foreground=T.dim)
            self.drivers_view.tree.tag_configure("fail", foreground=T.warn)
        self.set_status("Bereit", T.dim)

    def apply_font_scale(self, scale: float) -> None:
        self.font_scale = scale
        self._init_style()
        for widget in self._walk_widgets(self):
            try:
                raw_font = widget.cget("font")
                if not raw_font or str(raw_font).startswith("Tk"):
                    continue
                base = self._base_font_specs.get(widget)
                if base is None:
                    font = tkfont.Font(root=self, font=raw_font)
                    base = (
                        str(font.actual("family")), abs(int(font.actual("size"))),
                        str(font.actual("weight")), str(font.actual("slant")),
                    )
                    self._base_font_specs[widget] = base
                family, size, weight, slant = base
                widget.configure(font=(family, max(8, round(size * scale)), weight, slant))
            except (tk.TclError, TypeError, ValueError):
                continue

    @staticmethod
    def _walk_widgets(widget: tk.Misc):
        yield widget
        for child in widget.winfo_children():
            yield from App._walk_widgets(child)

    def open_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Einstellungen")
        dialog.configure(bg=T.bg)
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        outer = ttk.Frame(dialog, style="Bg.TFrame", padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Einstellungen", style="H1.TLabel").pack(anchor="w")

        theme_labels = {label: key for key, label in THEME_LABELS.items()}
        theme_var = tk.StringVar(value=THEME_LABELS.get(self.theme_name, THEME_LABELS["dark"]))
        ttk.Label(outer, text="Farbschema", style="Muted.TLabel").pack(
            anchor="w", pady=(16, 4))
        theme_row = ttk.Frame(outer, style="Bg.TFrame")
        theme_row.pack(fill="x")
        theme_picker = ttk.Combobox(
            theme_row, textvariable=theme_var, state="readonly",
            values=tuple(THEME_LABELS.values()), width=24,
        )
        theme_picker.pack(side="left")
        preview = tk.Frame(theme_row, bg=T.card, padx=3, pady=3)
        preview.pack(side="left", padx=(10, 0))
        preview_swatches = [
            tk.Frame(preview, width=22, height=18, bg=color)
            for color in (T.bg, T.accent, T.accent_2)
        ]
        for swatch in preview_swatches:
            swatch.pack(side="left", padx=2)
            swatch.pack_propagate(False)

        def update_theme_preview(_event=None) -> None:
            theme_key = theme_labels.get(theme_var.get())
            if theme_key:
                palette = THEME_PALETTES[theme_key]
                for swatch, color in zip(
                        preview_swatches,
                        (palette["bg"], palette["accent"], palette["accent_2"])):
                    swatch.configure(bg=color)

        theme_picker.bind("<<ComboboxSelected>>", update_theme_preview)

        scale_var = tk.StringVar(value=str(self.font_scale))
        ttk.Label(outer, text="Schriftgröße", style="Muted.TLabel").pack(
            anchor="w", pady=(14, 4))
        scale_labels = {"90% – Kompakt": "0.9", "100% – Standard": "1.0",
                        "110% – Groß": "1.1", "120% – Sehr groß": "1.2"}
        current_scale_label = next(
            (label for label, value in scale_labels.items()
             if float(value) == self.font_scale), "100% – Standard"
        )
        scale_var.set(current_scale_label)
        ttk.Combobox(outer, textvariable=scale_var, state="readonly",
                     values=tuple(scale_labels), width=22).pack(anchor="w")

        start_labels = {
            "overview": "Übersicht", "drivers": "Treiber", "files": "Dateien",
            "startup": "Autostart", "storage": "Speicher", "monitor": "Monitor",
            "network": "Netzwerk", "recovery": "Wiederherstellung",
            "history": "Verlauf", "apps": "App-Nutzung", "apps": "App-Nutzung",
        }
        start_var = tk.StringVar(value=start_labels.get(self.start_view, "Übersicht"))
        ttk.Label(outer, text="Startbereich", style="Muted.TLabel").pack(
            anchor="w", pady=(14, 4))
        ttk.Combobox(
            outer, textvariable=start_var, state="readonly",
            values=tuple(start_labels.values()), width=22,
        ).pack(anchor="w")

        beginner_var = tk.BooleanVar(value=self.beginner_mode)
        ttk.Checkbutton(
            outer, text="Einfacher Modus mit Erklärungen", variable=beginner_var,
        ).pack(anchor="w", pady=(14, 2))
        ttk.Label(
            outer, text="Zeigt zusätzliche Hinweise zu Zweck und möglichen Auswirkungen.",
            style="Muted.TLabel", wraplength=360,
        ).pack(anchor="w")
        alerts_var = tk.BooleanVar(value=self.alerts_enabled)
        ttk.Checkbutton(
            outer, text="Systemwarnungen automatisch anzeigen", variable=alerts_var,
        ).pack(anchor="w", pady=(10, 2))
        ttk.Label(
            outer, text="Prüft die Übersicht alle 5 Minuten, solange sie geöffnet ist. "
                        "Warnungen erscheinen nur bei neuem RAM-, CPU- oder Laufwerksproblem.",
            style="Muted.TLabel", wraplength=360,
        ).pack(anchor="w")
        footer = ttk.Frame(outer, style="Bg.TFrame")
        footer.pack(fill="x", pady=(20, 0))
        ttk.Button(footer, text="Abbrechen", style="Ghost.TButton",
                   command=dialog.destroy).pack(side="right")
        def save() -> None:
            try:
                scale = float(scale_labels[scale_var.get()])
                self.start_view = next(
                    key for key, label in start_labels.items() if label == start_var.get()
                )
            except (KeyError, ValueError, StopIteration):
                show_action_error(APP_NAME, "Einstellungen speichern",
                                  ValueError("Schriftgröße oder Startbereich ist ungültig."),
                                  "Wähle gültige Werte aus den Listen.", dialog)
                return
            self.apply_theme(theme_labels[theme_var.get()])
            self.apply_font_scale(scale)
            self.beginner_mode = bool(beginner_var.get())
            alerts_were_enabled = self.alerts_enabled
            self.alerts_enabled = bool(alerts_var.get())
            if self.alerts_enabled and not alerts_were_enabled:
                self._active_alerts.clear()
            self._schedule_alert_refresh()
            self.h_sub.config(text=self._view_description(self.current_view))
            self._save_config()
            dialog.destroy()
        ttk.Button(footer, text="✓  Übernehmen", style="Accent.TButton",
                   command=save).pack(side="right", padx=8)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.bind("<Return>", lambda _event: save())
        dialog.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry("+%d+%d" % (x, y))

    # -- Layout -------------------------------------------------------------
    def _build_ui(self) -> None:
        self.sidebar = tk.Frame(self, bg=T.card, width=236,
                                highlightthickness=1, highlightbackground=T.line)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand = tk.Frame(self.sidebar, bg=T.card)
        brand.pack(fill="x", padx=22, pady=(24, 19))
        tk.Label(brand, text="🛠  SYSTEM", bg=T.card, fg=T.accent,
                 font=(self.title_font, 10, "bold")).pack(anchor="w")
        tk.Label(brand, text="CONTROL", bg=T.card, fg=T.text,
                 font=(self.title_font, 19, "bold")).pack(anchor="w", pady=(1, 0))
        tk.Label(brand, text="DEVICE MANAGEMENT", bg=T.card, fg=T.faint,
                 font=(self.ui, 8, "bold")).pack(anchor="w", pady=(4, 0))

        self.nav_items: List[Tuple[tk.Frame, tk.Label, str, Callable[[], None]]] = []
        self.nav_icons: Dict[tk.Frame, tk.Label] = {}
        navigation = [
            ("START", [("overview", "Übersicht", "🏠")]),
            ("VERWALTEN", [("drivers", "Treiber", "🚘"),
                           ("files", "Dateien", "📁")]),
            ("SYSTEM", [("startup", "Autostart", "🚀"),
                        ("storage", "Speicher", "💾"),
                        ("monitor", "Monitor", "📊"),
                        ("apps", "App-Nutzung", "⏱️"),
                        ("network", "Netzwerk", "🌐"),
                        ("recovery", "Wiederherstellung", "🛡️"),
                        ("history", "Verlauf", "🕘")]),
        ]
        for section, items in navigation:
            tk.Label(self.sidebar, text=section, bg=T.card, fg=T.faint,
                     anchor="w", font=(self.ui, 8, "bold")).pack(
                         fill="x", padx=22, pady=(8, 5))
            for key, label, glyph in items:
                cb = lambda view=key: self.show_view(view)
                row = tk.Frame(self.sidebar, bg=T.card, height=40)
                row.pack(fill="x", padx=10, pady=2)
                row.pack_propagate(False)
                bar = tk.Frame(row, bg=T.accent, width=3)
                bar.place(x=0, y=7, height=26)
                icon = tk.Label(row, text=glyph, bg=T.card, fg=T.dim, width=3,
                                anchor="center", font=(self.emoji, 12))
                icon.pack(side="left", padx=(5, 3), pady=5)
                lbl = tk.Label(row, text=label, bg=T.card, fg=T.dim,
                               anchor="w", font=(self.ui, 10, "bold"), takefocus=True,
                               highlightthickness=1, highlightbackground=T.card,
                               highlightcolor=T.accent)
                lbl.pack(side="left", fill="both", expand=True, padx=(2, 4))
                self.nav_icons[row] = icon
                for widget in (row, icon, lbl):
                    widget.bind("<Enter>", lambda _e, r=row, l=lbl:
                                self._nav_hover(r, l, True))
                    widget.bind("<Leave>", lambda _e, r=row, l=lbl:
                                self._nav_hover(r, l, False))
                    widget.bind("<Button-1>", lambda _e, c=cb: c())
                lbl.bind("<Return>", lambda _e, c=cb: c())
                lbl.bind("<space>", lambda _e, c=cb: c())
                lbl.bind("<FocusIn>", lambda _e, r=row, l=lbl:
                         self._nav_focus(r, l, True))
                lbl.bind("<FocusOut>", lambda _e, r=row, l=lbl:
                         self._nav_focus(r, l, False))
                self.nav_items.append((row, lbl, key, cb))

        foot = tk.Frame(self.sidebar, bg=T.card)
        foot.pack(side="bottom", fill="x", padx=18, pady=16)
        self.dot = tk.Label(foot, text="●", bg=T.card, fg=T.ok, font=(self.ui, 9))
        self.dot.pack(side="left")
        self.foot_lbl = tk.Label(foot, text="Bereit", bg=T.card, fg=T.faint, font=(self.ui, 9))
        self.foot_lbl.pack(side="left", padx=(6, 0))
        tk.Label(self.sidebar, text="v%s" % APP_VERSION, bg=T.card, fg=T.faint,
                 font=(self.ui, 8)).pack(side="bottom", pady=(0, 8))

        right = tk.Frame(self, bg=T.bg)
        right.pack(side="left", fill="both", expand=True)

        self.header = tk.Frame(right, bg=T.bg, height=78)
        self.header.pack(fill="x", padx=26, pady=(15, 2))
        self.header.pack_propagate(False)
        heading = tk.Frame(self.header, bg=T.bg)
        heading.pack(side="left", fill="y", pady=(2, 6))
        self.h_title = tk.Label(heading, text="Übersicht", bg=T.bg, fg=T.text,
                                anchor="w", font=(self.title_font, 20, "bold"))
        self.h_title.pack(anchor="w")
        self.h_sub = tk.Label(heading, text="Systemstatus und Schnellzugriff",
                              bg=T.bg, fg=T.faint, anchor="w", font=(self.ui, 9))
        self.h_sub.pack(anchor="w", pady=(2, 0))

        self.pill = tk.Label(self.header, text="●  Bereit", bg=T.accent_soft, fg=T.accent,
                             font=(self.ui, 9, "bold"), padx=12, pady=7,
                             highlightthickness=1, highlightbackground=T.line)
        self.pill.pack(side="right", pady=17)
        self.settings_button = ttk.Button(
            self.header, text="⚙  Einstellungen", style="Ghost.TButton",
            command=self.open_settings,
        )
        self.settings_button.pack(side="right", padx=10, pady=17)

        self.activity = tk.Frame(right, bg=T.bg)
        self.activity.pack(fill="x", padx=26, pady=(0, 5))
        self.activity_label = tk.Label(self.activity, text="", bg=T.bg, fg=T.dim,
                                       anchor="w", font=(self.ui, 9))
        self.activity_label.pack(side="left", padx=(0, 10))
        self.activity_progress = ttk.Progressbar(
            self.activity, mode="indeterminate", style="Accent.Horizontal.TProgressbar"
        )
        self.activity_progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.cancel_button = ttk.Button(
            self.activity, text="✕  Abbrechen", style="Ghost.TButton",
            command=self.cancel_operation,
        )
        self.cancel_button.pack(side="right")
        self.activity.pack_forget()

        self.body = tk.Frame(right, bg=T.bg)
        self.body.pack(fill="both", expand=True)
        self.drivers_view = DriversView(self.body, self)
        self.files_view = FilesView(self.body, self)
        self.tools_view = ToolsView(self.body, self)
        self.drivers_view.pack(fill="both", expand=True)
        self.files_view.pack_forget()
        self.tools_view.pack_forget()

        logwrap = tk.Frame(right, bg=T.bg)
        logwrap.pack(fill="x", padx=26, pady=(0, 14))
        logwrap.pack_propagate(False)
        logwrap.configure(height=148)
        tk.Label(logwrap, text="AKTIVITÄT", bg=T.bg, fg=T.faint,
                 font=(self.ui, 8, "bold"), anchor="w").pack(fill="x", pady=(0, 5))
        self.log_panel = LogPanel(logwrap, self)
        self.log_panel.pack(fill="both", expand=True)

        self.show_view("overview")
        self.bind("<F5>", lambda _event: self.refresh_current_view())
        self.bind("<Escape>", lambda _event: self.cancel_operation() if self.busy else None)
        self.bind("<Control-f>", lambda _event: self.focus_search())
        self._install_tooltips()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _install_tooltips(self) -> None:
        tips = (
            (self.settings_button, "Farbschema, Schriftgröße und Startbereich anpassen."),
            (self.drivers_view.btn_scan, "Installierte Geräte im Microsoft Update Catalog prüfen."),
            (self.drivers_view.btn_install, "Ausgewählte, passende Treiberupdates installieren."),
            (self.files_view.btn_scan, "Dateien im gewählten Ordner suchen. Es wird nichts automatisch gelöscht."),
            (self.files_view.btn_perm, "Markierte Dateien endgültig löschen – nicht rückgängig zu machen."),
            (self.tools_view.startup_toggle, "Ausgewählten Benutzer-Autostarteintrag deaktivieren oder zurückholen."),
            (self.cancel_button, "Laufende, abbrechbare Scans oder Exporte anhalten."),
        )
        self.tooltips = [Tooltip(widget, text) for widget, text in tips]

    def focus_search(self) -> None:
        if self.current_view == "drivers":
            self.drivers_view.search.focus_set()
            self.drivers_view._search_focus_in()
        elif self.current_view == "startup":
            self.tools_view.startup_search.focus_set()
            self.tools_view._startup_search_focus_in()

    def refresh_current_view(self) -> None:
        if self.current_view == "drivers":
            self.scan_updates()
        elif self.current_view == "files":
            self.files_view.start_scan()
        elif self.current_view == "overview":
            self.tools_view.refresh_overview()
        elif self.current_view == "startup":
            self.tools_view.refresh_startup()
        elif self.current_view == "storage":
            self.tools_view.scan_storage()
        elif self.current_view == "monitor":
            self.tools_view.refresh_monitor()
        elif self.current_view == "network":
            self.tools_view.refresh_network()
        elif self.current_view == "history":
            self.tools_view.refresh_history()
        elif self.current_view == "apps":
            self.tools_view.refresh_app_usage()

    def _nav_hover(self, row: tk.Frame, lbl: tk.Label, entering: bool) -> None:
        active = getattr(self, "_active_nav", None)
        if row is active:
            return
        bg = T.card_hi if entering else T.card
        row.configure(bg=bg)
        lbl.configure(fg=T.text if entering else T.dim, bg=bg)
        self.nav_icons[row].configure(fg=T.accent if entering else T.dim, bg=bg)

    def _nav_focus(self, row: tk.Frame, lbl: tk.Label, focused: bool) -> None:
        if focused:
            if row is self._active_nav:
                lbl.configure(highlightbackground=T.accent, fg=T.accent)
                return
            row.configure(bg=T.card_hi)
            lbl.configure(bg=T.card_hi, fg=T.text, highlightbackground=T.accent)
            self.nav_icons[row].configure(bg=T.card_hi, fg=T.accent)
        else:
            if row is self._active_nav:
                lbl.configure(highlightbackground=T.accent, bg=T.accent_soft, fg=T.accent)
                self.nav_icons[row].configure(bg=T.accent_soft, fg=T.accent)
                return
            lbl.configure(highlightbackground=T.card)
            self._nav_hover(row, lbl, False)

    def show_view(self, which: str) -> None:
        if which not in ("drivers", "files", *ToolsView.PAGES):
            which = "overview"
        self._active_nav = None
        nav_names = {
            "overview": ("Übersicht", "Systemstatus und Schnellzugriff"),
            "drivers": ("Treiber", "Updates aus dem Microsoft Update Catalog"),
            "files": ("Dateien", "Duplikate und alte Dateien bereinigen"),
            **{key: value for key, value in ToolsView.PAGES.items() if key != "overview"},
        }
        for row, lbl, key, _cb in self.nav_items:
            if key == which:
                row.configure(bg=T.accent_soft)
                lbl.configure(fg=T.accent, bg=T.accent_soft, highlightbackground=T.accent)
                self.nav_icons[row].configure(fg=T.accent, bg=T.accent_soft)
                for child in row.winfo_children():
                    if isinstance(child, tk.Frame):
                        child.place(x=0, y=7, height=26)
                self._active_nav = row
            else:
                row.configure(bg=T.card)
                lbl.configure(fg=T.dim, bg=T.card, highlightbackground=T.card)
                self.nav_icons[row].configure(fg=T.dim, bg=T.card)
                for child in row.winfo_children():
                    if isinstance(child, tk.Frame):
                        child.place_forget()
        self.drivers_view.pack_forget()
        self.files_view.pack_forget()
        self.tools_view.pack_forget()
        if which == "drivers":
            self.drivers_view.pack(fill="both", expand=True)
        elif which == "files":
            self.files_view.pack(fill="both", expand=True)
        else:
            if which not in ToolsView.PAGES:
                which = "overview"
            self.tools_view.pack(fill="both", expand=True)
            self.tools_view.show(which)
        title, subtitle = nav_names.get(which, nav_names["overview"])
        self.h_title.config(text=title)
        self.h_sub.config(text=self._view_description(which, subtitle))
        self.current_view = which
        self._schedule_alert_refresh()

    def _view_description(self, which: str, fallback: str = "") -> str:
        if self.beginner_mode:
            explanations = {
                "overview": "Zeigt den Zustand von Windows. Warnwerte sind Hinweise, keine automatische Reparatur.",
                "drivers": "Prüft passende Updates. Treiber werden erst nach deiner Bestätigung installiert.",
                "files": "Analysiert nur den gewählten Ordner. Dateien werden erst nach Auswahl gelöscht.",
                "startup": "Verwaltet Programme, die bei deiner Anmeldung starten.",
                "storage": "Liest Dateigrößen im gewählten Ordner; dabei wird nichts verändert.",
                "monitor": "Zeigt momentane Auslastung. Beenden von Prozessen erfolgt nicht automatisch.",
                "apps": "Erfasst auf Wunsch lokal, welche App im Vordergrund ist und wie lange. Nur solange diese App läuft.",
                "network": "Liest Adapterdaten und testet eine HTTPS-Verbindung.",
                "recovery": "Sichert Treiber oder erstellt Schutzpunkte; Wiederherstellungen verlangen Bestätigung.",
                "history": "Zeigt lokale Wartungsaktionen und gespeicherte Backup-Verweise.",
            }
            return explanations.get(which, fallback)
        return fallback or {
            "overview": "Systemstatus und Schnellzugriff",
            "drivers": "Updates aus dem Microsoft Update Catalog",
            "files": "Duplikate und alte Dateien bereinigen",
            "apps": "Nutzungszeit aktiver Apps lokal erfassen",
            **{key: value[1] for key, value in ToolsView.PAGES.items()},
        }.get(which, "")

    def _post_ui(self, callback: Callable, *args) -> bool:
        try:
            self.after(0, callback, *args)
            return True
        except tk.TclError:
            return False

    def _schedule_alert_refresh(self) -> None:
        if self._alert_refresh_id:
            try:
                self.after_cancel(self._alert_refresh_id)
            except tk.TclError:
                pass
            self._alert_refresh_id = None
        if self.alerts_enabled and getattr(self, "current_view", None) == "overview":
            self._alert_refresh_id = self.after(300000, self._run_alert_refresh)

    def _run_alert_refresh(self) -> None:
        self._alert_refresh_id = None
        if self.alerts_enabled and self.current_view == "overview":
            if self.busy:
                self._schedule_alert_refresh()
            else:
                self.tools_view.refresh_overview()

    # -- Status -------------------------------------------------------------
    def set_status(self, text: str, color: str = T.dim) -> None:
        bg = T.accent_soft if color == T.accent else T.card_hi
        self.pill.config(text="●  %s" % text, fg=color, bg=bg)
        self.foot_lbl.config(text=text)

    def record_scan(self, scan_type: str, details: str = "") -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        if scan_type == "drivers":
            self.last_driver_scan = stamp
            action = "Treiber-Scan"
        elif scan_type == "files":
            self.last_file_scan = stamp
            action = "Datei-Scan"
        else:
            action = "System-Scan"
        self.record_history(action, "Abgeschlossen", details)
        if getattr(self, "current_view", None) == "overview":
            self.tools_view.update_scan_summary()

    def record_history(self, action: str, result: str, details: str = "") -> None:
        try:
            maintenance.append_history(HISTORY_FILE, action, result, details)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOG.warn("Wartungseintrag konnte nicht gespeichert werden: %s" % exc)
            return
        if getattr(self, "current_view", None) == "history" and hasattr(self, "tools_view"):
            self.tools_view.refresh_history()

    def set_busy(self, busy: bool, text: str = "Bereit",
                 cancellable: bool = False) -> None:
        was_busy = self.busy
        self.busy = busy
        if busy and not was_busy:
            self.cancel_event.clear()
            self.operation_started_at = time.monotonic()
        self.drivers_view.btn_scan.config(state="disabled" if busy else "normal")
        self.drivers_view.btn_clear.config(state="disabled" if busy else "normal")
        if busy:
            self.drivers_view.btn_install.config(state="disabled")
        self.files_view.btn_scan.config(state="disabled" if busy else "normal")
        self.files_view.btn_dupes.config(state="disabled" if busy else "normal")
        self.files_view.btn_clear.config(state="disabled" if busy else "normal")
        if not busy:
            self.refresh_action_state()
        self.files_view._update_summary()
        self.set_status(text, T.accent if busy else T.dim)
        self.dot.config(fg=T.accent if busy else T.ok)
        if busy:
            self.activity_label.config(text=text)
            self.activity.pack(fill="x", padx=26, pady=(0, 8), before=self.body)
            self.cancel_button.config(text="✕  Abbrechen")
            if not was_busy:
                self.activity_progress.stop()
                self.activity_progress.configure(mode="indeterminate", value=0)
            if self.activity_progress["mode"] == "indeterminate":
                self.activity_progress.start(12)
            self.cancel_button.config(state="normal" if cancellable else "disabled")
        else:
            self.activity_progress.stop()
            self.activity_progress.configure(mode="indeterminate", value=0)
            self.activity.pack_forget()
            self.cancel_button.config(text="✕  Abbrechen", state="normal")
            self.cancel_event.clear()
        if not busy and hasattr(self, "tools_view"):
            self.tools_view.refresh_pending()

    def report_progress(self, current: int, total: Optional[int], text: str) -> None:
        def update() -> None:
            if not self.busy or self.cancel_event.is_set():
                return
            if total and total > 0:
                elapsed = max(0.0, time.monotonic() - self.operation_started_at)
                remaining = elapsed * max(0, total - current) / max(current, 1)
                eta = ""
                if current > 0:
                    eta = " · noch ca. %s" % (
                        "%d s" % remaining if remaining < 60 else
                        "%d min" % (remaining / 60))
                self.activity_label.config(text=text + eta)
                self.activity_progress.stop()
                self.activity_progress.configure(mode="determinate",
                                                 maximum=total, value=min(current, total))
            else:
                self.activity_label.config(text=text)
                if self.activity_progress["mode"] != "indeterminate":
                    self.activity_progress.configure(mode="indeterminate")
                    self.activity_progress.start(12)
        self._post_ui(update)

    def cancel_operation(self) -> None:
        if not self.busy or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.cancel_button.config(state="disabled", text="⏳  Wird beendet …")
        self.activity_label.config(text="Abbruch angefordert – laufender Schritt wird beendet …")
        self.set_status("Abbruch angefordert", T.warn)

    def refresh_action_state(self) -> None:
        if self.busy:
            return
        n = 0
        for iid in self.drivers_view.rows:
            vals = self.drivers_view.tree.item(iid, "values")
            if vals and vals[0] == "☑" and self.drivers_view.rows.get(iid, {}).get("update"):
                n += 1
        self.drivers_view.btn_install.config(state="normal" if n else "disabled")
        self.drivers_view.count_lbl.config(text=("%d Update(s) markiert" % n) if n else "")

    # -- Treiberlogik -------------------------------------------------------
    def load_drivers(self) -> None:
        if self.busy:
            self.after(350, self.load_drivers)
            return
        self.set_busy(True, "Lese Geraetetreiber ...")

        def work() -> None:
            try:
                devs = scan_installed()
                vendor = [d for d in devs if d.is_vendor]
                vendor.sort(key=lambda d: (d.device_class, d.name))
            except Exception as exc:
                self._post_ui(self._drivers_load_failed, exc)
                return
            self._post_ui(self._drivers_loaded, vendor, len(devs))

        threading.Thread(target=work, daemon=True).start()

    def _drivers_loaded(self, vendor: List[InstalledDriver], total: int) -> None:
        self._hide_startup_screen()
        if self.cancel_event.is_set():
            self.set_busy(False, "Treiberlesen abgebrochen")
            LOG.warn("Treiberlesen abgebrochen.")
            return
        self.drivers = vendor
        self.drivers_loaded = True
        self.record_scan("drivers", "%d Herstellertreiber von %d Geräten" %
                         (len(vendor), total))
        self.drivers_view.set_rows(vendor, self.updates, self.statuses)
        self.set_busy(False)
        if vendor:
            LOG.ok("%d Herstellertreiber von %d Geraeten geladen." % (len(vendor), total))
        else:
            LOG.warn("Keine Herstellertreiber gefunden (Geraete insgesamt: %d)." % total)
            self.set_status("Keine Herstellertreiber", T.warn)

    def _drivers_load_failed(self, exc: Exception) -> None:
        self._hide_startup_screen()
        self.set_busy(False)
        LOG.err("Treiber konnten nicht geladen werden: %s" % exc)
        show_action_error(APP_NAME, "Installierte Treiber lesen", exc,
                          "Prüfe, ob PowerShell und der Windows-Treiberdienst verfügbar sind.",
                          self)

    def scan_updates(self) -> None:
        if self.busy:
            return
        if not self.drivers_loaded:
            self.load_drivers()
            return
        if not self.drivers:
            messagebox.showinfo(APP_NAME, "Es wurden keine passenden Herstellertreiber gefunden.")
            return
        self.set_busy(True, "Suche im Update Catalog ...", cancellable=True)

        def work() -> None:
            found: Dict[str, PreparedUpdate] = {}
            errors: Dict[str, str] = {}
            done = [0]
            lock = threading.Lock()

            # identische Treiber (gleiche INF/Version) nur einmal recherchieren
            groups: Dict[tuple, List[InstalledDriver]] = {}
            for dev in self.drivers:
                key = (dev.manufacturer.lower(), dev.inf.lower(), dev.version)
                groups.setdefault(key, []).append(dev)
            try:
                engine = UpdateEngine(
                    CatalogClient(), Path(tempfile.gettempdir()) / "SystemUpdaterCleaner" / "pkgs"
                )
            except Exception as exc:
                self._post_ui(self._driver_scan_failed, exc)
                return
            LOG.info("Katalog-Suche fuer %d Treiber (Feature-Release %s) ..." %
                     (len(self.drivers), engine.release))
            self._post_ui(self._update_driver_scan_progress, 0, len(groups))

            def check(group: List[InstalledDriver]) -> None:
                if self.cancel_event.is_set():
                    raise OperationCancelled("Treiberprüfung wurde abgebrochen.")
                dev = group[0]
                prep = None
                failure = ""
                try:
                    self.report_progress(
                        0, None, "Suche im Update-Katalog: %s" % dev.name[:48]
                    )
                    cands = engine.candidates(dev, cancel_event=self.cancel_event)
                    for cand in cands[:6]:
                        if self.cancel_event.is_set():
                            raise OperationCancelled("Treiberprüfung wurde abgebrochen.")
                        LOG.info("  %s -> Kandidat %s" % (dev.name[:42], cand.version))
                        try:
                            prep = engine.prepare(
                                dev, cand, cancel_event=self.cancel_event
                            )
                        except OperationCancelled:
                            raise
                        except Exception as exc:
                            failure = str(exc)
                            continue
                        if prep is not None:
                            LOG.ok("Update verfuegbar: %s  %s -> %s"
                                   % (dev.name[:46], dev.version, prep.new_version))
                            break
                        LOG.warn("    Paket passt nicht zur Hardware-ID, uebersprungen")
                except OperationCancelled:
                    raise
                except Exception as exc:
                    failure = str(exc)
                with lock:
                    if prep is not None:
                        for d in group:
                            found[d.device_id] = replace(prep, driver=d)
                    elif failure:
                        for d in group:
                            errors[d.device_id] = failure
                    done[0] += 1
                    n = done[0]
                self._post_ui(self._update_driver_scan_progress, n, len(groups))
                self.report_progress(n, len(groups),
                                     "Treiberprüfung %d von %d" % (n, len(groups)))

            try:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(check, list(groups.values())))
            except OperationCancelled:
                self._post_ui(self._driver_scan_cancelled)
                return
            except Exception as exc:
                self._post_ui(self._driver_scan_failed, exc)
                return
            if self.cancel_event.is_set():
                self._post_ui(self._driver_scan_cancelled)
                return
            self._post_ui(self._updates_found, found, errors)

        self._scan_thread = threading.Thread(target=work, daemon=True)
        self._scan_thread.start()

    def _update_driver_scan_progress(self, current: int, total: int) -> None:
        if not self.busy or self.cancel_event.is_set():
            return
        self.drivers_view.progress.configure(maximum=max(total, 1), value=current)

    def _updates_found(self, found: Dict[str, PreparedUpdate],
                       errors: Optional[Dict[str, str]] = None) -> None:
        errors = errors or {}
        previous = self.updates
        current_drivers = {driver.device_id: driver for driver in self.drivers}
        preserved = set()
        for device_id in errors:
            cached = previous.get(device_id)
            driver = current_drivers.get(device_id)
            if cached and driver and cached.inf_path.is_file() and is_newer(
                    cached.new_version, driver.version):
                found.setdefault(device_id, replace(cached, driver=driver))
                preserved.add(device_id)
        self.updates = found
        for dev in self.drivers:
            if dev.device_id in preserved:
                self.statuses[dev.device_id] = "Pruefung fehlgeschlagen"
            elif dev.device_id in found:
                self.statuses[dev.device_id] = "Update verfuegbar"
            elif dev.device_id in errors:
                self.statuses[dev.device_id] = "Pruefung fehlgeschlagen"
            else:
                self.statuses[dev.device_id] = "Aktuell"
        self.drivers_view.set_rows(self.drivers, self.updates, self.statuses)
        self.set_busy(False)
        installed_count = sum(
            1 for update in picked
            if self.statuses.get(update.driver.device_id) == "Installiert")
        self.record_history(
            "Treiberinstallation", "Erfolgreich" if code == 0 else "Fehlgeschlagen",
            "%d Update(s) ausgewählt · %d aktuell als installiert bestätigt" %
            (len(picked), installed_count))
        if errors:
            LOG.warn("Suche teilweise abgeschlossen: %d Update(s) gefunden." % len(found))
            LOG.warn("Fuer %d Geraet(e) konnte kein verlaesslicher Status ermittelt werden." %
                     len(errors))
        else:
            LOG.ok("Suche abgeschlossen: %d Update(s) gefunden." % len(found))

    def _driver_scan_failed(self, exc: Exception) -> None:
        self.set_busy(False)
        LOG.err("Update-Suche fehlgeschlagen: %s" % exc)
        show_action_error(APP_NAME, "Treiberupdates suchen", exc,
                          "Prüfe die Internetverbindung und versuche die Suche später erneut.",
                          self)

    def _driver_scan_cancelled(self) -> None:
        self.drivers_view.progress.configure(value=0)
        self.set_busy(False, "Treiberprüfung abgebrochen")
        LOG.warn("Treiberprüfung abgebrochen.")

    def install_selected(self) -> None:
        picked: List[PreparedUpdate] = []
        for iid in self.drivers_view.rows:
            vals = self.drivers_view.tree.item(iid, "values")
            info = self.drivers_view.rows.get(iid, {})
            if vals and vals[0] == "☑" and info.get("update"):
                picked.append(info["update"])
        if not picked:
            return
        lines = "\n".join("  %s\n    %s  ->  %s" % (p.driver.name, p.driver.version, p.new_version)
                          for p in picked)
        if not messagebox.askyesno(APP_NAME,
                                   "%d Treiber-Update(s) installieren?\n\n%s\n\n"
                                   "Die Installation laeuft elevated und startet Windows nicht neu."
                                   % (len(picked), lines)):
            return
        workdir = Path(tempfile.gettempdir()) / "SystemUpdaterCleaner"
        self.set_busy(True, "Installiere ...")
        LOG.cmd("Starte elevated Installation von %d Paket(en) ..." % len(picked))

        def work() -> None:
            try:
                code = install_prepared(picked, workdir)
            except Exception as exc:
                self._post_ui(self._install_failed, exc)
                return
            devs = None
            if code != -1:
                try:
                    devs = scan_installed()
                except Exception as exc:
                    LOG.warn("Installierte Treiber konnten nicht aktualisiert werden: %s" % exc)
            self._post_ui(self._install_done, code, devs, picked)

        threading.Thread(target=work, daemon=True).start()

    def _install_failed(self, exc: Exception) -> None:
        self.set_busy(False)
        LOG.err("Installation fehlgeschlagen: %s" % exc)
        show_action_error(APP_NAME, "Treiberinstallation", exc,
                          "Prüfe die UAC-Abfrage, den Geräteanschluss und das Installationsprotokoll.",
                          self)

    def _install_done(self, code: int, devs: Optional[List[InstalledDriver]],
                      picked: Sequence[PreparedUpdate]) -> None:
        if code == -1:
            LOG.warn("Installation abgebrochen (UAC-Dialog abgelehnt).")
            self.set_busy(False)
            self.record_history("Treiberinstallation", "Abgebrochen",
                                "%d Updates · UAC abgelehnt" % len(picked))
            return
        by_id = {d.device_id: d for d in devs or []}
        if devs is not None:
            for dev in self.drivers:
                fresh = by_id.get(dev.device_id)
                if fresh and fresh.version != dev.version:
                    LOG.ok("  %s: %s -> %s" % (dev.name[:44], dev.version, fresh.version))
                    dev.version = fresh.version
                    dev.inf = fresh.inf
                    dev.date = fresh.date
        selected_ids = {update.driver.device_id for update in picked}
        for dev in self.drivers:
            if dev.device_id not in selected_ids:
                continue
            fresh = by_id.get(dev.device_id)
            installed = code == 0 or (
                fresh is not None and not is_newer(self.updates[dev.device_id].new_version,
                                                   fresh.version)
            )
            if installed:
                self.updates.pop(dev.device_id, None)
                self.statuses[dev.device_id] = "Installiert"
            else:
                self.statuses[dev.device_id] = "Installation fehlgeschlagen"
        if code == 0:
            LOG.ok("Installation abgeschlossen.")
        else:
            LOG.err("Installation endete mit Code %d." % code)
            if devs is None:
                LOG.warn("Treiberstatus konnte nach dem Installationsfehler nicht aktualisiert werden.")
        self.drivers_view.set_rows(self.drivers, self.updates, self.statuses)
        self.set_busy(False)
        if code != 0:
            messagebox.showwarning(
                APP_NAME,
                "Mindestens ein Treiber konnte nicht bestaetigt installiert werden. "
                "Fehlgeschlagene Updates bleiben fuer einen erneuten Versuch markiert."
            )

    # -- Konfiguration ------------------------------------------------------
    def _load_config(self) -> None:
        if not CONFIG_FILE.exists():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warn("Konfiguration konnte nicht geladen werden: %s" % exc)
            return
        if not isinstance(data, dict):
            LOG.warn("Konfiguration hat ein ungueltiges Format und wurde ignoriert.")
            return
        geom = data.get("geometry")
        if isinstance(geom, str) and re.fullmatch(r"\d+x\d+(?:[+-]\d+){0,2}", geom):
            try:
                self.geometry(geom)
            except tk.TclError:
                LOG.warn("Gespeicherte Fensterposition ist ungueltig.")
        elif geom is not None:
            LOG.warn("Gespeicherte Fensterposition wurde wegen ungueltigem Format ignoriert.")
        for key, attribute in (("last_driver_scan", "last_driver_scan"),
                               ("last_file_scan", "last_file_scan")):
            value = data.get(key)
            if isinstance(value, str) and len(value) <= 32:
                setattr(self, attribute, value)
        view = data.get("view")
        valid_views = (
            "overview", "drivers", "files", "startup", "storage", "monitor",
            "network", "recovery", "history", "apps",
        )
        configured_view = data.get("start_view")
        self.start_view = (configured_view if configured_view in valid_views else
                           view if view in valid_views else "overview")
        theme = data.get("theme")
        if theme in THEME_PALETTES:
            self.apply_theme(theme)
        scale = data.get("font_scale")
        if isinstance(scale, (float, int)) and 0.85 <= scale <= 1.25:
            self.apply_font_scale(float(scale))
        self.beginner_mode = data.get("beginner_mode") is True
        self.alerts_enabled = data.get("alerts_enabled") is True
        self.show_view(self.start_view)

    def _save_config(self) -> None:
        temporary: Optional[Path] = None
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=str(APP_DIR),
                    prefix="config-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({
                    "geometry": self.geometry(),
                    "view": getattr(self, "current_view", "overview"),
                    "start_view": self.start_view,
                    "theme": self.theme_name,
                    "font_scale": self.font_scale,
                    "beginner_mode": self.beginner_mode,
                    "alerts_enabled": self.alerts_enabled,
                    "last_driver_scan": self.last_driver_scan,
                    "last_file_scan": self.last_file_scan,
                }, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(CONFIG_FILE)
        except OSError as exc:
            LOG.warn("Konfiguration konnte nicht gespeichert werden: %s" % exc)
        finally:
            if temporary and temporary.exists():
                try:
                    temporary.unlink()
                except OSError as exc:
                    LOG.warn("Temporäre Konfigurationsdatei blieb zurueck: %s" % exc)

    def on_close(self) -> None:
        LOG.bind(None)
        self._save_config()
        self.destroy()


def main() -> None:
    if "--background-tracker" in sys.argv[1:]:
        raise SystemExit(_run_background_usage_tracker())
    if IS_WINDOWS:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
