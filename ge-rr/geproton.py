"""GE-Proton Rolling Release: find, download, verify and install the latest GE-Proton
and Proton-CachyOS.

Plain functions over a Steam root and a cache folder, so the tests run on a fake tree.
Each tool is always installed in the same folder of compatibilitytools.d (GE-Proton,
proton-cachyos), shown in Steam with the same name whatever its version, so games set
to it follow every update.
"""
import fcntl
import hashlib
import json
import os
import platform
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

STEAM_ROOT = Path.home() / ".local/share/Steam"
USER_AGENT = "invasor-ge-rr"
URL_MARKER = "url_downloaded"  # same markers as ge-proton-rr.sh, so its installs are recognised
VERSION_MARKER = "version-"
CHUNK = 1 << 20
LOCK_NAME = "lock"
WORK_PREFIX = "job-"

# What x86-64-v3 needs (lzcnt shows as abm), and v2 below it.
V3_FLAGS = {"cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3",
            "avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave"}


def _vdf(internal, display):
    return f""""compatibilitytools"
{{
  "compat_tools"
  {{
    "{internal}" // Internal name of this tool
    {{
      "install_path" "."
      "display_name" "{display}"
      "from_oslist"  "windows"
      "to_oslist"    "linux"
    }}
  }}
}}
"""


@dataclass(frozen=True)
class Tool:
    id: str  # also the settings key: <id>_enabled
    name: str  # for messages, and Steam's display_name
    folder: str  # in compatibilitytools.d, and the vdf's internal name
    api_url: str
    ext: str
    arm: str  # how the release calls aarch64
    v3: bool  # has x86_64_v3 builds
    space_factor: int  # the archive plus the tool extracted, in archive sizes

    @property
    def vdf(self):
        return _vdf(self.folder, self.name)


GE = Tool("ge", "GE-Proton", "GE-Proton",
          "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest",
          ".tar.gz", "aarch64", False, 4)
CACHYOS = Tool("cachyos", "Proton-CachyOS", "proton-cachyos",
               "https://api.github.com/repos/CachyOS/proton-cachyos/releases/latest",
               ".tar.xz", "arm64", True, 6)  # xz: about 5 times the archive extracted
TOOLS = {t.id: t for t in (GE, CACHYOS)}


class Error(Exception):
    """A failure with a message fit for the user."""


class NetworkError(Error):
    """No connection, or GitHub didn't answer: worth retrying."""


class Cancelled(Error):
    pass


def compat_dir(steam_root=STEAM_ROOT):
    return Path(steam_root) / "compatibilitytools.d"


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "invasor-ge-rr"


class JobLock:
    """One update at a time, also across module instances: after a reload or a Rescan,
    the old instance's thread may still be finishing (it was told to stop) while the new
    one starts. An flock on a file in the cache folder, which both instances share."""

    def __init__(self, cache=None):
        self.path = Path(cache or cache_dir()) / LOCK_NAME
        self._f = None

    def acquire(self, stop=None, wait=0):
        """True once held; False if still busy after `wait` seconds or if `stop` is set."""
        deadline = time.monotonic() + wait
        while not self._try():
            if time.monotonic() >= deadline:
                return False
            if stop is not None:
                if stop.wait(1):
                    return False
            else:
                time.sleep(1)
        return True

    def _try(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self):
        if self._f:
            fcntl.flock(self._f, fcntl.LOCK_UN)
            self._f.close()
            self._f = None


def workdir(cache=None):
    """A temporary folder of this update's own in the cache folder."""
    cache = Path(cache or cache_dir())
    cache.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir=cache))


def arch(machine=None, tool=GE):
    """This machine's architecture: "x86_64" or "aarch64"."""
    m = (machine if machine is not None else platform.machine()).lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    raise Error(f"{tool.name} isn't built for this machine ({m or 'unknown'})")


def has_v3(cpuinfo=None):
    """True if the CPU runs x86-64-v3 code (from /proc/cpuinfo's flags)."""
    if cpuinfo is None:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
        except OSError:
            return False
    for line in cpuinfo.splitlines():
        if line.startswith("flags"):
            return V3_FLAGS <= set(line.partition(":")[2].split())
    return False


def flavour(tool=GE, machine=None, cpuinfo=None):
    """The build of tool for this machine, as its release names it: GE-Proton "x86_64" or
    "aarch64"; Proton-CachyOS "x86_64_v3" if the CPU runs it, else "x86_64", or "arm64"."""
    a = arch(machine, tool)
    if a == "aarch64":
        return tool.arm
    return "x86_64_v3" if tool.v3 and has_v3(cpuinfo) else a


def _open(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            raise Error("GitHub API rate limit reached, try again later") from None
        raise NetworkError(f"GitHub answered {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise NetworkError(f"No connection to GitHub ({getattr(e, 'reason', e)})") from None


def http_get(url, timeout=15):
    with _open(url, timeout) as r:
        try:
            return r.read()
        except OSError as e:
            raise NetworkError(f"Connection lost ({e})") from None


def pick_asset(release, flavour_name, tool=GE):
    """{tag, url, size, sha512_url, sha256} for this build from a GitHub release."""
    ext = tool.ext
    assets = {a.get("name", ""): a for a in release.get("assets") or []}
    tars = [n for n in assets if n.endswith(ext)]
    name = next((n for n in tars if n.endswith(f"-{flavour_name}{ext}")), None)
    if name is None and flavour_name == "x86_64_v3":
        name = next((n for n in tars if n.endswith(f"-x86_64{ext}")), None)
    if name is None and flavour_name == "x86_64" and tool is GE:
        # Older releases had a single unsuffixed x86_64 tarball.
        name = next((n for n in tars if "aarch64" not in n), None)
    if name is None:
        raise Error(f"The latest {tool.name} has no {flavour_name} download")
    asset = assets[name]
    sha = assets.get(name[: -len(ext)] + ".sha512sum")
    digest = asset.get("digest") or ""
    return {
        "tag": release.get("tag_name") or name[: -len(ext)],
        "url": asset["browser_download_url"],
        "size": int(asset.get("size") or 0),
        "sha512_url": sha["browser_download_url"] if sha else None,
        "sha256": digest[len("sha256:"):] if digest.startswith("sha256:") else None,
    }


def latest_release(flavour_name, tool=GE, get=http_get):
    try:
        release = json.loads(get(tool.api_url))
    except ValueError:
        raise NetworkError("GitHub sent an unreadable answer") from None
    return pick_asset(release, flavour_name, tool)


def installed(root, tool=GE):
    """{tag, url} of the tool in compatibilitytools.d, or None if there's none."""
    folder = Path(root) / tool.folder
    if not folder.is_dir():
        return None
    url = tag = None
    try:
        url = (folder / URL_MARKER).read_text().strip() or None
    except OSError:
        pass
    try:  # the tool's own "version" file: "<timestamp> <tag>"
        tag = (folder / "version").read_text().split()[-1]
    except (OSError, IndexError):
        marker = next(folder.glob(VERSION_MARKER + "*"), None)
        tag = marker.name[len(VERSION_MARKER):] if marker else None
    return {"tag": tag, "url": url}


def needs_update(current, latest):
    """By the URL it was downloaded from; one installed by hand (no URL) by its version."""
    if current is None:
        return True
    if current.get("url"):
        return current["url"] != latest["url"]
    return not current.get("tag") or current["tag"] != latest["tag"]


def check_space(folder, size, factor=GE.space_factor):
    folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(folder).free
    need = size * factor
    if size and free < need:
        raise Error(f"Not enough free space: {need >> 20} MB needed, {free >> 20} MB free")


def download(url, dest, size=0, progress=None, cancel=None, timeout=30):
    """Stream url to dest. Returns (sha512, sha256) hex digests of what was written."""
    h512, h256 = hashlib.sha512(), hashlib.sha256()
    done = 0
    with _open(url, timeout) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0) or size
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled("Cancelled")
            try:
                chunk = r.read(CHUNK)
            except OSError as e:
                raise NetworkError(f"Download interrupted ({e})") from None
            if not chunk:
                break
            f.write(chunk)
            h512.update(chunk)
            h256.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if total and done != total:
        raise NetworkError(f"Download incomplete ({done >> 20} of {total >> 20} MB)")
    return h512.hexdigest(), h256.hexdigest()


def verify(release, sha512, sha256, get=http_get):
    """The release's .sha512sum must match, or else GitHub's sha256 digest. Never unverified."""
    if release.get("sha512_url"):
        try:
            expected = get(release["sha512_url"]).decode(errors="replace").split()[0].lower()
        except (Error, IndexError):
            expected = None
        if expected:
            if expected != sha512:
                raise Error("The download is corrupt (sha512 doesn't match)")
            return "sha512"
    if release.get("sha256"):
        if release["sha256"].lower() != sha256:
            raise Error("The download is corrupt (sha256 doesn't match)")
        return "sha256"
    raise Error("Can't verify the download: GitHub gave no checksum")


def extract(archive, dest, cancel=None, tool=GE):
    """Extract into dest (which must be empty or missing); returns the single top folder."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:*") as tar:
            for member in tar:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("Cancelled")
                # "data" refuses absolute paths, "..", devices and links leaving dest.
                tar.extract(member, dest, filter="data")
    except (tarfile.TarError, OSError) as e:
        raise Error(f"Can't extract {tool.name} ({e})") from None
    tops = [p for p in dest.iterdir()]
    if len(tops) != 1 or not tops[0].is_dir():
        raise Error(f"Unexpected {tool.name} archive (not a single folder)")
    return tops[0]


def install(extracted, root, release, tool=GE):
    """Make extracted the new compatibilitytools.d/<tool folder>. The old one is swapped out
    with renames and only deleted once the new one is in place."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    extracted = Path(extracted)
    (extracted / "compatibilitytool.vdf").write_text(tool.vdf)
    (extracted / (VERSION_MARKER + extracted.name)).touch()
    (extracted / URL_MARKER).write_text(release["url"] + "\n")

    target, old, staged = root / tool.folder, root / f".{tool.folder}.old", root / f".{tool.folder}.new"
    remove(old)
    remove(staged)
    if extracted.stat().st_dev != root.stat().st_dev:
        shutil.move(str(extracted), staged)  # a copy across filesystems; the swap below stays a rename
        extracted = staged
    if target.exists() or target.is_symlink():
        try:
            target.rename(old)
        except OSError as e:
            raise Error(f"Can't replace the installed {tool.name} ({e})") from None
    try:
        extracted.rename(target)
    except OSError as e:
        if old.exists():
            old.rename(target)
        raise Error(f"Can't install {tool.name} ({e})") from None
    finally:
        remove(staged)
    remove(old)


def remove_installed(root, tool=GE):
    remove(Path(root) / tool.folder)


def clean(root, cache=None, everything=False):
    """Remove leftovers of interrupted runs. Only with the JobLock held: then no update is
    running, so every work folder is a leftover. everything also removes the cache folder."""
    cache = Path(cache or cache_dir())
    if everything:
        remove(cache)
    else:
        for work in cache.glob(WORK_PREFIX + "*"):
            remove(work)
    for tool in TOOLS.values():
        remove(Path(root) / f".{tool.folder}.old")
        remove(Path(root) / f".{tool.folder}.new")


def remove(path):
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)
