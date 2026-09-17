"""Orchestration layer: Steam library + settings + Syncthing, in one object.

main.py is a thin async shell over this. Keeping the logic here means it can
be exercised from a plain Python REPL on the Deck (or a dev box with fixture
directories) without Decky in the loop.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional

from . import appinfo, daemon, saves, steam
from .discovery import SyncthingEndpoint, discover
from .errors import SavePathError, SyncDeckError, SyncthingNotFound, SyncthingUnreachable
from .store import Store
from .syncthing import SyncthingClient, build_folder

# Ports SyncDeck warns about when Syncthing's GUI is configured to use them.
_RESERVED_PORTS = {
    1337: "Decky Loader",
    8080: "commonly used by other Deck tooling",
}

# Where Steam libraries usually are on a Windows PC, relative to a drive root.
_PC_STEAM_LIBRARY_DIRS = (
    "Program Files (x86)\\Steam",
    "SteamLibrary",
    "Steam",
    "Games\\Steam",
    "Program Files\\Steam",
)


# A file the user drops on the PC to carry the API key across without typing
# it on the on-screen keyboard.
KEY_FILE_NAME = "syncdeck-key.txt"
_KEY_FILE_MAX_BYTES = 8192

# Syncthing generates 32 character keys, but a user-set one can differ. This
# only has to be tight enough to pick the key out of a small text file.
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")


def _parse_key_file(content: str) -> tuple[Optional[str], Optional[str]]:
    """Pull an address and an API key out of a dropped file.

    Accepts JSON, `key=value` lines, or the bare key on a line of its own,
    so it does not matter how the user writes it.
    """
    base_url: Optional[str] = None
    api_key: Optional[str] = None

    stripped = content.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = {}
        if isinstance(data, dict):
            lowered = {str(k).lower(): v for k, v in data.items()}
            for key in ("apikey", "api_key", "key"):
                if isinstance(lowered.get(key), str):
                    api_key = lowered[key].strip()
                    break
            for key in ("baseurl", "base_url", "url", "address"):
                if isinstance(lowered.get(key), str):
                    base_url = lowered[key].strip()
                    break
            if api_key:
                return base_url, api_key

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and not line.startswith("http"):
            name, _, value = line.partition("=")
            name, value = name.strip().lower(), value.strip()
            if name in ("apikey", "api_key", "key") and _API_KEY_RE.match(value):
                api_key = value
            elif name in ("baseurl", "base_url", "url", "address"):
                base_url = value
            continue
        if line.startswith("http://") or line.startswith("https://"):
            base_url = line
        elif api_key is None and _API_KEY_RE.match(line):
            api_key = line

    return base_url, api_key


def _safe_folder_name(name: str, appid: int) -> str:
    """A game name as a Windows folder name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or f"App {appid}"


class SyncDeckService:
    def __init__(self, store: Optional[Store] = None):
        self.store = store or Store()
        self._endpoint: Optional[SyncthingEndpoint] = None
        self._client: Optional[SyncthingClient] = None
        self._library_fingerprint = ""
        self._known_appids: set[int] = set()

    # -- connection --------------------------------------------------------

    def endpoint(self, refresh: bool = False) -> SyncthingEndpoint:
        if self._endpoint is not None and not refresh:
            return self._endpoint

        configured = self.store.get("syncthing") or {}
        if configured.get("mode") == "manual":
            base_url = (configured.get("baseUrl") or "").strip()
            api_key = (configured.get("apiKey") or "").strip()
            if not base_url or not api_key:
                raise SyncthingNotFound(
                    "SyncDeck is set to manual Syncthing settings but the address or API key is missing."
                )
            self._endpoint = SyncthingEndpoint(base_url=base_url, api_key=api_key, source="manual")
        else:
            self._endpoint = discover()

        self._client = SyncthingClient(self._endpoint)
        return self._endpoint

    def client(self, refresh: bool = False) -> SyncthingClient:
        self.endpoint(refresh=refresh)
        assert self._client is not None
        return self._client

    def invalidate_connection(self) -> None:
        self._endpoint = None
        self._client = None

    def connection_status(self) -> dict:
        """What the QAM header shows: are we talking to Syncthing, and to whom."""
        try:
            endpoint = self.endpoint(refresh=True)
        except SyncDeckError as exc:
            return {"connected": False, "error": exc.as_dict(), "daemon": self._daemon_status(None)}

        try:
            version = self.client().version()
            system = self.client().system_status()
        except SyncDeckError as exc:
            result = {"connected": False, "endpoint": endpoint.as_dict(), "error": exc.as_dict()}
            result["portConflict"] = self._port_conflict(endpoint, exc)
            result["daemon"] = self._daemon_status(endpoint.config_path)
            return result

        devices = []
        my_id = str(system.get("myID", ""))
        try:
            for device in self.client().get_devices():
                device_id = device.get("deviceID", "")
                devices.append(
                    {
                        "deviceId": device_id,
                        "name": device.get("name") or device_id[:7],
                        "isLocal": device_id == my_id,
                    }
                )
        except SyncDeckError:
            pass

        return {
            "connected": True,
            "endpoint": endpoint.as_dict(),
            "version": version.get("version", ""),
            "myDeviceId": my_id,
            "devices": devices,
            "targetDevices": self.store.get("targetDevices") or [],
            "daemon": self._daemon_status(endpoint.config_path),
            "remote": self.remote_status(),
        }

    # -- PC (remote Syncthing) ---------------------------------------------

    def remote_client(self) -> Optional[SyncthingClient]:
        """Client for the PC's Syncthing API, if the user configured it."""
        remote = self.store.get("remote") or {}
        base_url = (remote.get("baseUrl") or "").strip()
        api_key = (remote.get("apiKey") or "").strip()
        if not base_url or not api_key:
            return None
        if "://" not in base_url:
            base_url = "https://" + base_url
        return SyncthingClient(SyncthingEndpoint(base_url=base_url, api_key=api_key, source="manual"), timeout=6.0)

    def remote_status(self) -> dict:
        client = self.remote_client()
        if client is None:
            return {"configured": False}
        try:
            version = client.version()
            return {
                "configured": True,
                "connected": True,
                "baseUrl": client.endpoint.base_url,
                "name": client.my_device_name(),
                "deviceId": client.my_device_id(),
                "os": version.get("os", ""),
                "version": version.get("version", ""),
                # Only Windows paths are mapped for now; other OSes still get
                # the folder shared and accept it by hand.
                "autoAdd": version.get("os") == "windows",
            }
        except SyncDeckError as exc:
            return {"configured": True, "connected": False, "baseUrl": client.endpoint.base_url, "error": exc.as_dict()}

    def detect_pc_url(self) -> Optional[str]:
        """Guess the PC's Syncthing GUI address from the live sync connection.

        The local daemon is already talking to the PC on port 22000, so its
        address is known. The GUI is conventionally on 8384. This is only a
        default for the text box: typing an IP on the on-screen keyboard is
        the worst part of setting this up.
        """
        try:
            my_id = self.client().my_device_id()
            connections = (self.client().connections() or {}).get("connections", {})
        except SyncDeckError:
            return None
        for device_id, info in connections.items():
            if device_id == my_id or not info.get("connected"):
                continue
            address = str(info.get("address") or "")
            host = address.rsplit(":", 1)[0] if ":" in address else address
            if host:
                return f"https://{host}:8384"
        return None

    def _key_file_locations(self) -> list[str]:
        """Directories to check for a dropped key file, most useful first.

        Folders already syncing come first: dropping the file into one on
        the PC is the least painful way to get a 32 character key onto a
        Deck in Gaming Mode, since it arrives by itself.
        """
        places: list[str] = []
        try:
            places.extend(f.get("path", "") for f in self.client().get_folders())
        except SyncDeckError:
            pass
        home = os.environ.get("DECKY_USER_HOME") or os.path.expanduser("~")
        places.append(os.path.join(home, "Downloads"))
        places.append(os.path.join(home, "Desktop"))
        media = "/run/media/" + os.path.basename(home)
        try:
            places.extend(os.path.join(media, name) for name in sorted(os.listdir(media)))
        except OSError:
            pass
        return [p for p in places if p]

    def find_pc_key_file(self) -> Optional[dict]:
        """Locate a dropped key file without consuming it."""
        for directory in self._key_file_locations():
            for name in (KEY_FILE_NAME, KEY_FILE_NAME.upper()):
                path = os.path.join(directory, name)
                try:
                    if os.path.isfile(path) and os.path.getsize(path) <= _KEY_FILE_MAX_BYTES:
                        return {"path": path, "directory": directory}
                except OSError:
                    continue
        return None

    def import_pc_key(self) -> dict:
        """Read the PC's address and API key from a dropped file, then delete it.

        The file is removed once imported: it holds a credential, and if it
        arrived through a synced folder it exists on every device sharing
        that folder until deleted.
        """
        found = self.find_pc_key_file()
        if not found:
            raise SyncDeckError(
                f"No {KEY_FILE_NAME} found. Put it in a folder this Deck already syncs, "
                "in ~/Downloads, or on a USB stick, then try again."
            )

        try:
            with open(found["path"], "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read(_KEY_FILE_MAX_BYTES)
        except OSError as exc:
            raise SyncDeckError(f"Could not read {found['path']}: {exc}") from exc

        base_url, api_key = _parse_key_file(content)
        if not api_key:
            raise SyncDeckError(
                f"{found['path']} does not contain something that looks like a Syncthing API key."
            )

        status = self.set_remote_config(base_url or self.detect_pc_url() or "", api_key)

        removed = True
        try:
            os.remove(found["path"])
        except OSError:
            removed = False

        synced_folders = []
        try:
            synced_folders = [os.path.realpath(f.get("path", "")) for f in self.client().get_folders()]
        except SyncDeckError:
            pass
        directory = os.path.realpath(found["directory"])
        was_synced = any(directory == f or directory.startswith(f + os.sep) for f in synced_folders if f)

        return {
            "imported": True,
            "fileRemoved": removed,
            "fromSyncedFolder": was_synced,
            "path": found["path"],
            "status": status,
        }

    def set_remote_config(self, base_url: str, api_key: str) -> dict:
        current = dict(self.store.get("remote") or {})
        current["baseUrl"] = base_url.strip() or None
        if api_key.strip():
            current["apiKey"] = api_key.strip()
        if not current["baseUrl"]:
            current["apiKey"] = None
        self.store.set("remote", current)
        return self.remote_status()

    @staticmethod
    def _pc_dir_exists(client: SyncthingClient, path: str) -> bool:
        """Does a directory exist on the PC? Uses /rest/system/browse on its parent."""
        clean = path.rstrip("\\/")
        parent, _, name = clean.rpartition("\\")
        if not parent or not name:
            return False
        entries = client.browse(parent + "\\")
        return any(entry.rstrip("\\/").lower() == clean.lower() for entry in entries)

    def _find_pc_install_dir(self, client: SyncthingClient, install_dir: str) -> Optional[str]:
        """Locate a game's install dir in the PC's Steam libraries.

        Steam's libraryfolders.vdf on the PC cannot be read through the
        Syncthing API, so the usual library locations are checked on every
        drive instead.
        """
        for root in client.browse(""):
            root = root.rstrip("\\/") + "\\"
            for library in _PC_STEAM_LIBRARY_DIRS:
                candidate = f"{root}{library}\\steamapps\\common\\{install_dir}"
                if self._pc_dir_exists(client, candidate):
                    return candidate
        return None

    def _pc_target(self, client: SyncthingClient, save_path: str, game: steam.SteamGame) -> dict:
        """Where the folder should live on the PC.

        Prefix saves map exactly to the Windows profile. Saves inside the
        install dir are looked up in the PC's Steam libraries. Anything
        else (Linux-native saves) goes under ~\\SyncDeck\\<game> so it is
        still created without asking. Reports whether the directory already
        exists on the PC, since existing saves will be merged.
        """
        mapped = saves.pc_path(save_path, game)
        home = client.home_dir().rstrip("\\/")

        def absolute(path: str) -> str:
            return home + path[1:] if path.startswith("~") and home else path

        if mapped and mapped.get("kind") in ("profile", "drive_c"):
            return {"pcPath": mapped["path"], "how": "mapped", "pcExisting": self._pc_dir_exists(client, absolute(mapped["path"]))}

        if mapped and mapped.get("kind") == "install":
            found = self._find_pc_install_dir(client, game.install_dir)
            if found:
                install = os.path.realpath(game.install_path)
                rest = os.path.realpath(save_path)[len(install):].strip(os.sep).replace(os.sep, "\\")
                target = f"{found}\\{rest}" if rest else found
                return {"pcPath": target, "how": "install_found", "pcExisting": self._pc_dir_exists(client, target)}

        default = "~\\SyncDeck\\" + _safe_folder_name(game.name, game.appid)
        return {"pcPath": default, "how": "default", "pcExisting": self._pc_dir_exists(client, absolute(default))}

    def _add_on_remote(self, folder_id: str, label: str, save_path: str, game: steam.SteamGame) -> dict:
        """Create the folder on the PC so nothing has to be accepted there.

        Skipped, with a reason, when the PC is not configured or is not
        Windows. Never overwrites a folder the PC already has.
        """
        client = self.remote_client()
        if client is None:
            return {"added": False, "reason": "not_configured"}
        try:
            if client.version().get("os") != "windows":
                return {"added": False, "reason": "not_windows"}
            existing = client.get_folder(folder_id)
            if existing:
                return {"added": True, "reason": "exists", "pcPath": existing.get("path"), "how": "kept"}
            target = self._pc_target(client, save_path, game)
            device_ids = [client.my_device_id(), self.client().my_device_id()]
            folder = build_folder(folder_id, label, target["pcPath"], device_ids, int(self.store.get("versioningDays") or 0))
            client.add_folder(folder)
            return {"added": True, "reason": "created", **target}
        except SyncDeckError as exc:
            return {"added": False, "reason": "error", "error": exc.as_dict()}

    # -- daemon ------------------------------------------------------------

    def _config_path(self) -> Optional[str]:
        try:
            return self.endpoint().config_path
        except SyncDeckError:
            return None

    def _daemon_status(self, config_path: Optional[str]) -> dict:
        try:
            return daemon.status(config_path)
        except Exception as exc:  # noqa: BLE001 -- status must never break the panel
            return {"running": False, "canManage": False, "error": str(exc)}

    def start_syncthing(self) -> dict:
        try:
            result = daemon.start(self._config_path())
        except RuntimeError as exc:
            raise SyncDeckError(str(exc)) from exc
        self.invalidate_connection()
        return result

    def set_syncthing_autostart(self, enabled: bool) -> dict:
        try:
            result = daemon.set_autostart(enabled, self._config_path())
        except RuntimeError as exc:
            raise SyncDeckError(str(exc)) from exc
        self.store.set("autostartSyncthing", bool(enabled))
        return result

    def ensure_daemon(self) -> None:
        """Called at plugin start: honour the autostart setting even if the unit
        was not yet enabled when this login happened."""
        if self.store.get("autostartSyncthing") and not daemon.daemon_running():
            try:
                daemon.start(self._config_path())
            except RuntimeError:
                pass

    def _port_conflict(self, endpoint: SyncthingEndpoint, exc: SyncDeckError) -> Optional[dict]:
        """Turn 'something is on that port but it isn't Syncthing' into a real message."""
        port_text = endpoint.base_url.rsplit(":", 1)[-1]
        try:
            port = int(port_text)
        except ValueError:
            return None
        if port in _RESERVED_PORTS and not isinstance(exc, SyncthingUnreachable):
            return {
                "port": port,
                "owner": _RESERVED_PORTS[port],
                "message": (
                    f"Syncthing's GUI is configured on port {port}, which is {_RESERVED_PORTS[port]}. "
                    "Change Syncthing's GUI port (default 8384) and reconnect."
                ),
            }
        return None

    # -- devices -----------------------------------------------------------

    def set_target_devices(self, device_ids: list[str]) -> list[str]:
        cleaned = [d.strip() for d in device_ids if d and d.strip()]
        self.store.set("targetDevices", cleaned)
        return cleaned

    def _share_device_ids(self) -> list[str]:
        """Local device plus every configured target, deduplicated."""
        targets = list(self.store.get("targetDevices") or [])
        if not targets:
            # Default to every paired remote device. v1 assumes the user has
            # already paired Deck <-> PC, per the project plan's scope call.
            my_id = self.client().my_device_id()
            targets = [
                device.get("deviceID", "")
                for device in self.client().get_devices()
                if device.get("deviceID") and device.get("deviceID") != my_id
            ]
        ids = [self.client().my_device_id()] + targets
        seen: set[str] = set()
        return [i for i in ids if i and not (i in seen or seen.add(i))]

    # -- Steam Cloud -------------------------------------------------------

    def cloud_status(self, games: list[steam.SteamGame]) -> dict[int, dict]:
        """appid -> {"hasCloud": bool, "source": str} for the given games.

        appinfo.vdf is authoritative: Valve's per-app cloud quota and save
        rules, present whether or not the game was ever launched. If it
        cannot be read, fall back to Steam's per-app cloud manifest, which
        only exists for launched cloud games.
        """
        wanted = {g.appid for g in games}
        try:
            info = appinfo.load(wanted=wanted)
        except (appinfo.AppInfoError, OSError):
            info = {}

        status: dict[int, dict] = {}
        for game in games:
            entry = info.get(game.appid)
            if entry is not None:
                status[game.appid] = {"hasCloud": entry.has_cloud, "source": "appinfo"}
            elif steam.has_cloud_manifest(game.appid):
                status[game.appid] = {"hasCloud": True, "source": "remotecache"}
            else:
                status[game.appid] = {"hasCloud": False, "source": "none"}
        return status

    def set_skip_cloud(self, skip: bool) -> bool:
        self.store.set("skipCloudSaves", bool(skip))
        return bool(skip)

    # -- games -------------------------------------------------------------

    def _folder_id(self, appid: int) -> str:
        """The Syncthing folder for a game: an adopted one if present, else ours."""
        mapping = self.store.get_mapping(appid) or {}
        return mapping.get("folderId") or self.store.folder_id(appid)

    def _adopt_existing(self, games: list[steam.SteamGame], folders: list[dict]) -> None:
        """Recognize Syncthing folders the user created by hand.

        Any folder whose path lies inside a game's Proton prefix or install
        directory is that game's save sync, whoever set it up. Recording it
        as an adopted mapping makes it show as synced and keeps it out of
        the Steam Cloud filter.
        """
        roots: list[tuple[str, steam.SteamGame]] = []
        for game in games:
            for root in (game.compat_path, game.install_path):
                if root:
                    roots.append((os.path.realpath(root), game))

        for folder in folders:
            folder_id = folder.get("id") or ""
            path = os.path.realpath(folder.get("path") or "")
            if not folder_id or not path:
                continue
            for root, game in roots:
                if path == root or path.startswith(root + os.sep):
                    existing = self.store.get_mapping(game.appid)
                    if existing and existing.get("folderId") and existing.get("folderId") != folder_id:
                        break  # already mapped to a different folder; leave it
                    if not existing or existing.get("folderId") != folder_id:
                        self.store.put_mapping(
                            game.appid,
                            name=game.name,
                            savePath=path,
                            source="adopted",
                            folderId=folder_id,
                            adopted=True,
                            **self._library_fields(path, game),
                        )
                    break

    @staticmethod
    def _library_fields(path: str, game: Optional[steam.SteamGame] = None) -> dict:
        """Which Steam libraries (drives) a mapping depends on.

        The save path's library answers "can I reach the saves?"; the game's
        own library answers "is the game's drive even inserted?". They are
        often different: a Proton prefix is usually internal while the game
        sits on a card. Empty for paths outside any library (e.g. ~/.config).
        """
        library = steam.library_for_path(path)
        fields = {
            "libraryPath": library.path if library else None,
            "libraryLabel": (library.label or None) if library else None,
            "libraryContentId": (library.content_id or None) if library else None,
        }
        if game is not None:
            game_library = steam.library_for_path(game.library_path)
            fields["gameLibraryLabel"] = (game_library.label or None) if game_library else None
            fields["gameLibraryContentId"] = (game_library.content_id or None) if game_library else None
        return fields

    def _availability(self, mapping: dict, installed: set[int]) -> Optional[dict]:
        """Why a mapped save path cannot be reached right now, or None if it can.

        Distinguishes the three things that look identical on disk:
        the drive is out (library known to Steam but not mounted), the
        game was uninstalled (its files went with it), or the saves moved.
        """
        path = mapping.get("savePath") or ""
        appid = int(mapping.get("appid", 0))
        saves_reachable = bool(path) and os.path.isdir(path)
        game_library = steam.library_by_content_id(mapping.get("gameLibraryContentId") or "")
        game_drive_out = game_library is not None and not game_library.mounted

        if saves_reachable and appid in installed:
            return None
        if saves_reachable:
            # Game gone, saves still here (the usual Proton case: prefix on
            # internal storage). Syncthing carries on; nothing to fix.
            return {
                "reason": "game_missing",
                "libraryLabel": (game_library.label if game_drive_out else None) or mapping.get("gameLibraryLabel"),
                "driveOut": game_drive_out,
                "installed": False,
            }
        library = steam.library_by_content_id(mapping.get("libraryContentId") or "")
        if library is not None and not library.mounted:
            reason = "library_missing"
        elif appid not in installed:
            reason = "uninstalled"
        else:
            reason = "moved"
        return {
            "reason": reason,
            "libraryLabel": (library.label if library else None) or mapping.get("libraryLabel"),
            "driveOut": library is not None and not library.mounted,
            "installed": appid in installed,
        }

    def list_games(self) -> dict:
        """Installed games merged with mapping, sync state and cloud status.

        Games Valve already syncs through Steam Cloud are hidden by default
        (`skipCloudSaves`); SyncDeck is for the ones Steam does not cover.
        Mapped games are always shown.
        """
        games = steam.installed_games()
        try:
            folder_list = self.client().get_folders()
        except SyncDeckError:
            folder_list = []
        folders = {f.get("id"): f for f in folder_list}
        self._adopt_existing(games, folder_list)

        mappings = self.store.all_mappings()
        cloud = self.cloud_status(games)
        skip_cloud = bool(self.store.get("skipCloudSaves"))

        installed = {g.appid for g in games}
        results: list[dict] = []
        hidden_cloud = 0
        for game in games:
            mapping = mappings.get(str(game.appid))
            # Mappings made before library tracking existed: fill it in
            # while the path is still reachable, so a later "drive out" can
            # be named.
            if mapping and mapping.get("savePath") and "libraryContentId" not in mapping \
                    and os.path.isdir(mapping["savePath"]):
                mapping = self.store.put_mapping(game.appid, **self._library_fields(mapping["savePath"], game))
            folder_id = (mapping or {}).get("folderId") or self.store.folder_id(game.appid)
            has_cloud = cloud[game.appid]["hasCloud"]
            synced = folder_id in folders

            if skip_cloud and has_cloud and not synced and not mapping:
                hidden_cloud += 1
                continue

            save_path = (mapping or {}).get("savePath")
            detected = None
            if not save_path and not self.store.is_ignored(game.appid):
                # Auto-detection: the best guess is shown inline so the common
                # case is one confirmation, not a search. Never auto-committed.
                ranked = saves.candidates(game)
                detected = ranked[0].as_dict() if ranked else None

            # A mapped path that vanished while the game is still installed:
            # the saves moved (game moved between drives, prefix recreated).
            # Offer the current best guess as a one-press relocation.
            path_missing = bool(save_path) and not os.path.isdir(save_path)
            relocate_to = None
            if path_missing:
                ranked = saves.candidates(game)
                relocate_to = ranked[0].as_dict() if ranked else None

            entry = game.as_dict()
            entry.update(
                {
                    "folderId": folder_id,
                    "savePath": save_path,
                    "pathSource": (mapping or {}).get("source"),
                    "adopted": bool((mapping or {}).get("adopted")),
                    "synced": synced,
                    "ignored": self.store.is_ignored(game.appid),
                    "hasSteamCloud": has_cloud,
                    "cloudSource": cloud[game.appid]["source"],
                    "detected": detected,
                    "playedOnDeck": saves.prefix_exists(game),
                    "pathMissing": path_missing,
                    "relocateTo": relocate_to,
                    "pcPath": (saves.pc_path(save_path, game) or {}).get("path") if save_path else None,
                    "library": (lambda lib: lib.as_dict() if lib else None)(
                        steam.library_for_path(save_path) if save_path else None
                    ),
                }
            )
            results.append(entry)

        # Mapped games that are not in the installed list at all: on an SD
        # card that is out, or uninstalled. Kept visible so they can be
        # forgotten or relocated.
        unavailable: list[dict] = []
        for key, mapping in mappings.items():
            appid = int(key)
            if appid in installed:
                continue
            why = self._availability(mapping, installed) or {
                "reason": "uninstalled", "libraryLabel": mapping.get("libraryLabel"), "driveOut": False, "installed": False,
            }
            unavailable.append(
                {
                    "appid": appid,
                    "name": mapping.get("name") or f"App {appid}",
                    "savePath": mapping.get("savePath"),
                    "folderId": mapping.get("folderId") or self.store.folder_id(appid),
                    "adopted": bool(mapping.get("adopted")),
                    "relocateTo": None,
                    **why,
                }
            )
        unavailable.sort(key=lambda g: g["name"].lower())

        return {
            "games": results,
            "hiddenCloud": hidden_cloud,
            "skipCloudSaves": skip_cloud,
            "total": len(games),
            "unavailable": unavailable,
        }

    def _game(self, appid: int) -> steam.SteamGame:
        for game in steam.installed_games():
            if game.appid == appid:
                return game
        raise SyncDeckError(f"App {appid} is not installed on this device.")

    def suggest_paths(self, appid: int) -> dict:
        """Candidate save paths for a game, best guess first."""
        game = self._game(appid)

        manifest_hit = saves.resolve_manifest(game)
        ranked = saves.candidates(game)
        if manifest_hit:
            ranked = [manifest_hit] + [c for c in ranked if c.path != manifest_hit.path]

        mapping = self.store.get_mapping(appid)
        return {
            "appid": appid,
            "name": game.name,
            "current": (mapping or {}).get("savePath"),
            "hasSteamCloud": self.cloud_status([game])[appid]["hasCloud"],
            "prefixRoot": game.prefix_path,
            "installPath": game.install_path,
            "candidates": [
                {**c.as_dict(), "pcPath": (saves.pc_path(c.path, game) or {}).get("path")} for c in ranked
            ],
        }

    # -- sync --------------------------------------------------------------

    def sync_game(self, appid: int, save_path: str, source: str = "manual") -> dict:
        """Register a game's save folder with Syncthing and remember the mapping."""
        game = self._game(appid)
        resolved = saves.validate_path(save_path)

        folder_id = self._folder_id(appid)
        folder = build_folder(
            folder_id=folder_id,
            label=f"{game.name} (Deck saves)",
            path=resolved,
            device_ids=self._share_device_ids(),
            versioning_days=int(self.store.get("versioningDays") or 0),
        )

        mapping = self.store.get_mapping(appid) or {}
        existing = self.client().get_folder(folder_id)
        if existing and mapping.get("adopted"):
            # The user's own folder: change only what they asked for.
            if existing.get("path") != resolved:
                existing["path"] = resolved
                result = self.client().update_folder(existing)
            else:
                result = existing
        elif existing:
            existing.update({"path": resolved, "label": folder["label"], "devices": folder["devices"]})
            result = self.client().update_folder(existing)
        else:
            result = self.client().add_folder(folder)

        self.store.put_mapping(
            appid,
            name=game.name,
            savePath=resolved,
            source=source if not mapping.get("adopted") else "adopted",
            folderId=folder_id,
            syncedAt=int(time.time()),
            **self._library_fields(resolved, game),
        )

        remote = self._add_on_remote(folder_id, folder["label"], resolved, game)

        return {
            "appid": appid,
            "folderId": folder_id,
            "path": resolved,
            "isFlatpakPath": saves.is_flatpak_path(resolved),
            "folder": result,
            "remote": remote,
        }

    def relocate_game(self, appid: int) -> dict:
        """Point a game's folder at where its saves are now.

        For when a game moved drives or its prefix was recreated: the
        mapping's path is gone but detection finds the new one.
        """
        game = self._game(appid)
        ranked = saves.candidates(game)
        if not ranked:
            raise SavePathError(f"Could not find where {game.name} keeps its saves now; pick the folder manually.")
        result = self.sync_game(appid, ranked[0].path, source=ranked[0].kind)
        return {"appid": appid, "path": result["path"], "folderId": result["folderId"]}

    def forget_game(self, appid: int) -> dict:
        """Drop a mapping for a game that is gone (uninstalled / drive out).

        SyncDeck's own folder is removed from Syncthing so it stops erroring;
        an adopted folder is left alone -- it was never ours to delete.
        """
        mapping = self.store.get_mapping(appid) or {}
        removed = False
        if mapping and not mapping.get("adopted"):
            folder_id = mapping.get("folderId") or self.store.folder_id(appid)
            try:
                if self.client().get_folder(folder_id):
                    self.client().delete_folder(folder_id)
                    removed = True
            except SyncDeckError:
                pass
        self.store.remove_mapping(appid)
        return {"appid": appid, "removed": removed}

    def unsync_game(self, appid: int, forget_path: bool = False) -> dict:
        mapping = self.store.get_mapping(appid) or {}
        if mapping.get("adopted"):
            raise SyncDeckError(
                "This folder was set up outside SyncDeck. Remove it in Syncthing itself if you want to stop syncing it."
            )
        folder_id = self._folder_id(appid)
        try:
            if self.client().get_folder(folder_id):
                self.client().delete_folder(folder_id)
        except SyncDeckError as exc:
            return {"appid": appid, "removed": False, "error": exc.as_dict()}

        if forget_path:
            self.store.remove_mapping(appid)
        else:
            # Keep the resolved path so re-enabling never re-asks the user.
            self.store.put_mapping(appid, syncedAt=None)
        return {"appid": appid, "removed": True, "forgotPath": forget_path}

    def _remote_devices(self) -> dict[str, str]:
        """deviceID -> name for every paired remote device."""
        my_id = self.client().my_device_id()
        return {
            d["deviceID"]: (d.get("name") or d["deviceID"][:7])
            for d in self.client().get_devices()
            if d.get("deviceID") and d["deviceID"] != my_id
        }

    def _remote_acceptance(self, folder_id: str, folder: dict, remotes: dict[str, str]) -> dict:
        """Which shared devices have accepted this folder, and which have not."""
        accepted: list[str] = []
        waiting: list[str] = []
        for device in folder.get("devices") or []:
            device_id = device.get("deviceID")
            if device_id not in remotes:
                continue
            try:
                remote_state = self.client().completion(folder_id, device_id).get("remoteState")
            except SyncDeckError:
                remote_state = None
            (accepted if remote_state == "valid" else waiting).append(remotes[device_id])
        return {"accepted": accepted, "awaitingAccept": waiting}

    def sync_status(self, appids: Optional[list[int]] = None) -> dict[str, Any]:
        """Per-game sync state for the QAM list."""
        targets = appids if appids is not None else [int(a) for a in self.store.all_mappings()]
        out: dict[str, Any] = {}
        try:
            remotes = self._remote_devices()
            folders = {f.get("id"): f for f in self.client().get_folders()}
        except SyncDeckError:
            remotes, folders = {}, {}

        for appid in targets:
            folder_id = self._folder_id(appid)
            try:
                status = self.client().folder_status(folder_id)
            except SyncDeckError as exc:
                out[str(appid)] = {"state": "unknown", "error": exc.as_dict()}
                continue

            acceptance = self._remote_acceptance(folder_id, folders.get(folder_id) or {}, remotes)
            need = int(status.get("needTotalItems", 0) or 0)
            state = status.get("state") or "unknown"
            out[str(appid)] = {
                **acceptance,
                "folderError": status.get("error") or None,
                "state": state,
                "stateChanged": status.get("stateChanged"),
                "needItems": need,
                "globalBytes": status.get("globalBytes", 0),
                "localBytes": status.get("localBytes", 0),
                "errors": int(status.get("errors", 0) or 0),
                "pullErrors": int(status.get("pullErrors", 0) or 0),
                # "scanning" on a fresh large folder can take many minutes;
                # the UI uses this to show progress instead of looking frozen.
                "scanning": state == "scanning",
                "inSync": state == "idle" and need == 0,
            }
        return out

    def conflicts(self, appid: int) -> list[dict]:
        """Syncthing's *.sync-conflict-* files under a game's save folder."""
        mapping = self.store.get_mapping(appid) or {}
        path = mapping.get("savePath")
        if not path or not os.path.isdir(path):
            return []

        found: list[dict] = []
        for root, _dirs, files in os.walk(path):
            for name in files:
                if ".sync-conflict-" in name:
                    full = os.path.join(root, name)
                    try:
                        stat = os.stat(full)
                    except OSError:
                        continue
                    found.append(
                        {
                            "path": full,
                            "name": name,
                            "relative": os.path.relpath(full, path),
                            "size": stat.st_size,
                            "modified": int(stat.st_mtime),
                        }
                    )
            if len(found) >= 50:
                break
        return found

    # -- library change detection -----------------------------------------

    def poll_library(self) -> dict:
        """Detect newly installed games since the last poll.

        Backend-side counterpart to the frontend's download-completion hook:
        this catches installs that happened while the QAM was closed.
        """
        fingerprint = steam.library_fingerprint()
        games = steam.installed_games()
        current = {g.appid for g in games}

        first_run = not self._known_appids
        added = sorted(current - self._known_appids) if not first_run else []
        removed = sorted(self._known_appids - current) if not first_run else []

        self._known_appids = current
        self._library_fingerprint = fingerprint

        mapped = set(int(a) for a in self.store.all_mappings())
        new_unmapped = [
            {"appid": g.appid, "name": g.name}
            for g in games
            if g.appid in added and g.appid not in mapped and not self.store.is_ignored(g.appid)
        ]
        return {"changed": bool(added or removed), "added": added, "removed": removed, "newUnmapped": new_unmapped}
