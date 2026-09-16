"""Persistent plugin settings.

Lives in DECKY_PLUGIN_SETTINGS_DIR so it survives plugin updates. Written
atomically because a half-written settings file would cost the user every
manual save-path mapping they've entered.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any, Optional

SCHEMA_VERSION = 1

_DEFAULTS: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    # mode: "auto" reads Syncthing's config.xml; "manual" uses the values here.
    "syncthing": {"mode": "auto", "baseUrl": None, "apiKey": None},
    # Device IDs to share every created folder with (the user's PC).
    "targetDevices": [],
    # appid -> mapping record. Keys are strings because JSON objects are.
    "games": {},
    "ignoredAppids": [],
    "versioningDays": 30,
    "skipCloudSaves": True,
    "folderIdPrefix": "deck",
    # Start Syncthing via a user systemd unit at login (see daemon.py).
    "autostartSyncthing": False,
}


def _settings_dir() -> str:
    return os.environ.get("DECKY_PLUGIN_SETTINGS_DIR") or os.path.join(
        os.path.expanduser("~"), ".config", "syncdeck"
    )


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(_settings_dir(), "settings.json")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = json.loads(json.dumps(_DEFAULTS))
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> dict:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (OSError, json.JSONDecodeError):
                return self._data
            if isinstance(loaded, dict):
                self._data = _merge_defaults(loaded)
            return self._data

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=os.path.dirname(self.path), delete=False, suffix=".tmp"
            )
            try:
                json.dump(self._data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(handle.name, self.path)

    # -- generic access ----------------------------------------------------

    @property
    def data(self) -> dict:
        return self._data

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self.save()

    # -- game mappings -----------------------------------------------------

    def folder_id(self, appid: int) -> str:
        prefix = self._data.get("folderIdPrefix") or "deck"
        return f"{prefix}-{appid}"

    def get_mapping(self, appid: int) -> Optional[dict]:
        with self._lock:
            return self._data["games"].get(str(appid))

    def all_mappings(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._data["games"])

    def put_mapping(self, appid: int, **fields: Any) -> dict:
        with self._lock:
            key = str(appid)
            record = self._data["games"].get(key, {"appid": appid})
            record.update(fields)
            record["appid"] = appid
            record.setdefault("folderId", self.folder_id(appid))
            self._data["games"][key] = record
            self.save()
            return record

    def remove_mapping(self, appid: int) -> None:
        with self._lock:
            self._data["games"].pop(str(appid), None)
            self.save()

    def ignore(self, appid: int, ignored: bool = True) -> None:
        with self._lock:
            current = set(self._data.get("ignoredAppids") or [])
            current.add(appid) if ignored else current.discard(appid)
            self._data["ignoredAppids"] = sorted(current)
            self.save()

    def is_ignored(self, appid: int) -> bool:
        with self._lock:
            return appid in set(self._data.get("ignoredAppids") or [])


def _merge_defaults(loaded: dict) -> dict:
    """Shallow-merge stored settings over defaults so new keys appear on upgrade."""
    merged = json.loads(json.dumps(_DEFAULTS))
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    merged["version"] = SCHEMA_VERSION
    return merged
