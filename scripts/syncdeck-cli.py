#!/usr/bin/env python3
"""Drive the SyncDeck backend without Decky or the QAM.

This is the Phase 1 acceptance harness: copy the repo to the Deck, run
`python3 scripts/syncdeck-cli.py status`, and you have proven the
Deck -> plugin -> Syncthing path end to end before any UI exists.

    ./scripts/syncdeck-cli.py status
    ./scripts/syncdeck-cli.py games
    ./scripts/syncdeck-cli.py suggest 620
    ./scripts/syncdeck-cli.py sync 620 /home/deck/.../Saved\\ Games/Portal\\ 2
    ./scripts/syncdeck-cli.py sync-status
    ./scripts/syncdeck-cli.py unsync 620
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "py_modules"))

from syncdeck.errors import SyncDeckError  # noqa: E402
from syncdeck.service import SyncDeckService  # noqa: E402


def dump(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description="SyncDeck backend CLI")
    parser.add_argument(
        "--settings",
        help="Path to settings.json (defaults to DECKY_PLUGIN_SETTINGS_DIR or ~/.config/syncdeck)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Syncthing connection, version and paired devices")
    games = sub.add_parser("games", help="Installed Steam games and their sync state")
    games.add_argument("--all", action="store_true", help="Include games that use Steam Cloud")
    sub.add_parser("sync-status", help="Folder status for every mapped game")
    sub.add_parser("poll", help="Check for newly installed games")

    suggest = sub.add_parser("suggest", help="Candidate save paths for a game")
    suggest.add_argument("appid", type=int)

    sync = sub.add_parser("sync", help="Register a game's save folder with Syncthing")
    sync.add_argument("appid", type=int)
    sync.add_argument("path")

    unsync = sub.add_parser("unsync", help="Remove a game's Syncthing folder")
    unsync.add_argument("appid", type=int)
    unsync.add_argument("--forget", action="store_true", help="Also forget the stored save path")

    conflicts = sub.add_parser("conflicts", help="List Syncthing conflict files for a game")
    conflicts.add_argument("appid", type=int)

    args = parser.parse_args()

    from syncdeck.store import Store

    service = SyncDeckService(Store(args.settings) if args.settings else None)

    try:
        if args.command == "status":
            dump(service.connection_status())
        elif args.command == "games":
            if args.all:
                service.store.data["skipCloudSaves"] = False  # this run only; not saved
            dump(service.list_games())
        elif args.command == "sync-status":
            dump(service.sync_status())
        elif args.command == "poll":
            dump(service.poll_library())
        elif args.command == "suggest":
            dump(service.suggest_paths(args.appid))
        elif args.command == "sync":
            dump(service.sync_game(args.appid, args.path))
        elif args.command == "unsync":
            dump(service.unsync_game(args.appid, args.forget))
        elif args.command == "conflicts":
            dump(service.conflicts(args.appid))
    except SyncDeckError as exc:
        dump({"error": exc.as_dict()})
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
