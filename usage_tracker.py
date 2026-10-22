"""Local foreground application usage tracking without window titles or content."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import math
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


SAMPLE_INTERVAL = 5.0
MAX_CREDITED_INTERVAL = SAMPLE_INTERVAL * 1.5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def foreground_application() -> Optional[str]:
    if os.name != "nt":
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    process_id = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)):
        return None
    if process_id.value == os.getpid():
        return None
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value,
    )
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        image_path = ctypes.create_unicode_buffer(size.value)
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        if not kernel32.QueryFullProcessImageNameW(
                handle, 0, image_path, ctypes.byref(size)):
            return None
        return Path(image_path.value).name or None
    finally:
        kernel32.CloseHandle(handle)


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return "%d Std. %02d Min." % (hours, minutes)
    if minutes:
        return "%d Min. %02d Sek." % (minutes, secs)
    return "%d Sek." % secs


class UsageTracker:
    def __init__(self, path: Path,
                 sampler: Callable[[], Optional[str]] = foreground_application,
                 interval: float = SAMPLE_INTERVAL) -> None:
        if interval <= 0:
            raise ValueError("Das Messintervall muss groesser als 0 sein.")
        self.path = path
        self.sampler = sampler
        self.interval = interval
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_application: Optional[str] = None
        self._last_sample_at: Optional[float] = None
        self._last_persisted_at = time.monotonic()
        self.last_error = ""
        self._state = self._load()

    def _load(self) -> Dict[str, object]:
        if not self.path.exists():
            return {"enabled": False, "started_at": "", "apps": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Nutzungsdaten können nicht gelesen werden: %s" % exc) from exc
        if not isinstance(data, dict) or not isinstance(data.get("apps"), dict):
            raise ValueError("Nutzungsdaten haben ein ungültiges Format.")
        apps: Dict[str, float] = {}
        for name, duration in data["apps"].items():
            if (not isinstance(name, str) or not name or
                    isinstance(duration, bool) or
                    not isinstance(duration, (int, float)) or
                    not math.isfinite(duration) or duration < 0):
                raise ValueError("Nutzungsdaten enthalten einen ungültigen App-Eintrag.")
            apps[name[:260]] = float(duration)
        started_at = data.get("started_at", "")
        if not isinstance(started_at, str):
            raise ValueError("Startzeit der Nutzungserfassung ist ungültig.")
        return {
            "enabled": data.get("enabled") is True,
            "started_at": started_at,
            "apps": apps,
        }

    def _persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=str(self.path.parent),
                    prefix=self.path.stem + "-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self._state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        self._last_persisted_at = time.monotonic()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return bool(self._state["enabled"])

    def start(self) -> None:
        with self._lock:
            if not self._state["started_at"]:
                self._state["started_at"] = datetime.now().astimezone().isoformat(
                    timespec="seconds")
            self._state["enabled"] = True
            self._last_application = None
            self._last_sample_at = None
            self.last_error = ""
            self._persist_locked()
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="AppUsageTracker", daemon=True)
            self._thread.start()

    def pause(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.interval + 1))
        with self._lock:
            self._finalize_current_locked(time.monotonic())
            self._state["enabled"] = False
            self._persist_locked()

    def shutdown(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.interval + 1))
        with self._lock:
            self._finalize_current_locked(time.monotonic())
            self._persist_locked()

    def reset(self) -> None:
        with self._lock:
            enabled = bool(self._state["enabled"])
            self._state = {
                "enabled": enabled,
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "apps": {},
            }
            self._last_application = None
            self._last_sample_at = None
            self._persist_locked()

    def record_sample(self, application: Optional[str], now: Optional[float] = None) -> None:
        current_time = time.monotonic() if now is None else now
        name = Path(application).name[:260] if application else None
        with self._lock:
            if self._last_application and self._last_sample_at is not None:
                elapsed = max(0.0, min(
                    current_time - self._last_sample_at, MAX_CREDITED_INTERVAL))
                if elapsed > 0:
                    apps = self._state["apps"]
                    apps[self._last_application] = (
                        float(apps.get(self._last_application, 0.0)) + elapsed)
            self._last_application = name
            self._last_sample_at = current_time
            if current_time - self._last_persisted_at >= 30:
                self._persist_locked()

    def _finalize_current_locked(self, current_time: float) -> None:
        if self._last_application and self._last_sample_at is not None:
            elapsed = max(0.0, min(
                current_time - self._last_sample_at, MAX_CREDITED_INTERVAL))
            if elapsed > 0:
                apps = self._state["apps"]
                apps[self._last_application] = (
                    float(apps.get(self._last_application, 0.0)) + elapsed)
        self._last_application = None
        self._last_sample_at = None

    def snapshot(self, now: Optional[float] = None) -> Dict[str, object]:
        with self._lock:
            apps = dict(self._state["apps"])
            if self._last_application and self._last_sample_at is not None:
                current_time = time.monotonic() if now is None else now
                elapsed = max(0.0, min(
                    current_time - self._last_sample_at, MAX_CREDITED_INTERVAL))
                if elapsed > 0:
                    apps[self._last_application] = float(
                        apps.get(self._last_application, 0.0)) + elapsed
            return {
                "enabled": bool(self._state["enabled"]),
                "started_at": str(self._state["started_at"]),
                "apps": sorted(
                    ((name, seconds) for name, seconds in apps.items() if seconds > 0),
                    key=lambda item: item[1], reverse=True),
                "last_error": self.last_error,
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.record_sample(self.sampler())
            except OSError as exc:
                with self._lock:
                    self.last_error = str(exc)
            if self._stop_event.wait(self.interval):
                break
        with self._lock:
            try:
                self._persist_locked()
            except OSError as exc:
                self.last_error = str(exc)
