"""Resolve where a game's saves live.

Resolution order, most trusted first:
  1. A user override stored in settings (set once, reused forever).
  2. The Ludusavi manifest, when available (Phase 3 -- see resolve_manifest).
  3. Filesystem heuristics over the Proton prefix / XDG dirs, offered to the
     user as ranked candidates.

The heuristics never auto-commit: a wrong save path syncs the wrong
directory to the user's PC.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from typing import Optional

from .errors import SavePathError
from .steam import SteamGame, libraries, steam_root

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

# Directories inside a Proton prefix that commonly hold save data, as
# (relative path, display label, confidence, search depth).
#
# Depth 2 means "also look inside one more level", which is how publisher
# folders work: AppData/LocalLow/<Publisher>/<Game>. Paths are matched
# case-insensitively because a Proton prefix lives on a case-sensitive
# filesystem and games are inconsistent ("My Games" vs "My games").
_PREFIX_SAVE_PARENTS = (
    ("users/steamuser/Saved Games", "Saved Games", 90, 1),
    ("users/steamuser/Documents/Saved Games", "Documents/Saved Games", 90, 1),
    ("users/steamuser/Documents/My Games", "Documents/My Games", 90, 1),
    # Depth 2: Documents/<Publisher>/<Game> is common too (Eidos, Telltale Games).
    ("users/steamuser/Documents", "Documents", 70, 2),
    ("users/steamuser/AppData/Roaming", "AppData/Roaming", 70, 2),
    ("users/steamuser/AppData/Local", "AppData/Local", 70, 2),
    ("users/steamuser/AppData/LocalLow", "AppData/LocalLow", 70, 2),
)

# Names that scream "this is the save folder" when found under an install dir.
_SAVE_DIR_NAMES = {"save", "saves", "savegame", "savegames", "savedata", "profiles", "player"}

# Paths we refuse to hand to Syncthing regardless of how we got them.
_FORBIDDEN_SUFFIXES = ("/", "/home", "/root", "/usr", "/etc", "/var", "/run")


@dataclass
class SaveCandidate:
    path: str
    label: str
    kind: str  # "prefix" | "xdg" | "install" | "manual"
    confidence: int  # 0-100, only meaningful for ranking within a game
    entry_count: int = 0

    def as_dict(self) -> dict:
        data = asdict(self)
        data["entryCount"] = data.pop("entry_count")
        return data


def normalize(name: str) -> str:
    return _NORMALIZE_RE.sub("", name.lower())


# Below this length, a substring hit is more likely noise than a match --
# "ark" is inside "legobatmanlegacyofthedarkknight".
_MIN_SUBSTRING_MATCH = 5


def _names_match(a: str, b: str) -> bool:
    """Fuzzy name comparison that resists accidental substring hits.

    Exact matches and prefixes always count ("Pal" is Palworld's folder).
    A hit in the middle of a longer name has to be substantial to count,
    which is what keeps KDE's ~/.local/share/ark out of LEGO Batman.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if longer.startswith(shorter):
        return True
    return len(shorter) >= _MIN_SUBSTRING_MATCH and shorter in longer


def _entry_count(path: str) -> int:
    try:
        with os.scandir(path) as entries:
            return sum(1 for _ in entries)
    except OSError:
        return 0


def _user_home() -> str:
    return os.environ.get("DECKY_USER_HOME") or os.path.expanduser("~")


# -- validation ------------------------------------------------------------


def validate_path(path: str) -> str:
    """Normalize a save path and reject anything unsafe to sync wholesale."""
    if not path or not path.strip():
        raise SavePathError("Save path is empty.")

    resolved = os.path.realpath(os.path.expanduser(path.strip()))

    if not os.path.isdir(resolved):
        raise SavePathError(f"{resolved} is not a directory.")

    if resolved in _FORBIDDEN_SUFFIXES or resolved in _forbidden_roots():
        raise SavePathError(f"Refusing to sync {resolved} -- pick the game's save folder, not its parent.")

    for tree in _forbidden_trees():
        if resolved == tree or resolved.startswith(tree + os.sep):
            raise SavePathError(
                f"Refusing to sync {resolved}: it holds credentials, not game saves."
            )

    if not os.access(resolved, os.R_OK | os.X_OK):
        raise SavePathError(f"No read access to {resolved}.")

    return resolved


def _forbidden_roots() -> set[str]:
    """Directories that are never a save folder themselves.

    Syncing one of these would push a Steam library, every Proton prefix or
    the whole home directory to the PC. Their contents are fine: a save
    folder inside compatdata is exactly what this plugin is for.
    """
    home = _user_home()
    roots = {home, os.path.join(home, ".local"), os.path.join(home, ".local", "share"), os.path.join(home, ".config"),
             os.path.join(home, ".var"), os.path.join(home, ".var", "app"), os.path.join(home, ".steam")}
    root = steam_root()
    if root:
        roots.add(root)
    for library in libraries(include_missing=True):
        roots.add(library.path)
        for sub in ("steamapps", "steamapps/common", "steamapps/compatdata", "userdata"):
            roots.add(os.path.join(library.path, *sub.split("/")))
    return {os.path.realpath(r) for r in roots}


def _forbidden_trees() -> set[str]:
    """Directories that are off limits along with everything inside them.

    These hold credentials, not saves. `homebrew` is Decky's own directory:
    it contains SyncDeck's settings file, which may hold the PC's Syncthing
    API key, so syncing it would copy that key to the PC.
    """
    home = _user_home()
    trees = {os.path.join(home, name) for name in (".ssh", ".gnupg", ".pki", ".password-store", "homebrew")}
    settings_dir = os.environ.get("DECKY_PLUGIN_SETTINGS_DIR")
    if settings_dir:
        trees.add(os.path.dirname(os.path.realpath(settings_dir)))
    return {os.path.realpath(t) for t in trees}


def pc_path(path: str, game: Optional[SteamGame] = None) -> Optional[dict]:
    """Where the same save folder lives on a Windows PC, if that is knowable.

    A Proton prefix mirrors a Windows user profile, so anything under
    users/steamuser/ maps to the user's profile on the PC (Syncthing on
    Windows expands a leading ~ to it). Saves inside the install dir map to the
    game's install dir in the PC's Steam library, whose location is not
    known here; that is returned as a relative hint. Linux-native paths
    have no Windows equivalent.
    """
    real = os.path.realpath(path)
    marker = os.sep + "drive_c" + os.sep
    index = real.find(marker)
    if index != -1:
        inside = real[index + len(marker):]
        parts = inside.split(os.sep)
        if len(parts) >= 3 and parts[0] == "users":
            # users/<name>/<rest> -> ~\<rest>
            rest = parts[2:]
            return {"os": "windows", "path": "~\\" + "\\".join(rest), "kind": "profile"}
        return {"os": "windows", "path": "C:\\" + "\\".join(parts), "kind": "drive_c"}
    if game is not None:
        install = os.path.realpath(game.install_path)
        if real == install or real.startswith(install + os.sep):
            rest = real[len(install):].strip(os.sep).split(os.sep) if real != install else []
            return {
                "os": "windows",
                "path": "\\".join(["<Steam library>", "steamapps", "common", game.install_dir] + rest),
                "kind": "install",
                "relative": True,
            }
    return None


def prefix_exists(game: SteamGame) -> bool:
    """Has this game been launched on this device at all?

    A Proton prefix is created on first launch; without one there is no save
    folder to find, and the UI should say "play it once" rather than
    "nothing found".
    """
    prefix = game.prefix_path
    if prefix:
        return os.path.isdir(os.path.join(prefix, "users", "steamuser"))
    return os.path.isdir(game.install_path)


def is_flatpak_path(path: str) -> bool:
    """Flatpak-sandboxed paths; see docs/NOTES.md."""
    return "/.var/app/" in os.path.realpath(path)


# -- candidate discovery ---------------------------------------------------


def _resolve_ci(base: str, *segments: str) -> Optional[str]:
    """Walk a relative path case-insensitively, a segment at a time."""
    current = base
    for segment in segments:
        try:
            entries = os.listdir(current)
        except OSError:
            return None
        match = next((entry for entry in entries if entry.lower() == segment.lower()), None)
        if match is None:
            return None
        current = os.path.join(current, match)
    return current if os.path.isdir(current) else None


def _scan_for_match(
    parent: str, label: str, game: SteamGame, confidence: int, depth: int
) -> list[SaveCandidate]:
    """Find directories under `parent` whose name looks like the game's.

    Recurses one extra level when depth allows, to catch the
    <Publisher>/<Game> layout that Unity titles use under AppData/LocalLow.
    """
    found: list[SaveCandidate] = []
    try:
        entries = sorted(os.scandir(parent), key=lambda e: e.name)
    except OSError:
        return found

    for entry in entries:
        if not entry.is_dir():
            continue
        if _names_match(entry.name, game.name) or _names_match(entry.name, game.install_dir):
            found.append(
                SaveCandidate(
                    path=entry.path,
                    label=f"{label}/{entry.name}",
                    kind="prefix",
                    confidence=confidence,
                    entry_count=_entry_count(entry.path),
                )
            )
        elif depth > 1:
            # Not a match itself -- it may be the publisher folder.
            found.extend(
                _scan_for_match(entry.path, f"{label}/{entry.name}", game, confidence - 10, depth - 1)
            )
    return found


def _prefix_candidates(game: SteamGame) -> list[SaveCandidate]:
    drive_c = game.prefix_path
    if not drive_c or not os.path.isdir(drive_c):
        return []

    found: list[SaveCandidate] = []
    for relative, label, confidence, depth in _PREFIX_SAVE_PARENTS:
        parent = _resolve_ci(drive_c, *relative.split("/"))
        if parent:
            found.extend(_scan_for_match(parent, label, game, confidence, depth))
    return found


def _xdg_candidates(game: SteamGame) -> list[SaveCandidate]:
    found: list[SaveCandidate] = []
    home = _user_home()
    roots = (
        (os.path.join(home, ".local", "share"), "~/.local/share", 75),
        (os.path.join(home, ".config"), "~/.config", 65),
        (os.path.join(home, ".local", "share", "Steam", "steamapps", "compatdata"), "", 0),
    )
    for root, label, confidence in roots:
        if not label or not os.path.isdir(root):
            continue
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and (
                _names_match(entry.name, game.name) or _names_match(entry.name, game.install_dir)
            ):
                found.append(
                    SaveCandidate(
                        path=entry.path,
                        label=f"{label}/{entry.name}",
                        kind="xdg",
                        confidence=confidence,
                        entry_count=_entry_count(entry.path),
                    )
                )
    return found


def _install_candidates(game: SteamGame) -> list[SaveCandidate]:
    """Some games (especially older/indie ones) save next to their binaries."""
    found: list[SaveCandidate] = []
    install = game.install_path
    if not os.path.isdir(install):
        return found
    try:
        entries = sorted(os.scandir(install), key=lambda e: e.name)
    except OSError:
        return found
    for entry in entries:
        if entry.is_dir() and normalize(entry.name) in _SAVE_DIR_NAMES:
            found.append(
                SaveCandidate(
                    path=entry.path,
                    label=f"{game.install_dir}/{entry.name}",
                    kind="install",
                    confidence=60,
                    entry_count=_entry_count(entry.path),
                )
            )
    return found


def candidates(game: SteamGame) -> list[SaveCandidate]:
    """Ranked save-path guesses for a game, best first, de-duplicated."""
    everything = _prefix_candidates(game) + _xdg_candidates(game) + _install_candidates(game)

    by_path: dict[str, SaveCandidate] = {}
    for candidate in everything:
        real = os.path.realpath(candidate.path)
        existing = by_path.get(real)
        if existing is None or candidate.confidence > existing.confidence:
            by_path[real] = candidate

    ranked = list(by_path.values())
    # Empty directories are almost never the right answer -- a game that has
    # been played has written something.
    ranked.sort(key=lambda c: (c.confidence, c.entry_count > 0), reverse=True)
    return ranked


def resolve_manifest(game: SteamGame) -> Optional[SaveCandidate]:
    """Phase 3 hook: look the game up in a Ludusavi-style manifest.

    Returns None today. Wiring this up is tracked in docs/ROADMAP.md; the
    call site in main.py already prefers its result over the heuristics, so
    Phase 3 is a drop-in here rather than a refactor.
    """
    _ = game
    return None

