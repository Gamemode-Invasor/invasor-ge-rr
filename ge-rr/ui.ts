import { defineModule, ui, type ModuleCtx } from "invasor";

// GE-Proton Rolling Release: panel with the installed version, Check / Download / Reinstall,
// and a Credits sub-tab. The update itself also runs by itself every time Steam starts.

interface Release {
  tag: string;
  url: string;
  size: number;
}

interface Status {
  phase: "idle" | "checking" | "downloading" | "verifying" | "extracting" | "installing" | "done" | "error";
  tag: string | null;
  done: number;
  total: number;
  error: string | null;
  latest: Release | null;
  checked: string | null;
  restart: boolean;
  arch: string | null;
  installed: { tag: string | null; url: string | null } | null;
  busy: boolean;
  update_available: boolean;
}

const errorText = (e: unknown) => String((e as Error)?.message ?? e);
const mb = (n: number) => `${Math.round(n / 1048576)} MB`;

let refresh: (() => Promise<void>) | null = null;
let timer: number | undefined;
let visible = false;

function stopPolling() {
  clearTimeout(timer);
  timer = undefined;
}

function phaseText(s: Status): string {
  const tag = s.tag ?? "GE-Proton";
  switch (s.phase) {
    case "checking":
      return "Checking for updates…";
    case "downloading": {
      const pct = s.total ? ` ${Math.floor((100 * s.done) / s.total)}%` : "";
      return `Downloading ${tag}…${pct} (${mb(s.done)}${s.total ? ` of ${mb(s.total)}` : ""})`;
    }
    case "verifying":
      return `Verifying ${tag}…`;
    case "extracting":
      return `Extracting ${tag}…`;
    case "installing":
      return `Installing ${tag}…`;
    case "error":
      return `Failed: ${s.error ?? "unknown error"}`;
    default:
      return "";
  }
}

function renderMain(el: HTMLElement, ctx: ModuleCtx) {
  const installedInfo = ui.info("…");
  const latestInfo = ui.info("Not checked yet.");
  const progressInfo = ui.info("");
  const restartInfo = ui.info("Restart Steam to load the new GE-Proton.");

  const check = ui.button({
    label: "Check for updates",
    onClick: async () => {
      check.setDisabled(true, "Checking…");
      latestInfo.textContent = "Checking…";
      try {
        paint(await ctx.call<Status>("check"));
      } catch (e) {
        latestInfo.textContent = errorText(e);
      } finally {
        check.setDisabled(false);
      }
    },
  });
  const download = ui.button({
    label: "Download update",
    onClick: () => start("download"),
  });
  const reinstall = ui.button({
    label: "Reinstall GE-Proton",
    onClick: async () => {
      if (await ui.confirm("Download GE-Proton again and replace the installed one?", { ok: "Reinstall" })) {
        await start("reinstall");
      }
    },
  });

  async function start(method: "download" | "reinstall") {
    try {
      const r = await ctx.call<{ started: boolean; busy: boolean }>(method);
      if (r.busy) ctx.toast("GE-Proton is already being updated");
    } catch (e) {
      ctx.toast(errorText(e), "error");
    }
    await refresh?.();
  }

  function paint(s: Status) {
    const arch = s.arch ?? "unsupported architecture";
    installedInfo.textContent = s.installed
      ? `Installed: ${s.installed.tag ?? "unknown version"} (${arch})`
      : `GE-Proton isn't installed (${arch}).`;
    if (s.latest) {
      const when = s.checked ? ` · checked at ${s.checked}` : "";
      latestInfo.textContent = s.update_available
        ? `${s.latest.tag} is available (${mb(s.latest.size)})${when}`
        : `Up to date: ${s.latest.tag} is the latest${when}`;
    }
    progressInfo.textContent = phaseText(s);
    progressInfo.style.display = progressInfo.textContent ? "" : "none";
    restartInfo.style.display = s.restart ? "" : "none";
    check.setDisabled(s.busy, s.busy ? "Busy updating" : undefined);
    reinstall.setDisabled(s.busy, s.busy ? "Busy updating" : undefined);
    download.setDisabled(
      s.busy || !s.update_available,
      s.busy ? "Busy updating" : "Check for updates first, or restart Steam to update",
    );
  }

  refresh = async () => {
    stopPolling();
    let s: Status;
    try {
      s = await ctx.call<Status>("status");
    } catch (e) {
      installedInfo.textContent = errorText(e);
      return;
    }
    paint(s);
    if (s.busy && visible) timer = window.setTimeout(() => void refresh?.(), 1000);
  };

  el.append(
    installedInfo,
    latestInfo,
    progressInfo,
    restartInfo,
    check,
    download,
    reinstall,
    ui.separator(),
    ui.info("GE-Proton is checked and updated by itself every time Steam starts."),
    ui.info("Uninstalling this module and also deleting its data deletes GE-Proton too."),
  );
  void refresh(); // polling while busy starts with the sub-tab's onShow
}

export default defineModule({
  tabs: [
    {
      label: "GE-Proton",
      render: (el, ctx) => renderMain(el, ctx),
      onShow: () => {
        visible = true;
        void refresh?.();
      },
      onHide: () => {
        visible = false;
        stopPolling();
      },
    },
    {
      label: "Credits",
      render(el) {
        el.append(
          ui.info("GE-RR keeps the latest GE-Proton installed for Steam, downloaded from its official releases (github.com/GloriousEggroll/proton-ge-custom)."),
          ui.separator(),
          ui.info("Thanks to GloriousEggroll and every GE-Proton contributor. GE-Proton is based on Valve's Proton and keeps its own licenses."),
          ui.separator(),
          ui.info("GE-RR is not affiliated with GE-Proton or GloriousEggroll. Please don't report problems with this module to them."),
        );
      },
    },
  ],
  tabsAlign: "justify",
  destroy: () => {
    stopPolling();
    refresh = null;
  },
});
