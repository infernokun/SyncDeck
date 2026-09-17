"""Offline tests for the parsing and settings layers.

Everything here runs against fixture directories, so `python3 -m unittest`
works on a dev box with no Steam, no Syncthing and no Decky installed.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "py_modules"))

import struct  # noqa: E402

from syncdeck import appinfo, daemon, discovery, saves, steam, vdf  # noqa: E402
from syncdeck.service import SyncDeckService  # noqa: E402
from syncdeck.errors import SavePathError, SyncDeckError, SyncthingNotFound  # noqa: E402
from syncdeck.store import Store  # noqa: E402
from syncdeck import syncthing as syncthing_mod  # noqa: E402
from syncdeck.discovery import SyncthingEndpoint  # noqa: E402
from syncdeck.errors import SyncthingApiError, SyncthingAuthError, SyncthingUnreachable  # noqa: E402
from syncdeck.syncthing import SyncthingClient, build_folder  # noqa: E402

APPMANIFEST = """"AppState"
{{
\t"appid"\t\t"{appid}"
\t"name"\t\t"{name}"
\t"installdir"\t\t"{installdir}"
\t"StateFlags"\t\t"{flags}"
\t"LastUpdated"\t\t"1700000000"
\t"SizeOnDisk"\t\t"12345"
}}
"""

CONFIG_XML = """<configuration version="37">
  <gui enabled="true" tls="false">
    <address>0.0.0.0:8384</address>
    <apikey>test-api-key</apikey>
  </gui>
</configuration>
"""


def make_steam_root(base: str, games: list[tuple[int, str, str]]) -> str:
    root = os.path.join(base, ".local", "share", "Steam")
    steamapps = os.path.join(root, "steamapps")
    os.makedirs(os.path.join(steamapps, "common"), exist_ok=True)
    for appid, name, installdir in games:
        os.makedirs(os.path.join(steamapps, "common", installdir), exist_ok=True)
        with open(os.path.join(steamapps, f"appmanifest_{appid}.acf"), "w", encoding="utf-8") as handle:
            handle.write(APPMANIFEST.format(appid=appid, name=name, installdir=installdir, flags=4))
    return root


def encode_appinfo_v29(apps: list[dict]) -> bytes:
    """Build a minimal appinfo.vdf (format v29) for tests.

    Mirrors what Steam writes closely enough for the reader: a 16-byte file
    header, one sized block per app with the 60-byte fixed header, binary
    KeyValues with string-table keys, and the string table at the end.
    """
    strings: list[str] = []

    def sid(key: str) -> int:
        if key not in strings:
            strings.append(key)
        return strings.index(key)

    def kv(node: dict) -> bytes:
        out = b""
        for key, value in node.items():
            if isinstance(value, dict):
                out += bytes([0]) + struct.pack("<I", sid(key)) + kv(value)
            elif isinstance(value, int):
                out += bytes([2]) + struct.pack("<I", sid(key)) + struct.pack("<i", value)
            else:
                out += bytes([1]) + struct.pack("<I", sid(key)) + str(value).encode() + b"\0"
        return out + bytes([8])

    blocks = b""
    for app in apps:
        body = b"\0" * 60 + kv({"appinfo": app["kv"]})
        blocks += struct.pack("<II", app["appid"], len(body)) + body

    table = struct.pack("<I", len(strings)) + b"".join(s.encode() + b"\0" for s in strings)
    header = struct.pack("<II", appinfo.MAGIC_V29, 1) + struct.pack("<q", 16 + len(blocks))
    return header + blocks + table


class AppInfoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "appinfo.vdf")
        with open(self.path, "wb") as handle:
            handle.write(encode_appinfo_v29([
                {"appid": 8140, "kv": {"common": {"name": "Tomb Raider: Underworld", "type": "Game"}}},
                {"appid": 1145360, "kv": {
                    "common": {"name": "Hades", "type": "Game", "category": {"category_23": 1, "category_2": 1}},
                    "ufs": {"quota": 62914560, "maxnumfiles": 100, "savefiles": {"0": {"root": "WinSavedGames"}}},
                }},
                {"appid": 3658110, "kv": {"common": {"name": "Proton 10.0", "type": "Tool"}}},
                # Quota + save rules but hidecloudui and no store badge: the
                # Steam client shows no Cloud option, so neither do we.
                {"appid": 1449690, "kv": {
                    "common": {"name": "TWD", "type": "Game", "category": {"category_2": 1}},
                    "ufs": {"quota": 104857600, "hidecloudui": 1, "savefiles": {"0": {"root": "WinMyDocuments"}}},
                }},
                # Quota, no store badge, no hidecloudui: the client shows Cloud
                # (Have a Nice Death is this shape and Steam does sync it).
                {"appid": 498240, "kv": {
                    "common": {"name": "Batman", "type": "Game", "category": {"category_2": 1}},
                    "ufs": {"quota": 104857600, "savefiles": {"0": {"root": "WinMyDocuments"}}},
                }},
                {"appid": 777, "kv": {"common": {"name": "Legacy"}, "ufs": {"quota": 5}}},
            ]))

    def test_cloud_follows_quota_unless_the_developer_hid_it(self):
        info = appinfo.load(self.path)
        self.assertFalse(info[1449690].has_cloud, "hidecloudui=1: client shows no Cloud section")
        self.assertTrue(info[1449690].hide_cloud_ui)
        self.assertTrue(info[498240].has_cloud, "quota without hidecloudui, badge irrelevant")
        self.assertTrue(info[777].has_cloud)
        self.assertFalse(info[1145360].hide_cloud_ui)

    def test_reads_cloud_quota_and_type(self):
        info = appinfo.load(self.path)
        self.assertEqual(sorted(info), [777, 8140, 498240, 1145360, 1449690, 3658110])
        self.assertTrue(info[1145360].has_cloud)
        self.assertEqual(info[1145360].cloud_quota, 62914560)
        self.assertFalse(info[8140].has_cloud)
        self.assertEqual(info[8140].app_type, "game")
        self.assertEqual(info[3658110].app_type, "tool")
        self.assertFalse(info[3658110].is_game)

    def test_wanted_filter_skips_other_blocks(self):
        info = appinfo.load(self.path, wanted={8140})
        self.assertEqual(list(info), [8140])

    def test_rejects_unknown_format(self):
        with open(self.path, "wb") as handle:
            handle.write(struct.pack("<II", 0x07564428, 1) + b"\0" * 16)
        with self.assertRaises(appinfo.AppInfoError):
            appinfo.load(self.path)


class CloudFilterTests(unittest.TestCase):
    """SyncDeck is for games Steam Cloud does not cover; the rest are hidden."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["DECKY_USER_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "DECKY_USER_HOME", None)
        root = make_steam_root(self.tmp.name, [
            (8140, "Tomb Raider: Underworld", "TRU"),
            (1145360, "Hades", "Hades"),
            (1449690, "The Walking Dead", "TWD"),
        ])
        os.makedirs(os.path.join(root, "appcache"))
        with open(os.path.join(root, "appcache", "appinfo.vdf"), "wb") as handle:
            handle.write(encode_appinfo_v29([
                {"appid": 8140, "kv": {"common": {"name": "TRU", "type": "Game"}}},
                {"appid": 1145360, "kv": {"common": {"name": "Hades", "type": "Game", "category": {"category_23": 1}}, "ufs": {"quota": 1}}},
                {"appid": 1449690, "kv": {"common": {"name": "TWD", "type": "Game", "category": {"category_23": 1}}, "ufs": {"quota": 1}}},
            ]))
        self.service = SyncDeckService(Store(os.path.join(self.tmp.name, "settings.json")))
        # No Syncthing in tests: folder lookup fails closed to "nothing synced".
        self.service.client = lambda refresh=False: (_ for _ in ()).throw(SyncthingUnreachable("test"))

    def test_hides_cloud_games_by_default(self):
        result = self.service.list_games()
        self.assertEqual([g["appid"] for g in result["games"]], [8140])
        self.assertEqual(result["hiddenCloud"], 2)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["games"][0]["cloudSource"], "appinfo")

    def test_toggle_shows_cloud_games(self):
        self.service.set_skip_cloud(False)
        result = self.service.list_games()
        self.assertEqual(len(result["games"]), 3)
        self.assertEqual(result["hiddenCloud"], 0)
        self.assertTrue(next(g for g in result["games"] if g["appid"] == 1145360)["hasSteamCloud"])

    def test_mapped_cloud_game_stays_visible(self):
        """A sync the user set up must never vanish because of the filter."""
        self.service.store.put_mapping(1449690, name="TWD", savePath="/tmp", source="manual")
        result = self.service.list_games()
        self.assertIn(1449690, [g["appid"] for g in result["games"]])
        self.assertEqual(result["hiddenCloud"], 1)


class _FakeClient:
    """Enough of SyncthingClient for list/sync tests: an in-memory folder list."""

    def __init__(self, folders):
        self.folders = {f["id"]: dict(f) for f in folders}
        self.deleted = []

    def get_folders(self):
        return list(self.folders.values())

    def get_folder(self, folder_id):
        return self.folders.get(folder_id)

    def my_device_id(self):
        return "LOCAL"

    def get_devices(self):
        return [{"deviceID": "LOCAL"}, {"deviceID": "PC"}]

    def add_folder(self, folder):
        self.folders[folder["id"]] = dict(folder)
        return folder

    def update_folder(self, folder):
        self.folders[folder["id"]] = dict(folder)
        return folder

    def delete_folder(self, folder_id):
        self.deleted.append(folder_id)
        self.folders.pop(folder_id, None)

    def folder_status(self, folder_id):
        return {"state": "idle", "needTotalItems": 0}

    def version(self):
        return {"version": "v2.1.5", "os": getattr(self, "os_name", "windows")}

    conns: dict = {}

    def connections(self):
        return {"connections": self.conns}

    # a fake Windows filesystem for /rest/system/browse: parent -> [children]
    tree: dict = {}
    home = "C:\\Users\\infer"

    def home_dir(self):
        return self.home

    def browse(self, current=""):
        key = current.rstrip("\\/") if current else ""
        return [key + "\\" + child + "\\" if key else child + "\\" for child in self.tree.get(key, [])]

    def my_device_name(self):
        return "PC"

    # remote acceptance: PC has accepted only folders listed here
    accepted_on_pc: set = set()

    def completion(self, folder_id, device_id):
        if device_id == "PC" and folder_id in self.accepted_on_pc:
            return {"remoteState": "valid", "completion": 100}
        return {"remoteState": "unknown", "completion": 0}


class AdoptionTests(unittest.TestCase):
    """Folders the user made by hand inside a game's prefix are that game's sync."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["DECKY_USER_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "DECKY_USER_HOME", None)
        root = make_steam_root(self.tmp.name, [(1449690, "The Walking Dead", "TWD"), (8140, "Tomb Raider", "TRU")])
        self.saves = os.path.join(
            root, "steamapps", "compatdata", "1449690", "pfx", "drive_c",
            "users", "steamuser", "Documents", "Telltale Games", "The Walking Dead Definitive",
        )
        os.makedirs(self.saves)
        os.makedirs(os.path.join(root, "steamapps", "compatdata", "8140", "pfx", "drive_c", "users", "steamuser"))
        self.service = SyncDeckService(Store(os.path.join(self.tmp.name, "settings.json")))
        self.client = _FakeClient([{"id": "gsi4n-zdnvt", "label": "The Walking Dead", "path": self.saves + "/", "devices": []}])
        self.service.client = lambda refresh=False: self.client

    def test_existing_folder_is_adopted_and_shown_as_synced(self):
        result = self.service.list_games()
        twd = next(g for g in result["games"] if g["appid"] == 1449690)
        self.assertTrue(twd["synced"])
        self.assertTrue(twd["adopted"])
        self.assertEqual(twd["folderId"], "gsi4n-zdnvt")
        self.assertEqual(twd["savePath"], os.path.realpath(self.saves))
        self.assertEqual(self.service.store.get_mapping(1449690)["source"], "adopted")

    def test_status_uses_the_adopted_folder_id(self):
        self.service.list_games()
        self.assertIn("1449690", self.service.sync_status([1449690]))
        self.assertEqual(self.service._folder_id(1449690), "gsi4n-zdnvt")

    def test_status_reports_devices_that_have_not_accepted_the_folder(self):
        """Regression: a freshly shared folder showed "Up to date" while the PC
        had not approved it and nothing was actually syncing."""
        os.makedirs(os.path.join(self.tmp.name, "saves"))
        self.service.sync_game(8140, os.path.join(self.tmp.name, "saves"))
        status = self.service.sync_status([8140])["8140"]
        self.assertEqual(status["awaitingAccept"], ["PC"])
        self.assertEqual(status["accepted"], [])

        self.client.accepted_on_pc.add("deck-8140")
        status = self.service.sync_status([8140])["8140"]
        self.assertEqual(status["awaitingAccept"], [])
        self.assertEqual(status["accepted"], ["PC"])

    def test_unsync_refuses_to_delete_an_adopted_folder(self):
        self.service.list_games()
        with self.assertRaises(SyncDeckError):
            self.service.unsync_game(1449690)
        self.assertEqual(self.client.deleted, [])

    def test_mapping_records_the_library_it_lives_on(self):
        self.service.list_games()
        mapping = self.service.store.get_mapping(1449690)
        self.assertEqual(mapping["libraryPath"], steam.steam_root())

    def test_pre_existing_mapping_gets_library_fields_backfilled(self):
        self.service.store.put_mapping(1449690, name="TWD", savePath=self.saves, source="adopted",
                                       folderId="gsi4n-zdnvt", adopted=True)
        self.assertNotIn("libraryContentId", self.service.store.get_mapping(1449690))
        self.service.list_games()
        self.assertIn("libraryContentId", self.service.store.get_mapping(1449690))

    def test_uninstalled_game_is_listed_as_unavailable(self):
        self.service.list_games()
        os.remove(os.path.join(steam.steam_root(), "steamapps", "appmanifest_1449690.acf"))
        import shutil; shutil.rmtree(self.saves)
        result = self.service.list_games()
        self.assertNotIn(1449690, [g["appid"] for g in result["games"]])
        entry = next(u for u in result["unavailable"] if u["appid"] == 1449690)
        self.assertEqual(entry["reason"], "uninstalled")
        self.assertTrue(entry["adopted"])

    def test_missing_path_on_installed_game_offers_relocation(self):
        old = os.path.join(self.tmp.name, "old-saves")
        os.makedirs(old)
        self.service.sync_game(8140, old)
        import shutil; shutil.rmtree(old)
        new = os.path.join(steam.steam_root(), "steamapps", "compatdata", "8140", "pfx", "drive_c",
                           "users", "steamuser", "Documents", "Eidos", "Tomb Raider - Underworld")
        os.makedirs(new); open(os.path.join(new, "save.dat"), "w").close()
        tru = next(g for g in self.service.list_games()["games"] if g["appid"] == 8140)
        self.assertTrue(tru["pathMissing"])
        self.assertEqual(tru["relocateTo"]["path"], new)
        self.service.relocate_game(8140)
        self.assertEqual(self.service.store.get_mapping(8140)["savePath"], new)
        self.assertEqual(self.client.folders["deck-8140"]["path"], new)

    def test_forget_removes_our_folder_but_never_an_adopted_one(self):
        self.service.list_games()
        os.makedirs(os.path.join(self.tmp.name, "s")); self.service.sync_game(8140, os.path.join(self.tmp.name, "s"))
        self.service.forget_game(8140)
        self.assertIn("deck-8140", self.client.deleted)
        self.assertIsNone(self.service.store.get_mapping(8140))
        self.service.forget_game(1449690)
        self.assertNotIn("gsi4n-zdnvt", self.client.deleted)
        self.assertIsNone(self.service.store.get_mapping(1449690))

    def test_sync_adds_folder_on_the_pc_with_the_windows_path(self):
        """With the PC's API configured, the folder is created there with the
        mapped path instead of waiting to be accepted."""
        prefix_saves = os.path.join(
            steam.steam_root(), "steamapps", "compatdata", "8140", "pfx", "drive_c",
            "users", "steamuser", "Documents", "Eidos", "Tomb Raider - Underworld",
        )
        os.makedirs(prefix_saves)
        pc = _FakeClient([])
        pc.my_device_id = lambda: "PC"
        self.service.remote_client = lambda: pc
        result = self.service.sync_game(8140, prefix_saves)
        self.assertEqual(result["remote"]["reason"], "created")
        self.assertEqual(pc.folders["deck-8140"]["path"], "~\\Documents\\Eidos\\Tomb Raider - Underworld")
        self.assertEqual([d["deviceID"] for d in pc.folders["deck-8140"]["devices"]], ["PC", "LOCAL"])
        # second sync: never overwrite what the PC already has
        self.assertEqual(self.service.sync_game(8140, prefix_saves)["remote"]["reason"], "exists")

    def test_sync_uses_default_pc_location_when_no_windows_equivalent(self):
        """Linux-native saves still get a folder on the PC, under ~\\SyncDeck."""
        xdg = os.path.join(self.tmp.name, ".config", "SomeGame")
        os.makedirs(xdg)
        pc = _FakeClient([])
        pc.my_device_id = lambda: "PC"
        self.service.remote_client = lambda: pc
        result = self.service.sync_game(8140, xdg)
        self.assertEqual(result["remote"]["how"], "default")
        self.assertEqual(pc.folders["deck-8140"]["path"], "~\\SyncDeck\\Tomb Raider")
        self.assertFalse(result["remote"]["pcExisting"])

    def test_sync_reports_when_the_pc_already_has_the_save_folder(self):
        prefix_saves = os.path.join(
            steam.steam_root(), "steamapps", "compatdata", "8140", "pfx", "drive_c",
            "users", "steamuser", "Documents", "Eidos", "Tomb Raider - Underworld",
        )
        os.makedirs(prefix_saves)
        pc = _FakeClient([])
        pc.my_device_id = lambda: "PC"
        pc.tree = {"C:\\Users\\infer\\Documents\\Eidos": ["Tomb Raider - Underworld"]}
        self.service.remote_client = lambda: pc
        result = self.service.sync_game(8140, prefix_saves)
        self.assertEqual(result["remote"]["how"], "mapped")
        self.assertTrue(result["remote"]["pcExisting"])

    def test_install_dir_saves_are_placed_in_the_pcs_steam_library(self):
        install_saves = os.path.join(steam.steam_root(), "steamapps", "common", "TRU", "save")
        os.makedirs(install_saves)
        pc = _FakeClient([])
        pc.my_device_id = lambda: "PC"
        pc.tree = {
            "": ["C:", "D:"],
            "D:\\SteamLibrary\\steamapps\\common": ["TRU", "Other"],
        }
        self.service.remote_client = lambda: pc
        result = self.service.sync_game(8140, install_saves)
        self.assertEqual(result["remote"]["how"], "install_found")
        self.assertEqual(pc.folders["deck-8140"]["path"], "D:\\SteamLibrary\\steamapps\\common\\TRU\\save")

    def test_install_dir_saves_fall_back_to_default_when_game_not_on_pc(self):
        install_saves = os.path.join(steam.steam_root(), "steamapps", "common", "TRU", "save")
        os.makedirs(install_saves)
        pc = _FakeClient([])
        pc.my_device_id = lambda: "PC"
        pc.tree = {"": ["C:"]}
        self.service.remote_client = lambda: pc
        self.assertEqual(self.service.sync_game(8140, install_saves)["remote"]["how"], "default")

    def test_sync_without_pc_configured_reports_it(self):
        os.makedirs(os.path.join(self.tmp.name, "s2"))
        self.assertEqual(self.service.sync_game(8140, os.path.join(self.tmp.name, "s2"))["remote"]["reason"], "not_configured")

    def test_unmapped_game_gets_inline_detection_and_played_flag(self):
        result = self.service.list_games()
        tru = next(g for g in result["games"] if g["appid"] == 8140)
        self.assertFalse(tru["synced"])
        self.assertIsNone(tru["detected"])
        self.assertTrue(tru["playedOnDeck"])


class KeyFileTests(unittest.TestCase):
    """Getting a 32 character key onto a Deck without the on-screen keyboard."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["DECKY_USER_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "DECKY_USER_HOME", None)
        make_steam_root(self.tmp.name, [(8140, "Tomb Raider", "TRU")])
        self.synced = os.path.join(self.tmp.name, "synced-saves")
        os.makedirs(self.synced)
        self.service = SyncDeckService(Store(os.path.join(self.tmp.name, "settings.json")))
        self.client = _FakeClient([{"id": "deck-8140", "path": self.synced, "devices": []}])
        self.client.conns = {"PC": {"connected": True, "address": "10.0.0.218:22000"}}
        self.service.client = lambda refresh=False: self.client
        self.service.remote_client = lambda: None

    def write_key_file(self, directory, text):
        path = os.path.join(directory, "syncdeck-key.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_parses_a_bare_key_json_and_key_value_lines(self):
        from syncdeck.service import _parse_key_file

        key = "abcdef0123456789abcdef0123456789"
        self.assertEqual(_parse_key_file(key), (None, key))
        self.assertEqual(_parse_key_file(f"# my key\nhttps://10.0.0.5:8384\n{key}\n"), ("https://10.0.0.5:8384", key))
        self.assertEqual(_parse_key_file(f"url=https://pc:8384\napikey={key}"), ("https://pc:8384", key))
        self.assertEqual(_parse_key_file(f'{{"baseUrl": "https://pc:8384", "apiKey": "{key}"}}'), ("https://pc:8384", key))
        self.assertEqual(_parse_key_file("nothing useful here")[1], None)

    def test_imports_from_a_synced_folder_and_deletes_the_file(self):
        key = "abcdef0123456789abcdef0123456789"
        path = self.write_key_file(self.synced, key)
        result = self.service.import_pc_key()
        self.assertTrue(result["imported"])
        self.assertTrue(result["fileRemoved"])
        self.assertTrue(result["fromSyncedFolder"], "must warn the user to delete it on the PC as well")
        self.assertFalse(os.path.exists(path), "a credential must not be left lying in a synced folder")
        self.assertEqual((self.service.store.get("remote") or {}).get("apiKey"), key)

    def test_import_falls_back_to_the_detected_address(self):
        self.write_key_file(self.synced, "abcdef0123456789abcdef0123456789")
        self.service.import_pc_key()
        self.assertEqual((self.service.store.get("remote") or {}).get("baseUrl"), "https://10.0.0.218:8384")

    def test_import_without_a_file_explains_where_to_put_one(self):
        with self.assertRaises(SyncDeckError) as caught:
            self.service.import_pc_key()
        self.assertIn("syncdeck-key.txt", str(caught.exception))

    def test_a_file_without_a_key_is_rejected_rather_than_saved(self):
        self.write_key_file(self.synced, "please put the key here")
        with self.assertRaises(SyncDeckError):
            self.service.import_pc_key()
        self.assertIsNone((self.service.store.get("remote") or {}).get("apiKey"))

    def test_detects_the_pc_address_from_the_live_connection(self):
        self.assertEqual(self.service.detect_pc_url(), "https://10.0.0.218:8384")


class DaemonTests(unittest.TestCase):
    def test_flatpak_config_path_yields_flatpak_command(self):
        cfg = "/home/deck/.var/app/com.github.zocker_160.SyncThingy/.local/state/syncthing/config.xml"
        self.assertEqual(daemon.flatpak_app_id(cfg), "com.github.zocker_160.SyncThingy")
        cmd = daemon.syncthing_command(cfg)
        self.assertEqual(cmd[1:4], ["run", "--command=syncthing", "com.github.zocker_160.SyncThingy"])
        self.assertIn("serve", cmd)

    def test_session_unit_is_derived_from_the_flatpak_id(self):
        cfg = "/home/deck/.var/app/com.github.zocker_160.SyncThingy/.local/state/syncthing/config.xml"
        self.assertEqual(daemon.session_unit(cfg), "app-com.github.zocker_160.SyncThingy@autostart.service")
        self.assertIsNone(daemon.session_unit("/home/deck/.config/syncthing/config.xml"))

    def test_spawn_env_drops_decky_library_path_and_forces_session_vars(self):
        """Regression: systemctl loaded Decky's bundled libcrypto via LD_LIBRARY_PATH."""
        saved = {k: os.environ.get(k) for k in ("LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG", "XDG_RUNTIME_DIR")}
        os.environ["LD_LIBRARY_PATH"] = "/tmp/_MEIxyz"
        os.environ["LD_LIBRARY_PATH_ORIG"] = "/opt/keep"
        os.environ["XDG_RUNTIME_DIR"] = "/run/user/0"
        try:
            env = daemon._env()
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/keep")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", env)
        self.assertEqual(env["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")
        self.assertTrue(env["DBUS_SESSION_BUS_ADDRESS"].endswith(f"/run/user/{os.getuid()}/bus"))

    def test_rejects_an_app_id_that_is_not_reverse_dns(self):
        """A directory name under ~/.var/app is untrusted: it reaches a unit file."""
        evil = "/home/deck/.var/app/com.evil\nExecStartPre=-/bin/touch x/.local/state/syncthing/config.xml"
        self.assertIsNone(daemon.flatpak_app_id(evil))
        self.assertIsNone(daemon.syncthing_command(evil))
        self.assertIsNone(daemon.flatpak_app_id("/home/deck/.var/app/../../etc/config.xml"))

    def test_unit_file_refuses_control_characters(self):
        with self.assertRaises(ValueError):
            daemon.unit_content(["/usr/bin/flatpak", "run", "com.evil\nExecStartPre=/bin/touch /tmp/pwned"])

    def test_unit_quoting_escapes_quotes_and_backslashes(self):
        line = [l for l in daemon.unit_content(['/opt/a b/sync"thing', "serve"]).splitlines() if l.startswith("ExecStart=")][0]
        self.assertEqual(line, 'ExecStart="/opt/a b/sync\\"thing" serve')

    def test_native_config_path_has_no_flatpak_id(self):
        self.assertIsNone(daemon.flatpak_app_id("/home/deck/.config/syncthing/config.xml"))
        self.assertIsNone(daemon.flatpak_app_id(None))

    def test_unit_runs_at_login_and_honours_syncthing_restart_codes(self):
        unit = daemon.unit_content(["/usr/bin/flatpak", "run", "--command=syncthing", "x", "serve", "--no-browser"])
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("ExecStart=/usr/bin/flatpak run --command=syncthing x serve --no-browser", unit)
        self.assertIn("RestartForceExitStatus=3 4", unit)

    def test_unit_quotes_paths_with_spaces(self):
        unit = daemon.unit_content(["/opt/my tools/syncthing", "serve"])
        self.assertIn('ExecStart="/opt/my tools/syncthing" serve', unit)


class VdfTests(unittest.TestCase):
    def test_parses_nested_blocks_and_comments(self):
        parsed = vdf.loads('"Root" { "a" "1" // note\n "Sub" { "b" "2" } }')
        self.assertEqual(parsed["Root"]["a"], "1")
        self.assertEqual(parsed["Root"]["Sub"]["b"], "2")

    def test_get_path_is_case_insensitive(self):
        parsed = vdf.loads('"AppState" { "Name" "Portal 2" }')
        self.assertEqual(vdf.get_path(parsed, "appstate", "name"), "Portal 2")

    def test_missing_key_returns_default(self):
        self.assertEqual(vdf.get_path({}, "a", "b", default="fallback"), "fallback")


class SteamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        make_steam_root(self.tmp.name, [(620, "Portal 2", "Portal 2"), (400, "Portal", "Portal")])
        os.environ["DECKY_USER_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "DECKY_USER_HOME", None)

    def test_finds_installed_games_sorted_by_name(self):
        games = steam.installed_games()
        self.assertEqual([g.name for g in games], ["Portal", "Portal 2"])

    def test_skips_games_that_are_not_fully_installed(self):
        steamapps = os.path.join(steam.steam_root(), "steamapps")
        with open(os.path.join(steamapps, "appmanifest_999.acf"), "w", encoding="utf-8") as handle:
            handle.write(APPMANIFEST.format(appid=999, name="Downloading", installdir="dl", flags=1026))
        self.assertNotIn(999, [g.appid for g in steam.installed_games()])

    def test_skips_compat_tools_by_toolmanifest_marker(self):
        """Proton and the Linux runtimes are excluded by marker, not appid.

        Regression test: a hardcoded appid list let Proton 10.0,
        Proton 11.0 and Steam Linux Runtime 4.0 into the game list on a real
        Deck, because they postdated the list.
        """
        steamapps = os.path.join(steam.steam_root(), "steamapps")
        for appid, name, installdir in (
            (4183110, "Steam Linux Runtime 4.0", "SteamLinuxRuntime_4"),
            (4628710, "Proton 11.0", "Proton 11.0"),
        ):
            tool_dir = os.path.join(steamapps, "common", installdir)
            os.makedirs(tool_dir, exist_ok=True)
            open(os.path.join(tool_dir, "toolmanifest.vdf"), "w").close()
            with open(os.path.join(steamapps, f"appmanifest_{appid}.acf"), "w", encoding="utf-8") as handle:
                handle.write(APPMANIFEST.format(appid=appid, name=name, installdir=installdir, flags=4))

        appids = [g.appid for g in steam.installed_games()]
        self.assertNotIn(4183110, appids)
        self.assertNotIn(4628710, appids)
        self.assertIn(620, appids)


class CompatDataTests(unittest.TestCase):
    """Regression tests for prefix lookup across libraries.

    On a real Deck, a game installed on an external drive had its Proton
    prefix in the *main* library. Deriving the prefix from the game's own
    library found nothing, which broke save-path detection for
    most Windows games.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["DECKY_USER_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "DECKY_USER_HOME", None)

        self.main = make_steam_root(self.tmp.name, [(620, "Portal 2", "Portal 2")])

        # A second library on "external storage" holding a different game.
        self.external = os.path.join(self.tmp.name, "run", "media", "deck", "1TB First")
        os.makedirs(os.path.join(self.external, "steamapps", "common", "Borderlands 2"))
        with open(
            os.path.join(self.external, "steamapps", "appmanifest_49520.acf"), "w", encoding="utf-8"
        ) as handle:
            handle.write(APPMANIFEST.format(appid=49520, name="Borderlands 2", installdir="Borderlands 2", flags=4))

        with open(
            os.path.join(self.main, "steamapps", "libraryfolders.vdf"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t\t"contentid"\t\t"111"\n\t}\n'
                '\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t\t"label"\t\t"1TB First"\n\t\t"contentid"\t\t"222"\n\t}\n}\n'
                % (self.main, self.external)
            )

    def test_finds_library_with_a_space_in_its_path(self):
        self.assertIn(self.external, steam.library_paths())
        self.assertIn(49520, [g.appid for g in steam.installed_games()])

    def test_prefix_resolves_to_a_different_library_than_the_install(self):
        prefix = os.path.join(self.main, "steamapps", "compatdata", "49520")
        os.makedirs(os.path.join(prefix, "pfx", "drive_c"))

        game = next(g for g in steam.installed_games() if g.appid == 49520)
        self.assertEqual(game.library_path, self.external)
        self.assertTrue(game.is_proton)
        self.assertEqual(game.prefix_path, os.path.join(prefix, "pfx", "drive_c"))

    def test_most_recently_used_prefix_wins_when_duplicated(self):
        """The Walking Dead: game on the SD card, live prefix on internal
        storage, a two-year-old copy left on the card. Newest wins."""
        import time as _time
        internal = os.path.join(self.main, "steamapps", "compatdata", "49520", "pfx")
        card = os.path.join(self.external, "steamapps", "compatdata", "49520", "pfx")
        os.makedirs(internal); os.makedirs(card)
        for prefix, age in ((internal, 0), (card, 60 * 60 * 24 * 400)):
            reg = os.path.join(prefix, "user.reg")
            open(reg, "w").close()
            os.utime(reg, (_time.time() - age, _time.time() - age))
        game = next(g for g in steam.installed_games() if g.appid == 49520)
        self.assertEqual(game.compat_path, os.path.dirname(internal))

    def test_libraries_carry_content_id_and_label_and_survive_unmount(self):
        libs = {l.path: l for l in steam.libraries(include_missing=True)}
        self.assertIn(self.external, libs)
        # Make the "card" disappear: the entry must still be known, just unmounted.
        import shutil
        shutil.rmtree(self.external)
        after = {l.path: l for l in steam.libraries(include_missing=True)}
        self.assertIn(self.external, after)
        self.assertFalse(after[self.external].mounted)
        self.assertNotIn(self.external, steam.library_paths())

    def test_duplicate_content_ids_prefer_the_labelled_or_mounted_entry(self):
        with open(os.path.join(self.main, "steamapps", "libraryfolders.vdf"), "a", encoding="utf-8") as handle:
            pass  # fixture already has ids 111/222; simulate a relabelled duplicate below
        vdf_path = os.path.join(self.main, "steamapps", "libraryfolders.vdf")
        text = open(vdf_path, encoding="utf-8").read().rstrip().rstrip("}")
        text += '\t"2"\n\t{\n\t\t"path"\t\t"/run/media/deck/old-uuid"\n\t\t"contentid"\t\t"222"\n\t}\n}\n'
        open(vdf_path, "w", encoding="utf-8").write(text)
        self.assertEqual(steam.library_by_content_id("222").label, "1TB First")

    def test_game_without_any_prefix_is_not_proton(self):
        game = next(g for g in steam.installed_games() if g.appid == 49520)
        self.assertFalse(game.is_proton)
        self.assertIsNone(game.prefix_path)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["DECKY_USER_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "DECKY_USER_HOME", None)

    def _write_config(self, relative: str) -> str:
        path = os.path.join(self.tmp.name, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(CONFIG_XML)
        return path

    def test_reads_api_key_and_rewrites_wildcard_address_to_loopback(self):
        self._write_config(".config/syncthing/config.xml")
        endpoint = discovery.discover()
        self.assertEqual(endpoint.api_key, "test-api-key")
        self.assertEqual(endpoint.base_url, "http://127.0.0.1:8384")

    def test_finds_flatpak_install(self):
        self._write_config(".var/app/com.github.zocker_160.SyncThingy/config/syncthing/config.xml")
        self.assertEqual(discovery.discover().api_key, "test-api-key")

    def test_raises_when_nothing_is_installed(self):
        with self.assertRaises(SyncthingNotFound):
            discovery.discover()

    def test_tls_flag_selects_https(self):
        path = self._write_config(".config/syncthing/config.xml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(CONFIG_XML.replace('tls="false"', 'tls="true"'))
        self.assertTrue(discovery.parse_config(path).base_url.startswith("https://"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")

    def test_mappings_round_trip(self):
        store = Store(self.path)
        store.put_mapping(620, name="Portal 2", savePath="/tmp", source="manual")
        self.assertEqual(Store(self.path).get_mapping(620)["savePath"], "/tmp")

    def test_unknown_keys_survive_and_defaults_are_filled_in(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"games": {}, "somethingNew": 1}, handle)
        store = Store(self.path)
        self.assertEqual(store.get("somethingNew"), 1)
        self.assertEqual(store.get("versioningDays"), 30)

    def test_corrupt_file_falls_back_to_defaults(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(Store(self.path).all_mappings(), {})


class SavePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["DECKY_USER_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "DECKY_USER_HOME", None)

    def test_rejects_home_directory(self):
        with self.assertRaises(SavePathError):
            saves.validate_path(self.tmp.name)

    def test_rejects_missing_directory(self):
        with self.assertRaises(SavePathError):
            saves.validate_path(os.path.join(self.tmp.name, "nope"))

    def test_accepts_and_normalizes_a_real_directory(self):
        target = os.path.join(self.tmp.name, "saves")
        os.makedirs(target)
        self.assertEqual(saves.validate_path(target + "/./"), os.path.realpath(target))

    def test_finds_save_folder_in_proton_prefix(self):
        root = make_steam_root(self.tmp.name, [(620, "Portal 2", "Portal 2")])
        prefix = os.path.join(root, "steamapps", "compatdata", "620", "pfx", "drive_c")
        target = os.path.join(prefix, "users", "steamuser", "Saved Games", "Portal 2")
        os.makedirs(target)
        open(os.path.join(target, "save1.sav"), "w").close()

        game = next(g for g in steam.installed_games() if g.appid == 620)
        found = saves.candidates(game)
        self.assertEqual(found[0].path, target)
        self.assertEqual(found[0].kind, "prefix")
        self.assertEqual(found[0].entry_count, 1)

    def test_name_matching_rejects_accidental_substrings(self):
        """Regression: KDE's ~/.local/share/ark matched 'LEGO Batman ... Dark Knight'."""
        self.assertFalse(saves._names_match("ark", "LEGO Batman: Legacy of the Dark Knight"))
        self.assertFalse(saves._names_match("gimp", "Ghostwire: Tokyo"))

    def test_name_matching_accepts_prefixes_and_substantial_hits(self):
        self.assertTrue(saves._names_match("Pal", "Palworld"))
        self.assertTrue(saves._names_match("Hades II", "HadesII"))
        self.assertTrue(saves._names_match("witcher3", "The Witcher 3"))
        self.assertTrue(saves._names_match("Outer Wilds", "outerwilds"))

    def test_finds_publisher_nested_save_folder(self):
        """AppData/LocalLow/<Publisher>/<Game> is how Unity titles lay out saves."""
        root = make_steam_root(self.tmp.name, [(753640, "Outer Wilds", "Outer Wilds")])
        target = os.path.join(
            root, "steamapps", "compatdata", "753640", "pfx", "drive_c",
            "users", "steamuser", "AppData", "LocalLow", "Mobius Digital", "Outer Wilds",
        )
        os.makedirs(target)
        open(os.path.join(target, "save.json"), "w").close()

        game = next(g for g in steam.installed_games() if g.appid == 753640)
        paths = [c.path for c in saves.candidates(game)]
        self.assertIn(target, paths)

    def test_finds_publisher_nested_folder_under_documents(self):
        """Documents/<Publisher>/<Game>: Tomb Raider: Underworld saves to Documents/Eidos/..."""
        root = make_steam_root(self.tmp.name, [(8140, "Tomb Raider: Underworld", "TRU")])
        target = os.path.join(
            root, "steamapps", "compatdata", "8140", "pfx", "drive_c",
            "users", "steamuser", "Documents", "Eidos", "Tomb Raider - Underworld",
        )
        os.makedirs(target)
        open(os.path.join(target, "save.dat"), "w").close()
        game = next(g for g in steam.installed_games() if g.appid == 8140)
        self.assertIn(target, [c.path for c in saves.candidates(game)])

    def test_prefix_parent_lookup_is_case_insensitive(self):
        """Proton prefixes are case-sensitive; games write 'My games' and 'My Games'."""
        root = make_steam_root(self.tmp.name, [(49520, "Borderlands 2", "Borderlands 2")])
        target = os.path.join(
            root, "steamapps", "compatdata", "49520", "pfx", "drive_c",
            "users", "steamuser", "Documents", "My games", "Borderlands 2",
        )
        os.makedirs(target)

        game = next(g for g in steam.installed_games() if g.appid == 49520)
        self.assertIn(target, [c.path for c in saves.candidates(game)])

    def test_pc_path_maps_prefix_profile_to_windows_home(self):
        mapped = saves.pc_path("/x/compatdata/8140/pfx/drive_c/users/steamuser/Documents/Eidos/Tomb Raider - Underworld")
        self.assertEqual(mapped["path"], "~\\Documents\\Eidos\\Tomb Raider - Underworld")
        self.assertEqual(mapped["kind"], "profile")
        self.assertEqual(saves.pc_path("/x/pfx/drive_c/ProgramData/Foo")["path"], "C:\\ProgramData\\Foo")
        self.assertIsNone(saves.pc_path("/home/deck/.config/StardewValley"))

    def test_pc_path_for_install_dir_saves_is_only_a_hint(self):
        root = make_steam_root(self.tmp.name, [(620, "Portal 2", "Portal 2")])
        game = next(g for g in steam.installed_games() if g.appid == 620)
        target = os.path.join(root, "steamapps", "common", "Portal 2", "save")
        os.makedirs(target)
        mapped = saves.pc_path(target, game)
        self.assertTrue(mapped["relative"])
        self.assertTrue(mapped["path"].endswith("common\\Portal 2\\save"))

    def test_refuses_paths_that_hold_credentials(self):
        """~/.ssh and Decky's own directory are never save folders. The latter
        holds SyncDeck's settings file, which may contain the PC's API key."""
        for name in (".ssh", ".ssh/keys", ".gnupg", "homebrew", "homebrew/settings/SyncDeck"):
            path = os.path.join(self.tmp.name, name)
            os.makedirs(path, exist_ok=True)
            with self.assertRaises(SavePathError, msg=name):
                saves.validate_path(path)

    def test_still_allows_a_save_folder_inside_a_steam_library(self):
        root = make_steam_root(self.tmp.name, [(620, "Portal 2", "Portal 2")])
        target = os.path.join(root, "steamapps", "compatdata", "620", "pfx", "drive_c", "saves")
        os.makedirs(target)
        self.assertEqual(saves.validate_path(target), os.path.realpath(target))

    def test_detects_flatpak_paths(self):
        self.assertTrue(saves.is_flatpak_path("/home/deck/.var/app/com.example.App/data"))
        self.assertFalse(saves.is_flatpak_path("/home/deck/.local/share/game"))


class FolderBuilderTests(unittest.TestCase):
    def test_includes_every_device_and_trashcan_versioning(self):
        folder = build_folder("deck-620", "Portal 2 (Deck saves)", "/tmp/saves", ["LOCAL", "PC"], 30)
        self.assertEqual([d["deviceID"] for d in folder["devices"]], ["LOCAL", "PC"])
        self.assertEqual(folder["versioning"]["type"], "trashcan")
        self.assertEqual(folder["versioning"]["params"]["cleanoutDays"], "30")
        self.assertTrue(folder["fsWatcherEnabled"])

    def test_versioning_can_be_disabled(self):
        self.assertNotIn("versioning", build_folder("deck-1", "x", "/tmp", ["A"], 0))


class AppInfoDepthTests(unittest.TestCase):
    def test_deeply_nested_block_is_reported_not_crashed(self):
        """A corrupt file must not take the whole game list down."""
        import struct as _struct

        blob = b""
        for _ in range(_MAX_NEST := 200):
            blob += bytes([0]) + _struct.pack("<I", 0)
        with self.assertRaises(appinfo.AppInfoError):
            appinfo._parse_kv(blob + bytes([8]) * _MAX_NEST, 0, ["k"])


class RetryTests(unittest.TestCase):
    """The daemon drops connections when its config reloads.

    Observed on a real Deck (Syncthing v2.1.3): creating a folder succeeds,
    then the next request fails with ECONNRESET. Without retries the user's
    first sync reports an error for an action that actually worked.
    """

    def setUp(self):
        self.client = SyncthingClient(
            SyncthingEndpoint(base_url="https://127.0.0.1:8384", api_key="k"), timeout=0.1
        )
        self._real_urlopen = syncthing_mod.urllib.request.urlopen
        self._real_backoff = syncthing_mod._RETRY_BACKOFF_SECONDS
        syncthing_mod._RETRY_BACKOFF_SECONDS = (0.0, 0.0, 0.0)
        self.addCleanup(setattr, syncthing_mod, "_RETRY_BACKOFF_SECONDS", self._real_backoff)
        self.addCleanup(setattr, syncthing_mod.urllib.request, "urlopen", self._real_urlopen)

    def _patch(self, side_effects):
        calls = {"n": 0}

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None, context=None):
            effect = side_effects[min(calls["n"], len(side_effects) - 1)]
            calls["n"] += 1
            if isinstance(effect, Exception):
                raise effect
            return FakeResponse(effect)

        syncthing_mod.urllib.request.urlopen = fake_urlopen
        return calls

    def test_recovers_from_a_connection_reset(self):
        calls = self._patch([ConnectionResetError(104, "Connection reset by peer"), b'{"ok": 1}'])
        self.assertEqual(self.client.get_config(), {"ok": 1})
        self.assertEqual(calls["n"], 2)

    def test_gives_up_after_the_backoff_is_exhausted(self):
        calls = self._patch([ConnectionResetError(104, "reset")])
        with self.assertRaises(SyncthingUnreachable):
            self.client.get_config()
        self.assertEqual(calls["n"], len(syncthing_mod._RETRY_BACKOFF_SECONDS) + 1)

    def test_http_errors_are_never_retried(self):
        error = syncthing_mod.urllib.error.HTTPError("u", 500, "boom", {}, None)
        calls = self._patch([error])
        with self.assertRaises(SyncthingApiError):
            self.client.get_config()
        self.assertEqual(calls["n"], 1, "a 500 means the daemon answered; retrying just doubles the write")

    def test_rejected_api_key_is_reported_as_auth_not_unreachable(self):
        error = syncthing_mod.urllib.error.HTTPError("u", 403, "forbidden", {}, None)
        self._patch([error])
        with self.assertRaises(SyncthingAuthError):
            self.client.get_config()


if __name__ == "__main__":
    unittest.main()
