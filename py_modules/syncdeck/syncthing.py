"""A small synchronous Syncthing REST client built on urllib.

Every call is blocking; main.py runs them through asyncio.to_thread so the
Decky event loop is never stalled by a slow or wedged daemon.
"""

from __future__ import annotations

import json
import socket
import time
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .discovery import SyncthingEndpoint
from .errors import SyncthingApiError, SyncthingAuthError, SyncthingUnreachable

DEFAULT_TIMEOUT = 8.0

# Writing to /rest/config makes Syncthing reload, which drops the GUI
# listener and resets in-flight connections. Observed on a real Deck
# (Syncthing v2.1.3, SyncThingy Flatpak): the POST that creates a folder
# succeeds, then the very next request fails with ECONNRESET. Without
# retries the user's first sync reports an error for an action that worked.
#
# Retrying is safe for every call SyncDeck makes: reads are idempotent, and
# folder create/update/delete are keyed by folder id, so a repeat is an
# upsert rather than a duplicate.
#
# Kept short: a reload takes a second or two, and when the daemon is down
# every status poll pays the full backoff before the UI can say so.
_RETRY_BACKOFF_SECONDS = (0.5, 1.0, 1.5)

# Syncthing's GUI cert is self-signed and we only talk to loopback, so
# verification is skipped.
_TLS_CONTEXT = ssl.create_default_context()
_TLS_CONTEXT.check_hostname = False
_TLS_CONTEXT.verify_mode = ssl.CERT_NONE


class SyncthingClient:
    def __init__(self, endpoint: SyncthingEndpoint, timeout: float = DEFAULT_TIMEOUT):
        self.endpoint = endpoint
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, params: Optional[dict] = None, body: Any = None) -> Any:
        url = self.endpoint.base_url.rstrip("/") + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        data = None
        headers = {
            "X-API-Key": self.endpoint.api_key,
            "Accept": "application/json",
            "User-Agent": "SyncDeck",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)

        raw = self._send(request, method, path)

        if raw is None or not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw.decode("utf-8", "replace")

    def _send(self, request: urllib.request.Request, method: str, path: str) -> bytes:
        """Perform the request, retrying while the daemon is reloading."""
        last_error: Optional[Exception] = None

        for attempt in range(len(_RETRY_BACKOFF_SECONDS) + 1):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout, context=_TLS_CONTEXT
                ) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                # A real HTTP response: the daemon is up and has an opinion.
                # Never retry these.
                detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
                if exc.code in (401, 403):
                    raise SyncthingAuthError(
                        "Syncthing rejected the API key. Re-read it from config.xml or set it manually."
                    ) from exc
                raise SyncthingApiError(
                    f"{method} {path} failed with HTTP {exc.code}", status=exc.code, body=detail
                ) from exc
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
                last_error = exc
                if attempt < len(_RETRY_BACKOFF_SECONDS):
                    time.sleep(_RETRY_BACKOFF_SECONDS[attempt])

        raise SyncthingUnreachable(
            f"Could not reach Syncthing at {self.endpoint.base_url} after "
            f"{len(_RETRY_BACKOFF_SECONDS) + 1} attempts: {last_error}"
        ) from last_error

    # -- system ------------------------------------------------------------

    def ping(self) -> bool:
        self._request("GET", "/rest/system/ping")
        return True

    def version(self) -> dict:
        return self._request("GET", "/rest/system/version") or {}

    def system_status(self) -> dict:
        return self._request("GET", "/rest/system/status") or {}

    def my_device_id(self) -> str:
        return str(self.system_status().get("myID", ""))

    def restart_required(self) -> bool:
        result = self._request("GET", "/rest/config/restart-required")
        return bool(result.get("requiresRestart")) if isinstance(result, dict) else False

    def restart(self) -> None:
        self._request("POST", "/rest/system/restart")

    # -- config ------------------------------------------------------------

    def get_config(self) -> dict:
        return self._request("GET", "/rest/config") or {}

    def get_devices(self) -> list[dict]:
        return self._request("GET", "/rest/config/devices") or []

    def get_folders(self) -> list[dict]:
        return self._request("GET", "/rest/config/folders") or []

    def get_folder(self, folder_id: str) -> Optional[dict]:
        try:
            return self._request("GET", f"/rest/config/folders/{urllib.parse.quote(folder_id)}")
        except SyncthingApiError as exc:
            if exc.status == 404:
                return None
            raise

    def add_folder(self, folder: dict) -> dict:
        """Create a folder.

        Syncthing applies this without a user-visible restart, but it does
        reload and drop the GUI listener, so the read-back below frequently
        lands mid-reload. _send handles that.
        """
        self._request("POST", "/rest/config/folders", body=folder)
        return self.get_folder(folder["id"]) or folder

    def update_folder(self, folder: dict) -> dict:
        folder_id = urllib.parse.quote(folder["id"])
        self._request("PUT", f"/rest/config/folders/{folder_id}", body=folder)
        return self.get_folder(folder["id"]) or folder

    def delete_folder(self, folder_id: str) -> None:
        self._request("DELETE", f"/rest/config/folders/{urllib.parse.quote(folder_id)}")

    # -- database / status -------------------------------------------------

    def folder_status(self, folder_id: str) -> dict:
        return self._request("GET", "/rest/db/status", params={"folder": folder_id}) or {}

    def completion(self, folder_id: str, device_id: str) -> dict:
        """Sync completion of one remote device for a folder.

        `remoteState` is the useful part: "valid" once the remote has
        accepted the folder, "notSharing"/"unknown" while it has not.
        Syncthing never adds a shared folder on the remote by itself.
        """
        return self._request("GET", "/rest/db/completion", params={"folder": folder_id, "device": device_id}) or {}

    def folder_errors(self, folder_id: str) -> list[dict]:
        result = self._request("GET", "/rest/folder/errors", params={"folder": folder_id})
        if isinstance(result, dict):
            return result.get("errors") or []
        return []

    def rescan(self, folder_id: str) -> None:
        self._request("POST", "/rest/db/scan", params={"folder": folder_id})


def build_folder(
    folder_id: str,
    label: str,
    path: str,
    device_ids: list[str],
    versioning_days: int = 30,
) -> dict:
    """The folder object SyncDeck creates for a game.

    Defaults chosen for save data specifically: filesystem watcher on with a
    short delay (saves are small and written at quit time), and trashcan
    versioning as the safety net described in the conflict-handling plan.
    """
    folder: dict = {
        "id": folder_id,
        "label": label,
        "path": path,
        "type": "sendreceive",
        "devices": [{"deviceID": device_id} for device_id in device_ids],
        "rescanIntervalS": 3600,
        "fsWatcherEnabled": True,
        "fsWatcherDelayS": 10,
        "ignorePerms": True,
        "minDiskFree": {"value": 1, "unit": "%"},
    }
    if versioning_days > 0:
        folder["versioning"] = {
            "type": "trashcan",
            "params": {"cleanoutDays": str(versioning_days)},
            "cleanupIntervalS": 3600,
        }
    return folder
