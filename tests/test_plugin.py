"""Smoke test for main.py's Decky-facing layer.

Decky injects a `decky` module at runtime; a stub stands in for it here so
the Plugin class can be imported and its methods exercised end to end
(envelope, thread hop, error mapping) without Decky or Syncthing present.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "py_modules"))
sys.path.insert(0, ROOT)


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _install_decky_stub():
    stub = types.ModuleType("decky")
    stub.logger = _Logger()
    stub.emitted = []

    async def emit(event, *args):
        stub.emitted.append((event, args))

    stub.emit = emit
    sys.modules["decky"] = stub
    return stub


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["DECKY_USER_HOME"] = self.tmp.name
        os.environ["DECKY_PLUGIN_SETTINGS_DIR"] = os.path.join(self.tmp.name, "settings")
        self.addCleanup(os.environ.pop, "DECKY_USER_HOME", None)
        self.addCleanup(os.environ.pop, "DECKY_PLUGIN_SETTINGS_DIR", None)
        self.decky = _install_decky_stub()
        # No daemon here; do not pay the reload backoff on every call.
        from syncdeck import syncthing as syncthing_mod

        real = syncthing_mod._RETRY_BACKOFF_SECONDS
        syncthing_mod._RETRY_BACKOFF_SECONDS = ()
        self.addCleanup(setattr, syncthing_mod, "_RETRY_BACKOFF_SECONDS", real)
        sys.modules.pop("main", None)
        import main  # noqa: WPS433 -- imported here so the stub is in place

        self.main = main
        self.plugin = main.Plugin()

    def run_async(self, coro):
        async def wrapper():
            await self.plugin._main()
            try:
                return await coro
            finally:
                await self.plugin._unload()

        return asyncio.run(wrapper())

    def test_status_reports_missing_syncthing_as_data_not_exception(self):
        result = self.run_async(self.plugin.get_status())
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["connected"])
        self.assertEqual(result["data"]["error"]["code"], "syncthing_not_found")

    def test_list_games_with_no_steam_is_empty_not_an_error(self):
        result = self.run_async(self.plugin.list_games())
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["games"], [])
        self.assertEqual(result["data"]["total"], 0)

    def test_domain_errors_are_wrapped_in_the_envelope(self):
        result = self.run_async(self.plugin.suggest_paths(999999))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "error")
        self.assertIn("not installed", result["error"]["message"])

    def test_settings_never_expose_the_api_key(self):
        async def scenario():
            await self.plugin.set_syncthing_config("manual", "https://127.0.0.1:8384", "secret-key")
            return await self.plugin.get_settings()

        result = self.run_async(scenario())
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["syncthing"]["mode"], "manual")
        self.assertEqual(result["data"]["syncthing"]["apiKey"], "********")
        self.assertNotIn("secret-key", str(result))

    def test_skip_cloud_toggle_round_trips(self):
        async def scenario():
            await self.plugin.set_skip_cloud(False)
            return await self.plugin.get_settings()

        result = self.run_async(scenario())
        self.assertFalse(result["data"]["skipCloudSaves"])

    def test_unload_cancels_the_poll_task(self):
        async def scenario():
            await self.plugin._main()
            task = self.plugin._poll_task
            await self.plugin._unload()
            return task

        task = asyncio.run(scenario())
        self.assertTrue(task.cancelled() or task.done())
        self.assertIsNone(self.plugin._poll_task)


class LoggingPolicyTests(unittest.TestCase):
    """The plugin log is world-readable and gets attached to bug reports.

    Regression: set_remote_config was briefly in the list of methods whose
    arguments are logged, which wrote the PC's Syncthing API key to disk.
    """

    def setUp(self):
        _install_decky_stub()
        sys.modules.pop("main", None)
        import main

        self.main = main

    def test_no_credential_method_has_its_arguments_logged(self):
        self.assertTrue(self.main.CREDENTIAL_METHODS)
        self.assertEqual(self.main.LOGGABLE_ARGS & self.main.CREDENTIAL_METHODS, frozenset())

    def test_every_method_taking_a_secret_is_declared_a_credential_method(self):
        import inspect

        for name, method in inspect.getmembers(self.main.Plugin, inspect.isfunction):
            params = inspect.signature(method).parameters
            if any(p in params for p in ("api_key", "apiKey", "password", "token")):
                self.assertIn(name, self.main.CREDENTIAL_METHODS, f"{name} takes a secret")

    def test_setting_the_pc_key_does_not_write_it_to_the_log(self):
        records = []
        self.main.decky.logger.info = lambda *args, **kwargs: records.append(" ".join(str(a) for a in args))

        # Named exactly like the real method so it takes the same log path.
        def set_remote_config(base_url, api_key):
            return {"configured": True}

        async def scenario():
            await self.main._run(set_remote_config, "https://pc:8384", "SUPERSECRETKEY")

        asyncio.run(scenario())
        joined = " ".join(records)
        self.assertNotIn("SUPERSECRETKEY", joined)
        self.assertIn("set_remote_config", joined, "the call should still be logged, just without its arguments")


if __name__ == "__main__":
    unittest.main()
