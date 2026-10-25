# tarklone CLI

Reine Kommandozeilen-Variante ohne Tray, Tkinter oder GUI-Abhängigkeiten.

```bash
bash builds/cli/build.sh
./builds/cli/dist/linux/tarklone-cli --help
```

Windows:

```powershell
.\builds\cli\build.ps1
.\builds\cli\dist\windows\tarklone-cli.exe --help
```

Befehle: `login`, `logout`, `status`, `init`, `sync`, `probe`.
Der Befehl `tray` ist in dieser Variante nicht enthalten und meldet das klar.
