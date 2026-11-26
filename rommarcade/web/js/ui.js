// Tiny DOM helpers and formatting shared by every view.

export function h(tag, attrs = null, ...children) {
  const el = document.createElement(tag);
  if (attrs && typeof attrs === "object" && !(attrs instanceof Node) && !Array.isArray(attrs)) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class" || key === "className") el.className = value;
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
      else if (key === "html") el.innerHTML = value;
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
      else if (value === true) el.setAttribute(key, "");
      else el.setAttribute(key, value);
    }
  } else if (attrs !== null && attrs !== undefined) {
    children.unshift(attrs);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

const SVG_NS = "http://www.w3.org/2000/svg";
export function icon(name, cls = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

export function fmtBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let n = value / 1024;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n >= 10 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
}

export function fmtSpeed(bytesPerSecond) {
  if (!bytesPerSecond) return "";
  return `${fmtBytes(bytesPerSecond)}/s`;
}

export function installLabel(job) {
  if (!job) return "Installing";
  if (job.status === "extracting") return "Extracting";
  if (job.status === "queued") return "Queued";
  const pct = Math.max(0, Math.min(100, Math.round(Number(job.progress || 0) * 100)));
  return pct > 0 ? `Installing ${pct}%` : "Installing";
}

export function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(1, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(then).toLocaleDateString();
}

export function shortDate(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function debounce(fn, wait = 300) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

export function initials(name) {
  return String(name || "?")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0])
    .join("")
    .toUpperCase();
}

export function toast(message, kind = "info", timeout = 4200) {
  const root = document.getElementById("toasts");
  if (!root) return;
  const glyph = kind === "good" ? "check" : kind === "bad" ? "alert" : "cart";
  const el = h("div", { class: `toast ${kind}` },
    icon(glyph),
    h("div", { class: "msg" }, message),
    h("button", { class: "close", "aria-label": "Dismiss", onClick: () => el.remove() }, icon("close", "icon")),
  );
  root.append(el);
  setTimeout(() => el.remove(), timeout);
}

export function openModal(node, { onClose } = {}) {
  const root = document.getElementById("modal");
  root.hidden = false;
  root.replaceChildren(node);
  const close = (event) => {
    if (event && event.target !== root) return;
    root.hidden = true;
    root.replaceChildren();
    document.removeEventListener("keydown", onKey);
    onClose?.();
  };
  const onKey = (event) => { if (event.key === "Escape") close(); };
  root.addEventListener("click", close);
  document.addEventListener("keydown", onKey);
  return close;
}

export function closeModal() {
  const root = document.getElementById("modal");
  if (!root || root.hidden) return;
  root.hidden = true;
  root.replaceChildren();
}

export function emptyState({ glyph = "cart", title, body, action = null }) {
  return h("div", { class: "empty" },
    h("div", { class: "glyph" }, icon(glyph)),
    h("h3", null, title),
    body ? h("p", null, body) : null,
    action,
  );
}

export function coverImage(rom, { size = "small", className = "", loading = "lazy" } = {}) {
  const wrap = h("div", { class: `cover ${className}` });
  if (rom.cover) {
    const img = h("img", {
      src: `/api/cover/${rom.id}?size=${size}`,
      alt: "",
      loading,
      decoding: "async",
    });
    img.addEventListener("error", () => {
      img.remove();
      wrap.prepend(fallbackCover(rom));
    }, { once: true });
    wrap.append(img);
  } else {
    wrap.append(fallbackCover(rom));
  }
  return wrap;
}

function fallbackCover(rom) {
  const label = rom.platform_name || rom.platform_fs_slug || "RomM";
  return h("div", { class: "cover-fallback" },
    h("div", { class: "glyph" }, icon("cart")),
    h("div", { class: "name" }, rom.name || label),
  );
}

export function gameCard(rom, { showPlay = true, eager = false } = {}) {
  const installed = Boolean(rom.installed);
  const job = rom.install_job || null;
  const installing = Boolean(job);
  const canPlay = Boolean(rom.mapping?.launcher && rom.mapping?.launcher_ready !== false);
  const card = h("article", {
    class: `game-card ${installing || rom.installing ? "installing" : ""}`,
    dataset: { rom: rom.id },
    tabindex: "0",
    role: "link",
    "aria-label": rom.name,
    onClick: (event) => {
      if (event.target.closest("[data-stop]")) return;
      location.hash = `#/game/${rom.id}`;
    },
    onKeydown: (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        location.hash = `#/game/${rom.id}`;
      }
    },
  });
  const cover = coverImage(rom, { size: "small", loading: eager ? "eager" : "lazy" });
  const badges = h("div", { class: "badges" });
  if (installed) badges.append(h("span", { class: "badge local" }, "Local"));
  if (installing) badges.append(h("span", { class: "badge cloud", "data-install-badge": rom.id }, installLabel(job)));
  if (rom.has_multiple_files) badges.append(h("span", { class: "badge muted" }, "Multi"));
  if (!installed && !installing && rom.mapping && !rom.mapping.exists) badges.append(h("span", { class: "badge warn" }, "No folder"));
  card.append(cover, badges);
  if (showPlay && installing) {
    card.append(h("button", {
      class: "card-play installing",
      disabled: true,
      "data-install-rom": rom.id,
      title: installLabel(job),
      "aria-label": `${installLabel(job)}: ${rom.name}`,
    }, h("span", { class: "spinner", "aria-hidden": "true" })));
  } else if (showPlay && (!installed || canPlay)) {
    card.append(h("button", {
      class: "card-play",
      "data-stop": "",
      dataset: { playRom: String(rom.id) },
      title: installed ? "Play" : canPlay ? "Install and play" : "Install game",
      "aria-label": installed ? `Play ${rom.name}` : `Install ${rom.name}`,
      onClick: (event) => {
        event.stopPropagation();
        if (installed) window.dispatchEvent(new CustomEvent("rommarcade:play", { detail: { romId: rom.id } }));
        else window.dispatchEvent(new CustomEvent("rommarcade:install", { detail: { romId: rom.id, play: canPlay } }));
      },
    }, icon("play")));
  }
  card.append(h("div", { class: "meta" },
    h("h3", { class: "game-name" }, rom.name),
    h("p", { class: "game-sub" },
      h("span", null, rom.platform_name || rom.platform_fs_slug || "Unknown"),
      h("span", { class: "size" }, rom.size ? fmtBytes(rom.size) : ""),
    ),
  ));
  return card;
}

const LOGO_BOOST = {
  "3ds": 3.1, "n3ds": 3.1, "atari2600": 2.0, "xbox360": 2.6,
  "dc": 1.8, "dreamcast": 1.8, "ps2": 1.55, "gc": 1.35, "ngc": 1.35,
  "genesis": 1.45, "gamegear": 1.3, "switch": 1.15, "saturn": 1.1, "wiiu": 1.05,
};
const NO_LOGO = new Set(["gba", "n64", "3ds", "n3ds", "ps4", "psx"]);
const NO_LOGO_LABELS = { gba: "GBA", n64: "N64", "3ds": "3DS", n3ds: "3DS", ps4: "PS4", psx: "PS1" };

export function platformCard(platform, active = false) {
  const local = platform.local || {};
  const roms = Number(platform.game_count ?? platform.rom_count) || 0;
  const enabled = local.enabled !== false;
  const mapped = Boolean(enabled && local.exists);
  const launchable = Boolean(mapped && local.launcher);
  const slug = String(platform.fs_slug || platform.slug || "").toLowerCase();
  const normalized = Boolean(platform.logo_normalized);
  const boost = normalized ? 1 : (LOGO_BOOST[slug] || 1.55);
  const monogram = NO_LOGO_LABELS[slug] || platform.abbreviation || initials(platform.display_name || platform.name || platform.slug);
  return h("a", {
    class: `plat-card ${active ? "active" : ""}`,
    href: `#/platform/${encodeURIComponent(platform.fs_slug || platform.slug)}`,
    title: mapped
      ? launchable
        ? `${platform.display_name || platform.name}: ${local.launcher_name || local.launcher}${local.core ? " / " + local.core : ""}`
        : `${platform.display_name || platform.name}: game folder ready, no launcher on this machine`
      : `${platform.display_name || platform.name}: no game folder mapped`,
  },
    h("div", { class: "plat-art" },
      platform.logo_url && !NO_LOGO.has(slug)
        ? h("img", {
            src: platform.logo_url,
            alt: "",
            loading: "lazy",
            style: {
              mixBlendMode: normalized ? "normal" : "screen",
              filter: normalized
                ? "drop-shadow(0 5px 12px rgba(0,0,0,.45))"
                : `brightness(${boost}) saturate(1.12) drop-shadow(0 5px 12px rgba(0,0,0,.45))`,
            },
          })
        : h("span", { class: "plat-initials" }, monogram),
    ),
    h("div", { class: "plat-info" },
      h("div", null,
        h("div", { class: "plat-name" }, platform.display_name || platform.name || platform.fs_slug),
        h("div", { class: "plat-sub" },
          h("span", null, `${roms} game${roms === 1 ? "" : "s"}`),
          local.installed_count ? h("span", { class: "local" }, `${local.installed_count} local`) : null,
          enabled ? h("span", { class: `plat-state ${mapped ? "ok" : "warn"}`, title: mapped ? (launchable ? "Folder and launcher mapped" : "Game folder ready") : "No game folder mapped" }) : null,
        ),
      ),
      h("span", { class: "plat-go", "aria-hidden": "true" }, icon("arrow")),
    ),
  );
}

export function skeletonGrid(count = 12) {
  return h("div", { class: "grid" }, Array.from({ length: count }, () => h("div", { class: "skel card" })));
}
