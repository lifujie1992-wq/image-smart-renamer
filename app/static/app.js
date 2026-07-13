const SUPPORTED_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".webp"]);
const LOCAL_THUMB_EDGE = 240;
const LOCAL_THUMB_QUALITY = 0.55;

const state = {
  folderId: null,
  folderName: null,
  folderMode: null, // client | server
  dirHandle: null,
  images: [], // {id, original_name, extension, thumbUrl?, localFile?}
  assignments: new Map(), // imageId -> number
  mode: "sequence", // sequence | pick
  selectedNumber: null,
  maxNumber: 20,
  plan: null,
  thumbObjectUrls: [],
};

const $ = (id) => document.getElementById(id);

function showMessage(text) {
  $("message").textContent = text;
  $("message").classList.add("show");
  setTimeout(() => $("message").classList.remove("show"), 3200);
}

function isLocalHost() {
  const host = location.hostname;
  return host === "127.0.0.1" || host === "localhost" || host === "[::1]";
}

function extensionOf(name) {
  const index = name.lastIndexOf(".");
  return index >= 0 ? name.slice(index).toLowerCase() : "";
}

function revokeThumbs() {
  for (const url of state.thumbObjectUrls) {
    try {
      URL.revokeObjectURL(url);
    } catch {
      /* ignore */
    }
  }
  state.thumbObjectUrls = [];
}

async function api(url, options = {}) {
  const isForm = typeof FormData !== "undefined" && options.body instanceof FormData;
  const headers = { ...(options.headers || {}) };
  if (!isForm && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(url, { ...options, headers });
  const data = await response.json().catch(() => ({ detail: "请求失败" }));
  if (!response.ok) {
    const detail = data.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((item) => item.msg || JSON.stringify(item)).join("; ")
          : "请求失败";
    throw new Error(message);
  }
  return data;
}

async function ensureDirPermission(dirHandle, mode = "readwrite") {
  const options = { mode };
  if (dirHandle.queryPermission) {
    if ((await dirHandle.queryPermission(options)) === "granted") return true;
  }
  if (dirHandle.requestPermission) {
    return (await dirHandle.requestPermission(options)) === "granted";
  }
  return true;
}

/** Downscale for grid preview — never load full-res into <img>. */
async function makeLocalThumbUrl(file) {
  // 1) Preferred: decode + resize to small JPEG blob
  try {
    let bitmap;
    if (typeof createImageBitmap === "function") {
      bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    } else {
      throw new Error("no createImageBitmap");
    }
    const scale = Math.min(1, LOCAL_THUMB_EDGE / Math.max(bitmap.width, bitmap.height, 1));
    const width = Math.max(1, Math.round(bitmap.width * scale));
    const height = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d", { alpha: false });
    ctx.drawImage(bitmap, 0, 0, width, height);
    bitmap.close();
    const blob = await new Promise((resolve) =>
      canvas.toBlob(resolve, "image/jpeg", LOCAL_THUMB_QUALITY)
    );
    if (!blob) throw new Error("thumb failed");
    const url = URL.createObjectURL(blob);
    state.thumbObjectUrls.push(url);
    return url;
  } catch {
    // 2) Fallback: object URL of original (still shows something)
    try {
      const url = URL.createObjectURL(file);
      state.thumbObjectUrls.push(url);
      return url;
    } catch {
      return "";
    }
  }
}

async function mapPool(items, limit, worker) {
  const results = new Array(items.length);
  let index = 0;
  async function run() {
    while (index < items.length) {
      const current = index;
      index += 1;
      results[current] = await worker(items[current], current);
    }
  }
  const runners = Array.from({ length: Math.min(limit, items.length) }, () => run());
  await Promise.all(runners);
  return results;
}

async function collectClientImages(dirHandle) {
  const entries = [];
  for await (const [name, handle] of dirHandle.entries()) {
    if (handle.kind !== "file") continue;
    if (name.startsWith(".") || name.startsWith(".image-smart-renamer-")) continue;
    if (!SUPPORTED_EXTENSIONS.has(extensionOf(name))) continue;
    entries.push({ name, handle });
  }
  entries.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
  return entries;
}

function localImageId(name, index) {
  return `local-${index}-${name.length}-${name.slice(0, 24)}`;
}

function nameKey(name) {
  return name.normalize("NFC").toLowerCase();
}

/** Live-read folder and refresh image names (handles stay stable after renames). */
async function rescanClientFolder() {
  if (!state.dirHandle) {
    throw new Error("本机会话已失效，请重新选择文件夹");
  }
  if (!(await ensureDirPermission(state.dirHandle, "readwrite"))) {
    throw new Error("需要本机文件夹的读写权限");
  }

  const live = [];
  for await (const [name, handle] of state.dirHandle.entries()) {
    if (handle.kind !== "file") continue;
    if (name.startsWith(".") || name.startsWith(".image-smart-renamer-")) continue;
    if (!SUPPORTED_EXTENSIONS.has(extensionOf(name))) continue;
    live.push({ name, handle, extension: extensionOf(name) });
  }

  const used = new Set();
  const refreshed = [];

  for (const image of state.images) {
    let matched = null;
    if (image.handle && typeof image.handle.isSameEntry === "function") {
      for (const entry of live) {
        if (used.has(entry.name)) continue;
        try {
          if (await image.handle.isSameEntry(entry.handle)) {
            matched = entry;
            break;
          }
        } catch {
          /* ignore compare errors */
        }
      }
    }
    if (!matched) {
      matched = live.find(
        (entry) => !used.has(entry.name) && nameKey(entry.name) === nameKey(image.original_name)
      );
    }
    if (!matched) {
      // File disappeared — drop assignment if any
      state.assignments.delete(image.id);
      continue;
    }
    used.add(matched.name);
    refreshed.push({
      ...image,
      original_name: matched.name,
      extension: matched.extension,
      handle: matched.handle,
    });
  }

  // New files that appeared since last scan
  let extraIndex = state.images.length;
  for (const entry of live) {
    if (used.has(entry.name)) continue;
    let thumbUrl = "";
    try {
      const file = await entry.handle.getFile();
      thumbUrl = await makeLocalThumbUrl(file);
    } catch {
      /* preview optional */
    }
    refreshed.push({
      id: localImageId(entry.name, extraIndex),
      original_name: entry.name,
      extension: entry.extension,
      thumbUrl,
      handle: entry.handle,
      scan_error: null,
    });
    extraIndex += 1;
  }

  state.images = refreshed;
  return {
    names: new Set(live.map((entry) => entry.name)),
    nameKeys: new Set(live.map((entry) => nameKey(entry.name))),
  };
}

function findOccupantName(target, liveNames) {
  const key = nameKey(target);
  for (const name of liveNames) {
    if (nameKey(name) === key) return name;
  }
  return null;
}

async function applyLocalRenamePlan(dirHandle, plan) {
  if (!(await ensureDirPermission(dirHandle, "readwrite"))) {
    throw new Error("需要本机文件夹的读写权限才能改名");
  }
  // Always use a fresh listing at rename time.
  const handles = new Map(); // lower(name) -> { name, handle }
  for await (const [name, handle] of dirHandle.entries()) {
    if (handle.kind === "file") handles.set(nameKey(name), { name, handle });
  }

  const active = plan.entries.filter((entry) => !entry.no_op);
  const phases = [
    active.map((entry) => ({ from: entry.source_name, to: entry.temporary_name })),
    active.map((entry) => ({ from: entry.temporary_name, to: entry.target_name })),
  ];

  for (const phase of phases) {
    for (const { from, to } of phase) {
      const fromKey = nameKey(from);
      const toKey = nameKey(to);
      const source = handles.get(fromKey);
      if (!source) throw new Error(`本机找不到文件：${from}`);
      if (handles.has(toKey)) throw new Error(`本机目标名已存在：${to}`);
      if (typeof source.handle.move !== "function") {
        throw new Error("当前浏览器不支持本机原地改名，请使用 Chrome 或 Edge");
      }
      await source.handle.move(to);
      handles.delete(fromKey);
      handles.set(toKey, { name: to, handle: source.handle });
    }
  }
}

function buildClientPlan(assignmentsList, liveNames) {
  // assignmentsList: [{image, number}]  liveNames: Set of current disk filenames
  const sourceKeys = new Set(assignmentsList.map((item) => nameKey(item.image.original_name)));
  const entries = [];
  const sorted = [...assignmentsList].sort((a, b) => a.number - b.number);
  const stamp = Date.now().toString(36);
  const plannedTargets = new Map(); // nameKey(target) -> source

  for (const [index, item] of sorted.entries()) {
    const target = `${item.number}${item.image.extension}`;
    const targetKey = nameKey(target);
    const sourceKey = nameKey(item.image.original_name);

    if (plannedTargets.has(targetKey)) {
      throw new Error(`编号冲突：多个文件都要改成 ${target}`);
    }

    const occupant = findOccupantName(target, liveNames);
    // Free if missing, or the occupant is this file, or occupant is also being renamed away.
    if (
      occupant &&
      nameKey(occupant) !== sourceKey &&
      !sourceKeys.has(nameKey(occupant))
    ) {
      const owner = state.images.find((image) => nameKey(image.original_name) === nameKey(occupant));
      const ownerLabel = owner?.original_name || occupant;
      throw new Error(
        `目标「${target}」当前被「${ownerLabel}」占用。请先给占用文件编其他号，或取消该编号后重试。`
      );
    }

    plannedTargets.set(targetKey, item.image.original_name);
    entries.push({
      item_id: item.image.id,
      source_name: item.image.original_name,
      target_name: target,
      temporary_name: `.image-smart-renamer-${stamp}-${index}`,
      no_op: nameKey(item.image.original_name) === targetKey,
    });
  }

  return {
    plan_id: `local-${stamp}`,
    entries,
  };
}

function readMaxNumber() {
  const value = Number($("max-number").value);
  if (!Number.isInteger(value) || value < 1 || value > 200) {
    throw new Error("编号范围请填 1–200 的整数");
  }
  return value;
}

function usedNumbers() {
  return new Set(state.assignments.values());
}

function nextFreeNumber() {
  const used = usedNumbers();
  for (let n = 1; n <= state.maxNumber; n += 1) {
    if (!used.has(n)) return n;
  }
  return null;
}

function numberOwner(number) {
  for (const [imageId, value] of state.assignments) {
    if (value === number) return imageId;
  }
  return null;
}

function setAssignment(imageId, number) {
  state.assignments.delete(imageId);
  const owner = numberOwner(number);
  if (owner) state.assignments.delete(owner);
  state.assignments.set(imageId, number);
}

function clearAssignment(imageId) {
  state.assignments.delete(imageId);
}

function enterWorkspace({ folderId, folderName, folderMode, images }) {
  // Do NOT revoke thumbs here — selectFolder already built blob URLs in state.thumbObjectUrls.
  state.folderId = folderId;
  state.folderName = folderName;
  state.folderMode = folderMode;
  state.images = images;
  state.assignments = new Map();
  state.selectedNumber = null;
  state.plan = null;
  $("folder-name").textContent = folderName;
  const where = folderMode === "client" ? "本机" : "服务器本机";
  $("folder-count").textContent = `${where} · 当前层 ${images.length} 张 · 预览为低清缩略图`;
  $("setup-panel").classList.add("hidden");
  $("plan-panel").classList.add("hidden");
  $("done-panel").classList.add("hidden");
  $("work-panel").classList.remove("hidden");
  renderAll();
}

async function selectFolderOnClient() {
  if (typeof window.showDirectoryPicker !== "function") {
    throw new Error("当前浏览器不支持本机选文件夹，请用 Chrome / Edge 打开");
  }
  const dirHandle = await window.showDirectoryPicker({ mode: "readwrite" });
  if (!(await ensureDirPermission(dirHandle, "readwrite"))) {
    throw new Error("未获得文件夹读写权限");
  }
  const entries = await collectClientImages(dirHandle);
  if (!entries.length) {
    throw new Error("该文件夹当前层没有支持的图片（JPG/PNG/WebP）");
  }

  // Drop previous session previews only after user confirmed a new folder.
  revokeThumbs();
  showMessage(`正在生成 ${entries.length} 张低清预览…`);
  const images = await mapPool(entries, 4, async (entry, index) => {
    const file = await entry.handle.getFile();
    const thumbUrl = await makeLocalThumbUrl(file);
    return {
      id: localImageId(entry.name, index),
      original_name: entry.name,
      extension: extensionOf(entry.name),
      thumbUrl,
      handle: entry.handle,
      scan_error: null,
    };
  });

  const failed = images.filter((image) => !image.thumbUrl).length;
  if (failed === images.length) {
    throw new Error("预览生成失败，请换 Chrome/Edge 重试");
  }

  state.dirHandle = dirHandle;
  return {
    folderId: `client-${Date.now().toString(36)}`,
    folderName: dirHandle.name || "images",
    folderMode: "client",
    images,
  };
}

async function selectFolderOnServer() {
  const data = await api("/api/folders/select", { method: "POST" });
  state.dirHandle = null;
  const images = (data.images || []).map((image) => ({
    ...image,
    thumbUrl: `/api/folders/${data.folder_id}/images/${image.id}/thumbnail`,
  }));
  return {
    folderId: data.folder_id,
    folderName: data.folder_name,
    folderMode: "server",
    images,
  };
}

$("choose-folder").onclick = async () => {
  try {
    state.maxNumber = readMaxNumber();
    $("choose-folder").disabled = true;
    let data;
    if (typeof window.showDirectoryPicker === "function") {
      data = await selectFolderOnClient();
    } else if (isLocalHost()) {
      data = await selectFolderOnServer();
    } else {
      throw new Error("局域网访问请用 Chrome 或 Edge，才能在本机选择文件夹并改名");
    }
    enterWorkspace(data);
    showMessage(`已加载 ${data.images.length} 张，点图编号即可`);
  } catch (error) {
    if (error?.name === "AbortError") showMessage("已取消选择文件夹");
    else showMessage(error.message || String(error));
  } finally {
    $("choose-folder").disabled = false;
  }
};

$("change-folder").onclick = () => $("choose-folder").click();

$("max-number").onchange = () => {
  try {
    const next = readMaxNumber();
    state.maxNumber = next;
    for (const [id, number] of [...state.assignments]) {
      if (number > next) state.assignments.delete(id);
    }
    if (state.selectedNumber && state.selectedNumber > next) {
      state.selectedNumber = null;
    }
    if (state.images.length) renderAll();
  } catch (error) {
    showMessage(error.message);
  }
};

$("modes").onclick = (event) => {
  const button = event.target.closest("button[data-mode]");
  if (!button) return;
  state.mode = button.dataset.mode;
  if (state.mode === "sequence") state.selectedNumber = null;
  document
    .querySelectorAll("#modes button")
    .forEach((item) => item.classList.toggle("active", item === button));
  $("mode-hint").textContent =
    state.mode === "sequence"
      ? "点击图片：自动分配最小空号；再点一次取消。"
      : "先点上方编号，再点图片绑定；点已编号图片可取消。";
  renderNumberStrip();
  renderGallery();
};

$("clear-all").onclick = () => {
  if (!state.assignments.size) return;
  if (!confirm("清空全部编号？")) return;
  state.assignments.clear();
  state.selectedNumber = null;
  renderAll();
};

function renderAll() {
  renderNumberStrip();
  renderGallery();
  renderSticky();
}

function renderNumberStrip() {
  const strip = $("number-strip");
  strip.replaceChildren();
  for (let n = 1; n <= state.maxNumber; n += 1) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "num-chip";
    btn.textContent = String(n);
    const owner = numberOwner(n);
    if (owner) btn.classList.add("filled");
    if (state.mode === "pick" && state.selectedNumber === n) btn.classList.add("selected");
    btn.title = owner
      ? `编号 ${n}：${state.images.find((img) => img.id === owner)?.original_name || ""}`
      : `编号 ${n} 空闲`;
    btn.onclick = () => {
      if (state.mode !== "pick") {
        if (owner) {
          clearAssignment(owner);
          renderAll();
        }
        return;
      }
      state.selectedNumber = state.selectedNumber === n ? null : n;
      renderNumberStrip();
      renderGallery();
    };
    strip.append(btn);
  }
}

function renderGallery() {
  const gallery = $("gallery");
  gallery.replaceChildren();
  if (!state.images.length) {
    gallery.innerHTML = `<p class="empty">文件夹里没有可用图片</p>`;
    return;
  }
  for (const image of state.images) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "tile";
    const number = state.assignments.get(image.id);
    if (number != null) card.classList.add("assigned");
    if (image.scan_error) card.classList.add("broken");

    const img = document.createElement("img");
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = image.original_name;
    if (image.thumbUrl) {
      img.src = image.thumbUrl;
    } else if (state.folderMode === "server" && state.folderId) {
      img.src = `/api/folders/${state.folderId}/images/${image.id}/thumbnail`;
    } else {
      img.alt = "预览失败";
    }
    img.onerror = () => {
      img.replaceWith(Object.assign(document.createElement("div"), {
        className: "tile-fallback",
        textContent: "无预览",
      }));
    };

    const badge = document.createElement("span");
    badge.className = "tile-num";
    badge.textContent = number != null ? String(number) : "·";

    const name = document.createElement("span");
    name.className = "tile-name";
    name.textContent = image.original_name;

    card.append(img, badge, name);
    card.onclick = () => onImageClick(image);
    gallery.append(card);
  }
}

function onImageClick(image) {
  if (image.scan_error) {
    showMessage(`图片无法使用：${image.original_name}`);
    return;
  }
  const current = state.assignments.get(image.id);

  if (state.mode === "sequence") {
    if (current != null) {
      clearAssignment(image.id);
    } else {
      const free = nextFreeNumber();
      if (free == null) {
        showMessage(`编号 1–${state.maxNumber} 已满，先取消某张或加大范围`);
        return;
      }
      setAssignment(image.id, free);
    }
    renderAll();
    return;
  }

  if (state.selectedNumber == null) {
    if (current != null) {
      clearAssignment(image.id);
      renderAll();
      return;
    }
    showMessage("请先点上方编号，再点图片");
    return;
  }
  if (current === state.selectedNumber) {
    clearAssignment(image.id);
  } else {
    setAssignment(image.id, state.selectedNumber);
  }
  state.selectedNumber = nextFreeNumber();
  renderAll();
}

function renderSticky() {
  const count = state.assignments.size;
  $("sticky-count").textContent = `${count} / ${state.maxNumber}`;
  if (!count) {
    $("sticky-detail").textContent = "点图片开始编号";
  } else {
    const nums = [...state.assignments.values()].sort((a, b) => a - b);
    $("sticky-detail").textContent = `已选：${nums.join("、")}`;
  }
  $("progress").textContent = `文件夹「${state.folderName}」· ${state.images.length} 张图 · 已编号 ${count} 张`;
  $("rename-btn").disabled = count === 0;
}

$("rename-btn").onclick = async () => {
  if (!state.assignments.size) return;
  try {
    $("rename-btn").disabled = true;
    $("rename-btn").textContent = "扫描文件夹…";

    // Always re-read disk names before planning — assignments use handles, names may change.
    let liveNames;
    if (state.folderMode === "client") {
      const scanned = await rescanClientFolder();
      liveNames = scanned.names;
      renderAll();
    }

    $("rename-btn").textContent = "生成预览…";
    const assignmentsList = [...state.assignments.entries()].map(([imageId, number]) => {
      const image = state.images.find((item) => item.id === imageId);
      if (!image) throw new Error("图片状态已失效，请重选文件夹");
      return { image, number };
    });
    if (!assignmentsList.length) {
      throw new Error("没有可改名的编号，请重新点选");
    }

    if (state.folderMode === "client") {
      state.plan = buildClientPlan(assignmentsList, liveNames);
    } else {
      // Server mode: force a fresh scan from disk before planning.
      const listed = await api(`/api/folders/${state.folderId}/images?rescan=true`);
      const byId = new Map((listed.images || []).map((image) => [image.id, image]));
      state.images = state.images.map((image) => {
        const fresh = byId.get(image.id);
        if (!fresh) return image;
        return {
          ...image,
          original_name: fresh.original_name,
          extension: fresh.extension,
          thumbUrl: `/api/folders/${state.folderId}/images/${image.id}/thumbnail?t=${Date.now()}`,
        };
      });
      renderAll();
      state.plan = await api(`/api/folders/${state.folderId}/plan`, {
        method: "POST",
        body: JSON.stringify({
          assignments: assignmentsList.map((item) => {
            const image = state.images.find((row) => row.id === item.image.id) || item.image;
            return { image_id: image.id, number: item.number };
          }),
        }),
      });
    }

    renderPlan();
    $("work-panel").classList.add("hidden");
    $("plan-panel").classList.remove("hidden");
  } catch (error) {
    showMessage(error.message);
  } finally {
    $("rename-btn").textContent = "预览并改名";
    renderSticky();
  }
};

function renderPlan() {
  const list = $("plan-list");
  list.replaceChildren();
  const ordered = [...state.plan.entries].sort((a, b) => {
    const an = state.assignments.get(a.item_id) || 0;
    const bn = state.assignments.get(b.item_id) || 0;
    return an - bn;
  });
  for (const entry of ordered) {
    const row = document.createElement("div");
    row.className = "plan-row";
    const num = document.createElement("b");
    num.className = "plan-num";
    num.textContent = String(state.assignments.get(entry.item_id) || "");
    const from = document.createElement("span");
    from.textContent = entry.source_name;
    const arrow = document.createElement("span");
    arrow.className = "arrow";
    arrow.textContent = "→";
    const to = document.createElement("span");
    to.textContent = entry.no_op ? "不变" : entry.target_name;
    row.append(num, from, arrow, to);
    list.append(row);
  }
}

$("back-work").onclick = () => {
  $("plan-panel").classList.add("hidden");
  $("work-panel").classList.remove("hidden");
};

$("commit-btn").onclick = async () => {
  if (!state.plan) return;
  const changed = state.plan.entries.filter((entry) => !entry.no_op).length;
  if (!confirm(`将原地重命名 ${changed} 张图片，是否继续？`)) return;
  try {
    $("commit-btn").disabled = true;

    if (state.folderMode === "client") {
      if (!state.dirHandle) throw new Error("本机会话已失效，请重新选择文件夹");
      await applyLocalRenamePlan(state.dirHandle, state.plan);
      // Refresh local names after rename
      const renamed = new Map(
        state.plan.entries.map((entry) => [entry.item_id, entry.target_name])
      );
      state.images = state.images.map((image) => {
        const nextName = renamed.get(image.id);
        if (!nextName) return image;
        return {
          ...image,
          original_name: nextName,
          extension: extensionOf(nextName),
        };
      });
    } else {
      const data = await api(`/api/folders/${state.folderId}/commit`, {
        method: "POST",
        body: JSON.stringify({ plan_id: state.plan.plan_id }),
      });
      state.images = (data.images || []).map((image) => ({
        ...image,
        thumbUrl: `/api/folders/${state.folderId}/images/${image.id}/thumbnail`,
      }));
    }

    state.assignments.clear();
    state.plan = null;
    $("plan-panel").classList.add("hidden");
    $("done-panel").classList.remove("hidden");
    const place = state.folderMode === "client" ? "本机文件夹" : "服务器文件夹";
    $("done-copy").textContent = `${place}已完成改名（${changed} 张）。可继续为剩余图片编号，或重新开始。`;
  } catch (error) {
    showMessage(error.message);
  } finally {
    $("commit-btn").disabled = false;
  }
};

$("continue-btn").onclick = () => {
  $("done-panel").classList.add("hidden");
  $("work-panel").classList.remove("hidden");
  renderAll();
};

$("restart-btn").onclick = () => {
  revokeThumbs();
  state.folderId = null;
  state.images = [];
  state.assignments.clear();
  state.plan = null;
  state.dirHandle = null;
  $("done-panel").classList.add("hidden");
  $("work-panel").classList.add("hidden");
  $("setup-panel").classList.remove("hidden");
  $("folder-name").textContent = "尚未选择";
  $("folder-count").textContent = "推荐 Chrome / Edge · 只扫描当前层 · 预览为低清缩略图";
};
