# tarklone 1.3

Bidirektionaler Ordner-Sync zwischen Windows und [Tarkle](https://tarkle.com) – als Tray-App.

> **Wichtig:** Tarkle hat derzeit noch keine öffentliche API. Developer API, WebDAV/rclone und die
> Mac-App sind auf tarkle.com als „Soon“ markiert. tarklone nutzt deshalb den normalen
> Browser-Login (Supabase OAuth mit PKCE) sowie die offizielle MCP-Beta unter
> `https://mcp.tarklesend.com/mcp` und die interne Web-API der Tarkle-App. Die Anbindung ist
> inoffiziell und kann sich ändern.

## Funktionen

- Browser-Login bei Tarkle, ohne Passwort im Programm zu speichern
- Zugriffstoken im OS-Keyring; Windows-Dateifallback mit DPAPI geschützt
- **Ein Tab pro Tarkle-Workspace** mit lokalem Ordner, Sync-Intervall, Lösch-Sync,
  Verschlüsselung und Speicheranzeige
- **Mehrere Workspace-Profile** mit jeweils eigenem lokalen Ordner, Sync-Intervall,
  Löschverhalten und eigener Verschlüsselung
- Bidirektionaler Sync eines lokalen Ordners mit dem Workspace-Root oder einem Remote-Ordnerpfad
- Dateien und Unterordner in beide Richtungen
- **Speicherplatzanzeige** pro Workspace als Wert und horizontaler Balken
- Optionaler AES-256-GCM-Ordner `encrypted/`: lokal lesbar, in Tarkle verschlüsselt
- Datei- und Ordnernamen unter `encrypted/` werden zusätzlich mit AES-SIV verschlüsselt
- Argon2id-geschützter Master-Key; Passwortänderung ohne Neuverschlüsselung der Dateien oder Namen
- Konfliktkopien statt Überschreiben (`Name (Konflikt PC Zeitstempel).ext`)
- Löschungen standardmäßig **nicht** übertragen; lokale Löschungen landen in `.tarklone/trash`
- Tray-App mit Status, Fortschritt in Prozent und geschätzter Restzeit, manuellem Sync, Pause
  und Einstellungen
- Hintergrund-Sync je Profil per eigenem Zeitintervall und Dateiüberwachung (`watchdog`)
- Update-Prüfung mit automatischem Download der passenden Plattform-Datei
- Danger Zone zum Zurücksetzen des Verschlüsselungsschlüssels
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

   - Nach dem Login erscheint für jeden Tarkle-Workspace ein eigener Tab.
   - Im Tab unter **Sync** den lokalen Ordner auswählen, optional einen
     Remote-Ordnerpfad eintragen (leer = Workspace-Root), das Sync-Intervall
     festlegen und die Löschungsübertragung aktivieren.
   - Der Abschnitt **Verschlüsselung** im selben Tab gilt nur für diesen Workspace.
   - Der Abschnitt **Speicherplatz** zeigt den belegten Speicher als Wert und Balken.
   - **Allgemeines** bleibt unter den Tabs sichtbar und gilt für alle Workspaces.

   Jeder Workspace wird anschließend mit seinem eigenen Intervall synchronisiert.
   Workspaces ohne lokalen Ordner werden nicht synchronisiert.

   Danach läuft tarklone im Infobereich. Ein Doppelklick auf das Symbol startet
   einen Sync aller Profile; das Kontextmenü bietet Einstellungen, Pause,
   Fortschritt, Restzeit, Ordner öffnen und Protokoll öffnen.

## Kommandozeile

```powershell
# Einmaliger Sync aller eingerichteten Profile
.\.venv\Scripts\tarklone.exe sync --verbose

# Nur anzeigen, was passieren würde
.\.venv\Scripts\tarklone.exe sync --dry-run --verbose

# Bestimmtes Profil synchronisieren
.\.venv\Scripts\tarklone.exe sync --workspace <ID>

# Remote-Ordner für ein Profil überschreiben
.\.venv\Scripts\tarklone.exe sync --workspace <ID> --folder "Kunden/Projekt"
```

## Konfiguration

| Datei | Pfad |
|---|---|
| Einstellungen | `%LOCALAPPDATA%\docbyte\tarklone\config.json` |
| Zustand/Sync-Datenbank | `%LOCALAPPDATA%\docbyte\tarklone\profiles\<workspace-id>.sqlite3` |
| Protokoll | `%LOCALAPPDATA%\docbyte\tarklone\Logs\tarklone.log` |
| Zugriffstoken | Windows Credential Manager, Dienst `tarklone` |
| Token-Fallback (headless) | `<AppData>\tarklone\token.json` (POSIX: 0600; Windows: DPAPI) |
| Verschlüsselungs-Keyfile | `<Sync-Ordner>\encrypted\.tarklone-key.json` |

Beim ersten Start wird die bisherige Einzelkonfiguration automatisch in ein
Workspace-Profil überführt; der bisherige Sync-Zustand wird für dieses Profil
übernommen.

## Tray-Anzeige

Während eines Syncs zeigt das Tray-Menü das aktive Profil, den Fortschritt in
Prozent und eine geschätzte Restzeit. Stehen weitere Profile an, wird zusätzlich
die Anzahl der ausstehenden Profile angezeigt. Der Fortschritt wird aus den
geplanten Sync-Aktionen berechnet; bei sehr großen einzelnen Dateien springt er
erst nach Abschluss der Datei weiter.

Die Einstellungen zeigen im Tab jedes Workspaces den belegten Speicher als Wert
und als horizontalen Balken.

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

> **Upgrade-Hinweis 1.3:** Aktualisiere alle Rechner auf 1.3, bevor du den
> verschlüsselten Workspace auf einem Rechner entsperrst oder neue Dateien
> synchronisierst. Die neue Keyfile-/Dateiversion wird von 1.2.1 und älteren
> Builds nicht unterstützt.

- Lokal liegen die Dateien im Klartext und bleiben normal nutzbar.
- Beim Upload werden die Inhalte mit **AES-256-GCM** verschlüsselt; in Tarkle
  liegt nur Ciphertext. Neue Inhalte verwenden eigene Dateischlüssel und
  authentifizieren zusätzlich den logischen Pfad und eine Dateirevision.
- Datei- und Ordnernamen unter `encrypted/` werden mit **AES-SIV** verschlüsselt.
  Tarkle sieht nur Token-Namen; der lokale Klartextname bleibt erhalten.
- Beim Download werden Inhalt und Namen wieder entschlüsselt.
- Das Passwort verpackt einen zufälligen 32-Byte-Master-Key mit **Argon2id**.
  Neue Passwörter müssen mindestens 12 Zeichen lang sein. Ältere PBKDF2-Keyfiles
  werden beim erfolgreichen Entsperren auf Argon2id umgestellt.
- Der Master-Key wird in getrennte Dateiinhalts- und Namensschlüssel abgeleitet.
  Bei einer Passwortänderung wird nur der Master-Key neu verpackt.
  **Dateien und Namen werden dabei nicht neu verschlüsselt.**
- Für automatisches Entsperren wird der Master-Key im OS-Keyring zwischengespeichert.
  **Sperren** entfernt diesen Cache; Schadsoftware unter demselben angemeldeten
  Benutzerkonto kann grundsätzlich auf dessen Keyring zugreifen.
- Die Schlüsseldatei `encrypted/.tarklone-key.json` enthält nur den mit dem
  Passwort verpackten Master-Key und wird mitgesynct. Der Dateiname der
  Schlüsseldatei bleibt sichtbar; er enthält keine Nutzdatennamen.
- Der Ordner muss beim erstmaligen Setzen des Passworts leer sein. Dateien, die
  vorher unverschlüsselt in Tarkle unter `encrypted/` liegen, müssen entfernt
  werden.
- **Passwort verloren = Daten nicht entschlüsselbar.** Es gibt keine
  Wiederherstellung ohne Passwort oder Schlüsseldatei.
- Auf einem neuen Rechner wird die Schlüsseldatei beim ersten Sync
  heruntergeladen. Danach in den Einstellungen das Passwort eingeben
  (**Entsperren**); der nächste Sync entschlüsselt Dateien und Namen.
- Dateien nicht direkt über die Tarkle-Weboberfläche in `encrypted/` hochladen;
  sie würden als Klartext an der Verschlüsselung vorbeigehen.
- Bereits vorhandene Klartextnamen unter `encrypted/` werden beim ersten Sync
  automatisch auf Token-Namen migriert. Der Ciphertext wird dabei unverändert
  kopiert, nicht neu verschlüsselt.
- Sehr lange Datei-/Ordnernamen können das Token-Limit überschreiten; dann
  meldet der Sync einen Fehler und der Name muss gekürzt werden.
- Ältere Inhaltsdateien werden bei vorhandenem lokalen Klartext beim nächsten Sync
  in das pfadgebundene Format migriert. Gleichheit von Namen und Dateigrößen sowie
  Änderungszeitpunkte bleiben als Metadaten für Tarkle sichtbar.
- Ein bereits eingerichteter Client erkennt rückläufige oder widersprüchliche
  verschlüsselte Revisionen und lädt sie nicht stillschweigend herunter. Ein
  frischer Client hat noch keinen lokalen Revisionsstand, daher bleibt die
  Wiederherstellung alter Daten durch einen vertrauenswürdigen Speicheranbieter
  grundsätzlich möglich.
- Lokale Dateien unter `encrypted/` bleiben Klartext. App-Logs schwärzen ihre
  Dateinamen; der lokale Sync-Zustand speichert dafür deterministische Namenstoken.

### Danger Zone: Schlüssel löschen

Wenn ein Passwort gesetzt ist, erscheint in den Einstellungen eine rot
umrahmte **Danger Zone**. Dort kann der Schlüssel gelöscht werden, um ein neues
Passwort zu setzen.

- Bestätigung nur durch Eingabe von `DELETE`
- Die verschlüsselten Dateien in Tarkle werden entfernt
- Die lokalen Klartextdateien bleiben erhalten
- Die alten Daten können danach **nicht mehr entschlüsselt werden – auch wenn
  das Passwort später wieder einfällt**
- Nach dem Zurücksetzen kann mit den lokalen Dateien ein neuer Schlüssel
  erzeugt und erneut hochgeladen werden

### Updates

- **Nach Updates suchen** prüft das öffentliche GitHub-Release.
- Gibt es eine neuere Version, wird die passende Datei für Windows, Ubuntu oder
  macOS automatisch heruntergeladen (standardmäßig in den Downloads-Ordner).
- Der Download wird nur gespeichert, wenn die passende SHA-256-Prüfsumme
  erfolgreich geladen und geprüft wurde.
- Die Installation erfolgt manuell; die laufende EXE wird nicht überschrieben.
- Das Intervall ist einstellbar: **Nie**, **Täglich**, **Alle 7 Tage** oder
  **Alle 30 Tage**.
- Die Update-Prüfung läuft im Hintergrund; Fehler werden nur protokolliert.

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
einem Tag `v*` an das Release. Die Update-Prüfung nutzt die öffentliche
GitHub-API und das neueste Release. Release-Assets erhalten außerdem eine
GitHub-Build-Provenance-Attestierung. Sie lässt sich beispielsweise mit
`gh attestation verify <datei> --repo docbyte/tarklone` prüfen.

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
