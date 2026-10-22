# Nano System Control

Windows-Desktop-App zur Anzeige und Verwaltung von Treibern und Systemwartung.

## Download

Die fertige Windows-Anwendung findest du unter **Releases** als
`NanoSystemControl.exe`. Alternativ kannst du den letzten erfolgreichen Build
unter **Actions → Windows app → Artifacts** herunterladen.

## Aus dem Quellcode starten

Voraussetzung: Python 3.10 oder neuer.

```powershell
python driver_updater.py
```

## Windows-App lokal bauen

```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed --name NanoSystemControl driver_updater.py
```

Die EXE wird in `dist\NanoSystemControl.exe` erstellt.

## GitHub Release erstellen

Push eines Tags mit `v`-Präfix, zum Beispiel `v1.0.0`, startet den Windows-Build,
führt die Tests aus und veröffentlicht die EXE automatisch als GitHub Release.
