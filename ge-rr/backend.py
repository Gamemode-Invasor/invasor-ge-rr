"""GE-Proton Rolling Release: keeps the latest GE-Proton installed as "GE-Proton".

Every time Steam starts (and when Invasor starts with Steam already up) it checks GitHub
and, if there's a newer GE-Proton for this machine's architecture, installs it. The tab
can check, download now or reinstall from scratch. Steam loads compatibility tools only
when it starts, so after an update it recommends restarting Steam.
"""
import threading
import time

from . import geproton

ctx = None
_stop = None
_lock = threading.Lock()  # one update at a time in this instance (geproton.JobLock: across instances)
_state_lock = threading.Lock()
_state = {}
NET_TRIES, NET_WAIT = 6, 10  # the network may not be up yet when Steam starts
LOCK_WAIT = 60  # for an old instance's update to notice it was stopped (a stalled read: up to 30 s)
UNINSTALL_WAIT = 5  # uninstall() has 10 s in all


def _reset_state():
    global _state
    with _state_lock:
        _state = {"phase": "idle", "tag": None, "done": 0, "total": 0, "error": None,
                  "latest": None, "checked": None, "restart": False}


def _set(**kw):
    with _state_lock:
        _state.update(kw)


def _root():
    return geproton.compat_dir()


def setup(context):
    global ctx, _stop
    ctx = context
    _stop = threading.Event()
    _reset_state()
    ctx.events.on("steam_start", auto_update)


def teardown():
    if _stop:
        _stop.set()  # stops the network wait and any download in progress


def uninstall(context, purge):
    """Leftovers always go; GE-Proton itself only when the user asked to delete the data too."""
    lock = geproton.JobLock()
    if not lock.acquire(wait=UNINSTALL_WAIT):
        context.log.warning("an update is still running: its files and GE-Proton are left")
        return
    try:
        geproton.clean(_root(), everything=True)
        if purge:
            geproton.remove_installed(_root())
            context.log.info("removed %s", _root() / geproton.TOOL)
    finally:
        lock.release()


def _notify(title, body):
    try:
        ctx.notify(title, body, sound="toast")
    except (ctx.Unavailable, ctx.InvalidArgument) as e:
        ctx.log.warning("notification not shown: %s", e)  # plan B: the log


def _task(label):
    """The download in the Tasks panel (besides the notifications); None if it can't be shown."""
    try:
        return ctx.task(label)
    except ctx.Unavailable as e:
        ctx.log.warning("task not shown: %s", e)
        return None


def _task_update(task, fraction, text):
    if task:
        task.update(fraction, text)


def _task_end(task, ok, text):
    if task:
        (task.done if ok else task.fail)(text)


def _latest(stop, retry=False):
    tries = NET_TRIES if retry else 1
    for i in range(tries):
        try:
            return geproton.latest_release(geproton.arch())
        except geproton.NetworkError as e:
            if i + 1 == tries:
                raise
            ctx.log.info("%s; trying again in %ds (%d/%d)", e, NET_WAIT, i + 1, tries)
            if stop.wait(NET_WAIT):
                raise geproton.Cancelled("Cancelled") from None


def _job(stop, force, auto):
    """Check and install. The caller holds _lock; it's released here."""
    started = False
    root = _root()
    lock = geproton.JobLock()
    work = None
    task = None
    try:
        _set(phase="checking", error=None, done=0, total=0)
        if not lock.acquire(stop, wait=LOCK_WAIT):
            if not stop.is_set():
                ctx.log.warning("another GE-Proton update is still running; skipped")
            _set(phase="idle")
            return
        geproton.clean(root)  # leftovers of interrupted runs: no other update is running now
        latest = _latest(stop, retry=auto)
        current = geproton.installed(root)
        _set(latest=latest)
        if not force and not geproton.needs_update(current, latest):
            ctx.log.info("%s is up to date", latest["tag"])
            _set(phase="idle")
            return
        cache = geproton.cache_dir()
        geproton.check_space(cache, latest["size"])
        work = geproton.workdir(cache)
        ctx.log.info("downloading %s", latest["url"])
        _set(phase="downloading", tag=latest["tag"], total=latest["size"])
        started = True
        _notify("Downloading GE-Proton", f"{latest['tag']} ({latest['size'] >> 20} MB)")
        task = _task(f"GE-Proton {latest['tag']}")
        _task_update(task, 0, "Downloading")
        percent = [-1]

        def progress(done, total):
            _set(done=done, total=total)
            pc = done * 100 // total if total else -1
            if pc != percent[0]:  # one update per percent, not per chunk
                percent[0] = pc
                _task_update(task, done / total if total else None,
                             f"Downloading {done >> 20}/{total >> 20} MB" if total else "Downloading")

        archive = work / "GE-Proton.tar.gz"
        sha512, sha256 = geproton.download(latest["url"], archive, latest["size"],
                                           progress=progress, cancel=stop)
        _set(phase="verifying")
        _task_update(task, None, "Verifying")
        how = geproton.verify(latest, sha512, sha256)
        ctx.log.info("checksum OK (%s)", how)
        _set(phase="extracting")
        _task_update(task, None, "Extracting")
        extracted = geproton.extract(archive, work / "extract", cancel=stop)
        archive.unlink()
        _set(phase="installing")
        _task_update(task, None, "Installing")
        geproton.install(extracted, root, latest)
        ctx.log.info("installed %s in %s", latest["tag"], root / geproton.TOOL)
        _set(phase="done", restart=True)
        _task_end(task, True, "Installed. Restart Steam to load it")
        _notify("GE-Proton installed", f"{latest['tag']} is installed. Restart Steam to load it.")
    except geproton.Cancelled:
        _set(phase="idle")
        _task_end(task, False, "Cancelled")
    except geproton.Error as e:
        ctx.log.warning("%s", e)
        _set(phase="error", error=str(e))
        _task_end(task, False, str(e))
        if started:  # no toast for a check that failed at boot (e.g. no network)
            _notify("GE-Proton update failed", str(e))
    except Exception as e:
        ctx.log.exception("update failed")
        _set(phase="error", error=str(e))
        _task_end(task, False, str(e))
        if started:
            _notify("GE-Proton update failed", str(e))
    finally:
        _task_end(task, False, "Interrupted")  # no-op if it already ended
        if work is not None:
            geproton.remove(work)  # only this update's own folder
        lock.release()
        _lock.release()


def auto_update(payload=None):
    """A new Steam instance: it has loaded whatever is installed, so no restart is pending."""
    _set(restart=False)
    if not _lock.acquire(blocking=False):
        return
    _job(_stop, force=False, auto=True)


def _start(force):
    if not _lock.acquire(blocking=False):
        return {"started": False, "busy": True}
    threading.Thread(target=_job, args=(_stop, force, False), name="ge-rr-update", daemon=True).start()
    return {"started": True, "busy": False}


def status():
    with _state_lock:
        state = dict(_state)
    current = geproton.installed(_root())
    latest = state["latest"]
    try:
        arch = geproton.arch()
    except geproton.Error:
        arch = None
    return {**state, "arch": arch, "installed": current, "busy": _lock.locked(),
            "update_available": bool(latest) and geproton.needs_update(current, latest)}


def check():
    """Ask GitHub for the latest GE-Proton (doesn't download anything)."""
    try:
        latest = geproton.latest_release(geproton.arch())
    except geproton.Error as e:
        raise ctx.Unavailable(str(e)) from None
    _set(latest=latest, checked=time.strftime("%H:%M"))
    return status()


def download():
    """Install the latest GE-Proton now, if it's newer than the installed one."""
    return _start(force=False)


def reinstall():
    """Download the latest GE-Proton again and replace the installed one, even if it's the same."""
    return _start(force=True)


METHODS = {"status": status, "check": check, "download": download, "reinstall": reinstall}
