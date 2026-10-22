# Nano System Control

Windows-Desktop-App zur Anzeige und Verwaltung von Treibern und Systemwartung.
Das violette Farbschema ist die Voreinstellung; zehn Themes stehen in den
Einstellungen zur Auswahl. Bedienelemente und Navigation reagieren mit
passendem Hover- und Fokus-Feedback.
Beim Start prüft die App automatisch auf eine neuere Version von Nano System
Control und bietet bei Bedarf die GitHub-Downloadseite an. Das Programm wird
nicht ohne deine Zustimmung heruntergeladen oder installiert.

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

## App-Nutzung im Hintergrund

Aktiviere unter **App-Nutzung** die Erfassung. Sie läuft dann in einem separaten,
unsichtbaren Prozess weiter, wenn das Hauptfenster geschlossen wird, und startet
bei aktiver Erfassung nach der Windows-Anmeldung automatisch. Über **Erfassung
pausieren** wird die Aufzeichnung und ihr Autostart wieder deaktiviert.
Gespeichert werden lokal nur Prozessnamen und Vordergrundzeiten.

## GitHub Release erstellen

Push eines Tags mit `v`-Präfix, zum Beispiel `v1.0.0`, startet den Windows-Build,
führt die Tests aus und veröffentlicht die EXE automatisch als GitHub Release.
