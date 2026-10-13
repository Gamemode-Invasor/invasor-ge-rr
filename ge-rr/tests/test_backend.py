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


class InvalidArgument(ValueError):
    pass


class Unavailable(RuntimeError):
    pass


class FakeEvents:
    def __init__(self, cbs):
        self.cbs = cbs

    def on(self, name, cb):
        self.cbs.setdefault(name, []).append(cb)

    def off(self, name, cb):
        if cb in self.cbs.get(name, []):
            self.cbs[name].remove(cb)


class FakeTask:
    def __init__(self, label):
        self.label, self.updates, self.end = label, [], None

    def update(self, fraction, text=""):
        if not self.end:
            self.updates.append((fraction, text))

    def done(self, text=""):
        self.end = self.end or ("done", text)

    def fail(self, text=""):
        self.end = self.end or ("fail", text)


class FakeCtx:
    InvalidArgument = InvalidArgument
    Unavailable = Unavailable

    def __init__(self):
        self.log = logging.getLogger("test.ge_rr")
        self.cbs = {}
        self.game = types.SimpleNamespace(running=None)
        self.sounds = []
        self.tasks = []
        self.task_ok = True
        self.events = FakeEvents(self.cbs)
        self.notes = []
        self.notify_ok = True

    def task(self, label):
        if not self.task_ok:
            raise Unavailable("module unloaded")
        t = FakeTask(label)
        self.tasks.append(t)
        return t

    def notify(self, title, body="", icon="", timeout=15, sound=""):
        if not self.notify_ok:
            raise Unavailable("no Steam")
        self.notes.append((title, body))
        self.sounds.append(sound)


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
            mock.patch.object(g, "arch", return_value="x86_64"),
            mock.patch.object(g, "latest_release", return_value=dict(LATEST)),
            mock.patch.object(g, "check_space"),
            mock.patch.object(g, "download", side_effect=self._download),
            mock.patch.object(g, "verify", return_value="sha512"),
            mock.patch.object(g, "extract", side_effect=self._extract),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ctx = FakeCtx()
        self.b.setup(self.ctx)

    def tearDown(self):
        self.b.teardown()
        self.tmp.cleanup()

    def _download(self, url, dest, size, progress=None, cancel=None):
        Path(dest).write_bytes(b"tar")
        progress(100, 100)
        return "a", "b"

    def _extract(self, archive, dest, cancel=None):
        top = Path(dest) / "GE-Proton11-7-x86_64"
        top.mkdir(parents=True)
        (top / "version").write_text("1 GE-Proton11-7\n")
        return top

    def test_registers_steam_start(self):
        self.assertEqual(self.ctx.cbs, {"steam_start": [self.b.auto_update], "game_started": [self.b._game_started],
                                          "game_stopped": [self.b._game_stopped]})

    def test_auto_update_installs_and_notifies(self):
        with self.assertLogs("test.ge_rr", "INFO"):
            self.b.auto_update()
        s = self.b.status()
        self.assertEqual(s["phase"], "done")
        self.assertTrue(s["restart"])
        self.assertFalse(s["update_available"])
        self.assertEqual(s["installed"], {"tag": "GE-Proton11-7", "url": LATEST["url"]})
        self.assertEqual([t for t, _ in self.ctx.notes], ["Downloading GE-Proton", "GE-Proton installed"])
        self.assertIn("Restart Steam", self.ctx.notes[1][1])
        self.assertEqual([p.name for p in self.cache.iterdir()], ["lock"])  # no work folder left
        self.assertFalse(self.b._lock.locked())

    def test_download_shows_a_task(self):
        self.b.auto_update()
        (t,) = self.ctx.tasks
        self.assertEqual(t.label, "GE-Proton GE-Proton11-7")
        self.assertEqual(t.updates[0], (0, "Downloading"))
        self.assertIn((1.0, "Downloading 0/0 MB"), t.updates)
        self.assertEqual([x for _, x in t.updates][-3:], ["Verifying", "Extracting", "Installing"])
        self.assertEqual(t.end, ("done", "Installed. Restart Steam to load it"))

    def test_failed_download_fails_the_task(self):
        self.b.geproton.verify.side_effect = self.b.geproton.Error("The download is corrupt")
        with self.assertLogs("test.ge_rr", "INFO"):
            self.b.auto_update()
        self.assertEqual(self.ctx.tasks[0].end, ("fail", "The download is corrupt"))

    def test_cancelled_download_fails_the_task(self):
        self.b.geproton.download.side_effect = self.b.geproton.Cancelled("Cancelled")
        self.b.auto_update()
        self.assertEqual(self.ctx.tasks[0].end, ("fail", "Cancelled"))

    def test_up_to_date_shows_no_task(self):
        self.b.auto_update()
        self.ctx.tasks.clear()
        self.b.auto_update()
        self.assertEqual(self.ctx.tasks, [])

    def test_task_plan_b(self):
        self.ctx.task_ok = False
        with self.assertLogs("test.ge_rr", "INFO") as logs:
            self.b.auto_update()
        self.assertEqual(self.b.status()["phase"], "done")
        self.assertTrue(any("task not shown" in line for line in logs.output))

    def test_up_to_date_does_nothing(self):
        self.b.auto_update()
        self.ctx.notes.clear()
        self.b.geproton.download.reset_mock()
        self.b.auto_update()
        self.b.geproton.download.assert_not_called()
        self.assertEqual(self.ctx.notes, [])
        self.assertFalse(self.b.status()["restart"])  # a Steam start loads what's installed

    def test_check_reports_update(self):
        s = self.b.check()
        self.assertTrue(s["update_available"])
        self.assertEqual(s["latest"]["tag"], "GE-Proton11-7")
        self.b.geproton.latest_release.side_effect = self.b.geproton.NetworkError("offline")
        with self.assertRaisesRegex(Unavailable, "offline"):
            self.b.check()

    def _wait_job(self):
        for t in threading.enumerate():
            if t.name == "ge-rr-update":
                t.join(5)

    def test_download_and_reinstall(self):
        self.assertEqual(self.b.download(), {"started": True, "busy": False})
        self._wait_job()
        self.assertEqual(self.b.geproton.download.call_count, 1)
        self.b.download()  # already up to date: nothing downloaded
        self._wait_job()
        self.assertEqual(self.b.geproton.download.call_count, 1)
        self.b.reinstall()  # forced
        self._wait_job()
        self.assertEqual(self.b.geproton.download.call_count, 2)
        self.assertEqual(self.b.status()["phase"], "done")

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
        self.assertEqual((s["phase"], s["pending"]), ("done", False))

    def test_game_stopped_without_pending_does_nothing(self):
        self.b._game_stopped(self.GAME)
        self.b.geproton.download.assert_not_called()

    def test_game_blocks_the_buttons(self):
        self.ctx.game.running = self.GAME
        for call in (self.b.download, self.b.reinstall):
            self.assertEqual(call(), {"started": False, "busy": False, "game": True})
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
        self.assertEqual((s["phase"], s["pending"]), ("idle", True))
        self.assertEqual(self.ctx.tasks[0].end, ("fail", "Paused: a game is running"))
        self.assertFalse(self.b._lock.locked())
        self.b.geproton.download.side_effect = self._download
        self.ctx.game.running = None
        self.b._game_stopped(self.GAME)
        self.assertEqual(self.b.status()["phase"], "done")

    def test_busy(self):
        self.b._lock.acquire()
        try:
            self.assertEqual(self.b.reinstall(), {"started": False, "busy": True})
            self.b.auto_update()  # skipped
            self.b.geproton.latest_release.assert_not_called()
        finally:
            self.b._lock.release()

    def test_failure_after_start_notifies(self):
        self.b.geproton.verify.side_effect = self.b.geproton.Error("The download is corrupt")
        with self.assertLogs("test.ge_rr", "INFO"):
            self.b.auto_update()
        s = self.b.status()
        self.assertEqual((s["phase"], s["error"]), ("error", "The download is corrupt"))
        self.assertEqual(self.ctx.notes[-1], ("GE-Proton update failed", "The download is corrupt"))
        self.assertIsNone(s["installed"])
        self.assertEqual([p.name for p in self.cache.iterdir()], ["lock"])  # no work folder left

    def test_offline_at_boot_is_quiet(self):
        self.b.NET_TRIES, self.b.NET_WAIT = 2, 0
        self.b.geproton.latest_release.side_effect = self.b.geproton.NetworkError("offline")
        with self.assertLogs("test.ge_rr", "INFO"):
            self.b.auto_update()
        self.assertEqual(self.b.geproton.latest_release.call_count, 2)
        self.assertEqual(self.b.status()["phase"], "error")
        self.assertEqual(self.ctx.notes, [])

    def test_notification_plan_b(self):
        self.ctx.notify_ok = False
        with self.assertLogs("test.ge_rr", "INFO") as logs:
            self.b.auto_update()
        self.assertEqual(self.b.status()["phase"], "done")
        self.assertTrue(any("notification not shown" in line for line in logs.output))

    def test_waits_for_an_old_instance(self):
        """After a reload, the old instance's update may still hold the JobLock: the new one
        waits for it instead of downloading into the same place at the same time."""
        old = self.b.geproton.JobLock()
        old.acquire()
        threading.Timer(0.3, old.release).start()
        self.b.auto_update()
        self.assertEqual(self.b.status()["phase"], "done")
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


if __name__ == "__main__":
    unittest.main()
