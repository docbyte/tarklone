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
from .api import TarkleApi, TarkleApiError
from .auth import ApiKeyMissing, ApiKeyStore
from .config import (
    SyncFolder,
    SyncProfile,
    crypto_keyring_username,
    ensure_dirs,
    is_workspace_key,
    load_settings,
    log_path,
    profile_state_path,
    save_settings,
    state_path,
)
from .crypto import KEYFILE_NAME, EncryptionManager
from .encrypted_remote import EncryptedStateAdapter, EncryptionAdapter
from .state import SyncState
from .sync import (
    RemoteUnsupported,
    SyncBusy,
    SyncEngine,
    SyncError,
    SyncProgress,
    SyncResult,
)
from .ui import SetupDialog
from .updates import check_for_update, download_update

logger = logging.getLogger("tarklone")

LOGO_FILES = {
    "green": "Tarklone_Logo_green.png",
    "blue": "Tarklone_Logo_blue.png",
    "red": "Tarklone_Logo_red.png",
}
COLORS = {
    "green": "#1E9E4A",
    "blue": "#2587CE",
    "red": "#D6382F",
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


def assets_dir() -> Path:
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return bundle / "assets"
    return Path(__file__).resolve().parents[2] / "assets"


def logo_path(variant: str) -> Path:
    name = LOGO_FILES.get(variant, LOGO_FILES["green"])
    return assets_dir() / name


def aggregate_icon_variant(
    *,
    has_api_key: bool,
    paused: bool,
    configured: bool,
    tones: list[str] | tuple[str, ...],
) -> str:
    """Maps workspace states to the green/blue/red logo set."""
    if not has_api_key or paused or not configured:
        return "red"
    if "red" in tones:
        return "red"
    if "blue" in tones:
        return "blue"
    return "green"


def _fallback_icon(variant: str = "green") -> Image.Image:
    color = COLORS.get(variant, COLORS["green"])
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


def make_icon(variant: str = "green") -> Image.Image:
    """Loads one of the supplied status logos for the tray and window icons."""
    try:
        with Image.open(logo_path(variant)) as source:
            image = source.convert("RGBA")
            return image.resize((64, 64), Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        return _fallback_icon(variant)


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
        self._profile_text: dict[str, str] = {}
        self._profile_tone: dict[str, str] = {}
        self._reset_profile_statuses()
        self._icon_variant = ""
        self._window_icon_variant = ""
        self._logo_cache: dict[str, Image.Image] = {}
        self._load_last_sync()

    def _state_for(
        self, profile: SyncProfile, folder: SyncFolder | None = None
    ) -> SyncState:
        if folder is None:
            folders = profile.active_folders()
            if not folders:
                raise ValueError("Profil hat keinen lokalen Ordner.")
            folder = folders[0]
        state_id = (
            profile.id
            if profile.sync_mode != "multi" or folder.id == profile.id
            else f"{profile.id}-{folder.id}"
        )
        path = profile_state_path(state_id)
        existing_states = (
            list(path.parent.glob("*.sqlite3")) if path.parent.exists() else []
        )
        if (
            not path.exists()
            and not existing_states
            and state_id == profile.id
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
            folders = profile.active_folders()
            if not folders:
                continue
            values: list[float] = []
            complete = True
            for folder in folders:
                state = None
                try:
                    state = self._state_for(profile, folder)
                    raw = state.get_meta("last_sync")
                    if raw:
                        values.append(datetime.fromisoformat(raw).timestamp())
                    else:
                        complete = False
                except Exception:
                    complete = False
                finally:
                    if state is not None:
                        state.close()
            if complete and values:
                self._last_sync[profile.id] = min(values)

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

        initial_variant = self._icon_variant_for(self._has_api_key())
        self.icon = pystray.Icon(
            "tarklone", self._get_logo(initial_variant), "tarklone", self._menu()
        )
        self.icon.run_detached()

        threading.Thread(target=self._sync_worker, name="tarklone-sync", daemon=True).start()
        threading.Thread(target=self._scheduler, name="tarklone-scheduler", daemon=True).start()
        self._restart_watcher()
        self._refresh_icon()

        if not self.settings.is_configured or ApiKeyStore().load() is None:
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
            return f"noch {remaining} %, Dauer wird geschätzt …"
        eta = self._format_eta(self._progress.eta_seconds)
        return f"noch {remaining} %, ca. {eta}"

    def _reset_profile_statuses(self) -> None:
        self._profile_text = {
            profile.id: "Bereit" for profile in self.settings.enabled_profiles
        }
        self._profile_tone = {
            profile.id: "green" for profile in self.settings.enabled_profiles
        }

    def _set_profile_status(
        self,
        profile_id: str,
        text: str,
        tone: str,
        *,
        refresh: bool = True,
    ) -> None:
        self._profile_text[profile_id] = text
        self._profile_tone[profile_id] = tone
        self.status = text
        if refresh:
            self._refresh_icon()

    def _set_all_profile_status(self, text: str, tone: str) -> None:
        for profile in self.settings.enabled_profiles:
            self._profile_text[profile.id] = text
            self._profile_tone[profile.id] = tone
        self.status = text
        self._refresh_icon()

    def _has_api_key(self) -> bool:
        try:
            return ApiKeyStore().load() is not None
        except Exception:
            return False

    def _is_queued(self, profile_id: str) -> bool:
        with self._queue_lock:
            return profile_id in self._queued_ids

    def _profile_status_text(self, profile: SyncProfile, has_api_key: bool) -> str:
        if not has_api_key:
            return "Kein API-Key"
        if self.paused:
            return "Pausiert"
        if self._is_queued(profile.id) and profile.id != self._current_profile_id:
            return "Wartet auf Sync"
        return self._profile_text.get(profile.id, "Bereit")

    def _profile_status_tone(self, profile: SyncProfile, has_api_key: bool) -> str:
        if not has_api_key or self.paused:
            return "red"
        if self._is_queued(profile.id) and profile.id != self._current_profile_id:
            return "blue"
        return self._profile_tone.get(profile.id, "green")

    def _icon_variant_for(self, has_api_key: bool) -> str:
        tones = [
            self._profile_status_tone(profile, has_api_key)
            for profile in self.settings.enabled_profiles
        ]
        return aggregate_icon_variant(
            has_api_key=has_api_key,
            paused=self.paused,
            configured=self.settings.is_configured,
            tones=tones,
        )

    def _status_summary(self, has_api_key: bool, variant: str) -> str:
        if not has_api_key:
            return "Kein API-Key"
        if self.paused:
            return "Pausiert"
        if not self.settings.is_configured:
            return "Einrichtung erforderlich"
        if variant == "red":
            return "Fehler"
        if variant == "blue":
            return "Synchronisiert"
        return "Alles aktuell"

    def _status_menu_item(self, profile: SyncProfile, has_api_key: bool) -> MenuItem:
        def status_text(item=None) -> str:  # noqa: ANN001 - pystray callback
            return (
                f"{profile.display_name}: "
                f"{self._profile_status_text(profile, has_api_key)}"
            )

        return MenuItem(status_text, None, enabled=False)

    def _get_logo(self, variant: str) -> Image.Image:
        image = self._logo_cache.get(variant)
        if image is None:
            image = make_icon(variant)
            self._logo_cache[variant] = image
        return image.copy()

    def _set_window_icon(self, variant: str) -> None:
        path = logo_path(variant)
        for window in (self.root, self._settings_window):
            if window is None:
                continue
            try:
                if not window.winfo_exists():
                    continue
                photo = tk.PhotoImage(file=str(path))
                window.iconphoto(True, photo)
                window._tarklone_icon = photo  # type: ignore[attr-defined]
            except (OSError, tk.TclError):
                logger.debug("Fensterlogo konnte nicht gesetzt werden", exc_info=True)

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
        has_api_key = self._has_api_key()
        profiles = self.settings.enabled_profiles
        if profiles:
            items = [
                self._status_menu_item(profile, has_api_key) for profile in profiles
            ]
        else:
            items = [MenuItem("Keine Workspaces eingerichtet", None, enabled=False)]
        folder_items = []
        for profile in profiles:
            folders = profile.active_folders()
            if len(folders) <= 1:
                folder_items.append(
                    MenuItem(
                        profile.display_name,
                        self._profile_folder_action(profile.id),
                    )
                )
                continue
            for folder in folders:
                folder_items.append(
                    MenuItem(
                        f"{profile.display_name}: {folder.display_name}",
                        self._folder_action(profile.id, folder.id),
                    )
                )
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
        try:
            has_api_key = self._has_api_key()
        except Exception:
            has_api_key = False
        variant = self._icon_variant_for(has_api_key)
        if self.icon:
            try:
                if variant != self._icon_variant:
                    self.icon.icon = self._get_logo(variant)
                self._icon_variant = variant
                self.icon.title = f"tarklone – {self._status_summary(has_api_key, variant)}"
                self.icon.update_menu()
            except Exception:
                logger.debug("Tray-Icon konnte nicht aktualisiert werden", exc_info=True)
        if variant != self._window_icon_variant:
            self._window_icon_variant = variant
            if self.root:
                self._ui_queue.put(lambda v=variant: self._set_window_icon(v))

    def _set_status(self, status: str, *, error: bool = False) -> None:
        self.status = status
        self._last_error = status if error else ""
        self._refresh_icon()

    def _action_sync(self, icon=None, item=None) -> None:
        if ApiKeyStore().load() is None:
            self._notify(
                "Kein Tarkle-API-Key gespeichert. Bitte in den Einstellungen "
                "einen Key eintragen."
            )
            return
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
        folders = profile.active_folders()
        folder = folders[0] if folders else None
        target = folder.local_folder if folder else ""
        if target and Path(target).is_dir():
            open_path(target)
        else:
            self._notify("Kein gültiger lokaler Ordner konfiguriert.")

    def _profile_folder_action(self, profile_id: str):
        def action(icon=None, item=None) -> None:
            self._open_profile_folder(profile_id)

        return action

    def _open_folder(self, profile_id: str, folder_id: str) -> None:
        profile = self.settings.profile(profile_id)
        if profile is None:
            self._notify("Workspace-Profil nicht gefunden.")
            return
        folder = next(
            (item for item in profile.active_folders() if item.id == folder_id),
            None,
        )
        if folder is None:
            self._notify("Lokaler Ordner nicht gefunden.")
            return
        if Path(folder.local_folder).is_dir():
            open_path(folder.local_folder)
        else:
            self._notify("Kein gültiger lokaler Ordner konfiguriert.")

    def _folder_action(self, profile_id: str, folder_id: str):
        def action(icon=None, item=None) -> None:
            self._open_folder(profile_id, folder_id)

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
        self._set_window_icon(
            self._icon_variant or self._icon_variant_for(self._has_api_key())
        )

    def _on_settings_saved(self, settings) -> None:  # noqa: ANN001
        self.settings = settings
        self._settings_window = None
        self._last_error = ""
        self._reset_profile_statuses()
        self._load_last_sync()
        self._restart_watcher()
        self._lock_notified.clear()
        self._set_status("Bereit")
        if ApiKeyStore().load() is not None:
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
                for folder in profile.active_folders():
                    path = folder.local_folder
                    if not path or not Path(path).is_dir():
                        continue
                    observer.schedule(
                        FileChangeHandler(self, profile.id),
                        path,
                        recursive=True,
                    )
                    watched += 1
                    logger.info(
                        "Überwache Ordner für %s/%s: %s",
                        profile.display_name,
                        folder.display_name,
                        path,
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
            if not self.settings.is_configured or ApiKeyStore().load() is None:
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

    def reset_encryption(
        self, profile: SyncProfile, folder: SyncFolder | None = None
    ) -> tuple[bool, str]:
        """Löscht Schlüssel und verschlüsselte Remote-Dateien eines Ordners.

        Lokale Klartextdateien unter encrypted/ bleiben erhalten. Die
        verschlüsselten Remote-Dateien werden in den Tarkle-Papierkorb
        verschoben; danach kann ein neues Passwort gesetzt werden.
        """
        if profile is None:
            return False, "Kein Workspace-Profil ausgewählt."
        if folder is None:
            folders = profile.active_folders()
            if not folders:
                return False, "Das Workspace-Profil hat keinen lokalen Ordner."
            folder = folders[0]
        if not folder.local_folder or not profile.workspace_id:
            return False, "Das Workspace-Profil ist unvollständig."
        if not self._sync_lock.acquire(timeout=30):
            return False, "Es läuft gerade ein Sync. Bitte gleich erneut versuchen."
        state = None
        try:
            from .tarkle import TarkleRemote

            manager = EncryptionManager(
                Path(folder.local_folder),
                keyring_username=crypto_keyring_username(
                    profile.id, folder.local_folder
                ),
            )
            api_key = ApiKeyStore().load()
            if not api_key:
                return False, "Kein Tarkle-API-Key gespeichert."
            if not manager.clear_cached_key():
                return False, (
                    "Der Schlüssel konnte nicht aus dem System-Keyring entfernt "
                    "werden. Es wurden keine Remote-Dateien gelöscht."
                )
            profile_settings = replace(
                self.settings,
                local_folder=folder.local_folder,
                workspace_id=profile.workspace_id,
                workspace_name=profile.workspace_name,
                remote_folder_id=folder.remote_folder_id,
                sync_deletions=profile.sync_deletions,
                sync_interval_seconds=profile.sync_interval_seconds,
            )
            remote = TarkleRemote(profile_settings, api_key)
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
            state = self._state_for(profile, folder)
            state.delete_prefix("encrypted")
            manager.reset_key(cache_already_cleared=True)
            logger.warning(
                "Verschlüsselungsschlüssel und Remote-Daten für %s/%s zurückgesetzt",
                profile.display_name,
                folder.display_name,
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
            if not profile.active_folders() or not profile.workspace_id:
                with self._queue_lock:
                    self._queued_ids.discard(profile_id)
                continue
            try:
                self._run_sync(profile)
            finally:
                with self._queue_lock:
                    self._queued_ids.discard(profile_id)
                # The menu may have been drawn while the profile was still
                # marked as queued. Refresh once more after removing it.
                self._refresh_icon()

    def _migrate_workspace_keys(self) -> bool:
        """Migriert alte Workspace-IDs anhand des Namens auf API-Workspace-Keys."""
        if not any(
            profile.workspace_name
            and not is_workspace_key(profile.workspace_id)
            for profile in self.settings.profiles
        ):
            return False
        api_key = ApiKeyStore().load()
        if not api_key:
            return False
        api = TarkleApi(api_key)
        try:
            workspaces = api.list_workspaces()
        except TarkleApiError as exc:
            logger.warning("Workspace-Migration fehlgeschlagen: %s", exc)
            return False
        finally:
            api.close()
        by_name = {
            str(item.get("name") or "").strip().casefold(): str(
                item.get("workspace_key") or ""
            )
            for item in workspaces
            if item.get("name") and item.get("workspace_key")
        }
        changed = False
        for profile in self.settings.profiles:
            if is_workspace_key(profile.workspace_id):
                continue
            match = by_name.get(profile.workspace_name.strip().casefold())
            if match:
                profile.workspace_id = match
                changed = True
        if changed:
            save_settings(self.settings)
            logger.info(
                "Workspace-Profile auf API-Workspace-Keys migriert: %d",
                len(self.settings.profiles),
            )
        return changed

    def _run_sync(self, profile: SyncProfile) -> None:
        if not self._sync_lock.acquire(blocking=False):
            return
        folders = profile.active_folders()
        if not folders:
            self._set_profile_status(profile.id, "Kein lokaler Ordner", "red")
            self._sync_lock.release()
            return
        self._migrate_workspace_keys()
        self._syncing = True
        self._current_profile_id = profile.id
        self._progress = None
        self._last_error = ""
        start_text = (
            f"Synchronisiere {len(folders)} Ordner …"
            if len(folders) > 1
            else f"Synchronisiere {folders[0].display_name} …"
        )
        self._set_profile_status(profile.id, start_text, "blue")
        logger.info(
            "Sync gestartet: %s (%d Ordner)", profile.display_name, len(folders)
        )
        combined = SyncResult()
        errors: list[str] = []
        locked_count = 0
        setup_errors = 0
        try:
            api_key = ApiKeyStore().load()
            if not api_key:
                raise ApiKeyMissing(
                    "Kein Tarkle-API-Key gespeichert. Bitte in den Einstellungen "
                    "einen Key eintragen."
                )

            for index, folder in enumerate(folders, start=1):
                folder_prefix = (
                    f"{folder.display_name} ({index}/{len(folders)})"
                    if len(folders) > 1
                    else folder.display_name
                )
                self._set_profile_status(
                    profile.id, f"Synchronisiere {folder_prefix} …", "blue"
                )
                try:
                    result, is_locked, setup_error = self._sync_folder(
                        profile, folder, api_key, index, len(folders)
                    )
                except (ApiKeyMissing, TarkleApiError):
                    raise
                except Exception as exc:  # noqa: BLE001 - pro Ordner weiterarbeiten
                    logger.exception(
                        "Sync-Ordner %s von %s fehlgeschlagen",
                        folder.display_name,
                        profile.display_name,
                    )
                    errors.append(f"{folder.display_name}: {exc}")
                    continue
                if is_locked:
                    locked_count += 1
                    continue
                if setup_error:
                    setup_errors += 1
                    errors.append(f"{folder.display_name}: {setup_error}")
                    continue
                if result is None:
                    continue
                combined.uploaded += result.uploaded
                combined.downloaded += result.downloaded
                combined.deleted_local += result.deleted_local
                combined.deleted_remote += result.deleted_remote
                combined.created_folders += result.created_folders
                combined.conflicts += result.conflicts
                combined.skipped += result.skipped
                errors.extend(
                    f"{folder.display_name}: {error}" for error in result.errors
                )

            self._last_sync[profile.id] = time.time()
            if locked_count == 0:
                self._lock_notified.discard(profile.id)
            total_errors = len(errors)
            if total_errors:
                suffix = (
                    f", {locked_count} Ordner gesperrt" if locked_count else ""
                )
                self._set_profile_status(
                    profile.id, f"{total_errors} Fehler{suffix}", "red"
                )
                logger.warning(
                    "Sync-Fehler (%s): %s",
                    profile.display_name,
                    "; ".join(errors[:5]),
                )
                self._notify(
                    f"{total_errors} Fehler beim Sync von "
                    f"„{profile.display_name}“. Details im Protokoll.",
                    "tarklone – Sync-Fehler",
                )
            elif combined.changed:
                suffix = (
                    f"; {locked_count} Ordner gesperrt" if locked_count else ""
                )
                self._set_profile_status(
                    profile.id,
                    f"{combined.changed} Änderung(en) synchronisiert{suffix}",
                    "blue",
                )
                logger.info(
                    "Sync fertig (%s): %s hoch, %s runter, %s Konflikte, "
                    "%s Fehler, %s gesperrt",
                    profile.display_name,
                    combined.uploaded,
                    combined.downloaded,
                    combined.conflicts,
                    total_errors,
                    locked_count,
                )
            elif locked_count:
                self._set_profile_status(
                    profile.id,
                    f"Verschlüsselung gesperrt "
                    f"({locked_count}/{len(folders)} Ordner)",
                    "neutral",
                )
                logger.warning(
                    "Verschlüsselung für %d Ordner von %s gesperrt",
                    locked_count,
                    profile.display_name,
                )
            else:
                self._set_profile_status(profile.id, "Alles aktuell", "green")
                logger.info(
                    "Sync fertig (%s): keine Änderungen", profile.display_name
                )

            if locked_count or setup_errors:
                if profile.id not in self._lock_notified:
                    self._lock_notified.add(profile.id)
                    if locked_count:
                        self._notify(
                            f"Bitte das Passwort für „{profile.display_name}“ in den "
                            "Einstellungen eingeben.",
                            "tarklone – Verschlüsselung",
                        )
                    if self.root:
                        self._ui_queue.put(
                            lambda: self.show_settings(first_run=False)
                        )
        except ApiKeyMissing as exc:
            self._set_all_profile_status("API-Key erforderlich", "red")
            logger.warning("%s", exc)
            if self.root:
                self._ui_queue.put(lambda: self.show_settings(first_run=True))
        except TarkleApiError as exc:
            if exc.status == 401:
                self._set_all_profile_status("API-Key ungültig", "red")
                if self.root:
                    self._ui_queue.put(lambda: self.show_settings(first_run=False))
            else:
                self._set_all_profile_status("Tarkle-Fehler", "red")
            logger.error("%s", exc)
            self._notify(str(exc), "tarklone – Tarkle-API")
        except RemoteUnsupported as exc:
            self._set_profile_status(
                profile.id, "Tarkle-Adapter unvollständig", "red"
            )
            logger.error("%s", exc)
            self._notify(str(exc), "tarklone – Sync")
        except SyncBusy:
            logger.info("Sync läuft bereits")
        except SyncError as exc:
            self._set_profile_status(profile.id, "Sync-Fehler", "red")
            logger.error("%s", exc)
            self._notify(str(exc), "tarklone – Sync-Fehler")
        except Exception as exc:  # noqa: BLE001 - Tray darf nicht abstürzen
            self._set_profile_status(profile.id, "Unerwarteter Fehler", "red")
            logger.exception("Unerwarteter Sync-Fehler")
            self._notify(str(exc), "tarklone – Fehler")
        finally:
            self._syncing = False
            self._progress = None
            self._current_profile_id = ""
            self._refresh_icon()
            self._sync_lock.release()

    def _sync_folder(
        self,
        profile: SyncProfile,
        folder: SyncFolder,
        api_key: str,
        index: int,
        count: int,
    ) -> tuple[SyncResult | None, bool, str]:
        from .tarkle import TarkleRemote

        local_root = Path(folder.local_folder)
        encryption = EncryptionManager(
            local_root,
            keyring_username=crypto_keyring_username(
                profile.id, folder.local_folder
            ),
        )
        encryption.load_cached_key()
        if encryption.enabled and not encryption.unlocked:
            return None, True, ""
        if not encryption.enabled:
            encrypted_dir = local_root / "encrypted"
            if encrypted_dir.is_dir() and any(
                path.is_file() and path.name != KEYFILE_NAME
                for path in encrypted_dir.rglob("*")
            ):
                return None, False, "Verschlüsselung nicht eingerichtet"

        profile_settings = replace(
            self.settings,
            local_folder=folder.local_folder,
            workspace_id=profile.workspace_id,
            workspace_name=profile.workspace_name,
            remote_folder_id=folder.remote_folder_id,
            sync_deletions=profile.sync_deletions,
            sync_interval_seconds=profile.sync_interval_seconds,
        )
        remote = None
        state = None
        try:
            remote = TarkleRemote(profile_settings, api_key)
            adapter = EncryptionAdapter(remote, encryption)
            state = self._state_for(profile, folder)
            sync_state = (
                EncryptedStateAdapter(state, encryption)
                if encryption.enabled
                else state
            )
            engine = SyncEngine(
                local_root,
                adapter,
                sync_state,
                sync_deletions=profile.sync_deletions,
                conflict_policy=self.settings.conflict_policy,
                exclude=self.settings.exclude,
                logger=logger,
                progress=self._on_progress,
                on_progress=lambda progress: self._on_sync_progress(
                    profile, progress, folder, index, count
                ),
            )
            return engine.sync(), False, ""
        finally:
            if remote is not None:
                try:
                    remote.close()
                except Exception:
                    pass
            if state is not None:
                try:
                    state.close()
                except Exception:
                    pass

    def _on_progress(self, message: str) -> None:
        logger.debug("%s", message)

    def _on_sync_progress(
        self,
        profile: SyncProfile,
        progress: SyncProgress,
        folder: SyncFolder | None = None,
        index: int = 1,
        count: int = 1,
    ) -> None:
        self._progress = progress
        prefix = "Synchronisiere"
        if folder is not None:
            prefix = f"Synchronisiere {folder.display_name}"
            if count > 1:
                prefix += f" ({index}/{count})"
        self._profile_text[profile.id] = f"{prefix} – {self._progress_text()}"
        self._profile_tone[profile.id] = "blue"
        self.status = self._profile_text[profile.id]
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
