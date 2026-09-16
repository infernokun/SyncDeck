"""SyncDeck -- Decky Loader entry point.

Every public method here is callable from the frontend via @decky/api's
callable(). They are intentionally thin: all real work lives in
py_modules/syncdeck/service.py, and all of it is blocking I/O pushed onto a
worker thread so a wedged Syncthing can never stall Decky's event loop.
"""

import asyncio
import functools
import importlib
from typing import Any, Callable, Optional

import decky

from syncdeck.errors import SyncDeckError
from syncdeck.service import SyncDeckService

# How often the backend re-reads the Steam library looking for new installs.
LIBRARY_POLL_SECONDS = 60

# Decky runs plugins in its PyInstaller-frozen Python, which bundles only
# the stdlib modules Decky itself happened to import. Everything SyncDeck
# needs is listed here and checked at startup so a missing one shows up as
# a single clear log line instead of an ImportError deep in a request.
REQUIRED_STDLIB = (
    "asyncio", "dataclasses", "functools", "glob", "json", "os", "re", "socket",
    "ssl", "struct", "subprocess", "tempfile", "threading", "time", "urllib.error",
    "urllib.parse", "urllib.request",
)

EVENT_LIBRARY_CHANGED = "syncdeck/library_changed"
EVENT_CONNECTION_CHANGED = "syncdeck/connection_changed"


def _importable(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:  # noqa: BLE001 -- any failure means "not usable here"
        return False


def _ok(payload: Any = None) -> dict:
    return {"ok": True, "data": payload}


def _err(exc: Exception) -> dict:
    if isinstance(exc, SyncDeckError):
        # Expected failures still deserve a line in the plugin log; without
        # one, "Syncthing keeps stopping" leaves nothing to read afterwards.
        decky.logger.warning("SyncDeck: %s: %s", exc.code, exc)
        return {"ok": False, "error": exc.as_dict()}
    decky.logger.exception("SyncDeck: unhandled backend error")
    return {"ok": False, "error": {"code": "internal", "message": str(exc) or exc.__class__.__name__}}


async def _run(func: Callable[..., Any], *args: Any, **kwargs: Any) -> dict:
    """Run blocking service work off the event loop and normalize the result."""
    try:
        result = await asyncio.to_thread(functools.partial(func, *args, **kwargs))
        if func.__name__ in ("sync_game", "unsync_game", "relocate_game", "forget_game", "start_syncthing", "set_syncthing_autostart"):
            decky.logger.info("SyncDeck: %s%s -> ok", func.__name__, args)
        return _ok(result)
    except Exception as exc:  # noqa: BLE001 -- every error must reach the UI
        return _err(exc)


class Plugin:
    service: Optional[SyncDeckService] = None
    _poll_task: Optional[asyncio.Task] = None
    _last_connected: Optional[bool] = None

    # -- lifecycle ---------------------------------------------------------

    async def _main(self):
        missing = [name for name in REQUIRED_STDLIB if not _importable(name)]
        if missing:
            decky.logger.error("SyncDeck: stdlib modules missing from Decky's Python: %s", ", ".join(missing))
        else:
            decky.logger.info("SyncDeck: all %d required stdlib modules available", len(REQUIRED_STDLIB))

        self.service = SyncDeckService()
        self._last_connected = None
        try:
            await asyncio.to_thread(self.service.ensure_daemon)
        except Exception:  # noqa: BLE001 -- best effort; the UI has a Start button
            decky.logger.exception("SyncDeck: autostart of Syncthing failed")
        self._poll_task = asyncio.create_task(self._library_poll_loop())
        decky.logger.info("SyncDeck backend started (settings: %s)", self.service.store.path)

    async def _unload(self):
        # Decky SIGKILLs a plugin that has not stopped within 5s of the
        # request. Cancellation is immediate, but never let a stuck worker
        # thread turn unload into that timeout.
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await asyncio.wait_for(self._poll_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._poll_task = None
        decky.logger.info("SyncDeck backend stopped")

    async def _uninstall(self):
        decky.logger.info("SyncDeck uninstalled; Syncthing folders were left in place")

    async def _migration(self):
        # No released schema versions to migrate from yet. When one exists,
        # migrate here before _main runs.
        pass

    # -- background --------------------------------------------------------

    async def _library_poll_loop(self):
        """Emit an event when a game appears that has no save mapping yet."""
        while True:
            try:
                await asyncio.sleep(LIBRARY_POLL_SECONDS)
                assert self.service is not None

                changes = await asyncio.to_thread(self.service.poll_library)
                if changes.get("newUnmapped"):
                    await decky.emit(EVENT_LIBRARY_CHANGED, changes)

                # If the user asked for autostart and Syncthing died (e.g. a
                # session-scoped instance went with Desktop Mode), bring ours up.
                await asyncio.to_thread(self.service.ensure_daemon)

                connected = bool((await asyncio.to_thread(self.service.connection_status)).get("connected"))
                if connected != self._last_connected:
                    self._last_connected = connected
                    await decky.emit(EVENT_CONNECTION_CHANGED, {"connected": connected})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- a bad poll must not kill the loop
                decky.logger.exception("SyncDeck: library poll failed")

    # -- connection --------------------------------------------------------

    async def get_status(self) -> dict:
        return await _run(self.service.connection_status)

    async def reconnect(self) -> dict:
        self.service.invalidate_connection()
        return await _run(self.service.connection_status)

    async def get_settings(self) -> dict:
        def read() -> dict:
            data = dict(self.service.store.data)
            syncthing = dict(data.get("syncthing") or {})
            # Never hand the API key to the frontend.
            syncthing["apiKey"] = "********" if syncthing.get("apiKey") else None
            data["syncthing"] = syncthing
            data["settingsPath"] = self.service.store.path
            return data

        return await _run(read)

    async def set_syncthing_config(self, mode: str, base_url: str = "", api_key: str = "") -> dict:
        def write() -> dict:
            current = dict(self.service.store.get("syncthing") or {})
            current["mode"] = "manual" if mode == "manual" else "auto"
            if mode == "manual":
                current["baseUrl"] = base_url.strip() or current.get("baseUrl")
                if api_key.strip():
                    current["apiKey"] = api_key.strip()
            self.service.store.set("syncthing", current)
            self.service.invalidate_connection()
            return self.service.connection_status()

        return await _run(write)

    async def set_target_devices(self, device_ids: list) -> dict:
        return await _run(self.service.set_target_devices, [str(d) for d in device_ids])

    async def set_skip_cloud(self, skip: bool) -> dict:
        return await _run(self.service.set_skip_cloud, bool(skip))

    async def start_syncthing(self) -> dict:
        return await _run(self.service.start_syncthing)

    async def set_syncthing_autostart(self, enabled: bool) -> dict:
        return await _run(self.service.set_syncthing_autostart, bool(enabled))

    # -- games -------------------------------------------------------------

    async def list_games(self) -> dict:
        return await _run(self.service.list_games)

    async def suggest_paths(self, appid: int) -> dict:
        return await _run(self.service.suggest_paths, int(appid))

    async def sync_game(self, appid: int, save_path: str, source: str = "manual") -> dict:
        return await _run(self.service.sync_game, int(appid), save_path, source)

    async def unsync_game(self, appid: int, forget_path: bool = False) -> dict:
        return await _run(self.service.unsync_game, int(appid), bool(forget_path))

    async def relocate_game(self, appid: int) -> dict:
        return await _run(self.service.relocate_game, int(appid))

    async def forget_game(self, appid: int) -> dict:
        return await _run(self.service.forget_game, int(appid))

    async def sync_status(self, appids: Optional[list] = None) -> dict:
        parsed = [int(a) for a in appids] if appids else None
        return await _run(self.service.sync_status, parsed)

    async def get_conflicts(self, appid: int) -> dict:
        return await _run(self.service.conflicts, int(appid))

    async def rescan_game(self, appid: int) -> dict:
        def rescan() -> dict:
            folder_id = self.service._folder_id(int(appid))
            self.service.client().rescan(folder_id)
            return {"folderId": folder_id}

        return await _run(rescan)

    async def ignore_game(self, appid: int, ignored: bool = True) -> dict:
        return await _run(self.service.store.ignore, int(appid), bool(ignored))

    async def poll_library(self) -> dict:
        return await _run(self.service.poll_library)
