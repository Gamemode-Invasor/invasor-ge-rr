"""GE-Proton Rolling Release: keeps the latest GE-Proton and Proton-CachyOS installed as
"GE-Proton" and "Proton-CachyOS".

Every time Steam starts (and when Invasor starts with Steam already up) it checks GitHub
for each enabled tool and, if there's a newer build for this machine, installs it, one tool
after the other. Never while a game is running: an automatic update waits for the game to
stop (and is paused if a game starts meanwhile); the buttons say so instead of starting.
Enabling a tool checks it right away; disabling it deletes nothing. The tab can check,
download now or reinstall from scratch. Steam loads compatibility tools only when it starts,
so after an update it recommends restarting Steam.
"""
import threading
import time

from . import geproton

ctx = None
_stop = None
_lock = threading.Lock()  # one update at a time in this instance (geproton.JobLock: across instances)
_state_lock = threading.Lock()
_state = {}  # per tool id
_pending = False  # an automatic update waits for the game to stop
_job_stop = None  # the automatic update in progress: its own cancel event (None: none running)
NET_TRIES, NET_WAIT = 6, 10  # the network may not be up yet when Steam starts
LOCK_WAIT = 60  # for an old instance's update to notice it was stopped (a stalled read: up to 30 s)
UNINSTALL_WAIT = 5  # uninstall() has 10 s in all


def _reset_state():
    global _state, _pending
    with _state_lock:
        _state = {t: {"phase": "idle", "tag": None, "done": 0, "total": 0, "error": None,
                      "latest": None, "checked": None, "restart": False} for t in geproton.TOOLS}
        _pending = False


def _set(tool, **kw):
    with _state_lock:
        _state[tool.id].update(kw)


def _root(context=None):
    """compatibilitytools.d of Steam's folder. uninstall() passes its own context: the module
    may be disabled, with no ctx of its own."""
    return geproton.compat_dir((context or ctx).paths.steam_root())


def _enabled(tool):
    return bool(ctx.settings.get(f"{tool.id}_enabled"))


def _tool(tool):
    """The tool a UI call names, if it exists and is enabled."""
    t = geproton.TOOLS.get(tool)
    if t is None:
        raise ctx.InvalidArgument(f"unknown tool {tool!r}")
    if not _enabled(t):
        raise ctx.InvalidArgument(f"{t.name} is disabled")
    return t


def setup(context):
    global ctx, _stop
    ctx = context
    _stop = threading.Event()
    _reset_state()
    ctx.events.on("steam_start", auto_update)
    ctx.events.on("game_started", _game_started)
    ctx.events.on("game_stopped", _game_stopped)
    ctx.settings.on_change(_changed)


def teardown():
    if _stop:
        _stop.set()  # stops the network wait and any download in progress
    if _job_stop:
        _job_stop.set()


def uninstall(context, purge):
    """Leftovers always go; the tools themselves only when the user asked to delete the data
    too, and Proton-CachyOS only if this module installed it (not one installed by hand)."""
    lock = geproton.JobLock()
    if not lock.acquire(wait=UNINSTALL_WAIT):
        context.log.warning("an update is still running: its files and the tools are left")
        return
    try:
        try:
            root = _root(context)
        except context.Unavailable as e:  # the cache still goes
            context.log.warning("Steam's tools are left: %s", e)
            root = None
        geproton.clean(root, everything=True)
        if purge and root is not None:
            for tool in geproton.TOOLS.values():
                current = geproton.installed(root, tool)
                if current is None or (tool is not geproton.GE and not current["url"]):
                    continue
                geproton.remove_installed(root, tool)
                context.log.info("removed %s", root / tool.folder)
    finally:
        lock.release()


def _game_running():
    return ctx.game.running is not None


def _core(stop, what, tool, fn, *args, **kw):
    """A ctx.archive call with its errors as the module's: cancelled while `stop` is set is a
    pause (Cancelled), anything else a failure fit for the user (Error)."""
    try:
        return fn(*args, **kw)
    except (ctx.Unavailable, ctx.InvalidArgument) as e:
        if stop.is_set():
            raise geproton.Cancelled("Cancelled") from None
        raise geproton.Error(f"Can't {what} {tool.name} ({e})") from None


def _latest(tool, stop, retry=False):
    tries = NET_TRIES if retry else 1
    for i in range(tries):
        try:
            return geproton.latest_release(geproton.flavour(tool), tool)
        except geproton.NetworkError as e:
            if i + 1 == tries:
                raise
            ctx.log.info("%s; trying again in %ds (%d/%d)", e, NET_WAIT, i + 1, tries)
            if stop.wait(NET_WAIT):
                raise geproton.Cancelled("Cancelled") from None


def _job(stop, tools, force, auto):
    """Check and install each tool, one after the other. The caller holds _lock; it's released here."""
    global _job_stop
    lock = geproton.JobLock()
    try:
        if not lock.acquire(stop, wait=LOCK_WAIT):
            if not stop.is_set():
                ctx.log.warning("another update is still running; skipped")
            return
        try:
            root = _root()
        except ctx.Unavailable as e:  # Steam's folder wasn't found
            ctx.log.warning("%s", e)
            for tool in tools:
                _set(tool, phase="error", error=str(e))
            return
        geproton.clean(root)  # leftovers of interrupted runs: no other update is running now
        for tool in tools:
            if stop.is_set() or not _update(tool, root, stop, force, auto):
                break  # cancelled: the rest waits for the next time
    finally:
        lock.release()
        _job_stop = None
        _lock.release()


def _update(tool, root, stop, force, auto):
    """Check and install one tool, with the JobLock held. False if it was cancelled."""
    work = None
    task = None  # opened when the download starts: a check that fails at boot shows no toast
    name = tool.name
    try:
        _set(tool, phase="checking", error=None, done=0, total=0)
        latest = _latest(tool, stop, retry=auto)
        current = geproton.installed(root, tool)
        _set(tool, latest=latest)
        if not force and not geproton.needs_update(current, latest):
            ctx.log.info("%s %s is up to date", name, latest["tag"])
            _set(tool, phase="idle")
            return True
        cache = geproton.cache_dir()
        geproton.check_space(cache, latest["size"], tool.space_factor)
        work = geproton.workdir(cache)
        ctx.log.info("downloading %s", latest["url"])
        _set(tool, phase="downloading", tag=latest["tag"], total=latest["size"])
        task = ctx.task(f"{name} update: {latest['tag']} ({latest['size'] >> 20} MB)")
        task.update(0, "Downloading")
        percent = [-1]

        def progress(done, total):
            _set(tool, done=done, total=total)
            pc = done * 100 // total if total else -1
            if pc != percent[0]:  # one update per percent, not per chunk
                percent[0] = pc
                task.update(done / total if total else None,
                            f"Downloading {done >> 20}/{total >> 20} MB" if total else "Downloading")

        archive = work / f"{tool.folder}{tool.ext}"
        sha512, sha256 = geproton.download(latest["url"], archive, latest["size"],
                                           progress=progress, cancel=stop)
        _set(tool, phase="verifying")
        task.update(None, "Verifying")
        how = geproton.verify(latest, sha512, sha256)
        ctx.log.info("checksum OK (%s)", how)
        _set(tool, phase="extracting")
        task.update(None, "Extracting")
        limit = {"max_bytes": latest["size"] * tool.space_factor} if latest["size"] else {}  # else the core's
        extracted = _core(stop, "extract", tool, ctx.archive.extract, archive, work / "extract", single_root=True,
                          symlinks="inside", cancel=stop, **limit)
        archive.unlink()
        _set(tool, phase="installing")
        task.update(None, "Installing")
        _core(stop, "install", tool, geproton.install, extracted, root, latest, tool, ctx.archive.swap_dir)
        ctx.log.info("installed %s in %s", latest["tag"], root / tool.folder)
        _set(tool, phase="done", restart=True)
        task.done("Installed. Restart Steam to load it.")
    except geproton.Cancelled:
        paused = _pending and not _stop.is_set()
        _set(tool, phase="idle")
        if task:
            task.fail("Paused: a game is running" if paused else "Cancelled")
        return False
    except Exception as e:
        if isinstance(e, (geproton.Error, ctx.Unavailable)):  # Unavailable: ctx.task, once the module is unloaded
            ctx.log.warning("%s: %s", name, e)
        else:
            ctx.log.exception("%s update failed", name)
        _set(tool, phase="error", error=str(e))
        if task:
            task.fail(f"Update failed: {e}")
    finally:
        if task:
            task.fail("Interrupted")  # no-op if it already ended
        if work is not None:
            geproton.remove(work)  # only this update's own folder
    return True


def _auto(tools=None, wait=0):
    """The automatic update of the enabled tools (or of these), unless a game is running
    (then it's left pending for game_stopped, which updates every enabled tool)."""
    global _job_stop, _pending
    tools = [t for t in (tools or geproton.TOOLS.values()) if _enabled(t)]
    if not tools:
        return
    if _game_running():
        ctx.log.info("a game is running: the update waits for it to stop")
        _pending = True
        return
    if not (_lock.acquire(timeout=wait) if wait else _lock.acquire(blocking=False)):
        if wait:
            ctx.log.info("busy: %s is checked the next time Steam starts", ", ".join(t.name for t in tools))
        return
    _pending = False
    _job_stop = threading.Event()
    if _stop.is_set():
        _job_stop.set()
    _job(_job_stop, tools, force=False, auto=True)


def auto_update(payload=None):
    """A new Steam instance: it has loaded whatever is installed, so no restart is pending."""
    for tool in geproton.TOOLS.values():
        _set(tool, restart=False)
    _auto()


def _changed(key, value):
    """A tool was enabled (from the tab or ⚙ Settings): check it now, in the background."""
    tool = next((t for t in geproton.TOOLS.values() if key == f"{t.id}_enabled"), None)
    if tool is None or not value or _stop.is_set():
        return
    threading.Thread(target=_auto, args=([tool], LOCK_WAIT), name="ge-rr-update", daemon=True).start()


def _game_started(payload=None):
    """An automatic update in progress stops (it resumes when the game does)."""
    global _pending
    stop = _job_stop
    if stop and not stop.is_set():
        ctx.log.info("a game started: pausing the update")
        _pending = True
        stop.set()


def _game_stopped(payload=None):
    if _pending and not _stop.is_set():
        _auto(wait=LOCK_WAIT)  # a paused job may still be finishing


def _start(tool, force):
    if _game_running():
        return {"started": False, "busy": False, "game": True}
    if not _lock.acquire(blocking=False):
        return {"started": False, "busy": True}
    threading.Thread(target=_job, args=(_stop, [tool], force, False), name="ge-rr-update", daemon=True).start()
    return {"started": True, "busy": False}


def status():
    with _state_lock:
        state = {t: dict(v) for t, v in _state.items()}
    tools = {}
    try:
        root = _root()
    except ctx.Unavailable:  # Steam's folder wasn't found: nothing shows as installed
        root = None
    for tid, tool in geproton.TOOLS.items():
        s = state[tid]
        current = geproton.installed(root, tool) if root else None
        try:
            flavour = geproton.flavour(tool)
        except geproton.Error:
            flavour = None
        tools[tid] = {**s, "name": tool.name, "enabled": _enabled(tool), "flavour": flavour, "installed": current,
                      "update_available": bool(s["latest"]) and geproton.needs_update(current, s["latest"])}
    return {"tools": tools, "busy": _lock.locked(), "game_running": _game_running(), "pending": _pending}


def check(tool):
    """Ask GitHub for the latest build of a tool (doesn't download anything)."""
    t = _tool(tool)
    try:
        latest = _latest(t, _stop)
    except geproton.Error as e:
        raise ctx.Unavailable(str(e)) from None
    _set(t, latest=latest, checked=time.strftime("%H:%M"))
    return status()


def download(tool):
    """Install the latest build of a tool now, if it's newer than the installed one."""
    return _start(_tool(tool), force=False)


def reinstall(tool):
    """Download a tool again and replace the installed one, even if it's the same."""
    return _start(_tool(tool), force=True)


METHODS = {"status": status, "check": check, "download": download, "reinstall": reinstall}
