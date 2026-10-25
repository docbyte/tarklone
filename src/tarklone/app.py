from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont
from pystray import Menu, MenuItem
from watchdog.events import FileSystemEventHandler

from . import __version__
from .auth import AuthError, NotLoggedIn, OAuthClient, TokenStore
from .config import ensure_dirs, load_settings, log_path, state_path
from .crypto import KEYFILE_NAME, EncryptionManager
from .encrypted_remote import EncryptionAdapter
from .state import SyncState
from .sync import RemoteUnsupported, SyncBusy, SyncEngine, SyncError
from .ui import SetupDialog
from .webapi import WebAuthError

logger = logging.getLogger("tarklone")

COLORS = {
    "idle": "#714CB6",
    "syncing": "#2F80ED",
    "paused": "#E08A2E",
    "error": "#D64545",
    "offline": "#8C8C96",
}


def make_icon(color: str = COLORS["idle"]) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=16, fill=color)
    font = None
    for candidate in ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"):
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
    def __init__(self, app: "TarkloneApp"):
        self.app = app

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
            self.app.note_local_change()
        except Exception:
            pass


class TarkloneApp:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.state = SyncState(state_path())
        self.status = "Bereit"
        self.paused = False
        self.stop_event = threading.Event()
        self.sync_event = threading.Event()
        self.local_event = threading.Event()
        self.last_local_change = 0.0
        self.last_sync = self._read_last_sync()
        self._syncing = False
        self._sync_lock = threading.Lock()
        self._watcher = None
        self._settings_window: SetupDialog | None = None
        self.icon: pystray.Icon | None = None
        self.root: tk.Tk | None = None
        self._last_error = ""
        self._ui_queue: queue.Queue = queue.Queue()

    def _read_last_sync(self) -> float:
        raw = self.state.get_meta("last_sync")
        if not raw:
            return 0.0
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return 0.0

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
            logger.info("Konfiguration geladen: %s", self.settings.local_folder)

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

    def _menu(self) -> Menu:
        return Menu(
            MenuItem(lambda item: self.status, None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("Jetzt synchronisieren", self._action_sync, default=True),
            MenuItem(
                lambda item: "Fortsetzen" if self.paused else "Pausieren",
                self._action_pause,
            ),
            MenuItem("Lokalen Ordner öffnen", self._action_open_folder),
            MenuItem("Einstellungen …", self._action_settings),
            MenuItem("Protokoll öffnen", self._action_open_log),
            Menu.SEPARATOR,
            MenuItem("Beenden", self._action_quit),
        )

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
        self.sync_event.set()

    def _action_pause(self, icon=None, item=None) -> None:
        self.paused = not self.paused
        self._set_status("Pausiert" if self.paused else "Bereit")
        logger.info("Sync %s", "pausiert" if self.paused else "fortgesetzt")

    def _action_open_folder(self, icon=None, item=None) -> None:
        folder = self.settings.local_folder
        if folder and Path(folder).is_dir():
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            self._notify("Kein gültiger lokaler Ordner konfiguriert.")

    def _action_settings(self, icon=None, item=None) -> None:
        if self.root:
            self._ui_queue.put(lambda: self.show_settings(first_run=False))

    def _action_open_log(self, icon=None, item=None) -> None:
        path = log_path()
        if path.exists():
            os.startfile(str(path))  # type: ignore[attr-defined]
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

    def show_settings(self, *, first_run: bool = False) -> None:
        if not self.root:
            return
        if self._settings_window and self._settings_window.winfo_exists():
            self._settings_window.lift()
            self._settings_window.focus_force()
            return
        self._settings_window = SetupDialog(
            self.root,
            self.settings,
            self._on_settings_saved,
            first_run=first_run,
            on_cancel=self._on_settings_cancelled,
        )

    def _on_settings_saved(self, settings) -> None:  # noqa: ANN001
        self.settings = settings
        self._settings_window = None
        self._last_error = ""
        self._restart_watcher()
        self._set_status("Bereit")
        self.sync_event.set()
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

    def note_local_change(self) -> None:
        self.last_local_change = time.time()
        self.local_event.set()

    def _restart_watcher(self) -> None:
        if self._watcher:
            try:
                self._watcher.stop()
                self._watcher.join(timeout=2)
            except Exception:
                logger.debug("Watcher konnte nicht gestoppt werden", exc_info=True)
            self._watcher = None
        folder = self.settings.local_folder
        if not folder or not Path(folder).is_dir():
            return
        try:
            from watchdog.observers import Observer

            observer = Observer()
            observer.schedule(FileChangeHandler(self), folder, recursive=True)
            observer.start()
            self._watcher = observer
            logger.info("Überwache Ordner: %s", folder)
        except Exception:
            logger.exception("Ordnerüberwachung konnte nicht gestartet werden")

    def _scheduler(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(1.0)
            if self.paused or self._syncing:
                continue
            if not self.settings.is_configured or TokenStore().load() is None:
                continue
            interval = max(15, int(self.settings.sync_interval_seconds))
            due = (time.time() - self.last_sync) >= interval
            local_due = self.local_event.is_set() and (time.time() - self.last_local_change) >= 4.0
            if due or local_due:
                self.local_event.clear()
                self.sync_event.set()

    def _sync_worker(self) -> None:
        while not self.stop_event.is_set():
            triggered = self.sync_event.wait(timeout=1.0)
            if self.stop_event.is_set():
                break
            if not triggered:
                continue
            self.sync_event.clear()
            if self.paused or not self.settings.is_configured:
                continue
            self._run_sync()

    def _run_sync(self) -> None:
        if not self._sync_lock.acquire(blocking=False):
            return
        self._syncing = True
        self._set_status("Synchronisiere …")
        logger.info("Sync gestartet")
        adapter = None
        try:
            from .tarkle import TarkleRemote

            oauth = OAuthClient(self.settings)
            try:
                token = oauth.ensure_valid()
            finally:
                oauth.close()

            local_root = Path(self.settings.local_folder)
            encryption = EncryptionManager(local_root)
            encryption.load_cached_key()
            if encryption.enabled and not encryption.unlocked:
                self._set_status("Verschlüsselung gesperrt", error=True)
                logger.warning(
                    "Der encrypted-Ordner ist gesperrt; bitte das Passwort eingeben."
                )
                self._notify(
                    "Bitte das Passwort für den encrypted-Ordner in den Einstellungen eingeben.",
                    "tarklone – Verschlüsselung",
                )
                if self.root:
                    self._ui_queue.put(lambda: self.show_settings(first_run=False))
                return
            if not encryption.enabled:
                encrypted_dir = local_root / "encrypted"
                if encrypted_dir.is_dir() and any(
                    path.is_file() and path.name != KEYFILE_NAME
                    for path in encrypted_dir.rglob("*")
                ):
                    self._set_status("Verschlüsselung nicht eingerichtet", error=True)
                    self._notify(
                        "Der Ordner encrypted enthält Dateien, aber es ist kein "
                        "Passwort gesetzt. Bitte zuerst ein Passwort festlegen.",
                        "tarklone – Verschlüsselung",
                    )
                    if self.root:
                        self._ui_queue.put(lambda: self.show_settings(first_run=False))
                    return

            remote = TarkleRemote(self.settings, token)
            adapter = EncryptionAdapter(remote, encryption)
            engine = SyncEngine(
                Path(self.settings.local_folder),
                adapter,
                self.state,
                sync_deletions=self.settings.sync_deletions,
                conflict_policy=self.settings.conflict_policy,
                exclude=self.settings.exclude,
                logger=logger,
                progress=self._on_progress,
            )
            result = engine.sync()
            self.last_sync = time.time()
            if not encryption.enabled and (
                local_root / "encrypted" / KEYFILE_NAME
            ).is_file():
                self._notify(
                    "Verschlüsselter Ordner erkannt. Bitte das Passwort in den "
                    "Einstellungen eingeben.",
                    "tarklone – Verschlüsselung",
                )
                if self.root:
                    self._ui_queue.put(lambda: self.show_settings(first_run=False))
            if result.errors:
                self._set_status(f"Sync mit {len(result.errors)} Fehler(n)", error=True)
                logger.warning("Sync-Fehler: %s", "; ".join(result.errors[:5]))
                self._notify(
                    f"{len(result.errors)} Fehler beim Sync. Details im Protokoll.",
                    "tarklone – Sync-Fehler",
                )
            elif result.changed:
                self._set_status(f"{result.changed} Änderung(en) synchronisiert")
                logger.info(
                    "Sync fertig: %s hoch, %s runter, %s Konflikte, %s Fehler",
                    result.uploaded,
                    result.downloaded,
                    result.conflicts,
                    len(result.errors),
                )
            else:
                self._set_status("Alles aktuell")
                logger.info("Sync fertig: keine Änderungen")
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
            self._syncing = False
            self._refresh_icon()
            self._sync_lock.release()

    def _on_progress(self, message: str) -> None:
        logger.debug("%s", message)

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
        try:
            self.state.close()
        except Exception:
            logger.debug("Zustandsdatenbank konnte nicht geschlossen werden", exc_info=True)


def main() -> int:
    try:
        return TarkloneApp().run()
    except Exception:
        setup_logging()
        logger.exception("tarklone konnte nicht gestartet werden")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
