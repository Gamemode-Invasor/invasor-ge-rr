"""GE-Proton Rolling Release: keeps the latest GE-Proton installed as "GE-Proton".

Every time Steam starts (and when Invasor starts with Steam already up) it checks GitHub
and, if there's a newer GE-Proton for this machine's architecture, installs it. Never while a
game is running: an automatic update waits for the game to stop (and is paused if a game
starts meanwhile); the buttons say so instead of starting. The tab
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
_job_stop = None  # the automatic update in progress: its own cancel event (None: none running)
NET_TRIES, NET_WAIT = 6, 10  # the network may not be up yet when Steam starts
LOCK_WAIT = 60  # for an old instance's update to notice it was stopped (a stalled read: up to 30 s)
UNINSTALL_WAIT = 5  # uninstall() has 10 s in all


def _reset_state():
    global _state
    with _state_lock:
        _state = {"phase": "idle", "tag": None, "done": 0, "total": 0, "error": None,
                  "latest": None, "checked": None, "restart": False, "pending": False}


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
    ctx.events.on("game_started", _game_started)
    ctx.events.on("game_stopped", _game_stopped)


def teardown():
    if _stop:
        _stop.set()  # stops the network wait and any download in progress
    if _job_stop:
        _job_stop.set()


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


def _game_running():
    try:
        return bool(ctx.game.running)
    except AttributeError:
        return False


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
    global _job_stop
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
        paused = _state["pending"] and not _stop.is_set()
        _set(phase="idle")
        _task_end(task, False, "Paused: a game is running" if paused else "Cancelled")
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
        _job_stop = None
        _lock.release()


def _auto(wait=0):
    """The automatic update, unless a game is running (then it's left pending for game_stopped)."""
    global _job_stop
    if _game_running():
        ctx.log.info("a game is running: the GE-Proton update waits for it to stop")
        _set(pending=True)
        return
    if not (_lock.acquire(timeout=wait) if wait else _lock.acquire(blocking=False)):
        return
    _set(pending=False)
    _job_stop = threading.Event()
    if _stop.is_set():
        _job_stop.set()
    _job(_job_stop, force=False, auto=True)


def auto_update(payload=None):
    """A new Steam instance: it has loaded whatever is installed, so no restart is pending."""
    _set(restart=False)
    _auto()


def _game_started(payload=None):
    """An automatic update in progress stops (it resumes when the game does)."""
    stop = _job_stop
    if stop and not stop.is_set():
        ctx.log.info("a game started: pausing the GE-Proton update")
        _set(pending=True)
        stop.set()


def _game_stopped(payload=None):
    if _state["pending"] and not _stop.is_set():
        _auto(wait=LOCK_WAIT)  # a paused job may still be finishing


def _start(force):
    if _game_running():
        return {"started": False, "busy": False, "game": True}
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
    return {**state, "arch": arch, "installed": current, "busy": _lock.locked(), "game_running": _game_running(),
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
