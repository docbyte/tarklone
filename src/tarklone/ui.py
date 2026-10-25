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
from .crypto import KEYFILE_NAME, CryptoError, EncryptionManager
from .updates import check_for_update, download_update

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


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024


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
        self._selected_profile_id = self._profiles[0].id if self._profiles else ""
        self.on_saved = on_saved
        self.on_cancel = on_cancel
        self.on_reset_encryption = on_reset_encryption
        self.first_run = first_run
        self._closed = False
        self._login_running = False
        self._encryption_reset_allowed = False
        self._update_running = False
        self._storage_running = False
        self._storage_pending = False
        self._storage_cache: dict[str, dict[str, int]] = {}
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._workspaces: dict[str, tuple[str, str]] = {}
        self._workspaces_loading = False
        self._workspace_load_attempted = False

        selected = self._selected_profile()
        self.local_var = tk.StringVar(value=selected.local_folder if selected else "")
        self.workspace_display_var = tk.StringVar(
            value=self._profile_display(selected) if selected else ""
        )
        self.folder_var = tk.StringVar(value=selected.remote_folder_id if selected else "")
        self.interval_var = tk.IntVar(
            value=selected.sync_interval_seconds if selected else 60
        )
        self.deletions_var = tk.BooleanVar(
            value=selected.sync_deletions if selected else False
        )
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
        self.storage_value_var = tk.StringVar(value="Noch nicht geladen.")

        self.title("tarklone einrichten" if first_run else "tarklone Einstellungen")
        self.configure(bg=BACKGROUND)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._build()
        self._refresh_profile_tree()
        self._refresh_login_state()
        self._refresh_encryption_state()
        self._refresh_storage()
        self.local_var.trace_add(
            "write", lambda *_: self._on_local_or_workspace_changed()
        )
        self.workspace_display_var.trace_add(
            "write", lambda *_: self._refresh_storage()
        )
        self.after(100, self._poll_ui_queue)
        _center(self)
        self.deiconify()
        self.lift()
        self.focus_force()

    def _release_grab(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass

    def _selected_profile(self) -> SyncProfile | None:
        return self.settings.profile(self._selected_profile_id)

    @staticmethod
    def _profile_display(profile: SyncProfile | None) -> str:
        if profile is None:
            return ""
        if profile.workspace_name and profile.workspace_name != profile.workspace_id:
            return f"{profile.workspace_name} — {profile.workspace_id}"
        return profile.workspace_id

    def _profile_from_fields(self) -> tuple[str, str]:
        display = self.workspace_display_var.get().strip()
        if display in self._workspaces:
            return self._workspaces[display]
        if " — " in display:
            name, workspace_id = display.rsplit(" — ", 1)
            return workspace_id.strip(), name.strip()
        return display, ""

    def _build(self) -> None:
        outer = tk.Frame(self, bg=BACKGROUND)
        outer.pack(fill="both", expand=True, padx=26, pady=20)

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
        ).pack(fill="x", pady=(2, 14))

        login_box = tk.Frame(
            outer,
            bg="white",
            highlightbackground="#E6E2F0",
            highlightthickness=1,
        )
        login_box.pack(fill="x", pady=(0, 12))
        login_inner = tk.Frame(login_box, bg="white")
        login_inner.pack(fill="x", padx=16, pady=10)
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

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        sync_tab = tk.Frame(self.notebook, bg=BACKGROUND)
        encryption_tab = tk.Frame(self.notebook, bg=BACKGROUND)
        general_tab = tk.Frame(self.notebook, bg=BACKGROUND)
        self.notebook.add(sync_tab, text="Sync")
        self.notebook.add(encryption_tab, text="Verschlüsselung")
        self.notebook.add(general_tab, text="Updates & Allgemein")

        self._build_sync_tab(sync_tab)
        self._build_encryption_tab(encryption_tab)
        self._build_general_tab(general_tab)

        buttons = tk.Frame(outer, bg=BACKGROUND)
        buttons.pack(fill="x", pady=(14, 0))
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

    def _build_sync_tab(self, tab: tk.Frame) -> None:
        inner = tk.Frame(tab, bg=BACKGROUND)
        inner.pack(fill="both", expand=True, padx=16, pady=14)

        tk.Label(
            inner,
            text="Workspace-Profile",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).pack(fill="x")
        tree_frame = tk.Frame(inner, bg=BACKGROUND)
        tree_frame.pack(fill="x", pady=(4, 6))
        self.profile_tree = ttk.Treeview(
            tree_frame,
            columns=("workspace", "folder", "interval", "deletions", "encryption"),
            show="headings",
            height=4,
            selectmode="browse",
        )
        for column, label, width in (
            ("workspace", "Workspace", 150),
            ("folder", "Lokaler Ordner", 200),
            ("interval", "Intervall", 70),
            ("deletions", "Löschen", 65),
            ("encryption", "Verschlüsselung", 95),
        ):
            self.profile_tree.heading(column, text=label)
            self.profile_tree.column(column, width=width, anchor="w")
        self.profile_tree.pack(side="left", fill="x", expand=True)
        scrollbar = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.profile_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.profile_tree.configure(yscrollcommand=scrollbar.set)
        self.profile_tree.bind("<<TreeviewSelect>>", self._on_profile_select)

        profile_buttons = tk.Frame(inner, bg=BACKGROUND)
        profile_buttons.pack(fill="x", pady=(0, 10))
        self.apply_profile_button = ttk.Button(
            profile_buttons,
            text="Profil übernehmen",
            command=self._apply_profile,
            width=18,
        )
        self.apply_profile_button.pack(side="left")
        self.remove_profile_button = ttk.Button(
            profile_buttons,
            text="Profil entfernen",
            command=self._remove_profile,
            width=18,
        )
        self.remove_profile_button.pack(side="left", padx=(8, 0))

        form = tk.Frame(inner, bg=BACKGROUND)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        tk.Label(
            form,
            text="Tarkle-Workspace",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI", 10),
            anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 5))
        workspace_row = tk.Frame(form, bg=BACKGROUND)
        workspace_row.grid(row=0, column=1, sticky="ew", padx=(14, 0), pady=(0, 5))
        workspace_row.columnconfigure(0, weight=1)
        self.workspace_combo = ttk.Combobox(
            workspace_row,
            textvariable=self.workspace_display_var,
            values=[],
        )
        self.workspace_combo.grid(row=0, column=0, sticky="ew")
        self.workspace_combo.bind("<<ComboboxSelected>>", self._on_workspace_selected)
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
        ).grid(row=1, column=1, sticky="w", padx=(14, 0), pady=(0, 7))

        tk.Label(
            form,
            text="Lokaler Ordner",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI", 10),
            anchor="w",
        ).grid(row=2, column=0, sticky="w", pady=(0, 5))
        local_row = tk.Frame(form, bg=BACKGROUND)
        local_row.grid(row=2, column=1, sticky="ew", padx=(14, 0), pady=(0, 5))
        local_row.columnconfigure(0, weight=1)
        self.local_entry = ttk.Entry(local_row, textvariable=self.local_var)
        self.local_entry.grid(row=0, column=0, sticky="ew")
        self.browse_button = ttk.Button(
            local_row,
            text="Durchsuchen",
            command=self._browse,
            width=13,
        )
        self.browse_button.grid(row=0, column=1, padx=(8, 0))

        tk.Label(
            form,
            text="Remote-Ordner (Pfad)",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI", 10),
            anchor="w",
        ).grid(row=3, column=0, sticky="w", pady=(0, 5))
        ttk.Entry(form, textvariable=self.folder_var).grid(
            row=3, column=1, sticky="ew", padx=(14, 0), pady=(0, 5)
        )
        tk.Label(
            form,
            text="Leer lassen für den Workspace-Root, z. B. Kunden/Projekt.",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).grid(row=4, column=1, sticky="w", padx=(14, 0), pady=(0, 7))

        tk.Label(
            form,
            text="Sync-Intervall (Sekunden)",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI", 10),
            anchor="w",
        ).grid(row=5, column=0, sticky="w", pady=(0, 5))
        self.interval_spin = ttk.Spinbox(
            form,
            from_=15,
            to=3600,
            increment=15,
            textvariable=self.interval_var,
            width=8,
        )
        self.interval_spin.grid(row=5, column=1, sticky="w", padx=(14, 0), pady=(0, 4))

        self.deletions_check = ttk.Checkbutton(
            form,
            text="Löschungen in beide Richtungen übertragen",
            variable=self.deletions_var,
        )
        self.deletions_check.grid(
            row=6, column=1, sticky="w", padx=(14, 0), pady=(0, 2)
        )
        tk.Label(
            form,
            text="Ohne diese Option werden gelöschte Dateien auf der anderen Seite "
            "wiederhergestellt. Lokale Löschungen landen zusätzlich im Papierkorb "
            ".tarklone/trash.",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 8),
            wraplength=470,
            justify="left",
            anchor="w",
        ).grid(row=7, column=1, sticky="w", padx=(14, 0), pady=(0, 8))

        storage = tk.Frame(inner, bg=BACKGROUND)
        storage.pack(fill="x", pady=(4, 0))
        tk.Label(
            storage,
            text="Speicherplatz",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            storage,
            textvariable=self.storage_value_var,
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(2, 3))
        self.storage_bar = ttk.Progressbar(
            storage,
            mode="determinate",
            maximum=100,
            value=0,
            length=480,
        )
        self.storage_bar.pack(fill="x")

    def _build_encryption_tab(self, tab: tk.Frame) -> None:
        inner = tk.Frame(tab, bg=BACKGROUND)
        inner.pack(fill="both", expand=True, padx=16, pady=14)
        tk.Label(
            inner,
            text="Verschlüsselter Ordner (AES-256)",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            inner,
            text="Gilt für den lokalen Ordner des ausgewählten Workspace-Profils.",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(2, 6))
        enc_row = tk.Frame(inner, bg=BACKGROUND)
        enc_row.pack(fill="x")
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
        self.lock_button = ttk.Button(
            enc_row,
            text="Sperren",
            command=self._lock_encryption,
            width=10,
        )

        self.danger_frame = tk.Frame(
            inner,
            bg="#FFF5F5",
            highlightbackground="#D64545",
            highlightthickness=2,
        )
        danger_inner = tk.Frame(self.danger_frame, bg="#FFF5F5")
        danger_inner.pack(fill="x", padx=12, pady=10)
        tk.Label(
            danger_inner,
            text="Danger Zone",
            bg="#FFF5F5",
            fg="#B42318",
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))
        self.danger_button = tk.Button(
            danger_inner,
            text="Schlüssel löschen …",
            command=self._reset_encryption_key,
            bg="#D64545",
            fg="white",
            activebackground="#B42318",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
        )
        self.danger_button.pack(anchor="w")

    def _build_general_tab(self, tab: tk.Frame) -> None:
        inner = tk.Frame(tab, bg=BACKGROUND)
        inner.pack(fill="both", expand=True, padx=16, pady=14)
        self.update_frame = tk.Frame(inner, bg=BACKGROUND)
        self.update_frame.pack(fill="x")
        tk.Label(
            self.update_frame,
            text="Updates",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).pack(fill="x")
        update_row = tk.Frame(self.update_frame, bg=BACKGROUND)
        update_row.pack(fill="x", pady=(3, 0))
        tk.Label(
            update_row,
            text="Prüfen:",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")
        self.update_days_combo = ttk.Combobox(
            update_row,
            textvariable=self.update_days_var,
            values=["Nie", "Täglich", "Alle 7 Tage", "Alle 30 Tage"],
            state="readonly",
            width=16,
        )
        self.update_days_combo.pack(side="left", padx=(8, 0))
        self.update_button = ttk.Button(
            update_row,
            text="Nach Updates suchen",
            command=self._check_updates,
        )
        self.update_button.pack(side="right")
        tk.Label(
            self.update_frame,
            textvariable=self.update_status_var,
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(4, 0))
        tk.Label(
            self.update_frame,
            text="Bei einem Update wird die passende Datei automatisch heruntergeladen.",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(1, 0))

        tk.Frame(inner, bg="#E6E2F0", height=1).pack(fill="x", pady=(14, 10))
        tk.Label(
            inner,
            text="Allgemein",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).pack(fill="x")
        ttk.Checkbutton(
            inner,
            text="Beim Start minimiert im Tray starten",
            variable=self.start_minimized_var,
        ).pack(anchor="w", pady=(5, 0))
        if sys.platform == "win32":
            ttk.Checkbutton(
                inner,
                text="Beim Windows-Login automatisch starten",
                variable=self.launch_var,
            ).pack(anchor="w", pady=(2, 0))

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

    # --- Profile ---------------------------------------------------------

    def _refresh_profile_tree(self) -> None:
        for item in self.profile_tree.get_children():
            self.profile_tree.delete(item)
        for profile in self._profiles:
            encrypted = (
                Path(profile.local_folder) / "encrypted" / KEYFILE_NAME
            ).is_file()
            self.profile_tree.insert(
                "",
                "end",
                iid=profile.id,
                values=(
                    self._profile_display(profile),
                    profile.local_folder,
                    f"{profile.sync_interval_seconds}s",
                    "Ja" if profile.sync_deletions else "Nein",
                    "AES-256" if encrypted else "–",
                ),
            )
        if self._selected_profile_id and self.profile_tree.exists(
            self._selected_profile_id
        ):
            self.profile_tree.selection_set(self._selected_profile_id)
            self.profile_tree.see(self._selected_profile_id)
        elif self._profiles:
            self._selected_profile_id = self._profiles[0].id
            self.profile_tree.selection_set(self._selected_profile_id)
            self._load_profile_into_form()
        else:
            self._selected_profile_id = ""
            self._clear_profile_form()

    def _on_profile_select(self, _event=None) -> None:
        if TokenStore().load() is None:
            return
        selection = self.profile_tree.selection()
        if not selection:
            return
        self._selected_profile_id = str(selection[0])
        self._load_profile_into_form()
        self._refresh_encryption_state()
        self._refresh_storage()

    def _load_profile_into_form(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self._clear_profile_form()
            return
        display = next(
            (
                key
                for key, (workspace_id, _name) in self._workspaces.items()
                if workspace_id == profile.workspace_id
            ),
            "",
        )
        self.workspace_display_var.set(display or self._profile_display(profile))
        self.local_var.set(profile.local_folder)
        self.folder_var.set(profile.remote_folder_id)
        self.interval_var.set(profile.sync_interval_seconds)
        self.deletions_var.set(profile.sync_deletions)

    def _clear_profile_form(self) -> None:
        self.workspace_display_var.set("")
        self.local_var.set("")
        self.folder_var.set("")
        self.interval_var.set(60)
        self.deletions_var.set(False)

    def _on_workspace_selected(self, _event=None) -> None:
        if TokenStore().load() is None:
            return
        workspace_id, _name = self._profile_from_fields()
        profile = next(
            (p for p in self._profiles if p.workspace_id == workspace_id),
            None,
        )
        if profile is not None:
            self._selected_profile_id = profile.id
            self._refresh_profile_tree()
            self._load_profile_into_form()
        else:
            self._selected_profile_id = ""
            self.local_var.set("")
            self.folder_var.set("")
            self.interval_var.set(60)
            self.deletions_var.set(False)
        self._refresh_encryption_state()
        self._refresh_storage()

    def _apply_profile(self, silent: bool = False) -> bool:
        if TokenStore().load() is None:
            if not silent:
                messagebox.showwarning(
                    "Workspace-Profil",
                    "Bitte zuerst bei Tarkle anmelden.",
                    parent=self,
                )
            return False
        workspace_id, workspace_name = self._profile_from_fields()
        local_folder = self.local_var.get().strip()
        if not workspace_id or not local_folder:
            if not silent:
                messagebox.showwarning(
                    "Workspace-Profil",
                    "Bitte Workspace und lokalen Ordner auswählen.",
                    parent=self,
                )
            return False
        if not Path(local_folder).is_dir():
            if not silent:
                messagebox.showerror(
                    "Workspace-Profil",
                    f"Der lokale Ordner existiert nicht:\n{local_folder}",
                    parent=self,
                )
            return False
        profile = next(
            (p for p in self._profiles if p.workspace_id == workspace_id),
            None,
        )
        if profile is None:
            profile = SyncProfile(id=workspace_id, workspace_id=workspace_id)
            self._profiles.append(profile)
        profile.workspace_name = workspace_name
        profile.local_folder = local_folder
        profile.remote_folder_id = (
            self.folder_var.get().strip().replace("\\", "/").strip("/")
        )
        try:
            interval = int(self.interval_var.get() or 60)
        except (TypeError, ValueError, tk.TclError):
            interval = 60
        profile.sync_interval_seconds = max(15, interval)
        profile.sync_deletions = bool(self.deletions_var.get())
        profile.enabled = True
        self.settings.profiles = self._profiles
        self._selected_profile_id = profile.id
        self._refresh_profile_tree()
        self._load_profile_into_form()
        self._refresh_encryption_state()
        self._refresh_storage()
        return True

    def _remove_profile(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        if not messagebox.askyesno(
            "Profil entfernen",
            f"Workspace-Profil „{profile.display_name}“ entfernen?\n\n"
            "Der lokale Ordner und die Dateien bleiben erhalten.",
            parent=self,
        ):
            return
        self._profiles.remove(profile)
        self._selected_profile_id = self._profiles[0].id if self._profiles else ""
        self._refresh_profile_tree()
        if self._selected_profile_id:
            self._load_profile_into_form()
        else:
            self._clear_profile_form()
        self._refresh_encryption_state()
        self._refresh_storage()

    # --- Login / Workspaces ---------------------------------------------

    def _refresh_login_state(self) -> None:
        token = TokenStore().load()
        if token:
            email = token.email or "Tarkle-Konto"
            self.login_status.config(text=f"Angemeldet als {email}")
            self.login_button.pack_forget()
            self.logout_button.pack(side="right")
            for widget in (
                self.workspace_combo,
                self.workspace_button,
                self.local_entry,
                self.browse_button,
                self.interval_spin,
                self.deletions_check,
                self.apply_profile_button,
                self.remove_profile_button,
                self.encryption_button,
            ):
                widget.config(state="normal")
            self.profile_tree.state(["!disabled"])
            if self._profiles:
                if not self._selected_profile_id or not self.settings.profile(
                    self._selected_profile_id
                ):
                    self._selected_profile_id = self._profiles[0].id
                self._load_profile_into_form()
            if not self._workspaces and not self._workspace_load_attempted:
                self._workspace_load_attempted = True
                self.after(250, self._load_workspaces)
        else:
            self.login_status.config(text="Noch nicht bei Tarkle angemeldet.")
            self.logout_button.pack_forget()
            self.login_button.pack(side="right")
            self.login_button.config(state="normal")
            self.workspace_display_var.set("")
            self.local_var.set("")
            self.folder_var.set("")
            self.interval_var.set(60)
            self.deletions_var.set(False)
            for widget in (
                self.workspace_combo,
                self.workspace_button,
                self.local_entry,
                self.browse_button,
                self.interval_spin,
                self.deletions_check,
                self.apply_profile_button,
                self.remove_profile_button,
                self.encryption_button,
            ):
                widget.config(state="disabled")
            self.profile_tree.state(["disabled"])
            self.danger_frame.pack_forget()
            self.encryption_status_label.config(text="Bitte bei Tarkle anmelden.")
            self.storage_value_var.set("Nicht angemeldet.")
            self.storage_bar["value"] = 0
        self._refresh_encryption_state()

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
        self.workspace_button.config(
            state="normal" if token else "disabled", text="Laden"
        )
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
        profile = self._selected_profile()
        if profile is not None:
            selected = next(
                (
                    display
                    for display, (workspace_id, _name) in self._workspaces.items()
                    if workspace_id == profile.workspace_id
                ),
                "",
            )
            if selected:
                self.workspace_display_var.set(selected)
        elif len(values) == 1:
            self.workspace_display_var.set(values[0])

    # --- Storage ---------------------------------------------------------

    def _refresh_storage(self) -> None:
        token = TokenStore().load()
        if token is None:
            self.storage_value_var.set("Nicht angemeldet.")
            self.storage_bar["value"] = 0
            return
        workspace_id, _name = self._profile_from_fields()
        if not workspace_id:
            self.storage_value_var.set("Kein Workspace ausgewählt.")
            self.storage_bar["value"] = 0
            return
        cached = self._storage_cache.get(workspace_id)
        if cached is not None:
            self._set_storage(cached)
            return
        if self._storage_running:
            self._storage_pending = True
            return
        self._storage_running = True
        self.storage_value_var.set("Lade Speicherplatz …")

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
        self._storage_running = False
        if TokenStore().load() is None:
            self.storage_value_var.set("Nicht angemeldet.")
            self.storage_bar["value"] = 0
            self._storage_pending = False
            return
        if error is not None:
            self.storage_value_var.set(f"Speicher nicht verfügbar: {error}")
            self.storage_bar["value"] = 0
        else:
            if usage is not None:
                self._storage_cache[workspace_id] = usage
            current_workspace, _name = self._profile_from_fields()
            if current_workspace == workspace_id and usage is not None:
                self._set_storage(usage)
        if self._storage_pending:
            self._storage_pending = False
            self.after(0, self._refresh_storage)

    def _set_storage(self, usage: dict[str, int]) -> None:
        used = int(usage.get("used", 0))
        limit = int(usage.get("limit", 0))
        percent = (used * 100 / limit) if limit else 0.0
        if limit:
            self.storage_value_var.set(
                f"{_format_bytes(used)} von {_format_bytes(limit)} belegt "
                f"({percent:.1f} %)".replace(".", ",")
            )
        else:
            self.storage_value_var.set(f"{_format_bytes(used)} belegt")
        self.storage_bar["value"] = max(0.0, min(100.0, percent))

    def _on_local_or_workspace_changed(self) -> None:
        self._refresh_encryption_state()
        self._refresh_storage()

    # --- Encryption ------------------------------------------------------

    def _encryption_manager(self) -> EncryptionManager | None:
        if TokenStore().load() is None:
            return None
        local = self.local_var.get().strip()
        if not local:
            return None
        path = Path(local)
        if not path.is_dir():
            return None
        profile_id = self._selected_profile_id or "default"
        manager = EncryptionManager(
            path,
            keyring_username=crypto_keyring_username(profile_id, str(path)),
        )
        manager.load_cached_key()
        return manager

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

    def _refresh_encryption_state(self) -> None:
        manager = self._encryption_manager()
        if manager is None:
            self.encryption_status_label.config(
                text="Bitte anmelden und lokalen Ordner auswählen."
            )
            self.encryption_button.config(state="disabled")
            self.lock_button.pack_forget()
            self.danger_frame.pack_forget()
            return
        self.encryption_button.config(state="normal")
        self.lock_button.pack_forget()
        if not manager.enabled:
            self.encryption_status_label.config(text="Nicht eingerichtet.")
            self.encryption_button.config(text="Passwort festlegen …")
            self.danger_frame.pack_forget()
        elif manager.unlocked:
            self.encryption_status_label.config(text="Aktiv – entsperrt.")
            self.encryption_button.config(text="Passwort ändern …")
            self.lock_button.pack(side="right", padx=(0, 8))
            if not self.danger_frame.winfo_manager():
                self.danger_frame.pack(fill="x", pady=(12, 0))
        else:
            self.encryption_status_label.config(text="Aktiv – gesperrt.")
            self.encryption_button.config(text="Entsperren …")
            if not self.danger_frame.winfo_manager():
                self.danger_frame.pack(fill="x", pady=(12, 0))

    def _set_encryption_password(self) -> None:
        manager = self._encryption_manager()
        if manager is None:
            messagebox.showwarning(
                "Verschlüsselung",
                "Bitte zuerst bei Tarkle anmelden und einen gültigen lokalen Ordner auswählen.",
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
            messagebox.showerror(
                "Verschlüsselung",
                "Die Passwörter stimmen nicht überein.",
                parent=self,
            )
            return
        try:
            manager.set_password(
                new,
                allow_existing=self._encryption_reset_allowed,
            )
        except CryptoError as exc:
            messagebox.showerror("Verschlüsselung", str(exc), parent=self)
            return
        self._encryption_reset_allowed = False
        messagebox.showinfo(
            "Verschlüsselung",
            "Verschlüsselung eingerichtet.",
            parent=self,
        )
        self._refresh_encryption_state()

    def _lock_encryption(self) -> None:
        manager = self._encryption_manager()
        if manager is None or not manager.unlocked:
            return
        manager.lock(clear_cache=True)
        messagebox.showinfo(
            "Verschlüsselung",
            "Verschlüsselung gesperrt.",
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
        messagebox.showinfo(
            "Verschlüsselung", "Verschlüsselung entsperrt.", parent=self
        )
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
            "Verschlüsselung",
            "Passwort geändert.",
            parent=self,
        )
        self._refresh_encryption_state()

    def _reset_encryption_key(self) -> None:
        manager = self._encryption_manager()
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
        profile = self._selected_profile()

        def worker() -> None:
            try:
                if self.on_reset_encryption is not None and profile is not None:
                    ok, message = self.on_reset_encryption(profile)
                else:
                    manager.reset_key()
                    ok, message = True, "Schlüssel gelöscht."
            except Exception as exc:  # noqa: BLE001 - im UI anzeigen
                ok, message = False, str(exc)
            self._ui_queue.put(
                lambda ok=ok, message=message: self._reset_done(ok, message)
            )

        threading.Thread(
            target=worker,
            name="tarklone-reset-encryption",
            daemon=True,
        ).start()

    def _reset_done(self, ok: bool, message: str) -> None:
        self.danger_button.config(state="normal", text="Schlüssel löschen …")
        if ok:
            self._encryption_reset_allowed = True
            self._refresh_encryption_state()
            messagebox.showinfo(
                "Verschlüsselung zurückgesetzt",
                message,
                parent=self,
            )
        else:
            self._refresh_encryption_state()
            messagebox.showerror(
                "Zurücksetzen fehlgeschlagen",
                message,
                parent=self,
            )

    def _encryption_action(self) -> None:
        manager = self._encryption_manager()
        if manager is None:
            messagebox.showwarning(
                "Verschlüsselung",
                "Bitte zuerst anmelden und einen gültigen lokalen Ordner auswählen.",
                parent=self,
            )
            return
        if not manager.enabled:
            self._set_encryption_password()
        elif not manager.unlocked:
            self._unlock_encryption(manager)
        else:
            self._change_encryption_password(manager)

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
        self._refresh_login_state()

    def _browse(self) -> None:
        selected = filedialog.askdirectory(
            parent=self,
            title="Lokalen Sync-Ordner auswählen",
            initialdir=self.local_var.get() or str(Path.home()),
        )
        if selected:
            self.local_var.set(selected)

    # --- Save ------------------------------------------------------------

    def _save(self) -> None:
        if TokenStore().load() is None:
            messagebox.showwarning(
                "Einrichtung",
                "Bitte zuerst bei Tarkle anmelden.",
                parent=self,
            )
            return
        self._apply_profile(silent=True)
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
