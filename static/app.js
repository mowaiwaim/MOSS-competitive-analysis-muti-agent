const state = {
  task: null,
  dag: null,
  report: null,
  sources: [],
  claims: [],
  events: [],
  history: [],
  historyArchived: false,
  onlineSearchEnabled: false,
  surveyResearchSelected: false,
  uploadedFileName: "",
  uploadedFileNames: [],
  uploadedFiles: [],
  selectedText: "",
  fullMode: true,
  pollTimer: null,
  pollBusy: false,
  historyMenuTaskId: "",
  pendingDeleteTaskId: "",
  activeSourcesKey: "",
  activeModalKey: "",
  manualFindingId: "",
  manualClaimId: "",
  manualQaAction: "",
  manualSubmitting: false,
  questionnaireDesigns: [],
  surveyAnalyses: [],
  interviewAnalyses: [],
};

const planSteps = [
  "准备任务",
  "采集资料",
  "结构化分析",
  "质检复核",
  "生成报告",
];

const focusAreaLabels = {
  "功能对比": "核心能力",
  "定价": "商业模式与定价",
  "用户评价": "用户反馈",
  "用户画像": "用户与场景",
  "SWOT": "SWOT与壁垒",
  "市场与赛道": "市场与赛道",
  "竞品分层": "竞品分层",
  "核心能力": "核心能力",
  "商业模式与定价": "商业模式与定价",
  "增长与分发": "增长与分发",
  "用户与场景": "用户与场景",
  "SWOT与壁垒": "SWOT与壁垒",
  "机会建议": "机会建议",
};

const defaultFocusAreas = [
  "市场与赛道",
  "竞品分层",
  "核心能力",
  "商业模式与定价",
  "增长与分发",
  "用户与场景",
  "SWOT与壁垒",
  "机会建议",
];

const $ = (selector) => document.querySelector(selector);
const shanghaiFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (value === undefined || value === null || value === false) return;
    if (key === "className") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style" && typeof value === "string") node.style.cssText = value;
    else if (key === "style" && value && typeof value === "object") Object.assign(node.style, value);
    else if (key === "dataset") {
      Object.entries(value).forEach(([dataKey, dataValue]) => {
        node.dataset[dataKey] = dataValue;
      });
    } else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else {
      node.setAttribute(key, String(value));
    }
  });
  const childList = Array.isArray(children) ? children : [children];
  childList.forEach((child) => {
    if (child === null || child === undefined) return;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  });
  return node;
}

function svgEl(tag, attrs = {}, children = []) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (value === undefined || value === null || value === false) return;
    node.setAttribute(key, String(value));
  });
  const childList = Array.isArray(children) ? children : [children];
  childList.forEach((child) => {
    if (child === null || child === undefined) return;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  });
  return node;
}

function show(viewId) {
  ["#createView", "#boardView", "#reportView"].forEach((selector) => $(selector).classList.add("hidden"));
  $(viewId).classList.remove("hidden");
  syncNavTabs();
}

function setHidden(selector, hidden) {
  const node = $(selector);
  if (!node) return;
  node.classList.toggle("hidden", hidden);
}

function resetFloatingPosition(selector) {
  const panel = $(selector);
  panel.style.left = "";
  panel.style.top = "";
  panel.style.right = "";
}

function showToast(message, type = "info") {
  let toast = $("#toast");
  if (!toast) {
    toast = el("div", { id: "toast", className: "toast hidden", role: "status", "aria-live": "polite" });
    document.body.append(toast);
  }
  toast.textContent = message;
  toast.className = `toast ${type}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    toast.classList.add("hidden");
  }, 2800);
}

async function withButtonLoading(button, loadingText, work) {
  if (!button) return work();
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = loadingText;
  try {
    return await work();
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

function showCreateHome() {
  show("#createView");
  setHidden("#floatingPlan", true);
  setHidden("#planChip", true);
  setHidden("#sourceMenu", true);
  setHidden("#researchPanel", true);
  setHidden("#researchFloatBall", false);
  $("#sourceMenuButton").setAttribute("aria-expanded", "false");
}

function startNewConversation() {
  stopTaskPolling();
  state.task = null;
  state.dag = null;
  state.report = null;
  state.sources = [];
  state.claims = [];
  state.events = [];
  state.onlineSearchEnabled = false;
  state.surveyResearchSelected = false;
  state.uploadedFileName = "";
  state.uploadedFileNames = [];
  state.uploadedFiles = [];
  state.fullMode = false;
  $("#taskPromptInput").value = "";
  $("#uploadInput").value = "";
  document.querySelectorAll("#focusOptions input[type='checkbox']").forEach((checkbox) => {
    checkbox.checked = true;
  });
  $("#startButton").disabled = false;
  $("#startButton").textContent = "开始分析";
  updatePromptFeedback();
  updateSourceModeStatus();
  renderHistory();
  revealTaskNav();
  showCreateHome();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const raw = await response.text();
    let message = raw;
    try {
      const parsed = JSON.parse(raw);
      message = parsed.message || parsed.error || raw;
    } catch (error) {
      message = raw.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
    }
    throw new Error(message || `请求失败：${response.status}`);
  }
  return response.json();
}

function splitInput(value) {
  return value
    .split(/[\n,，、]+|(?:以及|和|及)/u)
    .map((item) => item.trim())
    .map((item) => item.replace(/^(和|及|以及)\s*/, "").replace(/[。；;,.，、]+$/g, ""))
    .filter(Boolean);
}

function extractUrls(value) {
  return Array.from(value.matchAll(/https?:\/\/[^\s，,。；;）)]+/gi))
    .map((match) => match[0].replace(/[。；;,.，、]+$/g, ""))
    .filter(Boolean);
}

function stripUrls(value) {
  return value.replace(/https?:\/\/[^\s，,。；;）)]+/gi, " ").replace(/\s+/g, " ").trim();
}

function formatDateTime(value) {
  if (!value) return "";
  const normalized = /Z$|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return shanghaiFormatter.format(date).replace(/\//g, "-");
}

function getFocusAreas() {
  const focusOptions = $("#focusOptions");
  if (!focusOptions) return [...defaultFocusAreas];
  const selected = Array.from(focusOptions.querySelectorAll("input:checked")).map((input) => input.value);
  return selected.length ? selected : [...defaultFocusAreas];
}

function inferIndustryFromCompetitors(competitors, fallback = "") {
  const text = `${fallback} ${competitors.join(" ")}`.toLowerCase();
  if (/(比亚迪|byd|小鹏|小鹏汽车|xpeng|xiaopeng|理想|理想汽车|li auto|蔚来|nio|特斯拉|tesla|问界|aito|极氪|zeekr|新能源汽车|智能汽车|汽车)/i.test(text)) {
    return "新能源汽车与智能汽车";
  }
  if (/(chatgpt|openai|豆包|doubao|claude|deepseek|kimi|通义|文心|gemini)/i.test(text)) {
    return "AI 大模型与智能助手";
  }
  if (/(飞书|notion|airtable|slack|trello|asana)/i.test(text)) {
    return "协同办公与知识管理";
  }
  if (/(淘宝|京东|拼多多|amazon|shopify)/i.test(text)) {
    return "电商与零售平台";
  }
  if (/(抖音|快手|小红书|bilibili|youtube|tiktok)/i.test(text)) {
    return "内容社区与短视频平台";
  }
  return fallback && !["ai", "待识别行业"].includes(fallback.toLowerCase()) ? fallback : "待识别行业";
}

function cleanupIndustry(value) {
  return value
    .trim()
    .replace(/^(请|麻烦)?(帮我)?(分析一下|分析|研究一下|研究|对比一下|对比|做一个|做下|看看)?/, "")
    .replace(/[的在]$/g, "")
    .trim();
}

function cleanupCompetitorSegment(value) {
  return value
    .replace(/(?:，|,|。|；|;)\s*(重点|主要|关注|看|分析重点|希望|想看).*$/u, "")
    .replace(/^(的|中|里|下|竞品|产品|包括|有|是|为|分析|对比)+/u, "")
    .replace(/等竞品?$/u, "")
    .trim();
}

function parseTaskPrompt(rawValue) {
  const raw = rawValue.trim().replace(/\s+/g, " ");
  if (!raw) return { industry: "待识别行业", competitors: [], websites: [], raw };
  const websites = extractUrls(raw);

  let industry = "";
  let competitorSegment = raw;
  const normalized = stripUrls(raw)
    .replace(/^(请|麻烦)?(帮我)?(分析一下|分析|研究一下|研究|对比一下|对比|做一个|做下|看看)?/, "")
    .trim();
  const industryFirst = normalized.match(/(.+?)(领域|行业|赛道)\s*(的|中|里|下)?\s*(.+)$/u);
  const competitorFirst = normalized.match(/(.+?)\s*(在|属于)\s*(.+?)(领域|行业|赛道)/u);
  const industryOnly = normalized.match(/^(.+?)(领域|行业|赛道)$/u);
  const colonStyle = normalized.match(/^(.{1,40})[:：]\s*(.+)$/u);

  if (colonStyle) {
    industry = cleanupIndustry(colonStyle[1]);
    competitorSegment = colonStyle[2];
  } else if (industryFirst) {
    industry = cleanupIndustry(industryFirst[1]);
    competitorSegment = industryFirst[4];
  } else if (competitorFirst) {
    industry = cleanupIndustry(competitorFirst[3]);
    competitorSegment = competitorFirst[1];
  } else if (industryOnly) {
    industry = cleanupIndustry(industryOnly[1]);
    competitorSegment = "";
  } else {
    competitorSegment = normalized;
  }

  const competitors = splitInput(cleanupCompetitorSegment(competitorSegment));
  const inferredIndustry = inferIndustryFromCompetitors(competitors, industry || "待识别行业");
  return {
    industry: inferredIndustry,
    competitors,
    websites,
    raw,
  };
}

function updatePromptFeedback() {
  const feedback = $("#inputFeedback");
  const parsed = parseTaskPrompt($("#taskPromptInput").value);
  feedback.classList.remove("error");
  if (!parsed.raw) {
    feedback.textContent = "输入竞品名称即可开始";
    return;
  }
  if (!parsed.competitors.length) {
    feedback.textContent = "还没有识别到竞品名称，请补充至少一个竞品。";
    feedback.classList.add("error");
    return;
  }
  feedback.textContent = `将创建：${parsed.industry} / ${parsed.competitors.join("、")}`;
}

function updateSourceModeStatus() {
  const mode = getSourceMode();
  const uploadNames = selectedUploadNames();
  const suffix = uploadNames.length ? ` · ${uploadNames.length} 份材料` : "";
  const researchSuffix = state.surveyResearchSelected ? " · 问卷调研" : "";
  $("#sourceModeStatus").textContent = `数据来源：${mode}${suffix}${researchSuffix}`;
  $("#onlineSearchButton").classList.toggle("selected", state.onlineSearchEnabled);
  $("#uploadMenuButton").classList.toggle("selected", uploadNames.length > 0);
  $("#questionnaireMenuButton")?.classList.toggle("selected", state.surveyResearchSelected);
  $("#onlineSearchButton").textContent = state.onlineSearchEnabled ? "✓ 联网搜索" : "联网搜索";
  $("#uploadMenuButton").textContent = uploadNames.length ? `继续上传（已选 ${uploadNames.length}）` : "上传文件";
  if ($("#questionnaireMenuButton")) {
    $("#questionnaireMenuButton").textContent = state.surveyResearchSelected ? "✓ 问卷调研" : "问卷调研";
  }
  renderUploadChips();
}

function getSourceMode() {
  const hasUploads = selectedUploadFiles().length > 0;
  const hasUserUrls = extractUrls($("#taskPromptInput")?.value || "").length > 0;
  if (state.onlineSearchEnabled && hasUploads) return "实时采集+上传资料";
  if (state.onlineSearchEnabled) return "实时采集";
  if (hasUploads || hasUserUrls) return "上传资料";
  return "缓存样例";
}

function selectedUploadFiles() {
  return state.uploadedFiles || [];
}

function selectedUploadNames() {
  return selectedUploadFiles().map((file) => file.name);
}

function uploadFileKey(file) {
  return `${file.name}::${file.size}::${file.lastModified}`;
}

function addUploadFiles(files) {
  const existing = new Set(selectedUploadFiles().map(uploadFileKey));
  Array.from(files || []).forEach((file) => {
    const key = uploadFileKey(file);
    if (existing.has(key)) return;
    state.uploadedFiles.push(file);
    existing.add(key);
  });
  state.uploadedFileName = state.uploadedFiles[0]?.name || "";
  state.uploadedFileNames = selectedUploadNames();
}

function removeUploadFile(index) {
  state.uploadedFiles.splice(index, 1);
  state.uploadedFileName = state.uploadedFiles[0]?.name || "";
  state.uploadedFileNames = selectedUploadNames();
  updateSourceModeStatus();
}

function renderUploadChips() {
  const wrap = $("#uploadChips");
  if (!wrap) return;
  wrap.replaceChildren();
  selectedUploadFiles().forEach((file, index) => {
    wrap.append(
      el("span", { className: "upload-chip" }, [
        el("span", { className: "upload-chip-name", text: file.name }),
        el("button", {
          type: "button",
          className: "upload-chip-remove",
          "aria-label": `移除 ${file.name}`,
          onClick: () => removeUploadFile(index),
        }, "×"),
      ])
    );
  });
  setHidden("#uploadChips", selectedUploadFiles().length === 0);
}

function resizePromptInput() {
  const input = $("#taskPromptInput");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 130)}px`;
}

function formatDuration(ms) {
  const raw = Number(ms) || 0;
  if (raw > 0 && raw < 1000) return "< 1 秒";
  const seconds = Math.max(raw > 0 ? 1 : 0, Math.round(raw / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, "0")} 秒`;
}

function statusClass(status) {
  if (["completed", "已完成", "passed", "confirmed", "reportable", "fixed"].includes(status)) return "pass";
  if (["rejected", "被打回", "qa_failed", "failed", "stopped"].includes(status)) return "danger";
  return "warn";
}

function statusLabel(status = "") {
  return {
    created: "已创建",
    collecting: "采集中",
    analyzing: "分析中",
    reanalyzing: "重做分析中",
    qa_review: "质检中",
    qa_rework: "复核处理中",
    qa_failed: "需人工复核",
    qa_passed: "质检通过",
    reporting: "报告生成中",
    completed: "已完成",
    failed: "失败",
    stopped: "已停止",
    passed: "通过",
    rejected: "需复核",
    rerun_completed: "已完成",
    needs_review: "需人工复核",
    reportable: "可进入报告",
    confirmed: "已确认",
    open: "待复核",
    manual_pending: "待人工复核",
    fixed: "已修复",
    no_change: "暂无变化",
    running: "运行中",
  }[status] || status || "等待中";
}

function manualFindingReviewState(finding = {}) {
  return finding.manual_review_state || (finding.meta && finding.meta.manual_review_state) || "";
}

function findingStatusLabel(finding = {}) {
  const reviewState = manualFindingReviewState(finding);
  if (reviewState === "awaiting_recheck") return "待复核/复检中";
  if (reviewState === "system_rechecked") return "系统复检通过";
  if (reviewState === "manual_confirmed") return "人工确认已修复";
  if (reviewState === "needs_more_input") return "需继续补充";
  if (finding.fix_status === "fixed") return "系统复检通过";
  return statusLabel(finding.fix_status);
}

function findingStatusClass(finding = {}) {
  const reviewState = manualFindingReviewState(finding);
  if (["system_rechecked", "manual_confirmed"].includes(reviewState)) return "pass";
  if (reviewState === "needs_more_input") return "danger";
  if (reviewState === "awaiting_recheck") return "warn";
  return statusClass(finding.fix_status);
}

function findingRecheckText(finding = {}, canRecheckWithoutInput = false) {
  if (finding.recheck_result) return finding.recheck_result;
  const reviewState = manualFindingReviewState(finding);
  if (reviewState === "awaiting_recheck") return "已收到人工补充或修正，等待系统复检。";
  if (reviewState === "system_rechecked") return "系统自动复检已通过，报告版本已刷新。";
  if (reviewState === "manual_confirmed") return "用户已确认无误，结论已记录为人工确认。";
  if (reviewState === "needs_more_input") return "复检仍未通过，请继续补充来源、修订结论或打回。";
  if (finding.fix_status === "fixed") return "系统自动复检已通过，报告版本已刷新。";
  return canRecheckWithoutInput
    ? "已发生修复动作，可重新质检。"
    : "未修复：直接重新质检不会通过，请先执行修复动作或补充材料。";
}

function providerLabel(value = "") {
  const raw = String(value || "").trim();
  if (raw.includes("、") || raw.includes("/")) {
    return raw
      .split(/[、/]/)
      .map((item) => providerLabel(item.trim()))
      .filter(Boolean)
      .join("、");
  }
  return {
    doubao: "豆包大模型",
    "doubao-react": "豆包 ReAct 深度分析",
    "deepseek-react": "DeepSeek ReAct 深度分析",
    "zhipu-react": "智谱深度分析",
    "local-react-fallback": "本地深度分析备用规则",
    "report-renderer": "报告排版生成",
    mock: "规则模式",
    volc_search: "火山联网搜索",
    official_seed: "官方种子来源",
    none: "未调用",
  }[raw] || raw.replace(/fallback/gi, "备用规则") || "未调用";
}

function rawStatusLabel(value = "") {
  return {
    fetched: "已抓取正文",
    summary_only: "检索线索",
    cached: "缓存样例",
    not_collected: "未采到正文",
  }[String(value).trim()] || String(value || "未标记").replace(/summary_only/gi, "检索线索");
}

function sourceTypeLabel(value = "") {
  return {
    official_site: "官网",
    pricing_page: "定价页",
    public_doc: "公开文档",
    review_page: "评价平台",
    news: "新闻/风险",
    official: "官方来源",
    official_pricing: "官方价格/报价",
    official_doc: "官方文档",
    review: "第三方评价",
    source_gap: "范围说明",
    search_result: "搜索结果",
    volc_search_result: "搜索结果",
    manual_scope: "任务范围说明",
    manual_input: "人工补充",
    manual_url: "人工补充网址",
    manual_confirmation: "人工确认",
    demo_scope_note: "缓存范围说明",
    questionnaire_design: "问卷设计",
    interview_guide: "访谈提纲",
  }[String(value).trim()] || String(value || "未分类");
}

function credibilityLabel(value = "") {
  return { high: "高", medium: "中", low: "低" }[String(value).trim()] || value || "未评估";
}

function initPlanWidget() {
  ["#planSteps", "#inlinePlanSteps"].forEach((selector) => {
    const list = $(selector);
    if (!list) return;
    list.replaceChildren(
      ...planSteps.map((step, index) =>
        el("li", { className: "plan-step", dataset: { step: index } }, [
          el("span", { className: "step-dot", text: "" }),
          el("span", { text: step }),
        ]),
      ),
    );
  });
}

function updatePlanProgress(activeIndex, rollbackText = "") {
  ["#planSteps", "#inlinePlanSteps"].forEach((selector) => {
    const list = $(selector);
    if (!list) return;
    const items = Array.from(list.children);
    items.forEach((item, index) => {
      const label = planSteps[index];
      item.replaceChildren(
        el("span", { className: "step-dot", text: index < activeIndex ? "✓" : "" }),
        el("span", { text: label }),
      );
      item.classList.toggle("done", index < activeIndex);
      item.classList.toggle("active", index === activeIndex);
    });
    if (rollbackText && items[3]) {
      items[3].replaceChildren(
        el("span", { className: "step-dot", text: "!" }),
        el("span", { text: rollbackText }),
      );
      items[3].classList.add("active");
    }
  });
  $("#inlinePlanSummary").textContent = rollbackText || planSteps[Math.min(activeIndex, planSteps.length - 1)] || "等待开始";
}

function animatePlan(hasRollback = true) {
  initPlanWidget();
  updatePlanProgress(hasRollback ? 1 : 0);
}

async function startTask(event) {
  event.preventDefault();
  const parsed = parseTaskPrompt($("#taskPromptInput").value);
  if (!parsed.competitors.length) {
    updatePromptFeedback();
    $("#taskPromptInput").focus();
    return;
  }

  $("#startButton").disabled = true;
  $("#startButton").textContent = "创建中";

  const payload = {
    industry: parsed.industry,
    competitors: parsed.competitors,
    websites: parsed.websites,
    focus_areas: getFocusAreas(),
    source_mode: getSourceMode(),
    notes: `用户原始输入：${parsed.raw}${state.surveyResearchSelected ? "；已选择问卷调研" : ""}`,
    defer_workflow: selectedUploadFiles().length > 0,
  };

  try {
    state.task = await api("/api/tasks", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (selectedUploadFiles().length) {
      await uploadSelectedFiles();
      state.task = await api(`/api/tasks/${state.task.id}/start`, { method: "POST" });
    }
    await loadTaskState({ allowMissingReport: true });
    renderBoard();
    if (state.report) {
      renderReport();
      show("#reportView");
    } else {
      show("#boardView");
    }
    revealTaskNav();
    beginTaskPolling();
    await loadHistory();
  } catch (error) {
    alert(error.message);
  } finally {
    $("#startButton").disabled = false;
    $("#startButton").textContent = "开始分析";
  }
}

async function uploadSelectedFiles() {
  const files = selectedUploadFiles();
  if (!files.length || !state.task) return;
  for (const file of files) {
    await uploadOneFile(file);
  }
}

async function uploadOneFile(file) {
  if (!file || !state.task) return;
  const formData = new FormData();
  formData.append("task_id", state.task.id);
  formData.append("file", file);
  const response = await fetch("/api/uploads", { method: "POST", body: formData });
  if (!response.ok) throw new Error("上传材料失败");
}

async function loadTaskState({ allowMissingReport = false } = {}) {
  if (!state.task) return;
  const taskId = state.task.id;
  const optionalRequest = (path, fallback) => api(path).catch((error) => {
    if (allowMissingReport) return fallback;
    throw error;
  });
  const reportRequest = optionalRequest(`/api/tasks/${taskId}/report`, null);
  const eventsRequest = optionalRequest(`/api/tasks/${taskId}/events`, []);
  const [task, dag, report, sources, claims, events] = await Promise.all([
    api(`/api/tasks/${taskId}`),
    api(`/api/tasks/${taskId}/dag`),
    reportRequest,
    api(`/api/tasks/${taskId}/sources`),
    api(`/api/tasks/${taskId}/claims`),
    eventsRequest,
  ]);
  state.task = task;
  state.dag = dag;
  state.report = report;
  state.sources = sources;
  state.claims = claims;
  state.events = events;
}

function isTerminalTask(task) {
  return task && ["completed", "failed", "stopped"].includes(task.status);
}

function isRunningTask(task) {
  return task && !isTerminalTask(task) && task.status !== "waiting_materials";
}

function stopTaskPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  state.pollBusy = false;
}

async function refreshCurrentTaskView() {
  if (!state.task || state.pollBusy) return;
  state.pollBusy = true;
  try {
    await loadTaskState({ allowMissingReport: true });
    renderBoard();
    if (state.report) {
      renderReport();
      if (state.task && state.task.status === "completed") show("#reportView");
    }
    revealTaskNav();
    renderHistory();
    if (isTerminalTask(state.task)) {
      stopTaskPolling();
      await loadHistory();
    }
  } catch (error) {
    console.warn("任务状态刷新失败", error);
  } finally {
    state.pollBusy = false;
  }
}

function beginTaskPolling() {
  stopTaskPolling();
  if (!state.task || isTerminalTask(state.task)) return;
  state.pollTimer = setInterval(refreshCurrentTaskView, 1200);
}

function revealTaskNav() {
  setHidden("#boardNavButton", !state.task);
  setHidden("#reportNavButton", !state.report);
  setHidden("#manualTopButton", !state.task);
  syncNavTabs();
}

function syncNavTabs() {
  $("#boardNavButton").classList.toggle("active", !$("#boardView").classList.contains("hidden"));
  $("#reportNavButton").classList.toggle("active", !$("#reportView").classList.contains("hidden"));
}

async function loadHistory() {
  const list = $("#historyList");
  try {
    state.history = await api(`/api/tasks?archived=${state.historyArchived ? 1 : 0}`);
    renderHistory();
  } catch (error) {
    list.replaceChildren(el("p", { className: "history-empty", text: "历史记录读取失败，请稍后刷新。" }));
  }
}

function renderHistory() {
  const list = $("#historyList");
  $("#historyTabButton").classList.toggle("active", !state.historyArchived);
  $("#archiveTabButton").classList.toggle("active", state.historyArchived);
  if (!state.history.length) {
    list.replaceChildren(el("p", { className: "history-empty", text: state.historyArchived ? "暂无归档任务。" : "暂无历史分析。" }));
    return;
  }
  list.replaceChildren(
    ...state.history.map((task) =>
      el("button", {
        className: `history-item ${state.task && state.task.id === task.id ? "active" : ""}`,
        type: "button",
        dataset: { taskId: task.id },
        onclick: () => openTaskFromHistory(task.id),
        oncontextmenu: (event) => openHistoryContextMenu(event, task.id),
      }, [
        el("strong", { text: task.name, title: task.name }),
        el("span", { text: `${statusLabel(task.status)} · ${formatDateTime(task.created_at)}` }),
        el("span", { text: (task.competitor_names || []).join("、") || "未记录竞品" }),
      ]),
    ),
  );
}

async function openTaskFromHistory(taskId) {
  stopTaskPolling();
  state.task = { id: taskId };
  await loadTaskState({ allowMissingReport: true });
  revealTaskNav();
  renderHistory();
  if (state.report) {
    renderReport();
    show("#reportView");
  } else {
    renderBoard();
    show("#boardView");
  }
  beginTaskPolling();
}

function openHistoryContextMenu(event, taskId) {
  event.preventDefault();
  event.stopPropagation();
  state.historyMenuTaskId = taskId;
  const menu = $("#historyContextMenu");
  const archiveButton = menu.querySelector('[data-action="archive"]');
  archiveButton.textContent = state.historyArchived ? "恢复该任务" : "归档该任务";
  menu.style.left = `${event.clientX}px`;
  menu.style.top = `${event.clientY}px`;
  setHidden("#contextMenu", true);
  setHidden("#historyContextMenu", false);
}

async function archiveHistoryTask(taskId) {
  if (!taskId) return;
  const archived = !state.historyArchived;
  await api(`/api/tasks/${taskId}/archive`, { method: "POST", body: JSON.stringify({ archived }) });
  if (archived && state.task && state.task.id === taskId) {
    startNewConversation();
  }
  await loadHistory();
}

function openDeleteDialog(taskId) {
  if (!taskId) return;
  const task = state.history.find((item) => item.id === taskId);
  const name = task ? task.name : "这个任务";
  state.pendingDeleteTaskId = taskId;
  $("#deleteMessage").textContent = `确定删除「${name}」吗？删除后会清除报告、日志、来源、证据和 Agent 事件，无法恢复。`;
  setHidden("#deleteBackdrop", false);
}

function closeDeleteDialog() {
  state.pendingDeleteTaskId = "";
  setHidden("#deleteBackdrop", true);
}

async function confirmDeleteTask() {
  const taskId = state.pendingDeleteTaskId;
  if (!taskId) return;
  await api(`/api/tasks/${taskId}`, { method: "DELETE" });
  if (state.task && state.task.id === taskId) {
    startNewConversation();
  }
  closeDeleteDialog();
  await loadHistory();
}

function renderNodeEvents(node) {
  const events = (node.events || []).slice(-5);
  if (!events.length) {
    return el("ul", { className: "node-events" }, [
      el("li", {}, [
        el("span", { text: node.status === "等待中" ? "等待上游 Agent 完成。" : "暂无细粒度事件。" }),
      ]),
    ]);
  }
  return el(
    "ul",
    { className: "node-events" },
    events.map((event) =>
      el("li", { className: event.severity || "info" }, [
        el("span", { text: event.message }),
        el("time", { text: formatDateTime(event.created_at) }),
      ]),
    ),
  );
}

function editCurrentTaskAsNew() {
  if (!state.task) return;
  stopTaskPolling();
  const rawInput = (state.task.notes || "").replace(/^用户原始输入：/, "").trim();
  const fallbackInput = `${state.task.industry || "待识别行业"}：${(state.task.competitor_names || []).join("、")}`;
  $("#taskPromptInput").value = rawInput || fallbackInput;
  state.onlineSearchEnabled = String(state.task.source_mode || "").includes("实时采集");
  state.uploadedFileName = "";
  state.uploadedFileNames = [];
  state.uploadedFiles = [];
  $("#uploadInput").value = "";
  const focusSet = new Set(state.task.focus_areas || []);
  const compatibleFocus = new Set(expandFocusAreas(state.task.focus_areas || []));
  document.querySelectorAll("#focusOptions input[type='checkbox']").forEach((checkbox) => {
    checkbox.checked = focusSet.size ? compatibleFocus.has(checkbox.value) : checkbox.checked;
  });
  updateSourceModeStatus();
  updatePromptFeedback();
  resizePromptInput();
  showCreateHome();
}

async function stopCurrentTask() {
  if (!isRunningTask(state.task)) return;
  const button = $("#stopTaskButton");
  await withButtonLoading(button, "停止中", async () => {
    const task = await api(`/api/tasks/${state.task.id}/stop`, { method: "POST", body: "{}" });
    state.task = task;
    await loadTaskState({ allowMissingReport: true });
    renderBoard();
    revealTaskNav();
    stopTaskPolling();
    await loadHistory();
    showToast("已停止任务；若刚好有一次模型调用在途，会在超时后结束。", "warning");
  });
}

function liveModelStatus(metrics = {}) {
  const providerText = [
    metrics.model_provider,
    metrics.analysis_provider,
    metrics.react_report_provider,
    metrics.provider_used,
  ].filter(Boolean).join("、");
  const deepMode = String(metrics.deep_report_execution_mode || "");
  const used = [];
  if (/doubao/i.test(providerText) || metrics.llm_called) used.push("豆包结构化");
  if (/deepseek_direct_thinking/i.test(deepMode)) used.push("DeepSeek direct");
  else if (/deepseek-react/i.test(providerText)) used.push("DeepSeek ReAct");
  if (/zhipu_direct_safe/i.test(deepMode)) used.push("智谱 direct");
  else if (/zhipu-react/i.test(providerText)) used.push("智谱深度分析");
  if (/stategraph_react_tools/i.test(deepMode)) used.push("StateGraph ReAct工具链");
  if (/doubao-react/i.test(providerText)) used.push("豆包 ReAct");
  if (/local-react-fallback|备用规则/i.test(providerText)) used.push("本地规则");
  if (!used.length && providerText) used.push(providerLabel(providerText));
  const stage = currentStageLabel();
  const providerSummary = used.length ? Array.from(new Set(used)).join(" + ") : "未调用";
  if (isRunningTask(state.task)) {
    return `当前：${stage} · ${providerSummary}`;
  }
  return state.task?.status === "stopped" ? `已停止：${providerSummary}` : `已完成：${providerSummary}`;
}

function liveSearchStatus(metrics = {}) {
  const eventTypes = new Set((state.events || []).map((event) => event.event_type));
  const resultCount = Number(metrics.search_result_count || 0);
  const sourceCount = Number(metrics.source_count || 0);
  const isRunning = isRunningTask(state.task);
  if (isRunning && (eventTypes.has("search_query") || eventTypes.has("volc_search_started"))) return "采集中";
  if (isRunning && String(state.task.source_mode || "").includes("实时采集")) return "准备搜索";
  if (resultCount > 0) return `采集检索 ${resultCount} 条`;
  if (metrics.search_called) return `ReAct检索已用 · 来源 ${sourceCount}`;
  if (eventTypes.has("volc_search_config")) return "已读取配置";
  if (state.task && String(state.task.source_mode || "").includes("上传资料")) return "上传材料";
  if (state.task && String(state.task.source_mode || "").includes("缓存")) return "缓存样例";
  return "未搜索";
}

function currentStageLabel() {
  const running = (state.dag?.nodes || []).find((node) => node.status === "运行中" || node.status === "running");
  if (running) return running.label.replace(/\s*Agent$/, "");
  return {
    created: "准备任务",
    collecting: "采集 Agent",
    analyzing: "分析 Agent",
    reanalyzing: "分析 Agent",
    qa_review: "质检 Agent",
    qa_rework: "人工复核",
    qa_failed: "需人工复核",
    qa_passed: "质检通过",
    reporting: "报告 Agent",
    completed: "已完成",
    failed: "失败",
    stopped: "已停止",
  }[state.task?.status] || "准备任务";
}

function formatFocusAreas(items = []) {
  const labels = (items || []).map((item) => focusAreaLabels[item] || item).filter(Boolean);
  return Array.from(new Set(labels)).join("、") || "默认报告维度";
}

function expandFocusAreas(items = []) {
  const aliases = {
    "功能对比": "核心能力",
    "定价": "商业模式与定价",
    "用户评价": "市场与赛道",
    "用户画像": "用户与场景",
    "SWOT": "SWOT与壁垒",
  };
  return (items || []).map((item) => aliases[item] || item);
}

function boardPhaseSummary(metrics = {}) {
  const sectionCount = (state.report?.content?.display_sections || []).length || Number(state.report?.content?.technical_section_count || 0);
  const qaOpen = Number(metrics.qa_open_count || 0);
  const qaFixed = Number(metrics.qa_fixed_count || 0);
  const qaText = qaOpen ? `质检待处理 ${qaOpen}` : qaFixed ? `质检修复 ${qaFixed}` : "质检通过";
  return `来源 ${metrics.source_count || 0} · ${sectionCount || 0}章 · ${qaText}`;
}

function renderBoard() {
  $("#taskTitle").textContent = state.task.name;
  $("#taskStatus").textContent = statusLabel(state.task.status);
  $("#taskStatus").className = `status-pill ${statusClass(state.task.status)}`;
  $("#taskElapsed").textContent = `总耗时 ${state.task.elapsed_label}`;
  setHidden("#stopTaskButton", !isRunningTask(state.task));
  renderInlinePlanFromDag();

  const canvas = $("#dagCanvas");
  const stages = el(
    "div",
    { className: "stage-list" },
    state.dag.nodes.map((node, index) =>
      el("article", { className: "stage-card", dataset: { status: node.status } }, [
        el("div", { className: "stage-index", text: String(index + 1).padStart(2, "0") }),
        el("div", { className: "stage-body" }, [
          el("header", {}, [
            el("h3", { text: node.label }),
            el("span", { className: `badge ${statusClass(node.status)}`, text: statusLabel(node.status) }),
          ]),
          el("p", { text: node.detail }),
          el("div", { className: "stage-meta" }, [
            el("span", { text: (node.status === "运行中" || node.status === "running") ? `正在运行 ${formatDuration(node.running_ms || 0)}` : `耗时 ${formatDuration(node.duration_ms)}` }),
            el("span", { text: (node.status === "运行中" || node.status === "running") ? "实时读取事件进度" : node.status === "重做中" ? "复核项已转入工作台" : "阶段结果已保存" }),
          ]),
          renderNodeEvents(node),
        ]),
      ]),
    ),
  );
  canvas.replaceChildren(stages);
  renderQaCard();
}

function renderInlinePlanFromDag() {
  initPlanWidget();
  const hasRollback = state.dag && state.dag.edges.some((edge) => edge.edge_type === "rollback");
  const activeStep = {
    created: 0,
    collecting: 1,
    analyzing: 2,
    reanalyzing: 2,
    qa_review: 3,
    qa_failed: 3,
    qa_rework: 2,
    qa_passed: 4,
    reporting: 4,
    completed: planSteps.length,
    stopped: planSteps.length,
  }[state.task && state.task.status] ?? 0;
  updatePlanProgress(activeStep, hasRollback ? "质检发现问题，已进入人工复核工作台" : "");
  if (state.task && state.task.status === "completed") {
    $("#inlinePlanSummary").textContent = "报告已生成";
  } else if (state.task && state.task.status === "stopped") {
    $("#inlinePlanSummary").textContent = "任务已停止";
  }
}

function buildQaSupplementPrompt(finding) {
  const guidance = finding.supplement_guidance || {};
  return guidance.fill_template || [
    `来源链接/材料名称：`,
    `材料类型：${sectionLabel(finding.claim_section) || "质检补证"}`,
    "来源日期或采集日期：",
    "这份材料能证明：",
    `对应竞品：${finding.affected_competitor || ""}`,
    "仍不确定或需要保守表述的地方：",
  ].join("\n");
}

function renderQaCard() {
  const card = $("#qaCard");
  const busy = isRunningTask(state.task);
  const children = [
    el("h2", { text: "人工复核工作台" }),
    el("p", {
      text: "首版报告会在自动质检后直接生成；这里集中处理可选的人工复核、补充来源、修订结论和重新质检。",
    }),
  ];
  if (busy) {
    children.push(el("div", { className: "qa-item qa-busy" }, [
      el("p", { text: "自动流程仍在运行中；系统会在自动质检后生成报告，人工复核入口暂时锁定。" }),
    ]));
  }
  if (!state.task.qa_findings.length) {
    children.push(el("div", { className: "qa-item" }, [el("p", { text: "暂无人工复核项；首版报告已通过自动质检。" })]));
  } else {
    state.task.qa_findings.forEach((finding) => {
      const recheckText = findingRecheckText(finding, finding.can_recheck_without_input);
      const objectLabel = [
        sectionLabel(finding.claim_section),
        finding.affected_competitor || "综合",
        finding.claim_id ? `结论 ${finding.claim_id.slice(0, 6)}` : "",
      ].filter(Boolean).join(" / ");
      const prompt = buildQaSupplementPrompt(finding);
      const sourceNodes = (finding.current_sources || []).length
        ? (finding.current_sources || []).slice(0, 4).map((source) =>
            el("li", {}, [
              el("strong", { text: `${source.ref || source.id} ` }),
              sourceUrlNode(source),
              el("span", { text: ` · ${source.title || "未命名来源"} · ${source.competitor || "未归属"} · ${sourceTypeLabel(source.role || source.raw_content_status || source.module)}` }),
            ]),
          )
        : [el("li", { text: "当前没有绑定可展示来源。" })];
      const queryNodes = (finding.suggested_queries || []).slice(0, 3).map((query) => el("span", { className: "qa-query", text: query }));
      children.push(
        el("div", { className: "qa-item" }, [
          el("div", { className: "qa-head" }, [
            el("span", { className: `badge ${findingStatusClass(finding)}`, text: findingStatusLabel(finding) }),
            el("span", { className: "badge", text: severityLabel(finding.severity) }),
            el("span", { className: "badge", text: findingTypeLabel(finding.finding_type) }),
          ]),
          el("strong", { text: objectLabel || "未定位问题对象" }),
          el("p", { className: "qa-reason", text: finding.reason }),
          el("div", { className: "qa-detail" }, [
            el("span", { text: "问题结论" }),
            el("p", { text: finding.claim_content || "未找到绑定结论。" }),
          ]),
          el("div", { className: "qa-detail" }, [
            el("span", { text: "当前来源" }),
            el("ul", {}, sourceNodes),
          ]),
          el("div", { className: "qa-detail" }, [
            el("span", { text: "需要补充" }),
            el("p", { text: finding.missing_material || finding.action_hint || "补充可追溯来源或人工确认。" }),
          ]),
          queryNodes.length ? el("div", { className: "qa-queries" }, queryNodes) : null,
          el("p", { text: `复检结果：${recheckText}` }),
          el("div", { className: "claim-actions" }, [
            el("button", {
              className: "mini-button",
              type: "button",
              disabled: busy,
              onclick: (event) => repairQaFinding(finding.id, finding.repair_action || "auto_collect", event.currentTarget),
              text: finding.repair_action === "manual_supplement" ? "补充材料/说明" : "自动补采并刷新报告",
            }),
            el("button", {
              className: "mini-button",
              type: "button",
              disabled: busy,
              onclick: () => openManualModal("", prompt, "qa_finding", { findingId: finding.id, claimId: finding.claim_id, finding }),
              text: "补充来源/材料",
            }),
            el("button", {
              className: "mini-button",
              type: "button",
              disabled: busy,
              onclick: (event) => repairQaFinding(finding.id, "confirm_uncertainty", event.currentTarget),
              text: "确认无误",
            }),
            el("button", {
              className: "mini-button",
              type: "button",
              disabled: busy,
              onclick: () => openManualModal(
                "",
                `我质疑这条结论，请打回并降级置信度：${finding.claim_content || finding.reason || ""}`,
                "qa_finding",
                { findingId: finding.id, claimId: finding.claim_id, finding, action: "dispute_claim" },
              ),
              text: "质疑/打回",
            }),
          ]),
        ]),
      );
    });
  }
  if (state.task.qa_findings.length) {
    children.push(
      el("button", {
        className: "ghost",
        type: "button",
        disabled: busy,
        onclick: (event) => recheckQa(event.currentTarget),
        text: "我已处理完，重新质检",
      }),
    );
  }
  card.replaceChildren(...children);
}

function severityLabel(value = "") {
  return {
    low: "低风险",
    medium: "中风险",
    high: "高风险",
    critical: "严重",
    info: "普通",
    warning: "提醒",
    error: "错误",
  }[String(value)] || "中风险";
}

function findingTypeLabel(value = "") {
  return {
    source_ownership_mismatch: "来源归属不匹配",
    pricing_missing_official: "缺官方价格",
    missing_date: "缺时间口径",
    collection_log_content: "日志口吻",
    duplicate_claim: "重复结论",
    scope_only: "只有范围说明",
    model_review: "模型质检意见",
    missing_source: "缺少来源",
    low_confidence_missing_uncertainty: "低置信缺说明",
    needs_review_missing_uncertainty: "待复核缺说明",
    manual_evidence_needs_validation: "人工材料待核验",
    manual_dispute: "人工质疑",
    general: "通用问题",
  }[String(value)] || "通用问题";
}

function sectionLabel(value = "") {
  return {
    pricing_model: "定价对比",
    feature_tree: "功能对比",
    reviews: "用户评价",
    user_persona: "用户画像",
    swot: "SWOT",
    overview: "概览",
  }[value] || value || "未分组";
}

async function renderLogs() {
  if (!state.task) return;
  const path = buildLogPath("logs");
  const logs = await api(path);
  $("#logList").replaceChildren(
    ...logs.map((log) =>
      el("article", { className: "log-entry" }, [
        el("strong", {}, [
          el("span", { text: log.agent_name }),
          el("span", { className: `badge ${statusClass(log.status)}`, text: statusLabel(log.status) }),
        ]),
        el("p", { text: `输入：${log.input_summary}` }),
        el("p", { text: `输出：${log.output_summary}` }),
        el("p", { text: `时间：${formatDateTime(log.started_at)} - ${formatDateTime(log.ended_at)}` }),
        el("p", { text: `耗时：${formatDuration(log.duration_ms)} / 重试：${log.retry_count} / 令牌：${log.token_input}+${log.token_output}` }),
        el("p", { text: `级别：${severityLabel(log.severity)} / 模型：${providerLabel(log.model_provider)} / 打回：${log.has_rework ? "是" : "否"}` }),
        log.fallback_reason ? el("p", { text: `备用规则说明：${String(log.fallback_reason).replace(/fallback/gi, "备用规则")}` }) : null,
        el("p", { text: log.error ? `错误：${log.error}` : "错误：无" }),
      ]),
    ),
  );
}

function buildLogPath(kind = "logs") {
  const params = new URLSearchParams();
  const agent = $("#logFilter").value;
  const status = $("#logStatusFilter").value;
  const severity = $("#logSeverityFilter").value;
  const hasRework = $("#logReworkFilter").value;
  if (agent) params.set("agent", agent);
  if (status) params.set("status", status);
  if (severity) params.set("severity", severity);
  if (hasRework) params.set("has_rework", hasRework);
  const query = params.toString();
  return `/api/tasks/${state.task.id}/${kind}${query ? `?${query}` : ""}`;
}

function downloadLogs() {
  if (!state.task) return;
  window.location.href = buildLogPath("logs/download");
}

function downloadPdf() {
  if (!state.task || !state.report) return;
  window.location.href = `/api/tasks/${state.task.id}/report/pdf`;
}

function renderReport() {
  if (!state.report) return;
  const content = state.report.content;
  $("#reportTitle").textContent = content.title;
  $("#reportGenerated").textContent = `生成时间 ${formatDateTime(state.report.generated_at)}`;
  $("#reportConfidence").textContent = `可信度 ${Math.round(state.report.confidence_score * 100)}%`;
  $("#reportSources").textContent = `来源 ${content.metrics.source_count}`;
  $("#reportQa").textContent = "正式报告";
  $("#reportMetrics").replaceChildren(
    renderMetric("来源数量", content.metrics.source_count),
    renderMetric("证据分片", content.metrics.evidence_chunk_count || 0),
    renderMetric("关键结论", content.metrics.claim_count),
    renderMetric("引用覆盖率", `${Math.round(content.metrics.citation_coverage * 100)}%`),
    renderMetric("待确认", content.metrics.manual_review_count),
    renderMetric("开放质检", content.metrics.qa_open_count || 0),
    renderMetric("复核修复", content.metrics.qa_rework_count || 0),
    renderMetric("结构化完成", `${Math.round((content.metrics.structured_field_completion || 0) * 100)}%`),
    renderMetric("采集提供方", providerLabel(content.metrics.collection_provider || "未调用")),
    renderMetric("搜索结果", content.metrics.search_result_count || 0),
  );

  const sections = content.display_sections || content.sections || [];
  $("#reportBoards").replaceChildren();
  renderFullReport({ ...content, sections });
  setHidden("#reportBoards", true);
  setHidden("#reportMetrics", true);
  setHidden("#fullReport", false);
  setHidden("#toggleReportModeButton", true);
}

function renderReportCard(section) {
  const children = [
    el("header", {}, [
      el("div", {}, [
        el("span", { className: "section-key", text: section.key || "section" }),
        el("h3", { text: section.title }),
      ]),
      el("button", {
        className: "mini-button",
        type: "button",
        onclick: () => openSectionModal(section),
        text: "放大",
      }),
    ]),
    el("p", { text: section.body }),
  ];
  if (section.table) children.push(renderTable(section.table));
  children.push(renderClaims(section.claims || []));
  const wideKeys = ["overview", "swot"];
  return el("article", { className: `report-card ${wideKeys.includes(section.key) ? "wide" : ""}` }, children);
}

function renderMetric(label, value) {
  return el("div", { className: "metric-card" }, [
    el("span", { text: label }),
    el("strong", { text: String(value) }),
  ]);
}

function renderFullReport(content) {
  const reportItems = buildRenderedReportItems(content);
  const children = [
    renderReportToc(reportItems),
    ...reportItems.map((item, index) => {
      if (item.type === "visual") return renderVisualSection(content, index);
      if (item.type === "sources") return renderSourceCatalog(content.source_catalog || [], index);
      return renderFullReportSection(item.section, index);
    }),
  ];
  $("#fullReport").replaceChildren(...children);
}

function buildRenderedReportItems(content) {
  const sections = (content.sections || []).filter((section) => !isConclusionSection(section));
  const items = [{ type: "visual", title: "可视化总览" }, ...sections.map((section) => ({ type: "section", section }))];
  if ((content.source_catalog || []).length) items.push({ type: "sources", title: "参考文献（来源链接）" });
  return items;
}

function isConclusionSection(section) {
  const key = String((section && (section.key || section.id)) || "").toLowerCase();
  const title = stripSectionOrdinal(section && section.title ? section.title : "");
  return key.includes("conclusion") || /^结语(?:\s|$)/.test(title);
}

function sectionAnchorId(index) {
  return `report-section-${index + 1}`;
}

const SECTION_NUMERALS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二", "十三", "十四", "十五", "十六"];

function sectionOrdinal(index) {
  return SECTION_NUMERALS[index] || String(index + 1);
}

function sectionOrdinalFromNumber(value) {
  const numeric = Number.parseInt(value, 10);
  if (!Number.isFinite(numeric) || numeric < 1) return "";
  return SECTION_NUMERALS[numeric - 1] || String(numeric);
}

function sectionOrdinalFromTitle(title) {
  const match = String(title || "").match(/^\s*(?:第)?([一二三四五六七八九十]{1,3}|[0-9]{1,2})[、.．，,\s]+/);
  if (!match) return "";
  return /^\d+$/.test(match[1]) ? sectionOrdinalFromNumber(match[1]) : match[1];
}

function sectionOrdinalFromMarkdown(markdown) {
  const match = String(markdown || "").match(/(?:^|\n)#{3,5}\s+(\d{1,2})\.\d{1,2}\b/);
  return match ? sectionOrdinalFromNumber(match[1]) : "";
}

function reportSectionOrdinal(item, index) {
  return sectionOrdinal(index);
}

function syncMarkdownSectionNumbers(markdown, renderedIndex) {
  const target = String(renderedIndex + 1);
  return String(markdown || "").replace(
    /(^|\n)(#{3,5}\s+)(\d{1,2})((?:\.\d{1,2})+\b)/g,
    (_match, prefix, hashes, _section, rest) => `${prefix}${hashes}${target}${rest}`,
  );
}

function stripSectionOrdinal(title) {
  return String(title || "")
    .replace(/^\s*(?:第?[一二三四五六七八九十]{1,3}|[0-9]{1,2})[、.．，,\s]+/, "")
    .trim();
}

function reportSectionTitle(item, index, includeOrdinal = true) {
  const rawTitle = item && item.type === "visual"
    ? item.title || "可视化总览"
    : item && item.type === "sources"
      ? item.title || "参考文献（来源链接）"
    : item && item.section
      ? item.section.title
      : item && item.title
        ? item.title
        : `章节 ${index + 1}`;
  const title = stripSectionOrdinal(rawTitle);
  return includeOrdinal ? `${reportSectionOrdinal(item, index)}、${title}` : title;
}

function renderReportToc(items) {
  return el("nav", { className: "report-toc", "aria-label": "报告目录" }, [
    el("h2", { className: "report-toc-title", text: "目录" }),
    el("div", { className: "report-toc-grid" }, items.map((item, index) =>
      el("a", { href: `#${sectionAnchorId(index)}`, text: reportSectionTitle(item, index, true) }),
    )),
  ]);
}

function renderExecutiveCards(cards) {
  if (!cards.length) return el("section", { className: "report-empty-block" });
  return el("section", { className: "executive-cards" }, cards.map((card) =>
    el("article", { className: `executive-card ${card.type || ""}` }, [
      el("span", { text: card.title || "核心结论" }),
      el("strong", { text: card.status || "核心判断" }),
      el("p", { text: card.verdict || "当前来源不足，未进入核心结论。" }),
      el("small", {}, [
        el("span", { text: `置信度 ${Math.round((card.confidence || 0) * 100)}% · 来源 ` }),
        renderEvidenceRefs(card.evidence_refs || []),
      ]),
    ]),
  ));
}

function renderMethodology(methodology, reliability) {
  const rows = [["来源类型", "数量", "正文抓取", "检索线索", "高可信"]];
  reliability.forEach((item) => rows.push([
    item.category || "来源",
    item.count || 0,
    item.fetched || 0,
    item.summary_only || 0,
    item.high || 0,
  ]));
  return el("section", { className: "report-band" }, [
    el("div", { className: "full-section-heading" }, [
      el("span", { text: "M" }),
      el("h2", { text: "方法与来源可靠性" }),
    ]),
    el("p", { text: methodology.scope || "本报告仅基于已入库来源生成。" }),
    el("p", { text: methodology.source_policy || "优先使用官方与可信第三方来源。" }),
    reliability.length ? renderTable(rows) : el("p", { className: "panel-empty", text: "暂无来源可靠性统计。" }),
  ]);
}

function renderVisualSection(content, index = 0) {
  return el("section", { className: "report-visuals full-report-section", id: sectionAnchorId(index) }, [
    el("div", { className: "full-section-heading" }, [
      el("span", { text: sectionOrdinal(index) }),
      el("h2", { text: "可视化总览" }),
    ]),
    el("div", { className: "visual-grid" }, [
      renderScoreHeatmap(content.score_dimensions || content.feature_scores || []),
      renderApiCostData(content.api_cost_data || {}),
      renderRadar(content.chart_data && content.chart_data.radar ? content.chart_data.radar : []),
      renderAppMarketData(content.app_market_data || (content.chart_data && content.chart_data.app_market) || {}),
    ]),
  ]);
}

function renderScoreHeatmap(rows) {
  if (!rows.length) return el("article", { className: "visual-panel" }, [
    el("h3", { text: "评分热力图" }),
    el("p", { className: "panel-empty", text: "暂无评分数据。" }),
  ]);
  const competitors = Array.from(new Set(rows.map((row) => row.competitor)));
  const dimensions = Array.from(new Set(rows.map((row) => row.dimension)));
  const lookup = new Map(rows.map((row) => [`${row.competitor}::${row.dimension}`, row]));
  const gridStyle = `grid-template-columns:104px repeat(${Math.max(1, dimensions.length)}, minmax(86px, 1fr));`;
  return el("article", { className: "visual-panel wide-panel" }, [
    el("h3", { text: `${dimensions.length || 0}维评分热力图` }),
    el("p", { className: "chart-note", text: "评分为分析判断，不是官方指标；每项必须有评分口径和证据引用。" }),
    el("div", { className: "score-heatmap" }, [
      el("div", { className: "score-row score-head", style: gridStyle }, [
        el("span", { text: "竞品" }),
        ...dimensions.map((dimension) => el("span", { text: dimension })),
      ]),
      ...competitors.map((competitor) =>
        el("div", { className: "score-row", style: gridStyle }, [
          el("strong", { text: competitor }),
          ...dimensions.map((dimension) => {
            const item = lookup.get(`${competitor}::${dimension}`) || { score: 0, max_score: 5, status: "未评分" };
            const value = Number(item.score || 0);
            const level = Math.max(0, Math.min(5, Math.round(value)));
            return el("span", {
              className: `heat-cell level-${level}`,
              title: `${item.status || ""}｜${item.rationale || ""} ${formatRefs(item.evidence_refs || item.section_refs)}`,
              text: value ? `${value}/${item.max_score || 5}` : "NA",
            });
          }),
        ]),
      ),
    ]),
  ]);
}

function renderFeatureHeatmap(rows) {
  const competitors = Array.from(new Set(rows.map((row) => row.competitor)));
  const dimensions = Array.from(new Set(rows.map((row) => row.dimension)));
  const lookup = new Map(rows.map((row) => [`${row.competitor}::${row.dimension}`, row]));
  return el("article", { className: "visual-panel" }, [
    el("h3", { text: "功能热力图" }),
    el("div", { className: "heatmap" }, [
      el("div", { className: "heatmap-row heatmap-head" }, [
        el("span", { text: "竞品" }),
        ...dimensions.map((dimension) => el("span", { text: dimension })),
      ]),
      ...competitors.map((competitor) =>
        el("div", { className: "heatmap-row" }, [
          el("strong", { text: competitor }),
          ...dimensions.map((dimension) => {
            const item = lookup.get(`${competitor}::${dimension}`) || { score: 0, max_score: 5 };
            const level = Math.max(0, Math.min(5, Number(item.score || 0)));
            return el("span", { className: `heat-cell level-${level}`, title: formatRefs(item.evidence_refs), text: `${level}/${item.max_score || 5}` });
          }),
        ]),
      ),
    ]),
  ]);
}

function renderPositioningMap(mapData) {
  const points = mapData.points || [];
  return el("article", { className: "visual-panel" }, [
    el("h3", { text: "竞争定位图" }),
    el("p", { className: "chart-note", text: mapData.interpretation || "暂无定位解释。" }),
    el("div", { className: "positioning-plane" }, [
      el("span", { className: "axis-label y-axis", text: mapData.y_axis || "应用层/治理成熟度" }),
      el("span", { className: "axis-label x-axis", text: mapData.x_axis || "成本/开放价值" }),
      ...points.map((point, index) =>
        el("button", {
          className: `position-point point-${index}`,
          type: "button",
          style: `left:${Math.max(4, Math.min(92, (Number(point.x || 0) / 5) * 88 + 4))}%;top:${Math.max(6, Math.min(86, 92 - (Number(point.y || 0) / 5) * 82))}%;`,
          title: point.label || "",
          text: point.competitor || "竞品",
        }),
      ),
    ]),
  ]);
}

function renderApiCostData(data) {
  const rows = data.rows || [];
  const maxValue = Math.max(...rows.map((row) => Number(row.cost_index || 0)), 100, 1);
  return el("article", { className: "visual-panel" }, [
    el("h3", { text: data.title || (data.enabled === false ? "行业价格口径" : "API成本指数") }),
    el("p", { className: "chart-note", text: data.formula || data.caveat || "按官方输出价归一化计算。" }),
    rows.length ? el("div", { className: "bar-list" }, rows.map((row) =>
      el("div", { className: "bar-row" }, [
        el("span", { text: row.competitor }),
        el("div", { className: "bar-track" }, [el("i", { style: `width:${Math.max(8, (Number(row.cost_index || 0) / maxValue) * 100)}%` })]),
        el("strong", { text: `${row.cost_index} · ${row.output_amount} ${row.currency}/${row.unit}` }),
        el("small", {}, [
          el("span", { text: row.note ? `${row.note} · 来源 ` : "来源 " }),
          renderEvidenceRefs(row.evidence_refs || []),
        ]),
      ]),
    )) : el("p", { className: "panel-empty", text: data.caveat || "未抽取到可计算的官方输出价。" }),
    el("small", { className: "chart-note", text: data.caveat || "" }),
  ]);
}

function rankDisplay(value) {
  return value === null || value === undefined || value === "" ? "-" : String(value);
}

function renderAppMarketBar(title, rows, valueKey, textKey) {
  const maxValue = Math.max(...rows.map((row) => Number(row[valueKey] || 0)), 1);
  return el("div", { className: "app-market-subchart" }, [
    el("h4", { text: title }),
    el("div", { className: "bar-list compact" }, rows.map((row) =>
      el("div", { className: "bar-row app-market-bar" }, [
        el("span", { text: row.competitor || row.app_name }),
        el("div", { className: "bar-track" }, [el("i", { style: `width:${Math.max(8, (Number(row[valueKey] || 0) / maxValue) * 100)}%` })]),
        el("strong", { text: row[textKey] || "NA" }),
      ]),
    )),
  ]);
}

function renderAppMarketData(data) {
  const rows = data.rows || [];
  return el("article", { className: "visual-panel wide-panel app-market-panel" }, [
    el("h3", { text: data.title || "App 市场表现" }),
    el("p", { className: "chart-note", text: rows.length ? "数据来自 AppArk 竞品对比页；排名数字越小表示榜单位置越靠前。" : (data.caveat || "暂无 AppArk 市场表现数据。") }),
    rows.length ? el("div", { className: "app-market-grid" }, [
      renderAppMarketBar("下载量", rows, "downloads_value", "downloads_text"),
      renderAppMarketBar("收入额", rows, "revenue_usd", "revenue_text"),
      el("div", { className: "app-market-rank" }, [
        el("h4", { text: "榜单排名" }),
        renderTable([
          ["应用", "免费榜", "付费榜", "总榜"],
          ...rows.map((row) => [
            row.competitor || row.app_name,
            rankDisplay(row.free_rank),
            rankDisplay(row.paid_rank),
            rankDisplay(row.overall_rank),
          ]),
        ]),
      ]),
    ]) : el("p", { className: "panel-empty", text: data.caveat || "暂无 AppArk 数据。" }),
    rows.length ? el("small", { className: "chart-note", text: `来源：${data.source || "AppArk"}${data.collected_at ? ` · ${formatDateTime(data.collected_at)}` : ""}` }) : null,
  ]);
}

function renderRadar(rows) {
  const dimensions = Array.from(new Set(rows.flatMap((row) => Object.keys(row.scores || {}))));
  if (!rows.length || !dimensions.length) {
    return el("article", { className: "visual-panel" }, [
      el("h3", { text: "能力雷达图" }),
      el("p", { className: "panel-empty", text: "暂无可渲染的评分维度，完成分析后会从评分表自动生成雷达图。" }),
    ]);
  }
  const size = 220;
  const center = size / 2;
  const radius = 78;
  const palette = ["#d9b860", "#72d6a0", "#8fb5ff", "#d16464"];
  const maxScore = Math.max(...rows.flatMap((row) => dimensions.map((dimension) => Number((row.scores || {})[dimension] || 0))), 0);
  const axes = dimensions.map((dimension, index) => {
    const angle = (-Math.PI / 2) + (index * Math.PI * 2) / Math.max(dimensions.length, 1);
    return { dimension, x: center + Math.cos(angle) * radius, y: center + Math.sin(angle) * radius, angle };
  });
  const polygons = rows.map((row, rowIndex) => {
    const points = axes.map((axis) => {
      const value = Math.max(0, Math.min(5, Number((row.scores || {})[axis.dimension] || 0)));
      const r = (value / 5) * radius;
      return `${center + Math.cos(axis.angle) * r},${center + Math.sin(axis.angle) * r}`;
    }).join(" ");
    return `<polygon points="${points}" fill="${palette[rowIndex % palette.length]}22" stroke="${palette[rowIndex % palette.length]}" stroke-width="2"></polygon>`;
  }).join("");
  const axisLines = axes.map((axis) => `<line x1="${center}" y1="${center}" x2="${axis.x}" y2="${axis.y}" stroke="rgba(242, 215, 138, 0.45)"></line><text x="${axis.x}" y="${axis.y}" fill="#f6edd8" font-size="9" font-weight="700" text-anchor="middle">${escapeHtml(axis.dimension)}</text>`).join("");
  const svg = svgEl("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: "img", "aria-label": "能力雷达图" });
  svg.innerHTML = `<circle cx="${center}" cy="${center}" r="${radius}" fill="rgba(255, 248, 225, 0.035)" stroke="rgba(242, 215, 138, 0.45)"></circle>${axisLines}${polygons}`;
  return el("article", { className: "visual-panel" }, [
    el("h3", { text: "能力雷达" }),
    maxScore <= 0 ? el("p", { className: "panel-empty", text: "当前评分均为 NA 或 0，雷达图仅展示维度框架；需要更多来源或章节依据后才会形成有效轮廓。" }) : null,
    el("div", { className: "radar-wrap" }, [svg]),
    el("div", { className: "chart-legend" }, rows.map((row, index) => el("span", { text: row.competitor, style: `--legend:${palette[index % palette.length]}` }))),
  ]);
}

function renderPricingBars(rows, dimensionProfile = {}) {
  const values = rows.map((row) => Number(row.cost_index || row.output_amount || 0)).filter((value) => value > 0);
  const maxValue = Math.max(...values, 1);
  const title = dimensionProfile.price_metric_label || "价格口径";
  return el("article", { className: "visual-panel" }, [
    el("h3", { text: title }),
    dimensionProfile.price_metric_description ? el("p", { className: "chart-note", text: dimensionProfile.price_metric_description }) : null,
    el("div", { className: "bar-list" }, rows.map((row, index) =>
      el("div", { className: "bar-row" }, [
        el("span", { text: row.competitor }),
        el("div", { className: "bar-track" }, [el("i", { style: `width:${Math.max(8, ((Number(row.cost_index || row.output_amount || 0) || 0) / maxValue) * 100)}%` })]),
        el("strong", { text: row.price_text || "未抽取金额" }),
        el("small", {}, [
          el("span", { text: row.calculation_note ? `${row.calculation_note} · 来源 ` : "来源 " }),
          renderEvidenceRefs(row.evidence_refs || []),
        ]),
      ]),
    )),
  ]);
}

function renderReviewSummary(rows) {
  return el("article", { className: "visual-panel" }, [
    el("h3", { text: "用户评价与口碑解读" }),
    ...rows.map((row) => el("blockquote", { className: "mini-quote" }, [
      el("strong", { text: row.competitor }),
      el("p", { text: row.summary }),
      el("small", {}, [
        el("span", { text: `${row.platform_count || 0} 个平台 · ${row.bias_note || ""} · 来源 ` }),
        renderEvidenceRefs(row.evidence_refs || []),
      ]),
    ])),
  ]);
}

function renderSwotBoard(swot) {
  const names = Object.keys(swot);
  if (!names.length) return el("section", { className: "report-empty-block" });
  return el("section", { className: "swot-board" }, [
    el("div", { className: "full-section-heading" }, [
      el("span", { text: "S" }),
      el("h2", { text: "竞品独立 SWOT" }),
    ]),
    el("div", { className: "swot-grid" }, names.map((name) => {
      const item = swot[name] || {};
      return el("article", { className: "swot-card" }, [
        el("h3", { text: name }),
        ...["优势", "劣势", "机会", "威胁"].map((label) => el("div", { className: `swot-line swot-${label}` }, [
          el("strong", { text: label }),
          el("p", { text: item[label] || "未形成判断" }),
        ])),
      ]);
    })),
  ]);
}

function renderDecisionMatrix(rows) {
  if (!rows.length) return el("section", { className: "report-empty-block" });
  return el("section", { className: "report-band" }, [
    el("div", { className: "full-section-heading" }, [
      el("span", { text: "D" }),
      el("h2", { text: "场景化决策矩阵" }),
    ]),
    renderTable([["场景", "竞品", "优先级", "理由", "下一步"], ...rows.map((row) => [
      row.scenario,
      row.competitor,
      row.priority,
      row.reason,
      row.next_action,
    ])]),
  ]);
}

function renderScenarioRecommendations(rows) {
  if (!rows.length) return el("section", { className: "report-empty-block" });
  return el("section", { className: "report-band" }, [
    el("div", { className: "full-section-heading" }, [
      el("span", { text: "P" }),
      el("h2", { text: "场景化采购/产品建议" }),
    ]),
    renderTable([["场景", "推荐对象", "置信度", "理由", "下一步"], ...rows.map((row) => [
      row.scenario,
      row.recommended,
      row.confidence,
      `${row.reason} ${formatRefs(row.evidence_refs)}`,
      row.next_action,
    ])]),
  ]);
}

function renderKeyInsights(rows) {
  if (!rows.length) return el("section", { className: "report-empty-block" });
  return el("section", { className: "report-band" }, [
    el("div", { className: "full-section-heading" }, [
      el("span", { text: "I" }),
      el("h2", { text: "关键洞察" }),
    ]),
    el("div", { className: "insight-grid" }, rows.map((row, index) =>
      el("article", { className: "insight-card" }, [
        el("span", { text: String(index + 1).padStart(2, "0") }),
        el("h3", { text: row.title }),
        el("p", { text: row.insight }),
        el("small", {}, [renderEvidenceRefs(row.evidence_refs || [])]),
      ]),
    )),
  ]);
}

function renderFactNotes(rows) {
  if (!rows.length) return el("section", { className: "report-empty-block" });
  return el("section", { className: "report-band" }, [
    el("div", { className: "full-section-heading" }, [
      el("span", { text: "N" }),
      el("h2", { text: "事实备注与口径" }),
    ]),
    el("div", { className: "fact-note-list" }, rows.map((row) =>
      el("div", { className: "fact-note" }, [
        el("strong", { text: row.topic }),
        el("p", { text: row.note }),
        el("small", {}, [renderEvidenceRefs(row.evidence_refs || [])]),
      ]),
    )),
  ]);
}

function renderRiskControls(rows) {
  if (!rows.length) return el("section", { className: "report-empty-block" });
  return el("section", { className: "report-band" }, [
    el("div", { className: "full-section-heading" }, [
      el("span", { text: "R" }),
      el("h2", { text: "风险控制" }),
    ]),
    renderTable([["风险", "影响", "控制动作", "负责人"], ...rows.map((row) => [
      row.risk,
      row.impact,
      row.control,
      row.owner,
    ])]),
  ]);
}

function renderSourceCatalog(rows, index = 0) {
  if (!rows.length) return el("section", { className: "report-empty-block" });
  const entries = rows.filter((row) => row && (row.ref || row.title || row.url_or_path)).slice(0, 120);
  const list = el("ol", { className: "reference-list" }, entries.map((row) => {
    const url = row.url_or_path || "";
    const hasUrl = /^https?:\/\//i.test(url);
    const label = `[${sourceCitationNumber(row, url)}]`;
    const title = row.title || url || row.ref || "未命名来源";
    const meta = [row.site, row.competitor || "综合", sourceTypeLabel(row.type), row.published_at || row.collected_at]
      .filter(Boolean)
      .join(" · ");
    const numberNode = hasUrl
      ? el("a", { className: "reference-source-number", href: url, target: "_blank", rel: "noopener noreferrer", text: label, title: url })
      : el("span", { className: "reference-source-number", text: label });
    const titleNode = hasUrl
      ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: title, title: url })
      : el("button", { className: "source-ref-button", type: "button", onclick: () => openSourcesPanel([row.id || row.ref], row.ref || title), text: title });
    return el("li", {}, [
      numberNode,
      el("span", { className: "reference-source-entry" }, [
        titleNode,
        meta ? el("small", { text: meta }) : null,
      ]),
    ]);
  }));
  return el("section", { className: "source-catalog full-report-section", id: sectionAnchorId(index) }, [
    el("div", { className: "full-section-heading" }, [
      el("span", { text: sectionOrdinal(index) }),
      el("h2", { text: "参考文献（来源链接）" }),
    ]),
    list,
  ]);
}

function formatRefs(refs = []) {
  return refs && refs.length ? refs.map((ref) => sourceRefLabel(ref)).join(" ") : "未列入正文依据";
}

function sourceRecordForRef(ref) {
  const key = cleanSourceRefToken(ref);
  const catalog = (state.report && state.report.content && state.report.content.source_catalog) || [];
  const numeric = citationNumberFromRef(key);
  if (numeric) {
    const byNumber = sourceRecordForCitationNumber(numeric);
    if (byNumber) return byNumber;
  }
  const byCatalog = catalog.find((source) => source.ref === key || source.id === key);
  if (byCatalog) return byCatalog;
  const source = (state.sources || []).find((item) => item.id === key);
  if (!source) return null;
  const index = (state.sources || []).findIndex((item) => item.id === key);
  return { id: source.id, ref: `S${index + 1}`, title: source.title, url_or_path: source.url_or_path };
}

function catalogSourceRecordForRef(ref) {
  const key = cleanSourceRefToken(ref);
  const catalog = (state.report && state.report.content && state.report.content.source_catalog) || [];
  const numeric = citationNumberFromRef(key);
  if (numeric) {
    return catalog.find((source) => sourceCitationNumber(source, source.url_or_path || "") === numeric) || null;
  }
  return catalog.find((source) => source.ref === key || source.id === key) || null;
}

function normalizeReferenceUrl(value) {
  const raw = String(value || "").trim().replace(/[.,;，。；、]+$/g, "");
  if (!raw) return "";
  try {
    const parsed = new URL(raw);
    parsed.hash = "";
    let normalized = `${parsed.protocol}//${parsed.host}${parsed.pathname}`.replace(/\/+$/g, "");
    if (parsed.search) normalized += parsed.search;
    return normalized.toLowerCase();
  } catch (error) {
    return raw.replace(/\/+$/g, "").toLowerCase();
  }
}

function sourceRecordForUrl(url) {
  const normalized = normalizeReferenceUrl(url);
  if (!normalized) return null;
  const catalog = (state.report && state.report.content && state.report.content.source_catalog) || [];
  const localSources = (state.sources || []).map((source, index) => ({
    id: source.id,
    ref: `S${index + 1}`,
    title: source.title,
    url_or_path: source.url_or_path,
  }));
  return [...catalog, ...localSources].find((source) => normalizeReferenceUrl(source.url_or_path) === normalized) || null;
}

function maxCatalogCitationNumber() {
  const catalog = (state.report && state.report.content && state.report.content.source_catalog) || [];
  return catalog.reduce((max, source) => {
    const match = String(source.ref || "").match(/^S(\d+)$/i);
    return match ? Math.max(max, Number(match[1])) : max;
  }, 0);
}

function externalCitationNumberForUrl(url) {
  const normalized = normalizeReferenceUrl(url);
  if (!state.externalCitationNumbers || state.externalCitationTaskId !== (state.task && state.task.id)) {
    state.externalCitationNumbers = new Map();
    state.externalCitationTaskId = state.task && state.task.id;
  }
  if (!state.externalCitationNumbers.has(normalized)) {
    state.externalCitationNumbers.set(normalized, String(maxCatalogCitationNumber() + state.externalCitationNumbers.size + 1));
  }
  return state.externalCitationNumbers.get(normalized);
}

function sourceCitationNumber(source, url = "") {
  const ref = String((source && source.ref) || "");
  const match = ref.match(/^S(\d+)$/i);
  return match ? match[1] : ref || externalCitationNumberForUrl(url);
}

const CITATION_MATCH_GROUPS = [
  { aliases: ["chatgpt", "openai", "gpt"], sourceTerms: ["chatgpt", "openai", "gpt"] },
  { aliases: ["deepseek", "深度求索"], sourceTerms: ["deepseek", "深度求索"] },
  { aliases: ["豆包", "doubao", "字节", "bytedance", "volcengine", "火山"], sourceTerms: ["豆包", "doubao", "字节", "bytedance", "volcengine", "火山"] },
];

const CITATION_TOPIC_GROUPS = [
  { textTerms: ["价格", "定价", "订阅", "付费", "免费", "成本", "api", "token", "套餐"], sourceTerms: ["pricing", "price", "api", "billing", "套餐", "价格", "定价"] },
  { textTerms: ["企业", "团队", "合规", "安全", "隐私", "数据", "管理员"], sourceTerms: ["enterprise", "business", "security", "compliance", "privacy", "安全", "合规"] },
  { textTerms: ["app", "下载", "收入", "榜单", "应用商店", "移动端"], sourceTerms: ["app", "app store", "google play", "appark", "下载", "收入"] },
  { textTerms: ["模型", "推理", "agent", "多模态", "代码", "codex", "能力", "文档"], sourceTerms: ["docs", "documentation", "model", "agent", "codex", "文档"] },
  { textTerms: ["用户", "评价", "口碑", "社区", "评论", "g2", "reddit"], sourceTerms: ["review", "g2", "reddit", "评价", "评论"] },
];

function includesAnyText(haystack, terms = []) {
  const text = String(haystack || "").toLowerCase();
  return terms.some((term) => text.includes(String(term).toLowerCase()));
}

function sourceSearchText(source) {
  return [
    source.ref,
    source.title,
    source.url_or_path,
    source.type,
    source.site,
    source.competitor,
    source.module,
    source.role,
  ].filter(Boolean).join(" ").toLowerCase();
}

function citationScoreForText(source, text) {
  const sourceText = sourceSearchText(source);
  let score = 0;
  CITATION_MATCH_GROUPS.forEach((group) => {
    if (includesAnyText(text, group.aliases) && includesAnyText(sourceText, group.sourceTerms)) score += 30;
  });
  CITATION_TOPIC_GROUPS.forEach((group) => {
    if (includesAnyText(text, group.textTerms) && includesAnyText(sourceText, group.sourceTerms)) score += 10;
  });
  if (/official|pricing|docs|product|enterprise|security|help|openai|deepseek|volcengine|doubao/i.test(sourceText)) score += 3;
  if (String(source.credibility || "").toLowerCase() === "high") score += 2;
  score += Math.min(5, Number(source.relevance_score || 0) / 4);
  return score;
}

function bestCitationSourceForText(catalog, text, filterFn) {
  return catalog
    .filter((source) => /^https?:\/\//i.test(source.url_or_path || ""))
    .filter(filterFn)
    .map((source) => ({ source, score: citationScoreForText(source, text) }))
    .sort((a, b) => b.score - a.score || String(a.source.ref || "").localeCompare(String(b.source.ref || ""), "zh-Hans-CN", { numeric: true }))[0]?.source || null;
}

function citationSourcesForText(text, limit = 3) {
  const catalog = ((state.report && state.report.content && state.report.content.source_catalog) || [])
    .filter((source) => source && /^https?:\/\//i.test(source.url_or_path || ""));
  const cleaned = String(text || "").replace(/https?:\/\/\S+/g, " ").trim();
  if (!catalog.length || cleaned.length < 10) return [];
  const selected = [];
  const seen = new Set();
  CITATION_MATCH_GROUPS.forEach((group) => {
    if (!includesAnyText(cleaned, group.aliases)) return;
    const source = bestCitationSourceForText(catalog, cleaned, (row) => includesAnyText(sourceSearchText(row), group.sourceTerms));
    const key = source && (source.ref || source.url_or_path);
    if (source && key && !seen.has(key)) {
      selected.push(source);
      seen.add(key);
    }
  });
  if (!selected.length) {
    const source = bestCitationSourceForText(catalog, cleaned, () => true);
    const key = source && (source.ref || source.url_or_path);
    if (source && key) {
      selected.push(source);
      seen.add(key);
    }
  }
  if (selected.length < limit) {
    catalog
      .map((source) => ({ source, score: citationScoreForText(source, cleaned) }))
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score || String(a.source.ref || "").localeCompare(String(b.source.ref || ""), "zh-Hans-CN", { numeric: true }))
      .forEach((item) => {
        const key = item.source.ref || item.source.url_or_path;
        if (selected.length < limit && key && !seen.has(key)) {
          selected.push(item.source);
          seen.add(key);
        }
      });
  }
  return selected.slice(0, limit);
}

function renderSourceCitationFromSource(source) {
  const href = source && source.url_or_path ? source.url_or_path : "";
  const label = `[${sourceCitationNumber(source, href)}]`;
  const title = source ? `${source.ref || ""} ${source.title || ""} ${href}`.trim() : href;
  return el("sup", { className: "source-citation" }, [
    el("a", { href, target: "_blank", rel: "noopener noreferrer", text: label, title }),
  ]);
}

function appendTrailingCitations(node, text) {
  const sources = citationSourcesForText(text);
  if (!sources.length) return node;
  const existing = new Set(Array.from(node.querySelectorAll(".source-citation a")).map((link) => link.textContent.trim()));
  const additions = sources.filter((source) => !existing.has(`[${sourceCitationNumber(source, source.url_or_path || "")}]`));
  if (!additions.length) return node;
  node.append(document.createTextNode(" "));
  additions.forEach((source) => node.append(renderSourceCitationFromSource(source)));
  return node;
}

function splitUrlToken(token) {
  const match = String(token || "").match(/^(.*?)([.,;:!?，。；、]+)?$/);
  return {
    url: match ? match[1] : String(token || ""),
    suffix: match && match[2] ? match[2] : "",
  };
}

function renderSourceCitationFromUrl(token) {
  const { url, suffix } = splitUrlToken(token);
  const cleanSuffix = suffix.replace(/、+/g, "");
  const source = sourceRecordForUrl(url);
  const href = (source && source.url_or_path) || url;
  const label = `[${sourceCitationNumber(source, url)}]`;
  const title = source ? `${source.ref || ""} ${source.title || ""} ${href}`.trim() : href;
  const citation = el("sup", { className: "source-citation" }, [
    el("a", { href, target: "_blank", rel: "noopener noreferrer", text: label, title }),
  ]);
  return cleanSuffix ? [citation, document.createTextNode(cleanSuffix)] : [citation];
}

function renderReferenceSourceLine(title, token) {
  return el("p", { className: "reference-source-line" }, [renderReferenceSourceInline(title, token)]);
}

function renderReferenceSourceInline(title, token) {
  const { url } = splitUrlToken(token);
  const source = sourceRecordForUrl(url);
  const href = (source && source.url_or_path) || url;
  const label = `[${sourceCitationNumber(source, url)}]`;
  const displayTitle = cleanMarkdownLinkLabel(String(title || (source && source.title) || href).replace(/[：:]\s*$/g, ""));
  return el("span", { className: "reference-source-inline" }, [
    el("a", {
      className: "reference-source-number",
      href,
      target: "_blank",
      rel: "noopener noreferrer",
      text: label,
      title: href,
    }),
    el("a", {
      href,
      target: "_blank",
      rel: "noopener noreferrer",
      text: displayTitle || href,
      title: href,
    }),
  ]);
}

function isReferenceSourceTitle(title) {
  const cleaned = cleanMarkdownLinkLabel(title);
  return Boolean(cleaned) && cleaned.length <= 90 && !/[。；;]/.test(cleaned) && !String(title || "").includes("**");
}

function sourceRefLabel(ref) {
  const source = sourceRecordForRef(ref);
  const number = source ? sourceCitationNumber(source, source.url_or_path || "") : citationNumberFromRef(ref);
  return number ? `[${number}]` : String(ref || "");
}

function renderSingleSourceLink(ref) {
  const source = sourceRecordForRef(ref);
  const label = sourceCitationLabel(source, ref);
  const url = source ? source.url_or_path || "" : "";
  if (/^https?:\/\//i.test(url)) {
    return el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: label, title: source.title || url });
  }
  return el("button", { className: "source-ref-button", type: "button", onclick: () => openSourcesPanel([source && source.id ? source.id : ref], ref), text: label || "来源" });
}

function sourceCitationLabel(source, fallbackRef = "") {
  const number = source ? sourceCitationNumber(source, source.url_or_path || "") : citationNumberFromRef(fallbackRef);
  return number ? `[${number}]` : String(fallbackRef || "");
}

function citationNumberFromRef(ref) {
  const match = cleanSourceRefToken(ref).match(/^(?:S)?(\d+)$/i);
  return match ? match[1] : "";
}

function cleanSourceRefToken(value) {
  let token = String(value || "").trim();
  for (let index = 0; index < 3; index += 1) {
    token = token
      .replace(/^(?:资料来源|参考来源|来源|出处)\s*[：:]?\s*/g, "")
      .replace(/^[\[\]［］【】（）()\s]+|[\[\]［］【】（）()\s]+$/g, "")
      .replace(/[，,。；;、.]+$/g, "")
      .trim();
  }
  return token;
}

function hasSourceCue(value) {
  return /(?:资料来源|参考来源|来源|出处)\s*[：:]?/i.test(String(value || ""));
}

function isInternalSourceIdToken(value) {
  return /^[A-Za-z0-9]{4,}_(?:search|volc|ga|appark|rss|google|manual|cache|source|src|input|url)[A-Za-z0-9_]*$/i.test(cleanSourceRefToken(value));
}

function sourceRecordForCitationNumber(number) {
  const target = String(number || "").trim();
  if (!target) return null;
  const catalog = (state.report && state.report.content && state.report.content.source_catalog) || [];
  const catalogMatch = catalog.find((source) =>
    sourceCitationNumber(source, source.url_or_path || "") === target ||
    String(source.ref || "").toLowerCase() === `s${target}`,
  );
  if (catalogMatch) return catalogMatch;
  const localSources = (state.sources || []).map((source, index) => ({
    id: source.id,
    ref: `S${index + 1}`,
    title: source.title,
    url_or_path: source.url_or_path,
  }));
  return localSources.find((source) => sourceCitationNumber(source, source.url_or_path || "") === target) || null;
}

function renderCitationNumberLink(number) {
  const source = sourceRecordForCitationNumber(number);
  const label = sourceCitationLabel(source, number);
  const url = source ? source.url_or_path || "" : "";
  if (/^https?:\/\//i.test(url)) {
    return el("sup", { className: "source-citation" }, [
      el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: label, title: source.title || url }),
    ]);
  }
  return el("sup", { className: "source-citation" }, [
    el("button", {
      className: "source-ref-button",
      type: "button",
      onclick: () => openSourcesPanel([source && source.id ? source.id : `S${number}`], `S${number}`),
      text: label,
    }),
  ]);
}

function citationNumbersFromText(value) {
  const refs = [];
  String(value || "").replace(/\[?S?(\d+)\]?(?![A-Za-z0-9_])(?:\s*[-–—]\s*\[?S?(\d+)\]?(?![A-Za-z0-9_]))/gi, (_match, start, end) => {
    const from = Number(start);
    const to = Number(end);
    if (Number.isFinite(from) && Number.isFinite(to) && to >= from && to - from <= 20) {
      for (let value = from; value <= to; value += 1) refs.push(String(value));
    }
    return "";
  });
  String(value || "")
    .replace(/\[?S?\d+\]?(?![A-Za-z0-9_])\s*[-–—]\s*\[?S?\d+\]?(?![A-Za-z0-9_])/gi, " ")
    .replace(/\[?S?(\d+)\]?(?![A-Za-z0-9_])/gi, (_match, number) => {
      refs.push(String(number));
      return "";
    });
  return Array.from(new Set(refs));
}

function appendTextWithExplicitSourceRefs(node, value) {
  const text = String(value || "");
  const internalId = String.raw`[A-Za-z0-9]{4,}_(?:search|volc|ga|appark|rss|google|manual|cache|source|src|input|url)[A-Za-z0-9_]*`;
  const bracketedNumeric = String.raw`(?:[\[［【]?\s*S?\d+\s*[\]］】]?)`;
  const sourceToken = String.raw`(?:${bracketedNumeric}|${internalId})(?![A-Za-z0-9_])`;
  const pattern = new RegExp(`(?:资料来源|参考来源|来源|出处)[：:]?\\s*(${sourceToken}(?:\\s*(?:[、,，/;；]|和|及|-|–|—)\\s*${sourceToken})*)\\s*[，,。；;、.]*`, "gi");
  let cursor = 0;
  let match;
  while ((match = pattern.exec(text))) {
    if (match.index > cursor) node.append(document.createTextNode(text.slice(cursor, match.index)));
    const numbers = sourceNumbersFromBracketContent(match[1]);
    if (numbers.length) {
      numbers.forEach((number) => node.append(renderCitationNumberLink(number)));
    }
    cursor = pattern.lastIndex;
  }
  if (cursor < text.length) node.append(document.createTextNode(text.slice(cursor)));
}

function sourceNumbersFromBracketContent(value, options = {}) {
  const refs = [];
  const allowPlainNumber = options.allowPlainNumber !== false;
  const normalized = String(value || "").replace(/^(?:资料来源|参考来源|来源|出处)\s*[：:]?\s*/g, "");
  normalized
    .split(/\s*(?:[、,，/;；]|和|及|-|–|—)\s*/)
    .forEach((item) => {
      const cleaned = cleanSourceRefToken(item);
      if (!cleaned) return;
      const source = catalogSourceRecordForRef(cleaned) || sourceRecordForRef(cleaned);
      if (source) {
        refs.push(sourceCitationNumber(source, source.url_or_path || ""));
        return;
      }
      if (!allowPlainNumber && /^\d+$/.test(cleaned)) return;
      const number = citationNumberFromRef(cleaned);
      if (number) refs.push(number);
    });
  return Array.from(new Set(refs.filter(Boolean)));
}

function appendTextWithBracketedSourceRefs(node, value) {
  const text = String(value || "");
  const pattern = /\[\s*([^\]\n]{1,260})\s*\]|［\s*([^］\n]{1,260})\s*］|【\s*([^】\n]{1,260})\s*】|（\s*([^）\n]{1,260})\s*）|\(\s*([^)\n]{1,260})\s*\)/g;
  let cursor = 0;
  let match;
  while ((match = pattern.exec(text))) {
    if (match.index > cursor) {
      appendTextWithExplicitSourceRefs(node, text.slice(cursor, match.index));
    }
    const content = match.slice(1).find((item) => item !== undefined) || "";
    const bracket = match[0].trim().slice(0, 1);
    const allowPlainNumber = bracket !== "(" && bracket !== "（";
    const numbers = sourceNumbersFromBracketContent(content, { allowPlainNumber });
    if (numbers.length) {
      numbers.forEach((number) => node.append(renderCitationNumberLink(number)));
    } else if (!hasSourceCue(content) && !isInternalSourceIdToken(content)) {
      appendTextWithExplicitSourceRefs(node, match[0]);
    }
    cursor = pattern.lastIndex;
  }
  if (cursor < text.length) appendTextWithExplicitSourceRefs(node, text.slice(cursor));
}

function replaceInternalSourceIdsWithCatalogRefs(value) {
  return String(value || "").replace(
    /\b[A-Za-z0-9]{4,}_(?:search|volc|ga|appark|rss|google|manual|cache|source|src|input|url)[A-Za-z0-9_]*\b/gi,
    (sourceId) => {
      const source = catalogSourceRecordForRef(sourceId);
      return source && source.ref ? source.ref : "";
    },
  );
}

function stripInternalSourceIdsForDisplay(value) {
  return String(value || "")
    .replace(/（\s*(?:资料来源|参考来源|来源|出处)[：:]?\s*(?:[A-Za-z0-9]{4,}_(?:search|volc|ga|appark|rss|google|manual|cache|source|src|input|url)[A-Za-z0-9_]*(?:\s*[、,，/]\s*)?)+\s*）/gi, "")
    .replace(/\(\s*(?:资料来源|参考来源|来源|出处)[：:]?\s*(?:[A-Za-z0-9]{4,}_(?:search|volc|ga|appark|rss|google|manual|cache|source|src|input|url)[A-Za-z0-9_]*(?:\s*[、,，/]\s*)?)+\s*\)/gi, "")
    .replace(/(?:资料来源|参考来源|来源|出处)[：:]?\s*(?:[A-Za-z0-9]{4,}_(?:search|volc|ga|appark|rss|google|manual|cache|source|src|input|url)[A-Za-z0-9_]*(?:\s*[、,，/]\s*)?)+/gi, "")
    .replace(/\b[A-Za-z0-9]{4,}_(?:search|volc|ga|appark|rss|google|manual|cache|source|src|input|url)[A-Za-z0-9_]*\b/gi, "")
    .replace(/[\[［]\s*(?:资料来源|参考来源|来源|出处)[：:]?\s*[，,。；;、.\s]*[\]］]/gi, "")
    .replace(/[（(]\s*(?:资料来源|参考来源|来源|出处)[：:]?\s*[，,。；;、.\s]*[）)]/gi, "")
    .replace(/（\s*）|\(\s*\)/g, "")
    .replace(/\s+([，,。；;）)])/g, "$1")
    .replace(/([（(])\s+/g, "$1")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function renderEvidenceRefs(refs = []) {
  const items = (refs || []).filter(Boolean);
  if (!items.length) return el("span", { className: "evidence-links empty", text: "未列入正文依据" });
  return el("span", { className: "evidence-links" }, items.map((ref) => renderSingleSourceLink(ref)));
}

function renderFullReportSection(section, index) {
  const children = [
    el("div", { className: "full-section-heading" }, [
      el("span", { text: reportSectionOrdinal(section, index) }),
      el("h2", { text: reportSectionTitle(section, index, false) }),
    ]),
    section.markdown ? renderMarkdownBlock(syncMarkdownSectionNumbers(section.markdown, index)) : el("p", { text: section.body }),
  ];
  if (section.table) children.push(renderTable(section.table));
  (section.claims || []).forEach((claim) => {
    children.push(
      el("blockquote", { className: "report-quote" }, [
        el("span", { className: "badge", text: claimTypeLabel(claim.claim_type) }),
        el("p", { text: claim.content }),
        el("div", { className: "report-evidence-row" }, [
          el("span", { text: "来源：" }),
          renderEvidenceRefs(claim.source_refs || claim.source_ids || []),
        ]),
      ]),
    );
  });
  return el("section", { className: "full-report-section", id: sectionAnchorId(index) }, children);
}

function renderMarkdownBlock(markdown) {
  const container = el("div", { className: "markdown-report" });
  const lines = normalizeMarkdownText(markdown).split(/\r?\n/);
  let list = null;
  let tableRows = [];
  let bulletRows = [];
  let orderedRows = [];
  let paragraph = [];

  const flushParagraph = () => {
    const text = paragraph.join(" ").trim();
    paragraph = [];
    if (text) container.append(renderMarkdownInline("p", text, { appendCitations: true }));
  };
  const flushList = () => {
    if (list) {
      container.append(list);
      list = null;
    }
  };
  const flushBulletRows = () => {
    if (bulletRows.length) {
      container.append(renderTable(
        bulletRows.map((item) => [renderMarkdownInline("span", item, { appendCitations: true })]),
        { className: "bullet-table", headerRows: 0 },
      ));
      bulletRows = [];
    }
  };
  const flushOrderedRows = () => {
    if (orderedRows.length) {
      container.append(renderTable(orderedRows, { className: "ordered-table", headerRows: 0 }));
      orderedRows = [];
    }
  };
  const flushTable = () => {
    if (tableRows.length) {
      container.append(renderTable(tableRows));
      tableRows = [];
    }
  };

  lines.forEach((rawLine) => {
    const line = rawLine.trim();
    if (!line) {
      flushParagraph();
      flushList();
      flushBulletRows();
      flushOrderedRows();
      flushTable();
      return;
    }
    const tableCells = parseMarkdownTableRow(line);
    if (tableCells && !isMarkdownTableSeparator(tableCells)) {
      flushParagraph();
      flushList();
      flushBulletRows();
      flushOrderedRows();
      tableRows.push(tableCells);
      return;
    }
    if (tableCells && isMarkdownTableSeparator(tableCells)) {
      return;
    }
    flushTable();
    const heading = line.match(/^(#{3,5})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      flushBulletRows();
      flushOrderedRows();
      const level = Math.min(5, heading[1].length);
      container.append(renderMarkdownInline(`h${level}`, heading[2]));
      return;
    }
    const image = line.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
    if (image) {
      flushParagraph();
      flushList();
      flushBulletRows();
      flushOrderedRows();
      const src = image[2].startsWith("http") || image[2].startsWith("/")
        ? image[2]
        : `/static/${image[2]}`;
      container.append(el("figure", {}, [
        el("img", { src, alt: image[1] || "报告截图", loading: "lazy" }),
        el("figcaption", { text: image[1] || "页面截图" }),
      ]));
      return;
    }
    const bullet = line.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      flushParagraph();
      flushList();
      flushOrderedRows();
      flushTable();
      bulletRows.push(bullet[1]);
      return;
    }
    const referenceSource = line.match(/^\d+\.\s+(.+?)[：:]\s*(https?:\/\/[^\s]+)$/);
    if (referenceSource && isReferenceSourceTitle(referenceSource[1])) {
      flushParagraph();
      flushList();
      flushBulletRows();
      flushTable();
      orderedRows.push([renderReferenceSourceInline(referenceSource[1], referenceSource[2])]);
      return;
    }
    const ordered = line.match(/^(\d+)\.\s+(.+)$/);
    if (ordered) {
      flushParagraph();
      flushList();
      flushBulletRows();
      flushTable();
      orderedRows.push([ordered[1], renderMarkdownInline("span", ordered[2], { appendCitations: true })]);
      return;
    }
    flushBulletRows();
    flushOrderedRows();
    paragraph.push(line);
  });
  flushParagraph();
  flushList();
  flushBulletRows();
  flushOrderedRows();
  flushTable();
  return container;
}

function parseMarkdownTableRow(line) {
  if (!line.includes("|")) return null;
  const trimmed = line.replace(/^\|/, "").replace(/\|$/, "");
  const cells = trimmed.split("|").map((cell) => cell.trim());
  return cells.length >= 2 ? cells : null;
}

function isMarkdownTableSeparator(cells) {
  return cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function normalizeMarkdownText(markdown) {
  let text = String(markdown || "").replace(/竞争情报分析师/g, "MOSS团队").replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  if (!text) return "";
  text = text
    .replace(/\s+(#{3,5}\s+)/g, "\n\n$1")
    .replace(/\s+(-\s+\*\*[^*]+?\*\*[：:])/g, "\n$1")
    .replace(/\s+(-\s+)/g, "\n$1")
    .replace(/\s+(\d+\.\s+)/g, "\n$1")
    .replace(/\s+(>\s+)/g, "\n\n$1")
    .replace(/\n{4,}/g, "\n\n\n");
  [
    "核心发现",
    "市场规模与增长趋势",
    "市场结构与增长逻辑",
    "用户需求与痛点",
    "技术与产品趋势",
    "竞品分层框架",
    "产品定位",
    "核心功能",
    "差异化",
    "价格",
    "用户分层",
    "近期更新",
    "分发渠道",
    "商业模式",
    "风险短板",
    "收入模式对比",
    "订阅制与免费试用",
    "按量计费/API/SDK",
    "企业版与服务收入",
    "用户获取策略",
    "渠道策略",
    "生态与集成",
    "用户画像分析",
    "使用场景分析",
    "技术壁垒",
    "数据与用户反馈壁垒",
    "生态壁垒",
    "品牌与渠道壁垒",
    "市场机会",
    "产品策略建议",
    "产品定位建议",
    "功能规划建议",
    "定价策略建议",
    "增长策略建议",
    "风险规避建议",
    "来源列表",
    "测试方法",
  ].forEach((title) => {
    text = text.replace(new RegExp(`(#{3,5}\\s+\\d+(?:\\.\\d+)*\\s+${escapeRegExp(title)})\\s+(?=\\S)`, "gm"), "$1\n");
  });
  return text.trim();
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function renderMarkdownInline(tag, text, options = {}) {
  const node = el(tag, {});
  const sourceNormalizedText = replaceInternalSourceIdsWithCatalogRefs(text);
  const cleanedText = stripInternalSourceIdsForDisplay(sourceNormalizedText).replace(
      /(^|[\s（(，,；;。])(?:资料来源|参考来源|来源|出处)[:：]\s*(?=(?:https?:\/\/|\[[^\]]+\]\(https?:\/\/))/g,
      "$1",
    );
  const parts = cleanedText.split(/(\*\*[^*]+\*\*|\[[^\]]+\]\(https?:\/\/[^)]+\)|https?:\/\/[^\s)\]）}，。；、]+)/g);
  parts.forEach((part) => {
    if (!part) return;
    if (/^\s*、+\s*$/.test(part)) return;
    const bold = part.match(/^\*\*([^*]+)\*\*$/);
    if (bold) {
      node.append(el("strong", { text: bold[1] }));
      return;
    }
    const link = part.match(/^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/);
    if (link) {
      if (/^https?:\/\//i.test(link[1])) {
        node.append(...renderSourceCitationFromUrl(link[2]));
      } else if (isEvidenceStyleLinkLabel(link[1])) {
        const label = cleanMarkdownLinkLabel(link[1]);
        if (label) node.append(document.createTextNode(label));
        node.append(...renderSourceCitationFromUrl(link[2]));
      } else {
        node.append(el("a", { href: link[2], target: "_blank", rel: "noopener noreferrer", text: cleanMarkdownLinkLabel(link[1]) }));
      }
      return;
    }
    if (/^https?:\/\//.test(part)) {
      node.append(...renderSourceCitationFromUrl(part));
      return;
    }
    appendTextWithBracketedSourceRefs(node, part);
  });
  return options.appendCitations ? appendTrailingCitations(node, text) : node;
}

function cleanMarkdownLinkLabel(value) {
  return String(value || "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/(?:资料来源|参考来源|来源|出处)[：:]?\s*$/g, "")
    .trim();
}

function isEvidenceStyleLinkLabel(value) {
  const raw = String(value || "");
  const cleaned = cleanMarkdownLinkLabel(raw);
  return raw.includes("**") || /(?:资料来源|参考来源|来源|出处)/.test(raw) || cleaned.length > 36;
}

function claimTypeLabel(value) {
  const labels = {
    fact: "事实",
    inference: "推断",
    recommendation: "建议",
    assumption: "假设",
  };
  return labels[value] || "事实";
}

function renderClaimActions(claim) {
  const sourceRefs = claim.source_refs || claim.source_ids || [];
  const reviewPrompt = `请复查这条待复核结论，并补充来源、说明不确定性或要求重新质检：${claim.content || ""}`;
  const actions = [
    el("button", {
      className: "mini-button",
      type: "button",
      onclick: () => openSourcesPanel(sourceRefs, claim.id || claim.content),
      text: "查看证据",
    }),
    el("button", {
      className: "mini-button",
      type: "button",
      onclick: (event) => recheckQa(event.currentTarget),
      text: "重新质检",
    }),
  ];
  if (claim.needs_review) {
    actions.unshift(el("span", { className: "badge warn", text: "待复核" }));
    actions.push(
      el("button", {
        className: "mini-button",
        type: "button",
        onclick: () => openManualModal(claim.content || "", reviewPrompt, "selection"),
        text: "补充复查说明",
      }),
      el("button", {
        className: "mini-button",
        type: "button",
        onclick: (event) =>
          submitManualText("确认这条低置信度结论，并记录为人工确认。", claim.content, claim.id || "", event.currentTarget),
        text: "确认该结论",
      }),
    );
  }
  return actions;
}

function renderClaims(claims) {
  if (!claims.length) return el("div", { className: "claim-list" });
  return el(
    "div",
    { className: "claim-list" },
    claims.map((claim) => {
      return el("div", { className: "claim" }, [
        el("p", { text: claim.content }),
        el("div", { className: "claim-actions" }, renderClaimActions(claim)),
      ]);
    }),
  );
}

function renderTable(rows, options = {}) {
  const table = el("table", { className: options.className || "" });
  const headerRows = Number.isFinite(Number(options.headerRows)) ? Number(options.headerRows) : 1;
  rows.forEach((row, index) => {
    const tr = el("tr");
    row.forEach((cell) => {
      const content = cell && typeof cell.nodeType === "number"
        ? cell
        : renderTableCellContent(cell, index >= headerRows);
      tr.append(el(index < headerRows ? "th" : "td", {}, [content]));
    });
    table.append(tr);
  });
  return table;
}

function renderTableCellContent(cell, allowColonLabel = true) {
  const text = String(cell ?? "");
  const shouldAppendCitations = allowColonLabel && hasSourceCueForDisplay(text);
  if (allowColonLabel) {
    const match = text.match(/^([^：:\n]{2,28})([：:])\s*(.+)$/);
    if (match && !/^https?$/i.test(match[1])) {
      const label = cleanMarkdownLinkLabel(match[1]);
      const rest = renderMarkdownInline("span", match[3], { appendCitations: shouldAppendCitations });
      return el("span", { className: "table-cell-labeled" }, [
        el("strong", { text: `${label || match[1]}${match[2]}` }),
        document.createTextNode(" "),
        ...Array.from(rest.childNodes),
      ]);
    }
  }
  return renderMarkdownInline("span", text, { appendCitations: shouldAppendCitations });
}

function hasSourceCueForDisplay(value) {
  return /(?:资料来源|参考来源|来源|出处|[A-Za-z0-9]{4,}_(?:search|volc|ga|appark|rss|google|manual|cache|source|src|input|url)[A-Za-z0-9_]*)/i.test(String(value || ""));
}

function openSectionModal(section) {
  const modalKey = section.key || section.title;
  if (!$("#modalBackdrop").classList.contains("hidden") && state.activeModalKey === modalKey) {
    setHidden("#modalBackdrop", true);
    state.activeModalKey = "";
    return;
  }
  state.activeModalKey = modalKey;
  $("#modalTitle").textContent = section.title;
  const children = [el("p", { text: section.body })];
  if (section.table) children.push(renderTable(section.table));
  children.push(renderClaims(section.claims || []));
  $("#modalBody").replaceChildren(...children);
  setHidden("#modalBackdrop", false);
}

function sourceUrlNode(source) {
  const value = source.url_or_path || "";
  if (!/^https?:\/\//i.test(value)) return el("span", { text: value });
  try {
    const parsed = new URL(value);
    if (!["http:", "https:"].includes(parsed.protocol)) {
      return el("span", { text: value });
    }
    return el("a", {
      href: parsed.href,
      target: "_blank",
      rel: "noopener noreferrer",
      text: value,
    });
  } catch (error) {
    return el("span", { text: value });
  }
}

function openSourcesPanel(sourceIds = [], key = "") {
  const panelKey = key || sourceIds.slice().sort().join("|") || "all";
  if (!$("#sourcesPanel").classList.contains("hidden") && state.activeSourcesKey === panelKey) {
    setHidden("#sourcesPanel", true);
    resetFloatingPosition("#sourcesPanel");
    state.activeSourcesKey = "";
    return;
  }
  state.activeSourcesKey = panelKey;
  const selected = sourceIds.length
    ? state.sources.filter((source) => sourceIds.includes(source.id))
    : state.sources;
  const rows = selected.length ? selected : state.sources;
  const table = el("table");
  const header = el("tr");
  ["来源标题", "类型", "提供方", "搜索日志编号", "网址或文件路径", "发布时间", "原文片段", "采集时间", "可信度", "关联结论"].forEach((title) =>
    header.append(el("th", { text: title })),
  );
  table.append(header);
  rows.forEach((source) => {
    const relatedText = (source.related_claims || []).map((claim) => claim.content).join("；");
    table.append(
      el("tr", {}, [
        el("td", { text: source.title }),
        el("td", { text: sourceTypeLabel(source.source_type) }),
        el("td", { text: providerLabel(source.provider || "") }),
        el("td", { text: source.search_log_id || "" }),
        el("td", {}, [sourceUrlNode(source)]),
        el("td", { text: source.published_at || source.publish_time || "" }),
        el("td", { text: source.excerpt }),
        el("td", { text: formatDateTime(source.collected_at) }),
        el("td", { text: credibilityLabel(source.credibility) }),
        el("td", { text: relatedText || "暂未关联" }),
      ]),
    );
  });
  const children = [];
  if (sourceIds.length && !selected.length) {
    children.push(el("p", { className: "panel-empty", text: "这条结论的来源已更新或暂未匹配到，先展示本任务全部来源供核对。" }));
  }
  children.push(table);
  $("#sourcesTableWrap").replaceChildren(...children);
  setHidden("#sourcesPanel", false);
}

async function recheckQa(button = null) {
  if (!state.task) return;
  await withButtonLoading(button, "质检中...", async () => {
    try {
      const result = await api(`/api/tasks/${state.task.id}/qa/recheck`, { method: "POST", body: "{}" });
      animatePlan(false);
      await loadTaskState();
      renderBoard();
      renderReport();
      if (result.status === "no_change") {
        showToast(result.result_summary || "未修复前复检不会改变结果。");
      } else if (result.status === "busy") {
        showToast(result.result_summary || "自动流程仍在运行中，请等待报告自动生成。");
      } else if (result.status === "needs_review") {
        showToast("重新质检完成：仍有开放问题，请在待复核结论旁补充复查说明或补充来源。");
      } else {
        showToast("重新质检通过，报告版本已更新。", "success");
      }
    } catch (error) {
      showToast(`重新质检失败：${error.message}`, "error");
    }
  });
}

async function repairQaFinding(findingId, action = "auto_collect", button = null, userText = "") {
  if (!state.task || !findingId) return;
  const payload = {
    action,
    user_text: userText || (action === "manual_supplement" ? "请根据该质检问题补充材料，并重新分析。" : ""),
  };
  await withButtonLoading(button, "修复中...", async () => {
    try {
      const result = await api(`/api/tasks/${state.task.id}/qa/findings/${findingId}/repair`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      animatePlan(result.status !== "completed");
      await loadTaskState();
      renderBoard();
      renderReport();
      if (result.status === "busy") {
        showToast(result.result_summary || "自动流程仍在运行中，请等待报告自动生成。");
        return;
      }
      showToast(result.result_summary || "已执行质检问题修复。", result.status === "completed" ? "success" : "info");
    } catch (error) {
      showToast(`质检问题修复失败：${error.message}`, "error");
    }
  });
}

function renderManualGuide(mode, options = {}) {
  const guide = $("#manualGuide");
  if (!guide) return;
  guide.replaceChildren();
  if (mode !== "qa_finding") {
    setHidden("#manualGuide", true);
    return;
  }
  const finding = options.finding || {};
  const guidance = finding.supplement_guidance || {};
  const whatToAdd = guidance.what_to_add || [finding.missing_material || finding.action_hint || "补充可追溯来源或人工确认。"];
  const formats = guidance.accepted_formats || [
    "可打开的网页链接",
    "截图、PDF、表格或上传材料摘要",
    "人工确认说明和来源日期",
  ];
  const currentSources = guidance.current_sources || [];
  const queryItems = guidance.suggested_queries || finding.suggested_queries || [];
  const nodes = [
    el("div", { className: "manual-guide-head" }, [
      el("strong", { text: guidance.title || "质检 Agent 建议补充的材料" }),
      el("span", { text: findingTypeLabel(finding.finding_type) }),
    ]),
    el("p", { text: guidance.issue || finding.reason || "当前证据链不足，需要补来源、补解释或人工确认。" }),
    guidance.claim_preview ? el("div", { className: "manual-guide-claim" }, [
      el("span", { text: "待复核结论" }),
      el("p", { text: guidance.claim_preview }),
    ]) : null,
    el("div", { className: "manual-guide-grid" }, [
      el("div", {}, [
        el("span", { text: "建议补充" }),
        el("ul", {}, whatToAdd.slice(0, 4).map((item) => el("li", { text: item }))),
      ]),
      el("div", {}, [
        el("span", { text: "可接受格式" }),
        el("ul", {}, formats.slice(0, 4).map((item) => el("li", { text: item }))),
      ]),
    ]),
    currentSources.length ? el("div", { className: "manual-guide-line" }, [
      el("span", { text: "当前已有来源" }),
      el("p", { text: currentSources.join("；") }),
    ]) : null,
    queryItems.length ? el("div", { className: "qa-queries" }, queryItems.slice(0, 4).map((query) => el("span", { className: "qa-query", text: query }))) : null,
  ].filter(Boolean);
  guide.append(...nodes);
  setHidden("#manualGuide", false);
}

function openManualModal(selectedText = "", prompt = "", mode = "global", options = {}) {
  const hasSelectedText = mode === "selection" && Boolean(selectedText.trim());
  state.selectedText = hasSelectedText ? selectedText : "";
  state.manualFindingId = options.findingId || "";
  state.manualClaimId = options.claimId || "";
  state.manualQaAction = options.action || (hasSelectedText ? "revise_claim" : "manual_supplement");
  $("#manualTitle").textContent = mode === "qa_finding"
    ? (state.manualQaAction === "dispute_claim" ? "质疑/打回这条结论" : "按质检建议补充来源/材料")
    : hasSelectedText ? "复查这条结论" : "人工复查/补充材料";
  setHidden("#selectedTextField", !hasSelectedText);
  $("#selectedTextInput").value = hasSelectedText ? selectedText : "";
  renderManualGuide(mode, options);
  $("#manualTextLabel").textContent = mode === "qa_finding"
    ? (state.manualQaAction === "dispute_claim" ? "请说明质疑原因、错误点或需要降级的依据" : "请粘贴来源、材料摘要或人工确认说明")
    : (state.manualQaAction === "revise_claim" ? "修正说明" : "复查说明");
  $("#manualTextInput").value = prompt;
  $("#manualTextInput").setAttribute(
    "placeholder",
    mode === "qa_finding"
      ? (state.manualQaAction === "dispute_claim"
        ? "例如：这条判断不准确，缺少某竞品官方来源，请打回并重新补证。"
        : "按上方建议粘贴网址、材料原文、文件摘要，或说明为什么该结论可以确认/需要降级。")
      : hasSelectedText ? "例如：这段结论不准确，应改为……；请重新搜索/质检并更新该段。" : "例如：请复查开放质检问题，并补充缺失来源或不确定性说明。",
  );
  setHidden("#manualBackdrop", false);
  $("#manualTextInput").focus();
}

function closeManualModal() {
  state.manualFindingId = "";
  state.manualClaimId = "";
  state.manualQaAction = "";
  setHidden("#manualBackdrop", true);
}

async function submitManualText(userText, selectedText = "", claimId = "", button = null, action = "") {
  if (!state.task) return;
  const manualAction = action || state.manualQaAction || (selectedText ? "revise_claim" : "manual_supplement");
  await withButtonLoading(button, manualAction === "revise_claim" ? "重跑中..." : "确认中...", async () => {
    try {
      if (manualAction === "revise_claim") {
        showToast("已提交修正：正在重跑相关分析、质检并生成新报告...", "info");
      }
      const result = await api(`/api/tasks/${state.task.id}/manual-actions`, {
        method: "POST",
        body: JSON.stringify({ user_text: userText, selected_text: selectedText, claim_id: claimId, action: manualAction }),
      });
      animatePlan(!userText.includes("确认"));
      await loadTaskState();
      renderBoard();
      renderReport();
      if (result.status === "busy") {
        showToast(result.result_summary || "自动流程仍在运行中，请稍后再试。");
      } else {
        showToast(result.result_summary || (claimId ? "该条结论已人工确认，报告已刷新。" : "人工复查说明已记录，报告已刷新。"), "success");
      }
    } catch (error) {
      showToast(`人工复查失败：${error.message}`, "error");
    }
  });
}

async function refreshAfterManualBackground(taskId, result, defaultMessage) {
  if (!state.task || state.task.id !== taskId) return;
  animatePlan(result && result.status !== "completed");
  await loadTaskState({ allowMissingReport: true });
  renderBoard();
  if (state.report) renderReport();
  revealTaskNav();
  if (result && result.status === "busy") {
    showToast(result.result_summary || "自动流程仍在运行中，请稍后查看任务页。");
  } else {
    showToast((result && result.result_summary) || defaultMessage, (result && result.status === "needs_review") ? "info" : "success");
  }
}

async function submitManualTextForTask(taskId, userText, selectedText = "", claimId = "", action = "") {
  const manualAction = action || (selectedText ? "revise_claim" : "manual_supplement");
  try {
    const result = await api(`/api/tasks/${taskId}/manual-actions`, {
      method: "POST",
      body: JSON.stringify({ user_text: userText, selected_text: selectedText, claim_id: claimId, action: manualAction }),
    });
    await refreshAfterManualBackground(taskId, result, "复核已完成，报告版本已刷新。");
  } catch (error) {
    showToast(`人工复核后台提交失败：${error.message}`, "error");
  }
}

async function repairQaFindingForTask(taskId, findingId, action = "manual_supplement", userText = "") {
  try {
    const result = await api(`/api/tasks/${taskId}/qa/findings/${findingId}/repair`, {
      method: "POST",
      body: JSON.stringify({ action, user_text: userText }),
    });
    await refreshAfterManualBackground(taskId, result, "质检问题复核已完成，报告版本已刷新。");
  } catch (error) {
    showToast(`质检问题后台复核失败：${error.message}`, "error");
  }
}

async function submitManualForm(event) {
  if (event) event.preventDefault();
  if (state.manualSubmitting) return;
  const text = $("#manualTextInput").value.trim();
  if (!text) return;
  if (!state.task || !state.task.id) return;
  state.manualSubmitting = true;
  const taskId = state.task.id;
  const findingId = state.manualFindingId;
  const claimId = state.manualClaimId;
  const action = state.manualQaAction;
  const selectedText = $("#selectedTextInput").value;
  closeManualModal();
  show("#boardView");
  animatePlan(true);
  showToast("已提交复核请求，正在重跑相关采集、分析、质检和报告步骤...", "info");
  const backgroundTask = findingId
    ? repairQaFindingForTask(taskId, findingId, action || "manual_supplement", text)
    : submitManualTextForTask(taskId, text, selectedText, claimId, action);
  backgroundTask.finally(() => {
    state.manualSubmitting = false;
  });
}

function toggleReportMode() {
  state.fullMode = true;
  setHidden("#reportBoards", true);
  setHidden("#reportMetrics", true);
  setHidden("#fullReport", false);
  setHidden("#toggleReportModeButton", true);
}

function initDragging(panelSelector, handleSelector) {
  const panel = $(panelSelector);
  const handle = panel.querySelector(handleSelector);
  let offsetX = 0;
  let offsetY = 0;
  let dragging = false;
  handle.addEventListener("mousedown", (event) => {
    dragging = true;
    const rect = panel.getBoundingClientRect();
    offsetX = event.clientX - rect.left;
    offsetY = event.clientY - rect.top;
    document.body.style.userSelect = "none";
  });
  document.addEventListener("mousemove", (event) => {
    if (!dragging) return;
    panel.style.left = `${Math.max(8, event.clientX - offsetX)}px`;
    panel.style.top = `${Math.max(70, event.clientY - offsetY)}px`;
    panel.style.right = "auto";
  });
  document.addEventListener("mouseup", () => {
    dragging = false;
    document.body.style.userSelect = "";
  });
}

function initContextMenu() {
  document.addEventListener("contextmenu", (event) => {
    if (event.target.closest(".history-item")) return;
    const selection = window.getSelection().toString().trim();
    if (!selection || $("#reportView").classList.contains("hidden")) return;
    event.preventDefault();
    state.selectedText = selection.slice(0, 1000);
    const menu = $("#contextMenu");
    menu.style.left = `${event.clientX}px`;
    menu.style.top = `${event.clientY}px`;
    setHidden("#contextMenu", false);
  });
  document.addEventListener("click", () => {
    setHidden("#contextMenu", true);
    setHidden("#historyContextMenu", true);
  });
  $("#contextMenu").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const action = button.dataset.action;
    const actionMap = {
      修正结论: "revise_claim",
      补充来源: "supplement_source",
      要求重新搜索: "supplement_source",
      要求重新质检: "recheck_qa",
    };
    setHidden("#contextMenu", true);
    openManualModal(state.selectedText, `${action}：请处理我选中的这段内容。`, "selection", { action: actionMap[action] || "revise_claim" });
  });
  $("#historyContextMenu").addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    event.stopPropagation();
    setHidden("#historyContextMenu", true);
    const taskId = state.historyMenuTaskId;
    state.historyMenuTaskId = "";
    if (button.dataset.action === "archive") {
      await archiveHistoryTask(taskId);
    } else if (button.dataset.action === "delete") {
      openDeleteDialog(taskId);
    }
  });
}

function wireEvents() {
  $("#taskForm").addEventListener("submit", startTask);
  $("#taskPromptInput").addEventListener("input", () => {
    resizePromptInput();
    updatePromptFeedback();
  });
  $("#taskPromptInput").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    $("#taskForm").requestSubmit();
  });
  $("#sourceMenuButton").addEventListener("click", (event) => {
    event.stopPropagation();
    const isHidden = $("#sourceMenu").classList.contains("hidden");
    setHidden("#sourceMenu", !isHidden);
    $("#sourceMenuButton").setAttribute("aria-expanded", String(isHidden));
  });
  $("#sourceMenu").addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", () => {
    setHidden("#sourceMenu", true);
    $("#sourceMenuButton").setAttribute("aria-expanded", "false");
  });
  $("#onlineSearchButton").addEventListener("click", () => {
    state.onlineSearchEnabled = !state.onlineSearchEnabled;
    updateSourceModeStatus();
  });
  $("#uploadMenuButton").addEventListener("click", () => {
    $("#uploadInput").click();
    setHidden("#sourceMenu", true);
    $("#sourceMenuButton").setAttribute("aria-expanded", "false");
  });
  $("#questionnaireMenuButton").addEventListener("click", () => {
    state.surveyResearchSelected = true;
    updateSourceModeStatus();
    setHidden("#sourceMenu", true);
    $("#sourceMenuButton").setAttribute("aria-expanded", "false");
    openResearchPanel("questionnaire");
    showToast("已打开问卷调研助手；生成后可复制问卷链接。", "info");
  });
  $("#uploadInput").addEventListener("change", () => {
    const files = Array.from($("#uploadInput").files || []);
    if (!files.length) return;
    addUploadFiles(files);
    $("#uploadInput").value = "";
    updateSourceModeStatus();
  });
  // Floating research panel
  $("#researchFloatBall").addEventListener("click", () => openResearchPanel("questionnaire"));
  $("#closeResearchPanelButton").addEventListener("click", closeResearchPanel);
  $("#questionnaireTabButton").addEventListener("click", () => switchResearchTab("questionnaire"));
  $("#interviewTabButton").addEventListener("click", () => switchResearchTab("interview"));
  $("#researchForm").addEventListener("submit", submitResearchForm);

  $("#homeButton").addEventListener("click", showCreateHome);
  $("#newChatButton").addEventListener("click", startNewConversation);
  $("#refreshHistoryButton").addEventListener("click", loadHistory);
  $("#historyTabButton").addEventListener("click", async () => {
    state.historyArchived = false;
    await loadHistory();
  });
  $("#archiveTabButton").addEventListener("click", async () => {
    state.historyArchived = true;
    await loadHistory();
  });
  $("#boardNavButton").addEventListener("click", () => {
    if (state.task) {
      renderBoard();
      show("#boardView");
    }
  });
  $("#editTaskButton").addEventListener("click", editCurrentTaskAsNew);
  $("#stopTaskButton").addEventListener("click", stopCurrentTask);
  $("#reportNavButton").addEventListener("click", () => {
    if (state.report) {
      renderReport();
      show("#reportView");
    }
  });
  $("#toggleLogsButton").addEventListener("click", async () => {
    const willOpen = $("#logPanel").classList.contains("hidden");
    if (willOpen) resetFloatingPosition("#logPanel");
    setHidden("#logPanel", !willOpen);
    if (!willOpen) return;
    await renderLogs();
  });
  $("#closeLogsButton").addEventListener("click", () => {
    setHidden("#logPanel", true);
    resetFloatingPosition("#logPanel");
  });
  $("#logFilter").addEventListener("change", renderLogs);
  $("#logStatusFilter").addEventListener("change", renderLogs);
  $("#logSeverityFilter").addEventListener("change", renderLogs);
  $("#logReworkFilter").addEventListener("change", renderLogs);
  $("#downloadLogsButton").addEventListener("click", downloadLogs);
  $("#downloadPdfButton").addEventListener("click", downloadPdf);
  $("#closeSourcesButton").addEventListener("click", () => {
    setHidden("#sourcesPanel", true);
    resetFloatingPosition("#sourcesPanel");
    state.activeSourcesKey = "";
  });
  $("#closeModalButton").addEventListener("click", () => {
    setHidden("#modalBackdrop", true);
    state.activeModalKey = "";
  });
  $("#modalBackdrop").addEventListener("click", (event) => {
    if (event.target.id === "modalBackdrop") {
      setHidden("#modalBackdrop", true);
      state.activeModalKey = "";
    }
  });
  $("#manualTopButton").addEventListener("click", () => {
    if (!$("#manualBackdrop").classList.contains("hidden")) {
      closeManualModal();
      return;
    }
    openManualModal("", "可选人工复核：补充来源、修订结论、确认不确定项，或要求重新质检。", "global");
  });
  $("#closeManualButton").addEventListener("click", closeManualModal);
  $("#manualBackdrop").addEventListener("click", (event) => {
    if (event.target.id === "manualBackdrop") closeManualModal();
  });
  $("#manualForm").addEventListener("submit", submitManualForm);
  $("#manualSubmitButton").addEventListener("click", (event) => {
    event.preventDefault();
    submitManualForm(event);
  });
  $("#toggleReportModeButton").addEventListener("click", toggleReportMode);
  $("#cancelDeleteButton").addEventListener("click", closeDeleteDialog);
  $("#cancelDeleteTopButton").addEventListener("click", closeDeleteDialog);
  $("#confirmDeleteButton").addEventListener("click", confirmDeleteTask);
  $("#deleteBackdrop").addEventListener("click", (event) => {
    if (event.target.id === "deleteBackdrop") closeDeleteDialog();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    setHidden("#logPanel", true);
    setHidden("#sourcesPanel", true);
    setHidden("#modalBackdrop", true);
    closeManualModal();
    setHidden("#sourceMenu", true);
    setHidden("#deleteBackdrop", true);
    closeResearchPanel();
    state.activeSourcesKey = "";
    state.activeModalKey = "";
    $("#sourceMenuButton").setAttribute("aria-expanded", "false");
  });
  $("#collapsePlanButton").addEventListener("click", () => {
    setHidden("#floatingPlan", true);
    setHidden("#planChip", false);
  });
  $("#planChip").addEventListener("click", () => {
    setHidden("#floatingPlan", false);
    setHidden("#planChip", true);
  });
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = String(text || "");
  return div.innerHTML;
}

let researchActiveTab = "questionnaire";

function buildResearchContextPayload() {
  const parsed = parseTaskPrompt($("#taskPromptInput")?.value || "");
  const taskCompetitors = state.task?.competitor_names || state.task?.competitors || [];
  const competitors = state.task ? taskCompetitors : parsed.competitors;
  return {
    industry: state.task?.industry || parsed.industry || "待识别行业",
    competitors: competitors && competitors.length ? competitors : ["待补充竞品"],
    focus_areas: state.task?.focus_areas || getFocusAreas(),
    notes: state.task?.notes || parsed.raw || "",
  };
}

function defaultResearchObjective(tab) {
  const context = buildResearchContextPayload();
  const competitors = (context.competitors || []).join("、") || "目标竞品";
  if (tab === "interview") {
    return `深入了解目标用户对 ${competitors} 的真实使用场景、对比感受、痛点和切换条件`;
  }
  return `了解目标用户对 ${competitors} 的使用习惯、满意度、竞品偏好、付费意愿和核心痛点`;
}

function updateResearchContextHint() {
  const context = buildResearchContextPayload();
  const competitors = (context.competitors || []).join("、");
  const hint = state.task
    ? `已绑定当前任务：${competitors || state.task.name || "竞品分析"}。问卷生成后会保存来源并提供本地链接。`
    : `未绑定分析任务，将先生成调研草稿；创建任务后再生成问卷可获得可分享链接。`;
  $("#researchContextHint").textContent = hint;
}

async function copyText(text) {
  if (!text) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.setAttribute("readonly", "");
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.append(helper);
    helper.select();
    const copied = document.execCommand("copy");
    helper.remove();
    return copied;
  }
}

function openResearchPanel(tab = "questionnaire") {
  setHidden("#researchFloatBall", true);
  setHidden("#researchPanel", false);
  resetFloatingPosition("#researchPanel");
  $("#researchObjective").value = defaultResearchObjective(tab);
  $("#researchTargetUsers").value = "";
  $("#researchInterviewCount").value = "5";
  setHidden("#researchResult", true);
  setHidden("#researchForm", false);
  switchResearchTab(tab);
  updateResearchContextHint();
}

function closeResearchPanel() {
  setHidden("#researchPanel", true);
  setHidden("#researchFloatBall", false);
}

function switchResearchTab(tab) {
  researchActiveTab = tab;
  $("#questionnaireTabButton").classList.toggle("active", tab === "questionnaire");
  $("#interviewTabButton").classList.toggle("active", tab === "interview");
  setHidden("#researchFormExtra", false);
  setHidden("#researchInterviewCountWrap", tab === "questionnaire");
  setHidden("#researchResult", true);
  setHidden("#researchForm", false);
  $("#researchObjective").value = defaultResearchObjective(tab);
  $("#researchTargetUsers").value = "";
  const btn = $("#researchSubmitButton");
  btn.textContent = tab === "questionnaire" ? "生成问卷" : "生成访谈提纲";
  const placeholder = tab === "questionnaire"
    ? "描述调研目标，系统将自动生成对应问卷。\n例如：了解开发者对 Copilot 和 Cursor 的功能偏好与切换意愿"
    : "描述调研目标，系统将自动生成访谈提纲。\n例如：深入了解中小团队在选型协同工具时的决策因素和痛点";
  $("#researchObjective").placeholder = placeholder;
  updateResearchContextHint();
}

async function submitResearchForm(event) {
  event.preventDefault();
  const objective = ($("#researchObjective").value || "").trim();
  if (!objective) return;
  const btn = $("#researchSubmitButton");
  const loadingText = researchActiveTab === "questionnaire" ? "生成问卷中..." : "生成提纲中...";
  await withButtonLoading(btn, loadingText, async () => {
    try {
      let result;
      const context = buildResearchContextPayload();
      if (researchActiveTab === "questionnaire") {
        const path = state.task ? `/api/tasks/${state.task.id}/questionnaire` : "/api/research/questionnaire";
        result = await api(path, {
          method: "POST",
          body: JSON.stringify({
            objective,
            target_users: ($("#researchTargetUsers").value || "").trim(),
            dimensions: context.focus_areas || null,
            ...context,
          }),
        });
        renderResearchResult(result.design, "questionnaire", result);
      } else {
        const path = state.task ? `/api/tasks/${state.task.id}/interview-guide` : "/api/research/interview-guide";
        result = await api(path, {
          method: "POST",
          body: JSON.stringify({
            objective,
            target_users: ($("#researchTargetUsers").value || "").trim(),
            interview_count: parseInt($("#researchInterviewCount").value, 10) || 5,
            ...context,
          }),
        });
        renderResearchResult(result.guide, "interview", result);
      }
      showToast(researchActiveTab === "questionnaire" ? "问卷已生成。" : "访谈提纲已生成。", "success");
    } catch (error) {
      showToast(`生成失败：${error.message}`, "error");
    }
  });
}

function renderResearchResult(data, tab, meta = {}) {
  const container = $("#researchResult");
  setHidden("#researchForm", true);
  setHidden("#researchResult", false);

  let html = "";
  if (tab === "questionnaire") {
    const shareUrl = meta.share_url || "";
    const designId = meta.design_id || "";
    const feishuUrl = meta.feishu_url || "";
    const sections = (data.sections || []).map((s) => {
      const questions = (s.questions || []).map((q) => {
        const typeLabel = { single_choice: "单选", multiple_choice: "多选", likert: "量表", open_ended: "开放" }[q.type] || q.type;
        const opts = q.options ? `<div class="q-options">${q.options.map((o) => `<span class="q-opt">${escapeHtml(String(o))}</span>`).join("")}</div>` : "";
        return `<div class="q-item">
          <div class="q-item-head"><strong>${escapeHtml(q.id || "")}</strong> <span class="badge">${typeLabel}</span> ${q.required ? '<span class="q-required">必填</span>' : ""}</div>
          <p class="q-text">${escapeHtml(q.question_text || "")}</p>
          ${opts}
        </div>`;
      }).join("");
      return `<div class="q-section"><h3>${escapeHtml(s.section_title || "")}</h3>${questions}</div>`;
    }).join("");
    html = `
      <div class="q-design-head">
        <h3>${escapeHtml(data.title || "调研问卷")}</h3>
        <p>${escapeHtml(data.description || "")}</p>
        <p class="q-meta">预计填写 ${data.estimated_time_minutes || "—"} 分钟 · ${(data.recommended_channels || []).join("、") || "线上"}</p>
        ${shareUrl ? `<div class="research-share-box">
          <a href="${escapeHtml(shareUrl)}" target="_blank" rel="noopener">打开问卷链接</a>
          <button class="ghost small" type="button" id="copyQuestionnaireLinkButton">复制链接</button>
          ${feishuUrl ? `<a href="${escapeHtml(feishuUrl)}" target="_blank" rel="noopener">打开飞书问卷</a>
          <button class="ghost small" type="button" id="copyFeishuQuestionnaireLinkButton">复制飞书链接</button>` : ""}
          ${designId && !feishuUrl ? `<button class="ghost small" type="button" id="publishFeishuQuestionnaireButton">生成飞书问卷</button>` : ""}
          <span class="research-publish-status" id="feishuPublishStatus"></span>
        </div>` : `<p class="q-meta">当前是未绑定任务的草稿。创建任务后再生成，可得到可分享问卷链接。</p>`}
      </div>
      ${sections}`;
  } else {
    const phases = (data.phases || []).map((p) => {
      const questions = (p.questions || []).map((q) =>
        `<div class="interview-q"><strong>${escapeHtml(q.id || "")}</strong><p>${escapeHtml(q.text || "")}</p>${q.probe ? `<small>追问：${escapeHtml(q.probe)}</small>` : ""}</div>`
      ).join("");
      const goals = (p.goals || []).map((g) => `<span class="q-opt">${escapeHtml(String(g))}</span>`).join("");
      return `<div class="interview-phase">
        <h3>${escapeHtml(p.phase || "")} <span class="badge">${p.duration_minutes || "—"} 分钟</span></h3>
        ${goals ? `<div class="q-options">${goals}</div>` : ""}
        ${questions}
      </div>`;
    }).join("");
    const coverage = data.dimension_coverage ? Object.entries(data.dimension_coverage).map(([k, v]) => `<span>${escapeHtml(k)}：${Array.isArray(v) ? v.join("、") : v}</span>`).join(" · ") : "";
    html = `
      <div class="q-design-head">
        <h3>${escapeHtml(data.title || "用户访谈提纲")}</h3>
        <p class="q-meta">预计 ${data.estimated_duration_minutes || "—"} 分钟 · 目标：${escapeHtml(data.target_profile || "未指定")}</p>
        ${coverage ? `<p class="q-meta">${coverage}</p>` : ""}
      </div>
      ${phases}
      ${data.notes_for_interviewer ? `<div class="interview-notes"><strong>注意事项</strong><p>${escapeHtml(data.notes_for_interviewer)}</p></div>` : ""}`;
  }
  const fallbackNote = meta.fallback_reason ? `<p class="q-meta research-fallback-note">降级说明：${escapeHtml(meta.fallback_reason)}</p>` : "";
  html += `${fallbackNote}<button class="ghost" type="button" id="researchBackButton" style="margin-top:14px;">重新输入</button>`;
  container.innerHTML = html;
  const copyButton = $("#copyQuestionnaireLinkButton");
  if (copyButton && meta.share_url) {
    copyButton.addEventListener("click", async () => {
      const copied = await copyText(meta.share_url);
      showToast(copied ? "问卷链接已复制。" : "复制失败，请手动打开链接。", copied ? "success" : "error");
    });
  }
  const copyFeishuButton = $("#copyFeishuQuestionnaireLinkButton");
  if (copyFeishuButton && meta.feishu_url) {
    copyFeishuButton.addEventListener("click", async () => {
      const copied = await copyText(meta.feishu_url);
      showToast(copied ? "飞书问卷链接已复制。" : "复制失败，请手动打开链接。", copied ? "success" : "error");
    });
  }
  const publishFeishuButton = $("#publishFeishuQuestionnaireButton");
  if (publishFeishuButton && meta.design_id) {
    publishFeishuButton.addEventListener("click", async () => {
      const statusNode = $("#feishuPublishStatus");
      await withButtonLoading(publishFeishuButton, "生成飞书中...", async () => {
        try {
          const result = await api(`/api/questionnaires/${encodeURIComponent(meta.design_id)}/publish/feishu`, {
            method: "POST",
            body: JSON.stringify({}),
          });
          renderResearchResult(data, tab, { ...meta, feishu_url: result.feishu_url, feishu_publish: result });
          showToast(result.reused ? "已返回已有飞书问卷。" : "飞书问卷已生成。", "success");
        } catch (error) {
          if (statusNode) statusNode.textContent = "飞书发布失败，本地链接仍可使用。";
          showToast(`飞书发布失败：${error.message}`, "error");
        }
      });
    });
  }
  $("#researchBackButton").addEventListener("click", () => {
    setHidden("#researchResult", true);
    setHidden("#researchForm", false);
  });
}

function initFloatBallDrag() {
  const ball = $("#researchFloatBall");
  if (!ball) return;
  let dragging = false, startX = 0, startY = 0, ballX = 0, ballY = 0, moved = false;

  ball.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    dragging = true;
    moved = false;
    startX = e.clientX;
    startY = e.clientY;
    const rect = ball.getBoundingClientRect();
    ballX = rect.left;
    ballY = rect.top;
    ball.classList.add("dragging");
    document.body.style.userSelect = "none";
    e.preventDefault();
  });

  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true;
    const size = ball.getBoundingClientRect().width;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    ball.style.left = `${Math.max(0, Math.min(vw - size, ballX + dx))}px`;
    ball.style.top = `${Math.max(0, Math.min(vh - size, ballY + dy))}px`;
    ball.style.right = "auto";
    ball.style.bottom = "auto";
  });

  document.addEventListener("mouseup", () => {
    dragging = false;
    ball.classList.remove("dragging");
    document.body.style.userSelect = "";
  });

  ball.addEventListener("click", (e) => {
    if (moved) { e.stopPropagation(); e.preventDefault(); }
  }, true);
}

document.addEventListener("DOMContentLoaded", () => {
  initPlanWidget();
  wireEvents();
  initContextMenu();
  resizePromptInput();
  updatePromptFeedback();
  updateSourceModeStatus();
  loadHistory();
  initDragging("#floatingPlan", ".drag-handle");
  initDragging("#logPanel", ".panel-drag");
  initDragging("#sourcesPanel", ".panel-drag");
  initDragging("#researchPanel", ".research-panel-drag");
  initFloatBallDrag();
});
