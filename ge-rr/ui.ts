import { defineModule, ui, type ModuleCtx } from "invasor";

// GE-Proton Rolling Release: one folded section per tool (GE-Proton, Proton-CachyOS), each with
// its Enabled switch, the installed version and Check / Download / Reinstall; and a Credits
// sub-tab. The update itself also runs by itself every time Steam starts, for the enabled tools.

type ToolId = "ge" | "cachyos";

interface Release {
  tag: string;
  url: string;
  size: number;
}

interface ToolStatus {
  name: string;
  enabled: boolean;
  phase: "idle" | "checking" | "downloading" | "verifying" | "extracting" | "installing" | "done" | "error";
  tag: string | null;
  done: number;
  total: number;
  error: string | null;
  latest: Release | null;
  checked: string | null;
  restart: boolean;
  flavour: string | null;
  installed: { tag: string | null; url: string | null } | null;
  update_available: boolean;
}

interface Status {
  tools: Record<ToolId, ToolStatus>;
  busy: boolean;
  game_running: boolean;
  pending: boolean;
}

const TOOLS: { id: ToolId; name: string }[] = [
  { id: "ge", name: "GE-Proton" },
  { id: "cachyos", name: "Proton-CachyOS" },
];

const DISABLED = "Disabled: it isn't checked or updated.";
const errorText = (e: unknown) => String((e as Error)?.message ?? e);
const mb = (n: number) => `${Math.round(n / 1048576)} MB`;

let refresh: (() => Promise<void>) | null = null;
let timer: number | undefined;
let visible = false;

function stopPolling() {
  clearTimeout(timer);
  timer = undefined;
}

function phaseText(t: ToolStatus): string {
  const tag = t.tag ?? t.name;
  switch (t.phase) {
    case "checking":
      return "Checking for updates…";
    case "downloading": {
      const pct = t.total ? ` ${Math.floor((100 * t.done) / t.total)}%` : "";
      return `Downloading ${tag}…${pct} (${mb(t.done)}${t.total ? ` of ${mb(t.total)}` : ""})`;
    }
    case "verifying":
      return `Verifying ${tag}…`;
    case "extracting":
      return `Extracting ${tag}…`;
    case "installing":
      return `Installing ${tag}…`;
    case "error":
      return `Failed: ${t.error ?? "unknown error"}`;
    default:
      return "";
  }
}

/** One tool's section: its controls, and paint() to show a status. */
function renderTool(ctx: ModuleCtx, id: ToolId, name: string) {
  const installedInfo = ui.info("…");
  const latestInfo = ui.info("Not checked yet.");
  const progressInfo = ui.info("");
  const restartInfo = ui.info(`Restart Steam to load the new ${name}.`);

  const enabled = ui.toggle({
    label: "Enabled",
    value: false,
    hint: `Keep the latest ${name} installed. Disabling it deletes nothing`,
    onChange: async (on) => {
      try {
        await ctx.settings.set(`${id}_enabled`, on);
      } catch (e) {
        enabled.set(!on);
        ctx.toast(errorText(e), "error");
      }
      await refresh?.();
    },
  });
  const check = ui.button({
    label: "Check for updates",
    onClick: async () => {
      check.setDisabled(true, "Checking…");
      latestInfo.textContent = "Checking…";
      try {
        paint(await ctx.call<Status>("check", { tool: id }));
      } catch (e) {
        latestInfo.textContent = errorText(e);
        check.setDisabled(false);
      }
    },
  });
  const download = ui.button({
    label: "Download update",
    onClick: () => start("download"),
  });
  const reinstall = ui.button({
    label: `Reinstall ${name}`,
    onClick: async () => {
      if (await ui.confirm(`Download ${name} again and replace the installed one?`, { ok: "Reinstall" })) {
        await start("reinstall");
      }
    },
  });

  async function start(method: "download" | "reinstall") {
    try {
      const r = await ctx.call<{ started: boolean; busy: boolean; game?: boolean }>(method, { tool: id });
      if (r.game) ctx.toast("A game is running: try again when it ends");
      else if (r.busy) ctx.toast("An update is already running");
    } catch (e) {
      ctx.toast(errorText(e), "error");
    }
    await refresh?.();
  }

  function paint(s: Status) {
    const t = s.tools[id];
    const flavour = t.flavour ?? "unsupported architecture";
    enabled.set(t.enabled);
    installedInfo.textContent = t.installed
      ? `Installed: ${t.installed.tag ?? "unknown version"} (${flavour})`
      : `${name} isn't installed (${flavour}).`;
    if (t.latest) {
      const when = t.checked ? ` · checked at ${t.checked}` : "";
      latestInfo.textContent = t.update_available
        ? `${t.latest.tag} is available (${mb(t.latest.size)})${when}`
        : `Up to date: ${t.latest.tag} is the latest${when}`;
    } else if (!t.enabled) {
      latestInfo.textContent = DISABLED;
    } else if (latestInfo.textContent === DISABLED) {
      latestInfo.textContent = "Not checked yet.";
    }
    progressInfo.textContent =
      phaseText(t) || (s.pending && t.enabled ? "Update postponed until the game ends." : "");
    progressInfo.style.display = progressInfo.textContent ? "" : "none";
    restartInfo.style.display = t.restart ? "" : "none";
    const off = t.enabled ? undefined : "Enable it first";
    check.setDisabled(s.busy || !t.enabled, s.busy ? "Busy updating" : off);
    const why = s.busy ? "Busy updating" : s.game_running ? "A game is running" : off;
    reinstall.setDisabled(s.busy || s.game_running || !t.enabled, why);
    download.setDisabled(
      s.busy || s.game_running || !t.enabled || !t.update_available,
      why ?? "Check for updates first, or restart Steam to update",
    );
  }

  const section = ui.section(name, [enabled, installedInfo, latestInfo, progressInfo, restartInfo, check, download, reinstall], {
    open: false,
  });
  return { section, paint };
}

function renderMain(el: HTMLElement, ctx: ModuleCtx) {
  const errorInfo = ui.info("");
  errorInfo.style.display = "none";
  const tools = TOOLS.map((t) => renderTool(ctx, t.id, t.name));

  refresh = async () => {
    stopPolling();
    let s: Status;
    try {
      s = await ctx.call<Status>("status");
    } catch (e) {
      errorInfo.textContent = errorText(e);
      errorInfo.style.display = "";
      return;
    }
    errorInfo.style.display = "none";
    for (const t of tools) t.paint(s);
    if (s.busy && visible) timer = window.setTimeout(() => void refresh?.(), 1000);
  };

  el.append(
    errorInfo,
    ...tools.map((t) => t.section),
    ui.separator(),
    ui.info("The enabled tools are checked and updated by themselves every time Steam starts, never while a game is running."),
    ui.info("Uninstalling this module and also deleting its data deletes GE-Proton too, and Proton-CachyOS if this module installed it."),
  );
  void refresh(); // polling while busy starts with the sub-tab's onShow
}

export default defineModule({
  tabs: [
    {
      label: "Proton",
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
          ui.info("GE-RR keeps the latest GE-Proton and Proton-CachyOS installed for Steam, downloaded from their official releases (github.com/GloriousEggroll/proton-ge-custom and github.com/CachyOS/proton-cachyos)."),
          ui.separator(),
          ui.info("Thanks to GloriousEggroll and every GE-Proton contributor, and to the CachyOS team and every Proton-CachyOS contributor. Both are based on Valve's Proton and keep their own licenses."),
          ui.separator(),
          ui.info("GE-RR is not affiliated with GE-Proton, GloriousEggroll or CachyOS. Please don't report problems with this module to them."),
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
