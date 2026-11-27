// Fetch wrapper for the local RomMArcade API.

class ApiError extends Error {
  constructor(message, status = 0, payload = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload || {};
  }
}

async function request(path, { method = "GET", body = null, timeout = 30000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    const text = await response.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = { error: text }; }
    }
    if (!response.ok) {
      throw new ApiError(payload?.error || `Request failed (${response.status})`, response.status, payload);
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new ApiError("The RomMArcade service did not answer in time.", 0);
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  status: () => request("/api/status"),
  config: () => request("/api/config"),
  saveConfig: (body) => request("/api/config", { method: "PUT", body }),
  testServer: (body) => request("/api/config/test", { method: "POST", body, timeout: 40000 }),
  pairStart: (body) => request("/api/config/pair/start", { method: "POST", body, timeout: 40000 }),
  pairPoll: (body) => request("/api/config/pair/poll", { method: "POST", body, timeout: 40000 }),
  servers: () => request("/api/servers"),
  addServer: (body) => request("/api/servers", { method: "POST", body }),
  activateServer: (id) => request(`/api/servers/${encodeURIComponent(id)}/activate`, { method: "POST" }),
  removeServer: (id) => request(`/api/servers/${encodeURIComponent(id)}`, { method: "DELETE" }),
  platforms: () => request("/api/platforms", { timeout: 60000 }),
  roms: (params) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params || {})) {
      if (value !== null && value !== undefined && value !== "") query.set(key, value);
    }
    return request(`/api/roms?${query}`, { timeout: 60000 });
  },
  rom: (id) => request(`/api/roms/${Number(id)}`, { timeout: 60000 }),
  firmware: (platformId) => request(`/api/firmware/${Number(platformId)}`),
  uploadFirmware: (platformId, fileName, content) => request("/api/firmware/upload", {
    method: "POST",
    body: { platform_id: Number(platformId), file_name: fileName, content },
    timeout: 180000,
  }),
  deleteFirmware: (id) => request(`/api/firmware/${Number(id)}/delete`, { method: "POST", body: {} }),
  localBios: (platformId) => request(`/api/bios/local/${Number(platformId)}`),
  deleteLocalBios: (platformId, name) => request("/api/bios/local/delete", {
    method: "POST", body: { platform_id: Number(platformId), name },
  }),
  repairBios: (platformId) => request("/api/bios/repair", {
    method: "POST", body: { platform_id: Number(platformId) },
  }),
  downloadStorage: (platformId, kind = "hdd") => request("/api/storage/download", {
    method: "POST", body: { platform_id: Number(platformId), kind },
  }),
  emudeck: () => request("/api/emudeck", { timeout: 60000 }),
  library: () => request("/api/library", { timeout: 60000 }),
  saveLibrary: (body) => request("/api/library", { method: "POST", body, timeout: 60000 }),
  openInstaller: () => request("/api/emudeck/open-installer", { method: "POST", body: {} }),
  rescanEmudeck: () => request("/api/emudeck/rescan", { method: "POST", body: {} }),
  mapPlatform: (body) => request("/api/emudeck/mapping", { method: "POST", body }),
  installs: () => request("/api/installs"),
  uninstall: (romId, removeSaves = true) => request(`/api/installs/${Number(romId)}/uninstall`, { method: "POST", body: { remove_saves: Boolean(removeSaves) } }),
  repairInstalls: () => request("/api/installs/repair", { method: "POST" }),
  downloads: () => request("/api/downloads"),
  cancelDownload: (id) => request(`/api/downloads/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  install: (romId) => request("/api/install", { method: "POST", body: { rom_id: romId } }),
  bios: (platformId, firmwareId = null) => request("/api/bios", {
    method: "POST",
    body: { platform_id: platformId, ...(firmwareId ? { firmware_id: Number(firmwareId) } : {}) },
  }),
  play: (romId, dryRun = false) => request("/api/play", { method: "POST", body: { rom_id: romId, dry_run: dryRun } }),
  deeplink: () => request("/api/deeplink"),
  pushDeeplink: (url) => request("/api/deeplink", { method: "POST", body: { url } }),
  history: () => request("/api/history"),
  romSaves: (id) => request(`/api/roms/${Number(id)}/saves`, { timeout: 60000 }),
  restoreSave: (romId, saveId, play = false) => request(`/api/roms/${Number(romId)}/saves/restore`, { method: "POST", body: { save_id: Number(saveId), play: Boolean(play) }, timeout: 180000 }),
  uploadLocalSave: (romId, name) => request(`/api/roms/${Number(romId)}/saves/upload`, { method: "POST", body: { name }, timeout: 120000 }),
  setSaveSlot: (romId, slot) => request(`/api/roms/${Number(romId)}/saves/slot`, { method: "POST", body: { slot }, timeout: 60000 }),
  createSaveSlot: (romId, slot, seed = true) => request(`/api/roms/${Number(romId)}/saves/slots`, { method: "POST", body: { slot, seed: Boolean(seed) }, timeout: 120000 }),
  saveFavorite: (id, value) => request(`/api/saves/${Number(id)}/favorite`, { method: "POST", body: { is_favorite: Boolean(value) } }),
  saveVisibility: (id, value) => request(`/api/saves/${Number(id)}/visibility`, { method: "POST", body: { is_public: Boolean(value) } }),
  saveLabels: (id, labels) => request(`/api/saves/${Number(id)}/labels`, { method: "POST", body: { labels } }),
  renameSave: (id, name) => request(`/api/saves/${Number(id)}/rename`, { method: "POST", body: { file_name: name } }),
  deleteSave: (id) => request(`/api/saves/${Number(id)}/delete`, { method: "POST", body: {} }),
  log: (lines = 200) => request(`/api/log?lines=${lines}`),
};

export { ApiError };
