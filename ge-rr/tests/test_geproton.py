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
from invasor import archive, schema

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


CBASE = "https://github.com/CachyOS/proton-cachyos/releases/download/cachyos-11.0-20261014-slr/"
CTAG = "cachyos-11.0-20261014-slr"


def casset(suffix, size=10):
    name = f"proton-{CTAG}-{suffix}"
    return {"name": name, "browser_download_url": CBASE + name, "size": size}


CACHY_RELEASE = {
    "tag_name": CTAG,
    "assets": [casset(s) for s in ("arm64.sha512sum", "arm64.tar.xz", "x86_64.sha512sum", "x86_64.tar.xz",
                                     "x86_64_v3.sha512sum", "x86_64_v3.tar.xz")],
}
V3_CPU = "processor\t: 0\nflags\t\t: fpu " + " ".join(sorted(geproton.V3_FLAGS)) + " sse2\n"
V2_CPU = "processor\t: 0\nflags\t\t: fpu cx16 lahf_lm popcnt sse4_1 sse4_2 ssse3 avx\n"


def make_tar(path, files, top="GE-Proton11-7-x86_64", mode="w:gz", links=None):
    """files: {name: bytes}; links: {name: target} (symbolic)."""
    with tarfile.open(path, mode) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)


def extract(tar, dest):
    """What backend.py runs: the core's extract, with the options it passes."""
    return archive.extract(tar, dest, single_root=True, symlinks="inside")


class Arch(unittest.TestCase):
    def test_known_machines(self):
        self.assertEqual(geproton.arch("x86_64"), "x86_64")
        self.assertEqual(geproton.arch("AMD64"), "x86_64")
        self.assertEqual(geproton.arch("aarch64"), "aarch64")
        self.assertEqual(geproton.arch("arm64"), "aarch64")

    def test_unsupported(self):
        with self.assertRaisesRegex(geproton.Error, "riscv64"):
            geproton.arch("riscv64")


class Flavour(unittest.TestCase):
    def test_ge(self):
        self.assertEqual(geproton.flavour(geproton.GE, "x86_64", V3_CPU), "x86_64")
        self.assertEqual(geproton.flavour(geproton.GE, "aarch64"), "aarch64")

    def test_cachyos(self):
        c = geproton.CACHYOS
        self.assertEqual(geproton.flavour(c, "x86_64", V3_CPU), "x86_64_v3")
        self.assertEqual(geproton.flavour(c, "x86_64", V2_CPU), "x86_64")
        self.assertEqual(geproton.flavour(c, "x86_64", ""), "x86_64")
        self.assertEqual(geproton.flavour(c, "aarch64"), "arm64")
        with self.assertRaisesRegex(geproton.Error, "Proton-CachyOS isn't built"):
            geproton.flavour(c, "riscv64")


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

    def test_cachyos(self):
        c = geproton.CACHYOS
        r = geproton.pick_asset(CACHY_RELEASE, "x86_64_v3", c)
        self.assertEqual(r["tag"], CTAG)
        self.assertEqual(r["url"], CBASE + f"proton-{CTAG}-x86_64_v3.tar.xz")
        self.assertEqual(r["sha512_url"], CBASE + f"proton-{CTAG}-x86_64_v3.sha512sum")
        self.assertEqual(geproton.pick_asset(CACHY_RELEASE, "x86_64", c)["url"], CBASE + f"proton-{CTAG}-x86_64.tar.xz")
        self.assertEqual(geproton.pick_asset(CACHY_RELEASE, "arm64", c)["url"], CBASE + f"proton-{CTAG}-arm64.tar.xz")

    def test_only_the_repositorys_own_downloads(self):
        self.assertEqual(geproton.GE.download_prefix, "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/")
        self.assertEqual(geproton.CACHYOS.download_prefix, "https://github.com/CachyOS/proton-cachyos/releases/download/")
        for bad in ("https://evil.example/", "http://github.com/GloriousEggroll/proton-ge-custom/releases/download/x/",
                    "https://github.com/someone/proton-ge-custom/releases/download/x/"):
            for which in ("tar.gz", "sha512sum"):
                with self.subTest(bad=bad, which=which):
                    release = {"tag_name": "GE-Proton11-7", "assets": [
                        {"name": n, "size": 1, "browser_download_url": (bad if n.endswith(which) else BASE) + n}
                        for n in ("GE-Proton11-7-x86_64.tar.gz", "GE-Proton11-7-x86_64.sha512sum")]}
                    with self.assertRaisesRegex(geproton.Error, "unexpected download address"):
                        geproton.pick_asset(release, "x86_64")

    def test_cachyos_without_v3_falls_back(self):
        c = geproton.CACHYOS
        no_v3 = {**CACHY_RELEASE, "assets": [a for a in CACHY_RELEASE["assets"] if "_v3" not in a["name"]]}
        self.assertEqual(geproton.pick_asset(no_v3, "x86_64_v3", c)["url"], CBASE + f"proton-{CTAG}-x86_64.tar.xz")
        only_arm = {**CACHY_RELEASE, "assets": [a for a in CACHY_RELEASE["assets"] if "arm64" in a["name"]]}
        with self.assertRaisesRegex(geproton.Error, "Proton-CachyOS has no x86_64 download"):
            geproton.pick_asset(only_arm, "x86_64", c)  # no unsuffixed fallback as for GE

    def test_latest_release_reads_the_api(self):
        r = geproton.latest_release("x86_64", get=lambda url: json.dumps(RELEASE).encode())
        self.assertEqual(r["tag"], "GE-Proton11-7")
        with self.assertRaises(geproton.NetworkError):
            geproton.latest_release("x86_64", get=lambda url: b"<html>")
        urls = []
        r = geproton.latest_release("x86_64_v3", geproton.CACHYOS,
                                    get=lambda url: urls.append(url) or json.dumps(CACHY_RELEASE).encode())
        self.assertEqual((r["tag"], urls), (CTAG, [geproton.CACHYOS.api_url]))


class Http(unittest.TestCase):
    def _http_error(self, code, remaining):
        headers = Message()
        if remaining is not None:
            headers["X-RateLimit-Remaining"] = remaining
        return urllib.error.HTTPError(geproton.GE.api_url, code, "x", headers, None)

    def test_rate_limit(self):
        with mock.patch.object(geproton._opener, "open", side_effect=self._http_error(403, "0")):
            with self.assertRaisesRegex(geproton.Error, "rate limit") as cm:
                geproton.http_get(geproton.GE.api_url)
        self.assertNotIsInstance(cm.exception, geproton.NetworkError)  # not worth retrying

    def test_redirects_never_leave_https(self):
        handler = geproton._HttpsOnly()
        req = urllib.request.Request("https://github.com/x/y/releases/download/t/a.tar.gz")
        with self.assertRaisesRegex(urllib.error.URLError, "off https"):
            handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/a.tar.gz")
        ok = handler.redirect_request(req, None, 302, "Found", {}, "https://objects.githubusercontent.com/a")
        self.assertEqual(ok.full_url, "https://objects.githubusercontent.com/a")

    def test_the_cpu_is_read_once(self):
        geproton._cpuinfo.cache_clear()
        self.addCleanup(geproton._cpuinfo.cache_clear)
        with mock.patch.object(Path, "read_text", return_value="flags\t: " + " ".join(geproton.V3_FLAGS)) as read:
            self.assertTrue(geproton.has_v3())
            self.assertTrue(geproton.has_v3())
        read.assert_called_once()

    def test_other_errors_are_network_errors(self):
        with mock.patch.object(geproton._opener, "open", side_effect=self._http_error(502, None)):
            with self.assertRaises(geproton.NetworkError):
                geproton.http_get(geproton.GE.api_url)
        with mock.patch.object(geproton._opener, "open", side_effect=urllib.error.URLError("down")):
            with self.assertRaisesRegex(geproton.NetworkError, "down"):
                geproton.http_get(geproton.GE.api_url)


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

    def test_installed_by_hand_is_compared_by_tag(self):
        tool = self.root / "proton-cachyos"
        tool.mkdir()
        (tool / "version").write_text(f"1792277004 {CTAG}\n")
        c = geproton.CACHYOS
        cur = geproton.installed(self.root, c)
        self.assertEqual(cur, {"tag": CTAG, "url": None})
        self.assertIsNone(geproton.installed(self.root))  # GE-Proton isn't there
        self.assertFalse(geproton.needs_update(cur, geproton.pick_asset(CACHY_RELEASE, "x86_64_v3", c)))
        newer = {**CACHY_RELEASE, "tag_name": "cachyos-11.0-20261101-slr"}
        self.assertTrue(geproton.needs_update(cur, geproton.pick_asset(newer, "x86_64_v3", c)))
        self.assertTrue(geproton.needs_update({"tag": None, "url": None}, geproton.pick_asset(newer, "x86_64", c)))


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
        with mock.patch.object(geproton._opener, "open", return_value=self._response(data)):
            h = geproton.download("https://x/u", self.dest, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(h, (hashlib.sha512(data).hexdigest(), hashlib.sha256(data).hexdigest()))
        self.assertEqual(self.dest.read_bytes(), data)
        self.assertEqual(seen[-1], (len(data), len(data)))

    def test_incomplete(self):
        with mock.patch.object(geproton._opener, "open", return_value=self._response(b"abc", 10)):
            with self.assertRaisesRegex(geproton.NetworkError, "incomplete"):
                geproton.download("https://x/u", self.dest)

    def test_cancel(self):
        stop = threading.Event()
        stop.set()
        with mock.patch.object(geproton._opener, "open", return_value=self._response(b"abc")):
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
        make_tar(tar, {"proton": b"#!/bin/sh\n", "version": b"1 GE-Proton11-7\n", "lib/libx.so.1.2": b"x"},
                 links={"lib/libx.so.1": "libx.so.1.2"})
        top = extract(tar, self.base / "x")
        self.assertEqual(top.name, "GE-Proton11-7-x86_64")
        old = self.root / "GE-Proton"
        old.mkdir(parents=True)
        (old / "stale").touch()
        geproton.install(top, self.root, self.release, geproton.GE, archive.swap_dir)
        tool = self.root / "GE-Proton"
        self.assertFalse((tool / "stale").exists())
        self.assertEqual((tool / "lib/libx.so.1").read_bytes(), b"x")  # Proton's own relative links stay
        self.assertIn('"display_name" "GE-Proton"', (tool / "compatibilitytool.vdf").read_text())
        self.assertTrue((tool / "version-GE-Proton11-7-x86_64").exists())
        self.assertEqual(geproton.installed(self.root), {"tag": "GE-Proton11-7", "url": self.release["url"]})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["GE-Proton"])

    def test_cachyos_tar_xz(self):
        c = geproton.CACHYOS
        release = geproton.pick_asset(CACHY_RELEASE, "x86_64_v3", c)
        tar = self.base / "c.tar.xz"
        top_name = f"proton-{CTAG}-x86_64_v3"
        make_tar(tar, {"proton": b"#!/bin/sh\n", "version": f"1 {CTAG}\n".encode()}, top=top_name, mode="w:xz")
        top = extract(tar, self.base / "x")
        self.assertEqual(top.name, top_name)
        geproton.install(top, self.root, release, c, archive.swap_dir)
        vdf = (self.root / "proton-cachyos" / "compatibilitytool.vdf").read_text()
        self.assertIn('"proton-cachyos" // Internal name of this tool', vdf)
        self.assertIn('"display_name" "Proton-CachyOS"', vdf)
        self.assertEqual(geproton.installed(self.root, c), {"tag": CTAG, "url": release["url"]})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["proton-cachyos"])

    def test_unsafe_archives_are_refused(self):
        cases = {"traversal": ({"../evil": b"x"}, None, None),
                 "several folders": ({"a/f": b"1", "b/f": b"2"}, None, None),
                 "link leaving it": ({"f": b"1"}, "GE-Proton11-7-x86_64", {"out": "../../outside"}),
                 "absolute link": ({"f": b"1"}, "GE-Proton11-7-x86_64", {"out": "/etc/passwd"})}
        for case, (files, top, links) in cases.items():
            with self.subTest(case):
                tar = self.base / f"{case}.tar.gz"
                make_tar(tar, files, top=top, links=links)
                with self.assertRaises(schema.InvalidArgument):
                    extract(tar, self.base / case)
                self.assertFalse((self.base / case).exists())
        self.assertFalse((self.base / "evil").exists())

    def test_failed_swap_leaves_the_installed_one(self):
        new = self.base / "new"
        new.mkdir()
        old = self.root / "GE-Proton"
        old.mkdir(parents=True)
        (old / "keep").touch()

        def swap(src, target):
            raise schema.Unavailable("disk on fire")

        with self.assertRaisesRegex(schema.Unavailable, "disk on fire"):
            geproton.install(new, self.root, self.release, geproton.GE, swap)
        self.assertEqual(sorted(p.name for p in old.iterdir()), ["keep"])

    def test_check_space(self):
        free = mock.Mock(free=100)
        with mock.patch("shutil.disk_usage", return_value=free):
            geproton.check_space(self.base, 25)
            with self.assertRaisesRegex(geproton.Error, "free space"):
                geproton.check_space(self.base, 26)
            geproton.check_space(self.base, 16, geproton.CACHYOS.space_factor)
            with self.assertRaisesRegex(geproton.Error, "free space"):
                geproton.check_space(self.base, 17, geproton.CACHYOS.space_factor)

    def test_clean_and_remove(self):
        cache = self.base / "cache"
        work = geproton.workdir(cache)
        self.assertTrue(work.name.startswith("job-"))
        (cache / "lock").touch()
        (self.root / ".GE-Proton.old").mkdir(parents=True)  # older versions of this module
        (self.root / ".proton-cachyos.new").mkdir()
        (self.root / ".GE-Proton.old-1a2b3c4d").mkdir()  # an interrupted ctx.archive.swap_dir
        (self.root / ".proton-cachyos.new-5e6f7a8b").mkdir()
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
