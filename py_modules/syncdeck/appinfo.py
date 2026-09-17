"""Reader for Steam's appcache/appinfo.vdf (binary, format v29).

This is the only local, authoritative answer to "does this app use Steam
Cloud?" -- the `ufs` section carries the cloud quota and save-file rules
Valve publishes for every app, whether or not the user has launched it.
It also carries `common.type`, which distinguishes games from tools/DLC.

Only v29 (magic 0x07564429, string-table keys) is supported. That is what
current SteamOS writes; older formats are not worth carrying.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Any, Optional

MAGIC_V29 = 0x07564429

# Real appinfo entries nest a handful of levels; this is only a corruption guard.
_MAX_DEPTH = 64

_T_NESTED, _T_STRING, _T_INT32, _T_FLOAT, _T_PTR, _T_WSTRING, _T_COLOR, _T_UINT64, _T_END = range(9)


# Store feature category "Steam Cloud" -- the badge on the store page. It is
# ticked by hand by developers and is *not* what the client's Cloud checkbox
# follows (Have a Nice Death: no badge, yet Steam syncs its save). Exposed
# for information only.
CATEGORY_STEAM_CLOUD = "category_23"


@dataclass
class AppInfo:
    appid: int
    name: str
    app_type: str  # "game", "tool", "dlc", "application", "music", ...
    cloud_quota: int  # bytes from ufs.quota; 0 when absent
    cloud_save_rules: int  # number of ufs.savefiles entries
    cloud_badge: Optional[bool] = None  # category_23 present; None if no category data
    hide_cloud_ui: bool = False  # ufs.hidecloudui: developer switched cloud off

    @property
    def has_cloud(self) -> bool:
        """Does the Steam client offer Cloud for this game?

        Mirrors the client: a cloud quota is configured and the developer
        has not set hidecloudui. The Walking Dead Definitive has a quota and
        save rules but hidecloudui=1, and the client shows no Cloud section
        for it -- so neither do we.
        """
        if self.hide_cloud_ui:
            return False
        return self.cloud_quota > 0 or self.cloud_save_rules > 0

    @property
    def is_game(self) -> bool:
        return self.app_type in ("game", "application", "")


class AppInfoError(Exception):
    pass


def default_path() -> Optional[str]:
    from .steam import steam_root

    root = steam_root()
    if not root:
        return None
    path = os.path.join(root, "appcache", "appinfo.vdf")
    return path if os.path.isfile(path) else None


def _read_cstring(data: bytes, pos: int) -> tuple[str, int]:
    end = data.index(b"\0", pos)
    return data[pos:end].decode("utf-8", "replace"), end + 1


def _read_string_table(data: bytes, offset: int) -> list[str]:
    (count,) = struct.unpack_from("<I", data, offset)
    pos = offset + 4
    strings: list[str] = []
    for _ in range(count):
        s, pos = _read_cstring(data, pos)
        strings.append(s)
    return strings


def _parse_kv(data: bytes, pos: int, strings: list[str], depth: int = 0) -> tuple[dict[str, Any], int]:
    """Parse one binary-KV block; keys are string-table indices in v29."""
    # A corrupt file could otherwise nest until Python's recursion limit,
    # and a RecursionError would escape the per-app guard in load() and take
    # the whole game list with it.
    if depth > _MAX_DEPTH:
        raise AppInfoError("appinfo.vdf nesting is deeper than expected; treating the app as unreadable")
    result: dict[str, Any] = {}
    while True:
        kind = data[pos]
        pos += 1
        if kind == _T_END:
            return result, pos
        (key_index,) = struct.unpack_from("<I", data, pos)
        pos += 4
        key = strings[key_index] if key_index < len(strings) else f"?{key_index}"

        if kind == _T_NESTED:
            result[key], pos = _parse_kv(data, pos, strings, depth + 1)
        elif kind == _T_STRING:
            result[key], pos = _read_cstring(data, pos)
        elif kind in (_T_INT32, _T_COLOR, _T_PTR):
            (result[key],) = struct.unpack_from("<i", data, pos)
            pos += 4
        elif kind == _T_FLOAT:
            (result[key],) = struct.unpack_from("<f", data, pos)
            pos += 4
        elif kind == _T_UINT64:
            (result[key],) = struct.unpack_from("<Q", data, pos)
            pos += 8
        elif kind == _T_WSTRING:
            # Not used by appinfo in practice; skip a UTF-16 cstring.
            end = data.index(b"\0\0", pos)
            result[key] = data[pos:end].decode("utf-16-le", "replace")
            pos = end + 2
        else:
            raise AppInfoError(f"unknown binary KV type {kind} at offset {pos - 5}")


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def load(path: Optional[str] = None, wanted: Optional[set[int]] = None) -> dict[int, AppInfo]:
    """Parse appinfo.vdf into {appid: AppInfo}.

    `wanted` restricts parsing to those appids, which matters: the file is
    several MB and holds every app Steam has ever shown this account. App
    blocks carry their own size, so unwanted ones are skipped in O(1).
    """
    path = path or default_path()
    if not path:
        return {}
    with open(path, "rb") as handle:
        data = handle.read()

    if len(data) < 16:
        return {}
    magic, _universe = struct.unpack_from("<II", data, 0)
    if magic != MAGIC_V29:
        raise AppInfoError(f"unsupported appinfo.vdf format 0x{magic:08x}; only v29 is supported")
    (strtab_offset,) = struct.unpack_from("<q", data, 8)
    strings = _read_string_table(data, strtab_offset)

    result: dict[int, AppInfo] = {}
    pos = 16
    while pos + 8 <= strtab_offset:
        appid, size = struct.unpack_from("<II", data, pos)
        if appid == 0:
            break
        block_start = pos + 8
        block_end = block_start + size
        pos = block_end

        if wanted is not None and appid not in wanted:
            continue

        # Fixed header inside the block: infoState(4) lastUpdated(4)
        # picsToken(8) sha1(20) changeNumber(4) binarySha1(20) = 60 bytes.
        try:
            kv, _ = _parse_kv(data, block_start + 60, strings)
        except (IndexError, struct.error, ValueError, AppInfoError):
            continue

        root = kv.get("appinfo", kv)
        common = root.get("common") or {}
        ufs = root.get("ufs") or {}
        savefiles = ufs.get("savefiles") or {}
        categories = common.get("category")
        result[appid] = AppInfo(
            appid=appid,
            name=str(common.get("name") or ""),
            app_type=str(common.get("type") or "").lower(),
            cloud_quota=_to_int(ufs.get("quota")),
            cloud_save_rules=len(savefiles) if isinstance(savefiles, dict) else 0,
            cloud_badge=(CATEGORY_STEAM_CLOUD in categories) if isinstance(categories, dict) else None,
            hide_cloud_ui=_to_int(ufs.get("hidecloudui")) == 1,
        )
        if wanted is not None and len(result) == len(wanted):
            break
    return result
