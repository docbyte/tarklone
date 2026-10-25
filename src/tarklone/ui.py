from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable

from . import __version__
from .auth import OAuthClient, Token, TokenStore
from .config import Settings, save_settings
from .crypto import CryptoError, EncryptionManager

PURPLE = "#714CB6"
TEXT = "#17151F"
MUTED = "#625D73"
BACKGROUND = "#F6F5FB"


def _center(window: tk.Toplevel) -> None:
    window.update_idletasks()
    width = window.winfo_width()
    height = window.winfo_height()
    x = max(0, (window.winfo_screenwidth() - width) // 2)
    y = max(0, (window.winfo_screenheight() - height) // 2)
    window.geometry(f"+{x}+{y}")


class SetupDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        settings: Settings,
        on_saved: Callable[[Settings], None],
        *,
        first_run: bool = False,
        on_cancel: Callable[[], None] | None = None,
    ):
        super().__init__(master)
        self.settings = settings
        self.on_saved = on_saved
        self.on_cancel = on_cancel
        self.first_run = first_run
        self._closed = False
        self._login_running = False

        self.title("tarklone einrichten" if first_run else "tarklone Einstellungen")
        self.configure(bg=BACKGROUND)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.local_var = tk.StringVar(value=settings.local_folder)
        self.workspace_display_var = tk.StringVar(
            value=(
                f"{settings.workspace_name} — {settings.workspace_id}"
                if settings.workspace_name and settings.workspace_id
                else settings.workspace_id
            )
        )
        self.folder_var = tk.StringVar(value=settings.remote_folder_id)
        self._workspaces: dict[str, tuple[str, str]] = {}
        self._workspaces_loading = False
        self._workspace_load_attempted = False
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self.interval_var = tk.IntVar(value=settings.sync_interval_seconds)
        self.deletions_var = tk.BooleanVar(value=settings.sync_deletions)
        self.start_minimized_var = tk.BooleanVar(value=settings.start_minimized)
        self.launch_var = tk.BooleanVar(value=settings.launch_at_login)

        self._build()
        self._refresh_login_state()
        self._refresh_encryption_state()
        self.local_var.trace_add("write", lambda *_: self._refresh_encryption_state())
        self.after(100, self._poll_ui_queue)
        _center(self)
        # Das Hauptfenster der Tray-App ist withdrawn. Ein transient-Fenster
        # würde darunter ebenfalls unsichtbar bleiben, daher eigenständig
        # anzeigen und den Grab erst nach dem Mapping setzen.
        self.deiconify()
        self.lift()
        self.focus_force()
        self.after(100, self._activate_grab)

    def _activate_grab(self) -> None:
        try:
            if self.winfo_exists() and self.winfo_viewable():
                self.grab_set()
        except tk.TclError:
            pass

    def _release_grab(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass

    def _build(self) -> None:
        outer = tk.Frame(self, bg=BACKGROUND)
        outer.pack(fill="both", expand=True, padx=26, pady=22)

        tk.Label(
            outer,
            text="tarklone",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI Semibold", 20),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            outer,
            text="Bidirektionaler Ordner-Sync für Tarkle",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(fill="x", pady=(2, 18))

        login_box = tk.Frame(outer, bg="white", highlightbackground="#E6E2F0", highlightthickness=1)
        login_box.pack(fill="x", pady=(0, 16))
        login_inner = tk.Frame(login_box, bg="white")
        login_inner.pack(fill="x", padx=16, pady=12)
        self.login_status = tk.Label(
            login_inner,
            text="",
            bg="white",
            fg=TEXT,
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
        )
        self.login_status.pack(side="left", fill="x", expand=True)
        self.login_button = ttk.Button(
            login_inner,
            text="Anmelden …",
            command=self._login_async,
            width=14,
        )
        self.login_button.pack(side="right")
        self.logout_button = ttk.Button(
            login_inner,
            text="Abmelden",
            command=self._logout,
            width=10,
        )

        form = tk.Frame(outer, bg=BACKGROUND)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        tk.Label(
            form, text="Lokaler Ordner", bg=BACKGROUND, fg=TEXT, font=("Segoe UI", 10), anchor="w"
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))
        local_row = tk.Frame(form, bg=BACKGROUND)
        local_row.grid(row=0, column=1, sticky="ew", padx=(14, 0), pady=(0, 6))
        local_row.columnconfigure(0, weight=1)
        ttk.Entry(local_row, textvariable=self.local_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(local_row, text="Durchsuchen", command=self._browse, width=13).grid(
            row=0, column=1, padx=(8, 0)
        )

        tk.Label(
            form,
            text="Tarkle-Workspace",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI", 10),
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(0, 6))
        workspace_row = tk.Frame(form, bg=BACKGROUND)
        workspace_row.grid(row=1, column=1, sticky="ew", padx=(14, 0), pady=(0, 6))
        workspace_row.columnconfigure(0, weight=1)
        self.workspace_combo = ttk.Combobox(
            workspace_row,
            textvariable=self.workspace_display_var,
            values=[],
        )
        self.workspace_combo.grid(row=0, column=0, sticky="ew")
        self.workspace_button = ttk.Button(
            workspace_row,
            text="Laden",
            command=self._load_workspaces,
            width=8,
        )
        self.workspace_button.grid(row=0, column=1, padx=(8, 0))
        tk.Label(
            form,
            text="Name oder ID; nach dem Login automatisch geladen.",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).grid(row=2, column=1, sticky="w", padx=(14, 0), pady=(0, 8))

        tk.Label(
            form,
            text="Remote-Ordner (Pfad)",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI", 10),
            anchor="w",
        ).grid(row=3, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(form, textvariable=self.folder_var).grid(
            row=3, column=1, sticky="ew", padx=(14, 0), pady=(0, 6)
        )
        tk.Label(
            form,
            text="Leer lassen für den Workspace-Root, z. B. Kunden/Projekt.",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).grid(row=4, column=1, sticky="w", padx=(14, 0), pady=(0, 10))

        tk.Label(
            form,
            text="Sync-Intervall (Sekunden)",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI", 10),
            anchor="w",
        ).grid(row=5, column=0, sticky="w", pady=(0, 6))
        ttk.Spinbox(
            form,
            from_=15,
            to=3600,
            increment=15,
            textvariable=self.interval_var,
            width=8,
        ).grid(row=5, column=1, sticky="w", padx=(14, 0), pady=(0, 6))

        options = tk.Frame(outer, bg=BACKGROUND)
        options.pack(fill="x", pady=(10, 8))
        ttk.Checkbutton(
            options,
            text="Löschungen in beide Richtungen übertragen",
            variable=self.deletions_var,
        ).pack(anchor="w")
        tk.Label(
            options,
            text="Ohne diese Option werden gelöschte Dateien auf der anderen Seite wiederhergestellt. "
            "Lokale Löschungen landen zusätzlich im Papierkorb .tarklone/trash.",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 8),
            wraplength=520,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(2, 8))
        ttk.Checkbutton(
            options,
            text="Beim Start minimiert im Tray starten",
            variable=self.start_minimized_var,
        ).pack(anchor="w")
        ttk.Checkbutton(
            options,
            text="Beim Windows-Login automatisch starten",
            variable=self.launch_var,
        ).pack(anchor="w", pady=(2, 0))

        encryption = tk.Frame(outer, bg=BACKGROUND)
        encryption.pack(fill="x", pady=(14, 0))
        tk.Label(
            encryption,
            text="Verschlüsselter Ordner (AES-256)",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(fill="x")
        enc_row = tk.Frame(encryption, bg=BACKGROUND)
        enc_row.pack(fill="x", pady=(3, 0))
        self.encryption_status_label = tk.Label(
            enc_row,
            text="",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.encryption_status_label.pack(side="left")
        self.encryption_button = ttk.Button(
            enc_row,
            text="Passwort festlegen …",
            command=self._encryption_action,
            width=20,
        )
        self.encryption_button.pack(side="right")
        tk.Label(
            encryption,
            text="Dateien unter encrypted/ werden beim Upload mit AES-256-GCM "
            "verschlüsselt und beim Download entschlüsselt. Eine Passwortänderung "
            "verpackt nur den Master-Key neu – Dateien werden nicht neu verschlüsselt. "
            "Die Schlüsseldatei encrypted/.tarklone-key.json wird mitgesynct.",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 8),
            wraplength=520,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(3, 0))

        buttons = tk.Frame(outer, bg=BACKGROUND)
        buttons.pack(fill="x", pady=(18, 0))
        tk.Label(
            buttons,
            text=f"Version {__version__}",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(side="left", anchor="s", pady=(0, 3))
        ttk.Button(buttons, text="Speichern", command=self._save, width=14).pack(side="right")
        ttk.Button(buttons, text="Abbrechen", command=self._close, width=12).pack(
            side="right", padx=(0, 8)
        )

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                callback = self._ui_queue.get_nowait()
                try:
                    callback()
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            if self.winfo_exists():
                self.after(100, self._poll_ui_queue)
        except tk.TclError:
            pass

    def _refresh_login_state(self) -> None:
        token = TokenStore().load()
        if token:
            email = token.email or "Tarkle-Konto"
            self.login_status.config(text=f"Angemeldet als {email}")
            self.login_button.pack_forget()
            self.logout_button.pack(side="right")
            if not self._workspaces and not self._workspace_load_attempted:
                self._workspace_load_attempted = True
                self.after(250, self._load_workspaces)
        else:
            self.login_status.config(text="Noch nicht bei Tarkle angemeldet.")
            self.logout_button.pack_forget()
            self.login_button.pack(side="right")
            self.login_button.config(state="normal")

    def _load_workspaces(self) -> None:
        if self._workspaces_loading:
            return
        token = TokenStore().load()
        if token is None:
            messagebox.showinfo(
                "Tarkle-Workspaces",
                "Bitte zuerst bei Tarkle anmelden.",
                parent=self,
            )
            return
        self._workspaces_loading = True
        self.workspace_button.config(state="disabled", text="Lade …")
        self.login_status.config(text="Lade Tarkle-Workspaces …")

        def worker() -> None:
            from .tarkle import TarkleRemote

            remote = None
            try:
                remote = TarkleRemote(self.settings, token)
                workspaces = remote.list_workspaces()
            except Exception as exc:  # noqa: BLE001 - im UI anzeigen
                error = exc
                self._ui_queue.put(
                    lambda error=error: self._workspaces_loaded([], error)
                )
            else:
                self._ui_queue.put(
                    lambda workspaces=workspaces: self._workspaces_loaded(workspaces, None)
                )
            finally:
                if remote is not None:
                    remote.close()

        threading.Thread(target=worker, name="tarklone-workspaces", daemon=True).start()

    def _workspaces_loaded(self, workspaces, error) -> None:  # noqa: ANN001
        self._workspaces_loading = False
        self.workspace_button.config(state="normal", text="Laden")
        token = TokenStore().load()
        if token:
            self.login_status.config(
                text=f"Angemeldet als {token.email or 'Tarkle-Konto'}"
            )
        if error:
            messagebox.showerror("Workspaces laden", str(error), parent=self)
            return
        self._workspaces = {
            f"{item.get('name') or item.get('id')} — {item.get('id')}": (
                str(item.get("id")),
                str(item.get("name") or ""),
            )
            for item in workspaces
            if item.get("id")
        }
        values = list(self._workspaces)
        self.workspace_combo["values"] = values
        current_id = self.settings.workspace_id
        selected = next(
            (display for display, (wid, _name) in self._workspaces.items() if wid == current_id),
            "",
        )
        if not selected and len(values) == 1:
            selected = values[0]
        if selected:
            self.workspace_display_var.set(selected)

    def _encryption_manager(self) -> EncryptionManager | None:
        local = self.local_var.get().strip()
        if not local:
            return None
        path = Path(local)
        if not path.is_dir():
            return None
        manager = EncryptionManager(path)
        manager.load_cached_key()
        return manager

    def _refresh_encryption_state(self) -> None:
        manager = self._encryption_manager()
        if manager is None:
            self.encryption_status_label.config(text="Lokalen Ordner auswählen.")
            self.encryption_button.config(state="disabled")
            return
        self.encryption_button.config(state="normal")
        if not manager.enabled:
            self.encryption_status_label.config(text="Nicht eingerichtet.")
            self.encryption_button.config(text="Passwort festlegen …")
        elif manager.unlocked:
            self.encryption_status_label.config(text="Aktiv – entsperrt.")
            self.encryption_button.config(text="Passwort ändern …")
        else:
            self.encryption_status_label.config(text="Aktiv – gesperrt.")
            self.encryption_button.config(text="Entsperren …")

    def _set_encryption_password(self) -> None:
        manager = self._encryption_manager()
        if manager is None:
            messagebox.showwarning(
                "Verschlüsselung",
                "Bitte zuerst einen gültigen lokalen Ordner auswählen.",
                parent=self,
            )
            return
        new = simpledialog.askstring(
            "Passwort festlegen",
            "Neues Passwort (mindestens 8 Zeichen):",
            show="*",
            parent=self,
        )
        if new is None:
            return
        confirm = simpledialog.askstring(
            "Passwort bestätigen",
            "Passwort wiederholen:",
            show="*",
            parent=self,
        )
        if new != confirm:
            messagebox.showerror("Verschlüsselung", "Die Passwörter stimmen nicht überein.", parent=self)
            return
        try:
            manager.set_password(new)
        except CryptoError as exc:
            messagebox.showerror("Verschlüsselung", str(exc), parent=self)
            return
        messagebox.showinfo(
            "Verschlüsselung",
            "Der Ordner „encrypted“ wurde erstellt.\n\n"
            "Dateien darin werden ab jetzt beim Upload AES-256-verschlüsselt. "
            "Die Schlüsseldatei wird mitgesynct.",
            parent=self,
        )
        self._refresh_encryption_state()

    def _unlock_encryption(self, manager: EncryptionManager) -> None:
        password = simpledialog.askstring(
            "Verschlüsselung entsperren",
            "Passwort für den encrypted-Ordner:",
            show="*",
            parent=self,
        )
        if password is None:
            return
        try:
            manager.unlock(password)
        except CryptoError as exc:
            messagebox.showerror("Verschlüsselung", str(exc), parent=self)
            return
        messagebox.showinfo("Verschlüsselung", "Der encrypted-Ordner ist entsperrt.", parent=self)
        self._refresh_encryption_state()

    def _change_encryption_password(self, manager: EncryptionManager) -> None:
        old = simpledialog.askstring(
            "Passwort ändern",
            "Aktuelles Passwort:",
            show="*",
            parent=self,
        )
        if old is None:
            return
        new = simpledialog.askstring(
            "Passwort ändern",
            "Neues Passwort (mindestens 8 Zeichen):",
            show="*",
            parent=self,
        )
        if new is None:
            return
        confirm = simpledialog.askstring(
            "Passwort ändern",
            "Neues Passwort wiederholen:",
            show="*",
            parent=self,
        )
        if new != confirm:
            messagebox.showerror("Verschlüsselung", "Die Passwörter stimmen nicht überein.", parent=self)
            return
        try:
            manager.change_password(old, new)
        except CryptoError as exc:
            messagebox.showerror("Verschlüsselung", str(exc), parent=self)
            return
        messagebox.showinfo(
            "Verschlüsselung",
            "Das Passwort wurde geändert. Die vorhandenen Dateien wurden nicht "
            "neu verschlüsselt; nur der Master-Key wurde neu verpackt.",
            parent=self,
        )
        self._refresh_encryption_state()

    def _encryption_action(self) -> None:
        manager = self._encryption_manager()
        if manager is None:
            messagebox.showwarning(
                "Verschlüsselung",
                "Bitte zuerst einen gültigen lokalen Ordner auswählen.",
                parent=self,
            )
            return
        if not manager.enabled:
            self._set_encryption_password()
        elif not manager.unlocked:
            self._unlock_encryption(manager)
        else:
            self._change_encryption_password(manager)

    def _login_async(self) -> None:
        if self._login_running:
            return
        self._login_running = True
        self.login_button.config(state="disabled", text="Browser geöffnet …")
        self.login_status.config(text="Bitte im Browser bei Tarkle anmelden …")

        def worker() -> None:
            client = OAuthClient(self.settings)
            try:
                token = client.login()
            except Exception as exc:  # noqa: BLE001 - wird im UI angezeigt
                error = exc
                self._ui_queue.put(lambda error=error: self._login_failed(error))
            else:
                self._ui_queue.put(lambda token=token: self._login_ok(token))
            finally:
                client.close()

        threading.Thread(target=worker, name="tarklone-login", daemon=True).start()

    def _login_ok(self, token: Token) -> None:
        self._login_running = False
        self.login_button.config(text="Anmelden …", state="normal")
        self._workspace_load_attempted = False
        self._refresh_login_state()
        messagebox.showinfo(
            "tarklone",
            f"Angemeldet als {token.email or 'Tarkle-Konto'}.",
            parent=self,
        )

    def _login_failed(self, exc: Exception) -> None:
        self._login_running = False
        self.login_button.config(text="Anmelden …", state="normal")
        self.login_status.config(text="Anmeldung fehlgeschlagen.")
        messagebox.showerror("Anmeldung fehlgeschlagen", str(exc), parent=self)

    def _logout(self) -> None:
        if not messagebox.askyesno(
            "Abmelden",
            "Tarkle-Anmeldung wirklich entfernen?",
            parent=self,
        ):
            return
        OAuthClient(self.settings).logout()
        self._refresh_login_state()

    def _browse(self) -> None:
        selected = filedialog.askdirectory(
            parent=self,
            title="Lokalen Sync-Ordner auswählen",
            initialdir=self.local_var.get() or str(Path.home()),
        )
        if selected:
            self.local_var.set(selected)

    def _save(self) -> None:
        self.settings.local_folder = self.local_var.get().strip()
        display = self.workspace_display_var.get().strip()
        if display in self._workspaces:
            workspace_id, workspace_name = self._workspaces[display]
        elif " — " in display:
            workspace_name, workspace_id = display.rsplit(" — ", 1)
        else:
            workspace_id = display
            workspace_name = (
                self.settings.workspace_name
                if display == self.settings.workspace_id
                else ""
            )
        self.settings.workspace_id = workspace_id.strip()
        self.settings.workspace_name = workspace_name.strip()
        self.settings.remote_folder_id = (
            self.folder_var.get().strip().replace("\\", "/").strip("/")
        )
        self.settings.sync_interval_seconds = max(15, int(self.interval_var.get() or 60))
        self.settings.sync_deletions = bool(self.deletions_var.get())
        self.settings.start_minimized = bool(self.start_minimized_var.get())
        self.settings.launch_at_login = bool(self.launch_var.get())

        errors = self.settings.validation_errors()
        if errors:
            messagebox.showwarning("Einrichtung unvollständig", "\n".join(errors), parent=self)
            return

        save_settings(self.settings)
        apply_launch_at_login(self.settings.launch_at_login)
        self._closed = True
        self._release_grab()
        self.destroy()
        self.on_saved(self.settings)

    def _close(self) -> None:
        if self.first_run and not messagebox.askyesno(
            "Einrichtung abbrechen",
            "Ohne Einrichtung kann tarklone nicht synchronisieren. Trotzdem schließen?",
            parent=self,
        ):
            return
        self._closed = True
        self._release_grab()
        self.destroy()
        if self.on_cancel:
            self.on_cancel()


def apply_launch_at_login(enabled: bool) -> None:
    """Trägt tarklone in den Windows-Autostart ein oder entfernt es."""
    import sys

    try:
        import winreg
    except ImportError:
        return

    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}" tray'
    else:
        command = f'"{sys.executable}" -m tarklone tray'
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, "tarklone", 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, "tarklone")
                except FileNotFoundError:
                    pass
    except OSError:
        pass
