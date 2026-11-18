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
from uuid import uuid4

from . import __version__
from .api import TarkleApi
from .auth import (
    ApiKeyStore,
    clear_legacy_oauth_token,
    mask_api_key,
)
from .config import (
    Settings,
    SyncFolder,
    SyncProfile,
    crypto_keyring_username,
    is_api_key,
    normalize_api_key,
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
        self._allow_existing_after_reset = False
        self.local_var = tk.StringVar(value=profile.local_folder)
        self.folder_var = tk.StringVar(value=profile.remote_folder_id)
        self.interval_var = tk.IntVar(value=profile.sync_interval_seconds)
        self.deletions_var = tk.BooleanVar(value=profile.sync_deletions)
        self.multi_var = tk.BooleanVar(value=profile.sync_mode == "multi")
        self.folders: list[SyncFolder] = [
            SyncFolder(
                id=folder.id,
                local_folder=folder.local_folder,
                remote_folder_id=folder.remote_folder_id,
            )
            for folder in profile.folders
        ]
        self._selected_folder_id = ""
        self.storage_value_var = tk.StringVar(value="Noch nicht geladen.")
        self.encryption_target_var = tk.StringVar(value="")
        self._build()
        self.local_var.trace_add("write", self._on_local_changed)
        self.multi_var.trace_add("write", self._on_mode_changed)
        self._refresh_sync_mode()
        self.refresh_encryption()

    def _build(self) -> None:
        outer = tk.Frame(self, bg=BACKGROUND)
        outer.pack(fill="both", expand=True, padx=14, pady=12)

        sync_box = _section(outer, "Sync")
        sync_box.pack(fill="x", pady=(0, 10))
        sync_inner = tk.Frame(sync_box, bg=CARD)
        sync_inner.pack(fill="x")
        sync_inner.columnconfigure(1, weight=1)

        self.multi_check = ttk.Checkbutton(
            sync_inner,
            text="Mehrere lokale Ordner synchronisieren",
            variable=self.multi_var,
        )
        self.multi_check.grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )

        self.single_frame = tk.Frame(sync_inner, bg=CARD)
        self.single_frame.grid(
            row=1, column=0, columnspan=2, sticky="ew"
        )
        self.single_frame.columnconfigure(1, weight=1)
        tk.Label(
            self.single_frame,
            text="Lokaler Ordner",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 9),
            anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))
        local_row = tk.Frame(self.single_frame, bg=CARD)
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
            self.single_frame,
            text="Remote-Ordner (Pfad)",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 9),
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(self.single_frame, textvariable=self.folder_var).grid(
            row=1, column=1, sticky="ew", padx=(12, 0), pady=(0, 6)
        )
        tk.Label(
            self.single_frame,
            text="Leer = Workspace-Root, z. B. Kunden/Projekt",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).grid(row=2, column=1, sticky="w", padx=(12, 0), pady=(0, 7))

        self.multi_frame = tk.Frame(sync_inner, bg=CARD)
        self.multi_frame.grid(
            row=1, column=0, columnspan=2, sticky="ew"
        )
        self.multi_frame.columnconfigure(0, weight=1)
        tk.Label(
            self.multi_frame,
            text="Lokale Ordner und ihre Remote-Ordner im Workspace",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 9),
            anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 5))
        mapping_wrap = tk.Frame(self.multi_frame, bg=CARD)
        mapping_wrap.grid(row=1, column=0, sticky="ew")
        mapping_wrap.columnconfigure(0, weight=1)
        self.mapping_tree = ttk.Treeview(
            mapping_wrap,
            columns=("local", "remote"),
            show="headings",
            height=4,
            selectmode="browse",
        )
        self.mapping_tree.heading("local", text="Lokaler Ordner")
        self.mapping_tree.heading("remote", text="Workspace-Ordner")
        self.mapping_tree.column("local", width=310, stretch=True)
        self.mapping_tree.column("remote", width=210, stretch=True)
        self.mapping_tree.grid(row=0, column=0, sticky="ew")
        mapping_scroll = ttk.Scrollbar(
            mapping_wrap,
            orient="vertical",
            command=self.mapping_tree.yview,
        )
        mapping_scroll.grid(row=0, column=1, sticky="ns")
        self.mapping_tree.configure(yscrollcommand=mapping_scroll.set)
        self.mapping_tree.bind("<<TreeviewSelect>>", self._on_mapping_select)
        self.mapping_tree.bind("<Double-1>", lambda _event: self._edit_mapping())
        mapping_buttons = tk.Frame(self.multi_frame, bg=CARD)
        mapping_buttons.grid(row=2, column=0, sticky="w", pady=(6, 4))
        ttk.Button(
            mapping_buttons,
            text="Ordner hinzufügen …",
            command=self._add_mapping,
        ).pack(side="left")
        ttk.Button(
            mapping_buttons,
            text="Remote ändern …",
            command=self._edit_mapping,
        ).pack(side="left", padx=(6, 0))
        ttk.Button(
            mapping_buttons,
            text="Entfernen",
            command=self._remove_mapping,
        ).pack(side="left", padx=(6, 0))
        tk.Label(
            self.multi_frame,
            text="Jeder lokale Ordner wird auf den angegebenen Ordner im "
            "Workspace abgebildet. Leerer Remote-Pfad = Workspace-Root.",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 8),
            wraplength=540,
            justify="left",
            anchor="w",
        ).grid(row=3, column=0, sticky="w", pady=(0, 5))

        tk.Label(
            sync_inner,
            text="Sync-Intervall (Sekunden)",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 9),
            anchor="w",
        ).grid(row=2, column=0, sticky="w", pady=(0, 6))
        self.interval_spin = ttk.Spinbox(
            sync_inner,
            from_=15,
            to=3600,
            increment=15,
            textvariable=self.interval_var,
            width=8,
        )
        self.interval_spin.grid(
            row=2, column=1, sticky="w", padx=(12, 0), pady=(0, 6)
        )

        self.deletions_check = ttk.Checkbutton(
            sync_inner,
            text="Löschungen in beide Richtungen übertragen",
            variable=self.deletions_var,
        )
        self.deletions_check.grid(
            row=3, column=1, sticky="w", padx=(12, 0), pady=(0, 2)
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
        ).grid(row=4, column=1, sticky="w", padx=(12, 0))

        encryption_box = _section(outer, "Verschlüsselung")
        encryption_box.pack(fill="x", pady=(0, 10))
        encryption_inner = tk.Frame(encryption_box, bg=CARD)
        encryption_inner.pack(fill="x")
        tk.Label(
            encryption_box,
            textvariable=self.encryption_target_var,
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
            justify="left",
            wraplength=540,
        ).pack(fill="x", pady=(0, 5))
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

    def _on_mode_changed(self, *_args) -> None:
        if self.multi_var.get():
            if not self.folders:
                local_folder = self.local_var.get().strip()
                if local_folder:
                    remote = (
                        self.folder_var.get().strip().replace("\\", "/").strip("/")
                        or Path(local_folder).name
                    )
                    self.folders.append(
                        SyncFolder(
                            id=self.profile.id,
                            local_folder=local_folder,
                            remote_folder_id=remote,
                        )
                    )
            self._refresh_mapping_tree()
        else:
            selected = self._selected_folder()
            if selected is not None:
                self.local_var.set(selected.local_folder)
                self.folder_var.set(selected.remote_folder_id)
        self._refresh_sync_mode()

    def _refresh_sync_mode(self) -> None:
        if self.multi_var.get():
            self.single_frame.grid_remove()
            self.multi_frame.grid()
            self._refresh_mapping_tree()
        else:
            self.multi_frame.grid_remove()
            self.single_frame.grid()
        self._sync_profile_folders()
        self.refresh_encryption()

    def _sync_profile_folders(self) -> None:
        self.profile.sync_mode = "multi" if self.multi_var.get() else "single"
        self.profile.folders = [
            SyncFolder(
                id=folder.id,
                local_folder=folder.local_folder.strip(),
                remote_folder_id=folder.remote_folder_id.replace("\\", "/").strip("/"),
            )
            for folder in self.folders
        ]
        if self.profile.sync_mode == "multi":
            first = next(
                (folder for folder in self.profile.folders if folder.local_folder),
                None,
            )
            if first is not None:
                self.profile.local_folder = first.local_folder
                self.profile.remote_folder_id = first.remote_folder_id
            self.profile.enabled = bool(
                self.profile.workspace_id and self.profile.folders
            )
        else:
            self.profile.enabled = bool(
                self.local_var.get().strip() and self.profile.workspace_id
            )

    def _refresh_mapping_tree(self) -> None:
        if not hasattr(self, "mapping_tree"):
            return
        selected = self._selected_folder_id
        self.mapping_tree.delete(*self.mapping_tree.get_children())
        for folder in self.folders:
            self.mapping_tree.insert(
                "",
                "end",
                iid=folder.id,
                values=(
                    folder.local_folder,
                    folder.remote_folder_id or "(Workspace-Root)",
                ),
            )
        children = self.mapping_tree.get_children()
        if selected not in children:
            selected = children[0] if children else ""
        self._selected_folder_id = selected
        if selected:
            self.mapping_tree.selection_set(selected)
            self.mapping_tree.focus(selected)

    def _selected_folder(self) -> SyncFolder | None:
        if not self._selected_folder_id:
            return None
        return next(
            (
                folder
                for folder in self.folders
                if folder.id == self._selected_folder_id
            ),
            None,
        )

    def _current_folder(self) -> SyncFolder | None:
        if self.multi_var.get():
            return self._selected_folder()
        local_folder = self.local_var.get().strip()
        if not local_folder:
            return None
        return SyncFolder(
            id=self.profile.id,
            local_folder=local_folder,
            remote_folder_id=(
                self.folder_var.get().strip().replace("\\", "/").strip("/")
            ),
        )

    def _current_local_folder(self) -> str:
        folder = self._current_folder()
        return folder.local_folder if folder is not None else ""

    def _on_mapping_select(self, *_args) -> None:
        selection = self.mapping_tree.selection()
        self._selected_folder_id = selection[0] if selection else ""
        self._sync_profile_folders()
        self.refresh_encryption()

    def _add_mapping(self) -> None:
        selected = filedialog.askdirectory(
            parent=self,
            title="Lokalen Ordner für Multi-Ordner-Sync auswählen",
            initialdir=self._current_local_folder() or str(Path.home()),
        )
        if not selected:
            return
        if any(
            Path(folder.local_folder).resolve() == Path(selected).resolve()
            for folder in self.folders
        ):
            messagebox.showwarning(
                "Multi-Ordner-Sync",
                "Dieser lokale Ordner ist bereits vorhanden.",
                parent=self,
            )
            return
        remote = simpledialog.askstring(
            "Remote-Ordner",
            "Pfad im Tarkle-Workspace (leer = Workspace-Root):",
            initialvalue=Path(selected).name,
            parent=self,
        )
        if remote is None:
            return
        folder = SyncFolder(
            id=uuid4().hex,
            local_folder=selected,
            remote_folder_id=remote.replace("\\", "/").strip("/"),
        )
        self.folders.append(folder)
        self._selected_folder_id = folder.id
        self._sync_profile_folders()
        self._refresh_mapping_tree()
        self.refresh_encryption()

    def _edit_mapping(self) -> None:
        folder = self._selected_folder()
        if folder is None:
            messagebox.showinfo(
                "Multi-Ordner-Sync",
                "Bitte zuerst einen Ordner auswählen.",
                parent=self,
            )
            return
        remote = simpledialog.askstring(
            "Remote-Ordner ändern",
            "Pfad im Tarkle-Workspace (leer = Workspace-Root):",
            initialvalue=folder.remote_folder_id,
            parent=self,
        )
        if remote is None:
            return
        folder.remote_folder_id = remote.replace("\\", "/").strip("/")
        self._sync_profile_folders()
        self._refresh_mapping_tree()
        self.refresh_encryption()

    def _remove_mapping(self) -> None:
        folder = self._selected_folder()
        if folder is None:
            return
        if not messagebox.askyesno(
            "Ordner entfernen",
            f"Mapping für „{folder.local_folder}“ entfernen?\n\n"
            "Die Dateien selbst werden nicht gelöscht.",
            parent=self,
        ):
            return
        self.folders = [item for item in self.folders if item.id != folder.id]
        self._selected_folder_id = ""
        self._sync_profile_folders()
        self._refresh_mapping_tree()
        self.refresh_encryption()

    def _on_local_changed(self, *_args) -> None:
        if self.multi_var.get():
            return
        local_folder = self.local_var.get().strip()
        self.profile.local_folder = local_folder
        self.profile.enabled = bool(local_folder and self.profile.workspace_id)
        self.refresh_encryption()

    def collect(self) -> None:
        try:
            interval = int(self.interval_var.get() or 60)
        except (TypeError, ValueError, tk.TclError):
            interval = 60
        self.profile.sync_interval_seconds = max(15, interval)
        self.profile.sync_deletions = bool(self.deletions_var.get())
        self.profile.sync_mode = "multi" if self.multi_var.get() else "single"
        if self.profile.sync_mode == "multi":
            self._sync_profile_folders()
            self.profile.enabled = bool(
                self.profile.workspace_id
                and any(folder.local_folder for folder in self.folders)
            )
            return
        self.profile.local_folder = self.local_var.get().strip()
        self.profile.remote_folder_id = (
            self.folder_var.get().strip().replace("\\", "/").strip("/")
        )
        self._sync_profile_folders()
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
        folder = self._current_folder()
        local = folder.local_folder if folder is not None else ""
        if folder is not None:
            self.encryption_target_var.set(
                "Ausgewählter Ordner: "
                f"{folder.local_folder} → "
                f"{folder.remote_folder_id or 'Workspace-Root'}"
            )
        elif self.multi_var.get():
            self.encryption_target_var.set(
                "Bitte zuerst im Sync-Abschnitt einen lokalen Ordner auswählen."
            )
        else:
            self.encryption_target_var.set(
                "Noch kein lokaler Ordner für die Verschlüsselung ausgewählt."
            )
        manager = self.dialog.encryption_manager(self.profile, local)
        self.lock_button.pack_forget()
        if manager is None:
            if not local:
                text = (
                    "Bitte zuerst einen Ordner auswählen."
                    if self.multi_var.get()
                    else "Lokalen Ordner auswählen."
                )
            elif not Path(local).is_dir():
                text = "Lokaler Ordner nicht gefunden."
            else:
                text = "Bitte einen Tarkle-API-Key speichern."
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
            self.profile, self._current_local_folder()
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
            "Neues Passwort (mindestens 12 Zeichen; am besten eine lange Passphrase):",
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
                allow_existing=self._allow_existing_after_reset,
            )
        except CryptoError as exc:
            messagebox.showerror("Verschlüsselung", str(exc), parent=self)
            return
        self._allow_existing_after_reset = False
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
            "Neues Passwort (mindestens 12 Zeichen; am besten eine lange Passphrase):",
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
        cleared = manager.lock(clear_cache=True)
        if not cleared:
            messagebox.showwarning(
                "Verschlüsselung",
                "Der Schlüssel wurde aus dem Arbeitsspeicher entfernt, aber der "
                "System-Keyring konnte nicht zuverlässig geleert werden. Prüfe den "
                "Keyring, bevor du das Gerät weitergibst.",
                parent=self,
            )
            self.refresh_encryption()
            return
        messagebox.showinfo(
            "Verschlüsselung",
            "Verschlüsselung gesperrt.",
            parent=self,
        )
        self.refresh_encryption()

    def _reset_encryption_key(self) -> None:
        folder = self._current_folder()
        local_folder = folder.local_folder if folder is not None else ""
        manager = self.dialog.encryption_manager(self.profile, local_folder)
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
        selected_folder = folder

        def worker() -> None:
            try:
                if self.dialog.on_reset_encryption is not None:
                    ok, message = self.dialog.on_reset_encryption(
                        profile, selected_folder
                    )
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
            self._allow_existing_after_reset = True
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
        on_reset_encryption: (
            Callable[[SyncProfile, SyncFolder | None], tuple[bool, str]] | None
        ) = None,
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
        self._api_key_running = False
        self._update_running = False
        self._workspaces_loading = False
        self._workspace_load_attempted = False
        self._workspaces_loaded_ok = False
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
        self.api_key_var = tk.StringVar(value=ApiKeyStore().load() or "")
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
        login_top = tk.Frame(login_inner, bg=CARD)
        login_top.pack(fill="x")
        self.login_status = tk.Label(
            login_top,
            text="",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
        )
        self.login_status.pack(side="left", fill="x", expand=True)
        self.api_key_button = ttk.Button(
            login_top,
            text="Key speichern",
            command=self._save_api_key_async,
            width=14,
        )
        self.api_key_button.pack(side="right")
        self.logout_button = ttk.Button(
            login_top,
            text="Key entfernen",
            command=self._remove_api_key,
            width=13,
        )
        key_row = tk.Frame(login_inner, bg=CARD)
        key_row.pack(fill="x", pady=(8, 0))
        tk.Label(
            key_row,
            text="API-Key",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 9),
            width=8,
            anchor="w",
        ).pack(side="left")
        self.api_key_entry = ttk.Entry(
            key_row,
            textvariable=self.api_key_var,
            show="•",
        )
        self.api_key_entry.pack(side="left", fill="x", expand=True)
        self.api_key_entry.bind("<Return>", lambda _event: self._save_api_key_async())
        tk.Label(
            login_inner,
            text=(
                "Key erstellen unter tarkle.com → Einstellungen → API → Create. "
                "Der Key beginnt mit tk_ und wird nur einmal angezeigt. "
                "Er wird im Windows Credential Manager gespeichert."
            ),
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 8),
            wraplength=560,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(5, 0))

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
                text="Beim Windows-Start automatisch starten",
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
            if show_load and ApiKeyStore().load() is not None:
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
        if ApiKeyStore().load() is None:
            self._show_placeholder(
                "API-Key eintragen, um die Tarkle-Workspaces zu laden."
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
                    if profile.enabled and profile.active_folders()
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

    # --- API-Key / Workspaces -------------------------------------------

    def _refresh_login_state(self) -> None:
        key = ApiKeyStore().load()
        if key:
            self.api_key_var.set(key)
            self.login_status.config(
                text=f"API-Key gespeichert ({mask_api_key(key)})."
            )
            self.logout_button.pack(side="right", padx=(0, 6))
            self.api_key_button.config(state="normal", text="Key aktualisieren")
            if not self._tabs:
                self._rebuild_tabs()
            if not self._workspace_load_attempted:
                self._workspace_load_attempted = True
                self.after(200, self._load_workspaces)
        else:
            self.login_status.config(text="Noch kein Tarkle-API-Key gespeichert.")
            self.logout_button.pack_forget()
            self.api_key_button.config(state="normal", text="Key speichern")
            self._clear_tabs()
            self._show_placeholder(
                "API-Key eintragen, um die Tarkle-Workspaces zu laden."
            )
            self._storage_cache.clear()
            self._storage_loading.clear()

    def _save_api_key_async(self) -> None:
        if self._api_key_running:
            return
        key = normalize_api_key(self.api_key_var.get())
        if not is_api_key(key):
            messagebox.showwarning(
                "Tarkle-API-Key",
                "Bitte einen gültigen API-Key eintragen. Er beginnt mit tk_ "
                "und wird in Tarkle unter Einstellungen → API erstellt.",
                parent=self,
            )
            return
        self._api_key_running = True
        self.api_key_button.config(state="disabled", text="Prüfe …")
        self.login_status.config(text="Prüfe API-Key bei Tarkle …")

        def worker() -> None:
            api = None
            try:
                api = TarkleApi(key)
                workspaces = api.list_workspaces()
            except Exception as exc:  # noqa: BLE001 - im UI anzeigen
                error = exc
                self._ui_queue.put(
                    lambda error=error, key=key: self._api_key_failed(key, error)
                )
            else:
                self._ui_queue.put(
                    lambda workspaces=workspaces, key=key: self._api_key_saved(
                        key, workspaces
                    )
                )
            finally:
                if api is not None:
                    api.close()

        threading.Thread(
            target=worker, name="tarklone-api-key", daemon=True
        ).start()

    def _api_key_saved(self, key: str, workspaces) -> None:  # noqa: ANN001
        self._api_key_running = False
        try:
            ApiKeyStore().save(key)
        except Exception as exc:  # noqa: BLE001 - im UI anzeigen
            self._api_key_failed(key, exc)
            return
        clear_legacy_oauth_token()
        self.api_key_button.config(state="normal", text="Key aktualisieren")
        self._workspace_load_attempted = True
        self._storage_cache.clear()
        self._storage_loading.clear()
        self._workspaces_loaded(workspaces, None)
        messagebox.showinfo(
            "Tarkle-API-Key",
            f"API-Key gespeichert ({mask_api_key(key)}). "
            f"{len(workspaces)} Workspace(s) verfügbar.",
            parent=self,
        )

    def _api_key_failed(self, key: str, exc: Exception) -> None:
        self._api_key_running = False
        self.api_key_button.config(state="normal", text="Key speichern")
        self.login_status.config(text="API-Key konnte nicht geprüft werden.")
        messagebox.showerror(
            "Tarkle-API-Key",
            f"Der API-Key wurde nicht gespeichert:\n{exc}",
            parent=self,
        )

    def _remove_api_key(self) -> None:
        if not messagebox.askyesno(
            "API-Key entfernen",
            "Tarkle-API-Key wirklich vom Gerät entfernen?",
            parent=self,
        ):
            return
        deleted = ApiKeyStore().delete()
        if not deleted:
            messagebox.showerror(
                "API-Key entfernen",
                "Der Key konnte nicht zuverlässig aus dem System-Keyring "
                "entfernt werden. Bitte Credential Manager/Keyring prüfen.",
                parent=self,
            )
            return
        clear_legacy_oauth_token()
        self.api_key_var.set("")
        self._workspaces_by_id.clear()
        self._workspace_load_attempted = False
        self._workspaces_loaded_ok = False
        self._storage_cache.clear()
        self._storage_loading.clear()
        self._refresh_login_state()
        messagebox.showinfo(
            "API-Key entfernt",
            "Der Tarkle-API-Key wurde von diesem Gerät entfernt. Die übrigen "
            "Einstellungen bleiben gespeichert. Zum Widerrufen den Key "
            "zusätzlich in Tarkle unter Einstellungen → API löschen.",
            parent=self,
        )

    def _load_workspaces(self) -> None:
        if self._workspaces_loading:
            return
        key = ApiKeyStore().load()
        if key is None:
            messagebox.showinfo(
                "Tarkle-Workspaces",
                "Bitte zuerst einen Tarkle-API-Key eintragen.",
                parent=self,
            )
            return
        self._workspaces_loading = True
        self.login_status.config(text="Lade Tarkle-Workspaces …")

        def worker() -> None:
            api = None
            try:
                api = TarkleApi(key)
                workspaces = api.list_workspaces()
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
                if api is not None:
                    api.close()

        threading.Thread(
            target=worker, name="tarklone-workspaces", daemon=True
        ).start()

    def _workspaces_loaded(self, workspaces, error) -> None:  # noqa: ANN001
        self._workspaces_loading = False
        key = ApiKeyStore().load()
        if key is None:
            return
        self.login_status.config(
            text=f"API-Key gespeichert ({mask_api_key(key)})."
        )
        if error:
            if not self._tabs:
                self._show_placeholder(
                    "Workspaces konnten nicht geladen werden.",
                    show_load=True,
                )
            messagebox.showerror("Workspaces laden", str(error), parent=self)
            return

        self._workspaces_loaded_ok = True
        returned: dict[str, str] = {}
        for item in workspaces:
            workspace_key = str(
                item.get("workspace_key") or item.get("id") or ""
            )
            if not workspace_key:
                continue
            returned[workspace_key] = str(
                item.get("name") or workspace_key
            )
        self._workspaces_by_id = returned
        existing = {
            profile.workspace_id: profile
            for profile in self._profiles
            if profile.workspace_id
        }
        used_ids = {profile.id for profile in self._profiles}
        for workspace_key, name in returned.items():
            profile = existing.get(workspace_key)
            if profile is None:
                # Migration alter OAuth-Profile: gleicher Workspace-Name.
                candidate = next(
                    (
                        item
                        for item in self._profiles
                        if item.workspace_id not in returned
                        and item.workspace_name.strip().casefold()
                        == name.strip().casefold()
                    ),
                    None,
                )
                if candidate is not None:
                    candidate.workspace_id = workspace_key
                    candidate.workspace_name = name
                    profile = candidate
                else:
                    profile_id = workspace_key
                    while profile_id in used_ids:
                        profile_id = f"{workspace_key}-{uuid4().hex[:6]}"
                    profile = SyncProfile(
                        id=profile_id,
                        workspace_id=workspace_key,
                        workspace_name=name,
                        local_folder="",
                        enabled=False,
                    )
                    self._profiles.append(profile)
                    used_ids.add(profile_id)
            else:
                profile.workspace_name = name
            existing[workspace_key] = profile

        # Profile, die der Key nicht mehr erreicht, werden deaktiviert, aber
        # nicht gelöscht (lokale Ordner und Sync-Zustand bleiben erhalten).
        for profile in self._profiles:
            if profile.workspace_id not in returned:
                profile.enabled = False

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
            profile = next(
                (item for item in self._profiles if item.id == profile_id),
                None,
            )
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
                    if profile.enabled and profile.active_folders()
                ),
                self._profiles[0].id,
            )
            self._select_profile_tab(target)

    # --- Speicherplatz ---------------------------------------------------

    def request_storage(self, tab: WorkspaceTab) -> None:
        api_key = ApiKeyStore().load()
        if not api_key:
            tab.set_storage_message("Kein API-Key gespeichert.")
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
            api = None
            try:
                api = TarkleApi(api_key)
                usage = api.usage(workspace_id)
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
                if api is not None:
                    api.close()

        threading.Thread(
            target=worker, name="tarklone-storage", daemon=True
        ).start()

    def _storage_done(self, workspace_id: str, usage, error) -> None:  # noqa: ANN001
        self._storage_loading.discard(workspace_id)
        if ApiKeyStore().load() is None:
            for tab in self._tabs.values():
                if tab.profile.workspace_id == workspace_id:
                    tab.set_storage_message("Kein API-Key gespeichert.")
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
        if ApiKeyStore().load() is None:
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

    # --- Speichern -------------------------------------------------------

    def _save(self) -> None:
        # Der Autostart wird sofort übernommen, damit er sich auch dann
        # entfernen lässt, wenn andere Angaben (noch) unvollständig sind.
        launch_at_login = bool(self.launch_var.get())
        apply_launch_at_login(launch_at_login)
        typed_key = normalize_api_key(self.api_key_var.get())
        stored_key = ApiKeyStore().load() or ""
        if typed_key != stored_key:
            messagebox.showwarning(
                "Tarkle-API-Key",
                "Der eingegebene API-Key wurde noch nicht gespeichert. Bitte "
                "zuerst „Key speichern“ wählen oder das Feld leeren und "
                "„Key entfernen“ verwenden.",
                parent=self,
            )
            return
        for tab in self._tabs.values():
            tab.collect()
        if self._workspaces_loaded_ok:
            for profile in self._profiles:
                if profile.workspace_id not in self._workspaces_by_id:
                    profile.enabled = False
        self.settings.profiles = self._profiles
        self.settings.start_minimized = bool(self.start_minimized_var.get())
        self.settings.launch_at_login = launch_at_login
        self.settings.update_check_interval_days = self._update_days_value()
        self.settings.sync_legacy_fields()
        if ApiKeyStore().load() is not None:
            errors = self.settings.validation_errors()
            if errors:
                messagebox.showwarning(
                    "Einrichtung unvollständig",
                    "\n".join(errors),
                    parent=self,
                )
                return
        # Ohne API-Key werden die allgemeinen Einstellungen trotzdem
        # gespeichert. So lässt sich z. B. der Windows-Autostart auch dann
        # entfernen, wenn gerade kein gültiger Key hinterlegt ist.
        save_settings(self.settings)
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
