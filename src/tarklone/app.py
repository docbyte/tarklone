from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import replace
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont
from pystray import Menu, MenuItem
from watchdog.events import FileSystemEventHandler

from . import __version__
from .auth import AuthError, NotLoggedIn, OAuthClient, TokenStore
from .config import (
    SyncProfile,
    crypto_keyring_username,
    ensure_dirs,
    load_settings,
    log_path,
    profile_state_path,
    save_settings,
    state_path,
)
from .crypto import KEYFILE_NAME, EncryptionManager
from .encrypted_remote import EncryptionAdapter
from .state import SyncState
from .sync import RemoteUnsupported, SyncBusy, SyncEngine, SyncError, SyncProgress
from .ui import SetupDialog
from .updates import check_for_update, download_update
from .webapi import WebAuthError

logger = logging.getLogger("tarklone")

COLORS = {
    "idle": "#714CB6",
    "syncing": "#2F80ED",
    "paused": "#E08A2E",
    "error": "#D64545",
    "offline": "#8C8C96",
}


def open_path(path: Path | str) -> None:
    """Öffnet einen Ordner/eine Datei mit dem Systemstandard."""
    target = str(path)
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


def make_icon(color: str = COLORS["idle"]) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=16, fill=color)
    font = None
    for candidate in (
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        try:
            font = ImageFont.truetype(candidate, 34)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    draw.text((32, 29), "T", fill="white", anchor="mm", font=font)
    draw.arc((14, 42, 50, 56), start=200, end=340, fill="white", width=3)
    draw.polygon([(47, 44), (54, 48), (47, 52)], fill="white")
    return image


def setup_logging(level: str = "INFO") -> None:
    ensure_dirs()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if logger.handlers:
        return
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_path(),
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if sys.stderr and sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)


class FileChangeHandler(FileSystemEventHandler):
    def __init__(self, app: "TarkloneApp", profile_id: str):
        self.app = app
        self.profile_id = profile_id

    def on_any_event(self, event) -> None:  # noqa: ANN001 - watchdog event
        try:
            path = str(getattr(event, "src_path", "") or "")
            if not path:
                return
            normalized = path.replace("\\", "/")
            if "/.tarklone/" in normalized or normalized.endswith("/.tarklone"):
                return
            if normalized.endswith((".tmp", ".part", ".tarklone-part")):
                return
            self.app.note_local_change(self.profile_id)
        except Exception:
            pass


class TarkloneApp:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.status = "Bereit"
        self.paused = False
        self.stop_event = threading.Event()
        self._syncing = False
        self._sync_lock = threading.Lock()
        self._watcher = None
        self._settings_window: SetupDialog | None = None
        self.icon: pystray.Icon | None = None
        self.root: tk.Tk | None = None
        self._last_error = ""
        self._ui_queue: queue.Queue = queue.Queue()
        self._update_check_running = False
        self._sync_queue: queue.Queue[str] = queue.Queue()
        self._queued_ids: set[str] = set()
        self._queue_lock = threading.Lock()
        self._local_due: dict[str, float] = {}
        self._last_sync: dict[str, float] = {}
        self._current_profile_id = ""
        self._progress: SyncProgress | None = None
        self._last_progress_refresh = 0.0
        self._lock_notified: set[str] = set()
        self._load_last_sync()

    def _state_for(self, profile: SyncProfile) -> SyncState:
        path = profile_state_path(profile.id)
        existing_states = (
            list(path.parent.glob("*.sqlite3")) if path.parent.exists() else []
        )
        if (
            not path.exists()
            and not existing_states
            and self.settings.profiles
            and profile.id == self.settings.profiles[0].id
            and state_path().exists()
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(state_path(), path)
        return SyncState(path)

    def _load_last_sync(self) -> None:
        self._last_sync.clear()
        for profile in self.settings.enabled_profiles:
            state = None
            try:
                state = self._state_for(profile)
                raw = state.get_meta("last_sync")
                if raw:
                    self._last_sync[profile.id] = datetime.fromisoformat(raw).timestamp()
            except Exception:
                continue
            finally:
                if state is not None:
                    state.close()

    def _enqueue_profile(self, profile_id: str) -> None:
        with self._queue_lock:
            if profile_id in self._queued_ids:
                return
            self._queued_ids.add(profile_id)
        self._sync_queue.put(profile_id)

    def run(self) -> int:
        setup_logging(self.settings.log_level)
        logger.info("tarklone %s startet", __version__)
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("tarklone")
        self.root.report_callback_exception = self._tk_exception
        self.root.protocol("WM_DELETE_WINDOW", self._hide_windows)
        self.root.after(100, self._poll_ui_queue)

        self.icon = pystray.Icon("tarklone", make_icon(), "tarklone", self._menu())
        self.icon.run_detached()

        threading.Thread(target=self._sync_worker, name="tarklone-sync", daemon=True).start()
        threading.Thread(target=self._scheduler, name="tarklone-scheduler", daemon=True).start()
        self._restart_watcher()
        self._refresh_icon()

        if not self.settings.is_configured or TokenStore().load() is None:
            self.root.after(350, lambda: self.show_settings(first_run=True))
        else:
            logger.info(
                "Konfiguration geladen: %d Workspace-Profil(e)",
                len(self.settings.enabled_profiles),
            )

        # Diagnose-/Test-Hook: beendet die App nach X Sekunden, ohne dass
        # jemand das Tray-Menü bedienen muss. Im Normalbetrieb ungesetzt.
        test_quit = os.environ.get("TARKLONE_TEST_QUIT_AFTER", "").strip()
        if test_quit:
            try:
                delay_ms = max(1000, int(float(test_quit) * 1000))
            except ValueError:
                delay_ms = 0
            if delay_ms:
                self.root.after(delay_ms, self._action_quit)

        try:
            self.root.mainloop()
        finally:
            self._shutdown()
        return 0

    def _tk_exception(self, exc_type, exc_value, exc_traceback) -> None:  # noqa: ANN001
        logger.exception("Tkinter-Fehler", exc_info=(exc_type, exc_value, exc_traceback))

    def _poll_ui_queue(self) -> None:
        if not self.root:
            return
        # Auch bei gesetztem stop_event müssen noch eingereihte Callbacks
        # (insbesondere _shutdown) ausgeführt werden. Erst danach wird das
        # Polling beendet.
        try:
            while True:
                callback = self._ui_queue.get_nowait()
                try:
                    callback()
                except Exception:
                    logger.exception("UI-Callback fehlgeschlagen")
        except queue.Empty:
            pass
        if self.root and not self.stop_event.is_set():
            try:
                self.root.after(100, self._poll_ui_queue)
            except tk.TclError:
                pass

    def _progress_text(self) -> str:
        if self._progress is None:
            return ""
        remaining = max(0, 100 - self._progress.percent)
        if self._progress.current <= 0 and self._progress.total > 0:
            return f"Noch {remaining} % – Dauer wird geschätzt …"
        eta = self._format_eta(self._progress.eta_seconds)
        return f"Noch {remaining} % – ca. {eta}"

    @staticmethod
    def _format_eta(seconds: float) -> str:
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f"{seconds} s"
        minutes, secs = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes} min {secs} s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours} h {minutes} min"

    def _menu(self) -> Menu:
        items = [MenuItem(lambda item: self.status, None, enabled=False)]
        if self._syncing and self._progress is not None:
            items.append(
                MenuItem(lambda item: self._progress_text(), None, enabled=False)
            )
        elif not self._sync_queue.empty():
            items.append(
                MenuItem(
                    lambda item: f"{self._sync_queue.qsize()} Profil(e) ausstehend",
                    None,
                    enabled=False,
                )
            )
        profiles = self.settings.enabled_profiles
        folder_items = [
            MenuItem(
                profile.display_name,
                self._profile_folder_action(profile.id),
            )
            for profile in profiles
        ]
        folder_menu = (
            MenuItem("Lokalen Ordner öffnen", Menu(*folder_items))
            if len(folder_items) > 1
            else MenuItem(
                "Lokalen Ordner öffnen",
                lambda icon, item: self._action_open_folder(),
            )
        )
        items.extend(
            [
                Menu.SEPARATOR,
                MenuItem(
                    "Jetzt alle synchronisieren",
                    self._action_sync,
                    default=True,
                ),
                MenuItem(
                    lambda item: "Fortsetzen" if self.paused else "Pausieren",
                    self._action_pause,
                ),
                folder_menu,
                MenuItem("Einstellungen …", self._action_settings),
                MenuItem("Protokoll öffnen", self._action_open_log),
                Menu.SEPARATOR,
                MenuItem("Beenden", self._action_quit),
            ]
        )
        return Menu(*items)

    def _refresh_icon(self) -> None:
        if not self.icon:
            return
        try:
            has_token = TokenStore().load() is not None
        except Exception:
            has_token = False
        if self._last_error:
            color = COLORS["error"]
        elif self.paused:
            color = COLORS["paused"]
        elif self._syncing:
            color = COLORS["syncing"]
        elif not self.settings.is_configured or not has_token:
            color = COLORS["offline"]
        else:
            color = COLORS["idle"]
        try:
            self.icon.icon = make_icon(color)
            self.icon.title = f"tarklone – {self.status}"
            self.icon.update_menu()
        except Exception:
            logger.debug("Tray-Icon konnte nicht aktualisiert werden", exc_info=True)

    def _set_status(self, status: str, *, error: bool = False) -> None:
        self.status = status
        self._last_error = status if error else ""
        self._refresh_icon()

    def _action_sync(self, icon=None, item=None) -> None:
        for profile in self.settings.enabled_profiles:
            self._enqueue_profile(profile.id)
        if not self.settings.enabled_profiles:
            self._notify("Kein Workspace-Profil eingerichtet.")

    def _action_pause(self, icon=None, item=None) -> None:
        self.paused = not self.paused
        self._set_status("Pausiert" if self.paused else "Bereit")
        logger.info("Sync %s", "pausiert" if self.paused else "fortgesetzt")

    def _action_open_folder(self, icon=None, item=None) -> None:
        profiles = self.settings.enabled_profiles
        if not profiles:
            self._notify("Kein Workspace-Profil eingerichtet.")
            return
        self._open_profile_folder(profiles[0].id)

    def _open_profile_folder(self, profile_id: str) -> None:
        profile = self.settings.profile(profile_id)
        if profile is None:
            self._notify("Workspace-Profil nicht gefunden.")
            return
        folder = profile.local_folder
        if folder and Path(folder).is_dir():
            open_path(folder)
        else:
            self._notify("Kein gültiger lokaler Ordner konfiguriert.")

    def _profile_folder_action(self, profile_id: str):
        def action(icon=None, item=None) -> None:
            self._open_profile_folder(profile_id)

        return action

    def _action_settings(self, icon=None, item=None) -> None:
        if self.root:
            self._ui_queue.put(
                lambda: self.show_settings(first_run=False, restore=True)
            )

    def _action_open_log(self, icon=None, item=None) -> None:
        path = log_path()
        if path.exists():
            open_path(path)
        else:
            self._notify("Noch keine Logdatei vorhanden.")

    def _action_quit(self, icon=None, item=None) -> None:
        if self.root:
            self._ui_queue.put(self._shutdown)
        self.stop_event.set()

    def _notify(self, message: str, title: str = "tarklone") -> None:
        if self.icon:
            try:
                self.icon.notify(message, title)
            except Exception:
                logger.debug("Windows-Benachrichtigung fehlgeschlagen", exc_info=True)

    def show_settings(self, *, first_run: bool = False, restore: bool = False) -> None:
        if not self.root:
            return
        if self._settings_window and self._settings_window.winfo_exists():
            # Automatische Aufrufe (z. B. bei gesperrter Verschlüsselung)
            # dürfen ein minimiertes Fenster nicht ständig wieder öffnen.
            if restore:
                self._settings_window.deiconify()
                self._settings_window.lift()
                self._settings_window.focus_force()
            return
        self._settings_window = SetupDialog(
            self.root,
            self.settings,
            self._on_settings_saved,
            first_run=first_run,
            on_cancel=self._on_settings_cancelled,
            on_reset_encryption=self.reset_encryption,
        )

    def _on_settings_saved(self, settings) -> None:  # noqa: ANN001
        self.settings = settings
        self._settings_window = None
        self._last_error = ""
        self._load_last_sync()
        self._restart_watcher()
        self._lock_notified.clear()
        self._set_status("Bereit")
        for profile in self.settings.enabled_profiles:
            self._enqueue_profile(profile.id)
        logger.info("Einstellungen gespeichert")

    def _on_settings_cancelled(self) -> None:
        self._settings_window = None
        if not self.settings.is_configured:
            self._set_status("Einrichtung erforderlich", error=True)

    def _hide_windows(self) -> None:
        if self._settings_window and self._settings_window.winfo_exists():
            self._settings_window.grab_release()
            self._settings_window.destroy()
            self._settings_window = None

    def note_local_change(self, profile_id: str) -> None:
        self._local_due[profile_id] = time.time()

    def _restart_watcher(self) -> None:
        if self._watcher:
            try:
                self._watcher.stop()
                self._watcher.join(timeout=2)
            except Exception:
                logger.debug("Watcher konnte nicht gestoppt werden", exc_info=True)
            self._watcher = None
        try:
            from watchdog.observers import Observer

            observer = Observer()
            watched = 0
            for profile in self.settings.enabled_profiles:
                folder = profile.local_folder
                if not folder or not Path(folder).is_dir():
                    continue
                observer.schedule(
                    FileChangeHandler(self, profile.id),
                    folder,
                    recursive=True,
                )
                watched += 1
                logger.info(
                    "Überwache Ordner für %s: %s",
                    profile.display_name,
                    folder,
                )
            if watched:
                observer.start()
                self._watcher = observer
        except Exception:
            logger.exception("Ordnerüberwachung konnte nicht gestartet werden")

    def _scheduler(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(1.0)
            self._maybe_check_updates()
            if self.paused:
                continue
            if not self.settings.is_configured or TokenStore().load() is None:
                continue
            now = time.time()
            for profile in self.settings.enabled_profiles:
                interval = max(15, int(profile.sync_interval_seconds))
                last = self._last_sync.get(profile.id, 0.0)
                due = (now - last) >= interval
                local_ts = self._local_due.get(profile.id, 0.0)
                local_due = bool(local_ts) and (now - local_ts) >= 4.0
                if due or local_due:
                    self._local_due.pop(profile.id, None)
                    self._enqueue_profile(profile.id)

    def _maybe_check_updates(self) -> None:
        if self._update_check_running:
            return
        days = int(self.settings.update_check_interval_days or 0)
        if days <= 0:
            return
        last = 0.0
        if self.settings.last_update_check:
            try:
                last = datetime.fromisoformat(self.settings.last_update_check).timestamp()
            except ValueError:
                last = 0.0
        if last and (time.time() - last) < days * 86400:
            return
        self._update_check_running = True
        self.settings.last_update_check = datetime.now().isoformat(timespec="seconds")
        try:
            save_settings(self.settings)
        except OSError:
            pass

        def worker() -> None:
            try:
                info = check_for_update()
                if info is not None:
                    path = download_update(info)
                    logger.info("Update %s heruntergeladen: %s", info.version, path)
                    self._notify(
                        f"Update {info.version} heruntergeladen:\n{path}",
                        "tarklone – Update",
                    )
                    self._set_status(f"Update {info.version} heruntergeladen")
            except Exception as exc:  # noqa: BLE001 - Hintergrundprüfung
                logger.warning("Updateprüfung fehlgeschlagen: %s", exc)
            finally:
                self._update_check_running = False

        threading.Thread(
            target=worker,
            name="tarklone-update-check",
            daemon=True,
        ).start()

    def reset_encryption(self, profile: SyncProfile) -> tuple[bool, str]:
        """Löscht Schlüssel und verschlüsselte Remote-Dateien eines Profils.

        Lokale Klartextdateien unter encrypted/ bleiben erhalten. Die
        verschlüsselten Remote-Dateien werden in den Tarkle-Papierkorb
        verschoben; danach kann ein neues Passwort gesetzt werden.
        """
        if profile is None:
            return False, "Kein Workspace-Profil ausgewählt."
        if not profile.local_folder or not profile.workspace_id:
            return False, "Das Workspace-Profil ist unvollständig."
        if not self._sync_lock.acquire(timeout=30):
            return False, "Es läuft gerade ein Sync. Bitte gleich erneut versuchen."
        state = None
        try:
            from .tarkle import TarkleRemote

            profile_settings = replace(
                self.settings,
                local_folder=profile.local_folder,
                workspace_id=profile.workspace_id,
                workspace_name=profile.workspace_name,
                remote_folder_id=profile.remote_folder_id,
                sync_deletions=profile.sync_deletions,
                sync_interval_seconds=profile.sync_interval_seconds,
            )
            oauth = OAuthClient(self.settings)
            try:
                token = oauth.ensure_valid()
            finally:
                oauth.close()
            remote = TarkleRemote(profile_settings, token)
            try:
                entries = [
                    entry
                    for entry in remote.list_tree()
                    if entry.path == "encrypted"
                    or entry.path.startswith("encrypted/")
                ]
                for entry in sorted(
                    entries,
                    key=lambda item: (
                        item.kind != "file",
                        -item.path.count("/"),
                    ),
                ):
                    remote.delete(entry)
            finally:
                remote.close()
            state = self._state_for(profile)
            state.delete_prefix("encrypted")
            manager = EncryptionManager(
                Path(profile.local_folder),
                keyring_username=crypto_keyring_username(profile.id, profile.local_folder),
            )
            manager.reset_key()
            logger.warning(
                "Verschlüsselungsschlüssel und Remote-Daten für %s zurückgesetzt",
                profile.display_name,
            )
            self._set_status("Verschlüsselung zurückgesetzt")
            return (
                True,
                (
                    "Schlüssel gelöscht. Die verschlüsselten Dateien in Tarkle "
                    "wurden entfernt. Du kannst jetzt ein neues Passwort setzen; "
                    "die lokalen Klartextdateien bleiben erhalten."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - im UI anzeigen
            logger.exception("Verschlüsselung zurücksetzen fehlgeschlagen")
            return False, str(exc)
        finally:
            if state is not None:
                try:
                    state.close()
                except Exception:
                    pass
            self._sync_lock.release()

    def _sync_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                profile_id = self._sync_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.stop_event.is_set():
                with self._queue_lock:
                    self._queued_ids.discard(profile_id)
                break
            if self.paused:
                with self._queue_lock:
                    self._queued_ids.discard(profile_id)
                continue
            profile = self.settings.profile(profile_id)
            if profile is None or not profile.enabled:
                with self._queue_lock:
                    self._queued_ids.discard(profile_id)
                continue
            if not profile.local_folder or not profile.workspace_id:
                with self._queue_lock:
                    self._queued_ids.discard(profile_id)
                continue
            try:
                self._run_sync(profile)
            finally:
                with self._queue_lock:
                    self._queued_ids.discard(profile_id)

    def _run_sync(self, profile: SyncProfile) -> None:
        if not self._sync_lock.acquire(blocking=False):
            return
        self._syncing = True
        self._current_profile_id = profile.id
        self._progress = None
        self._last_error = ""
        self.status = f"Synchronisiere {profile.display_name} …"
        self._refresh_icon()
        logger.info("Sync gestartet: %s", profile.display_name)
        adapter = None
        state = None
        try:
            from .tarkle import TarkleRemote

            profile_settings = replace(
                self.settings,
                local_folder=profile.local_folder,
                workspace_id=profile.workspace_id,
                workspace_name=profile.workspace_name,
                remote_folder_id=profile.remote_folder_id,
                sync_deletions=profile.sync_deletions,
                sync_interval_seconds=profile.sync_interval_seconds,
            )
            oauth = OAuthClient(self.settings)
            try:
                token = oauth.ensure_valid()
            finally:
                oauth.close()

            local_root = Path(profile.local_folder)
            encryption = EncryptionManager(
                local_root,
                keyring_username=crypto_keyring_username(profile.id, profile.local_folder),
            )
            encryption.load_cached_key()
            if encryption.enabled and not encryption.unlocked:
                self._last_sync[profile.id] = time.time()
                self._set_status("Verschlüsselung gesperrt", error=True)
                logger.warning(
                    "Der encrypted-Ordner von %s ist gesperrt; bitte das Passwort eingeben.",
                    profile.display_name,
                )
                if profile.id not in self._lock_notified:
                    self._lock_notified.add(profile.id)
                    self._notify(
                        f"Bitte das Passwort für „{profile.display_name}“ in den "
                        "Einstellungen eingeben.",
                        "tarklone – Verschlüsselung",
                    )
                    if self.root:
                        self._ui_queue.put(
                            lambda: self.show_settings(first_run=False)
                        )
                return
            if not encryption.enabled:
                encrypted_dir = local_root / "encrypted"
                if encrypted_dir.is_dir() and any(
                    path.is_file() and path.name != KEYFILE_NAME
                    for path in encrypted_dir.rglob("*")
                ):
                    self._last_sync[profile.id] = time.time()
                    self._set_status("Verschlüsselung nicht eingerichtet", error=True)
                    if profile.id not in self._lock_notified:
                        self._lock_notified.add(profile.id)
                        self._notify(
                            f"Der Ordner encrypted von „{profile.display_name}“ enthält "
                            "Dateien, aber es ist kein Passwort gesetzt. Bitte zuerst "
                            "ein Passwort festlegen.",
                            "tarklone – Verschlüsselung",
                        )
                        if self.root:
                            self._ui_queue.put(
                                lambda: self.show_settings(first_run=False)
                            )
                    return

            remote = TarkleRemote(profile_settings, token)
            adapter = EncryptionAdapter(remote, encryption)
            state = self._state_for(profile)
            engine = SyncEngine(
                local_root,
                adapter,
                state,
                sync_deletions=profile.sync_deletions,
                conflict_policy=self.settings.conflict_policy,
                exclude=self.settings.exclude,
                logger=logger,
                progress=self._on_progress,
                on_progress=lambda progress: self._on_sync_progress(
                    profile, progress
                ),
            )
            result = engine.sync()
            self._last_sync[profile.id] = time.time()
            self._lock_notified.discard(profile.id)
            if not encryption.enabled and (
                local_root / "encrypted" / KEYFILE_NAME
            ).is_file():
                self._notify(
                    f"Verschlüsselter Ordner in „{profile.display_name}“ erkannt. "
                    "Bitte das Passwort in den Einstellungen eingeben.",
                    "tarklone – Verschlüsselung",
                )
                if self.root:
                    self._ui_queue.put(lambda: self.show_settings(first_run=False))
            if result.errors:
                self._set_status(f"Sync mit {len(result.errors)} Fehler(n)", error=True)
                logger.warning("Sync-Fehler: %s", "; ".join(result.errors[:5]))
                self._notify(
                    f"{len(result.errors)} Fehler beim Sync von "
                    f"„{profile.display_name}“. Details im Protokoll.",
                    "tarklone – Sync-Fehler",
                )
            elif result.changed:
                self._set_status(
                    f"{profile.display_name}: {result.changed} Änderung(en)"
                )
                logger.info(
                    "Sync fertig (%s): %s hoch, %s runter, %s Konflikte, %s Fehler",
                    profile.display_name,
                    result.uploaded,
                    result.downloaded,
                    result.conflicts,
                    len(result.errors),
                )
            else:
                self._set_status(f"{profile.display_name}: Alles aktuell")
                logger.info(
                    "Sync fertig (%s): keine Änderungen", profile.display_name
                )
        except (NotLoggedIn, WebAuthError) as exc:
            self._set_status("Anmeldung erforderlich", error=True)
            logger.warning("%s", exc)
            if self.root:
                self._ui_queue.put(lambda: self.show_settings(first_run=True))
        except AuthError as exc:
            self._set_status("Anmeldung fehlgeschlagen", error=True)
            logger.error("%s", exc)
            self._notify(str(exc), "tarklone – Anmeldung")
        except RemoteUnsupported as exc:
            self._set_status("Tarkle-Adapter unvollständig", error=True)
            logger.error("%s", exc)
            self._notify(str(exc), "tarklone – Sync")
        except SyncBusy:
            logger.info("Sync läuft bereits")
        except SyncError as exc:
            self._set_status("Sync-Fehler", error=True)
            logger.error("%s", exc)
            self._notify(str(exc), "tarklone – Sync-Fehler")
        except Exception as exc:  # noqa: BLE001 - Tray darf nicht abstürzen
            self._set_status("Unerwarteter Fehler", error=True)
            logger.exception("Unerwarteter Sync-Fehler")
            self._notify(str(exc), "tarklone – Fehler")
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass
            if state is not None:
                try:
                    state.close()
                except Exception:
                    pass
            self._syncing = False
            self._progress = None
            self._current_profile_id = ""
            self._refresh_icon()
            self._sync_lock.release()

    def _on_progress(self, message: str) -> None:
        logger.debug("%s", message)

    def _on_sync_progress(
        self, profile: SyncProfile, progress: SyncProgress
    ) -> None:
        self._progress = progress
        self.status = (
            f"Synchronisiere {profile.display_name} – {progress.percent} %"
        )
        now = time.time()
        if (
            now - self._last_progress_refresh >= 0.5
            or progress.current >= progress.total
        ):
            self._last_progress_refresh = now
            self._refresh_icon()

    def _shutdown(self) -> None:
        if self.stop_event.is_set() and self.root is None:
            return
        self.stop_event.set()
        logger.info("tarklone beendet")

        # Falls ein nicht-daemonisierter Tray-/Watcher-Thread das Beenden
        # blockiert, wird der Prozess nach kurzer Frist sauber erzwungen.
        def force_exit() -> None:
            time.sleep(3.0)
            logger.warning("Erzwungenes Beenden nach Shutdown-Timeout")
            logging.shutdown()
            os._exit(0)

        threading.Thread(
            target=force_exit,
            name="tarklone-force-exit",
            daemon=True,
        ).start()

        if self._watcher:
            try:
                self._watcher.stop()
                self._watcher.join(timeout=2)
            except Exception:
                logger.debug("Watcher konnte nicht sauber beendet werden", exc_info=True)
            self._watcher = None
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                logger.debug("Tray-Icon konnte nicht sauber beendet werden", exc_info=True)
            self.icon = None
        if self.root:
            try:
                self.root.quit()
                self.root.destroy()
            except Exception:
                logger.debug("Tk-Fenster konnte nicht sauber beendet werden", exc_info=True)
            self.root = None


def main() -> int:
    try:
        return TarkloneApp().run()
    except Exception:
        setup_logging()
        logger.exception("tarklone konnte nicht gestartet werden")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
