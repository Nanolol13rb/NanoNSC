import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import driver_updater as updater
import maintenance
import usage_tracker


class UsageTrackerTests(unittest.TestCase):
    def test_records_foreground_time_and_persists_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            tracker = usage_tracker.UsageTracker(path)
            tracker.record_sample("editor.exe", now=10.0)
            tracker.record_sample("editor.exe", now=14.0)
            tracker.record_sample("browser.exe", now=19.0)
            tracker.record_sample(None, now=20.0)
            snapshot = tracker.snapshot(now=20.0)
            tracker.shutdown()
            reloaded = usage_tracker.UsageTracker(path).snapshot()
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(snapshot["apps"], [("editor.exe", 9.0), ("browser.exe", 1.0)])
        self.assertEqual(reloaded["apps"], snapshot["apps"])
        self.assertNotIn("window_title", persisted)
        self.assertNotIn("window_titles", persisted)

    def test_long_sampling_gap_is_capped_to_prevent_idle_overcounting(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = usage_tracker.UsageTracker(Path(directory) / "usage.json")
            tracker.record_sample("editor.exe", now=10.0)
            tracker.record_sample("browser.exe", now=100.0)
            snapshot = tracker.snapshot(now=100.0)
        self.assertEqual(snapshot["apps"], [("editor.exe", 7.5)])

    def test_pause_and_reset_persist_tracking_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            tracker = usage_tracker.UsageTracker(path)
            tracker.start()
            tracker.pause()
            self.assertFalse(tracker.enabled)
            tracker.reset()
            snapshot = usage_tracker.UsageTracker(path).snapshot()
        self.assertFalse(snapshot["enabled"])
        self.assertEqual(snapshot["apps"], [])
        self.assertTrue(snapshot["started_at"])

    def test_duration_format_is_readable(self):
        self.assertEqual(usage_tracker.format_duration(42), "42 Sek.")
        self.assertEqual(usage_tracker.format_duration(125), "2 Min. 05 Sek.")
        self.assertEqual(usage_tracker.format_duration(3665), "1 Std. 01 Min.")

    def test_invalid_usage_records_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            path.write_text(json.dumps({
                "enabled": False, "started_at": "", "apps": {"bad.exe": float("nan")},
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ungültigen App-Eintrag"):
                usage_tracker.UsageTracker(path)
            with self.assertRaisesRegex(ValueError, "Messintervall"):
                usage_tracker.UsageTracker(path.with_name("other.json"), interval=0)


class MaintenanceTests(unittest.TestCase):
    def test_history_is_bounded_and_keeps_newest_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            for index in range(maintenance.HISTORY_LIMIT + 1):
                maintenance.append_history(path, "Scan", "OK", str(index))
            records = maintenance.load_history(path)
        self.assertEqual(len(records), maintenance.HISTORY_LIMIT)
        self.assertEqual(records[0]["details"], "1")
        self.assertEqual(records[-1]["details"], str(maintenance.HISTORY_LIMIT))

    def test_system_warnings_use_memory_cpu_and_disk_thresholds(self):
        healthy = {
            "CpuLoad": 30, "TotalMemory": 100, "FreeMemory": 50,
            "Drives": [{"Size": 100, "Free": 50 * 1024 ** 3}],
        }
        low_resources = {
            "CpuLoad": 95, "TotalMemory": 100, "FreeMemory": 5,
            "Drives": [{"Size": 100 * 1024 ** 3, "Free": 5 * 1024 ** 3}],
        }
        self.assertEqual(maintenance.system_warnings(healthy), [])
        self.assertEqual(
            set(maintenance.system_warnings(low_resources)),
            {"wenig freier RAM", "Speicherplatz knapp", "CPU stark ausgelastet"},
        )

    def test_backup_manifest_detects_changed_inf_and_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inf = root / "device.inf"
            inf.write_text("[Version]\n", encoding="utf-8")
            (root / "driver.sys").write_bytes(b"driver")
            manifest = maintenance.create_backup_manifest(root, ["Example Device"])
            self.assertEqual(manifest["drivers"], ["Example Device"])
            verified = maintenance.inspect_driver_backup(root)
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["file_count"], 2)

            (root / "unexpected.dll").write_bytes(b"unexpected")
            unlisted = maintenance.inspect_driver_backup(root)
            self.assertFalse(unlisted["valid"])
            self.assertTrue(any("fehlt im Manifest" in issue for issue in unlisted["issues"]))
            (root / "unexpected.dll").unlink()

            inf.write_text("[Version]\n; changed\n", encoding="utf-8")
            (root / "driver.sys").unlink()
            damaged = maintenance.inspect_driver_backup(root)
        self.assertFalse(damaged["valid"])
        self.assertTrue(any("Pruefsumme" in issue for issue in damaged["issues"]))
        self.assertTrue(any("Datei fehlt" in issue for issue in damaged["issues"]))

    def test_legacy_backup_without_manifest_is_identified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "legacy.inf").write_text("[Version]\n", encoding="utf-8")
            result = maintenance.inspect_driver_backup(root)
        self.assertTrue(result["legacy"])
        self.assertTrue(result["valid"])
        self.assertEqual(result["file_count"], 1)

    def test_backup_directory_creation_uses_unique_dated_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            first = maintenance.create_backup_directory(parent)
            second = maintenance.create_backup_directory(parent)
        self.assertNotEqual(first, second)
        self.assertTrue(first.name.startswith("Treiber-Backup-"))
        self.assertTrue(second.name.startswith("Treiber-Backup-"))

    def test_cleanup_refuses_paths_outside_scan_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "scan"
            root.mkdir()
            outside = Path(directory) / "outside.txt"
            outside.write_text("keep", encoding="utf-8")
            local = root / "local.txt"
            local.write_text("remove", encoding="utf-8")
            recycled = []

            def recycle(paths):
                recycled.extend(paths)
                return len(paths), []

            ok, failed = maintenance.cleanup_files(
                [str(local), str(outside)], root, False, recycle
            )
            self.assertEqual(ok, 1)
            self.assertEqual(recycled, [str(local)])
            self.assertEqual(failed, [str(outside)])
            self.assertTrue(outside.exists())

    def test_cleanup_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "scan"
            root.mkdir()
            outside = Path(directory) / "outside.txt"
            outside.write_text("keep", encoding="utf-8")
            link = root / "linked.txt"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks are unavailable in this environment.")
            ok, failed = maintenance.cleanup_files(
                [str(link)], root, True, lambda paths: (len(paths), [])
            )
            self.assertEqual(ok, 0)
            self.assertEqual(failed, [str(link)])
            self.assertTrue(outside.exists())


class VersionAndHardwareTests(unittest.TestCase):
    def test_version_comparison_handles_component_lengths(self):
        self.assertTrue(updater.is_newer("31.0.15.2", "31.0.14.9"))
        self.assertFalse(updater.is_newer("1.2.0", "1.2"))
        self.assertFalse(updater.is_newer("", "1.0"))

    def test_hardware_match_rejects_conflicting_subsystem(self):
        device = r"PCI\VEN_8086&DEV_2723&SUBSYS_00748086&REV_01"
        matching = r"PCI\VEN_8086&DEV_2723&SUBSYS_00748086"
        conflicting = r"PCI\VEN_8086&DEV_2723&SUBSYS_00998086"
        self.assertTrue(updater.hwid_compatible(device, matching))
        self.assertFalse(updater.hwid_compatible(device, conflicting))

    def test_inf_parser_ignores_comments_and_finds_ids(self):
        parsed = updater.inf_hardware_ids(
            "; PCI\\VEN_8086&DEV_0000\n"
            "PCI\\VEN_8086&DEV_2723 = Install\n"
            "%Device% = Install, USB\\VID_1234&PID_ABCD\n"
        )
        self.assertEqual(parsed, [
            r"PCI\VEN_8086&DEV_2723",
            r"USB\VID_1234&PID_ABCD",
        ])


class FileCleanerTests(unittest.TestCase):
    def test_scan_detects_duplicates_and_does_not_preselect_old_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = root / "old.log"
            duplicate_a = root / "a.bin"
            duplicate_b = root / "b.bin"
            old_path.write_bytes(b"old")
            duplicate_a.write_bytes(b"identical")
            duplicate_b.write_bytes(b"identical")
            old_time = time.time() - 40 * 86400
            for path in (old_path, duplicate_a, duplicate_b):
                os.utime(str(path), (old_time, old_time))

            results = updater.FileCleaner().scan(
                root, find_dupes=True, max_age_days=30, skip_system=True
            )
            indexed = {Path(entry.path).name: entry for entry in results}
            self.assertEqual(set(indexed), {"old.log", "a.bin", "b.bin"})
            self.assertTrue(indexed["a.bin"].keep)
            self.assertFalse(indexed["b.bin"].keep)
            self.assertTrue(indexed["old.log"].keep)

    def test_scan_rejects_invalid_age_and_non_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                updater.FileCleaner().scan(root, max_age_days=-1)
            with self.assertRaises(ValueError):
                updater.FileCleaner().scan(root / "missing")

    def test_file_scan_honors_cancellation_before_walking(self):
        with tempfile.TemporaryDirectory() as directory:
            cancel = updater.threading.Event()
            cancel.set()
            with self.assertRaises(updater.OperationCancelled):
                updater.FileCleaner().scan(
                    Path(directory), cancel_event=cancel
                )

    def test_scan_refuses_protected_system_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            protected = Path(directory) / "system"
            selected = protected / "subfolder"
            selected.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"WINDIR": str(protected)}):
                with self.assertRaises(ValueError):
                    updater.FileCleaner().scan(selected)


class SystemToolsTests(unittest.TestCase):
    def test_storage_analysis_totals_files_by_folder_and_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            (root / "reports" / "one.log").write_bytes(b"12345")
            (root / "reports" / "two.log").write_bytes(b"123")
            (root / "top.bin").write_bytes(b"12")

            rows, total = updater.analyze_storage(root)

        indexed = {name: (size, count) for name, size, count in rows}
        self.assertEqual(total, 10)
        self.assertEqual(indexed["Ordner: reports"], (8, 2))
        self.assertEqual(indexed["Typ: .log"], (8, 2))
        self.assertEqual(indexed["Ordner: (Ordnerwurzel)"], (2, 1))
        self.assertEqual(indexed["Typ: .bin"], (2, 1))

    def test_storage_analysis_honors_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            cancel = updater.threading.Event()
            cancel.set()
            with self.assertRaises(updater.OperationCancelled):
                updater.analyze_storage(Path(directory), cancel_event=cancel)

    def test_network_data_accepts_a_single_adapter_json_object(self):
        adapter = {"Name": "Ethernet", "IPv4": ["192.0.2.10"], "DHCP": True}
        with mock.patch.object(updater, "run_ps", return_value=(0, json.dumps(adapter), "")):
            self.assertEqual(updater.get_network_data(), [adapter])

    def test_network_test_reports_connection_errors_without_raising(self):
        with mock.patch.object(updater.socket, "create_connection",
                               side_effect=OSError("offline")):
            success, message = updater.check_network_connection()
        self.assertFalse(success)
        self.assertIn("offline", message)

    def test_startup_folder_items_cannot_be_changed_as_registry_values(self):
        item = {"name": "example.lnk", "source": "Benutzer-Startordner", "writable": False}
        with self.assertRaises(ValueError):
            updater.set_startup_enabled(item, False)

    def test_startup_folder_entry_can_be_disabled_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            startup = root / "Startup"
            disabled = root / "Disabled"
            startup.mkdir()
            link = startup / "example.lnk"
            link.write_bytes(b"shortcut")
            item = {
                "name": link.name, "source": "Benutzer-Startordner",
                "writable": True, "file_path": str(link),
            }
            with mock.patch.object(updater, "STARTUP_FOLDER", startup), \
                    mock.patch.object(updater, "DISABLED_STARTUP_FOLDER", disabled):
                updater.set_startup_enabled(item, False)
                self.assertFalse(link.exists())
                disabled_item = dict(item, enabled=False,
                                     file_path=str(disabled / link.name))
                updater.set_startup_enabled(disabled_item, True)
            self.assertEqual(link.read_bytes(), b"shortcut")


class CatalogTests(unittest.TestCase):
    def test_diagnostic_report_only_contains_selected_sections(self):
        report = updater.build_diagnostic_report({
            "overview": {"OS": "Windows Test", "Build": 42, "Version": "1",
                         "CPU": "Example CPU", "CpuLoad": 5, "TotalMemory": 100,
                         "FreeMemory": 50, "Drives": []},
            "startup": [{"name": "Autostart App", "enabled": True,
                          "source": "Benutzer-Registry", "command": "private command"}],
        })
        self.assertIn("Windows Test", report)
        self.assertIn("Autostart App", report)
        self.assertNotIn("private command", report)
        self.assertNotIn("TREIBER", report)
        self.assertIn("nicht hochgeladen", report)

    def test_restore_rejects_folder_without_inf_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "keine INF"):
                updater.restore_driver_backup(Path(directory))

    def test_driver_restore_reports_reboot_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.inf").write_text("[Version]\n", encoding="utf-8")
            observed = {}

            def elevated(script, timeout):
                observed["script"] = script.read_text(encoding="utf-8-sig")
                observed["timeout"] = timeout
                return 3010

            with mock.patch.object(updater, "run_elevated", side_effect=elevated):
                code, message = updater.restore_driver_backup(root)

        self.assertEqual(code, 3010)
        self.assertIn("pnputil.exe /add-driver", observed["script"])
        self.assertIn("sample.inf", observed["script"])
        self.assertNotIn("Get-ChildItem", observed["script"])
        self.assertIn(root.name, observed["script"])
        self.assertEqual(observed["timeout"], 900)
        self.assertIn("neu gestartet", message)

    def test_parse_row_decodes_html_entities(self):
        entry = updater.CatalogClient._parse_row(
            "<tr><td></td><td>Intel &amp; Co Driver Update (2.0.1)</td>"
            "<td>Windows 11</td><td>Drivers</td><td>2026-01-01</td><td></td></tr>"
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.title, "Intel & Co")
        self.assertEqual(entry.version, "2.0.1")

    def test_download_dialog_rejects_invalid_update_id(self):
        with self.assertRaises(ValueError):
            updater.CatalogClient().resolve_files("not-a-guid")

    def test_download_is_streamed_and_replaces_file(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

            def geturl(self):
                return "https://downloads.example.test/driver.cab"

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "driver.cab"
            with mock.patch.object(
                    urllib.request, "urlopen", return_value=Response(b"package-data")):
                updater.http_download("https://downloads.example.test/driver.cab", target)
            self.assertEqual(target.read_bytes(), b"package-data")
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_zip_extraction_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "unsafe")
            target = root / "extract"
            target.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaises(ValueError):
                    updater._safe_extract_zip(archive, target)
            self.assertFalse((root / "outside.txt").exists())

    def test_zip_extraction_accepts_nested_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "safe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("drivers/device.inf", "DriverVer=01/01/2026,1.2.3")
            target = root / "extract"
            target.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                updater._safe_extract_zip(archive, target)
            self.assertEqual((target / "drivers" / "device.inf").read_text(),
                             "DriverVer=01/01/2026,1.2.3")


class UpdateSearchTests(unittest.TestCase):
    def test_all_catalog_failures_are_not_reported_as_current(self):
        device = updater.InstalledDriver(
            "Intel Wi-Fi AX200", "device", "oem.inf", "1.0", "", "Intel",
            "Net", [r"PCI\VEN_8086&DEV_2723"],
        )

        class BrokenCatalog:
            def search(self, query):
                raise OSError("offline")

        with mock.patch.object(updater, "os_build", return_value=26100):
            engine = updater.UpdateEngine(BrokenCatalog(), Path(tempfile.gettempdir()))
        with self.assertRaises(RuntimeError):
            engine.candidates(device)

    def test_single_successful_empty_search_is_a_valid_no_update_result(self):
        device = updater.InstalledDriver(
            "Intel Wi-Fi AX200", "device", "oem.inf", "1.0", "", "Intel",
            "Net", [r"PCI\VEN_8086&DEV_2723"],
        )

        class EmptyCatalog:
            def search(self, query):
                return []

        with mock.patch.object(updater, "os_build", return_value=26100):
            engine = updater.UpdateEngine(EmptyCatalog(), Path(tempfile.gettempdir()))
        self.assertEqual(engine.candidates(device), [])

    def test_installed_driver_json_shape_is_parsed(self):
        record = {
            "Name": "Adapter", "Id": "device", "Inf": "oem1.inf", "Ver": "1.2.3",
            "Date": "2026-01-01", "Mfr": "Vendor", "Cls": "Net",
            "Hw": [r"PCI\VEN_8086&DEV_2723"], "Prob": 0,
        }
        with mock.patch.object(updater, "run_ps", return_value=(0, json.dumps(record), "")):
            result = updater.scan_installed()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].hardware_ids, [r"PCI\VEN_8086&DEV_2723"])

    def test_partial_install_keeps_failed_and_unselected_updates(self):
        drivers = [
            updater.InstalledDriver("First", "first", "a.inf", "1.0", "", "Vendor", "Net"),
            updater.InstalledDriver("Second", "second", "b.inf", "1.0", "", "Vendor", "Net"),
            updater.InstalledDriver("Third", "third", "c.inf", "1.0", "", "Vendor", "Net"),
        ]

        def prepared(driver):
            return updater.PreparedUpdate(
                driver, updater.CatalogEntry("id", driver.name, "2.0", "", "", "Drivers"),
                Path("device.inf"), "2.0",
            )

        updates = {driver.device_id: prepared(driver) for driver in drivers}
        fake_app = SimpleNamespace(
            drivers=drivers,
            updates=updates.copy(),
            statuses={},
            drivers_view=SimpleNamespace(set_rows=mock.Mock()),
            set_busy=mock.Mock(),
        )
        refreshed = [
            updater.InstalledDriver("First", "first", "a.inf", "2.0", "", "Vendor", "Net"),
            updater.InstalledDriver("Second", "second", "b.inf", "1.0", "", "Vendor", "Net"),
            updater.InstalledDriver("Third", "third", "c.inf", "1.0", "", "Vendor", "Net"),
        ]
        with mock.patch.object(updater.messagebox, "showwarning"):
            updater.App._install_done(fake_app, 1, refreshed, [updates["first"], updates["second"]])
        self.assertNotIn("first", fake_app.updates)
        self.assertIn("second", fake_app.updates)
        self.assertIn("third", fake_app.updates)
        self.assertEqual(fake_app.statuses["first"], "Installiert")
        self.assertEqual(fake_app.statuses["second"], "Installation fehlgeschlagen")
        self.assertNotIn("third", fake_app.statuses)
        fake_app.drivers_view.set_rows.assert_called_once()


if __name__ == "__main__":
    unittest.main()
