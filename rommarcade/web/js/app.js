// RomMArcade web client: hash routing, library views, installs, deep links.
import { api } from "./api.js";
import {
  h, icon, toast, openModal, closeModal, emptyState, coverImage, gameCard,
  platformCard, skeletonGrid, fmtBytes, fmtSpeed, timeAgo, shortDate, debounce, initials, installLabel,
} from "./ui.js";

const state = {
  status: null,
  config: null,
  platforms: null,
  installs: null,
  emudeck: null,
  downloads: [],
  history: null,
  renderToken: 0,
  seenDone: new Set(),
  seenErrors: new Set(),
  downloadsPrimed: false,
  sessionStart: Date.now() / 1000,
  installRenderJobs: new Set(),
  pendingPlay: new Map(),
  autoPlay: null,
  trayHidden: 0,
  biosTimer: null,
  biosBatch: null,
  timer: null,
  setup: null,
  sort: {},
};

const viewEl = document.getElementById("view");
const topbar = document.getElementById("topbar");
const searchEl = document.getElementById("search");
const trayEl = document.getElementById("downloadTray");

// ---------------------------------------------------------------- boot ----
async function boot() {
  let status = null;
  try {
    status = await api.status();
  } catch (error) {
    showFatal(error.message);
    return;
  }
  state.status = status;
  try {
    const cfg = await api.config();
    state.config = cfg.config;
  } catch { state.config = null; }
  finishBoot();
  if (!status.configured) {
    if (location.hash !== "#/setup") location.hash = "#/setup";
  }
  window.addEventListener("hashchange", route);
  window.addEventListener("resize", debounce(renderTray, 150));
  document.addEventListener("keydown", onKey);
  document.addEventListener("click", onDocumentClick);
  window.addEventListener("rommarcade:play", (event) => playRom(event.detail.romId));
  window.addEventListener("rommarcade:install", (event) => installRom(event.detail.romId, { play: event.detail.play }));
  searchEl.addEventListener("input", debounce(onSearch, 320));
  await route();
  startTimers();
}

function finishBoot() {
  document.getElementById("boot")?.classList.add("done");
  topbar.hidden = false;
  viewEl.hidden = false;
  paintServerPill();
}

function showFatal(message) {
  document.getElementById("boot")?.classList.add("done");
  topbar.hidden = false;
  viewEl.hidden = false;
  viewEl.replaceChildren(emptyState({
    glyph: "alert",
    title: "RomMArcade cannot reach its own service",
    body: message,
    action: h("button", { class: "btn primary", onClick: () => location.reload() }, "Try again"),
  }));
}

function startTimers() {
  clearInterval(state.timer);
  state.timer = setInterval(async () => {
    await pollDownloads();
    await pollDeeplink();
    if (Math.random() < 0.12) refreshStatus();
  }, 1400);
}

async function refreshStatus() {
  try {
    state.status = await api.status();
    paintServerPill();
  } catch { /* service restarting */ }
}

function paintServerPill() {
  const pill = document.getElementById("serverPill");
  if (!pill) return;
  const server = state.status?.server || {};
  pill.classList.remove("ok", "bad");
  if (!server.configured) {
    pill.querySelector("span").textContent = "No server";
    pill.classList.add("bad");
  } else if (server.reachable) {
    pill.querySelector("span").textContent = server.name || hostOf(server.url);
    pill.classList.add("ok");
  } else {
    pill.querySelector("span").textContent = "Offline";
    pill.classList.add("bad");
  }
  pill.title = server.configured
    ? `${server.url}${server.reachable ? "" : " — " + (server.error || "unreachable")}`
    : "Connect a RomM server";
}

// ---------------------------------------------------------------- route ---
function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [name, ...rest] = raw.split("/");
  return { name: name || "library", args: rest.map((part) => decodeURIComponent(part)) };
}

function setRoute(name, ...args) {
  const suffix = args.length ? "/" + args.map((a) => encodeURIComponent(a)).join("/") : "";
  location.hash = `#/${name}${suffix}`;
}

async function route() {
  const current = parseRoute();
  setActiveNav(current);
  const token = ++state.renderToken;
  viewEl.replaceChildren();
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
  try {
    if (current.name === "setup") await renderSetup();
    else if (current.name === "platform") await renderPlatform(current.args[0] || "", token);
    else if (current.name === "game") await renderGame(current.args[0] || "", token);
    else if (current.name === "installed") await renderInstalled(token);
    else if (current.name === "emudeck") await renderEmudeck(token);
    else if (current.name === "settings") await renderSettings(current.args[0] || "server", token);
    else if (current.name === "search") await renderSearch(current.args.join("/"), token);
    else await renderLibrary(token);
  } catch (error) {
    if (token !== state.renderToken) return;
    viewEl.replaceChildren(emptyState({
      glyph: "alert",
      title: "That view could not load",
      body: error.message || String(error),
      action: h("button", { class: "btn", onClick: () => route() }, icon("refresh"), "Retry"),
    }));
  }
}

function setActiveNav(current) {
  document.querySelectorAll("[data-nav]").forEach((el) => {
    const key = el.dataset.nav;
    const on = (key === "library" && ["library", "platform", "game", "search"].includes(current.name))
      || (key === "installed" && current.name === "installed")
      || (key === "emudeck" && ["emudeck", "settings"].includes(current.name));
    el.classList.toggle("active", on);
  });
  if (current.name === "setup") searchEl.value = "";
}

// ---------------------------------------------------------------- data ----
async function getPlatforms(force = false) {
  if (!state.platforms || force) {
    const data = await api.platforms();
    state.platforms = data.platforms || [];
  }
  return state.platforms;
}

async function getInstalls(force = false) {
  if (!state.installs || force) {
    const data = await api.installs();
    state.installs = data.installs || [];
    state.history = data.history || [];
  }
  return state.installs;
}

function platformFor(slug) {
  return (state.platforms || []).find((p) => (p.fs_slug || p.slug) === slug || p.slug === slug);
}

function pseudoRomFromInstall(entry) {
  return {
    id: entry.rom_id,
    name: entry.name,
    platform_name: platformFor(entry.platform)?.display_name || entry.platform || "",
    platform_fs_slug: entry.platform,
    size: entry.size,
    cover: entry.cover || true,
    installed: entry,
    mapping: platformFor(entry.platform)?.local || {},
  };
}

function mount(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((child) => child !== null && child !== undefined && child !== false));
}

// -------------------------------------------------------------- library ---
async function renderLibrary(token) {
  viewEl.replaceChildren(h("div", { class: "grid" }, Array.from({ length: 10 }, () => h("div", { class: "skel card" }))));
  const [platforms, installs, recent] = await Promise.all([
    getPlatforms(),
    getInstalls(),
    api.roms({ limit: 18, sort: "newest" }),
  ]);
  if (token !== state.renderToken) return;
  const roms = recent.items || [];
  const withGames = platforms.filter((p) => p.is_game_platform !== false && Number(p.game_count ?? p.rom_count) > 0);
  const playable = state.status?.emudeck?.enabled !== false;
  const featured = roms.find((r) => r.installed) || roms[0] || installs[0];

  const frag = document.createDocumentFragment();
  frag.append(heroBlock(featured, installs.length));
  frag.append(emudeckStrip(withGames));
  frag.append(platformRail(withGames, ""));
  if (roms.length) frag.append(rail("Recently added", `${recent.total} in library`, roms.map((r) => gameCard(withInstallJob(r), { showPlay: playable }))));
  if (installs.length) {
    frag.append(rail("Ready to play", `${installs.length} local`, installs.slice(0, 12).map((entry) => gameCard(withInstallJob(pseudoRomFromInstall(entry)), { showPlay: playable, eager: true }))));
  }
  if (!roms.length && !installs.length) {
    frag.append(emptyState({
      glyph: "cart",
      title: "This library is empty",
      body: "RomM has no games on this server yet, or the account cannot see any. Add games to RomM and refresh.",
      action: h("button", { class: "btn", onClick: () => reloadLibrary() }, icon("refresh"), "Refresh"),
    }));
  }
  viewEl.replaceChildren(frag);
}

function heroBlock(rom, localCount) {
  if (!rom) {
    return h("section", { class: "hero" },
      h("div", { class: "hero-empty" },
        h("p", { class: "eyebrow", style: { justifyContent: "center" } }, "No games yet"),
        h("h1", { class: "page-title" }, "Your arcade is waiting"),
        h("p", { class: "page-sub" }, "Connect RomMArcade to a RomM server with games in it, then install one and play it through EmuDeck."),
        h("div", { class: "row", style: { justifyContent: "center" } },
          h("a", { class: "btn primary", href: "#/settings/server" }, icon("server"), "Server settings"),
        ),
      ),
    );
  }
  const installed = Boolean(rom.installed);
  const playable = state.status?.emudeck?.enabled !== false;
  const canPlay = Boolean(rom.mapping?.launcher && rom.mapping?.launcher_ready !== false);
  const installJob = activeInstallJob(rom.id);
  return h("section", { class: "hero", dataset: { rom: rom.id } },
    h("div", { class: "hero-art" },
      rom.cover ? h("img", { src: `/api/cover/${rom.id}?size=large`, alt: "", loading: "eager" }) : null,
    ),
    h("div", { class: "hero-body" },
      h("div", { class: "hero-kicker" }, h("span", { class: "pulse" }), installed ? "Ready on this machine" : "Fresh from the library"),
      h("h1", { class: "hero-title" }, rom.name),
      h("div", { class: "hero-meta" },
        h("span", null, rom.platform_name || rom.platform_fs_slug || "RomM"),
        rom.size ? h("span", null, fmtBytes(rom.size)) : null,
        localCount ? h("span", null, h("b", null, String(localCount)), ` game${localCount === 1 ? "" : "s"} ready on this PC`) : null,
      ),
      h("div", { class: "hero-actions" },
        playable
          ? (installJob
            ? installProgressButton(installJob)
            : (installed
              ? h("button", { class: "btn primary", disabled: !canPlay, title: canPlay ? "" : "EmuDeck is needed to launch games", onClick: () => playRom(rom.id) }, icon("play"), "Play now")
              : h("button", { class: "btn primary", onClick: () => installRom(rom.id, { play: canPlay }) }, icon("download"), canPlay ? "Install & play" : "Install game")))
          : h("a", { class: "btn primary", href: "#/emudeck" }, icon("settings"), "Turn on EmuDeck"),
        h("a", { class: "btn ghost", href: `#/game/${rom.id}` }, "Details", icon("arrow")),
      ),
    ),
  );
}

function platformRail(platforms, activeSlug) {
  if (!platforms.length) return document.createDocumentFragment();
  return h("section", { class: "rail" },
    h("div", { class: "rail-head" },
      h("h2", null, "Platforms"),
      h("span", { class: "count" }, `${platforms.length}`),
      h("button", { class: "btn small ghost more", onClick: () => setRoute("emudeck") }, icon("settings"), "EmuDeck map"),
    ),
    h("div", { class: "rail-track" }, platforms.map((p) => platformCard(p, (p.fs_slug || p.slug) === activeSlug))),
  );
}

function emudeckStrip(platforms) {
  const emu = state.status?.emudeck || {};
  const library = emu.library || {};
  const enabled = emu.enabled !== false;
  const mapped = enabled ? platforms.filter((p) => p.local?.exists && p.local?.launcher).length : 0;
  const localCount = platforms.filter((p) => p.local?.exists).length;
  const detected = Boolean(emu.detected);
  if (!enabled) {
    return h("a", { class: "emu-strip missing", href: "#/emudeck" },
      h("span", { class: "glyph" }, icon("alert")),
      h("span", { class: "txt" },
        h("span", { class: "t" }, "EmuDeck integration off"),
        h("span", { class: "s" }, "The library is games-only. Turn it on to install into EmuDeck and launch games."),
      ),
      h("span", { class: "badge warn" }, "Off"),
      h("span", { class: "go" }, icon("arrow")),
    );
  }
  if (!detected && library.configured) {
    return h("a", { class: "emu-strip", href: "#/emudeck" },
      h("span", { class: "glyph" }, icon("folder")),
      h("span", { class: "txt" },
        h("span", { class: "t" }, "Game folders ready"),
        h("span", { class: "s" }, `${library.roms_root} · ${localCount} systems with folders · EmuDeck needed to launch`),
      ),
      h("span", { class: "badge local" }, "Local"),
      h("span", { class: "go" }, icon("arrow")),
    );
  }
  if (!detected) {
    return h("a", { class: "emu-strip missing", href: "#/emudeck" },
      h("span", { class: "glyph" }, icon("alert")),
      h("span", { class: "txt" },
        h("span", { class: "t" }, "No game folders yet"),
        h("span", { class: "s" }, "Choose where installs land — mirror your RomM server or pick a folder"),
      ),
      h("span", { class: "badge warn" }, "Setup"),
      h("span", { class: "go" }, icon("arrow")),
    );
  }
  return h("a", { class: "emu-strip", href: "#/emudeck" },
    h("span", { class: "glyph" }, icon("cart")),
    h("span", { class: "txt" },
      h("span", { class: "t" }, "EmuDeck connected"),
      h("span", { class: "s" }, `${emu.root} · ${emu.launchers} launchers · ${mapped} systems mapped`),
    ),
    h("span", { class: "badge local" }, "Ready"),
    h("span", { class: "go" }, icon("arrow")),
  );
}

function rail(title, subtitle, cards, extra = null) {
  return h("section", { class: "rail" },
    h("div", { class: "rail-head" },
      h("h2", null, title),
      subtitle ? h("span", { class: "count" }, subtitle) : null,
      extra ? h("div", { class: "more" }, extra) : null,
    ),
    h("div", { class: "grid" }, cards),
  );
}

async function reloadLibrary() {
  state.platforms = null;
  state.installs = null;
  await route();
}

// ------------------------------------------------------------- platform ---
async function renderPlatform(slug, token) {
  const platforms = await getPlatforms();
  const platform = platforms.find((p) => (p.fs_slug || p.slug) === slug || p.slug === slug);
  if (!platform) {
    throw new Error(`No platform called ${slug} in this library.`);
  }
  const sort = state.sort[slug] || "name";
  const data = await api.roms({ platform: platform.fs_slug || platform.slug, limit: 150, sort });
  if (token !== state.renderToken) return;

  const local = platform.local || {};
  const enabled = local.enabled !== false;
  const gameCount = Number(platform.game_count ?? platform.rom_count) || 0;
  const mapped = Boolean(enabled && local.exists && local.launcher);
  const head = h("div", { class: "page-head" },
    h("div", { class: "grow" },
      h("p", { class: "eyebrow" }, "Platform"),
      h("h1", { class: "page-title" }, platform.display_name || platform.name || slug),
      h("p", { class: "page-sub" },
        `${gameCount} game${gameCount === 1 ? "" : "s"} in the library`,
        local.installed_count ? ` · ${local.installed_count} installed here` : "",
        mapped ? ` · ${local.launcher_name}${local.core ? " / " + local.core : ""}` : "",
      ),
    ),
    h("div", { class: "page-actions" },
      enabled && Number(platform.firmware_count) > 0 && state.status?.emudeck?.usable
        ? h("button", { class: "btn", onClick: (event) => downloadBios(platform.id, event.currentTarget) }, icon("download"), "Download BIOS")
        : null,
      h("select", {
        class: "select",
        style: { width: "160px" },
        "aria-label": "Sort games",
        onChange: (event) => { state.sort[slug] = event.target.value; renderPlatform(slug, ++state.renderToken); },
      },
        ["name", "newest", "size"].map((value) => h("option", { value, selected: value === sort }, { name: "Name A–Z", newest: "Newest first", size: "Largest first" }[value])),
      ),
    ),
  );

  const body = [head];
  if (!enabled) {
    body.push(banner({
      tone: "warn",
      title: "EmuDeck integration is off",
      text: "Games are still browsable. Turn the integration on to install and launch them through EmuDeck.",
      action: h("a", { class: "btn small", href: "#/emudeck" }, "Open EmuDeck settings"),
    }));
  } else if (!local.exists) {
    const library = state.status?.emudeck?.library || {};
    body.push(banner({
      tone: "warn",
      title: library.configured ? "No folder for this platform" : "No game folder yet",
      text: library.configured
        ? `RomMArcade could not find a folder for ${platform.fs_slug} under ${library.roms_root}. Create one or map another folder for this system.`
        : "RomMArcade has nowhere to put games on this machine yet. Choose a folder that mirrors your RomM server, or one of your own.",
      action: h("a", { class: "btn small", href: "#/emudeck" }, library.configured ? "Set the mapping" : "Set up game folders"),
    }));
  } else if (!local.launcher && (state.status?.emudeck?.detected || !state.status?.emudeck?.library?.configured)) {
    body.push(banner({
      tone: "warn",
      title: "No launcher for this platform",
      text: "The game folder exists, but no installed emulator launcher maps to this system. Install it in EmuDeck or pick a launcher.",
      action: h("a", { class: "btn small", href: "#/emudeck" }, "Pick a launcher"),
    }));
  }
  body.push(data.items?.length
    ? h("div", { class: "grid" }, data.items.map((rom) => gameCard(withInstallJob(rom), { showPlay: enabled })))
    : emptyState({ glyph: "cart", title: "No games here yet", body: "This platform exists in RomM but has no visible games on this server." }));
  if (data.total > (data.items?.length || 0)) {
    body.push(h("div", { class: "row", style: { justifyContent: "center", marginTop: "26px" } },
      h("button", { class: "btn", onClick: (event) => loadMorePlatform(platform, data, event.currentTarget) }, "Load more", icon("arrow")),
    ));
  }
  viewEl.replaceChildren(...body);
}

async function loadMorePlatform(platform, data, button) {
  button.disabled = true;
  try {
    const more = await api.roms({ platform: platform.fs_slug || platform.slug, limit: 150, offset: data.items.length, sort: state.sort[platform.fs_slug] || "name" });
    const grid = viewEl.querySelector(".grid");
    const playable = state.status?.emudeck?.enabled !== false;
    (more.items || []).forEach((rom) => grid.append(gameCard(withInstallJob(rom), { showPlay: playable })));
    data.items.push(...(more.items || []));
    if (data.items.length >= data.total) button.parentElement.remove();
  } catch (error) { toast(error.message, "bad"); }
  finally { button.disabled = false; }
}

function banner({ tone = "warn", title, text, action }) {
  return h("div", { class: `bios-banner ${tone === "warn" ? "" : tone}` },
    icon("alert"),
    h("div", { class: "txt" }, h("b", null, title), h("div", null, text)),
    action || null,
  );
}

// ----------------------------------------------------------------- game ---
async function renderGame(id, token) {
  viewEl.replaceChildren(h("div", { class: "skel card", style: { height: "360px" } }));
  const [detail, installs, platforms, saves] = await Promise.all([
    api.rom(id), getInstalls(), getPlatforms(),
    api.romSaves(id).catch(() => null),
  ]);
  if (token !== state.renderToken) return;
  const rom = detail.rom;
  const platform = platforms.find((p) => p.id === rom.platform_id);
  const installed = rom.installed;
  const mapping = rom.mapping || {};
  let firmware = [];
  if (platform && Number(platform.firmware_count) > 0) {
    try { firmware = (await api.firmware(platform.id)).firmware || []; } catch { firmware = []; }
  }
  if (token !== state.renderToken) return;
  const missingBios = firmware.filter((f) => !f.installed);

  const art = h("div", { class: "art" }, rom.cover
    ? h("img", { src: `/api/cover/${rom.id}?size=large`, alt: "", loading: "eager" })
    : null);
  if (!rom.cover) art.append(h("div", { class: "cover-fallback" }, icon("cart"), h("div", { class: "name" }, rom.name)));

  const actions = [];
  const emuOn = mapping.enabled !== false;
  const library = state.status?.emudeck?.library || {};
  const libraryMode = Boolean(library.configured) && !state.status?.emudeck?.detected;
  const canPlay = Boolean(mapping.launcher && mapping.launcher_ready !== false);
  const missingEmulator = emuOn && Boolean(mapping.launcher) && mapping.launcher_ready === false;
  const launcherName = mapping.launcher_name || mapping.launcher || "Emulator";
  const emuInstaller = state.status?.emudeck?.installer || "";
  const installJob = activeInstallJob(rom.id);
  if (!emuOn) {
    actions.push(h("a", { class: "btn", href: "#/emudeck" }, icon("settings"), "Turn on EmuDeck integration"));
    if (installed) {
      actions.push(h("button", { class: "btn danger", onClick: () => openUninstall(rom) }, icon("trash"), "Uninstall"));
    }
  } else if (installJob) {
    actions.push(installProgressButton(installJob));
  } else if (installed) {
    actions.push(h("button", {
      class: "btn primary",
      disabled: !canPlay,
      title: !canPlay ? (mapping.launcher ? `Install ${launcherName} first` : libraryMode ? "EmuDeck is needed to launch games" : "No launcher is mapped") : "",
      dataset: { playRom: String(rom.id) },
      onClick: () => playRom(rom.id),
    }, icon("play"), "Play"));
    actions.push(h("span", { class: "chip ok" }, icon("check"), "Installed"));
    actions.push(h("button", { class: "btn danger", onClick: () => openUninstall(rom) }, icon("trash"), "Uninstall"));
  } else if (mapping.exists && mapping.launcher) {
    if (missingEmulator) {
      actions.push(h("button", { class: "btn primary", onClick: () => installRom(rom.id) }, icon("download"), "Install game"));
    } else {
      actions.push(h("button", { class: "btn primary", onClick: () => installRom(rom.id, { play: true }) }, icon("download"), "Install & play"));
      actions.push(h("button", { class: "btn", onClick: () => installRom(rom.id) }, "Install only"));
    }
  } else if (mapping.exists) {
    actions.push(h("button", { class: "btn primary", onClick: () => installRom(rom.id) }, icon("download"), "Install game"));
  } else {
    actions.push(h("a", { class: "btn", href: "#/emudeck" }, icon("settings"), library.configured ? "Fix this platform's folder" : "Set up game folders"));
  }
  actions.push(h("a", { class: "btn ghost", href: `#/platform/${encodeURIComponent(rom.platform_fs_slug || "")}` }, "Back to platform"));

  const facts = h("div", { class: "facts" },
    fact("Platform", rom.platform_name || rom.platform_fs_slug),
    fact("Size", rom.size ? fmtBytes(rom.size) : "Unknown"),
    fact("Game folder", mapping.dir || "Unmapped", mapping.exists ? "" : "bad"),
    fact("Launcher", mapping.launcher_name || mapping.launcher || "Not set", mapping.launcher ? "" : "bad"),
    fact("Core", mapping.core || (mapping.launcher === "retroarch" ? "Auto" : "—")),
    fact("File", rom.target_name || rom.file_name || "—", "", true),
    fact("Install path", rom.target || "—", "", true, true),
    fact("Files", String((rom.files || []).length)),
  );

  const body = h("div", { class: "game-hero" },
    art,
    h("div", null,
      h("p", { class: "eyebrow" }, rom.platform_name || "Game"),
      h("h1", { class: "game-title" }, rom.name),
      h("div", { class: "game-meta" },
        rom.size ? h("span", { class: "chip" }, fmtBytes(rom.size)) : null,
        rom.has_multiple_files ? h("span", { class: "chip" }, "Multi-file") : null,
        installed ? h("span", { class: "chip ok" }, icon("check"), "On this machine") : h("span", { class: "chip" }, "Cloud"),
        rom.release_year ? h("span", { class: "chip" }, String(rom.release_year)) : null,
        rom.players ? h("span", { class: "chip" }, rom.players) : null,
      ),
      h("div", { class: "game-actions" }, actions),
      h("div", { class: "play-via" }, icon("cart"),
        !emuOn
          ? "EmuDeck integration is off — browsing only"
          : mapping.launcher
            ? `Plays through EmuDeck · ${mapping.launcher_name || mapping.launcher}${mapping.core ? " · " + mapping.core : ""}`
            : libraryMode
              ? `Installs to ${mapping.path || library.roms_root} · EmuDeck is needed to launch`
              : "No EmuDeck launcher mapped yet"),
      missingEmulator
        ? h("div", { class: "bios-banner" },
            icon("alert"),
            h("div", { class: "txt" },
              h("b", null, `${launcherName} is not installed`),
              h("div", null, emuInstaller
                ? "Install it with EmuDeck, then rescan this game."
                : `Install it from the ${launcherName} website, then rescan this game.`)),
            h("div", { class: "row" },
              emuInstaller
                ? h("button", { class: "btn small", onClick: openEmudeckInstaller }, icon("external"), "Install with EmuDeck")
                : mapping.launcher_url
                  ? h("a", { class: "btn small", href: mapping.launcher_url, target: "_blank", rel: "noreferrer" }, icon("external"), `Get ${launcherName}`)
                  : h("a", { class: "btn small", href: "#/emudeck" }, icon("settings"), "Emulator settings"),
              h("button", { class: "btn small ghost", onClick: (event) => rescanEmulator(event.currentTarget) }, icon("refresh"), "Rescan"),
            ),
          )
        : null,
      emuOn && missingBios.length
        ? h("div", { class: "bios-banner" },
            icon("alert"),
            h("div", { class: "txt" }, h("b", null, `${missingBios.length} BIOS file${missingBios.length === 1 ? "" : "s"} missing`),
              h("div", null, `${rom.platform_name || "This platform"} needs firmware in this machine's BIOS folder before some games will boot.`)),
            state.status?.emudeck?.usable
              ? h("button", { class: "btn small", onClick: (event) => downloadBios(platform.id, event.currentTarget) }, icon("download"), "Download BIOS")
              : h("a", { class: "btn small", href: "#/emudeck" }, icon("settings"), "Set up game folders"),
          )
        : null,
      rom.summary ? h("p", { class: "game-summary" }, rom.summary) : null,
      facts,
      savesSection(rom, saves),
      (rom.files || []).length ? fileListSection(rom.files) : null,
    ),
  );
  mount(viewEl, body);
  if (state.autoPlay && Number(state.autoPlay.romId) === Number(rom.id)) {
    const request = state.autoPlay;
    state.autoPlay = null;
    if (request.play) {
      if (missingEmulator) {
        // The game is usable, but the launcher needs an emulator first.
      } else if (installed) playRom(rom.id);
      else if (mapping.exists) installRom(rom.id, { play: canPlay });
    }
  }
}

function fact(key, value, tone = "", mono = false, wide = false) {
  return h("div", { class: `fact ${wide ? "wide" : ""}` },
    h("div", { class: "k" }, key),
    h("div", { class: `v ${mono ? "mono" : ""} ${tone}` }, value || "—"),
  );
}

const FILE_PREVIEW = 6;

// --------------------------------------------------------------- saves ---
const SAVE_PREVIEW = 8;

function refreshSavesViews() {
  document.querySelectorAll("[data-saves]").forEach((node) => node.__refresh?.());
}

function savesSection(rom, initial) {
  const wrap = h("div", { class: "saves-section", "data-saves": String(rom.id) });
  let data = initial;
  let expanded = false;

  const refresh = async () => {
    try {
      data = await api.romSaves(rom.id);
      draw();
    } catch (error) {
      toast(error.message, "bad");
    }
  };
  wrap.__refresh = refresh;

  const act = async (button, run, message) => {
    if (button) {
      button.disabled = true;
      button.classList.add("busy");
    }
    try {
      await run();
      if (message) toast(message, "good");
      await refresh();
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove("busy");
      }
    }
  };

  function openRename(save) {
    const input = h("input", { class: "input", type: "text", value: save.file_name, maxlength: "450" });
    const saveButton = h("button", { class: "btn primary" }, "Rename");
    saveButton.addEventListener("click", async () => {
      const name = input.value.trim();
      if (!name) return;
      saveButton.disabled = true;
      try {
        await api.renameSave(save.id, name);
        closeModal();
        toast("Save renamed", "good");
        await refresh();
      } catch (error) {
        saveButton.disabled = false;
        toast(error.message, "bad");
      }
    });
    openModal(h("div", { class: "sheet" },
      h("h2", null, "Rename this save"),
      h("p", null, "Only the name RomM stores changes; the save itself stays as it is."),
      input,
      h("div", { class: "row end" },
        h("button", { class: "btn ghost", onClick: closeModal }, "Cancel"),
        saveButton,
      ),
    ));
    input.focus();
    input.select();
  }

  function openLabels(save) {
    const input = h("input", { class: "input", type: "text", value: (save.labels || []).join(", "), placeholder: "before-boss, ending" });
    const saveButton = h("button", { class: "btn primary" }, "Save labels");
    saveButton.addEventListener("click", async () => {
      saveButton.disabled = true;
      try {
        const labels = input.value.split(",").map((label) => label.trim()).filter(Boolean);
        await api.saveLabels(save.id, labels);
        closeModal();
        await refresh();
      } catch (error) {
        saveButton.disabled = false;
        toast(error.message, "bad");
      }
    });
    openModal(h("div", { class: "sheet" },
      h("h2", null, "Labels for this save"),
      h("p", null, "Comma-separated, so a save can say what it is for."),
      input,
      h("div", { class: "row end" },
        h("button", { class: "btn ghost", onClick: closeModal }, "Cancel"),
        saveButton,
      ),
    ));
    input.focus();
  }

  function openDelete(save) {
    const confirm = h("button", { class: "btn danger" }, icon("trash"), "Delete save");
    confirm.addEventListener("click", async () => {
      confirm.disabled = true;
      try {
        await api.deleteSave(save.id);
        closeModal();
        toast("Save deleted from RomM", "good");
        await refresh();
      } catch (error) {
        confirm.disabled = false;
        toast(error.message, "bad");
      }
    });
    openModal(h("div", { class: "sheet" },
      h("h2", null, "Delete this save?"),
      h("p", null, `${save.file_name} is removed from RomM. Other versions in the slot stay, and the save on this machine is not touched.`),
      h("div", { class: "row end" },
        h("button", { class: "btn ghost", onClick: closeModal }, "Cancel"),
        confirm,
      ),
    ));
  }

  function serverRow(save) {
    const chips = h("span", { class: "chips" },
      h("span", { class: "chip" }, save.slot || "archive"),
      save.current ? h("span", { class: "chip ok" }, "current") : null,
      save.matches_local ? h("span", { class: "chip" }, "here") : null,
      save.public ? h("span", { class: "chip" }, "public") : null,
      ...(save.labels || []).map((label) => h("span", { class: "chip" }, label)),
    );
    const actions = h("span", { class: "acts" });
    if (rom.installed) {
      actions.append(h("button", {
        class: "btn small primary",
        title: "Restore this save here, make it the synced save, and launch the game",
        onClick: (event) => act(event.currentTarget, () => api.restoreSave(rom.id, save.id, true), `Playing with ${save.file_name}`),
      }, icon("play"), "Use & play"));
    }
    actions.append(
      h("button", {
        class: "btn small",
        title: "Restore this save here and sync from it, without launching",
        onClick: (event) => act(event.currentTarget, () => api.restoreSave(rom.id, save.id, false), `This machine now uses ${save.file_name}`),
      }, icon("download"), "Use"),
      h("a", { class: "icon-btn", href: `/api/saves/${save.id}/download`, title: "Download to this computer" }, icon("download")),
      h("button", {
        class: `icon-btn star ${save.favorite ? "on" : ""}`,
        title: save.favorite ? "Remove from favourites" : "Add to favourites",
        onClick: (event) => act(event.currentTarget, () => api.saveFavorite(save.id, !save.favorite), save.favorite ? "Removed from favourites" : "Added to favourites"),
      }, icon("star")),
      h("button", { class: "icon-btn", title: "Rename", onClick: () => openRename(save) }, icon("pencil")),
      h("button", { class: "icon-btn", title: "Labels", onClick: () => openLabels(save) }, icon("server")),
      h("button", {
        class: "icon-btn",
        title: save.public ? "Make private" : "Make public",
        onClick: (event) => act(event.currentTarget, () => api.saveVisibility(save.id, !save.public), save.public ? "Save is private" : "Save is public"),
      }, icon("external")),
      h("button", { class: "icon-btn danger", title: "Delete from RomM", onClick: () => openDelete(save) }, icon("trash")),
    );
    return h("div", { class: `save-row ${save.favorite ? "fav" : ""} ${save.current ? "current" : ""}` },
      h("div", { class: "save-main" },
        h("div", { class: "save-title" },
          h("span", { class: "n mono", title: save.file_name }, save.file_name),
          chips,
        ),
        h("div", { class: "save-meta" },
          h("span", null, save.emulator || "unknown emulator"),
          h("span", null, fmtBytes(save.size)),
          h("span", { title: save.updated_at || "" }, save.updated_at ? timeAgo(save.updated_at) : ""),
        ),
      ),
      actions,
    );
  }

  function openNewSlot() {
    const input = h("input", { class: "input", type: "text", placeholder: "before-boss", maxlength: "64" });
    const seed = h("input", { type: "checkbox", checked: true });
    const create = h("button", { class: "btn primary" }, "Create slot");
    create.addEventListener("click", async () => {
      const name = input.value.trim();
      if (!name) {
        input.focus();
        return;
      }
      create.disabled = true;
      try {
        data = await api.createSaveSlot(rom.id, name, seed.checked);
        closeModal();
        const created = data.created || {};
        toast(created.seeded
          ? `Slot ${created.slot} now holds this machine's save`
          : `Slot ${created.slot} is ready for the next save`, "good");
        draw();
      } catch (error) {
        create.disabled = false;
        toast(error.message, "bad");
      }
    });
    openModal(h("div", { class: "sheet" },
      h("h2", null, "New sync slot"),
      h("p", null, "A named history of this game's saves. This machine switches to it, so saves go there instead of the old slot."),
      input,
      h("label", { class: "switch", style: { margin: "14px 0" } },
        seed,
        h("span", null, "Copy the save on this machine into it"),
      ),
      h("div", { class: "row end" },
        h("button", { class: "btn ghost", onClick: closeModal }, "Cancel"),
        create,
      ),
    ));
    input.focus();
  }

  function draw() {
    if (!data) {
      mount(wrap,
        h("div", { class: "section-title" }, "Saves"),
        h("p", { class: "note" }, "Saves could not be loaded from RomM."));
      return;
    }
    const header = h("div", { class: "saves-head" },
      h("div", { class: "section-title" }, "Saves"),
      data.supported === false
        ? h("span", { class: "chip" }, "Sync off")
        : data.sync?.enabled === false
          ? h("span", { class: "chip" }, "Sync off")
          : h("span", { class: "chip ok" }, icon("check"), "Sync on"),
    );
    if (data.supported === false) {
      mount(wrap, header,
        h("p", { class: "note" }, data.reason || "This RomM server does not offer save sync."));
      return;
    }
    const slots = new Set(["autosave", data.slot, ...(data.slots || []).map((entry) => entry.slot).filter(Boolean)]);
    const slotSelect = h("select", { class: "select save-slot", title: "The slot this game syncs on this machine" },
      [...slots].sort().map((slot) => h("option", { value: slot, selected: slot === data.slot }, slot)),
    );
    slotSelect.addEventListener("change", async () => {
      slotSelect.disabled = true;
      try {
        data = await api.setSaveSlot(rom.id, slotSelect.value);
        toast(`Now syncing slot ${slotSelect.value}`, "good");
        draw();
      } catch (error) {
        toast(error.message, "bad");
      } finally {
        slotSelect.disabled = false;
      }
    });
    const local = data.local || [];
    const localBlock = local.length
      ? h("div", { class: "save-list" }, local.map((save) => h("div", { class: "save-row local" },
          h("div", { class: "save-main" },
            h("div", { class: "save-title" },
              h("span", { class: "n mono", title: save.path }, save.name),
              h("span", { class: "chips" },
                save.main ? h("span", { class: "chip ok" }, "playing") : h("span", { class: "chip" }, save.slot),
                save.dir ? h("span", { class: "chip" }, "folder") : null,
              ),
            ),
            h("div", { class: "save-meta" },
              h("span", null, fmtBytes(save.size)),
              h("span", null, save.mtime ? timeAgo(save.mtime) : ""),
            ),
          ),
          h("span", { class: "acts" },
            h("button", {
              class: "btn small",
              title: "Upload this file to RomM and sync from it",
              onClick: (event) => act(event.currentTarget, () => api.uploadLocalSave(rom.id, save.name), `Uploaded ${save.name}`),
            }, icon("upload"), "Upload"),
          ),
        )))
      : h("p", { class: "note" }, "No save for this game on this machine yet.");
    const rows = data.server || [];
    const shown = expanded ? rows : rows.slice(0, SAVE_PREVIEW);
    const serverBlock = rows.length
      ? h("div", { class: "save-list" }, shown.map(serverRow))
      : h("p", { class: "note" }, "No saves for this game on RomM yet. Play it, or upload the save from this machine.");
    mount(wrap,
      header,
      h("p", { class: "note" }, "Saves are pulled before the game starts and pushed when the emulator closes. Pick any version to make it the one this machine plays and syncs."),
      h("div", { class: "save-slot-row" },
        h("span", { class: "k" }, "Sync slot"),
        slotSelect,
        h("button", { class: "btn small", title: "Create a new sync slot for this game", onClick: openNewSlot }, icon("plus"), "New slot"),
        h("span", { class: "note" }, data.device && data.device !== "token" ? `device ${String(data.device).slice(0, 8)}` : "this machine's token device"),
      ),
      h("div", { class: "section-title small" }, "On this machine"),
      localBlock,
      h("div", { class: "section-title small" }, "On RomM"),
      serverBlock,
      rows.length > SAVE_PREVIEW
        ? h("button", { class: "btn small ghost file-more", onClick: () => { expanded = !expanded; draw(); } },
            expanded ? `Show first ${SAVE_PREVIEW}` : `Show all ${rows.length} saves`)
        : null,
    );
  }

  draw();
  return wrap;
}

function fileListSection(files) {
  const wrap = h("div");
  let expanded = false;
  const draw = () => {
    const shown = expanded ? files : files.slice(0, FILE_PREVIEW);
    mount(wrap,
      h("div", { class: "section-title" }, `Files · ${files.length}`),
      h("div", { class: `file-list ${expanded ? "scrollable" : ""}` }, shown.map((file) => h("div", { class: "file-row" },
        icon("folder"),
        h("span", { class: "n", title: file.file_path || file.file_name || "" }, file.file_name || "file"),
        h("span", null, file.size ? fmtBytes(file.size) : "")))),
      files.length > FILE_PREVIEW
        ? h("button", {
            class: "btn small ghost file-more",
            onClick: () => { expanded = !expanded; draw(); },
          }, expanded ? `Show first ${FILE_PREVIEW}` : `Show all ${files.length} files`)
        : null,
    );
  };
  draw();
  return wrap;
}

// ------------------------------------------------------------ installed ---
async function renderInstalled(token) {
  const [installs, history] = await Promise.all([
    getInstalls(true),
    api.history().then((data) => data.history || []).catch(() => []),
  ]);
  try { await getPlatforms(); } catch { /* mapping stays empty */ }
  if (token !== state.renderToken) return;
  const libraryMode = Boolean(state.status?.emudeck?.library?.configured) && !state.status?.emudeck?.detected;
  const head = h("div", { class: "page-head" },
    h("div", { class: "grow" },
      h("p", { class: "eyebrow" }, "On this machine"),
      h("h1", { class: "page-title" }, "Installed"),
      h("p", { class: "page-sub" }, installs.length
        ? `${installs.length} game${installs.length === 1 ? "" : "s"} ready ${libraryMode ? "in your game folders" : "in EmuDeck folders"}${state.status?.emudeck?.enabled === false ? " (integration off)" : ""}`
        : "Nothing installed by RomMArcade yet"),
    ),
    h("div", { class: "page-actions" },
      h("button", { class: "btn", onClick: repairInstalls }, icon("refresh"), "Check files"),
    ),
  );
  const body = [head];
  if (installs.length) {
    const playable = state.status?.emudeck?.enabled !== false;
    body.push(h("div", { class: "grid" }, installs.map((entry) => gameCard(withInstallJob(pseudoRomFromInstall(entry)), { showPlay: playable, eager: true }))));
  } else {
    body.push(emptyState({
      glyph: "download",
      title: "No installs yet",
      body: "Open any game and press Install. RomMArcade puts it in the right game folder and remembers it here.",
      action: h("a", { class: "btn primary", href: "#/library" }, "Browse the library"),
    }));
  }
  if (history.length) {
    body.push(h("div", { class: "section-title" }, "Recently played"));
    body.push(h("div", { class: "file-list" }, history.slice(0, 12).map((entry) => h("div", { class: "file-row" },
      icon("clock"),
      h("span", { class: "n" }, entry.name),
      h("span", null, entry.platform || ""),
      h("span", null, timeAgo(entry.played_at)),
    ))));
  }
  viewEl.replaceChildren(...body);
}

async function repairInstalls() {
  try {
    const result = await api.repairInstalls();
    state.installs = result.installs;
    toast(result.removed ? `Removed ${result.removed} missing install${result.removed === 1 ? "" : "s"}` : "Every install still exists on disk", "good");
    renderInstalled(++state.renderToken);
  } catch (error) { toast(error.message, "bad"); }
}

// --------------------------------------------------------------- search ---
async function renderSearch(query, token) {
  const q = (query || "").trim();
  if (!q) { location.hash = "#/library"; return; }
  const data = await api.roms({ search: q, limit: 150, sort: "name" });
  if (token !== state.renderToken) return;
  const head = h("div", { class: "page-head" },
    h("div", { class: "grow" },
      h("p", { class: "eyebrow" }, "Search"),
      h("h1", { class: "page-title" }, `“${q}”`),
      h("p", { class: "page-sub" }, `${data.total} match${data.total === 1 ? "" : "es"} in this RomM library`),
    ),
  );
  viewEl.replaceChildren(head, data.items?.length
    ? h("div", { class: "grid" }, data.items.map((rom) => gameCard(withInstallJob(rom), { showPlay: state.status?.emudeck?.enabled !== false })))
    : emptyState({ glyph: "search", title: "Nothing matched", body: "Try a shorter title, or search for the platform name." }));
}

async function onSearch(event) {
  const value = event.target.value.trim();
  const current = parseRoute();
  if (!value) {
    if (current.name === "search") location.hash = "#/library";
    return;
  }
  if (current.name === "search" && current.args.join("/") === value) return;
  setRoute("search", value);
}

// -------------------------------------------------------------- actions ---
function openUninstall(rom) {
  const removeSaves = h("input", { type: "checkbox", checked: true });
  const confirm = h("button", { class: "btn danger" }, icon("trash"), "Uninstall");
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    confirm.textContent = "Removing…";
    try {
      const result = await api.uninstall(rom.id, removeSaves.checked);
      closeModal();
      state.installs = null;
      state.platforms = null;
      const saveNote = result.saves_removed
        ? ` and removed ${result.saves_removed} save${result.saves_removed === 1 ? "" : "s"}`
        : "";
      toast(`Uninstalled ${rom.name}${saveNote}`, "good");
      await route();
    } catch (error) {
      confirm.disabled = false;
      confirm.replaceChildren(icon("trash"), "Uninstall");
      toast(error.message, "bad");
    }
  });
  openModal(h("div", { class: "sheet" },
    h("h2", null, `Uninstall ${rom.name}?`),
    h("p", null, "This removes the installed game from this machine's game folder. It cannot be undone from RomMArcade."),
    h("label", { class: "switch", style: { margin: "18px 0" } },
      removeSaves,
      h("span", null, "Also remove saves and save states"),
    ),
    h("div", { class: "row end" },
      h("button", { class: "btn ghost", onClick: closeModal }, "Cancel"),
      confirm,
    ),
  ));
}

async function installRom(romId, { play = false } = {}) {
  try {
    const result = await api.install(romId);
    if (result.download?.already) {
      toast(`${result.download.name} is already installed`, "good");
      if (play) playRom(romId);
      return;
    }
    state.pendingPlay.set(Number(romId), play);
    toast(`Installing ${result.download?.name || "game"}`, "info");
    state.trayHidden = 0;
    if (result.download?.id) {
      state.downloads = [result.download, ...state.downloads.filter((job) => job.id !== result.download.id)];
      applyInstallToView(Number(romId), result.download);
    }
    await pollDownloads();
  } catch (error) {
    toast(error.message, "bad");
  }
}

function setPlayBusy(romId, busy) {
  document.querySelectorAll(`[data-play-rom="${Number(romId)}"]`).forEach((el) => {
    if (busy) {
      if (!el.dataset.idleHtml) el.dataset.idleHtml = el.innerHTML;
      el.dataset.idleTitle = el.title;
      el.disabled = true;
      el.classList.add("syncing");
      el.title = "Syncing saves…";
      const spinner = h("span", { class: "spinner", "aria-hidden": "true" });
      if (el.classList.contains("card-play")) el.replaceChildren(spinner);
      else el.replaceChildren(spinner, "Syncing saves…");
    } else {
      el.disabled = false;
      el.classList.remove("syncing");
      if (el.dataset.idleHtml) {
        el.innerHTML = el.dataset.idleHtml;
        delete el.dataset.idleHtml;
      }
      if (el.dataset.idleTitle !== undefined) {
        el.title = el.dataset.idleTitle;
        delete el.dataset.idleTitle;
      }
    }
  });
}

function openSyncProblem(romId, error) {
  const summary = error.payload?.save_sync || {};
  const conflict = Boolean(error.payload?.save_conflict);
  const serverUrl = state.status?.server?.url || "";
  const close = h("button", { class: "btn primary", onClick: closeModal }, "Close");
  const actions = [close];
  if (serverUrl) {
    actions.unshift(h("a", {
      class: "btn",
      href: `${serverUrl}/saves`,
      target: "_blank",
      rel: "noreferrer",
    }, "Open RomM saves"));
  }
  openModal(h("div", { class: "sheet" },
    h("h2", null, conflict ? "Saves changed in both places" : "Saves did not sync"),
    h("p", null, error.message),
    h("p", { class: "note" },
      conflict
        ? "Nothing was overwritten and the emulator was not started. RomM keeps each version of a save, so you can pick the copy to keep there."
        : "The emulator was not started, so the game cannot open on an older save than RomM holds."),
    summary.reason ? h("p", { class: "note" }, summary.reason) : null,
    h("div", { class: "row end" }, actions),
  ));
}

async function playRom(romId) {
  setPlayBusy(romId, true);
  try {
    const result = await api.play(romId);
    const sync = result.saves || {};
    const pulled = Number(sync.downloaded) || 0;
    const pushed = Number(sync.uploaded) || 0;
    const note = pulled
      ? ` · ${pulled} save${pulled === 1 ? "" : "s"} synced`
      : pushed ? ` · ${pushed} save${pushed === 1 ? "" : "s"} uploaded` : "";
    toast(`Launching ${result.launcher || "the emulator"}${result.core ? " (" + result.core + ")" : ""}${note}`, "good");
    refreshSavesViews();
  } catch (error) {
    if (error.payload?.save_conflict || error.payload?.save_sync) {
      openSyncProblem(romId, error);
    } else if (error.payload?.needs_install) {
      openModal(h("div", { class: "sheet" },
        h("h2", null, "Not installed yet"),
        h("p", null, error.message),
        h("div", { class: "row end" },
          h("button", { class: "btn ghost", onClick: closeModal }, "Cancel"),
          h("button", { class: "btn primary", onClick: () => { closeModal(); installRom(romId, { play: true }); } }, icon("download"), "Install & play"),
        ),
      ));
    } else {
      toast(error.message, "bad");
    }
  } finally {
    setPlayBusy(romId, false);
  }
}

async function downloadBios(platformId, button) {
  if (button) button.disabled = true;
  try {
    const result = await api.bios(platformId);
    const jobs = result.downloads || [];
    if (!jobs.length) toast(`BIOS already present (${result.skipped} file${result.skipped === 1 ? "" : "s"})`, "good");
    else {
      const batch = state.biosBatch || { ids: new Set(), done: 0, failed: 0, total: 0 };
      for (const job of jobs) {
        batch.ids.add(job.id);
        batch.total += 1;
      }
      state.biosBatch = batch;
      state.trayHidden = 0;
      await pollDownloads();
    }
  } catch (error) { toast(error.message, "bad"); }
  finally { if (button) button.disabled = false; }
}

function onDocumentClick(event) {
  const stop = event.target.closest("[data-stop]");
  if (stop) event.stopPropagation();
  const serverPill = event.target.closest('[data-action="server-status"]');
  if (serverPill) showServerStatus();
  const action = event.target.closest("[data-action]");
  if (action) {
    const name = action.dataset.action;
    if (name === "install" && action.dataset.rom) installRom(Number(action.dataset.rom), { play: action.dataset.play === "1" });
    if (name === "play" && action.dataset.rom) playRom(Number(action.dataset.rom));
    if (name === "refresh") { action.classList.add("spin"); reloadLibrary().finally(() => action.classList.remove("spin")); }
  }
}

async function showServerStatus() {
  let status = state.status?.server || {};
  try { status = (await api.config()).status || status; } catch { /* keep cached */ }
  const rows = [
    ["Server", status.url || "Not configured"],
    ["State", status.reachable ? "Reachable" : (status.error || "Unreachable")],
    ["Version", status.version || "—"],
    ["User", status.user ? `${status.user.username} (${status.user.role})` : "—"],
    ["Library", status.platforms != null ? `${status.platforms} platforms` : "—"],
  ];
  openModal(h("div", { class: "sheet" },
    h("h2", null, "Server status"),
    h("div", { class: "file-list", style: { margin: "16px 0 20px" } }, rows.map(([k, v]) => h("div", { class: "file-row" },
      h("span", { style: { width: "88px", color: "var(--muted-2)" } }, k),
      h("span", { class: "n" }, String(v)),
    ))),
    h("div", { class: "row end" },
      h("a", { class: "btn ghost", href: "#/settings/server", onClick: closeModal }, icon("settings"), "Server settings"),
      h("button", { class: "btn primary", onClick: closeModal }, "Done"),
    ),
  ));
}

function onKey(event) {
  const tag = (event.target?.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea" || tag === "select" || event.target?.isContentEditable;
  if (event.key === "/" && !typing) {
    event.preventDefault();
    searchEl.focus();
    searchEl.select();
  }
  if (event.key === "Escape" && document.activeElement === searchEl) {
    searchEl.value = "";
    searchEl.blur();
    if (parseRoute().name === "search") location.hash = "#/library";
  }
}

// ------------------------------------------------------------ downloads ---
function activeInstallJob(romId) {
  return state.downloads.find((job) => Number(job.rom_id) === Number(romId)
    && ["queued", "downloading", "extracting"].includes(job.status));
}

function withInstallJob(rom) {
  const job = activeInstallJob(rom.id);
  return job ? { ...rom, installing: true, install_job: job } : rom;
}

function installProgressButton(job) {
  const label = installLabel(job);
  return h("button", {
    class: "btn primary installing",
    disabled: true,
    "data-install-progress": job.rom_id,
    title: label,
  },
    h("span", { class: "spinner", "aria-hidden": "true" }),
    h("span", { "data-install-label": "", role: "status", "aria-live": "polite" }, label),
  );
}

function playButtonFor(romId, label = "Play", { enabled = true, title = "" } = {}) {
  return h("button", {
    class: "btn primary",
    disabled: !enabled,
    title: title || (enabled ? "" : "EmuDeck is needed to launch games"),
    dataset: { playRom: String(romId) },
    onClick: () => playRom(romId),
  }, icon("play"), label);
}

function platformCanPlay(slug) {
  const mapping = platformFor(slug)?.local || {};
  return Boolean(mapping.launcher && mapping.launcher_ready !== false);
}

function cardPlayButtonFor(romId, name = "") {
  return h("button", {
    class: "card-play",
    "data-stop": "",
    dataset: { playRom: String(romId) },
    title: "Play",
    "aria-label": `Play ${name}`,
    onClick: (event) => {
      event.stopPropagation();
      playRom(romId);
    },
  }, icon("play"));
}

function markInstalledInView(romId, name = "", platform = "") {
  const canPlay = platform ? platformCanPlay(platform) : true;
  const current = parseRoute();
  if (current.name === "game" && Number(current.args[0]) === Number(romId)) {
    const actions = document.querySelector(".game-actions");
    if (actions) {
      const back = [...actions.querySelectorAll("a")].find((link) => (link.getAttribute("href") || "").startsWith("#/platform/"));
      actions.replaceChildren(
        playButtonFor(romId, "Play", { enabled: canPlay }),
        h("span", { class: "chip ok" }, icon("check"), "Installed"),
        h("button", { class: "btn danger", onClick: () => openUninstall({ id: romId, name }) }, icon("trash"), "Uninstall"),
      );
      if (back) actions.append(back);
    }
  }
  const heroActions = document.querySelector(`.hero[data-rom="${romId}"] .hero-actions`);
  const heroPrimary = heroActions?.querySelector(".btn.primary");
  if (heroPrimary) heroPrimary.replaceWith(playButtonFor(romId, "Play now", { enabled: canPlay }));
  document.querySelectorAll(`.game-card[data-rom="${romId}"]`).forEach((card) => {
    card.classList.remove("installing");
    const button = card.querySelector(".card-play.installing");
    if (button) {
      if (canPlay) button.replaceWith(cardPlayButtonFor(romId, card.getAttribute("aria-label") || ""));
      else button.remove();
    }
    card.querySelector("[data-install-badge]")?.remove();
    if (!card.querySelector(".badge.local")) {
      card.querySelector(".badges")?.prepend(h("span", { class: "badge local" }, "Local"));
    }
  });
}

function applyInstallToView(romId, job) {
  if (!job) return;
  const card = document.querySelector(`.game-card[data-rom="${romId}"]`);
  if (card) {
    card.classList.add("installing");
    const play = card.querySelector(".card-play");
    if (play && !play.classList.contains("installing")) {
      play.replaceWith(h("button", {
        class: "card-play installing",
        disabled: true,
        "data-install-rom": romId,
        title: installLabel(job),
        "aria-label": `${installLabel(job)}: ${card.getAttribute("aria-label") || ""}`,
      }, h("span", { class: "spinner", "aria-hidden": "true" })));
    }
    const badges = card.querySelector(".badges");
    if (badges && !badges.querySelector(".badge.cloud")) {
      badges.append(h("span", { class: "badge cloud", "data-install-badge": romId }, installLabel(job)));
    }
  }
  const current = parseRoute();
  if (current.name === "game" && Number(current.args[0]) === Number(romId)) {
    const actions = document.querySelector(".game-actions");
    if (actions) {
      const back = [...actions.querySelectorAll("a")].find((link) => (link.getAttribute("href") || "").startsWith("#/platform/"));
      actions.replaceChildren(installProgressButton(job));
      if (back) actions.append(back);
    }
  }
  const heroActions = document.querySelector(`.hero[data-rom="${romId}"] .hero-actions`);
  const primary = heroActions?.querySelector(".btn.primary");
  if (primary) primary.replaceWith(installProgressButton(job));
}

function syncInstallState() {
  document.querySelectorAll("[data-install-progress]").forEach((el) => {
    const job = activeInstallJob(el.dataset.installProgress);
    if (!job) return;
    const label = installLabel(job);
    el.title = label;
    const text = el.querySelector("[data-install-label]");
    if (text) text.textContent = label;
  });
  document.querySelectorAll("[data-install-rom]").forEach((el) => {
    const job = activeInstallJob(el.dataset.installRom);
    if (job) el.title = installLabel(job);
  });
  document.querySelectorAll("[data-install-badge]").forEach((el) => {
    const job = activeInstallJob(el.dataset.installBadge);
    if (job) el.textContent = installLabel(job);
  });
  const current = parseRoute();
  if (current.name !== "game") return;
  const job = activeInstallJob(current.args[0]);
  const hasProgress = document.querySelector("[data-install-progress]");
  if (!job && hasProgress) {
    const doneJob = state.downloads.find((entry) => Number(entry.rom_id) === Number(current.args[0])
      && entry.status === "done");
    if (doneJob) markInstalledInView(current.args[0], doneJob.name, doneJob.platform);
    else route();
    return;
  }
  if (job && !hasProgress && !state.installRenderJobs.has(job.id)) {
    state.installRenderJobs.add(job.id);
    route();
  }
}

async function pollDownloads() {
  try {
    const data = await api.downloads();
    state.downloads = data.downloads || [];
  } catch { return; }
  if (!state.downloadsPrimed) {
    // The server keeps completed jobs around for the tray.  On the first poll
    // mark old terminal jobs as already seen so they do not replay toasts or
    // force a route while a fresh install is on screen.
    state.downloadsPrimed = true;
    for (const job of state.downloads) {
      if (["done", "error", "canceled"].includes(job.status)
        && Number(job.started_at || 0) < state.sessionStart) {
        state.seenDone.add(job.id);
        state.seenErrors.add(job.id);
      }
    }
  }
  for (const job of state.downloads) {
    if (job.kind === "bios" && job.status === "done") noteBiosFinished(job);
    if (job.status === "done" && !state.seenDone.has(job.id)) {
      state.seenDone.add(job.id);
      if (job.kind === "rom") {
        toast(`${job.name} installed`, "good");
        state.installs = null;
        const current = parseRoute();
        const wantsPlay = state.pendingPlay.get(Number(job.rom_id));
        state.pendingPlay.delete(Number(job.rom_id));
        markInstalledInView(job.rom_id, job.name, job.platform);
        if (wantsPlay) playRom(job.rom_id);
        // A game page is already correct after the in-place swap.  Other
        // views still need a refresh so the new install appears in their rails.
        if (current.name !== "settings" && current.name !== "game") route();
      }
    }
    if (job.status === "error" && !state.seenErrors.has(job.id)) {
      state.seenErrors.add(job.id);
      toast(`${job.name}: ${job.error || "download failed"}`, "bad");
      state.pendingPlay.delete(Number(job.rom_id));
      if (job.kind === "bios" && state.biosBatch?.ids.has(job.id)) {
        state.biosBatch.ids.delete(job.id);
        state.biosBatch.failed += 1;
        if (!state.biosBatch.ids.size) finishBiosBatch();
      }
      if (parseRoute().name === "game" && Number(parseRoute().args[0]) === Number(job.rom_id)) route();
    }
    if (job.status === "canceled" && !state.seenErrors.has(job.id)) {
      state.seenErrors.add(job.id);
      toast(`${job.name}: install canceled`, "info");
      state.pendingPlay.delete(Number(job.rom_id));
      if (job.kind === "bios" && state.biosBatch?.ids.has(job.id)) {
        state.biosBatch.ids.delete(job.id);
        state.biosBatch.failed += 1;
        if (!state.biosBatch.ids.size) finishBiosBatch();
      }
      if (parseRoute().name === "game" && Number(parseRoute().args[0]) === Number(job.rom_id)) route();
    }
  }
  syncInstallState();
  renderTray();
}

function scheduleBiosRefresh() {
  clearTimeout(state.biosTimer);
  state.biosTimer = setTimeout(() => {
    const current = parseRoute();
    if (current.name === "game" || current.name === "platform") route();
  }, 600);
}

function noteBiosFinished(job) {
  const batch = state.biosBatch;
  if (!batch || !batch.ids.has(job.id)) return;
  batch.ids.delete(job.id);
  batch.done += 1;
  if (!batch.ids.size) finishBiosBatch();
}

function finishBiosBatch() {
  const batch = state.biosBatch;
  if (!batch) return;
  state.biosBatch = null;
  const saved = batch.done;
  const failed = batch.failed;
  if (saved && !failed) {
    toast(`${saved} BIOS file${saved === 1 ? "" : "s"} saved`, "good");
  } else if (saved) {
    toast(`${saved} BIOS file${saved === 1 ? "" : "s"} saved, ${failed} failed`, "info");
  } else if (failed) {
    toast(`${failed} BIOS file${failed === 1 ? "" : "s"} failed`, "bad");
  }
  scheduleBiosRefresh();
}

function renderTray() {
  const jobs = state.downloads.filter((job) => ["queued", "downloading", "extracting", "error"].includes(job.status)
    || (job.status === "done" && Date.now() - (job.finished_at || 0) * 1000 < 20000));
  if (!jobs.length || Date.now() < state.trayHidden) {
    trayEl.hidden = true;
    return;
  }
  trayEl.hidden = false;
  const active = jobs.filter((job) => ["queued", "downloading", "extracting"].includes(job.status)).length;
  trayEl.replaceChildren(
    h("div", { class: "tray-head" },
      icon("download"),
      h("span", null, active ? "Downloading" : "Downloads"),
      h("span", { class: "count" }, String(jobs.length)),
      h("button", {
        class: "icon-btn small",
        title: "Hide until the next download",
        "aria-label": "Hide downloads",
        onClick: () => { state.trayHidden = Date.now() + 60000; renderTray(); },
      }, icon("close")),
    ),
    h("div", { class: "jobs" }, jobs.map((job) => {
      const pct = job.status === "done" ? 100 : Math.round((job.progress || 0) * 100);
      const note = job.status === "error" ? job.error
        : job.status === "extracting" ? "Extracting archive"
        : job.status === "queued" ? "Queued"
        : job.status === "done" ? "Installed"
        : `${fmtBytes(job.received)} of ${job.total ? fmtBytes(job.total) : "?"}${job.speed ? " · " + fmtSpeed(job.speed) : ""}`;
      return h("div", { class: `job ${job.status}` },
        h("div", { class: "job-top" },
          h("span", { class: "job-name" }, job.name),
          h("span", { class: "job-pct" }, job.status === "extracting" ? "ZIP" : `${pct}%`),
          ["queued", "downloading", "extracting"].includes(job.status)
            ? h("button", { class: "x", title: "Cancel", "aria-label": "Cancel download", onClick: () => cancelJob(job.id) }, icon("close", "icon"))
            : null,
        ),
        h("div", { class: "bar" }, h("i", { style: { width: `${pct}%` } })),
        h("div", { class: "note" }, note),
      );
    })),
  );
}

async function cancelJob(id) {
  try {
    await api.cancelDownload(id);
    await pollDownloads();
  } catch (error) { toast(error.message, "bad"); }
}

async function pollDeeplink() {
  if (!state.status?.configured) return;
  try {
    const { target } = await api.deeplink();
    if (!target) return;
    handleDeepLink(target);
  } catch { /* service busy */ }
}

function handleDeepLink(target) {
  const label = target.name ? `“${target.name}”` : `ROM ${target.rom}`;
  toast(`RomMArcade opening ${label}`, "info", 2600);
  if (target.server) {
    state.platforms = null;
    state.installs = null;
  }
  state.autoPlay = { romId: target.rom, play: Boolean(target.play) };
  setRoute("game", String(target.rom));
}

// --------------------------------------------------------------- setup ----
function setupState() {
  if (!state.setup) {
    state.setup = {
      step: 1,
      url: state.config?.servers?.[0]?.url || "",
      authMode: "pair",
      token: "",
      username: "",
      password: "",
      probe: null,
      pair: null,
      pairing: false,
      error: "",
      emudeckOn: true,
      busy: false,
    };
  }
  return state.setup;
}

async function renderSetup() {
  const setup = setupState();
  const steps = h("div", { class: "steps" },
    [1, 2, 3].map((n) => h("span", { class: `step-dot ${setup.step > n ? "done" : setup.step === n ? "now" : ""}` })),
  );
  const card = h("div", { class: "setup-card" });
  if (setup.step === 1) setupServerStep(card, setup);
  else if (setup.step === 2) await setupAuthStep(card, setup);
  else await setupEmudeckStep(card, setup);
  viewEl.replaceChildren(
    h("div", { class: "setup" },
      h("div", { class: "setup-brand" },
        h("div", { class: "mark" }, h("span", null, "RomM"), h("span", null, "Arcade")),
        h("p", null, "Point it at a RomM server, pair this machine, and let EmuDeck do the playing."),
      ),
      steps,
      card,
    ),
  );
}

function setupServerStep(card, setup) {
  const input = h("input", {
    class: "input",
    type: "url",
    placeholder: "http://romm.local:7676",
    value: setup.url,
    autocomplete: "url",
    spellcheck: "false",
    onInput: (event) => { setup.url = event.target.value; },
    onKeydown: (event) => { if (event.key === "Enter") testSetupServer(setup, card); },
  });
  setTimeout(() => input.focus(), 30);
  mount(card, 
    h("h2", null, "Where is your RomM server?"),
    h("p", { class: "lead" }, "RomMArcade talks to RomM (or its ROMulus fork) over its HTTP API. Use the address you open in a browser, including the port, and any sub-path."),
    h("div", { class: "field" }, h("label", { for: "setupUrl" }, "Server URL"), input),
    setup.error ? h("p", { class: "hint bad", style: { marginTop: "10px" } }, setup.error) : null,
    setup.probe ? h("p", { class: "hint good", style: { marginTop: "10px" } },
      `RomM ${setup.probe.version || ""} answered${setup.probe.user ? " as " + setup.probe.user.username : ""}.`) : null,
    h("div", { class: "setup-actions" },
      h("button", { class: "btn primary", disabled: setup.busy, onClick: () => testSetupServer(setup, card) },
        setup.busy ? h("span", { class: "spinner" }) : icon("arrow"), "Continue"),
    ),
  );
  input.id = "setupUrl";
}

async function testSetupServer(setup, card) {
  setup.error = "";
  setup.probe = null;
  setup.busy = true;
  setupServerStep(card, setup);
  try {
    const auth = setup.authMode === "token" ? { mode: "token", token: setup.token }
      : setup.authMode === "password" ? { mode: "password", username: setup.username, password: setup.password }
      : { mode: "none" };
    const result = await api.testServer({ url: setup.url, auth });
    setup.probe = result.server;
    setup.url = result.server.url;
    setup.step = 2;
  } catch (error) {
    setup.error = error.message;
  } finally {
    setup.busy = false;
    renderSetup();
  }
}

async function setupAuthStep(card, setup) {
  const choice = (mode, title, subtitle) => h("div", {
    class: `auth-option ${setup.authMode === mode ? "active" : ""}`,
    onClick: () => { setup.authMode = mode; setup.pair = null; setup.pairing = false; renderSetup(); },
  }, h("span", { class: "radio" }), h("div", null, h("div", { class: "t" }, title), h("div", { class: "s" }, subtitle)));

  const widgets = [];
  if (setup.authMode === "pair") {
    if (setup.pair) {
      widgets.push(h("div", { class: "pair-box" },
        h("div", { class: "hint", style: { textAlign: "center" } }, "Enter this code in RomM to approve RomMArcade"),
        h("div", { class: "pair-code" }, setup.pair.user_code),
        h("a", { class: "pair-url", href: setup.pair.verification_url, target: "_blank", rel: "noreferrer" }, setup.pair.verification_url),
        h("div", { class: "pair-wait" }, setup.pairing ? h("span", { class: "spinner" }) : null,
          setup.pairing ? "Waiting for approval…" : (setup.error || "Approval needed")),
      ));
    } else {
      widgets.push(h("p", { class: "hint", style: { marginBottom: "12px" } },
        "RomM shows a short code; approve it there and this machine receives its own token. No password is stored."));
    }
  } else if (setup.authMode === "token") {
    widgets.push(h("div", { class: "field" },
      h("label", { for: "setupToken" }, "Client token"),
      h("input", {
        class: "input", id: "setupToken", type: "password", placeholder: "rmm_…", autocomplete: "off",
        onInput: (event) => { setup.token = event.target.value; },
      }),
      h("div", { class: "hint" }, "Create one in RomM under Settings, Client tokens. RomMArcade asks for roms.read, platforms.read, assets.read, firmware.read and devices."),
    ));
  } else if (setup.authMode === "password") {
    widgets.push(h("div", { class: "form-grid" },
      h("div", { class: "field" }, h("label", { for: "setupUser" }, "Username"),
        h("input", { class: "input", id: "setupUser", autocomplete: "username", value: setup.username, onInput: (event) => { setup.username = event.target.value; } })),
      h("div", { class: "field" }, h("label", { for: "setupPass" }, "Password"),
        h("input", { class: "input", id: "setupPass", type: "password", autocomplete: "current-password", onInput: (event) => { setup.password = event.target.value; } })),
    ));
    widgets.push(h("p", { class: "hint", style: { marginTop: "10px" } }, "Stored in RomMArcade's config file (mode 600) and sent as HTTP Basic to RomM."));
  }

  const canContinue = !setup.pairing;
  mount(card, 
    h("h2", null, "How should this machine sign in?"),
    h("p", { class: "lead" }, "Pairing is the cleanest: RomM issues a token for RomMArcade alone and you can revoke it later."),
    h("div", { class: "auth-choice" },
      choice("pair", "Pair this device", "Approve a short code in RomM's browser UI"),
      choice("token", "Paste a client token", "Already have an rmm_ token?"),
      choice("password", "Use username and password", "HTTP Basic, stored locally"),
    ),
    ...widgets,
    setup.error ? h("p", { class: "hint bad" }, setup.error) : null,
    h("div", { class: "row", style: { marginTop: "6px" } },
      h("button", { class: "btn ghost small", onClick: () => { setup.step = 1; setup.error = ""; renderSetup(); } }, "Back"),
      h("div", { class: "spacer" }),
      setup.authMode === "pair" && !setup.pair
        ? h("button", { class: "btn primary", disabled: setup.busy, onClick: () => startPairing(setup) }, icon("server"), "Start pairing")
        : canContinue
          ? h("button", { class: "btn primary", disabled: setup.busy, onClick: () => finishAuth(setup) }, icon("arrow"), "Continue")
          : null,
    ),
  );
  if (setup.pairing) schedulePairPoll(setup);
}

async function startPairing(setup) {
  setup.busy = true;
  setup.error = "";
  try {
    setup.pair = await api.pairStart({ url: setup.url });
    setup.pairing = true;
    setup.pollStart = Date.now();
  } catch (error) {
    setup.error = error.message;
  } finally {
    setup.busy = false;
    renderSetup();
  }
}

let pairTimer = null;
function schedulePairPoll(setup) {
  clearTimeout(pairTimer);
  if (!setup.pairing || !setup.pair) return;
  const interval = Math.max(3, Number(setup.pair.interval) || 5) * 1000;
  if (Date.now() - setup.pollStart > (Number(setup.pair.expires_in) || 600) * 1000) {
    setup.pairing = false;
    setup.error = "The code expired. Start pairing again.";
    renderSetup();
    return;
  }
  pairTimer = setTimeout(async () => {
    try {
      const result = await api.pairPoll({ device_code: setup.pair.device_code });
      if (result.status === "approved") {
        setup.pairing = false;
        setup.pair = null;
        state.config = (await api.config()).config;
        state.status = await api.status();
        paintServerPill();
        toast(`Paired as ${result.user?.username || "a RomM user"}`, "good");
        setup.step = 3;
        renderSetup();
        return;
      }
      if (result.status === "denied" || result.status === "expired") {
        setup.pairing = false;
        setup.error = result.status === "denied" ? "The request was denied in RomM." : "The code expired. Start again.";
        renderSetup();
        return;
      }
    } catch (error) {
      setup.error = error.message;
    }
    schedulePairPoll(setup);
  }, interval);
}

async function finishAuth(setup) {
  setup.busy = true;
  setup.error = "";
  try {
    const auth = setup.authMode === "token"
      ? { mode: "token", token: setup.token }
      : { mode: "password", username: setup.username, password: setup.password };
    if (setup.authMode !== "pair") {
      const probe = await api.testServer({ url: setup.url, auth });
      setup.probe = probe.server;
    }
    const result = await api.saveConfig({ server: { url: setup.url, auth } });
    state.config = result.config;
    state.status = await api.status();
    paintServerPill();
    setup.step = 3;
  } catch (error) {
    setup.error = error.message;
  } finally {
    setup.busy = false;
    renderSetup();
  }
}

function libraryCard(lib, { onEdit = null, onRemove = null } = {}) {
  const total = (lib?.platforms || []).length;
  const mapped = Number(lib?.mapped || 0);
  return h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("div", { class: "grow" },
        h("h3", null, "Game folders"),
        h("p", { class: "sub" }, lib?.mode === "romm"
          ? "Mirrors your RomM server's own layout on this machine."
          : "Your own folder, one subfolder per system."),
      ),
      onEdit ? h("button", { class: "btn small", onClick: onEdit }, icon("settings"), "Change") : null,
    ),
    h("div", { class: "emu-status" },
      h("div", { class: "glyph" }, icon("folder")),
      h("div", { class: "txt" },
        h("div", { class: "t" }, lib?.root || "No folder"),
        h("div", { class: "s" }, `ROMs ${lib?.roms_root || "—"}`),
        h("div", { class: "s" }, `BIOS ${lib?.bios_root || "—"}`),
        h("div", { class: "s" }, total ? `${mapped} of ${total} systems have a folder` : "No platforms with games yet"),
      ),
      h("span", { class: `badge ${lib?.exists !== false ? "local" : "warn"}` }, lib?.exists !== false ? "Ready" : "Missing"),
    ),
    lib?.exists === false
      ? h("p", { class: "hint bad", style: { marginTop: "10px" } }, "That folder is not on this machine right now. Reconnect the drive, or choose another folder.")
      : null,
    onRemove ? h("div", { class: "row end", style: { marginTop: "12px" } },
      h("button", { class: "btn small ghost", onClick: onRemove }, "Clear this folder"),
    ) : null,
  );
}

function libraryChooser(container, lib, { onDone = null, onCancel = null } = {}) {
  const suggestions = lib?.suggestions || {};
  const rows = (lib?.platforms || []).filter((row) => Number(row.games) > 0);
  const editing = Boolean(lib?.configured);
  const ui = {
    mode: editing && lib?.mode === "custom" ? "custom" : "romm",
    rommRoot: editing && lib?.mode === "romm" && lib?.root ? lib.root : (suggestions.romm || "~/RomM"),
    customRoot: editing && lib?.mode === "custom" && lib?.root ? lib.root : (suggestions.custom || "~/ROMs"),
    busy: false,
    error: "",
    done: false,
  };
  const systems = rows.length;

  const create = async () => {
    if (ui.busy) return;
    const root = String((ui.mode === "romm" ? ui.rommRoot : ui.customRoot) || "").trim();
    if (!root) { ui.error = "Choose a folder for the games."; draw(); return; }
    ui.busy = true;
    ui.error = "";
    draw();
    try {
      const result = await api.saveLibrary({ mode: ui.mode, root, create: true });
      state.status = await api.status();
      state.platforms = null;
      state.installs = null;
      state.emudeck = null;
      paintServerPill();
      const created = result.created?.length || 0;
      toast(created ? `Created ${created} game folder${created === 1 ? "" : "s"}` : "Game folders ready", "good");
      ui.done = true;
      if (onDone) onDone();
      else route();
    } catch (error) {
      ui.error = error.message;
    } finally {
      ui.busy = false;
      if (!ui.done) draw();
    }
  };

  const option = (mode, title, subtitle) => h("div", {
    class: `auth-option ${ui.mode === mode ? "active" : ""}`,
    onClick: () => { if (ui.busy) return; ui.mode = mode; ui.error = ""; draw(); },
  },
    h("span", { class: "radio" }),
    h("div", null, h("div", { class: "t" }, title), h("div", { class: "s" }, subtitle)),
  );

  const draw = () => {
    const rootValue = ui.mode === "romm" ? ui.rommRoot : ui.customRoot;
    const clean = String(rootValue || "").trim().replace(/\/+$/, "");
    const romPreview = ui.mode === "romm" ? `${clean}/roms/<system>` : `${clean}/<system>`;
    const biosPreview = `${clean}/bios`;
    const input = h("input", {
      class: "input",
      id: "libraryRoot",
      type: "text",
      spellcheck: "false",
      autocomplete: "off",
      placeholder: ui.mode === "romm" ? "/home/you/RomM" : "/home/you/ROMs",
      value: rootValue,
      disabled: ui.busy,
      onInput: (event) => {
        if (ui.mode === "romm") ui.rommRoot = event.target.value;
        else ui.customRoot = event.target.value;
      },
      onKeydown: (event) => { if (event.key === "Enter") create(); },
    });
    if (!ui.busy) setTimeout(() => input.focus(), 30);
    mount(container,
      h("h2", null, editing ? "Choose a different game folder" : "Where should your games live?"),
      h("p", { class: "lead" }, editing
        ? "Pick the layout you want from now on. Existing folders and installed games are left alone; missing system folders are added."
        : "This machine has no game folders yet. RomMArcade can copy the folder layout your RomM server uses, or create one in a folder you choose. You can also set this up later — browsing works either way."),
      h("div", { class: "auth-choice" },
        option("romm", "Same as my RomM server", "Mirrors the server's own layout: <root>/roms/<system> and <root>/bios"),
        option("custom", "My own folder", "One <system> folder per platform directly inside a folder you pick"),
      ),
      h("div", { class: "field" },
        h("label", { for: "libraryRoot" }, ui.mode === "romm" ? "Library root" : "Games folder"),
        input,
      ),
      h("div", { class: "hint", style: { marginTop: "10px", lineHeight: "1.7" } },
        systems
          ? `${systems} system folder${systems === 1 ? "" : "s"} from your RomM library will be created.`
          : "Folders are created from the platforms in your RomM library.",
        h("br"),
        h("code", null, romPreview), " · BIOS in ", h("code", null, biosPreview),
      ),
      ui.error ? h("p", { class: "hint bad", style: { marginTop: "10px" } }, ui.error) : null,
      h("div", { class: "setup-actions" },
        onCancel ? h("button", { class: "btn ghost", disabled: ui.busy, onClick: onCancel }, "Cancel") : null,
        onDone ? h("button", { class: "btn ghost", disabled: ui.busy, onClick: () => { ui.done = true; onDone(); } }, "Set up later") : null,
        h("button", { class: "btn primary", disabled: ui.busy, onClick: create },
          ui.busy ? h("span", { class: "spinner" }) : icon("folder"),
          ui.mode === "romm" ? "Create server folders" : "Create folders"),
      ),
    );
  };

  draw();
}

async function setupEmudeckStep(card, setup) {
  let emu = null;
  let lib = null;
  try { emu = await api.emudeck(); state.emudeck = emu; } catch { /* optional */ }
  try { lib = await api.library(); } catch { /* optional */ }
  if (emu?.detected) {
    const platforms = emu?.platforms?.filter((p) => p.dir)?.length || 0;
    mount(card,
      h("h2", null, "Give it a place to play"),
      h("p", { class: "lead" }, "RomMArcade uses the EmuDeck you already have. It installs games into EmuDeck's ROM folders and starts EmuDeck's own launchers, so saves and BIOS stay where EmuDeck expects them."),
      h("div", { class: "emu-status" },
        h("div", { class: "glyph" }, icon("folder")),
        h("div", { class: "txt" },
          h("div", { class: "t" }, "EmuDeck found"),
          h("div", { class: "s" }, emu.root),
          h("div", { class: "s" }, `${emu.launchers.filter((l) => l.ready).length} launchers · ${platforms} platforms mapped · ${emu.cores.length} RetroArch cores`),
        ),
        h("span", { class: "badge local" }, "Ready"),
      ),
      h("div", { class: "setup-actions" },
        h("a", { class: "btn ghost", href: "#/library", onClick: () => finishSetup(setup, false) }, "Skip for now"),
        h("button", { class: "btn primary", onClick: () => finishSetup(setup, true) }, icon("play"), "Enter the arcade"),
      ),
    );
    return;
  }
  if (lib?.configured) {
    mount(card,
      h("h2", null, "Games have a home"),
      h("p", { class: "lead" }, "This machine keeps its games outside EmuDeck, so installs work right away. EmuDeck is only needed when you want RomMArcade to launch a game."),
      libraryCard(lib),
      h("div", { class: "setup-actions" },
        h("button", { class: "btn primary", onClick: () => finishSetup(setup, true) }, icon("play"), "Enter the arcade"),
      ),
    );
    return;
  }
  libraryChooser(card, lib, { onDone: () => finishSetup(setup, true) });
}

async function finishSetup(setup, save) {
  if (save) {
    try { await api.saveConfig({ emudeck: { enabled: Boolean(setup.emudeckOn) } }); } catch { /* keep going */ }
  }
  state.setup = null;
  state.status = await api.status();
  state.platforms = null;
  state.installs = null;
  if (!state.status.configured) {
    toast("No server is configured yet", "bad");
    return;
  }
  location.hash = "#/library";
  await route();
}

// ------------------------------------------------------------- settings ---
async function renderEmudeck(token) {
  let emu = state.emudeck;
  if (!emu) {
    try { emu = await api.emudeck(); state.emudeck = emu; } catch { emu = null; }
  }
  let platforms = [];
  try { platforms = await getPlatforms(); } catch { platforms = []; }
  const enabled = emu?.enabled !== false;
  const library = emu?.library || {};
  const libraryMode = Boolean(library.configured) && !emu?.detected;
  const canOpen = Boolean(emu?.detected && emu?.installer);
  const mapped = platforms.filter((p) => p.is_game_platform !== false && Number(p.game_count ?? p.rom_count) > 0 && p.local?.exists && (p.local?.launcher || libraryMode)).length;
  const head = h("div", { class: "page-head" },
    h("div", { class: "grow" },
      h("p", { class: "eyebrow" }, "Integration"),
      h("h1", { class: "page-title" }, "EmuDeck"),
      h("p", { class: "page-sub" }, libraryMode
        ? "The library is games only. This machine keeps games in its own folders; EmuDeck is only needed to launch them."
        : "The library is games only. EmuDeck owns the emulators, the ROM folders and the launchers; RomMArcade hands it the right file."),
    ),
    h("div", { class: "page-actions" },
      h("button", {
        class: "btn small",
        onClick: openEmudeckInstaller,
        disabled: !canOpen,
        title: canOpen ? "Open the EmuDeck installer" : "EmuDeck was not found",
      }, icon("external"), "Open EmuDeck"),
      emudeckSwitch(enabled),
      h("span", { class: `chip ${enabled ? "ok" : ""}` }, enabled ? "On" : "Off"),
      enabled ? h("span", { class: `chip ${emu?.detected ? "ok" : libraryMode ? "" : "warn"}` }, emu?.detected ? "Detected" : libraryMode ? "Game folder" : "Not found") : null,
      enabled ? h("span", { class: "chip" }, `${mapped} systems mapped`) : null,
    ),
  );

  if (!enabled) {
    mount(viewEl, head, h("div", { class: "settings-body" }, emudeckDisabledCard()));
    return;
  }

  if (!emu?.detected) {
    let libSnap = null;
    try { libSnap = await api.library(); } catch { libSnap = null; }
    const body = h("div", { class: "settings-body" });
    mount(viewEl, head, body);
    if (libSnap?.configured || library.configured) {
      const holder = h("div");
      const snapshot = libSnap || library;
      const showChooser = () => {
        holder.replaceChildren();
        libraryChooser(holder, libSnap || snapshot, { onCancel: () => route() });
      };
      holder.append(libraryCard(snapshot, { onEdit: showChooser }));
      body.append(holder,
        h("p", { class: "hint", style: { marginTop: "4px" } },
          "EmuDeck is not installed on this machine, so RomMArcade can install games into these folders but cannot launch them yet. Install EmuDeck later and press Rescan to hand launching back to it."),
      );
    } else {
      const chooserCard = h("div", { class: "setup-card" });
      libraryChooser(chooserCard, libSnap, {});
      body.append(chooserCard);
    }
    await settingsEmulators(body, token);
    return;
  }

  // A dead variant beside a working launcher for the same emulator (EmuDeck's
  // xenia-emu next to xenia) is not a missing emulator.
  const dead = (emu?.launchers || []).filter((launcher) => !launcher.ready && !launcher.superseded);
  const missing = platforms.filter((p) => p.is_game_platform !== false && Number(p.game_count ?? p.rom_count) > 0 && !p.local?.launcher);
  const warnings = [];
  if (dead.length) {
    const deadNames = [...new Set(dead.map((l) => l.name))];
    warnings.push(h("div", { class: "bios-banner" },
      icon("alert"),
      h("div", { class: "txt" }, h("b", null, `${deadNames.length} emulator${deadNames.length === 1 ? "" : "s"} without an app`),
        h("div", null, `EmuDeck left ${deadNames.join(", ")} behind with no app to run. Install the missing emulators in EmuDeck.`)),
    ));
  } else if (missing.length) {
    warnings.push(h("div", { class: "bios-banner" },
      icon("alert"),
      h("div", { class: "txt" }, h("b", null, `${missing.length} game system${missing.length === 1 ? "" : "s"} without an emulator`),
        h("div", null, "Games on those systems can be browsed but not installed or played until EmuDeck has an emulator for them.")),
    ));
  }

  const body = h("div", { class: "settings-body" });
  body.append(h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("div", { class: "grow" },
        h("h3", null, "How RomMArcade uses EmuDeck"),
        h("p", { class: "sub" }, "No second emulator setup: the ROMs, launchers and BIOS are the ones EmuDeck already manages."),
      ),
    ),
    h("div", { class: "emu-how" },
      howStep("Install", "Games are written to EmuDeck's ROM folders; multi-file games become a folder.", emu?.roms_root ? `${emu.roms_root}/<system>` : "~/Emulation/roms/<system>"),
      howStep("Play", "The matching EmuDeck launcher runs with the exact ROM path; RetroArch gets -L <core>.", emu?.launchers_root ? `${emu.launchers_root}/<emu>.sh` : "~/Emulation/tools/launchers/<emu>.sh"),
      howStep("BIOS", "RomM firmware is mirrored under EmuDeck's BIOS root, preserving bios/<system>.", emu?.bios_root || "~/Emulation/bios"),
    ),
    h("p", { class: "hint", style: { marginTop: "12px" } },
      "Installing or updating an emulator stays in EmuDeck itself — RomMArcade never installs software behind your back."),
  ));
  if (warnings.length) body.append(...warnings);
  mount(viewEl, head, body);
  await settingsEmulators(body, token);
}

function emudeckSwitch(enabled) {
  return h("label", { class: "switch", title: "Toggle EmuDeck integration" },
    h("input", {
      type: "checkbox",
      checked: enabled,
      onChange: (event) => setEmudeckEnabled(event.target.checked),
    }),
    h("span", null, enabled ? "EmuDeck on" : "EmuDeck off"),
  );
}

function emudeckDisabledCard() {
  return h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("div", { class: "grow" },
        h("h3", null, "EmuDeck integration is off"),
        h("p", { class: "sub" }, "RomMArcade is a pure RomM library browser right now: no installs, no launches, no BIOS downloads. The library still only shows games. Turn it back on with the EmuDeck toggle on the EmuDeck page."),
      ),
    ),
  );
}

async function setEmudeckEnabled(enabled) {
  try {
    const result = await api.saveConfig({ emudeck: { enabled: Boolean(enabled) } });
    state.config = result.config;
    state.status = await api.status();
    state.platforms = null;
    state.installs = null;
    state.emudeck = null;
    paintServerPill();
    toast(enabled ? "EmuDeck integration is on" : "EmuDeck integration is off", "good");
    route();
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function openEmudeckInstaller() {
  try {
    const result = await api.openInstaller();
    toast(`Opened ${result.path.split("/").pop()}`, "good");
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function rescanEmulator(button) {
  if (button) button.disabled = true;
  try {
    await api.rescanEmudeck();
    state.platforms = null;
    state.emudeck = null;
    state.status = await api.status();
    toast("Emulator scan refreshed", "good");
    route();
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    if (button) button.disabled = false;
  }
}

function howStep(label, text, target) {
  return h("div", { class: "how-step" },
    h("div", { class: "k" }, label),
    h("div", { class: "v" }, text),
    h("div", { class: "p mono" }, target),
  );
}

async function renderSettings(tab, token) {
  const def = ["server", "emulators", "about"].includes(tab) ? tab : "server";
  const tabs = h("nav", { class: "tabs", "aria-label": "Settings" },
    [[ "server", "Server", "server"], ["emulators", "EmuDeck", "cart"], ["about", "About", "grid"]].map(([key, label, glyph]) =>
      h("a", { class: `tab ${def === key ? "active" : ""}`, href: `#/settings/${key}` }, icon(glyph), label)),
  );
  const body = h("div", { class: "settings-body" });
  viewEl.replaceChildren(
    h("div", { class: "page-head" },
      h("div", { class: "grow" },
        h("p", { class: "eyebrow" }, "Setup"),
        h("h1", { class: "page-title" }, "Settings"),
        h("p", { class: "page-sub" }, "Server, EmuDeck and how RomMArcade behaves on this machine."),
      ),
    ),
    h("div", { class: "settings-grid" }, tabs, body),
  );
  if (def === "server") await settingsServer(body, token);
  else if (def === "emulators") await settingsEmulators(body, token);
  else settingsAbout(body);
}

async function settingsServer(body, token) {
  const cfg = state.config || (await api.config()).config;
  const server = state.status?.server || {};
  const activeId = cfg.active_server;
  const list = (cfg.servers || []).map((s) => h("div", { class: "server-row" },
    h("div", { class: "info" },
      h("div", { class: "name" }, s.name || hostOf(s.url)),
      h("div", { class: "url" }, s.url),
    ),
    s.id === activeId ? h("span", { class: "pillty" }, "Active") : h("button", { class: "btn small ghost", onClick: () => activateServer(s.id) }, "Use"),
    h("button", { class: "icon-btn small", title: "Remove server", "aria-label": `Remove ${s.url}`, onClick: () => removeServer(s.id) }, icon("trash")),
  ));

  const authMode = h("select", { class: "select", id: "srvAuthMode" },
    [["pair", "Pair a new token"], ["token", "Client token"], ["password", "Username & password"]].map(([v, l]) => h("option", { value: v }, l)),
  );
  const activeEntry = (cfg.servers || []).find((item) => item.id === activeId) || {};
  const existingAuth = activeEntry.auth || {};
  authMode.value = existingAuth.mode === "token" ? "token" : existingAuth.mode === "password" ? "password" : "pair";
  const urlInput = h("input", { class: "input", id: "srvUrl", placeholder: "http://192.168.86.30:7676", value: server.url || "", spellcheck: "false" });
  const nameInput = h("input", { class: "input", id: "srvName", placeholder: "Living-room RomM", value: server.name || "" });
  const tokenInput = h("input", { class: "input", id: "srvToken", type: "password", placeholder: existingAuth.has_secret ? "•••••••• stored — leave blank to keep" : "rmm_…", autocomplete: "off" });
  const userInput = h("input", { class: "input", id: "srvUser", placeholder: "james", autocomplete: "username", value: existingAuth.mode === "password" ? existingAuth.username || "" : "" });
  const passInput = h("input", { class: "input", id: "srvPass", type: "password", autocomplete: "current-password", placeholder: existingAuth.has_secret ? "•••••••• stored — leave blank to keep" : "" });
  const authFields = h("div", { class: "form-grid", id: "srvAuthFields" });
  const statusLine = h("p", { class: "hint" }, server.reachable ? `Reachable · RomM ${server.version || ""}` : (server.error || ""));
  if (server.reachable) statusLine.classList.add("good");

  function paintAuthFields() {
    const mode = authMode.value;
    authFields.replaceChildren();
    if (mode === "pair") {
      authFields.append(h("div", { class: "field span" },
        h("div", { class: "hint" }, "Save the address, then start a pairing to approve in RomM. This creates a token dedicated to RomMArcade."),
        h("button", { class: "btn", style: { marginTop: "10px" }, onClick: () => startPairFromSettings(urlInput.value, nameInput.value) }, icon("server"), "Start pairing"),
      ));
    } else if (mode === "token") {
      authFields.append(h("div", { class: "field span" }, h("label", { for: "srvToken" }, "Client token"), tokenInput,
        h("div", { class: "hint" }, "A token from RomM, Client tokens. Leave the masked value untouched to keep the stored one.")));
    } else {
      authFields.append(
        h("div", { class: "field" }, h("label", { for: "srvUser" }, "Username"), userInput),
        h("div", { class: "field" }, h("label", { for: "srvPass" }, "Password"), passInput),
      );
    }
  }
  authMode.addEventListener("change", paintAuthFields);
  paintAuthFields();

  const card = h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("div", { class: "grow" }, h("h3", null, "Servers"), h("p", { class: "sub" }, "RomMArcade can keep several RomM addresses and switch between them. The active one feeds the library.")),
    ),
    list.length ? h("div", { class: "server-list" }, list) : h("p", { class: "hint", style: { marginBottom: "14px" } }, "No server yet."),
    h("div", { class: "divider" }),
    h("div", { class: "form-grid" },
      h("div", { class: "field span" }, h("label", { for: "srvUrl" }, "Server URL"), urlInput),
      h("div", { class: "field" }, h("label", { for: "srvName" }, "Display name"), nameInput),
      h("div", { class: "field" }, h("label", { for: "srvAuthMode" }, "Authentication"), authMode),
      h("div", { class: "field span" }, authFields),
    ),
    statusLine,
    h("div", { class: "row end", style: { marginTop: "16px" } },
      h("button", { class: "btn", onClick: () => testFromSettings(urlInput.value, statusLine) }, icon("refresh"), "Test"),
      h("button", { class: "btn primary", onClick: () => saveServerFromSettings(urlInput.value, nameInput.value, authMode.value, { token: tokenInput.value, username: userInput.value, password: passInput.value }) }, icon("check"), "Save server"),
    ),
  );
  body.append(card);
}

async function activateServer(id) {
  try {
    const result = await api.activateServer(id);
    state.status = await api.status();
    state.platforms = null;
    state.installs = null;
    state.config = (await api.config()).config;
    paintServerPill();
    toast(`Now using ${result.server.name || hostOf(result.server.url)}`, "good");
    route();
  } catch (error) { toast(error.message, "bad"); }
}

async function removeServer(id) {
  if (!confirm("Remove this server from RomMArcade? Installed games stay on disk.")) return;
  try {
    await api.removeServer(id);
    state.config = (await api.config()).config;
    state.status = await api.status();
    state.platforms = null;
    state.installs = null;
    paintServerPill();
    route();
  } catch (error) { toast(error.message, "bad"); }
}

async function testFromSettings(url, statusLine) {
  statusLine.className = "hint";
  statusLine.textContent = "Testing…";
  try {
    const result = await api.testServer({ url });
    statusLine.textContent = `Reachable · RomM ${result.server.version || ""}${result.server.user ? " · " + result.server.user.username : ""}`;
    statusLine.classList.add("good");
  } catch (error) {
    statusLine.textContent = error.message;
    statusLine.classList.add("bad");
  }
}

async function saveServerFromSettings(url, name, mode, secrets) {
  try {
    let auth;
    if (mode === "token" && secrets.token) auth = { mode: "token", token: secrets.token };
    else if (mode === "password" && secrets.username && secrets.password) auth = { mode: "password", username: secrets.username, password: secrets.password };
    else if (mode === "pair") auth = undefined;
    else auth = undefined; // blank secret: keep the stored auth untouched
    if (mode === "pair") {
      const cfg = await api.saveConfig({ server: { url, name } });
      state.config = cfg.config;
      await startPairFromSettings(url, name);
      return;
    }
    const result = await api.saveConfig(auth ? { server: { url, name, auth } } : { server: { url, name } });
    state.config = result.config;
    state.status = await api.status();
    state.platforms = null;
    state.installs = null;
    paintServerPill();
    toast("Server saved", "good");
    route();
  } catch (error) { toast(error.message, "bad"); }
}

async function startPairFromSettings(url, name) {
  if (!url) { toast("Enter a server URL first", "bad"); return; }
  try {
    const pair = await api.pairStart({ url, name });
    const sheet = h("div", { class: "sheet" },
      h("h2", null, "Pair with RomM"),
      h("p", null, "Open this address in RomM and approve the device. RomMArcade receives its own token."),
      h("div", { class: "pair-box" },
        h("div", { class: "pair-code" }, pair.user_code),
        h("a", { class: "pair-url", href: pair.verification_url, target: "_blank", rel: "noreferrer" }, pair.verification_url),
        h("div", { class: "pair-wait" }, h("span", { class: "spinner" }), "Waiting for approval…"),
      ),
      h("div", { class: "row end" }, h("button", { class: "btn ghost", onClick: closeModal }, "Later")),
    );
    const close = openModal(sheet);
    const started = Date.now();
    const poll = async () => {
      try {
        const result = await api.pairPoll({ device_code: pair.device_code });
        if (result.status === "approved") {
          state.config = (await api.config()).config;
          state.status = await api.status();
          state.platforms = null;
          state.installs = null;
          paintServerPill();
          close();
          toast(`Paired as ${result.user?.username || "a RomM user"}`, "good");
          route();
          return;
        }
        if (["denied", "expired"].includes(result.status)) {
          close();
          toast(result.status === "denied" ? "Pairing was denied in RomM" : "The pairing code expired", "bad");
          return;
        }
      } catch (error) { /* keep waiting */ }
      if (Date.now() - started < (Number(pair.expires_in) || 600) * 1000 && !document.getElementById("modal").hidden) {
        setTimeout(poll, Math.max(3, Number(pair.interval) || 5) * 1000);
      }
    };
    setTimeout(poll, Math.max(3, Number(pair.interval) || 5) * 1000);
  } catch (error) { toast(error.message, "bad"); }
}

async function settingsEmulators(body, token) {
  let emu = null;
  let platforms = [];
  try { [emu, platforms] = await Promise.all([api.emudeck(), getPlatforms()]); } catch (error) {
    body.append(h("div", { class: "card" }, h("h3", null, "EmuDeck"), h("p", { class: "sub" }, error.message)));
    return;
  }
  if (token !== state.renderToken) return;
  state.emudeck = emu;
  const enabled = emu.enabled !== false;
  if (!enabled) {
    body.append(emudeckDisabledCard());
    return;
  }
  const mapping = new Map((emu.platforms || []).map((p) => [p.platform, p]));
  const dirs = emu.rom_dirs || [];
  const launchers = emu.launchers || [];
  const cores = emu.cores || [];
  const rows = platforms
    .filter((p) => p.is_game_platform !== false && Number(p.game_count ?? p.rom_count) > 0)
    .sort((a, b) => String(a.display_name || a.name).localeCompare(String(b.display_name || b.name)))
    .map((p) => {
      const key = p.fs_slug || p.slug;
      const entry = mapping.get(key) || p.local || {};
      const dirSelect = h("select", { class: "select", "aria-label": `Game folder for ${key}` },
        h("option", { value: "" }, "— unmapped —"),
        dirs.map((dir) => h("option", { value: dir, selected: dir === entry.dir }, dir)),
      );
      const choices = (emu.options || {})[key] || {};
      const launcherIds = Array.isArray(choices.launchers) ? choices.launchers : null;
      const coreChoices = Array.isArray(choices.cores) ? choices.cores : cores;
      const launcherOptions = launchers.filter((l) =>
        (!launcherIds || launcherIds.includes(l.id)) && (!l.superseded || l.id === entry.launcher));
      if (entry.launcher && !launcherOptions.some((l) => l.id === entry.launcher)) {
        const current = launchers.find((l) => l.id === entry.launcher);
        launcherOptions.push(current || {
          id: entry.launcher,
          name: entry.launcher_name || entry.launcher,
          ready: false,
        });
      }
      const launcherSelect = h("select", { class: "select", "aria-label": `Launcher for ${key}` },
        h("option", { value: "" }, "— none —"),
        launcherOptions.map((l) => h("option", { value: l.id, selected: l.id === entry.launcher }, `${l.name}${l.ready ? "" : " (missing)"}`)),
      );
      const coreSelect = h("select", { class: "select", "aria-label": `Core for ${key}` });
      let lastCore = entry.core || "";
      const fillCores = () => {
        const retro = launcherSelect.value === "retroarch";
        const wanted = retro ? coreChoices : [];
        coreSelect.replaceChildren(
          h("option", { value: "" }, retro || !launcherSelect.value ? "— auto —" : "— not used —"),
          ...wanted.map((core) => h("option", { value: core }, core)),
        );
        coreSelect.disabled = !retro;
        coreSelect.title = retro
          ? "RetroArch core for this system"
          : "Only RetroArch takes a core; the standalone emulator brings its own";
        coreSelect.value = retro && lastCore && wanted.includes(lastCore) ? lastCore : "";
      };
      fillCores();
      const save = async () => {
        try {
          await api.mapPlatform({ platform: key, dir: dirSelect.value, launcher: launcherSelect.value, core: coreSelect.value });
          toast(`${p.display_name || key} mapping saved`, "good");
          state.platforms = null;
        } catch (error) { toast(error.message, "bad"); }
      };
      dirSelect.addEventListener("change", save);
      launcherSelect.addEventListener("change", async () => { fillCores(); await save(); });
      coreSelect.addEventListener("change", () => { lastCore = coreSelect.value; save(); });
      return h("tr", null,
        h("td", { class: "platform" }, p.display_name || p.name || key, h("small", null, key)),
        h("td", null, dirSelect),
        h("td", null, launcherSelect),
        h("td", null, coreSelect),
        h("td", { class: "status" }, entry.exists ? h("span", { class: "yes" }, icon("check")) : h("span", { class: "no" }, icon("alert"))),
      );
    });

  const emuCard = h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("div", { class: "grow" }, h("h3", null, "EmuDeck"), h("p", { class: "sub" }, "Detected installation and the launchers RomMArcade can hand a ROM to.")),
      h("div", { class: "row" },
        h("button", { class: "btn small", onClick: () => refreshEmudeck() }, icon("refresh"), "Rescan"),
      ),
    ),
    emu.detected
      ? h("div", { class: "emu-status" },
          h("div", { class: "glyph" }, icon("folder")),
          h("div", { class: "txt" },
            h("div", { class: "t" }, emu.root),
            h("div", { class: "s" }, `ROMs ${emu.roms_root}`),
            h("div", { class: "s" }, `BIOS ${emu.bios_root}`),
            h("div", { class: "s" }, `Launchers ${emu.launchers_root}`),
          ),
          h("span", { class: "badge local" }, "Found"),
        )
      : emu.library?.configured
        ? h("div", { class: "emu-status" },
            h("div", { class: "glyph" }, icon("folder")),
            h("div", { class: "txt" },
              h("div", { class: "t" }, "Game folders on this machine"),
              h("div", { class: "s" }, `ROMs ${emu.library.roms_root}`),
              h("div", { class: "s" }, `BIOS ${emu.library.bios_root}`),
              h("div", { class: "s" }, "EmuDeck not installed — installs work, launching needs it"),
            ),
            h("span", { class: "badge local" }, "Local"),
          )
        : h("div", { class: "emu-status" },
            h("div", { class: "glyph" }, icon("alert")),
            h("div", { class: "txt" }, h("div", { class: "t" }, "EmuDeck not found"),
              h("div", { class: "s" }, "Set a root below, or install EmuDeck and rescan.")),
          ),
    h("div", { class: "launcher-chips" },
      launchers
        .filter((l) => !l.superseded)
        .map((l) => h("span", { class: `launcher-chip ${l.ready ? "ready" : "dead"}`, title: l.path }, l.name))),
    h("div", { class: "form-grid", style: { marginTop: "16px" } },
      h("div", { class: "field span" },
        h("label", { for: "emuRoot" }, "EmuDeck root override"),
        h("input", { class: "input", id: "emuRoot", placeholder: "/home/james/Emulation", value: state.config?.emudeck?.root || "" }),
        h("div", { class: "hint" }, "Leave blank to use EmuDeck's own settings.json. A path accepts either the Emulation folder or the folder that contains it. Saving a root also switches installs back to EmuDeck."),
      ),
    ),
    h("div", { class: "row end", style: { marginTop: "14px" } },
      h("button", { class: "btn", onClick: () => saveEmuRoot() }, icon("check"), "Save root"),
    ),
  );

  const mapCard = h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("div", { class: "grow" }, h("h3", null, "Platform map"), h("p", { class: "sub" }, "Where each RomM platform lands on this machine, and which launcher and core opens it. Changes apply immediately.")),
    ),
    h("div", { class: "map-wrap" },
      h("table", { class: "map-table" },
        h("thead", null, h("tr", null,
          h("th", null, "Platform"), h("th", null, "Game folder"), h("th", null, "Launcher"), h("th", null, "Core"), h("th", null, ""),
        )),
        h("tbody", null, rows),
      ),
    ),
  );
  body.append(emuCard, mapCard);
}

async function refreshEmudeck() {
  state.platforms = null;
  state.emudeck = null;
  try { await api.emudeck(); } catch { /* ignore */ }
  route();
}

async function saveEmuRoot() {
  const value = document.getElementById("emuRoot")?.value?.trim() || "";
  try {
    // Naming an EmuDeck root is an explicit choice to let EmuDeck own the
    // folders again, so the plain game-folder setup is cleared with it.
    const body = { emudeck: { root: value } };
    if (value) body.library = { mode: "", root: "", bios_root: "" };
    const result = await api.saveConfig(body);
    state.config = result.config;
    state.platforms = null;
    state.emudeck = null;
    toast(value ? "EmuDeck root saved" : "EmuDeck root cleared", "good");
    route();
  } catch (error) { toast(error.message, "bad"); }
}

function settingsAbout(body) {
  const link = (href, label) => h("a", { class: "btn small", href, target: "_blank", rel: "noreferrer" }, label, icon("external"));
  body.append(
    h("div", { class: "card" },
      h("div", { class: "card-head" },
        h("div", { class: "grow" }, h("h3", null, "RomMArcade"), h("p", { class: "sub" }, state.status?.tagline || "Your RomM library, in an arcade shell.")),
      ),
      h("div", { class: "facts" },
        fact("Version", state.status?.version || "—"),
        fact("Server", state.status?.server?.url || "Not configured"),
        fact("EmuDeck", state.status?.emudeck?.root || "Not found"),
        fact("Data", "/.local/share/rommarcade", "", true),
      ),
      h("div", { class: "row", style: { marginTop: "16px" } },
        link("https://github.com/jkoehler11/RomMArcade", "Source"),
        h("button", { class: "btn small", onClick: () => showLog() }, icon("folder"), "Service log"),
        h("button", { class: "btn small", onClick: () => showDeeplinkHelp() }, icon("external"), "URL scheme"),
      ),
    ),
    h("div", { class: "card" },
      h("h3", null, "How the pieces fit"),
      h("p", { class: "sub", style: { marginTop: "8px", lineHeight: "1.65" } },
        "RomMArcade is a local service and this web UI. It reads your RomM library over HTTP, installs games into EmuDeck's folders, and launches EmuDeck's own launcher scripts. Saves, BIOS and emulator settings stay EmuDeck's, not ours."),
    ),
  );
}

async function showLog() {
  try {
    const data = await api.log(250);
    openModal(h("div", { class: "sheet" },
      h("h2", null, "Service log"),
      h("pre", { class: "mono", style: { fontSize: "10.5px", lineHeight: "1.5", color: "var(--muted)", whiteSpace: "pre-wrap", maxHeight: "60vh", overflow: "auto" } }, (data.lines || []).join("\n") || "Empty."),
      h("div", { class: "row end" }, h("button", { class: "btn", onClick: closeModal }, "Close")),
    ));
  } catch (error) { toast(error.message, "bad"); }
}

function showDeeplinkHelp() {
  openModal(h("div", { class: "sheet" },
    h("h2", null, "rommarcade:// links"),
    h("p", null, "Registering the URL handler lets RomM, a desktop shortcut or a script open a game here. The service consumes the target once, so a reload does not navigate again."),
    h("pre", { class: "mono", style: { padding: "12px", background: "#0a0d1a", border: "1px solid var(--line)", borderRadius: "9px", fontSize: "11px", whiteSpace: "pre-wrap" } },
      "rommarcade://open?rom=168&platform=nes&name=1942\nrommarcade://open?rom=168&play=1"),
    h("p", { class: "hint" }, "Install the handler with packaging/rommarcade-open and packaging/rommarcade-url.desktop, then approve the button in RomM."),
    h("div", { class: "row end" }, h("button", { class: "btn", onClick: closeModal }, "Close")),
  ));
}

// ---------------------------------------------------------------- utils ---
function hostOf(url) {
  try { return new URL(url).host; } catch { return url || ""; }
}

boot();
