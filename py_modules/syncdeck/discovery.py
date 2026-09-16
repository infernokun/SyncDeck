"""Locate a local Syncthing install and read its API key off disk.

Answering open question #2 from the project plan: we read config.xml
directly so the user never has to paste an API key. If discovery fails
(unusual install prefix, permissions), the settings store accepts a
manual host + key override and that path is used instead.

The XML is read with regular expressions rather than xml.etree: Decky runs
plugins inside its PyInstaller-frozen Python, which does not bundle
xml.etree (verified on a real Deck -- the import fails at plugin start).
The <gui> block is three well-known elements, so this is not a loss.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass
from typing import Optional

from .errors import SyncthingNotFound

# Ordered best-guess locations, relative to the user's home directory.
# SteamOS users overwhelmingly install Syncthing as a Flatpak, so those
# paths are checked alongside the native ones.
_RELATIVE_CANDIDATES = (
    ".local/state/syncthing/config.xml",  # Syncthing >= 1.27 (XDG_STATE_HOME)
    ".config/syncthing/config.xml",  # classic native location
)

_FLATPAK_GLOBS = (
    ".var/app/*/config/syncthing/config.xml",
    ".var/app/*/data/syncthing/config.xml",
    ".var/app/*/.local/state/syncthing/config.xml",
)


@dataclass
class SyncthingEndpoint:
    """Everything needed to talk to a Syncthing daemon."""

    base_url: str
    api_key: str
    config_path: Optional[str] = None
    source: str = "discovered"  # "discovered" | "manual"

    def as_dict(self) -> dict:
        return {
            "baseUrl": self.base_url,
            # The key is not returned to the frontend; the UI
            # only ever needs to know whether we have one.
            "hasApiKey": bool(self.api_key),
            "configPath": self.config_path,
            "source": self.source,
        }


def _user_home() -> str:
    # Decky exports the real user's home even when the plugin process
    # has a different HOME.
    return os.environ.get("DECKY_USER_HOME") or os.path.expanduser("~")


def candidate_config_paths() -> list[str]:
    """Every path we will look at, in priority order."""
    paths: list[str] = []

    explicit = os.environ.get("SYNCTHING_HOME") or os.environ.get("STHOMEDIR")
    if explicit:
        paths.append(os.path.join(explicit, "config.xml"))

    home = _user_home()
    paths.extend(os.path.join(home, rel) for rel in _RELATIVE_CANDIDATES)

    for pattern in _FLATPAK_GLOBS:
        paths.extend(sorted(glob.glob(os.path.join(home, pattern))))

    # Preserve order while dropping duplicates.
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def find_config_path() -> Optional[str]:
    for path in candidate_config_paths():
        if os.path.isfile(path) and os.access(path, os.R_OK):
            return path
    return None


_GUI_BLOCK_RE = re.compile(r"<gui\b([^>]*)>(.*?)</gui>", re.DOTALL | re.IGNORECASE)
_TLS_ATTR_RE = re.compile(r"""\btls\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def _element_text(block: str, name: str) -> Optional[str]:
    match = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", block, re.DOTALL | re.IGNORECASE)
    return _unescape(match.group(1)).strip() if match else None


def _unescape(text: str) -> str:
    # The five XML entities; Syncthing's API keys and addresses never need more.
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def parse_config(config_path: str) -> SyncthingEndpoint:
    """Pull the GUI address and API key out of Syncthing's config.xml."""
    try:
        with open(config_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise SyncthingNotFound(f"Could not read Syncthing config at {config_path}: {exc}") from exc

    gui_match = _GUI_BLOCK_RE.search(text)
    if gui_match is None:
        raise SyncthingNotFound(f"No <gui> section in {config_path}")
    gui_attrs, gui_body = gui_match.group(1), gui_match.group(2)

    api_key = _element_text(gui_body, "apikey") or ""
    address = _element_text(gui_body, "address") or "127.0.0.1:8384"

    # A GUI bound to 0.0.0.0 is still reachable on loopback, and loopback
    # is what we want to use regardless of what it advertises.
    host, _, port = address.rpartition(":")
    if host in ("", "0.0.0.0", "[::]", "::"):
        host = "127.0.0.1"
    host = host.strip("[]")
    if ":" in host:  # bare IPv6 literal
        host = f"[{host}]"

    tls_match = _TLS_ATTR_RE.search(gui_attrs)
    scheme = "https" if (tls_match and tls_match.group(1).lower() == "true") else "http"
    base_url = f"{scheme}://{host}:{port or '8384'}"

    return SyncthingEndpoint(base_url=base_url, api_key=api_key, config_path=config_path)


def discover() -> SyncthingEndpoint:
    """Find and parse the local Syncthing config, or explain why we can't."""
    config_path = find_config_path()
    if not config_path:
        raise SyncthingNotFound(
            "Could not find Syncthing's config.xml. Checked: "
            + ", ".join(candidate_config_paths()[:6])
        )
    endpoint = parse_config(config_path)
    if not endpoint.api_key:
        raise SyncthingNotFound(
            f"Found {config_path} but it contains no API key. "
            "Enable the Syncthing GUI once, or set the key manually in SyncDeck settings."
        )
    return endpoint
