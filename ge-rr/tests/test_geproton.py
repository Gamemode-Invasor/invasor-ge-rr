import hashlib
import io
import json
import tarfile
import tempfile
import threading
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

import geproton

BASE = "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/GE-Proton11-7/"


def asset(name, size=10, digest=None):
    a = {"name": name, "browser_download_url": BASE + name, "size": size}
    if digest:
        a["digest"] = digest
    return a


RELEASE = {
    "tag_name": "GE-Proton11-7",
    "assets": [
        asset("GE-Proton11-7-aarch64.sha512sum", 159),
        asset("GE-Proton11-7-aarch64.tar.gz", 645, "sha256:aa"),
        asset("GE-Proton11-7-x86_64.sha512sum", 158),
        asset("GE-Proton11-7-x86_64.tar.gz", 563, "sha256:bb"),
    ],
}


def make_tar(path, files, top="GE-Proton11-7-x86_64"):
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


class Arch(unittest.TestCase):
    def test_known_machines(self):
        self.assertEqual(geproton.arch("x86_64"), "x86_64")
        self.assertEqual(geproton.arch("AMD64"), "x86_64")
        self.assertEqual(geproton.arch("aarch64"), "aarch64")
        self.assertEqual(geproton.arch("arm64"), "aarch64")

    def test_unsupported(self):
        with self.assertRaisesRegex(geproton.Error, "riscv64"):
            geproton.arch("riscv64")


class PickAsset(unittest.TestCase):
    def test_x86_64(self):
        r = geproton.pick_asset(RELEASE, "x86_64")
        self.assertEqual(r, {"tag": "GE-Proton11-7", "url": BASE + "GE-Proton11-7-x86_64.tar.gz", "size": 563,
                             "sha512_url": BASE + "GE-Proton11-7-x86_64.sha512sum", "sha256": "bb"})

    def test_aarch64(self):
        r = geproton.pick_asset(RELEASE, "aarch64")
        self.assertEqual(r["url"], BASE + "GE-Proton11-7-aarch64.tar.gz")
        self.assertEqual(r["sha512_url"], BASE + "GE-Proton11-7-aarch64.sha512sum")

    def test_old_release_without_suffix(self):
        old = {"tag_name": "GE-Proton9-1", "assets": [asset("GE-Proton9-1.sha512sum"), asset("GE-Proton9-1.tar.gz")]}
        r = geproton.pick_asset(old, "x86_64")
        self.assertEqual(r["url"], BASE + "GE-Proton9-1.tar.gz")
        self.assertEqual(r["sha512_url"], BASE + "GE-Proton9-1.sha512sum")
        self.assertIsNone(r["sha256"])
        with self.assertRaisesRegex(geproton.Error, "aarch64"):
            geproton.pick_asset(old, "aarch64")

    def test_latest_release_reads_the_api(self):
        r = geproton.latest_release("x86_64", get=lambda url: json.dumps(RELEASE).encode())
        self.assertEqual(r["tag"], "GE-Proton11-7")
        with self.assertRaises(geproton.NetworkError):
            geproton.latest_release("x86_64", get=lambda url: b"<html>")


class Http(unittest.TestCase):
    def _http_error(self, code, remaining):
        headers = Message()
        if remaining is not None:
            headers["X-RateLimit-Remaining"] = remaining
        return urllib.error.HTTPError(geproton.API_URL, code, "x", headers, None)

    def test_rate_limit(self):
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(403, "0")):
            with self.assertRaisesRegex(geproton.Error, "rate limit") as cm:
                geproton.http_get(geproton.API_URL)
        self.assertNotIsInstance(cm.exception, geproton.NetworkError)  # not worth retrying

    def test_other_errors_are_network_errors(self):
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(502, None)):
            with self.assertRaises(geproton.NetworkError):
                geproton.http_get(geproton.API_URL)
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaisesRegex(geproton.NetworkError, "down"):
                geproton.http_get(geproton.API_URL)


class Installed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_none(self):
        self.assertIsNone(geproton.installed(self.root))

    def test_script_markers(self):
        tool = self.root / "GE-Proton"
        tool.mkdir()
        (tool / "url_downloaded").write_text(BASE + "GE-Proton11-7-x86_64.tar.gz\n")
        (tool / "version").write_text("1789520217 GE-Proton11-7\n")
        (tool / "version-GE-Proton11-7-x86_64").touch()
        cur = geproton.installed(self.root)
        self.assertEqual(cur, {"tag": "GE-Proton11-7", "url": BASE + "GE-Proton11-7-x86_64.tar.gz"})
        latest = geproton.pick_asset(RELEASE, "x86_64")
        self.assertFalse(geproton.needs_update(cur, latest))
        self.assertTrue(geproton.needs_update(cur, geproton.pick_asset(RELEASE, "aarch64")))
        self.assertTrue(geproton.needs_update(None, latest))

    def test_marker_fallback(self):
        tool = self.root / "GE-Proton"
        tool.mkdir()
        (tool / "version-GE-Proton10-3").touch()
        self.assertEqual(geproton.installed(self.root), {"tag": "GE-Proton10-3", "url": None})


class Download(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "f.tar.gz"

    def tearDown(self):
        self.tmp.cleanup()

    def _response(self, data, length=None):
        r = mock.MagicMock()
        r.__enter__.return_value = r
        r.headers = {"Content-Length": str(len(data) if length is None else length)}
        r.read.side_effect = io.BytesIO(data).read
        return r

    def test_hashes_and_progress(self):
        data = b"x" * (geproton.CHUNK + 5)
        seen = []
        with mock.patch("urllib.request.urlopen", return_value=self._response(data)):
            h = geproton.download("https://x/u", self.dest, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(h, (hashlib.sha512(data).hexdigest(), hashlib.sha256(data).hexdigest()))
        self.assertEqual(self.dest.read_bytes(), data)
        self.assertEqual(seen[-1], (len(data), len(data)))

    def test_incomplete(self):
        with mock.patch("urllib.request.urlopen", return_value=self._response(b"abc", 10)):
            with self.assertRaisesRegex(geproton.NetworkError, "incomplete"):
                geproton.download("https://x/u", self.dest)

    def test_cancel(self):
        stop = threading.Event()
        stop.set()
        with mock.patch("urllib.request.urlopen", return_value=self._response(b"abc")):
            with self.assertRaises(geproton.Cancelled):
                geproton.download("https://x/u", self.dest, cancel=stop)


class Verify(unittest.TestCase):
    REL = {"sha512_url": "s", "sha256": "bb"}

    def test_sha512(self):
        self.assertEqual(geproton.verify(self.REL, "aa", "x", get=lambda u: b"AA  file.tar.gz\n"), "sha512")
        with self.assertRaisesRegex(geproton.Error, "sha512"):
            geproton.verify(self.REL, "ab", "bb", get=lambda u: b"aa  file.tar.gz\n")

    def test_falls_back_to_digest(self):
        def down(url):
            raise geproton.NetworkError("down")
        self.assertEqual(geproton.verify(self.REL, "x", "bb", get=down), "sha256")
        with self.assertRaisesRegex(geproton.Error, "sha256"):
            geproton.verify(self.REL, "x", "cc", get=down)

    def test_never_unverified(self):
        with self.assertRaisesRegex(geproton.Error, "verify"):
            geproton.verify({"sha512_url": None, "sha256": None}, "a", "b")


class ExtractInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "compatibilitytools.d"
        self.release = geproton.pick_asset(RELEASE, "x86_64")

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_and_install(self):
        tar = self.base / "g.tar.gz"
        make_tar(tar, {"proton": b"#!/bin/sh\n", "version": b"1 GE-Proton11-7\n"})
        top = geproton.extract(tar, self.base / "x")
        self.assertEqual(top.name, "GE-Proton11-7-x86_64")
        old = self.root / "GE-Proton"
        old.mkdir(parents=True)
        (old / "stale").touch()
        geproton.install(top, self.root, self.release)
        tool = self.root / "GE-Proton"
        self.assertFalse((tool / "stale").exists())
        self.assertIn('"display_name" "GE-Proton"', (tool / "compatibilitytool.vdf").read_text())
        self.assertTrue((tool / "version-GE-Proton11-7-x86_64").exists())
        self.assertEqual(geproton.installed(self.root), {"tag": "GE-Proton11-7", "url": self.release["url"]})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["GE-Proton"])

    def test_refuses_unsafe_archive(self):
        tar = self.base / "bad.tar.gz"
        make_tar(tar, {"../evil": b"x"}, top=None)
        with self.assertRaises(geproton.Error):
            geproton.extract(tar, self.base / "x")
        self.assertFalse((self.base / "evil").exists())

    def test_refuses_several_folders(self):
        tar = self.base / "two.tar.gz"
        make_tar(tar, {"a/f": b"1", "b/f": b"2"}, top=None)
        with self.assertRaisesRegex(geproton.Error, "single folder"):
            geproton.extract(tar, self.base / "x")

    def test_failed_install_restores_the_old_one(self):
        new = self.base / "new"
        new.mkdir()
        old = self.root / "GE-Proton"
        old.mkdir(parents=True)
        (old / "keep").touch()
        real_rename = Path.rename

        def rename(self_, target):
            if self_ == new:
                raise OSError("disk on fire")
            return real_rename(self_, target)

        with mock.patch.object(Path, "rename", rename):
            with self.assertRaisesRegex(geproton.Error, "disk on fire"):
                geproton.install(new, self.root, self.release)
        self.assertTrue((old / "keep").exists())
        self.assertFalse((self.root / ".GE-Proton.old").exists())

    def test_check_space(self):
        free = mock.Mock(free=100)
        with mock.patch("shutil.disk_usage", return_value=free):
            geproton.check_space(self.base, 25)
            with self.assertRaisesRegex(geproton.Error, "free space"):
                geproton.check_space(self.base, 26)

    def test_clean_and_remove(self):
        cache = self.base / "cache"
        work = geproton.workdir(cache)
        self.assertTrue(work.name.startswith("job-"))
        (cache / "lock").touch()
        (self.root / ".GE-Proton.old").mkdir(parents=True)
        (self.root / "GE-Proton").mkdir()
        geproton.clean(self.root, cache)
        self.assertEqual([p.name for p in cache.iterdir()], ["lock"])  # the lock stays: others may wait on it
        self.assertEqual([p.name for p in self.root.iterdir()], ["GE-Proton"])
        geproton.clean(self.root, cache, everything=True)
        self.assertFalse(cache.exists())
        geproton.remove_installed(self.root)
        self.assertEqual(list(self.root.iterdir()), [])


class Lock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache"

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_holder_across_instances(self):
        a, b = geproton.JobLock(self.cache), geproton.JobLock(self.cache)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())  # another open file: refused even in the same process
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_waits_for_the_holder(self):
        a, b = geproton.JobLock(self.cache), geproton.JobLock(self.cache)
        a.acquire()
        threading.Timer(0.2, a.release).start()
        self.assertTrue(b.acquire(wait=5))
        b.release()

    def test_stop_ends_the_wait(self):
        a, b = geproton.JobLock(self.cache), geproton.JobLock(self.cache)
        a.acquire()
        stop = threading.Event()
        stop.set()
        self.assertFalse(b.acquire(stop, wait=60))
        a.release()

if __name__ == "__main__":
    unittest.main()
