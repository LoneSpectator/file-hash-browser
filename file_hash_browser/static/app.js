"use strict";

const state = {
  app: null,
  algorithms: [],
  algorithmById: new Map(),
  nodes: new Map(),
  roots: [],
  currentDirectory: null,
  currentItems: [],
  nextOffset: null,
  directoryRequest: 0,
  directoryLoading: false,
  selection: new Map(),
  selectionInputs: new Map(),
  rowById: new Map(),
  computeAlgorithms: new Set(),
  visibleColumns: loadColumnPreferences(),
  jobs: new Map(),
  jobsRefreshing: false,
  submitting: false,
};

const elements = {
  title: document.querySelector("#app-title"),
  privacyBadge: document.querySelector("#privacy-badge"),
  workerBadge: document.querySelector("#worker-badge"),
  jobsButton: document.querySelector("#jobs-button"),
  jobsCount: document.querySelector("#jobs-count"),
  tree: document.querySelector("#directory-tree"),
  treeRefresh: document.querySelector("#tree-refresh"),
  back: document.querySelector("#back-button"),
  breadcrumbs: document.querySelector("#breadcrumbs"),
  currentDirectory: document.querySelector("#current-directory"),
  selectDirectory: document.querySelector("#select-directory"),
  refreshDirectory: document.querySelector("#refresh-directory"),
  columnOptions: document.querySelector("#column-options"),
  tableRegion: document.querySelector("#table-region"),
  table: document.querySelector("#file-table"),
  tableHead: document.querySelector("#file-table-head"),
  tableBody: document.querySelector("#file-table-body"),
  emptyState: document.querySelector("#empty-state"),
  loadMore: document.querySelector("#load-more"),
  selectionCount: document.querySelector("#selection-count"),
  clearSelection: document.querySelector("#clear-selection"),
  algorithmPicker: document.querySelector("#algorithm-picker"),
  strategy: document.querySelector("#strategy-select"),
  startJob: document.querySelector("#start-job"),
  jobsDialog: document.querySelector("#jobs-dialog"),
  jobsClose: document.querySelector("#jobs-close"),
  jobsList: document.querySelector("#jobs-list"),
  clearJobs: document.querySelector("#clear-jobs"),
  toastRegion: document.querySelector("#toast-region"),
};


class ApiError extends Error {
  constructor(message, code, status) {
    super(message);
    this.code = code;
    this.status = status;
  }
}


function makeElement(tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}


async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  const request = { method, headers, credentials: "same-origin" };
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    request.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, request);
  const contentType = response.headers.get("Content-Type") || "";
  let payload = null;
  if (contentType.includes("application/json")) {
    payload = await response.json();
  }
  if (!response.ok) {
    const error = payload && payload.error;
    throw new ApiError(
      (error && error.message) || `请求失败（${response.status}）`,
      (error && error.code) || "request_failed",
      response.status,
    );
  }
  return payload;
}


function loadColumnPreferences() {
  try {
    const value = JSON.parse(localStorage.getItem("fhb-visible-columns") || "{}");
    return value && typeof value === "object" ? value : {};
  } catch (_error) {
    return {};
  }
}


function saveColumnPreferences() {
  try {
    localStorage.setItem("fhb-visible-columns", JSON.stringify(state.visibleColumns));
  } catch (_error) {
    // The interface remains usable when private browsing blocks localStorage.
  }
}


function showToast(message, type = "info") {
  const toast = makeElement("div", `toast${type === "error" ? " error" : ""}`, message);
  elements.toastRegion.append(toast);
  window.setTimeout(() => toast.remove(), 4200);
}


function publicError(error) {
  if (error instanceof ApiError) return error.message;
  if (error && error.name === "AbortError") return "";
  return "网络连接暂时不可用";
}


function upsertNode(raw, parent = undefined) {
  let node = state.nodes.get(raw.id);
  if (!node) {
    node = {
      id: raw.id,
      parent: parent || null,
      treeLoaded: false,
      treeLoading: false,
      treeChildIds: new Set(),
    };
    state.nodes.set(raw.id, node);
  }
  node.kind = raw.kind;
  node.displayName = raw.displayName;
  node.masked = Boolean(raw.masked);
  node.size = raw.size;
  node.modifiedAt = raw.modifiedAt;
  node.hashes = Array.isArray(raw.hashes) ? raw.hashes : (node.hashes || []);
  if (parent !== undefined && node.parent === null) node.parent = parent;
  return node;
}


function directoryPath(node) {
  const path = [];
  let current = node;
  const seen = new Set();
  while (current && !seen.has(current.id)) {
    path.unshift(current);
    seen.add(current.id);
    current = current.parent;
  }
  return path;
}


function renderLocation() {
  const current = state.currentDirectory;
  elements.breadcrumbs.textContent = "";
  if (!current) {
    elements.currentDirectory.textContent = "请选择目录";
    elements.back.disabled = true;
    elements.selectDirectory.disabled = true;
    elements.refreshDirectory.disabled = true;
    return;
  }
  const path = directoryPath(current);
  path.forEach((node, index) => {
    if (index) elements.breadcrumbs.append(makeElement("span", "breadcrumb-separator", "/"));
    const button = makeElement("button", "breadcrumb-button", node.displayName);
    button.type = "button";
    button.addEventListener("click", () => openDirectory(node));
    elements.breadcrumbs.append(button);
  });
  elements.currentDirectory.textContent = current.displayName;
  elements.back.disabled = !current.parent;
  elements.selectDirectory.disabled = false;
  elements.refreshDirectory.disabled = false;
  elements.selectDirectory.textContent = state.selection.has(current.id)
    ? "取消选择当前目录"
    : "选择当前目录";
}


function renderRoots() {
  elements.tree.textContent = "";
  const list = makeElement("ul", "tree-list");
  list.setAttribute("role", "group");
  for (const root of state.roots) {
    root.treeLoaded = false;
    root.treeLoading = false;
    root.treeChildIds = new Set();
    list.append(createTreeItem(root));
  }
  elements.tree.append(list);
  syncSelectionInputs();
  syncCurrentTree();
}


function createTreeItem(node) {
  const item = makeElement("li", "tree-item");
  item.setAttribute("role", "treeitem");
  item.dataset.nodeId = node.id;

  const row = makeElement("div", "tree-row");
  const toggle = makeElement("button", "tree-toggle", "+");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-label", `展开 ${node.displayName}`);
  toggle.addEventListener("click", () => toggleTreeNode(node));

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "tree-check";
  checkbox.setAttribute("aria-label", `选择目录 ${node.displayName}`);
  checkbox.addEventListener("change", () => toggleSelection(node, checkbox.checked));
  registerSelectionInput(node.id, checkbox);

  const icon = makeElement("span", "folder-icon");
  icon.setAttribute("aria-hidden", "true");
  const name = makeElement("button", "tree-name", node.displayName);
  name.type = "button";
  name.addEventListener("click", () => openDirectory(node));

  const children = makeElement("ul", "tree-list");
  children.setAttribute("role", "group");
  children.hidden = true;
  row.append(toggle, checkbox, icon, name);
  item.append(row, children);

  node.treeItem = item;
  node.treeRow = row;
  node.treeToggle = toggle;
  node.treeChildren = children;
  return item;
}


async function toggleTreeNode(node, forceOpen = false) {
  if (node.treeLoading) return;
  const isOpen = node.treeToggle.getAttribute("aria-expanded") === "true";
  if (isOpen && !forceOpen) {
    node.treeToggle.setAttribute("aria-expanded", "false");
    node.treeToggle.textContent = "+";
    node.treeChildren.hidden = true;
    return;
  }
  node.treeToggle.setAttribute("aria-expanded", "true");
  node.treeToggle.textContent = "−";
  node.treeChildren.hidden = false;
  if (!node.treeLoaded) await loadTreeChildren(node, 0);
}


async function loadTreeChildren(node, offset) {
  node.treeLoading = true;
  node.treeToggle.textContent = "·";
  if (offset === 0) {
    node.treeChildren.textContent = "";
    node.treeChildIds.clear();
  } else {
    const previousMore = node.treeChildren.querySelector(".tree-more");
    if (previousMore) previousMore.remove();
  }
  try {
    const pageSize = state.app.defaultPageSize;
    const data = await api(
      `/api/v1/nodes/${encodeURIComponent(node.id)}/children?offset=${offset}&limit=${pageSize}`,
    );
    let directoryCount = 0;
    for (const raw of data.items) {
      if (raw.kind !== "directory" || node.treeChildIds.has(raw.id)) continue;
      const child = upsertNode(raw, node);
      node.treeChildIds.add(child.id);
      node.treeChildren.append(createTreeItem(child));
      directoryCount += 1;
    }
    const pageEndsWithDirectory = data.items.length > 0 && data.items.at(-1).kind === "directory";
    if (data.nextOffset !== null && pageEndsWithDirectory) {
      const more = makeElement("button", "tree-more", "加载更多目录");
      more.type = "button";
      more.addEventListener("click", () => loadTreeChildren(node, data.nextOffset));
      node.treeChildren.append(more);
    } else if (offset === 0 && directoryCount === 0) {
      node.treeChildren.append(makeElement("li", "tree-message", "没有子目录"));
    }
    node.treeLoaded = true;
  } catch (error) {
    if (offset === 0) {
      const message = makeElement("li", "tree-message", publicError(error) || "目录读取失败");
      node.treeChildren.append(message);
      node.treeLoaded = false;
    } else {
      showToast(publicError(error), "error");
    }
  } finally {
    node.treeLoading = false;
    node.treeToggle.textContent = "−";
    syncSelectionInputs();
  }
}


function syncCurrentTree() {
  for (const node of state.nodes.values()) {
    if (node.treeRow) node.treeRow.classList.toggle("current", node === state.currentDirectory);
  }
}


async function openDirectory(node) {
  if (!node || node.kind !== "directory") return;
  state.currentDirectory = node;
  state.nextOffset = null;
  renderLocation();
  syncCurrentTree();
  await loadCurrentDirectory(true);
}


async function loadCurrentDirectory(reset, quiet = false) {
  const directory = state.currentDirectory;
  if (!directory || (state.directoryLoading && !reset)) return;
  state.directoryLoading = true;
  const requestNumber = ++state.directoryRequest;
  const offset = reset ? 0 : state.nextOffset;
  if (offset === null) {
    state.directoryLoading = false;
    return;
  }
  elements.tableRegion.setAttribute("aria-busy", "true");
  elements.loadMore.disabled = true;
  try {
    const data = await api(
      `/api/v1/nodes/${encodeURIComponent(directory.id)}/children?offset=${offset}&limit=${state.app.defaultPageSize}`,
    );
    if (requestNumber !== state.directoryRequest || directory !== state.currentDirectory) return;
    const nodes = data.items.map((raw) => upsertNode(raw, directory));
    if (reset) {
      state.currentItems = nodes;
    } else {
      const known = new Set(state.currentItems.map((item) => item.id));
      state.currentItems.push(...nodes.filter((item) => !known.has(item.id)));
    }
    state.nextOffset = data.nextOffset;
    renderFileTable();
  } catch (error) {
    if (!quiet) showToast(publicError(error), "error");
  } finally {
    if (requestNumber === state.directoryRequest) {
      state.directoryLoading = false;
      elements.tableRegion.setAttribute("aria-busy", "false");
      elements.loadMore.disabled = false;
    }
  }
}


function columnDefinitions() {
  return [
    { id: "size", label: "大小", kind: "size" },
    { id: "modified", label: "最后修改时间", kind: "modified" },
    { id: "last-hash", label: "最后哈希时间", kind: "last-hash" },
    ...state.algorithms.map((algorithm) => ({
      id: `hash:${algorithm.id}`,
      label: algorithm.label,
      kind: "hash",
      algorithmId: algorithm.id,
    })),
  ];
}


function isColumnVisible(id) {
  return Object.hasOwn(state.visibleColumns, id) ? Boolean(state.visibleColumns[id]) : true;
}


function renderColumnOptions() {
  elements.columnOptions.textContent = "";
  const legend = makeElement("legend", null, "选择表格列");
  elements.columnOptions.append(legend);
  for (const column of columnDefinitions()) {
    const label = makeElement("label", "column-option");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = isColumnVisible(column.id);
    input.addEventListener("change", () => {
      state.visibleColumns[column.id] = input.checked;
      saveColumnPreferences();
      renderFileTable();
    });
    label.append(input, document.createTextNode(column.label));
    elements.columnOptions.append(label);
  }
}


function renderFileTable() {
  elements.tableHead.textContent = "";
  elements.tableBody.textContent = "";
  state.rowById.clear();

  const nameHeader = makeElement("th", null, "名称");
  nameHeader.scope = "col";
  elements.tableHead.append(nameHeader);
  const columns = columnDefinitions().filter((column) => isColumnVisible(column.id));
  for (const column of columns) {
    const header = makeElement("th", null, column.label);
    header.scope = "col";
    elements.tableHead.append(header);
  }

  for (const node of state.currentItems) {
    const row = document.createElement("tr");
    row.classList.toggle("selected", state.selection.has(node.id));
    state.rowById.set(node.id, row);

    const nameCell = document.createElement("td");
    const nameContent = makeElement("div", "name-cell");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "row-check";
    checkbox.setAttribute("aria-label", `选择 ${node.displayName}`);
    checkbox.addEventListener("change", () => toggleSelection(node, checkbox.checked));
    registerSelectionInput(node.id, checkbox);
    const icon = makeElement("span", node.kind === "directory" ? "folder-icon" : "file-icon");
    icon.setAttribute("aria-hidden", "true");
    if (node.kind === "directory") {
      const nameButton = makeElement("button", "file-name-button", node.displayName);
      nameButton.type = "button";
      nameButton.addEventListener("click", () => openDirectory(node));
      nameContent.append(checkbox, icon, nameButton);
    } else {
      nameContent.append(checkbox, icon, makeElement("span", "name-text", node.displayName));
    }
    nameCell.append(nameContent);
    row.append(nameCell);

    for (const column of columns) {
      const cell = document.createElement("td");
      renderDataCell(cell, node, column);
      row.append(cell);
    }
    elements.tableBody.append(row);
  }

  const hasItems = state.currentItems.length > 0;
  elements.table.hidden = !hasItems;
  elements.emptyState.hidden = hasItems;
  if (!hasItems) {
    const heading = elements.emptyState.querySelector("h3");
    const paragraph = elements.emptyState.querySelector("p");
    if (state.currentDirectory) {
      heading.textContent = "这个目录是空的";
      paragraph.textContent = "未发现可显示的普通文件或子目录。";
    } else {
      heading.textContent = "从左侧选择一个目录";
      paragraph.textContent = "文件内容不会通过网页提供下载。";
    }
  }
  elements.loadMore.hidden = state.nextOffset === null;
  syncSelectionInputs();
}


function renderDataCell(cell, node, column) {
  if (column.kind === "size") {
    cell.textContent = node.kind === "file" ? formatSize(node.size) : "—";
    if (node.kind !== "file") cell.className = "muted-cell";
    return;
  }
  if (column.kind === "modified") {
    cell.textContent = formatTime(node.modifiedAt);
    if (!node.modifiedAt) cell.className = "muted-cell";
    return;
  }
  if (column.kind === "last-hash") {
    const values = node.hashes.map((item) => item.calculatedAt).filter(Boolean).sort();
    cell.textContent = values.length ? formatTime(values.at(-1)) : "—";
    if (!values.length) cell.className = "muted-cell";
    return;
  }
  if (column.kind === "hash") {
    const hash = node.hashes.find((item) => item.algorithmId === column.algorithmId);
    if (!hash) {
      cell.textContent = "—";
      cell.className = "muted-cell";
      return;
    }
    const button = makeElement("button", "hash-value", hash.value);
    button.type = "button";
    button.setAttribute("aria-label", `复制 ${column.label} 哈希值`);
    button.addEventListener("click", () => copyHash(hash.value, column.label));
    cell.append(button);
  }
}


function formatSize(value) {
  if (!Number.isFinite(value)) return "—";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let size = value;
  let index = -1;
  do {
    size /= 1024;
    index += 1;
  } while (size >= 1024 && index < units.length - 1);
  const digits = size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${units[index]}`;
}


function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}


async function copyHash(value, label) {
  try {
    await navigator.clipboard.writeText(value);
    showToast(`${label} 已复制`);
  } catch (_error) {
    const input = document.createElement("textarea");
    input.value = value;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.append(input);
    input.select();
    document.execCommand("copy");
    input.remove();
    showToast(`${label} 已复制`);
  }
}


function registerSelectionInput(nodeId, input) {
  if (!state.selectionInputs.has(nodeId)) state.selectionInputs.set(nodeId, new Set());
  state.selectionInputs.get(nodeId).add(input);
}


function syncSelectionInputs() {
  for (const [nodeId, inputs] of state.selectionInputs) {
    for (const input of [...inputs]) {
      if (!input.isConnected) {
        inputs.delete(input);
      } else {
        input.checked = state.selection.has(nodeId);
      }
    }
    if (!inputs.size) state.selectionInputs.delete(nodeId);
  }
  for (const [nodeId, row] of state.rowById) {
    row.classList.toggle("selected", state.selection.has(nodeId));
  }
}


function toggleSelection(node, selected) {
  if (selected) state.selection.set(node.id, node);
  else state.selection.delete(node.id);
  syncSelectionInputs();
  renderSelectionSummary();
  renderLocation();
}


function renderSelectionSummary() {
  let directories = 0;
  let files = 0;
  for (const node of state.selection.values()) {
    if (node.kind === "directory") directories += 1;
    else files += 1;
  }
  const pieces = [];
  if (directories) pieces.push(`${directories} 个目录（含子目录）`);
  if (files) pieces.push(`${files} 个文件`);
  elements.selectionCount.textContent = pieces.length ? pieces.join("，") : "尚未选择文件或目录";
  elements.clearSelection.disabled = state.selection.size === 0;
  updateStartButton();
}


function clearSelection() {
  state.selection.clear();
  syncSelectionInputs();
  renderSelectionSummary();
  renderLocation();
}


function renderAlgorithmPicker() {
  const legend = elements.algorithmPicker.querySelector("legend");
  elements.algorithmPicker.textContent = "";
  elements.algorithmPicker.append(legend || makeElement("legend", null, "计算算法"));
  if (!state.computeAlgorithms.size) {
    const preferred = state.algorithms.find((item) => item.id === "sha256") || state.algorithms[0];
    if (preferred) state.computeAlgorithms.add(preferred.id);
  }
  for (const algorithm of state.algorithms) {
    const label = makeElement("label", "algorithm-choice");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = algorithm.id;
    input.checked = state.computeAlgorithms.has(algorithm.id);
    input.addEventListener("change", () => {
      if (input.checked) state.computeAlgorithms.add(algorithm.id);
      else state.computeAlgorithms.delete(algorithm.id);
      updateStartButton();
    });
    label.append(input, document.createTextNode(algorithm.label));
    elements.algorithmPicker.append(label);
  }
  updateStartButton();
}


function updateStartButton() {
  elements.startJob.disabled =
    state.submitting || state.selection.size === 0 || state.computeAlgorithms.size === 0;
  elements.startJob.textContent = state.submitting ? "正在提交…" : "开始后台计算";
}


function idempotencyKey() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const random = crypto.getRandomValues(new Uint32Array(4));
  return `web-${[...random].map((item) => item.toString(16)).join("-")}`;
}


async function submitJob() {
  if (elements.startJob.disabled) return;
  state.submitting = true;
  updateStartButton();
  const selected = [...state.selection.values()];
  try {
    const data = await api("/api/v1/jobs", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: {
        items: selected.map((node) => ({
          entryId: node.id,
          recursive: node.kind === "directory",
        })),
        algorithmIds: [...state.computeAlgorithms],
        strategy: elements.strategy.value,
      },
    });
    state.jobs.set(data.job.id, data.job);
    clearSelection();
    renderJobs();
    if (!elements.jobsDialog.open) elements.jobsDialog.showModal();
    showToast(data.created ? "后台任务已提交" : "相同任务已经提交过");
    window.setTimeout(refreshJobs, 350);
  } catch (error) {
    showToast(publicError(error), "error");
  } finally {
    state.submitting = false;
    updateStartButton();
  }
}


function statusLabel(status) {
  return {
    queued: "等待计算",
    enumerating: "正在读取目录",
    running: "正在计算",
  }[status] || "处理中";
}


function renderJobs() {
  elements.jobsList.textContent = "";
  const jobs = [...state.jobs.values()];
  elements.jobsCount.textContent = String(jobs.length);
  elements.jobsCount.hidden = jobs.length === 0;
  elements.clearJobs.disabled = jobs.length === 0;
  if (!jobs.length) {
    const empty = makeElement("div", "jobs-empty");
    const block = makeElement("div");
    block.append(
      makeElement("strong", null, "当前没有后台任务"),
      document.createElement("br"),
      document.createTextNode("已完成任务不会保留记录。"),
    );
    empty.append(block);
    elements.jobsList.append(empty);
    return;
  }

  for (const job of jobs) {
    const card = makeElement("article", `job-card ${job.status}`);
    const content = makeElement("div");
    const titleRow = makeElement("div", "job-title-row");
    titleRow.append(
      makeElement("span", "job-status", statusLabel(job.status)),
      makeElement("span", "job-id", `#${job.id.slice(0, 8)}`),
    );
    const algorithmNames = job.algorithmIds
      .map((id) => state.algorithmById.get(id)?.label || id)
      .join("、");
    const progress = job.progress || {};
    const metaParts = [algorithmNames];
    if (job.status === "running") {
      metaParts.push(`已处理 ${progress.processed || 0} / ${progress.discovered || 0}`);
    } else if (job.status === "enumerating") {
      metaParts.push("正在统计文件数量");
    } else {
      metaParts.push(`${job.selectedCount || 0} 个选择项`);
    }
    content.append(titleRow, makeElement("div", "job-meta", metaParts.join(" · ")));
    const progressElement = document.createElement("progress");
    progressElement.className = "job-progress";
    if (job.status === "running" && progress.discovered > 0) {
      progressElement.max = progress.discovered;
      progressElement.value = Math.min(progress.processed || 0, progress.discovered);
    }
    content.append(progressElement);

    const action = makeElement(
      "button",
      "button danger job-action",
      job.status === "queued" ? "删除" : "中断",
    );
    action.type = "button";
    action.addEventListener("click", () => cancelJob(job, action));
    card.append(content, action);
    elements.jobsList.append(card);
  }
}


async function refreshJobs() {
  if (state.jobsRefreshing) return;
  state.jobsRefreshing = true;
  const previousIds = new Set(state.jobs.keys());
  try {
    const data = await api("/api/v1/jobs?limit=100");
    state.jobs = new Map(data.jobs.map((job) => [job.id, job]));
    const disappeared = [...previousIds].some((jobId) => !state.jobs.has(jobId));
    renderJobs();
    if (disappeared && state.currentDirectory) {
      await loadCurrentDirectory(true, true);
    }
  } catch (error) {
    if (elements.jobsDialog.open) showToast(publicError(error), "error");
  } finally {
    state.jobsRefreshing = false;
  }
}


async function cancelJob(job, button) {
  button.disabled = true;
  try {
    const path = `/api/v1/jobs/${encodeURIComponent(job.id)}`;
    if (job.status === "queued") await api(path, { method: "DELETE" });
    else await api(`${path}/cancel`, { method: "POST" });
    state.jobs.delete(job.id);
    renderJobs();
    showToast(job.status === "queued" ? "等待任务已删除" : "任务已中断并删除");
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      state.jobs.delete(job.id);
      renderJobs();
    } else {
      showToast(publicError(error), "error");
      button.disabled = false;
    }
  }
}


async function clearAllJobs() {
  if (!state.jobs.size) return;
  if (!window.confirm("确定中断并删除全部后台任务吗？")) return;
  elements.clearJobs.disabled = true;
  try {
    const data = await api("/api/v1/jobs", { method: "DELETE" });
    state.jobs.clear();
    renderJobs();
    showToast(`已清除 ${data.removed} 个任务`);
  } catch (error) {
    showToast(publicError(error), "error");
  }
}


function scheduleJobPolling() {
  const delay = state.jobs.size || elements.jobsDialog.open ? 1800 : 6000;
  window.setTimeout(async () => {
    await refreshJobs();
    scheduleJobPolling();
  }, delay);
}


function bindEvents() {
  elements.treeRefresh.addEventListener("click", async () => {
    renderRoots();
    for (const root of state.roots) await toggleTreeNode(root, true);
  });
  elements.back.addEventListener("click", () => {
    if (state.currentDirectory?.parent) openDirectory(state.currentDirectory.parent);
  });
  elements.selectDirectory.addEventListener("click", () => {
    if (!state.currentDirectory) return;
    toggleSelection(state.currentDirectory, !state.selection.has(state.currentDirectory.id));
  });
  elements.refreshDirectory.addEventListener("click", () => loadCurrentDirectory(true));
  elements.loadMore.addEventListener("click", () => loadCurrentDirectory(false));
  elements.clearSelection.addEventListener("click", clearSelection);
  elements.startJob.addEventListener("click", submitJob);
  elements.jobsButton.addEventListener("click", () => {
    if (!elements.jobsDialog.open) elements.jobsDialog.showModal();
    refreshJobs();
  });
  elements.jobsClose.addEventListener("click", () => elements.jobsDialog.close());
  elements.clearJobs.addEventListener("click", clearAllJobs);
  elements.jobsDialog.addEventListener("click", (event) => {
    if (event.target === elements.jobsDialog) elements.jobsDialog.close();
  });
}


async function initialize() {
  bindEvents();
  try {
    const data = await api("/api/v1/bootstrap");
    state.app = data.app;
    state.algorithms = data.algorithms;
    state.algorithmById = new Map(data.algorithms.map((item) => [item.id, item]));
    state.roots = data.roots.map((raw) => upsertNode(raw, undefined));
    document.title = data.app.title;
    elements.title.textContent = data.app.title;
    elements.privacyBadge.textContent =
      data.app.nameVisibility === "masked" ? "名称已半隐藏" : "显示完整名称";
    elements.workerBadge.textContent = `${data.app.parallelism.mode === "auto" ? "自动" : "固定"}并行 · ${data.app.parallelism.effective}`;
    renderAlgorithmPicker();
    renderColumnOptions();
    renderFileTable();
    renderRoots();
    renderJobs();
    renderSelectionSummary();
    if (state.roots.length) {
      await Promise.all(state.roots.map((root) => toggleTreeNode(root, true)));
      await openDirectory(state.roots[0]);
    } else {
      elements.tree.append(makeElement("p", "tree-message", "没有可用的授权目录"));
    }
    await refreshJobs();
    scheduleJobPolling();
  } catch (error) {
    elements.tree.textContent = "";
    elements.tree.append(makeElement("p", "tree-message", publicError(error)));
    showToast(publicError(error), "error");
  }
}


initialize();
