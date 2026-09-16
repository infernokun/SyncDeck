"""Start and autostart the Syncthing daemon from Gaming Mode.

SyncThingy (the Flatpak most Deck users install) only starts Syncthing from
its tray app, which needs a desktop session -- so in Gaming Mode there is no
Syncthing until the user switches to Desktop Mode once. The fix is a user
systemd unit that runs the same `syncthing serve` command at login in any
session. Verified on a Deck: `systemctl --user` and `flatpak run` both work
from the plugin's stripped environment once XDG_RUNTIME_DIR and the session
bus address are set explicitly.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

UNIT_NAME = "syncdeck-syncthing.service"
START_TIMEOUT_S = 12.0


def _user_home() -> str:
    return os.environ.get("DECKY_USER_HOME") or os.path.expanduser("~")


def _env() -> dict:
    """Environment for systemctl/flatpak from inside Decky's plugin process.

    Decky spawns plugins from a root service and drops to the user, so the
    inherited environment is not a login session's: XDG_RUNTIME_DIR and the
    bus address can be missing *or wrong*. Both are derived from the real
    uid and forced, not defaulted -- a stale value makes `systemctl --user`
    fail before systemd ever sees the request.
    """
    uid = os.getuid()
    env = dict(os.environ)
    env["HOME"] = _user_home()
    env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")

    # Decky is a PyInstaller bundle and points LD_LIBRARY_PATH at its own
    # extraction dir so its frozen Python finds its libraries. Children
    # inherit that, so `systemctl` loaded Decky's older libcrypto and died
    # with "version OPENSSL_3.4.0 not found". Give spawned system tools the
    # system's libraries back (PyInstaller keeps the original in *_ORIG).
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    env.pop("LD_LIBRARY_PATH", None)
    if original:
        env["LD_LIBRARY_PATH"] = original
    return env


def _run(args: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _which(name: str) -> Optional[str]:
    for directory in _env()["PATH"].split(os.pathsep):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def flatpak_app_id(config_path: Optional[str]) -> Optional[str]:
    """The Flatpak app that owns a config path under ~/.var/app/<id>/..."""
    if not config_path:
        return None
    marker = "/.var/app/"
    index = config_path.find(marker)
    if index == -1:
        return None
    rest = config_path[index + len(marker):]
    return rest.split("/", 1)[0] or None


def syncthing_command(config_path: Optional[str]) -> Optional[list[str]]:
    """How to launch the daemon that owns this config, or None if unknown."""
    app_id = flatpak_app_id(config_path)
    if app_id:
        flatpak = _which("flatpak") or "/usr/bin/flatpak"
        return [flatpak, "run", "--command=syncthing", app_id, "serve", "--no-browser", "--logfile=default"]
    binary = _which("syncthing")
    if binary:
        return [binary, "serve", "--no-browser", "--logfile=default"]
    return None


def unit_path() -> str:
    return os.path.join(_user_home(), ".config", "systemd", "user", UNIT_NAME)


def unit_content(command: list[str]) -> str:
    exec_start = " ".join(_quote(part) for part in command)
    return (
        "[Unit]\n"
        "Description=Syncthing (managed by SyncDeck)\n"
        "Documentation=https://github.com/infernokun/SyncDeck\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        # Syncthing exits with 3 to request a restart of itself; honour it.
        "SuccessExitStatus=3 4\n"
        "RestartForceExitStatus=3 4\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _quote(part: str) -> str:
    return f'"{part}"' if " " in part else part


def daemon_running() -> bool:
    """Is any `syncthing serve` process alive, however it was started?"""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                argv = handle.read().split(b"\0")
        except OSError:
            continue
        if argv and argv[0].endswith(b"syncthing") and b"serve" in argv:
            return True
    return False


def _systemctl(*args: str) -> tuple[int, str, str]:
    return _run(["systemctl", "--user", *args])


def session_unit(config_path: Optional[str]) -> Optional[str]:
    """The desktop-session autostart unit for a Flatpak Syncthing, if any.

    systemd generates `app-<flatpak-id>@autostart.service` from the tray
    app's XDG autostart entry. It belongs to the graphical session, so
    switching from Desktop Mode to Gaming Mode stops it -- and Syncthing
    with it. That is the "Syncthing keeps stopping" report.
    """
    app_id = flatpak_app_id(config_path)
    return f"app-{app_id}@autostart.service" if app_id else None


def status(config_path: Optional[str]) -> dict:
    command = syncthing_command(config_path)
    installed = os.path.isfile(unit_path())
    can_manage = bool(command) and bool(_which("systemctl"))
    enabled = active = session_scoped = False
    if can_manage and installed:
        enabled = _systemctl("is-enabled", UNIT_NAME)[1] == "enabled"
        active = _systemctl("is-active", UNIT_NAME)[1] == "active"
    session = session_unit(config_path)
    if can_manage and session and not active:
        session_scoped = _systemctl("is-active", session)[1] == "active"
    return {
        "running": daemon_running(),
        "canManage": can_manage,
        "command": command,
        "unitInstalled": installed,
        "autostart": enabled,
        "unitActive": active,
        # True when Syncthing is alive only because a desktop session
        # started it; it will die when that session ends.
        "sessionScoped": session_scoped,
        "unitPath": unit_path(),
    }


def install_unit(config_path: Optional[str]) -> list[str]:
    command = syncthing_command(config_path)
    if not command:
        raise RuntimeError("Could not work out how to launch Syncthing on this device.")
    path = unit_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = unit_content(command)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            unchanged = handle.read() == content
    except OSError:
        unchanged = False
    if not unchanged:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        code, _, err = _systemctl("daemon-reload")
        if code != 0:
            raise RuntimeError(f"systemctl daemon-reload failed: {err}")
    return command


def _unit_diagnostics() -> str:
    _, out, err = _systemctl("status", UNIT_NAME, "--no-pager", "-n", "8")
    return (out or err).strip()[-600:]


def start(config_path: Optional[str]) -> dict:
    """Start Syncthing under the SyncDeck unit and wait until it is alive.

    A daemon that is merely session-scoped still counts as "not ours": it
    is left alone if healthy, since two instances cannot share the ports.
    """
    if daemon_running():
        return status(config_path)
    install_unit(config_path)
    code, _, err = _systemctl("start", UNIT_NAME)
    if code != 0:
        raise RuntimeError(f"Could not start Syncthing: {err or 'systemctl start failed'}\n{_unit_diagnostics()}")

    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if daemon_running():
            return status(config_path)
        time.sleep(0.5)
    raise RuntimeError(
        f"Syncthing did not come up within {int(START_TIMEOUT_S)}s.\n{_unit_diagnostics()}"
    )


def set_autostart(enabled: bool, config_path: Optional[str]) -> dict:
    if enabled:
        install_unit(config_path)
        # --now also starts it, unless something else already runs Syncthing.
        verb = ["enable", "--now"] if not daemon_running() else ["enable"]
        code, _, err = _systemctl(*verb, UNIT_NAME)
    else:
        code, _, err = _systemctl("disable", UNIT_NAME) if os.path.isfile(unit_path()) else (0, "", "")
    if code != 0:
        raise RuntimeError(f"systemctl {'enable' if enabled else 'disable'} failed: {err}")
    return status(config_path)
