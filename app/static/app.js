const SUPPORTED_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".webp"]);

const state = {
  folderId: null,
  folderName: null,
  folderMode: null, // "client" | "server"
  dirHandle: null,
  fileHandles: null, // Map<name, FileSystemFileHandle>
  localRenamed: false,
  jobId: null,
  job: null,
  plan: null,
  manifestId: null,
  filter: "all",
  templateId: null,
  library: null,
  suppressDraft: false,
  draftTimer: null,
};

const $ = (id) => document.getElementById(id);

function showMessage(text) {
  $("message").textContent = text;
  $("message").classList.add("show");
  setTimeout(() => $("message").classList.remove("show"), 3500);
}

function isLocalHost() {
  const host = location.hostname;
  return host === "127.0.0.1" || host === "localhost" || host === "[::1]";
}

function extensionOf(name) {
  const index = name.lastIndexOf(".");
  return index >= 0 ? name.slice(index).toLowerCase() : "";
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

async function collectClientImages(dirHandle) {
  const files = [];
  const handles = new Map();
  for await (const [name, handle] of dirHandle.entries()) {
    if (handle.kind !== "file") continue;
    if (name.startsWith(".") || name.startsWith(".image-smart-renamer-")) continue;
    if (!SUPPORTED_EXTENSIONS.has(extensionOf(name))) continue;
    handles.set(name, handle);
    files.push(await handle.getFile());
  }
  return { files, handles };
}

async function applyLocalRenamePlan(dirHandle, plan, reverse = false) {
  if (!(await ensureDirPermission(dirHandle, "readwrite"))) {
    throw new Error("需要本机文件夹的读写权限才能改名");
  }
  const handles = new Map();
  for await (const [name, handle] of dirHandle.entries()) {
    if (handle.kind === "file") handles.set(name, handle);
  }

  const active = plan.entries.filter((entry) => !entry.no_op);
  const steps = reverse
    ? [
        // target -> temporary, then temporary -> source
        active.map((entry) => ({ from: entry.target_name, to: entry.temporary_name })),
        active.map((entry) => ({ from: entry.temporary_name, to: entry.source_name })),
      ]
    : [
        // source -> temporary, then temporary -> target
        active.map((entry) => ({ from: entry.source_name, to: entry.temporary_name })),
        active.map((entry) => ({ from: entry.temporary_name, to: entry.target_name })),
      ];

  for (const phase of steps) {
    for (const { from, to } of phase) {
      const handle = handles.get(from);
      if (!handle) throw new Error(`本机找不到文件：${from}`);
      if (handles.has(to)) throw new Error(`本机目标名已存在：${to}`);
      if (typeof handle.move !== "function") {
        throw new Error("当前浏览器不支持本机原地改名，请使用 Chrome 或 Edge");
      }
      await handle.move(to);
      handles.delete(from);
      handles.set(to, handle);
    }
  }

  state.fileHandles = handles;
}

async function selectFolderOnClient() {
  if (typeof window.showDirectoryPicker !== "function") {
    throw new Error("当前浏览器不支持本机选文件夹，请用 Chrome / Edge 打开");
  }
  const dirHandle = await window.showDirectoryPicker({ mode: "readwrite" });
  if (!(await ensureDirPermission(dirHandle, "readwrite"))) {
    throw new Error("未获得文件夹读写权限");
  }
  const { files, handles } = await collectClientImages(dirHandle);
  if (!files.length) {
    throw new Error("该文件夹当前层没有支持的图片（JPG/PNG/WebP）");
  }
  const form = new FormData();
  form.append("folder_name", dirHandle.name || "images");
  for (const file of files) {
    form.append("files", file, file.name);
  }
  const data = await api("/api/folders/from-upload", { method: "POST", body: form });
  state.dirHandle = dirHandle;
  state.fileHandles = handles;
  state.folderMode = "client";
  state.localRenamed = false;
  return data;
}

async function selectFolderOnServer() {
  const data = await api("/api/folders/select", { method: "POST" });
  state.dirHandle = null;
  state.fileHandles = null;
  state.folderMode = "server";
  state.localRenamed = false;
  return data;
}

async function warnIncomplete() {
  try {
    const data = await api("/api/history/incomplete");
    if (data.manifests.length) {
      showMessage(`发现 ${data.manifests.length} 条未完成的改名记录，请先检查历史目录。`);
    }
  } catch (error) {
    showMessage(error.message);
  }
}

function setDraftStatus(text) {
  $("draft-status").textContent = text;
}

function rules() {
  return [...document.querySelectorAll(".rule")].map((row) => ({
    number: Number(row.children[0].value),
    description: row.children[1].value.trim(),
  }));
}

function validRules() {
  const values = rules();
  if (
    !values.length ||
    !values.every(
      (rule) => Number.isInteger(rule.number) && rule.number > 0 && rule.description
    )
  ) {
    return null;
  }
  if (new Set(values.map((rule) => rule.number)).size !== values.length) {
    return null;
  }
  return values;
}

function validateSetup() {
  const values = validRules();
  $("start").disabled = !(values && state.folderId);
  $("save-template").disabled = !values;
  $("update-template").disabled = !(values && state.templateId);
  $("delete-template").disabled = !state.templateId;
}

function scheduleDraftSave() {
  if (state.suppressDraft) return;
  clearTimeout(state.draftTimer);
  state.draftTimer = setTimeout(() => {
    void persistDraft();
  }, 500);
}

async function persistDraft() {
  const values = validRules();
  if (!values) return;
  try {
    await api("/api/rules/draft", {
      method: "PUT",
      body: JSON.stringify({ rules: values, template_id: state.templateId }),
    });
    setDraftStatus("草稿已自动保存");
  } catch (error) {
    setDraftStatus(`草稿保存失败：${error.message}`);
  }
}

function addRule(number = "", description = "") {
  const row = document.createElement("div");
  row.className = "rule";
  const num = document.createElement("input");
  num.type = "number";
  num.min = "1";
  num.placeholder = "编号";
  num.value = number;
  const desc = document.createElement("input");
  desc.placeholder = "例如：白底正面红色运动鞋";
  desc.value = description;
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove";
  remove.textContent = "×";
  remove.onclick = () => {
    row.remove();
    validateSetup();
    scheduleDraftSave();
  };
  for (const input of [num, desc]) {
    input.oninput = () => {
      validateSetup();
      scheduleDraftSave();
    };
  }
  row.append(num, desc, remove);
  $("rules").append(row);
  validateSetup();
}

function renderRules(savedRules) {
  $("rules").replaceChildren();
  if (!savedRules || !savedRules.length) {
    addRule(1, "");
    return;
  }
  for (const rule of savedRules) {
    addRule(rule.number, rule.description);
  }
}

function renderTemplateSelect() {
  const select = $("template-select");
  const previous = state.templateId || "";
  select.replaceChildren();
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "未保存草稿";
  select.append(blank);
  for (const template of state.library?.templates || []) {
    const option = document.createElement("option");
    option.value = template.id;
    option.textContent = template.name;
    select.append(option);
  }
  select.value = previous;
  if (select.value !== previous) {
    select.value = "";
    state.templateId = null;
  }
  $("template-name").value =
    (state.library?.templates || []).find((item) => item.id === state.templateId)?.name ||
    $("template-name").value;
  validateSetup();
}

function applyTemplateSelection(templateId) {
  state.templateId = templateId || null;
  if (!templateId) {
    $("template-name").value = "";
    validateSetup();
    scheduleDraftSave();
    return;
  }
  const template = (state.library?.templates || []).find((item) => item.id === templateId);
  if (!template) {
    state.templateId = null;
    renderTemplateSelect();
    return;
  }
  state.suppressDraft = true;
  $("template-name").value = template.name;
  renderRules(template.rules);
  state.suppressDraft = false;
  validateSetup();
  scheduleDraftSave();
}

async function loadRuleLibrary() {
  setDraftStatus("正在恢复规则…");
  try {
    state.library = await api("/api/rules");
    const draftRules = state.library.draft?.rules || [];
    state.templateId = state.library.draft?.template_id || null;
    state.suppressDraft = true;
    renderRules(draftRules);
    renderTemplateSelect();
    state.suppressDraft = false;
    if (draftRules.length) {
      setDraftStatus("已恢复上次规则草稿");
    } else {
      setDraftStatus("规则会自动保存到本机，刷新后可恢复");
    }
  } catch (error) {
    state.suppressDraft = true;
    renderRules([]);
    renderTemplateSelect();
    state.suppressDraft = false;
    setDraftStatus(`规则恢复失败：${error.message}`);
    showMessage(error.message);
  }
}

$("add-rule").onclick = () => {
  addRule();
  scheduleDraftSave();
};

$("template-select").onchange = () => {
  applyTemplateSelection($("template-select").value || null);
};

$("save-template").onclick = async () => {
  const values = validRules();
  const name = $("template-name").value.trim();
  if (!values) {
    showMessage("请先填写完整且编号唯一的规则");
    return;
  }
  if (!name) {
    showMessage("请填写模板名称");
    return;
  }
  try {
    const created = await api("/api/rules/templates", {
      method: "POST",
      body: JSON.stringify({ name, rules: values }),
    });
    state.library = await api("/api/rules");
    state.templateId = created.id;
    renderTemplateSelect();
    setDraftStatus(`模板“${created.name}”已保存`);
    showMessage(`模板“${created.name}”已保存`);
  } catch (error) {
    showMessage(error.message);
  }
};

$("update-template").onclick = async () => {
  const values = validRules();
  const name = $("template-name").value.trim();
  if (!state.templateId || !values) {
    showMessage("请先选择模板并填写完整规则");
    return;
  }
  if (!name) {
    showMessage("请填写模板名称");
    return;
  }
  try {
    const updated = await api(`/api/rules/templates/${state.templateId}`, {
      method: "PUT",
      body: JSON.stringify({ name, rules: values }),
    });
    state.library = await api("/api/rules");
    state.templateId = updated.id;
    renderTemplateSelect();
    setDraftStatus(`模板“${updated.name}”已覆盖保存`);
    showMessage(`模板“${updated.name}”已覆盖保存`);
  } catch (error) {
    showMessage(error.message);
  }
};

$("delete-template").onclick = async () => {
  if (!state.templateId) return;
  const template = (state.library?.templates || []).find(
    (item) => item.id === state.templateId
  );
  const label = template?.name || "当前模板";
  if (!confirm(`删除模板“${label}”？当前规则会保留为草稿。`)) return;
  try {
    state.library = await api(`/api/rules/templates/${state.templateId}`, {
      method: "DELETE",
    });
    state.templateId = null;
    $("template-name").value = "";
    renderTemplateSelect();
    setDraftStatus("模板已删除，当前规则保留为草稿");
    showMessage("模板已删除");
    scheduleDraftSave();
  } catch (error) {
    showMessage(error.message);
  }
};

$("choose-folder").onclick = async () => {
  try {
    $("choose-folder").disabled = true;
    let data;
    // Prefer browser-native picker so the dialog opens on the machine running
    // the browser (Win/Mac), not the machine hosting the server.
    if (typeof window.showDirectoryPicker === "function") {
      data = await selectFolderOnClient();
    } else if (isLocalHost()) {
      data = await selectFolderOnServer();
    } else {
      throw new Error("局域网访问请用 Chrome 或 Edge，才能在本机选择文件夹并改名");
    }
    state.folderId = data.folder_id;
    state.folderName = data.folder_name;
    $("folder-name").textContent = data.folder_name;
    const where = state.folderMode === "client" ? "本机" : "服务器本机";
    $("folder-count").textContent = `${where} · 当前层 ${data.image_count} 张支持的图片`;
    validateSetup();
  } catch (error) {
    if (error?.name === "AbortError") {
      showMessage("已取消选择文件夹");
    } else {
      showMessage(error.message || String(error));
    }
  } finally {
    $("choose-folder").disabled = false;
  }
};

$("start").onclick = async () => {
  try {
    $("start").disabled = true;
    $("start").textContent = "识别中…";
    await persistDraft();
    const data = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ folder_id: state.folderId, rules: rules() }),
    });
    state.jobId = data.job_id;
    state.job = await api(`/api/jobs/${state.jobId}`);
    $("setup-panel").classList.add("hidden");
    $("review-panel").classList.remove("hidden");
    renderReview();
  } catch (error) {
    showMessage(error.message);
  } finally {
    $("start").textContent = "开始识别";
    validateSetup();
  }
};

function needsReview(item) {
  return (
    item.final_number == null ||
    (item.classification?.status === "needs_review" && !item.explicitly_reviewed)
  );
}

function visible(item) {
  if (state.filter === "review") return needsReview(item);
  if (state.filter === "failed") return Boolean(item.classification?.error_code);
  if (state.filter === "duplicate") return item.duplicate_count > 1;
  return true;
}

function renderReview() {
  const cards = $("cards");
  cards.replaceChildren();
  const unresolved = state.job.items.filter(needsReview).length;
  $("progress").textContent = `共 ${state.job.items.length} 张，${
    unresolved ? `${unresolved} 张需要确认` : "全部已有有效编号"
  }`;
  $("make-plan").disabled = unresolved > 0;
  for (const item of state.job.items.filter(visible)) {
    const card = document.createElement("article");
    card.className = "card";
    const img = document.createElement("img");
    img.src = `/api/jobs/${state.jobId}/items/${item.id}/thumbnail`;
    img.alt = "待审核图片";
    const body = document.createElement("div");
    body.className = "card-body";
    const name = document.createElement("div");
    name.className = "filename";
    name.textContent = item.original_name;
    const badges = document.createElement("div");
    badges.className = "badges";
    if (needsReview(item)) badge(badges, "待确认", "alert");
    if (item.classification?.error_code) badge(badges, "API 失败", "alert");
    if (item.duplicate_count > 1) badge(badges, `精确重复 ×${item.duplicate_count}`);
    const reason = document.createElement("p");
    reason.className = "reason";
    reason.textContent = item.classification?.reason || "尚无识别结果";
    const select = document.createElement("select");
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "请选择最终编号";
    select.append(empty);
    for (const rule of state.job.rules) {
      const option = document.createElement("option");
      option.value = rule.number;
      option.textContent = `${rule.number} · ${rule.description}`;
      select.append(option);
    }
    select.value = item.final_number ?? "";
    select.onchange = async () => {
      if (!select.value) return;
      try {
        await api(`/api/jobs/${state.jobId}/items/${item.id}`, {
          method: "PATCH",
          body: JSON.stringify({ final_number: Number(select.value) }),
        });
        state.job = await api(`/api/jobs/${state.jobId}`);
        renderReview();
      } catch (error) {
        showMessage(error.message);
      }
    };
    body.append(name, badges, reason, select);
    card.append(img, body);
    cards.append(card);
  }
}

function badge(parent, text, kind = "") {
  const span = document.createElement("span");
  span.className = `badge ${kind}`;
  span.textContent = text;
  parent.append(span);
}

$("filters").onclick = (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  state.filter = button.dataset.filter;
  document
    .querySelectorAll("#filters button")
    .forEach((item) => item.classList.toggle("active", item === button));
  renderReview();
};

$("make-plan").onclick = async () => {
  try {
    state.plan = await api(`/api/jobs/${state.jobId}/plan`, { method: "POST" });
    renderPlan();
    $("review-panel").classList.add("hidden");
    $("plan-panel").classList.remove("hidden");
  } catch (error) {
    showMessage(error.message);
  }
};

function renderPlan() {
  const list = $("plan-list");
  list.replaceChildren();
  for (const entry of state.plan.entries) {
    const row = document.createElement("div");
    row.className = "plan-row";
    const from = document.createElement("span");
    from.textContent = entry.source_name;
    const arrow = document.createElement("b");
    arrow.textContent = "→";
    const to = document.createElement("span");
    to.textContent = entry.no_op ? "不变" : entry.target_name;
    row.append(from, arrow, to);
    list.append(row);
  }
  const changed = state.plan.entries.filter((entry) => !entry.no_op).length;
  $("confirm-copy").textContent = `文件夹“${state.folderName}”将原地重命名 ${changed} 张图片。不删除图片，完成后可撤销最近一次操作。`;
}

$("back-review").onclick = () => {
  $("plan-panel").classList.add("hidden");
  $("review-panel").classList.remove("hidden");
};

$("commit").onclick = async () => {
  if (!confirm($("confirm-copy").textContent)) return;
  try {
    $("commit").disabled = true;
    // Client mode: rename on the browser machine first, then record on server.
    if (state.folderMode === "client" && !state.localRenamed) {
      if (!state.dirHandle) {
        throw new Error("本机会话已失效，请重新选择文件夹");
      }
      await applyLocalRenamePlan(state.dirHandle, state.plan, false);
      state.localRenamed = true;
    }
    const data = await api(`/api/jobs/${state.jobId}/commit`, {
      method: "POST",
      body: JSON.stringify({ plan_id: state.plan.plan_id }),
    });
    state.manifestId = data.manifest_id;
    $("plan-panel").classList.add("hidden");
    $("done-panel").classList.remove("hidden");
    const place = state.folderMode === "client" ? "本机文件夹" : "服务器文件夹";
    $("done-copy").textContent = `${place}改名已完成，操作记录 ${data.manifest_id.slice(0, 8)}。`;
    $("undo").disabled = false;
  } catch (error) {
    showMessage(error.message);
  } finally {
    $("commit").disabled = false;
  }
};

$("undo").onclick = async () => {
  if (!state.manifestId) {
    showMessage("找不到本次改名记录");
    return;
  }
  if (!confirm("撤销会把本次改名恢复为原文件名，是否继续？")) return;
  try {
    $("undo").disabled = true;
    if (state.folderMode === "client" && state.localRenamed) {
      if (!state.dirHandle || !state.plan) {
        throw new Error("本机会话已失效，无法自动撤销，请手动恢复文件名");
      }
      await applyLocalRenamePlan(state.dirHandle, state.plan, true);
      state.localRenamed = false;
    }
    await api("/api/history/undo", {
      method: "POST",
      body: JSON.stringify({
        folder_id: state.folderId,
        manifest_id: state.manifestId,
      }),
    });
    $("done-copy").textContent = "已撤销，所有文件名已恢复。";
  } catch (error) {
    $("undo").disabled = false;
    showMessage(error.message);
  }
};

warnIncomplete();
void loadRuleLibrary();
