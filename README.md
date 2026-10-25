# tarklone 1.0

Bidirektionaler Ordner-Sync zwischen Windows und [Tarkle](https://tarkle.com) – als Tray-App.

> **Wichtig:** Tarkle hat derzeit noch keine öffentliche API. Developer API, WebDAV/rclone und die
> Mac-App sind auf tarkle.com als „Soon“ markiert. tarklone nutzt deshalb den normalen
> Browser-Login (Supabase OAuth mit PKCE) sowie die offizielle MCP-Beta unter
> `https://mcp.tarklesend.com/mcp` und die interne Web-API der Tarkle-App. Die Anbindung ist
> inoffiziell und kann sich ändern.

## Funktionen

- Browser-Login bei Tarkle, ohne Passwort im Programm zu speichern
- Zugriffstoken im Windows Credential Manager
- Workspace-Auswahl direkt im Einrichtungsfenster
- Bidirektionaler Sync eines lokalen Ordners mit dem Workspace-Root oder einem Remote-Ordnerpfad
- Dateien und Unterordner in beide Richtungen
- Optionaler AES-256-GCM-Ordner `encrypted/`: lokal lesbar, in Tarkle verschlüsselt
- Passwortgeschützter Master-Key; Passwortänderung ohne Neuverschlüsselung der Dateien
- Konfliktkopien statt Überschreiben (`Name (Konflikt PC Zeitstempel).ext`)
- Löschungen standardmäßig **nicht** übertragen; lokale Löschungen landen in `.tarklone/trash`
- Tray-App mit Status, manuellem Sync, Pause und Einstellungen
- Hintergrund-Sync per Zeitintervall und Dateiüberwachung (`watchdog`)
- Schutz vor Pfad-Traversal und versehentlichem Löschen

## Installation (Entwicklung)

```powershell
cd C:\Users\M\code\tarklone
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## Erste Einrichtung

1. Bei Tarkle anmelden:

   ```powershell
   .\.venv\Scripts\tarklone.exe login
   ```

   Der Browser öffnet den Tarkle-Login. Nach der Bestätigung kehrt der Callback nach
   `127.0.0.1` zurück. Das Token wird im Windows Credential Manager gespeichert.

2. Tray-App starten:

   ```powershell
   .\.venv\Scripts\tarklone.exe tray
   ```

   Beim ersten Start öffnet sich das Einrichtungsfenster:

   - lokalen Ordner auswählen
   - Tarkle-Workspace aus der Liste wählen (nach dem Login automatisch geladen)
   - optional einen Remote-Ordnerpfad eintragen, z. B. `Kunden/Projekt`
     (leer = Workspace-Root)
   - Sync-Intervall und Löschverhalten festlegen

   Danach läuft tarklone im Infobereich. Ein Doppelklick auf das Symbol startet einen Sync;
   das Kontextmenü bietet Einstellungen, Pause, Ordner öffnen und Protokoll öffnen.

## Kommandozeile

```powershell
# Einmaliger Sync
.\.venv\Scripts\tarklone.exe sync --verbose

# Nur anzeigen, was passieren würde
.\.venv\Scripts\tarklone.exe sync --dry-run --verbose

# Alternativer Workspace / Remote-Ordner
.\.venv\Scripts\tarklone.exe sync --workspace <ID> --folder "Kunden/Projekt"
```

## Konfiguration

| Datei | Pfad |
|---|---|
| Einstellungen | `%LOCALAPPDATA%\docbyte\tarklone\config.json` |
| Zustand/Sync-Datenbank | `%LOCALAPPDATA%\docbyte\tarklone\state.sqlite3` |
| Protokoll | `%LOCALAPPDATA%\docbyte\tarklone\Logs\tarklone.log` |
| Zugriffstoken | Windows Credential Manager, Dienst `tarklone` |
| Token-Fallback (headless) | `<AppData>\tarklone\token.json`, Dateimodus 0600 |
| Verschlüsselungs-Keyfile | `<Sync-Ordner>\encrypted\.tarklone-key.json` |

## Sync-Verhalten

- **Beidseitig vorhanden, nur eine Seite geändert:** die geänderte Seite wird übertragen.
- **Beide Seiten geändert:** die Remote-Fassung bleibt am Originalpfad, die lokale Fassung
  wird als Konfliktkopie gesichert und zusätzlich hochgeladen.
- **Nur auf einer Seite vorhanden:** wird auf die andere Seite übertragen.
- **Lokal gelöscht:** ohne „Löschungen übertragen“ wird die Datei aus Tarkle wiederhergestellt;
  mit der Option wird sie remote in den Papierkorb verschoben. Lokale Löschungen werden zuerst
  nach `.tarklone/trash/<Zeitstempel>/` verschoben.
- **Remote gelöscht:** ohne „Löschungen übertragen“ wird die Datei wieder hochgeladen;
  mit der Option wird sie lokal in den Papierkorb verschoben.

### Verschlüsselter Ordner `encrypted/`

In den Einstellungen kann ein Passwort für den Unterordner `encrypted/` gesetzt
werden. Der Ordner wird beim Setzen des Passworts angelegt.

- Lokal liegen die Dateien im Klartext und bleiben normal nutzbar.
- Beim Upload werden sie mit **AES-256-GCM** verschlüsselt; in Tarkle liegt nur
  Ciphertext.
- Beim Download werden sie wieder entschlüsselt.
- Das Passwort verpackt einen zufälligen 32-Byte-Master-Key. Die Dateien selbst
  werden mit diesem Master-Key verschlüsselt; bei einer Passwortänderung wird
  nur der Master-Key neu verpackt. **Dateien werden nicht neu verschlüsselt.**
- Die Schlüsseldatei `encrypted/.tarklone-key.json` enthält nur den mit dem
  Passwort verpackten Master-Key und wird mitgesynct. Sie ist nicht geheim,
  aber ohne Passwort nicht entschlüsselbar.
- Der Ordner muss beim erstmaligen Setzen des Passworts leer sein. Dateien, die
  vorher unverschlüsselt in Tarkle unter `encrypted/` liegen, müssen entfernt
  werden.
- **Passwort verloren = Daten nicht entschlüsselbar.** Es gibt keine
  Wiederherstellung ohne Passwort oder Schlüsseldatei.
- Auf einem neuen Rechner wird die Schlüsseldatei beim ersten Sync
  heruntergeladen. Danach in den Einstellungen das Passwort eingeben
  (**Entsperren**); der nächste Sync entschlüsselt die Dateien.
- Dateien nicht direkt über die Tarkle-Weboberfläche in `encrypted/` hochladen;
  sie würden als Klartext an der Verschlüsselung vorbeigehen.

### Free- und Pro-Plan

- **Pro:** geänderte Dateien werden über die Tarkle-Dateiversionierung aktualisiert.
- **Free:** Dateiversionierung ist nicht verfügbar. tarklone verschiebt die alte Fassung in
  den Tarkle-Papierkorb und lädt die neue Fassung unter demselben Namen hoch. Das ist
  wiederherstellbar, erzeugt aber keine Versionshistorie.

## Entwicklung

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src
```

### Aufbau

- `src/tarklone/auth.py` – Supabase-OAuth (PKCE), Token-Refresh, Credential Manager
- `src/tarklone/mcp.py` – MCP-Client (JSON-RPC, SSE, Sessions)
- `src/tarklone/webapi.py` – Tarkle-Web-API: Session-Cookie, Dateiliste, Download, Upload, Papierkorb
- `src/tarklone/tarkle.py` – Adapter: MCP-Workspaces + Web-API-Dateioperationen
- `src/tarklone/sync.py` – Sync-Engine, Konflikte, Papierkorb, Pfadprüfung
- `src/tarklone/state.py` – SQLite-Zustand
- `src/tarklone/ui.py` – Einrichtungs- und Einstellungsfenster
- `src/tarklone/app.py` – Tray-App, Scheduler, Dateiüberwachung

## Windows-EXE bauen

### Standard-Build

```powershell
.\build.ps1
```

Ergebnis: `dist\tarklone.exe`. Die EXE startet ohne Konsolenfenster direkt als Tray-App.
Der Autostart-Eintrag zeigt bei einer gebauten EXE auf diese Datei.

### Plattform-Builds

Unter `builds/` liegen Varianten für Ubuntu, macOS und eine reine CLI:

| Variante | Skript |
|---|---|
| Ubuntu GUI | `builds/ubuntu/build.sh` |
| macOS GUI | `builds/macos/build.sh` |
| CLI Linux/macOS | `builds/cli/build.sh` |
| CLI Windows | `builds/cli/build.ps1` |
| Windows GUI | `builds/windows/build.ps1` |

Details und Voraussetzungen: `builds/README.md`. Der GitHub-Actions-Workflow
`.github/workflows/build-platforms.yml` baut alle Varianten und hängt sie bei
einem Tag `v*` an das Release.

Die GUI-Varianten sind unter Linux/macOS derzeit **experimentell**; die CLI
ist auf allen drei Plattformen die robusteste Variante.

### Single-EXE-Variante

Für eine explizit reproduzierbare Variante mit eigener PyInstaller-Spec und
Windows-Versionsinformationen:

```powershell
.\build-onefile.ps1
```

Ergebnis: **`dist\tarklone-single.exe`** – genau eine Datei, gleicher Funktionsumfang,
ohne Python-Installation oder zusätzliche Ordner. Die Spec liegt unter
`packaging\tarklone-onefile.spec`, die Versionsdaten unter
`packaging\version_info.txt`.

Beide Builds erzeugen eine einzelne EXE. Die Single-EXE-Variante ist die empfohlene
Datei zum Weitergeben; sie enthält Tcl/Tk, Tray-Icon, Watchdog und den
Windows-Credential-Manager-Backend explizit im Bundle.

## Bekannte Einschränkungen

- Die Tarkle-MCP-Beta muss für den Workspace freigeschaltet sein.
- Ohne öffentliche API können sich Endpunkte und HTML-Strukturen ändern.
- Die rekursive Remote-Liste wird aus den servergerenderten Tarkle-Seiten gelesen; das ist
  die derzeit einzige Möglichkeit, Unterordner zuverlässig zu erkennen.
- Leere lokale Ordner werden remote nicht angelegt (Tarkle legt Ordner beim ersten Upload an).
- Sehr große Dateien, die beim Update einen Multipart-Upload benötigen, werden noch nicht
  unterstützt. Neue Uploads und Downloads großer Dateien funktionieren, solange Tarkle sie
  über die normale Presign-Route annimmt.
- Die Verschlüsselung schützt die Daten in Tarkle, nicht den lokalen Klartext im Ordner
  `encrypted/`. Für lokale At-Rest-Verschlüsselung müsste der Ordner anders eingebunden
  werden (z. B. Cryptomator/ BitLocker).
- Der Sync ist ein Datei-Sync, kein Bieterverfahren: Bei gleichzeitigen Änderungen werden
  Konfliktkopien erzeugt, nichts wird still überschrieben.
- Wenn kein Keyring/SecretService verfügbar ist (z. B. headless Linux/WSL), wird der
  OAuth-Token als Fallback in `token.json` mit Dateimodus 0600 gespeichert. Das ist
  weniger geschützt als ein Keyring; auf Mehrbenutzersystemen Keyring bevorzugen.
