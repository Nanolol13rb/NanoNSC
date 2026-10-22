"""Lokale Wartungshistorie, Treiberbackup-Pruefung und sichere Dateibereinigung."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Sequence, Tuple


HISTORY_LIMIT = 500
BACKUP_MANIFEST = "system-updater-backup.json"


def _atomic_write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(path.parent),
                prefix=path.stem + "-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def append_history(path: Path, action: str, result: str,
                   details: str = "") -> None:
    records = load_history(path)
    records.append({
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action[:120],
        "result": result[:120],
        "details": details[:500],
    })
    _atomic_write_json(path, records[-HISTORY_LIMIT:])


def load_history(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Die Wartungshistorie hat ein ungueltiges Format.")
    records = []
    for item in data[-HISTORY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        records.append({
            key: str(item.get(key, ""))[:500]
            for key in ("timestamp", "action", "result", "details")
        })
    return records


def system_warnings(data: Dict[str, object]) -> List[str]:
    warnings = []
    cpu_load = float(data.get("CpuLoad") or 0)
    total_memory = int(data.get("TotalMemory") or 0)
    free_memory = int(data.get("FreeMemory") or 0)
    if total_memory and free_memory / total_memory < 0.1:
        warnings.append("wenig freier RAM")
    drives = data.get("Drives") or []
    if isinstance(drives, dict):
        drives = [drives]
    for drive in drives:
        if not isinstance(drive, dict):
            continue
        size = int(drive.get("Size") or 0)
        available = int(drive.get("Free") or 0)
        if size and (available / size < 0.12 or available < 10 * 1024 ** 3):
            warnings.append("Speicherplatz knapp")
            break
    if cpu_load >= 90:
        warnings.append("CPU stark ausgelastet")
    return warnings


def create_backup_directory(parent: Path) -> Path:
    root = parent.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Der ausgewaehlte Zielpfad ist kein Ordner.")
    base_name = "Treiber-Backup-" + time.strftime("%Y%m%d-%H%M%S")
    for index in range(1000):
        name = base_name if index == 0 else "%s-%d" % (base_name, index)
        destination = root / name
        try:
            destination.mkdir()
        except FileExistsError:
            continue
        return destination
    raise FileExistsError("Es konnte kein freier Backup-Ordnername gefunden werden.")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup_manifest(folder: Path, driver_names: Sequence[str]) -> Dict[str, object]:
    root = folder.resolve()
    files = []
    for path in sorted(root.rglob("*")):
        if path.name == BACKUP_MANIFEST or not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        item = {"path": relative, "size": path.stat().st_size}
        if path.suffix.lower() == ".inf":
            item["sha256"] = _sha256_file(path)
        files.append(item)
    if not any(Path(str(item["path"])).suffix.lower() == ".inf" for item in files):
        raise ValueError("Im Exportordner wurden keine INF-Treiberdateien gefunden.")
    manifest: Dict[str, object] = {
        "format": 1,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "drivers": sorted({name.strip() for name in driver_names if name.strip()}),
        "file_count": len(files),
        "total_bytes": sum(int(item["size"]) for item in files),
        "files": files,
    }
    _atomic_write_json(root / BACKUP_MANIFEST, manifest)
    return manifest


def inspect_driver_backup(folder: Path) -> Dict[str, object]:
    root = folder.resolve()
    if not root.is_dir():
        raise ValueError("Der ausgewaehlte Backup-Pfad ist kein Ordner.")
    inf_files = [path for path in root.rglob("*.inf")
                 if path.is_file() and not path.is_symlink()]
    if not inf_files:
        raise ValueError("Im ausgewaehlten Ordner wurden keine INF-Treiberdateien gefunden.")
    manifest_path = root / BACKUP_MANIFEST
    if not manifest_path.exists():
        return {
            "legacy": True, "valid": True, "issues": [],
            "created": "Unbekannt (Backup ohne Manifest)",
            "drivers": [], "file_count": len(inf_files),
            "total_bytes": sum(path.stat().st_size for path in inf_files),
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "legacy": False, "valid": False, "issues": ["Manifest kann nicht gelesen werden: %s" % exc],
            "created": "Unbekannt", "drivers": [], "file_count": len(inf_files),
            "total_bytes": 0,
        }
    if not isinstance(manifest, dict):
        return {
            "legacy": False, "valid": False, "issues": ["Manifestformat wird nicht unterstuetzt."],
            "created": "Unbekannt", "drivers": [], "file_count": len(inf_files),
            "total_bytes": 0,
        }
    issues: List[str] = []
    files = manifest.get("files")
    if manifest.get("format") != 1 or not isinstance(files, list):
        issues.append("Manifestformat wird nicht unterstuetzt.")
        files = []
    manifest_paths = set()
    total_bytes = 0
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            issues.append("Ein Dateieintrag im Manifest ist ungueltig.")
            continue
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            issues.append("Das Manifest enthaelt einen ungueltigen Dateipfad.")
            continue
        target = root / relative
        try:
            resolved = target.resolve(strict=True)
            resolved.relative_to(root)
            info = target.stat()
        except (OSError, ValueError):
            issues.append("Datei fehlt oder liegt ausserhalb des Backups: %s" % item["path"])
            continue
        if not stat.S_ISREG(info.st_mode) or target.is_symlink():
            issues.append("Backup-Datei ist keine regulaere Datei: %s" % item["path"])
            continue
        manifest_paths.add(relative.as_posix().lower())
        total_bytes += info.st_size
        if info.st_size != item.get("size"):
            issues.append("Dateigroesse stimmt nicht ueberein: %s" % item["path"])
        expected_hash = item.get("sha256")
        if expected_hash:
            try:
                actual_hash = _sha256_file(target)
            except OSError as exc:
                issues.append("Datei kann nicht geprueft werden: %s (%s)" % (item["path"], exc))
            else:
                if actual_hash.lower() != str(expected_hash).lower():
                    issues.append("Pruefsumme stimmt nicht ueberein: %s" % item["path"])
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.name == BACKUP_MANIFEST:
            continue
        if path.relative_to(root).as_posix().lower() not in manifest_paths:
            issues.append("Datei fehlt im Manifest: %s" % path.relative_to(root).as_posix())
    for inf in inf_files:
        if inf.relative_to(root).as_posix().lower() not in manifest_paths:
            issues.append("INF-Datei fehlt im Manifest: %s" % inf.relative_to(root).as_posix())
    drivers = manifest.get("drivers", [])
    if not isinstance(drivers, list):
        drivers = []
        issues.append("Die Geraeteliste im Manifest ist ungueltig.")
    return {
        "legacy": False, "valid": not issues, "issues": issues,
        "created": str(manifest.get("created", "Unbekannt")),
        "drivers": [str(name) for name in drivers],
        "file_count": len(files), "total_bytes": total_bytes,
    }


def cleanup_files(paths: Sequence[str], root: Path, permanent: bool,
                  recycle: Callable[[Sequence[str]], Tuple[int, List[str]]]
                  ) -> Tuple[int, List[str]]:
    """Revalidiert jeden Scan-Treffer unmittelbar vor der Dateiaktion."""
    scan_root = root.resolve(strict=True)
    valid: List[str] = []
    failed: List[str] = []
    for raw_path in paths:
        candidate = Path(raw_path)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(scan_root)
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError("Keine regulaere Datei.")
            valid.append(str(candidate))
        except (OSError, ValueError):
            failed.append(raw_path)
    if not permanent:
        if not valid:
            return 0, failed
        ok, recycle_failed = recycle(valid)
        return ok, failed + recycle_failed
    removed = 0
    for raw_path in valid:
        try:
            os.remove(raw_path)
            removed += 1
        except OSError:
            failed.append(raw_path)
    return removed, failed
