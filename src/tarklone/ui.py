from __future__ import annotations

import copy
import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__
from .auth import OAuthClient, Token, TokenStore
from .config import (
    Settings,
    SyncProfile,
    crypto_keyring_username,
    save_settings,
)
from .crypto import CryptoError, EncryptionManager
from .updates import check_for_update, download_update

TEXT = "#17151F"
MUTED = "#625D73"
BACKGROUND = "#F6F5FB"
CARD = "#FFFFFF"
BORDER = "#E2DEEE"
DANGER_BG = "#FFF5F5"
DANGER_FG = "#B42318"
DANGER_BORDER = "#D64545"


def _center(window: tk.Toplevel) -> None:
    window.update_idletasks()
    width = window.winfo_width()
    height = window.winfo_height()
    x = max(0, (window.winfo_screenwidth() - width) // 2)
    y = max(0, (window.winfo_screenheight() - height) // 2)
    window.geometry(f"+{x}+{y}")


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024


def _section(parent: tk.Misc, title: str) -> tk.LabelFrame:
    frame = tk.LabelFrame(
        parent,
        text=f" {title} ",
        bg=CARD,
        fg=TEXT,
        font=("Segoe UI Semibold", 10),
        bd=0,
        relief="flat",
        highlightbackground=BORDER,
        highlightthickness=1,
        labelanchor="nw",
        padx=12,
        pady=10,
    )
    return frame


class WorkspaceTab(tk.Frame):
    """Ein Tab mit allen Einstellungen eines Tarkle-Workspace."""

    def __init__(
        self,
        master: tk.Misc,
        dialog: "SetupDialog",
        profile: SyncProfile,
    ) -> None:
        super().__init__(master, bg=BACKGROUND)
        self.dialog = dialog
        self.profile = profile
        self.local_var = tk.StringVar(value=profile.local_folder)
        self.folder_var = tk.StringVar(value=profile.remote_folder_id)
        self.interval_var = tk.IntVar(value=profile.sync_interval_seconds)
        self.deletions_var = tk.BooleanVar(value=profile.sync_deletions)
        self.storage_value_var = tk.StringVar(value="Noch nicht geladen.")
        self._build()
        self.local_var.trace_add("write", self._on_local_changed)
        self.refresh_encryption()

    def _build(self) -> None:
        outer = tk.Frame(self, bg=BACKGROUND)
        outer.pack(fill="both", expand=True, padx=14, pady=12)

        sync_box = _section(outer, "Sync")
        sync_box.pack(fill="x", pady=(0, 10))
        sync_inner = tk.Frame(sync_box, bg=CARD)
        sync_inner.pack(fill="x")
        sync_inner.columnconfigure(1, weight=1)

        tk.Label(
            sync_inner,
            text="Lokaler Ordner",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 9),
            anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))
        local_row = tk.Frame(sync_inner, bg=CARD)
        local_row.grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))
        local_row.columnconfigure(0, weight=1)
        self.local_entry = ttk.Entry(local_row, textvariable=self.local_var)
        self.local_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            local_row,
            text="Durchsuchen",
            command=self._browse,
            width=13,
        ).grid(row=0, column=1, padx=(8, 0))

        tk.Label(
            sync_inner,
            text="Remote-Ordner (Pfad)",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 9),
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(sync_inner, textvariable=self.folder_var).grid(
            row=1, column=1, sticky="ew", padx=(12, 0), pady=(0, 6)
        )
        tk.Label(
            sync_inner,
            text="Leer = Workspace-Root, z. B. Kunden/Projekt",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).grid(row=2, column=1, sticky="w", padx=(12, 0), pady=(0, 7))

        tk.Label(
            sync_inner,
            text="Sync-Intervall (Sekunden)",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 9),
            anchor="w",
        ).grid(row=3, column=0, sticky="w", pady=(0, 6))
        self.interval_spin = ttk.Spinbox(
            sync_inner,
            from_=15,
            to=3600,
            increment=15,
            textvariable=self.interval_var,
            width=8,
        )
        self.interval_spin.grid(
            row=3, column=1, sticky="w", padx=(12, 0), pady=(0, 6)
        )

        self.deletions_check = ttk.Checkbutton(
            sync_inner,
            text="Löschungen in beide Richtungen übertragen",
            variable=self.deletions_var,
        )
        self.deletions_check.grid(
            row=4, column=1, sticky="w", padx=(12, 0), pady=(0, 2)
        )
        tk.Label(
            sync_inner,
            text="Ohne diese Option werden gelöschte Dateien auf der anderen Seite "
            "wiederhergestellt. Lokale Löschungen landen im Papierkorb "
            ".tarklone/trash.",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 8),
            wraplength=500,
            justify="left",
            anchor="w",
        ).grid(row=5, column=1, sticky="w", padx=(12, 0))

        encryption_box = _section(outer, "Verschlüsselung")
        encryption_box.pack(fill="x", pady=(0, 10))
        encryption_inner = tk.Frame(encryption_box, bg=CARD)
        encryption_inner.pack(fill="x")
        self.encryption_status_label = tk.Label(
            encryption_inner,
            text="",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.encryption_status_label.pack(side="left")
        self.encryption_button = ttk.Button(
            encryption_inner,
            text="Passwort festlegen …",
            command=self._encryption_action,
            width=20,
        )
        self.encryption_button.pack(side="right")
        self.lock_button = ttk.Button(
            encryption_inner,
            text="Sperren",
            command=self._lock_encryption,
            width=10,
        )

        self.danger_frame = tk.Frame(
            encryption_box,
            bg=DANGER_BG,
            highlightbackground=DANGER_BORDER,
            highlightthickness=2,
        )
        danger_inner = tk.Frame(self.danger_frame, bg=DANGER_BG)
        danger_inner.pack(fill="x", padx=12, pady=9)
        tk.Label(
            danger_inner,
            text="Danger Zone",
            bg=DANGER_BG,
            fg=DANGER_FG,
            font=("Segoe UI Semibold", 9),
            anchor="w",
        ).pack(fill="x", pady=(0, 5))
        self.danger_button = tk.Button(
            danger_inner,
            text="Schlüssel löschen …",
            command=self._reset_encryption_key,
            bg=DANGER_BORDER,
            fg="white",
            activebackground=DANGER_FG,
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=3,
            cursor="hand2",
        )
        self.danger_button.pack(anchor="w")

        storage_box = _section(outer, "Speicherplatz")
        storage_box.pack(fill="x")
        tk.Label(
            storage_box,
            textvariable=self.storage_value_var,
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=560,
        ).pack(fill="x", pady=(0, 4))
        self.storage_bar = ttk.Progressbar(
            storage_box,
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.storage_bar.pack(fill="x")

    def _on_local_changed(self, *_args) -> None:
        local_folder = self.local_var.get().strip()
        self.profile.local_folder = local_folder
        self.profile.enabled = bool(local_folder and self.profile.workspace_id)
        self.refresh_encryption()

    def collect(self) -> None:
        self.profile.local_folder = self.local_var.get().strip()
        self.profile.remote_folder_id = (
            self.folder_var.get().strip().replace("\\", "/").strip("/")
        )
        try:
            interval = int(self.interval_var.get() or 60)
        except (TypeError, ValueError, tk.TclError):
            interval = 60
        self.profile.sync_interval_seconds = max(15, interval)
        self.profile.sync_deletions = bool(self.deletions_var.get())
        self.profile.enabled = bool(
            self.profile.local_folder and self.profile.workspace_id
        )

    def _browse(self) -> None:
        selected = filedialog.askdirectory(
            parent=self,
            title="Lokalen Sync-Ordner auswählen",
            initialdir=self.local_var.get() or str(Path.home()),
        )
        if selected:
            self.local_var.set(selected)

    # --- Verschlüsselung -------------------------------------------------

    def refresh_encryption(self) -> None:
        manager = self.dialog.encryption_manager(
            self.profile, self.local_var.get().strip()
        )
        self.lock_button.pack_forget()
        if manager is None:
            local = self.local_var.get().strip()
            if not local:
                text = "Lokalen Ordner auswählen."
            elif not Path(local).is_dir():
                text = "Lokaler Ordner nicht gefunden."
            else:
                text = "Bitte bei Tarkle anmelden."
            self.encryption_status_label.config(text=text)
            self.encryption_button.config(state="disabled")
            self.danger_frame.pack_forget()
            return
        self.encryption_button.config(state="normal")
        if not manager.enabled:
            self.encryption_status_label.config(text="Nicht eingerichtet.")
            self.encryption_button.config(text="Passwort festlegen …")
            self.danger_frame.pack_forget()
        elif manager.unlocked:
            self.encryption_status_label.config(text="Aktiv – entsperrt.")
            self.encryption_button.config(text="Passwort ändern …")
            self.lock_button.pack(side="right", padx=(0, 8))
            if not self.danger_frame.winfo_manager():
                self.danger_frame.pack(fill="x", pady=(10, 0))
        else:
            self.encryption_status_label.config(text="Aktiv – gesperrt.")
            self.encryption_button.config(text="Entsperren …")
            if not self.danger_frame.winfo_manager():
                self.danger_frame.pack(fill="x", pady=(10, 0))

    def _manager(self) -> EncryptionManager | None:
        manager = self.dialog.encryption_manager(
            self.profile, self.local_var.get().strip()
        )
        if manager is None:
            messagebox.showwarning(
                "Verschlüsselung",
                "Bitte zuerst einen gültigen lokalen Ordner auswählen.",
                parent=self,
            )
        return manager

    def _encryption_action(self) -> None:
        manager = self._manager()
        if manager is None:
            return
        if not manager.enabled:
            self._set_encryption_password(manager)
        elif not manager.unlocked:
            self._unlock_encryption(manager)
        else:
            self._change_encryption_password(manager)

    def _set_encryption_password(self, manager: EncryptionManager) -> None:
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
            messagebox.showerror(
                "Verschlüsselung",
                "Die Passwörter stimmen nicht überein.",
                parent=self,
            )
            return
        try:
            manager.set_password(new)
        except CryptoError as exc:
            messagebox.showerror("Verschlüsselung", str(exc), parent=self)
            return
        messagebox.showinfo(
            "Verschlüsselung",
            "Verschlüsselung eingerichtet.",
            parent=self,
        )
        self.refresh_encryption()

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
        messagebox.showinfo(
            "Verschlüsselung", "Verschlüsselung entsperrt.", parent=self
        )
        self.refresh_encryption()

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
            messagebox.showerror(
                "Verschlüsselung",
                "Die Passwörter stimmen nicht überein.",
                parent=self,
            )
            return
        try:
            manager.change_password(old, new)
        except CryptoError as exc:
            messagebox.showerror("Verschlüsselung", str(exc), parent=self)
            return
        messagebox.showinfo(
            "Verschlüsselung", "Passwort geändert.", parent=self
        )
        self.refresh_encryption()

    def _lock_encryption(self) -> None:
        manager = self._manager()
        if manager is None or not manager.unlocked:
            return
        manager.lock(clear_cache=True)
        messagebox.showinfo(
            "Verschlüsselung",
            "Verschlüsselung gesperrt.",
            parent=self,
        )
        self.refresh_encryption()

    def _reset_encryption_key(self) -> None:
        manager = self.dialog.encryption_manager(
            self.profile, self.local_var.get().strip()
        )
        if manager is None or not manager.enabled:
            return
        answer = simpledialog.askstring(
            "DANGER ZONE – Schlüssel löschen",
            "ACHTUNG: Wenn du den Schlüssel löschst, können alle Dateien unter "
            "encrypted/ in Tarkle NICHT mehr entschlüsselt werden – auch dann "
            "nicht, wenn dir das Passwort später wieder einfällt.\n\n"
            "Die lokalen Klartextdateien bleiben erhalten. Die verschlüsselten "
            "Dateien in Tarkle werden entfernt.\n\n"
            "Tippe DELETE, um fortzufahren:",
            parent=self,
        )
        if answer != "DELETE":
            if answer is not None:
                messagebox.showinfo(
                    "Abgebrochen",
                    "Es wurde nichts gelöscht.",
                    parent=self,
                )
            return
        self.danger_button.config(state="disabled", text="Lösche …")
        self.encryption_status_label.config(text="Lösche verschlüsselte Daten …")
        profile = self.profile

        def worker() -> None:
            try:
                if self.dialog.on_reset_encryption is not None:
                    ok, message = self.dialog.on_reset_encryption(profile)
                else:
                    manager.reset_key()
                    ok, message = True, "Schlüssel gelöscht."
            except Exception as exc:  # noqa: BLE001 - im UI anzeigen
                ok, message = False, str(exc)
            self.dialog._ui_queue.put(
                lambda ok=ok, message=message: self._reset_done(ok, message)
            )

        threading.Thread(
            target=worker,
            name="tarklone-reset-encryption",
            daemon=True,
        ).start()

    def _reset_done(self, ok: bool, message: str) -> None:
        self.danger_button.config(state="normal", text="Schlüssel löschen …")
        self.refresh_encryption()
        if ok:
            messagebox.showinfo(
                "Verschlüsselung zurückgesetzt",
                message,
                parent=self,
            )
        else:
            messagebox.showerror(
                "Zurücksetzen fehlgeschlagen",
                message,
                parent=self,
            )

    # --- Speicherplatz ---------------------------------------------------

    def refresh_storage(self) -> None:
        self.dialog.request_storage(self)

    def set_storage_loading(self) -> None:
        self.storage_value_var.set("Lade Speicherplatz …")

    def set_storage(self, usage: dict[str, int]) -> None:
        used = int(usage.get("used", 0))
        limit = int(usage.get("limit", 0))
        percent = (used * 100 / limit) if limit else 0.0
        if limit:
            text = (
                f"{_format_bytes(used)} von {_format_bytes(limit)} belegt "
                f"({percent:.1f} %)"
            ).replace(".", ",")
        else:
            text = f"{_format_bytes(used)} belegt"
        self.storage_value_var.set(text)
        self.storage_bar["value"] = max(0.0, min(100.0, percent))

    def set_storage_message(self, message: str) -> None:
        self.storage_value_var.set(message)
        self.storage_bar["value"] = 0


class SetupDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        settings: Settings,
        on_saved: Callable[[Settings], None],
        *,
        first_run: bool = False,
        on_cancel: Callable[[], None] | None = None,
        on_reset_encryption: Callable[[SyncProfile], tuple[bool, str]] | None = None,
    ):
        super().__init__(master)
        self.settings = copy.deepcopy(settings)
        self.settings.ensure_profiles()
        self._profiles = self.settings.profiles
        self.on_saved = on_saved
        self.on_cancel = on_cancel
        self.on_reset_encryption = on_reset_encryption
        self.first_run = first_run
        self._closed = False
        self._login_running = False
        self._update_running = False
        self._workspaces_loading = False
        self._workspace_load_attempted = False
        self._workspaces_by_id: dict[str, str] = {}
        self._selected_profile_id = ""
        self._tabs: dict[str, WorkspaceTab] = {}
        self._tab_ids: dict[str, str] = {}
        self._tab_profiles: dict[str, str] = {}
        self._placeholder_id = ""
        self._suppress_tab_event = False
        self._storage_cache: dict[str, dict[str, int]] = {}
        self._storage_loading: set[str] = set()
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()

        self.start_minimized_var = tk.BooleanVar(value=settings.start_minimized)
        self.launch_var = tk.BooleanVar(value=settings.launch_at_login)
        self.update_days_var = tk.StringVar(
            value=self._update_days_label(settings.update_check_interval_days)
        )
        self.update_status_var = tk.StringVar(
            value=(
                f"Zuletzt geprüft: {settings.last_update_check}"
                if settings.last_update_check
                else "Noch nicht geprüft."
            )
        )

        self.title("tarklone einrichten" if first_run else "tarklone Einstellungen")
        self.configure(bg=BACKGROUND)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._build()
        self._refresh_login_state()
        self.after(100, self._poll_ui_queue)
        _center(self)
        self.deiconify()
        self.lift()
        self.focus_force()

    @staticmethod
    def _profile_title(profile: SyncProfile) -> str:
        return profile.workspace_name or profile.workspace_id or profile.id

    @staticmethod
    def _update_days_label(days: int) -> str:
        return {
            0: "Nie",
            1: "Täglich",
            7: "Alle 7 Tage",
            30: "Alle 30 Tage",
        }.get(days, "Alle 7 Tage")

    def _update_days_value(self) -> int:
        return {
            "Nie": 0,
            "Täglich": 1,
            "Alle 7 Tage": 7,
            "Alle 30 Tage": 30,
        }.get(self.update_days_var.get(), 7)

    # --- Aufbau ----------------------------------------------------------

    def _build(self) -> None:
        outer = tk.Frame(self, bg=BACKGROUND)
        outer.pack(fill="both", expand=True, padx=24, pady=18)

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
        ).pack(fill="x", pady=(2, 12))

        login_box = tk.Frame(
            outer,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        login_box.pack(fill="x", pady=(0, 12))
        login_inner = tk.Frame(login_box, bg=CARD)
        login_inner.pack(fill="x", padx=14, pady=9)
        self.login_status = tk.Label(
            login_inner,
            text="",
            bg=CARD,
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

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        general_box = _section(outer, "Allgemeines")
        general_box.pack(fill="x", pady=(12, 0))
        inner = tk.Frame(general_box, bg=CARD)
        inner.pack(fill="x")
        update_row = tk.Frame(inner, bg=CARD)
        update_row.pack(fill="x")
        tk.Label(
            update_row,
            text="Updates",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI Semibold", 9),
            anchor="w",
        ).pack(side="left")
        tk.Label(
            update_row,
            text="Prüfen:",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(14, 0))
        self.update_days_combo = ttk.Combobox(
            update_row,
            textvariable=self.update_days_var,
            values=["Nie", "Täglich", "Alle 7 Tage", "Alle 30 Tage"],
            state="readonly",
            width=14,
        )
        self.update_days_combo.pack(side="left", padx=(6, 0))
        self.update_button = ttk.Button(
            update_row,
            text="Nach Updates suchen",
            command=self._check_updates,
        )
        self.update_button.pack(side="right")
        tk.Label(
            inner,
            textvariable=self.update_status_var,
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(5, 0))
        tk.Label(
            inner,
            text="Bei einem Update wird die passende Datei automatisch heruntergeladen.",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(1, 0))
        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=(9, 7))
        ttk.Checkbutton(
            inner,
            text="Beim Start minimiert im Tray starten",
            variable=self.start_minimized_var,
        ).pack(anchor="w")
        if sys.platform == "win32":
            ttk.Checkbutton(
                inner,
                text="Beim Windows-Login automatisch starten",
                variable=self.launch_var,
            ).pack(anchor="w", pady=(2, 0))

        buttons = tk.Frame(outer, bg=BACKGROUND)
        buttons.pack(fill="x", pady=(12, 0))
        tk.Label(
            buttons,
            text=f"Version {__version__}",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(side="left", anchor="s", pady=(0, 3))
        ttk.Button(buttons, text="Speichern", command=self._save, width=14).pack(
            side="right"
        )
        ttk.Button(buttons, text="Abbrechen", command=self._close, width=12).pack(
            side="right", padx=(0, 8)
        )

    # --- Tabs ------------------------------------------------------------

    def _clear_tabs(self) -> None:
        self._suppress_tab_event = True
        try:
            for tab_id in list(self.notebook.tabs()):
                try:
                    widget = self.nametowidget(tab_id)
                except (KeyError, tk.TclError):
                    widget = None
                try:
                    self.notebook.forget(tab_id)
                except tk.TclError:
                    pass
                if widget is not None:
                    try:
                        widget.destroy()
                    except tk.TclError:
                        pass
        finally:
            self._tabs.clear()
            self._tab_ids.clear()
            self._tab_profiles.clear()
            self._placeholder_id = ""
            self._suppress_tab_event = False

    def _show_placeholder(self, text: str, *, show_load: bool = False) -> None:
        self._clear_tabs()
        self._suppress_tab_event = True
        try:
            frame = tk.Frame(self.notebook, bg=BACKGROUND)
            tk.Label(
                frame,
                text=text,
                bg=BACKGROUND,
                fg=MUTED,
                font=("Segoe UI", 10),
                wraplength=520,
                justify="center",
            ).pack(expand=True, pady=(48, 10))
            if show_load and TokenStore().load() is not None:
                ttk.Button(
                    frame,
                    text="Workspaces laden",
                    command=self._load_workspaces,
                ).pack(pady=(0, 48))
            self.notebook.add(frame, text="Workspace")
            self._placeholder_id = self.notebook.tabs()[-1]
        finally:
            self._suppress_tab_event = False

    def _add_tab(self, profile: SyncProfile) -> None:
        if profile.id in self._tabs:
            return
        tab = WorkspaceTab(self.notebook, self, profile)
        self.notebook.add(tab, text=self._profile_title(profile))
        tab_id = self.notebook.tabs()[-1]
        self._tabs[profile.id] = tab
        self._tab_ids[profile.id] = tab_id
        self._tab_profiles[tab_id] = profile.id

    def _rebuild_tabs(self) -> None:
        self._clear_tabs()
        if TokenStore().load() is None:
            self._show_placeholder(
                "Bitte bei Tarkle anmelden, um Workspaces zu konfigurieren."
            )
            return
        for profile in self._profiles:
            self._add_tab(profile)
        if not self._tabs:
            self._show_placeholder(
                "Workspaces werden geladen …", show_load=True
            )
            return
        target = self._selected_profile_id
        if target not in self._tabs:
            target = next(
                (
                    profile.id
                    for profile in self._profiles
                    if profile.enabled and profile.local_folder
                ),
                self._profiles[0].id,
            )
        self._select_profile_tab(target)

    def _select_profile_tab(self, profile_id: str) -> None:
        tab_id = self._tab_ids.get(profile_id)
        if tab_id is None:
            return
        self._selected_profile_id = profile_id
        self.notebook.select(tab_id)
        tab = self._tabs.get(profile_id)
        if tab is not None:
            tab.refresh_encryption()
            tab.refresh_storage()

    def _on_tab_changed(self, _event=None) -> None:
        if self._suppress_tab_event:
            return
        tab_id = self.notebook.select()
        profile_id = self._tab_profiles.get(tab_id)
        if not profile_id:
            return
        self._selected_profile_id = profile_id
        tab = self._tabs.get(profile_id)
        if tab is not None:
            tab.refresh_encryption()
            tab.refresh_storage()

    # --- Login / Workspaces ---------------------------------------------

    def _refresh_login_state(self) -> None:
        token = TokenStore().load()
        if token:
            self.login_status.config(
                text=f"Angemeldet als {token.email or 'Tarkle-Konto'}"
            )
            self.login_button.pack_forget()
            self.logout_button.pack(side="right")
            if not self._tabs:
                self._rebuild_tabs()
            if not self._workspace_load_attempted:
                self._workspace_load_attempted = True
                self.after(200, self._load_workspaces)
        else:
            self.login_status.config(text="Noch nicht bei Tarkle angemeldet.")
            self.logout_button.pack_forget()
            self.login_button.pack(side="right")
            self.login_button.config(state="normal")
            self._clear_tabs()
            self._show_placeholder(
                "Bitte bei Tarkle anmelden, um Workspaces zu konfigurieren."
            )
            self._storage_cache.clear()
            self._storage_loading.clear()

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
                    lambda workspaces=workspaces: self._workspaces_loaded(
                        workspaces, None
                    )
                )
            finally:
                if remote is not None:
                    remote.close()

        threading.Thread(
            target=worker, name="tarklone-workspaces", daemon=True
        ).start()

    def _workspaces_loaded(self, workspaces, error) -> None:  # noqa: ANN001
        self._workspaces_loading = False
        token = TokenStore().load()
        if token is None:
            return
        self.login_status.config(
            text=f"Angemeldet als {token.email or 'Tarkle-Konto'}"
        )
        if error:
            if not self._tabs:
                self._show_placeholder(
                    "Workspaces konnten nicht geladen werden.",
                    show_load=True,
                )
            messagebox.showerror("Workspaces laden", str(error), parent=self)
            return

        self._workspaces_by_id = {
            str(item.get("id")): str(item.get("name") or item.get("id"))
            for item in workspaces
            if item.get("id")
        }
        existing = {
            profile.workspace_id: profile for profile in self._profiles
        }
        for workspace_id, name in self._workspaces_by_id.items():
            profile = existing.get(workspace_id)
            if profile is None:
                profile = SyncProfile(
                    id=workspace_id,
                    workspace_id=workspace_id,
                    workspace_name=name,
                    local_folder="",
                    enabled=False,
                )
                self._profiles.append(profile)
                existing[workspace_id] = profile
            else:
                profile.workspace_name = name

        if self._placeholder_id:
            self._clear_tabs()
        for profile in self._profiles:
            self._add_tab(profile)
        if not self._tabs:
            self._show_placeholder(
                "Keine Workspaces gefunden.", show_load=True
            )
            return
        for profile_id, tab_id in self._tab_ids.items():
            profile = self.settings.profile(profile_id)
            if profile is not None:
                try:
                    self.notebook.tab(
                        tab_id, text=self._profile_title(profile)
                    )
                except tk.TclError:
                    pass
        if self._selected_profile_id not in self._tabs:
            target = next(
                (
                    profile.id
                    for profile in self._profiles
                    if profile.enabled and profile.local_folder
                ),
                self._profiles[0].id,
            )
            self._select_profile_tab(target)

    # --- Speicherplatz ---------------------------------------------------

    def request_storage(self, tab: WorkspaceTab) -> None:
        token = TokenStore().load()
        if token is None:
            tab.set_storage_message("Nicht angemeldet.")
            return
        workspace_id = tab.profile.workspace_id
        if not workspace_id:
            tab.set_storage_message("Kein Workspace ausgewählt.")
            return
        cached = self._storage_cache.get(workspace_id)
        if cached is not None:
            tab.set_storage(cached)
            return
        if workspace_id in self._storage_loading:
            tab.set_storage_loading()
            return
        self._storage_loading.add(workspace_id)
        tab.set_storage_loading()

        def worker() -> None:
            from .webapi import TarkleWeb

            web = None
            try:
                web = TarkleWeb(self.settings, token)
                usage = web.storage_usage(workspace_id)
            except Exception as exc:  # noqa: BLE001 - im UI anzeigen
                error = exc
                self._ui_queue.put(
                    lambda error=error: self._storage_done(
                        workspace_id, None, error
                    )
                )
            else:
                self._ui_queue.put(
                    lambda usage=usage: self._storage_done(
                        workspace_id, usage, None
                    )
                )
            finally:
                if web is not None:
                    web.close()

        threading.Thread(
            target=worker, name="tarklone-storage", daemon=True
        ).start()

    def _storage_done(self, workspace_id: str, usage, error) -> None:  # noqa: ANN001
        self._storage_loading.discard(workspace_id)
        if TokenStore().load() is None:
            for tab in self._tabs.values():
                if tab.profile.workspace_id == workspace_id:
                    tab.set_storage_message("Nicht angemeldet.")
            return
        if error is not None:
            for tab in self._tabs.values():
                if tab.profile.workspace_id == workspace_id:
                    tab.set_storage_message(
                        f"Speicher nicht verfügbar: {error}"
                    )
            return
        if usage is not None:
            self._storage_cache[workspace_id] = usage
        for tab in self._tabs.values():
            if tab.profile.workspace_id == workspace_id and usage is not None:
                tab.set_storage(usage)

    # --- Verschlüsselung -------------------------------------------------

    def encryption_manager(
        self, profile: SyncProfile, local_folder: str
    ) -> EncryptionManager | None:
        if TokenStore().load() is None:
            return None
        local = local_folder.strip()
        if not local:
            return None
        path = Path(local)
        if not path.is_dir():
            return None
        manager = EncryptionManager(
            path,
            keyring_username=crypto_keyring_username(profile.id, str(path)),
        )
        manager.load_cached_key()
        return manager

    # --- Updates ---------------------------------------------------------

    def _check_updates(self) -> None:
        if self._update_running:
            return
        self._update_running = True
        self.update_button.config(state="disabled")
        self.update_status_var.set("Prüfe auf Updates …")

        def worker() -> None:
            try:
                info = check_for_update()
                if info is None:
                    self._ui_queue.put(lambda: self._update_done(None, None, None))
                else:
                    self._ui_queue.put(
                        lambda info=info: self.update_status_var.set(
                            f"Update {info.version} gefunden – lade herunter …"
                        )
                    )
                    path = download_update(info)
                    self._ui_queue.put(
                        lambda info=info, path=path: self._update_done(
                            info, path, None
                        )
                    )
            except Exception as exc:
                self._ui_queue.put(
                    lambda exc=exc: self._update_done(None, None, exc)
                )

        threading.Thread(
            target=worker, name="tarklone-update-check", daemon=True
        ).start()

    def _update_done(self, info, path, error) -> None:  # noqa: ANN001
        self._update_running = False
        self.update_button.config(state="normal")
        self.settings.last_update_check = datetime.now().isoformat(timespec="seconds")
        self.settings.update_check_interval_days = self._update_days_value()
        if error is not None:
            self.update_status_var.set(f"Fehler: {error}")
            messagebox.showerror("Updateprüfung", str(error), parent=self)
        elif info is not None and path is not None:
            self.update_status_var.set(
                f"Update {info.version} heruntergeladen: {path}"
            )
            messagebox.showinfo(
                "Update heruntergeladen",
                f"Version {info.version} wurde heruntergeladen:\n{path}",
                parent=self,
            )
        else:
            self.update_status_var.set(f"Aktuell (Version {__version__}).")

    # --- Login -----------------------------------------------------------

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
        self._workspaces_by_id.clear()
        self._workspace_load_attempted = False
        self._refresh_login_state()

    # --- Speichern -------------------------------------------------------

    def _save(self) -> None:
        if TokenStore().load() is None:
            messagebox.showwarning(
                "Einrichtung",
                "Bitte zuerst bei Tarkle anmelden.",
                parent=self,
            )
            return
        for tab in self._tabs.values():
            tab.collect()
        self.settings.profiles = self._profiles
        self.settings.start_minimized = bool(self.start_minimized_var.get())
        self.settings.launch_at_login = bool(self.launch_var.get())
        self.settings.update_check_interval_days = self._update_days_value()
        self.settings.sync_legacy_fields()
        errors = self.settings.validation_errors()
        if errors:
            messagebox.showwarning(
                "Einrichtung unvollständig",
                "\n".join(errors),
                parent=self,
            )
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

    def _release_grab(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass

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


def apply_launch_at_login(enabled: bool) -> None:
    """Trägt tarklone in den Windows-Autostart ein oder entfernt es."""
    if sys.platform != "win32":
        return
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
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, "tarklone", 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, "tarklone")
                except FileNotFoundError:
                    pass
    except OSError:
        pass
