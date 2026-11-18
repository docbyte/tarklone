# tarklone 2.0

Bidirektionaler Ordner-Sync zwischen Windows und [Tarkle](https://tarkle.com) – als Tray-App.

tarklone nutzt die **offizielle Tarkle-API v1** unter `https://tarkle.tech/api/v1`
(`tarkle.com/api/v1` leitet dorthin weiter). Die Anmeldung erfolgt mit einem
API-Key (`tk_…`), den du in Tarkle unter **Einstellungen → API** erzeugst.
Browser-Login, MCP-Beta und die frühere inoffizielle Web-API werden nicht mehr
verwendet.

## Funktionen

- Anmeldung mit einem Tarkle-API-Key, ohne Passwort im Programm zu speichern
- API-Key im OS-Keyring; Windows-Dateifallback mit DPAPI geschützt
- **Ein Tab pro Tarkle-Workspace** mit einem oder mehreren lokalen Ordnern,
  Sync-Intervall, Lösch-Sync, Verschlüsselung und Speicheranzeige
- **Mehrere Workspace-Profile** mit jeweils eigenem Sync-Intervall,
  Löschverhalten und eigener Verschlüsselung
- **Multi-Ordner-Sync pro Workspace:** mehrere lokale Ordner auswählen und
  jeweils einem Ordner im Tarkle-Workspace zuordnen
- Bidirektionaler Sync eines lokalen Ordners mit dem Workspace-Root oder einem Remote-Ordnerpfad
- Dateien und Unterordner in beide Richtungen, inklusive leerer Ordner
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

1. **API-Key in Tarkle erzeugen**

   Melde dich bei Tarkle an und öffne **Einstellungen → API → API keys**.
   Nur Owner und Admins können Keys anlegen. Vergib einen Namen und wähle
   **All workspaces** oder einzelne Workspaces. Tarkle zeigt den Key nur einmal
   an; er beginnt mit `tk_`.

2. **API-Key in tarklone speichern**

   Entweder in der Tray-App im Feld **API-Key** eintragen und
   **Key speichern** wählen, oder per Kommandozeile:

   ```powershell
   .\.venv\Scripts\tarklone.exe login
   ```

   Der Key wird verdeckt abgefragt und im Windows Credential Manager abgelegt
   (Dienst `tarklone`). tarklone prüft ihn sofort mit `GET /workspaces`.

3. **Tray-App starten**

   ```powershell
   .\.venv\Scripts\tarklone.exe tray
   ```

   Beim ersten Start öffnet sich das Einrichtungsfenster:

   - Für jeden erreichbaren Tarkle-Workspace erscheint ein eigener Tab.
   - Im Tab unter **Sync** einen lokalen Ordner auswählen, optional einen
     Remote-Ordnerpfad eintragen (leer = Workspace-Root), das Sync-Intervall
     festlegen und die Löschungsübertragung aktivieren.
   - Für **mehrere lokale Ordner** im selben Workspace die Option
     „Mehrere lokale Ordner synchronisieren“ aktivieren, die Ordner hinzufügen
     und jeweils den Remote-Pfad im Workspace festlegen. Die Ordner dürfen sich
     weder lokal noch remote überschneiden.
   - Der Abschnitt **Verschlüsselung** gilt für den ausgewählten lokalen Ordner
     (bei Multi-Ordner-Sync für die ausgewählte Zuordnung).
   - Der Abschnitt **Speicherplatz** zeigt den belegten Speicher als Wert und Balken.
   - **Allgemeines** bleibt unter den Tabs sichtbar und gilt für alle Workspaces.

   Jeder Workspace wird anschließend mit seinem eigenen Intervall synchronisiert.
   Workspaces ohne lokalen Ordner werden nicht synchronisiert.

   Danach läuft tarklone im Infobereich. Ein Doppelklick auf das Symbol startet
   einen Sync aller Profile; das Kontextmenü bietet Einstellungen, Pause,
   Fortschritt, Restzeit, Ordner öffnen und Protokoll öffnen.

### Autostart ohne API-Key ändern

Die Option **Beim Windows-Start automatisch starten** lässt sich auch dann
speichern und entfernen, wenn kein gültiger API-Key hinterlegt ist. Die
allgemeinen Einstellungen werden ohne Key gespeichert; nur der Sync bleibt
deaktiviert.

## Kommandozeile

```powershell
# API-Key speichern (verdeckte Eingabe)
.\.venv\Scripts\tarklone.exe login

# API-Key entfernen
.\.venv\Scripts\tarklone.exe logout

# API-Key, Workspaces und Speicherplatz prüfen
.\.venv\Scripts\tarklone.exe probe

# Einmaliger Sync aller eingerichteten Profile
.\.venv\Scripts\tarklone.exe sync --verbose

# Nur anzeigen, was passieren würde
.\.venv\Scripts\tarklone.exe sync --dry-run --verbose

# Bestimmten Workspace synchronisieren
.\.venv\Scripts\tarklone.exe sync --workspace <KEY>

# Remote-Ordner für ein Profil überschreiben
.\.venv\Scripts\tarklone.exe sync --workspace <KEY> --folder "Kunden/Projekt"
```

`login` liest den Key verdeckt ein. Für Automatisierung kann er mit
`--key tk_…` übergeben werden; dann steht er allerdings in der
Shell-History.

## Konfiguration

| Datei | Pfad |
|---|---|
| Einstellungen | `%LOCALAPPDATA%\docbyte\tarklone\config.json` |
| Zustand/Sync-Datenbank | `%LOCALAPPDATA%\docbyte\tarklone\profiles\<workspace-key>.sqlite3` (Multi: zusätzlich je Ordner) |
| Protokoll | `%LOCALAPPDATA%\docbyte\tarklone\Logs\tarklone.log` |
| API-Key | Windows Credential Manager, Dienst `tarklone`, Benutzer `api-key` |
| Key-Fallback (headless) | `%LOCALAPPDATA%\docbyte\tarklone\api-key.json` (POSIX: 0600; Windows: DPAPI) |
| Verschlüsselungs-Keyfile | `<Sync-Ordner>\encrypted\.tarklone-key.json` |

Beim ersten Start wird die bisherige Einzelkonfiguration automatisch in ein
Workspace-Profil überführt. Profile aus früheren OAuth-Versionen werden beim
ersten Laden anhand ihres Workspace-Namens auf den neuen 8-stelligen
Workspace-Key migriert; der lokale Sync-Zustand bleibt dabei erhalten.

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

### Versionen, Free- und Pro-Plan

Die API wendet dieselben Regeln an wie die Tarkle-App:

- **Pro:** geänderte Dateien werden als neue Dateiversion gespeichert.
- **Free:** Wenn keine Version angelegt werden kann, verschiebt tarklone die alte
  Fassung in den Tarkle-Papierkorb und lädt die neue unter demselben Namen hoch.
  Das ist wiederherstellbar, erzeugt aber keine Versionshistorie.

### Verschlüsselter Ordner `encrypted/`

In den Einstellungen kann ein Passwort für den Unterordner `encrypted/` gesetzt
werden. Der Ordner wird beim Setzen des Passworts angelegt.

> **Upgrade-Hinweis 1.3:** Aktualisiere alle Rechner auf 1.3 oder neuer, bevor du
> den verschlüsselten Workspace auf einem Rechner entsperrst oder neue Dateien
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

## Entwicklung

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src
```

### Aufbau

- `src/tarklone/api.py` – offizieller Tarkle-API-v1-Client (Dateien, Ordner, Uploads, Usage)
- `src/tarklone/auth.py` – sichere Ablage des API-Keys (Keyring/DPAPI)
- `src/tarklone/tarkle.py` – Sync-Adapter auf Basis der API
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

- Die Tarkle-API akzeptiert nur Keys von Owner/Admin-Mitgliedern. Ein Key mit
  „Only selected workspaces“ sieht nur diese Workspaces; andere antworten mit
  `403 no_access`. Keys mit **Legacy**-Badge können weniger Rechte haben oder
  ganz abgelehnt werden; dafür einen neuen Key erstellen.
- API-Limits hängen vom Tarkle-Plan ab. tarklone wartet bei `429` automatisch
  bis zum `Retry-After`-Zeitpunkt; ein sehr großer Ersts-ync kann trotzdem
  mehrere Durchläufe benötigen. Das Tageskontingent wird erst am nächsten Tag
  (UTC) zurückgesetzt.
- Ein Workspace-Baum mit mehr als 5.000 Unterordnern wird von der rekursiven
  API abgelehnt; tarklone fällt dann auf seitenweises Durchlaufen der Ordner
  zurück.
- Die Verschlüsselung schützt die Daten in Tarkle, nicht den lokalen Klartext im Ordner
  `encrypted/`. Für lokale At-Rest-Verschlüsselung müsste der Ordner anders eingebunden
  werden (z. B. Cryptomator/ BitLocker).
- Der Sync ist ein Datei-Sync, kein Bieterverfahren: Bei gleichzeitigen Änderungen werden
  Konfliktkopien erzeugt, nichts wird still überschrieben.
- Wenn kein Keyring/SecretService verfügbar ist (z. B. headless Linux/WSL), wird der
  API-Key als Fallback in `api-key.json` mit Dateimodus 0600 gespeichert. Das ist
  weniger geschützt als ein Keyring; auf Mehrbenutzersystemen Keyring bevorzugen.

## Sicherheit

- Der API-Key wird nie protokolliert und nicht in `config.json` gespeichert.
- Der Key wirkt wie ein Passwort: Er erlaubt Lese- und Schreibzugriffe in allen
  Workspaces, für die er erstellt wurde. Einen eigenen Key pro Integration
  verwenden und nicht mehr benötigte Keys in Tarkle löschen. `tarklone logout`
  entfernt den Key nur lokal; zum Widerrufen den Key in Tarkle unter
  **Einstellungen → API** löschen.
- Der Key ist an das Tarkle-Konto und dessen Rollen gebunden. Verliert die Person
  den Zugriff auf einen Workspace, verliert auch der Key den Zugriff.
- Der Key-Fallback unter Windows ist mit DPAPI an das aktuelle Benutzerkonto
  gebunden; unter POSIX wird die Datei mit Modus 0600 angelegt.
