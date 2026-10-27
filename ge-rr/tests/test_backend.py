import importlib.util
import logging
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
LATEST = {"tag": "GE-Proton11-7", "url": "https://x/GE-Proton11-7-x86_64.tar.gz", "size": 100,
          "sha512_url": "s", "sha256": "bb"}
CTAG = "cachyos-11.0-20261014-slr"
CLATEST = {"tag": CTAG, "url": f"https://x/proton-{CTAG}-x86_64_v3.tar.xz", "size": 200,
           "sha512_url": "s", "sha256": "cc"}


def load_backend():
    """Load backend.py as a package, as Invasor does (it uses relative imports)."""
    name = "ge_rr_backend_under_test"
    for n in [n for n in sys.modules if n == name or n.startswith(name + ".")]:
        del sys.modules[n]
    spec = importlib.util.spec_from_file_location(name, HERE / "backend.py", submodule_search_locations=[str(HERE)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


from invasor import archive, schema  # noqa: E402

InvalidArgument, Unavailable = schema.InvalidArgument, schema.Unavailable


class FakeEvents:
    def __init__(self, cbs):
        self.cbs = cbs

    def on(self, name, cb):
        self.cbs.setdefault(name, []).append(cb)

    def off(self, name, cb):
        if cb in self.cbs.get(name, []):
            self.cbs[name].remove(cb)


class FakeSettings:
    def __init__(self):
        self.values = {"ge_enabled": True, "cachyos_enabled": False}
        self.cbs = []

    def get(self, key=None):
        return dict(self.values) if key is None else self.values[key]

    def set(self, key, value, notify=False):
        self.values[key] = value
        if notify:  # as the core does when the user changes it from the panel
            for cb in self.cbs:
                cb(key, value)
        return value

    def on_change(self, cb):
        self.cbs.append(cb)


class FakeTask:
    """As the core's: it notifies when it starts (the label) and ends (the label and final text)."""

    def __init__(self, label, notes):
        self.label, self.updates, self.end, self.notes = label, [], None, notes
        notes.append((label, ""))

    def update(self, fraction, text=""):
        if not self.end:
            self.updates.append((fraction, text))

    def done(self, text=""):
        self._end("done", text)

    def fail(self, text=""):
        self._end("fail", text)

    def _end(self, how, text):
        if not self.end:
            self.end = (how, text)
            self.notes.append((self.label, text))


class FakeCtx:
    InvalidArgument = InvalidArgument
    Unavailable = Unavailable

    def __init__(self):
        self.log = logging.getLogger("test.ge_rr")
        self.cbs = {}
        self.game = types.SimpleNamespace(running=None)
        self.tasks = []
        self.task_ok = True
        self.events = FakeEvents(self.cbs)
        self.settings = FakeSettings()
        self.notes = []
        self.paths = types.SimpleNamespace(steam_root=lambda: Path("/steam"))
        self.archive = types.SimpleNamespace(extract=None, swap_dir=archive.swap_dir)  # extract: set by the test

    def task(self, label, notify=True, sound="toast"):
        if not self.task_ok:
            raise Unavailable("module unloaded")
        t = FakeTask(label, self.notes if notify else [])
        self.tasks.append(t)
        return t


class Backend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "compatibilitytools.d"
        self.cache = Path(self.tmp.name) / "cache"
        self.b = load_backend()
        g = self.b.geproton
        patches = [
            mock.patch.object(g, "compat_dir", return_value=self.root),
            mock.patch.object(g, "cache_dir", return_value=self.cache),
            mock.patch.object(g, "flavour", side_effect=lambda tool: "x86_64_v3" if tool.v3 else "x86_64"),
            mock.patch.object(g, "latest_release",
                              side_effect=lambda flavour, tool: dict(LATEST if tool is g.GE else CLATEST)),
            mock.patch.object(g, "check_space"),
            mock.patch.object(g, "download", side_effect=self._download),
            mock.patch.object(g, "verify", return_value="sha512"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ctx = FakeCtx()
        self.ctx.archive.extract = mock.Mock(side_effect=self._extract)
        self.b.setup(self.ctx)

    def tearDown(self):
        self.b.teardown()
        self.tmp.cleanup()

    def _download(self, url, dest, size, progress=None, cancel=None):
        Path(dest).write_bytes(b"tar")
        progress(100, 100)
        return "a", "b"

    def _extract(self, archive, dest, single_root=False, symlinks="refuse", cancel=None, max_bytes=None):
        """ctx.archive.extract of a downloaded tool (its archive is named after its folder)."""
        tag = LATEST["tag"] if Path(archive).name.startswith(self.b.geproton.GE.folder) else CTAG
        top = Path(dest) / tag
        top.mkdir(parents=True)
        (top / "version").write_text(f"1 {tag}\n")
        return top

    def test_registers_steam_start(self):
        self.assertEqual(self.ctx.cbs, {"steam_start": [self.b.auto_update], "game_started": [self.b._game_started],
                                          "game_stopped": [self.b._game_stopped]})
        self.assertEqual(self.ctx.settings.cbs, [self.b._changed])

    def test_auto_update_installs_and_notifies(self):
        with self.assertLogs("test.ge_rr", "INFO"):
            self.b.auto_update()
        s = self.b.status()
        self.assertEqual(s["tools"]["ge"]["phase"], "done")
        self.assertTrue(s["tools"]["ge"]["restart"])
        self.assertFalse(s["tools"]["ge"]["update_available"])
        self.assertEqual(s["tools"]["ge"]["installed"], {"tag": "GE-Proton11-7", "url": LATEST["url"]})
        self.assertEqual(self.ctx.notes, [("GE-Proton update: GE-Proton11-7 (0 MB)", ""), ("GE-Proton update: GE-Proton11-7 (0 MB)", "Installed. Restart Steam to load it.")])
        self.assertEqual([p.name for p in self.cache.iterdir()], ["lock"])  # no work folder left
        self.assertFalse(self.b._lock.locked())

    def test_download_shows_a_task(self):
        self.b.auto_update()
        (t,) = self.ctx.tasks
        self.assertEqual(t.label, "GE-Proton update: GE-Proton11-7 (0 MB)")
        self.assertEqual(t.updates[0], (0, "Downloading"))
        self.assertIn((1.0, "Downloading 0/0 MB"), t.updates)
        self.assertEqual([x for _, x in t.updates][-3:], ["Verifying", "Extracting", "Installing"])
        self.assertEqual(t.end, ("done", "Installed. Restart Steam to load it."))

    def test_failed_download_fails_the_task(self):
        self.b.geproton.verify.side_effect = self.b.geproton.Error("The download is corrupt")
        with self.assertLogs("test.ge_rr", "INFO"):
            self.b.auto_update()
        self.assertEqual(self.ctx.tasks[0].end, ("fail", "Update failed: The download is corrupt"))

    def test_cancelled_download_fails_the_task(self):
        self.b.geproton.download.side_effect = self.b.geproton.Cancelled("Cancelled")
        self.b.auto_update()
        self.assertEqual(self.ctx.tasks[0].end, ("fail", "Cancelled"))

    def test_up_to_date_shows_no_task(self):
        self.b.auto_update()
        self.ctx.tasks.clear()
        self.b.auto_update()
        self.assertEqual(self.ctx.tasks, [])

    def test_an_unexpected_failure_is_logged_with_its_traceback(self):
        self.b.geproton.check_space.side_effect = RuntimeError("bug")
        with self.assertLogs("test.ge_rr", "WARNING") as logs:
            self.b.auto_update()
        self.assertEqual(self.b.status()["tools"]["ge"]["phase"], "error")
        self.assertIn("Traceback", "\n".join(logs.output))
        self.b.geproton.check_space.side_effect = self.b.geproton.Error("Not enough free space")
        with self.assertLogs("test.ge_rr", "WARNING") as logs:
            self.b.auto_update()
        self.assertNotIn("Traceback", "\n".join(logs.output))
        self.assertEqual(self.b.status()["tools"]["ge"]["error"], "Not enough free space")

    def test_no_task_is_a_failed_update(self):
        self.ctx.task_ok = False
        with self.assertLogs("test.ge_rr", "WARNING"):
            self.b.auto_update()
        self.assertEqual(self.b.status()["tools"]["ge"]["phase"], "error")
        self.assertIsNone(self.b.status()["tools"]["ge"]["installed"])

    def test_extraction_uses_the_core_with_links_inside_and_a_size_limit(self):
        self.b.auto_update()
        kw = self.ctx.archive.extract.call_args.kwargs
        self.assertEqual((kw["single_root"], kw["symlinks"]), (True, "inside"))
        self.assertEqual(kw["max_bytes"], LATEST["size"] * self.b.geproton.GE.space_factor)
        self.assertIsNotNone(kw["cancel"])

    def test_a_bad_archive_is_an_error_and_a_cancelled_extraction_a_pause(self):
        self.ctx.archive.extract.side_effect = InvalidArgument("unsafe path ../x")
        with self.assertLogs("test.ge_rr", "WARNING"):
            self.b.auto_update()
        ge = self.b.status()["tools"]["ge"]
        self.assertEqual((ge["phase"], ge["installed"]), ("error", None))
        self.assertIn("Can't extract GE-Proton (unsafe path ../x)", ge["error"])

        def cancelled(*args, cancel=None, **kw):
            cancel.set()  # a game started meanwhile
            raise Unavailable("cancelled")

        self.ctx.archive.extract.side_effect = cancelled
        self.b.auto_update()
        self.assertEqual(self.b.status()["tools"]["ge"]["phase"], "idle")
        self.assertEqual(self.ctx.tasks[-1].end, ("fail", "Cancelled"))

    def test_steam_folder_not_found(self):
        self.ctx.paths.steam_root = mock.Mock(side_effect=Unavailable("Steam's folder wasn't found"))
        with self.assertLogs("test.ge_rr", "WARNING"):
            self.b.auto_update()
        ge = self.b.status()["tools"]["ge"]  # the tab still shows the tools, with the error
        self.assertEqual((ge["phase"], ge["installed"], ge["update_available"]), ("error", None, False))
        self.assertIn("Steam's folder wasn't found", ge["error"])
        self.assertEqual(self.ctx.tasks, [])

    def test_no_size_from_github_leaves_the_cores_byte_limit(self):
        self.b.geproton.latest_release.side_effect = lambda flavour, tool: dict(LATEST, size=0)
        self.b.auto_update()
        self.assertNotIn("max_bytes", self.ctx.archive.extract.call_args.kwargs)
        self.assertEqual(self.b.status()["tools"]["ge"]["phase"], "done")

    def test_up_to_date_does_nothing(self):
        self.b.auto_update()
        self.ctx.notes.clear()
        self.b.geproton.download.reset_mock()
        self.b.auto_update()
        self.b.geproton.download.assert_not_called()
        self.assertEqual(self.ctx.notes, [])
        self.assertFalse(self.b.status()["tools"]["ge"]["restart"])  # a Steam start loads what's installed

    def test_check_reports_update(self):
        s = self.b.check("ge")
        self.assertTrue(s["tools"]["ge"]["update_available"])
        self.assertEqual(s["tools"]["ge"]["latest"]["tag"], "GE-Proton11-7")
        self.b.geproton.latest_release.side_effect = self.b.geproton.NetworkError("offline")
        with self.assertRaisesRegex(Unavailable, "offline"):
            self.b.check("ge")

    def _wait_job(self):
        for t in threading.enumerate():
            if t.name == "ge-rr-update":
                t.join(5)

    def test_download_and_reinstall(self):
        self.assertEqual(self.b.download("ge"), {"started": True, "busy": False})
        self._wait_job()
        self.assertEqual(self.b.geproton.download.call_count, 1)
        self.b.download("ge")  # already up to date: nothing downloaded
        self._wait_job()
        self.assertEqual(self.b.geproton.download.call_count, 1)
        self.b.reinstall("ge")  # forced
        self._wait_job()
        self.assertEqual(self.b.geproton.download.call_count, 2)
        self.assertEqual(self.b.status()["tools"]["ge"]["phase"], "done")

    GAME = {"appid": "1", "name": "G", "shortcut": False}

    def test_game_defers_auto_update_until_it_stops(self):
        self.ctx.game.running = self.GAME
        self.b.auto_update()
        self.b.geproton.download.assert_not_called()
        s = self.b.status()
        self.assertTrue(s["pending"] and s["game_running"])
        self.ctx.game.running = None
        self.b._game_stopped(self.GAME)
        self.assertEqual(self.b.geproton.download.call_count, 1)
        s = self.b.status()
        self.assertEqual((s["tools"]["ge"]["phase"], s["pending"]), ("done", False))

    def test_game_stopped_without_pending_does_nothing(self):
        self.b._game_stopped(self.GAME)
        self.b.geproton.download.assert_not_called()

    def test_game_blocks_the_buttons(self):
        self.ctx.game.running = self.GAME
        for call in (self.b.download, self.b.reinstall):
            self.assertEqual(call("ge"), {"started": False, "busy": False, "game": True})
        self.b.geproton.download.assert_not_called()
        self.assertFalse(self.b._lock.locked())

    def test_game_start_pauses_auto_download(self):
        def download(url, dest, size, progress=None, cancel=None):
            self.ctx.game.running = self.GAME
            self.b._game_started(self.GAME)
            self.assertTrue(cancel.is_set())
            raise self.b.geproton.Cancelled("Cancelled")

        self.b.geproton.download.side_effect = download
        self.b.auto_update()
        s = self.b.status()
        self.assertEqual((s["tools"]["ge"]["phase"], s["pending"]), ("idle", True))
        self.assertEqual(self.ctx.tasks[0].end, ("fail", "Paused: a game is running"))
        self.assertFalse(self.b._lock.locked())
        self.b.geproton.download.side_effect = self._download
        self.ctx.game.running = None
        self.b._game_stopped(self.GAME)
        self.assertEqual(self.b.status()["tools"]["ge"]["phase"], "done")

    def test_busy(self):
        self.b._lock.acquire()
        try:
            self.assertEqual(self.b.reinstall("ge"), {"started": False, "busy": True})
            self.b.auto_update()  # skipped
            self.b.geproton.latest_release.assert_not_called()
        finally:
            self.b._lock.release()

    def test_failure_after_start_notifies(self):
        self.b.geproton.verify.side_effect = self.b.geproton.Error("The download is corrupt")
        with self.assertLogs("test.ge_rr", "INFO"):
            self.b.auto_update()
        s = self.b.status()
        self.assertEqual((s["tools"]["ge"]["phase"], s["tools"]["ge"]["error"]), ("error", "The download is corrupt"))
        self.assertEqual(self.ctx.notes[-1], ("GE-Proton update: GE-Proton11-7 (0 MB)", "Update failed: The download is corrupt"))
        self.assertIsNone(s["tools"]["ge"]["installed"])
        self.assertEqual([p.name for p in self.cache.iterdir()], ["lock"])  # no work folder left

    def test_offline_at_boot_is_quiet(self):
        self.b.NET_TRIES, self.b.NET_WAIT = 2, 0
        self.b.geproton.latest_release.side_effect = self.b.geproton.NetworkError("offline")
        with self.assertLogs("test.ge_rr", "INFO"):
            self.b.auto_update()
        self.assertEqual(self.b.geproton.latest_release.call_count, 2)
        self.assertEqual(self.b.status()["tools"]["ge"]["phase"], "error")
        self.assertEqual(self.ctx.notes, [])

    def test_waits_for_an_old_instance(self):
        """After a reload, the old instance's update may still hold the JobLock: the new one
        waits for it instead of downloading into the same place at the same time."""
        old = self.b.geproton.JobLock()
        old.acquire()
        threading.Timer(0.3, old.release).start()
        self.b.auto_update()
        self.assertEqual(self.b.status()["tools"]["ge"]["phase"], "done")
        self.assertEqual(self.b.geproton.download.call_count, 1)

    def test_old_instance_busy_too_long(self):
        self.b.LOCK_WAIT = 0
        old = self.b.geproton.JobLock()
        old.acquire()
        try:
            with self.assertLogs("test.ge_rr", "INFO") as logs:
                self.b.auto_update()
        finally:
            old.release()
        self.b.geproton.latest_release.assert_not_called()
        self.assertIn("still running", logs.output[0])
        self.assertFalse(self.b._lock.locked())

    def test_leaves_another_updates_folder_alone(self):
        other = self.b.geproton.workdir(self.cache)  # e.g. created by a newer instance
        self.b.geproton.download.side_effect = self.b.geproton.Cancelled("Cancelled")
        self.b.LOCK_WAIT = 0
        held = self.b.geproton.JobLock()  # the owner of `other` holds the lock: nothing cleaned
        held.acquire()
        try:
            with self.assertLogs("test.ge_rr", "WARNING"):
                self.b.auto_update()
        finally:
            held.release()
        self.assertTrue(other.exists())

    def test_uninstall_waits_for_a_running_update(self):
        self.b.auto_update()
        self.b.UNINSTALL_WAIT = 0
        held = self.b.geproton.JobLock()
        held.acquire()
        try:
            with self.assertLogs("test.ge_rr", "WARNING"):
                self.b.uninstall(self.ctx, purge=True)
        finally:
            held.release()
        self.assertTrue((self.root / "GE-Proton").is_dir())

    def test_uninstall(self):
        self.b.auto_update()
        self.b.uninstall(self.ctx, purge=False)
        self.assertTrue((self.root / "GE-Proton").is_dir())
        self.b.uninstall(self.ctx, purge=True)
        self.assertFalse((self.root / "GE-Proton").exists())
        self.assertFalse(self.cache.exists())

    def test_uninstall_of_a_disabled_module_uses_its_own_context(self):
        self.b.auto_update()
        self.b.teardown()
        self.b.ctx = None  # disabled: uninstall() runs without setup()
        self.b.uninstall(self.ctx, purge=True)
        self.assertFalse((self.root / "GE-Proton").exists())

    def test_uninstall_without_steam_folder_removes_nothing(self):
        self.b.auto_update()
        self.ctx.paths.steam_root = mock.Mock(side_effect=Unavailable("Steam's folder wasn't found"))
        with self.assertLogs("test.ge_rr", "WARNING"):
            self.b.uninstall(self.ctx, purge=True)
        self.assertTrue((self.root / "GE-Proton").is_dir())
        self.assertFalse(self.cache.exists())  # the cache goes anyway


    # --- Proton-CachyOS, and the Enabled switches

    def _tools_downloaded(self):
        return [c.args[0] for c in self.b.geproton.download.call_args_list]

    def test_only_enabled_tools_are_updated(self):
        self.b.auto_update()
        self.assertEqual(self._tools_downloaded(), [LATEST["url"]])
        s = self.b.status()["tools"]
        self.assertEqual((s["ge"]["enabled"], s["cachyos"]["enabled"]), (True, False))
        self.assertEqual((s["ge"]["flavour"], s["cachyos"]["flavour"]), ("x86_64", "x86_64_v3"))
        self.assertEqual(s["cachyos"]["phase"], "idle")
        self.assertIsNone(s["cachyos"]["installed"])

    def test_both_one_after_the_other(self):
        self.ctx.settings.values["cachyos_enabled"] = True
        self.b.auto_update()
        self.assertEqual(self._tools_downloaded(), [LATEST["url"], CLATEST["url"]])
        self.assertEqual([t for t, _ in self.ctx.notes], ["GE-Proton update: GE-Proton11-7 (0 MB)"] * 2 + ["Proton-CachyOS update: cachyos-11.0-20261014-slr (0 MB)"] * 2)
        self.assertEqual([t.label for t in self.ctx.tasks], ["GE-Proton update: GE-Proton11-7 (0 MB)", "Proton-CachyOS update: cachyos-11.0-20261014-slr (0 MB)"])
        vdf = (self.root / "proton-cachyos" / "compatibilitytool.vdf").read_text()
        self.assertIn('"display_name" "Proton-CachyOS"', vdf)
        s = self.b.status()["tools"]
        self.assertEqual(s["cachyos"]["installed"], {"tag": CTAG, "url": CLATEST["url"]})
        self.assertTrue(s["ge"]["restart"] and s["cachyos"]["restart"])
        self.assertEqual([p.name for p in self.cache.iterdir()], ["lock"])

    def test_only_cachyos(self):
        self.ctx.settings.values.update(ge_enabled=False, cachyos_enabled=True)
        self.b.auto_update()
        self.assertEqual(self._tools_downloaded(), [CLATEST["url"]])
        self.assertFalse((self.root / "GE-Proton").exists())

    def test_none_enabled_does_nothing(self):
        self.ctx.settings.values["ge_enabled"] = False
        self.b.auto_update()
        self.b.geproton.latest_release.assert_not_called()
        self.assertFalse(self.b._lock.locked())

    def test_a_failed_tool_doesnt_stop_the_other(self):
        self.ctx.settings.values["cachyos_enabled"] = True
        g = self.b.geproton

        def verify(latest, sha512, sha256):
            if latest["url"] == LATEST["url"]:
                raise g.Error("corrupt")
            return "sha512"

        g.verify.side_effect = verify
        with self.assertLogs("test.ge_rr", "INFO"):
            self.b.auto_update()
        s = self.b.status()["tools"]
        self.assertEqual((s["ge"]["phase"], s["ge"]["error"]), ("error", "corrupt"))
        self.assertEqual(s["cachyos"]["phase"], "done")
        self.assertIn(("GE-Proton update: GE-Proton11-7 (0 MB)", "Update failed: corrupt"), self.ctx.notes)

    def test_a_game_pauses_the_rest_too(self):
        self.ctx.settings.values["cachyos_enabled"] = True

        def download(url, dest, size, progress=None, cancel=None):
            self.ctx.game.running = self.GAME
            self.b._game_started(self.GAME)
            raise self.b.geproton.Cancelled("Cancelled")

        self.b.geproton.download.side_effect = download
        self.b.auto_update()
        self.assertEqual(self.b.geproton.latest_release.call_count, 1)  # Proton-CachyOS wasn't even checked
        self.b.geproton.download.side_effect = self._download
        self.ctx.game.running = None
        self.b._game_stopped(self.GAME)
        s = self.b.status()["tools"]
        self.assertEqual((s["ge"]["phase"], s["cachyos"]["phase"]), ("done", "done"))

    def test_cachyos_installed_by_hand_is_adopted(self):
        tool = self.root / "proton-cachyos"
        tool.mkdir(parents=True)
        (tool / "version").write_text(f"1792277004 {CTAG}\n")
        self.ctx.settings.values["cachyos_enabled"] = True
        self.b.auto_update()
        self.assertEqual(self._tools_downloaded(), [LATEST["url"]])  # only GE-Proton
        self.assertFalse(self.b.status()["tools"]["cachyos"]["update_available"])

    def _wait_threads(self):
        for t in threading.enumerate():
            if t.name == "ge-rr-update":
                t.join(5)

    def test_enabling_checks_right_away(self):
        self.ctx.settings.set("cachyos_enabled", True, notify=True)
        self._wait_threads()
        self.assertEqual(self._tools_downloaded(), [CLATEST["url"]])  # only the one enabled
        self.ctx.settings.set("cachyos_enabled", False, notify=True)
        self.ctx.settings.set("ge_enabled", False, notify=True)
        self._wait_threads()
        self.assertEqual(self.b.geproton.download.call_count, 1)
        self.assertTrue((self.root / "proton-cachyos").is_dir())  # disabling deletes nothing

    def test_enabling_during_a_game_waits(self):
        self.ctx.game.running = self.GAME
        self.ctx.settings.set("cachyos_enabled", True, notify=True)
        self._wait_threads()
        self.b.geproton.download.assert_not_called()
        self.assertTrue(self.b.status()["pending"])

    def test_methods_need_a_known_enabled_tool(self):
        for call in (self.b.check, self.b.download, self.b.reinstall):
            with self.assertRaisesRegex(InvalidArgument, "Proton-CachyOS is disabled"):
                call("cachyos")
            with self.assertRaisesRegex(InvalidArgument, "unknown tool"):
                call("wine")
        self.b.geproton.latest_release.assert_not_called()
        self.ctx.settings.values["cachyos_enabled"] = True
        s = self.b.check("cachyos")
        self.assertEqual(s["tools"]["cachyos"]["latest"]["tag"], CTAG)
        self.assertIsNone(s["tools"]["ge"]["latest"])
        self.assertEqual(self.b.reinstall("cachyos"), {"started": True, "busy": False})
        self._wait_threads()
        self.assertEqual(self._tools_downloaded(), [CLATEST["url"]])

    def test_purge_keeps_a_cachyos_installed_by_hand(self):
        self.b.auto_update()
        tool = self.root / "proton-cachyos"
        tool.mkdir()
        (tool / "version").write_text(f"1 {CTAG}\n")
        self.b.uninstall(self.ctx, purge=True)
        self.assertEqual([p.name for p in self.root.iterdir()], ["proton-cachyos"])

    def test_purge_removes_the_cachyos_it_installed(self):
        self.ctx.settings.values["cachyos_enabled"] = True
        self.b.auto_update()
        self.b.uninstall(self.ctx, purge=True)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
