# GE-Proton Rolling Release

A module for [Invasor](../invasor) that keeps the latest [GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom)
installed as a single compatibility tool called **GE-Proton**. Its name never changes with the version, so a game set
to "GE-Proton" always runs the newest one: no more picking a new version in each game after every release.

- **When:** every time Steam starts or restarts (also when switching between Desktop and Game Mode), and when Invasor
  starts with Steam already running. If there's no network yet, it tries again for a minute.
- **What it downloads:** the latest GE-Proton release from its official GitHub page, built for this machine's
  architecture (`x86_64` or `aarch64`, detected automatically).
- **Checks:** the download must match the release's `.sha512sum` (or GitHub's sha256 digest if that file can't be
  read); nothing unverified is installed. It checks free space first, and refuses archives with unsafe paths.
- **Where:** `~/.local/share/Steam/compatibilitytools.d/GE-Proton`. The new version is fully downloaded and
  extracted before it replaces the old one, so a failed update leaves the installed GE-Proton untouched.
- **Notifications:** Steam shows one when a download starts and another when it's installed. Steam loads
  compatibility tools only when it starts: **restart Steam** to use the new version.

## The tab (Library only, not in Quick Access)
- **Check for updates:** asks GitHub for the latest version and says whether it's newer than the installed one.
- **Download update:** enabled when a newer version was found. Restarting Steam does the same.
- **Reinstall GE-Proton:** downloads the latest version again and replaces the installed one, even if it's the same
  (e.g. to repair a broken install).
- **Credits:** who makes GE-Proton.

## Uninstalling
Uninstalling the module keeps GE-Proton, so games set to it keep working. **If you also choose to delete the
module's data, GE-Proton is deleted too.** Disabling the module deletes nothing.

## Coming from `ge-proton-rr.sh`
This module does what `ge-proton-rr.sh --no-gui` did, and recognises the GE-Proton it installed (no new download).
Stop that script from running too: remove its autostart entry (`ge-proton-rr.sh --uninstall`) or whatever runs it.
Unlike the script, the module doesn't update itself.

## Build, test and install
With the core checked out next to this repository (`../invasor`):

```sh
python3 ../invasor/tools/pack_module.py ge-rr       # build, check, tests -> ge-rr-<version>.zip
python3 ../invasor/tools/install_module.py ge-rr    # or install that zip from ⚙ Settings › Install module
```

Needs an Invasor core with `ctx.on_steam_start` and `author` support in module.json.

## Credits
GE-Proton is by GloriousEggroll and its contributors, based on Valve's Proton, under its own licenses; GE-RR only
downloads it from its official releases. GE-RR is not affiliated with GE-Proton or GloriousEggroll.
Please don't report problems with this module to them.

## License
[GNU General Public License v3.0 or later](LICENSE) (GPL-3.0-or-later).
