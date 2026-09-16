"""Read the local Steam library straight off disk.

Reading appmanifest files rather than going through SteamClient means the
backend can enumerate games without the QAM being open, which is what makes
background reconciliation (see main.py) possible.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterator, Optional

from . import vdf

_APPMANIFEST_RE = re.compile(r"^appmanifest_(\d+)\.acf$")

# StateFlags bit 4 (StateFullyInstalled). Anything without it is mid-download
# or a leftover shortcut and has no save data worth syncing yet.
_STATE_FULLY_INSTALLED = 4

# Compatibility tools (Proton, the Linux runtimes) ship an appmanifest just
# like games do. They are identified by the toolmanifest.vdf that Steam
# itself uses to recognize them -- an appid blocklist goes stale every time
# Valve ships a new Proton.
_TOOL_MARKER = "toolmanifest.vdf"

# Redistributables have no toolmanifest and no saves, so they still need an
# explicit mention. This list should stay short; anything with a
# toolmanifest is already handled above.
_EXCLUDED_APPIDS = {
    228980,  # Steamworks Common Redistributables
}


@dataclass
class Library:
    """One Steam library, as libraryfolders.vdf describes it.

    Steam assigns each library a stable `contentid` and keeps the entry even
    while the drive is unplugged, so a mapping can say "on SD card '1TB
    First', not inserted" rather than merely "path missing".
    """

    path: str
    content_id: str = ""
    label: str = ""

    @property
    def mounted(self) -> bool:
        return os.path.isdir(os.path.join(self.path, "steamapps"))

    def display_name(self) -> str:
        if self.label:
            return self.label
        return "internal storage" if self.path.startswith(os.path.expanduser("~")) or "/home/" in self.path else self.path

    def as_dict(self) -> dict:
        return {"path": self.path, "contentId": self.content_id, "label": self.label, "mounted": self.mounted}


@dataclass
class SteamGame:
    appid: int
    name: str
    install_dir: str
    library_path: str
    last_updated: int = 0
    size_on_disk: int = 0
    manifest_path: str = ""
    # Resolved compatdata directory. Steam does not necessarily put a game's
    # Proton prefix in the library the game is installed in, so this is
    # looked up across every library rather than derived from library_path.
    compat_path: Optional[str] = None

    @property
    def install_path(self) -> str:
        return os.path.join(self.library_path, "steamapps", "common", self.install_dir)

    @property
    def prefix_path(self) -> Optional[str]:
        """The Proton prefix's drive_c, where Windows-game saves land."""
        if not self.compat_path:
            return None
        return os.path.join(self.compat_path, "pfx", "drive_c")

    @property
    def is_proton(self) -> bool:
        return bool(self.compat_path)

    def as_dict(self) -> dict:
        return {
            "appid": self.appid,
            "name": self.name,
            "installDir": self.install_dir,
            "installPath": self.install_path,
            "libraryPath": self.library_path,
            "lastUpdated": self.last_updated,
            "sizeOnDisk": self.size_on_disk,
            "isProton": self.is_proton,
            "prefixPath": self.prefix_path,
        }


def steam_root() -> Optional[str]:
    """The Steam install root (the directory containing steamapps/)."""
    home = os.environ.get("DECKY_USER_HOME") or os.path.expanduser("~")
    candidates = [
        os.environ.get("STEAM_ROOT", ""),
        os.path.join(home, ".local", "share", "Steam"),
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".steam", "root"),
        os.path.join(home, ".var", "app", "com.valvesoftware.Steam", "data", "Steam"),
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(os.path.join(candidate, "steamapps")):
            return os.path.realpath(candidate)
    return None


def libraries(include_missing: bool = False) -> list[Library]:
    """Every Steam library Steam knows about, main library first.

    Unmounted ones (an SD card that is out) are skipped unless asked for.
    """
    root = steam_root()
    if not root:
        return []

    found: list[Library] = [Library(path=root)]
    manifest = os.path.join(root, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(manifest):
        try:
            data = vdf.load(manifest)
        except OSError:
            data = {}
        folders = vdf.get_path(data, "libraryfolders", default={}) or {}
        if isinstance(folders, dict):
            for value in folders.values():
                if isinstance(value, dict):
                    path = value.get("path")
                    library = Library(
                        path=os.path.realpath(path) if isinstance(path, str) else "",
                        content_id=str(value.get("contentid") or ""),
                        label=str(value.get("label") or ""),
                    )
                else:
                    library = Library(path=os.path.realpath(value) if isinstance(value, str) else "")
                if not library.path:
                    continue
                if library.path == found[0].path:
                    found[0] = library  # the main library's own entry carries its content id
                    continue
                if library.mounted or include_missing:
                    found.append(library)

    seen: set[str] = set()
    unique: list[Library] = []
    for library in found:
        if library.path not in seen:
            seen.add(library.path)
            unique.append(library)
    return unique


def library_paths() -> list[str]:
    """Every mounted Steam library on this device, including SD cards."""
    return [library.path for library in libraries()]


def library_for_path(path: str) -> Optional[Library]:
    """The library a path lives in, if any (longest prefix wins)."""
    real = os.path.realpath(path)
    best: Optional[Library] = None
    for library in libraries(include_missing=True):
        if real == library.path or real.startswith(library.path + os.sep):
            if best is None or len(library.path) > len(best.path):
                best = library
    return best


def library_by_content_id(content_id: str) -> Optional[Library]:
    """Steam can list one physical card twice (relabelled/re-added); prefer
    the entry that is mounted, then the one that has a label."""
    if not content_id:
        return None
    matches = [l for l in libraries(include_missing=True) if l.content_id == content_id]
    if not matches:
        return None
    matches.sort(key=lambda l: (not l.mounted, not l.label))
    return matches[0]


def _iter_manifests(library: str) -> Iterator[tuple[int, str]]:
    steamapps = os.path.join(library, "steamapps")
    try:
        entries = os.listdir(steamapps)
    except OSError:
        return
    for entry in entries:
        match = _APPMANIFEST_RE.match(entry)
        if match:
            yield int(match.group(1)), os.path.join(steamapps, entry)


def _prefix_activity(compat_path: str) -> float:
    """When this prefix was last used. Proton rewrites the registry hives
    on every launch, so their mtime tracks real use better than the dir's."""
    newest = 0.0
    for relative in ("pfx/user.reg", "pfx/system.reg", "pfx", ""):
        try:
            newest = max(newest, os.stat(os.path.join(compat_path, relative)).st_mtime)
        except OSError:
            continue
    return newest


def _compatdata_index(library_paths: list[str]) -> dict[int, str]:
    """appid -> compatdata directory, across every library.

    Steam keeps a game's Proton prefix wherever it happened to create it,
    which is frequently not the library holding the game, and moving a
    game between drives can leave a stale copy behind. On the test Deck,
    The Walking Dead lives on the SD card while its live prefix is on
    internal storage and the card holds a two-year-old copy. When an appid
    has several, the most recently used one wins.
    """
    index: dict[int, tuple[float, str]] = {}
    for library in library_paths:
        compatdata = os.path.join(library, "steamapps", "compatdata")
        try:
            entries = os.listdir(compatdata)
        except OSError:
            continue
        for entry in entries:
            if not entry.isdigit():
                continue
            path = os.path.join(compatdata, entry)
            activity = _prefix_activity(path)
            appid = int(entry)
            if appid not in index or activity > index[appid][0]:
                index[appid] = (activity, path)
    return {appid: path for appid, (_, path) in index.items()}


def _is_compat_tool(install_path: str) -> bool:
    """True for Proton and the Steam Linux Runtimes, false for games."""
    return os.path.isfile(os.path.join(install_path, _TOOL_MARKER))


def _parse_manifest(appid: int, manifest_path: str, library: str) -> Optional[SteamGame]:
    try:
        data = vdf.load(manifest_path)
    except OSError:
        return None

    state = vdf.get_path(data, "AppState", default={}) or {}
    if not isinstance(state, dict):
        return None

    try:
        state_flags = int(vdf.get_path(state, "StateFlags", default="0") or 0)
    except ValueError:
        state_flags = 0
    if not state_flags & _STATE_FULLY_INSTALLED:
        return None

    install_dir = vdf.get_path(state, "installdir", default="") or ""
    name = vdf.get_path(state, "name", default="") or f"App {appid}"

    if install_dir and _is_compat_tool(os.path.join(library, "steamapps", "common", install_dir)):
        return None

    def as_int(key: str) -> int:
        try:
            return int(vdf.get_path(state, key, default="0") or 0)
        except ValueError:
            return 0

    return SteamGame(
        appid=appid,
        name=name,
        install_dir=install_dir,
        library_path=library,
        last_updated=as_int("LastUpdated"),
        size_on_disk=as_int("SizeOnDisk"),
        manifest_path=manifest_path,
    )


def installed_games() -> list[SteamGame]:
    """All fully-installed Steam games across every library, sorted by name."""
    libraries = library_paths()
    compat = _compatdata_index(libraries)

    games: dict[int, SteamGame] = {}
    for library in libraries:
        for appid, manifest_path in _iter_manifests(library):
            if appid in _EXCLUDED_APPIDS or appid in games:
                continue
            game = _parse_manifest(appid, manifest_path, library)
            if game:
                game.compat_path = compat.get(appid)
                games[appid] = game
    return sorted(games.values(), key=lambda g: g.name.lower())


def library_fingerprint() -> str:
    """A cheap value that changes when the set of installed games changes.

    Used by the backend's reconciliation poll so we can skip a full re-parse
    when nothing moved. See docs/NOTES.md on install detection.
    """
    parts: list[str] = []
    for library in library_paths():
        steamapps = os.path.join(library, "steamapps")
        try:
            stat = os.stat(steamapps)
        except OSError:
            continue
        parts.append(f"{steamapps}:{int(stat.st_mtime)}")
    return "|".join(parts)


def steam_user_ids() -> list[str]:
    """Steam3 account IDs with a userdata directory on this device."""
    root = steam_root()
    if not root:
        return []
    userdata = os.path.join(root, "userdata")
    try:
        return sorted(
            entry for entry in os.listdir(userdata)
            if entry.isdigit() and entry != "0" and os.path.isdir(os.path.join(userdata, entry))
        )
    except OSError:
        return []


def has_cloud_manifest(appid: int) -> bool:
    """True if Steam has written a cloud manifest (remotecache.vdf) for the app.

    Fallback signal for Steam Cloud when appinfo.vdf is unavailable. Steam
    only writes it for cloud-enabled apps, but only after the game has been
    launched under this account, so absence proves nothing. (The `remote/`
    directory is a weaker signal still: Hades has a manifest but no
    `remote/` dir on the test Deck.)
    """
    root = steam_root()
    if not root:
        return False
    return any(
        os.path.isfile(os.path.join(root, "userdata", user_id, str(appid), "remotecache.vdf"))
        for user_id in steam_user_ids()
    )
