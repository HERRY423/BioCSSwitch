import {
  CAP,
  classifyWorkflowPackResult,
  isNativeAdapter,
  modelCapability,
  openaiCustomAnthropicBaseMessage,
  sourceHint,
  workflowLaunchBlocker,
} from "./ui-logic.js";

// BioCSSwitch 桌面面板前端。只调用后端 Tauri command，绝不碰任何密钥落盘逻辑。
// 后端只把 key 的【掩码】回显给这里；完整 key 永不进前端。
//
// ── Tauri 参数键约定（务必遵守）──────────────────────────────────────────────
// 本项目所有命令都是裸 `#[tauri::command]`（无 rename_all）。tauri-macros 默认
// `ArgumentCase::Camel`，会把 Rust 蛇形【顶层参数名】转成 lowerCamelCase 交给 JS：
//   template_id→templateId、base_url→baseUrl、api_format→apiFormat、skip_verify→skipVerify。
// 所以 invoke 顶层 args 用【小驼峰】。而 serde 结构体入参（`req`=FetchModelsReq、
// `cfg`=UiSettings）内部字段按结构体字段名（蛇形）：proxy_port/sandbox_port、
// template_id/base_url/key/profile_id。核对表见任务报告。
//
// 预览兜底：在普通浏览器（没有 Tauri 后端）里打开时用 mockInvoke 返回假数据，
// 让界面能完整渲染。真实 app 里 window.__TAURI__ 存在，走真后端，此兜底不生效。
const PREVIEW = !window.__TAURI__;
const invoke = PREVIEW
  ? (cmd, args) => mockInvoke(cmd, args)
  : window.__TAURI__.core.invoke;

// ── 预览兜底 mock（仅浏览器预览用；node --check 只验语法，真实 app 走真后端） ──
const MOCK_TEMPLATES = [
  { id: "deepseek", name: "DeepSeek", category: "cn_official", api_format: "anthropic", adapter: "deepseek", base_url: "https://api.deepseek.com/anthropic", base_url_editable: false, requires_model_override: false, builtin_models: ["claude-opus-4-8", "claude-haiku-4-5"], icon: "deepseek", icon_color: "#1E88E5", website_url: "https://platform.deepseek.com" },
  { id: "glm", name: "智谱 GLM", category: "cn_official", api_format: "anthropic", adapter: "relay", base_url: "https://open.bigmodel.cn/api/anthropic", base_url_editable: true, requires_model_override: true, builtin_models: ["glm-5.2", "glm-4.7", "glm-4.6", "glm-4.5-air"], icon: "glm", icon_color: "#2E6BE6", website_url: "https://open.bigmodel.cn" },
  { id: "xiaomi", name: "小米 MiMo", category: "cn_official", api_format: "anthropic", adapter: "relay", base_url: "https://api.xiaomimimo.com/anthropic", base_url_editable: true, requires_model_override: true, builtin_models: ["mimo-v2.5-pro"], icon: "xiaomi", icon_color: "#FF6900", website_url: "https://xiaomimimo.com" },
  { id: "siliconflow", name: "硅基流动", category: "cn_official", api_format: "anthropic", adapter: "relay", base_url: "https://api.siliconflow.cn", base_url_editable: true, requires_model_override: true, builtin_models: ["deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4-Flash", "deepseek-ai/DeepSeek-V3.2", "zai-org/GLM-5.2"], icon: "siliconflow", icon_color: "#7C3AED", website_url: "https://siliconflow.cn" },
  { id: "kimi", name: "Kimi（Moonshot）", category: "cn_official", api_format: "anthropic", adapter: "relay", base_url: "https://api.moonshot.cn/anthropic", base_url_editable: true, requires_model_override: true, builtin_models: ["kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"], icon: "kimi", icon_color: "#16182F", website_url: "https://platform.moonshot.cn" },
  { id: "minimax", name: "MiniMax", category: "cn_official", api_format: "anthropic", adapter: "relay", base_url: "https://api.minimaxi.com/anthropic", base_url_editable: true, requires_model_override: true, builtin_models: ["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"], icon: "minimax", icon_color: "#E1341E", website_url: "https://platform.minimaxi.com" },
  { id: "openrouter", name: "OpenRouter", category: "custom", api_format: "anthropic", adapter: "relay", base_url: "https://openrouter.ai/api", base_url_editable: true, requires_model_override: true, builtin_models: ["anthropic/claude-sonnet-5", "anthropic/claude-opus-4.8", "anthropic/claude-opus-4.8-fast"], icon: "openrouter", icon_color: "#6467F2", website_url: "https://openrouter.ai" },
  { id: "qwen", name: "通义千问", category: "cn_official", api_format: "openai_chat", adapter: "qwen", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", base_url_editable: false, requires_model_override: false, builtin_models: ["qwen-max", "qwen-plus", "qwen-turbo"], icon: "qwen", icon_color: "#615CED", website_url: "https://dashscope.aliyun.com" },
  { id: "custom-openai", name: "自定义 OpenAI", category: "custom", api_format: "openai_chat", adapter: "openai-custom", base_url: "", base_url_editable: true, requires_model_override: true, builtin_models: [], icon: "custom", icon_color: "#2563EB", website_url: "" },
  { id: "custom-openai-responses", name: "自定义 OpenAI Responses", category: "custom", api_format: "openai_responses", adapter: "openai-responses", base_url: "", base_url_editable: true, requires_model_override: true, builtin_models: [], icon: "custom", icon_color: "#0F766E", website_url: "" },
  { id: "custom", name: "自定义 Anthropic", category: "custom", api_format: "anthropic", adapter: "relay", base_url: "", base_url_editable: true, requires_model_override: true, builtin_models: [], icon: "custom", icon_color: "#6B7280", website_url: "" },
];
const mockStore = {
  schema_version: 2,
  active_id: "",
  proxy_port: 18991,
  sandbox_port: 8990,
  mode: "proxy",
  agent_mode: "normal",
  enabled_packs: {},
  profiles: [
    { id: "p-demo1", name: "我的 GLM", template_id: "glm", category: "cn_official", api_format: "anthropic", base_url: "https://open.bigmodel.cn/api/anthropic", model: "glm-4.6", key: "••••••1234", icon: "glm", icon_color: "#2E6BE6", website_url: "https://open.bigmodel.cn", sort_index: 1, notes: "" },
  ],
};
function mockMask(k) { return k ? "••••" + String(k).slice(-4) : ""; }
function mockInvoke(cmd, args) {
  args = args || {};
  switch (cmd) {
    case "get_config":
      return Promise.resolve({
        schema_version: mockStore.schema_version, active_id: mockStore.active_id,
        proxy_port: mockStore.proxy_port, sandbox_port: mockStore.sandbox_port,
        mode: mockStore.mode, agent_mode: mockStore.agent_mode, templates: MOCK_TEMPLATES,
        profiles: mockStore.profiles.map((p) => ({ ...p })),
      });
    case "list_templates":
      return Promise.resolve(MOCK_TEMPLATES);
    case "create_profile": {
      const t = MOCK_TEMPLATES.find((x) => x.id === args.templateId) || {};
      const id = "p-" + Math.random().toString(16).slice(2, 10);
      mockStore.profiles.push({
        id, name: args.name || t.name || "新配置", template_id: args.templateId,
        category: t.category || "custom", api_format: t.api_format || "anthropic",
        base_url: args.baseUrl || t.base_url || "", model: args.model || "",
        key: mockMask(args.key || ""), icon: t.icon, icon_color: t.icon_color,
        website_url: t.website_url, sort_index: mockStore.profiles.length + 1, notes: "",
      });
      return Promise.resolve(id);
    }
    case "update_profile_metadata": {
      const p = mockStore.profiles.find((x) => x.id === args.id);
      if (!p) return Promise.reject("找不到 profile：" + args.id);
      p.name = args.name; p.notes = args.notes || "";
      return Promise.resolve(null);
    }
    case "update_profile_connection": {
      const p = mockStore.profiles.find((x) => x.id === args.id);
      if (!p) return Promise.reject("找不到 profile：" + args.id);
      if (args.baseUrl != null) p.base_url = args.baseUrl;
      if (args.model != null) p.model = args.model;
      if (args.key) p.key = mockMask(args.key);
      return Promise.resolve({ validated: true });
    }
    case "clear_profile_key": {
      const p = mockStore.profiles.find((x) => x.id === args.id);
      if (p) p.key = "";
      return Promise.resolve(null);
    }
    case "delete_profile":
      mockStore.profiles = mockStore.profiles.filter((x) => x.id !== args.id);
      if (mockStore.active_id === args.id) mockStore.active_id = "";
      return Promise.resolve(null);
    case "set_active_profile": {
      const p = mockStore.profiles.find((x) => x.id === args.id);
      if (!p) return Promise.reject("找不到 profile：" + args.id);
      mockStore.active_id = args.id;
      return Promise.resolve({ committed: true, active_id: args.id, hint: "（预览：已设为当前）" });
    }
    case "fetch_models":
      return Promise.resolve({ models: [{ id: "glm-4.6", supports_tools: true }, { id: "glm-5", supports_tools: null }], source: "live", error_kind: null, upstream_status: 200 });
    case "set_settings":
      if (args.cfg) { mockStore.proxy_port = args.cfg.proxy_port; mockStore.sandbox_port = args.cfg.sandbox_port; }
      return Promise.resolve(null);
    case "set_mode":
      mockStore.mode = args.mode;
      return Promise.resolve(null);
    case "set_agent_mode":
      mockStore.agent_mode = args.mode;
      return Promise.resolve(null);
    case "one_click_login":
      return Promise.resolve({ url: "http://127.0.0.1:8990", msg: "（预览模式：假装已就绪）", action: "started" });
    case "status":
      return Promise.resolve({ proxy: "amber", sandbox: "amber", upstream: "amber" });
    case "app_version":
      return Promise.resolve("0.0.0-preview");
    case "check_updates":
      return Promise.resolve({
        ok: true,
        current_version: "0.0.0-preview",
        latest_version: "0.0.0-preview",
        latest_tag: "v0.0.0-preview",
        release_url: "https://github.com/HERRY423/BioCSSwitch/releases/latest",
        update_available: false,
      });
    case "run_doctor":
      return Promise.resolve("（预览模式：后端未运行，这里是占位文本）");
    case "list_packs":
      return Promise.resolve({
        packs: [
          { id: "bio-lit", name: "生物医学文献检索", description: "PubMed / Europe PMC / Crossref / bioRxiv / medRxiv", optional_env: [{ name: "NCBI_API_KEY", label: "NCBI API Key（可选）" }], requires_env: [], dependencies: [], requires_tools: [] },
          { id: "bio-audit", name: "证据链与引用审计", description: "PMID/DOI/NCT 校验 + 证据类型 + Skill 强制规范", optional_env: [], requires_env: [], dependencies: ["bio-lit"], depends_on: ["bio-lit"], requires_tools: [] },
          { id: "bio-mcp-shim", name: "远程 MCP 本地替身", description: "让 Science 仍看到 pubmed / clinical-trials / chembl / biorxiv 同名 MCP，实际走本地", optional_env: [], requires_env: [], dependencies: ["bio-lit", "bio-trials", "bio-drug"], requires_tools: [] },
          { id: "bio-norm", name: "实体标准化 / 消歧", description: "HGNC / MeSH / MONDO / HPO / GO / ChEBI + disambiguate", optional_env: [], requires_env: [], dependencies: [], requires_tools: [] },
          { id: "bio-privacy", name: "隐私 / 合规模式", description: "PHI 扫描 + 脱敏 + 审计日志 + Skill 强制", optional_env: [], requires_env: [], dependencies: [], requires_tools: [] },
          { id: "bio-workflows", name: "科研工作流模板 Skills", description: "综述 / 靶点 / GEO / 试验 / rebuttal / grant aims", optional_env: [], requires_env: [], dependencies: ["bio-lit", "bio-audit"], requires_tools: [] },
          { id: "bio-ml", name: "机器学习突破板块", description: "多模态 FM / 虚拟细胞 / AI 药物发现 / 联邦学习 / 临床验证门", optional_env: [], requires_env: [], dependencies: ["bio-audit", "bio-privacy"], depends_on: ["bio-audit", "bio-privacy"], requires_tools: [] },
          { id: "bio-research-partner", name: "主动研究伙伴", description: "本地 HMAC 兴趣模型 / 主动简报 / 工作流预测 / 可删除", optional_env: [{ name: "BIOCSSWITCH_INTEREST_PROFILE_PATH", label: "本地研究画像路径（可选）" }], requires_env: [], dependencies: ["bio-lit", "bio-trials", "bio-kg", "bio-privacy"], depends_on: ["bio-lit", "bio-trials", "bio-kg", "bio-privacy"], requires_tools: [] },
          { id: "bio-crossmodal", name: "跨模态生物医学发现", description: "文献 / 基因 / 药物 / 试验 / 单细胞 / 空间证据统一编排", optional_env: [], requires_env: [], dependencies: ["bio-lit", "bio-audit", "bio-gene", "bio-drug", "bio-trials", "bio-singlecell", "bio-sc-downstream", "bio-spatial", "bio-kg"], requires_tools: [] },
        ],
        enabled: { ...mockStore.enabled_packs }, env_set: {}, mode: mockStore.mode,
        sensitive_mode: false, local_endpoint_hosts: [], current_upstream_host: "api.deepseek.com",
      });
    case "toggle_pack":
      mockStore.enabled_packs[args.id] = !!args.enabled;
      return Promise.resolve({
        ok: true,
        applied: Object.entries(mockStore.enabled_packs).filter(([, on]) => on).map(([id]) => id),
        warnings: [],
        sandbox_restarted: false,
      });
    case "set_pack_env":
      return Promise.resolve({ ok: true, set: !!(args && args.value) });
    case "set_sensitive_mode":
      return Promise.resolve({ ok: true, sensitive_mode: !!(args && args.enabled), sandbox_stopped: false, suggest_enable_bio_privacy: !!(args && args.enabled) });
    case "set_local_endpoint_hosts": {
      const hs = (args && args.hosts) || [];
      const pub = hs.filter((h) => !/^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(h));
      if (pub.length && !(args && args.confirmPublic)) {
        return Promise.resolve({ ok: false, needs_confirm: pub, invalid: [],
          hint: "（预览）公网域名需确认" });
      }
      return Promise.resolve({ ok: true, hosts: hs, invalid: [] });
    }
    case "list_biomed_tasks":
      return Promise.resolve({
        tasks: [
          { id: "research-partner", label: "主动研究伙伴", hint: "隐私优先的本地兴趣建模与个性化简报" },
          { id: "hypothesis-generation", label: "矛盾驱动假设生成", hint: "竞争假设 / 区分性实验 / 关键数据需求" },
          { id: "crossmodal-discovery", label: "跨模态靶点发现", hint: "六类证据统一上下文；工具调用 + 长上下文 + JSON" },
          { id: "lit-review", label: "文献综述", hint: "长上下文优先" },
          { id: "clinical-trials", label: "临床试验检索", hint: "工具调用密集" },
          { id: "target-discovery", label: "靶点发现 / 老药新用", hint: "多源组合查询" },
          { id: "tool-heavy", label: "工具调用密集任务", hint: "tool_use 稳定性优先" },
          { id: "evidence-check", label: "引用 / 证据审计", hint: "JSON 稳定性优先" },
        ],
        routes: {}, active_id: mockStore.active_id, probes: {},
      });
    case "set_task_route":
      return Promise.resolve(null);
    case "run_probes":
      return Promise.resolve({
        ok: true,
        results: {
          tool_use: { verdict: "ok", reason: "（预览）", upstream_status: 200 },
          long_ctx: { verdict: "ok", reason: "（预览）", upstream_status: 200 },
          json_stable: { verdict: "degraded", reason: "（预览）", upstream_status: 200 },
        },
      });
    case "start_smoke_verification":
      return Promise.resolve({ marker: "0123abcd" + Math.random().toString(16).slice(2, 10),
        next_step: "（预览）请重启沙箱后点检查" });
    case "poll_smoke_verification":
      return Promise.resolve({ verdict: "mcp_path_pending", reason: "（预览）尚未跑真实探测", marker: "" });
    case "confirm_skill_verified":
      return Promise.resolve({ verdict: args && args.userConfirmed ? "skill_path_ok" : "skill_path_fail",
        reason: "（预览）用户手动确认" });
    case "cleanup_smoke_verification":
      return Promise.resolve(null);
    default:
      return Promise.resolve(null);
  }
}

const $ = (id) => document.getElementById(id);
const els = {};
let statusTimer = null;
let updateTimer = null;
let busy = false;
let mode = "proxy"; // "proxy" 第三方 | "official" 官方
// 当前配置快照（get_config 结果）。全 key 绝不在此，只有掩码。
let state = { profiles: [], templates: [], active_id: "", proxy_port: 18991, sandbox_port: 8990, agent_mode: "normal" };
let pendingSkipActivateId = null;   // set_active 校验含糊时，允许「跳过验证」再切
let pendingConfirm = null;          // 危险操作（清 key / 删除）的「再点一次确认」态
let lastUpdateCheck = null;
const UPDATE_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

const CAT_LABELS = { official: "官方", cn_official: "国内", custom: "自定义" };

const MODEL_HINT = {
  native: "由 Science 选择器 + 内置映射自动选择（opus 深度 / haiku 快速）。",
  follow: "留空＝跟随 Science 选择器（保留 opus/haiku 各档）；选一个＝固定用于所有请求。",
  fixed: "该来源需选一个模型（不认 claude-*，将用于所有请求含后台任务）。",
};

// 据能力渲染模型字段。native：只读信息 + 隐藏下拉/获取按钮，但把既有 model 留在隐藏下拉里
// （避免保存时被空值覆盖，守「零运行语义变化」）；relay：走下拉。
function applyModelCapability(t, ui, currentModel) {
  const cap = modelCapability(t);
  const listId = ui.sel.getAttribute("list");
  const dl = listId && document.getElementById(listId);
  if (cap === CAP.NATIVE) {
    // native：控件隐藏，保留 profile 既有 model（connSave/wizSave 读回原值不清空），不写回任何默认/壳。
    ui.info.textContent = MODEL_HINT.native;
    ui.info.hidden = false;
    ui.sel.hidden = true;
    ui.sel.value = currentModel || "";
    clearChildren(dl);
    if (ui.fetchBtn) ui.fetchBtn.hidden = true;
    ui.hint.textContent = "";
    return cap;
  }
  // relay（FIXED）：input + datalist 候选（内置精选 + 可自填）；预填旗舰默认或既有值。
  ui.info.hidden = true;
  ui.sel.hidden = false;
  if (ui.fetchBtn) ui.fetchBtn.hidden = false;
  const builtin = ((t && t.builtin_models) || []).slice();
  if (currentModel && !builtin.includes(currentModel)) builtin.unshift(currentModel);
  const models = builtin.map((id) => ({ id, supports_tools: null }));
  renderModelOptions(ui.sel, models, "内置");
  ui.sel.value = currentModel || (builtin[0] || "");
  ui.hint.textContent = MODEL_HINT.fixed;
  return cap;
}

function setMsg(text, kind) {
  // 去掉常驻「就绪。」：空消息或纯 idle 时整条反馈栏不占位，有真实反馈（结果/错误/自检）才冒出来。
  const t = text && text !== "就绪。" ? text : "";
  els.msg.textContent = t;
  els.msg.className = "msg" + (kind ? " " + kind : "");
  els.msg.parentElement.hidden = !t;
  // 表单视图里反馈区可能落在折叠线以下：给出结果（ok/err）时滚到可见；
  // 中性提示（无 kind，多为打开表单时）不滚，避免把页面拽到底部。
  if (t && kind && els.panel && els.panel.classList.contains("view-form")) {
    els.msg.scrollIntoView({ block: "nearest" });
  }
}

function setLight(el, s) {
  if (!el) return;
  const cls = { green: "g", amber: "a", red: "r" }[s] || "a";
  el.className = "lt " + cls;
  el.dataset.state = s || "amber";
}

function setRuntimeText(el, s) {
  if (!el) return;
  el.textContent = ({ green: "在线", amber: "待命", red: "异常" })[s] || "未知";
}

function setBusy(on) {
  busy = on;
  [
    els.oneClickBtn, els.stopBtn, els.newBtn,
    els.wizSaveBtn, els.wizFetchBtn, els.wizCancelBtn,
    els.connSaveBtn, els.connFetchBtn, els.connClearBtn, els.connCancelBtn,
    els.metaSaveBtn, els.metaCancelBtn, els.skipActivateBtn,
    // 端口输入也纳入忙碌禁用：忙碌中改端口会与在途操作竞态（修 P1-c 前端侧）。
    els.proxyPort, els.sandboxPort, els.settingsBtn,
  ].forEach((b) => b && (b.disabled = on));
  document.querySelectorAll("[data-workflow]").forEach((b) => (b.disabled = on));
  // 模式切换按钮同样禁用：忙碌中切官方会与「一键开始」竞态（修 P1-b 前端侧）。
  if (els.modeSeg) els.modeSeg.querySelectorAll(".seg-btn").forEach((b) => (b.disabled = on));
  // 松开忙碌时，把 requires_model_override 的保存门控交回门（避免 setBusy(false) 覆盖门控）。
  if (!on) { refreshWizGate(); refreshConnGate(); }
}

async function call(cmd, args) {
  return await invoke(cmd, args);
}

function clearChildren(el) {
  if (el) el.replaceChildren();
}

function textEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = String(text ?? "");
  return el;
}

function appendText(el, text) {
  el.appendChild(document.createTextNode(String(text ?? "")));
  return el;
}

function setSafeBackground(el, color) {
  const c = String(color || "").trim();
  if (/^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.test(c)) {
    el.style.background = c;
  }
}

function applySafeExternalLink(a, url) {
  try {
    const u = new URL(String(url || ""));
    if (u.protocol !== "https:" && u.protocol !== "http:") return false;
    a.href = u.href;
    a.target = "_blank";
    a.rel = "noreferrer";
    return true;
  } catch (_) {
    return false;
  }
}

function tplById(id) {
  return (state.templates || []).find((t) => t.id === id) || null;
}

// ── 视图切换：列表 / 新建向导 / 连接编辑 / 改名。一次只显示一个表单（列表隐去减少高度）。──
function showView(v) {
  els.listSec.hidden = v !== "list";
  els.advSec.hidden = v !== "list";
  els.wizSec.hidden = v !== "wizard";
  els.connSec.hidden = v !== "conn";
  els.metaSec.hidden = v !== "meta";
  els.panel.classList.toggle("view-form", v !== "list");
  if (v === "list") hideSkip();
}
function cancelForm() { showView("list"); setMsg("就绪。"); }

function showSkip() { els.skipActivateBtn.hidden = false; }
function hideSkip() { els.skipActivateBtn.hidden = true; pendingSkipActivateId = null; }

// 危险操作「再点一次确认」（避免依赖 window.confirm，Tauri webview 里不可靠）。
function confirmAction(token, promptText, fn) {
  if (pendingConfirm && pendingConfirm.token === token) {
    clearTimeout(pendingConfirm.timer);
    pendingConfirm = null;
    fn();
    return;
  }
  if (pendingConfirm) clearTimeout(pendingConfirm.timer);
  pendingConfirm = {
    token,
    timer: setTimeout(() => { pendingConfirm = null; setMsg("已取消。"); }, 4000),
  };
  setMsg(promptText + " —— 再点一次同一按钮确认（4 秒内）。", "err");
}

// ── 加载配置 + 渲染列表 ──
async function loadConfig() {
  try {
    const cfg = await call("get_config");
    state.profiles = cfg.profiles || [];
    state.templates = cfg.templates || [];
    state.active_id = cfg.active_id || "";
    state.proxy_port = cfg.proxy_port ?? 18991;
    state.sandbox_port = cfg.sandbox_port ?? 8990;
    state.agent_mode = cfg.agent_mode || "normal";
    els.proxyPort.value = state.proxy_port;
    els.sandboxPort.value = state.sandbox_port;
    applyMode(cfg.mode === "official" ? "official" : "proxy");
    renderList();
    showView("list");
    // 一次性迁移提示（#9 甲）：后端 get_config 读后已清盘，只会出现一次。
    if (cfg.pending_notice) setMsg(cfg.pending_notice, "ok");
  } catch (e) {
    setMsg("读取配置失败：" + e, "err");
  }
}

// 列表里模型摘要：无显式 model 时按三能力给准确措辞（native 内置映射 / relay 跟随 / 需指定），
// 取代旧「（透传）」字样（三能力语义下不再有「透传」）。
function modelSummary(p) {
  if (p.model) return String(p.model);
  const cap = modelCapability(tplById(p.template_id));
  if (cap === CAP.NATIVE) return "内置映射";
  if (cap === CAP.FOLLOW) return "跟随 Science";
  return "未选模型";
}

function renderList() {
  const list = els.profileList;
  const ps = state.profiles || [];
  clearChildren(list);
  if (!ps.length) {
    list.appendChild(textEl("div", "empty", "还没有配置。点右上「＋ 新建」加一条第三方来源。"));
    return;
  }
  for (const p of ps) {
    const active = p.id === state.active_id;
    const catLabel = CAT_LABELS[p.category] || p.category || "";
    const keyMask = p.key ? String(p.key) : "未填 key";
    const modelTxt = modelSummary(p);
    const row = document.createElement("div");
    row.className = "prow" + (active ? " pactive" : "");
    row.dataset.id = String(p.id ?? "");

    const top = document.createElement("div");
    top.className = "prow-top";
    const dot = document.createElement("span");
    dot.className = "pico";
    setSafeBackground(dot, p.icon_color);
    top.appendChild(dot);
    top.appendChild(textEl("span", "pname", p.name));
    top.appendChild(textEl("span", "badge", catLabel));
    if (active) top.appendChild(textEl("span", "badge on", "当前生效"));

    row.appendChild(top);
    row.appendChild(textEl("div", "pmeta", p.base_url || "（未填地址）"));
    row.appendChild(textEl("div", "pmeta", `模型：${modelTxt} · Key：${keyMask}`));

    const acts = document.createElement("div");
    acts.className = "prow-acts";
    const addAction = (act, label, className = "abtn") => {
      const btn = document.createElement("button");
      btn.className = className;
      btn.dataset.act = act;
      btn.textContent = label;
      acts.appendChild(btn);
    };
    if (!active) addAction("activate", "设为当前", "abtn prim");
    addAction("editconn", "编辑连接");
    addAction("editmeta", "改名");
    addAction("clearkey", "清 key");
    addAction("delete", "删除", "abtn danger");
    row.appendChild(acts);
    list.appendChild(row);
  }
}

// ── 模式（第三方 / 官方）──
function applyMode(m) {
  mode = m === "official" ? "official" : "proxy";
  els.panel.classList.toggle("mode-official", mode === "official");
  els.modeSeg.querySelectorAll(".seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === mode)
  );
  els.oneClickBtn.textContent =
    mode === "official" ? "打开官方 Claude Science ↗" : "⚡ 一键开始";
  syncResearchContext();
}

async function switchMode(m) {
  if (m === mode) return;
  if (busy) return; // 忙碌中不切模式（防与「一键开始」竞态；按钮亦已禁用，此为双保险）。修 P1-b
  setBusy(true);
  try {
    await call("set_mode", { mode: m });
  } catch (e) {
    setMsg("切换模式失败：" + e, "err");
    setBusy(false);
    return;
  }
  applyMode(m);
  setBusy(false);
  showView("list");
  setMsg(
    mode === "official"
      ? "已切到官方模式：第三方代理/沙箱已停，点上方按钮打开你真实的 Claude Science。"
      : "已切到第三方模式：选一条配置「设为当前」后点「一键开始」。"
  );
  await refreshStatus();
}

async function openOfficial() {
  setBusy(true);
  setMsg("正在打开官方 Claude Science…");
  try {
    await call("open_official");
    setMsg("已打开官方 Claude Science（走你自己的官方登录与订阅）。", "ok");
  } catch (e) {
    setMsg("打开失败：" + e, "err");
  } finally {
    setBusy(false);
  }
}

// hero 按钮按当前模式分派。
async function heroClick() {
  if (mode === "official") await openOfficial();
  else await oneClick();
}

// ── 端口设置（替旧 set_config；纯端口，不含 provider/连接）──
async function persistPorts() {
  if (busy) return; // 忙碌中不改端口（防与在途操作竞态；输入亦已禁用，此为双保险）。修 P1-c
  const p = parseInt(els.proxyPort.value, 10) || 18991;
  const s = parseInt(els.sandboxPort.value, 10) || 8990;
  const changed = p !== state.proxy_port || s !== state.sandbox_port;
  // 本次端口提交全程置忙：仅靠开头的 `if (busy) return` 只挡「已在忙时进入」，挡不住本函数在途
  // 时其它操作（切模式/一键/连接编辑）启动。置忙 + 禁用控件才能保证操作顺序符合用户预期。修 GPT 三轮 P2
  setBusy(true);
  try {
    await call("set_settings", { cfg: { proxy_port: p, sandbox_port: s } });
    state.proxy_port = p;
    state.sandbox_port = s;
    // 后端在端口变化时会拆掉旧代理/沙箱（否则会复用指向旧端口的死链路），如实告知需重开。修 P1-c
    if (changed) {
      setMsg("端口已保存。改端口会重置正在运行的代理/沙箱，请重新「一键开始」。", "ok");
      await refreshStatus();
    }
  } catch (e) {
    // 出错＝端口未落盘（校验不过 / 停旧沙箱失败）：把输入框还原成实际生效值，避免显示未保存的数字。
    els.proxyPort.value = state.proxy_port;
    els.sandboxPort.value = state.sandbox_port;
    setMsg(String(e), "err");
  } finally {
    setBusy(false);
  }
}

// ── 模型下拉渲染（requires_override=false 时首项「跟随 Science 选择器」；按 supports_tools 标注）──
// 候选填进 input 关联的 <datalist>（下拉建议）；input 的值由调用方另设，用户可自由改。
function renderModelOptions(sel, models, sourceLabel) {
  const listId = sel.getAttribute("list");
  const dl = listId && document.getElementById(listId);
  if (!dl) return;
  clearChildren(dl);
  for (const m of models || []) {
    const o = document.createElement("option");
    o.value = m.id;
    const tag = m.supports_tools === true ? " ·工具✓" : m.supports_tools === false ? " ·无工具" : "";
    const src = sourceLabel ? " [" + sourceLabel + "]" : "";
    o.label = m.id + tag + src;
    dl.appendChild(o);
  }
}

// fetch_models 返回体 → 刷新 datalist 候选 + 提示（向导与连接编辑共用）。
// requiresOverride 保留形参（调用点仍传），但 datalist 无「跟随」空项，故此处不用。
function applyFetchResult(sel, requiresOverride, r) {
  void requiresOverride;
  const models = (r && r.models) || [];
  const src = r && r.source;
  // unsupported（端点不提供发现，4xx）与 builtin（200 但空）都铺内置，标「内置」；network/未知标「未验证」。
  const srcLabel = src === "live" ? "实时" : src === "builtin" || src === "unsupported" ? "内置" : "未验证";
  const prev = sel.value;
  renderModelOptions(sel, models, srcLabel);
  if (prev) sel.value = prev; // 保留用户已填/已选值，拉列表只刷新候选、绝不清空输入
  if (src === "unsupported") {
    // 端点未提供 /v1/models（如 Kimi）：内置模型可直接选，绝不表述成 key 无效。
    setMsg("该端点未提供模型列表，已用内置模型（可直接选择保存）。", "ok");
  } else if (r && r.error_kind === "network") {
    setMsg("未能连上上游验证，已铺内置模型（标「未验证」）。可仍试保存或重试。", "err");
  } else {
    setMsg("已获取 " + models.length + " 个模型（工具✓ 优先）。", "ok");
  }
}

// ── C2：新建向导 ──
function openWizard() {
  hideSkip();
  renderTemplateChips();
  const first = (state.templates || [])[0];
  selectWizTemplate(first ? first.id : "");
  showView("wizard");
  setMsg("选择来源，填 key 即可创建。");
}

function renderTemplateChips() {
  clearChildren(els.wizTemplateChips);
  for (const t of state.templates || []) {
    const cat = CAT_LABELS[t.category] || t.category || "";
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.setAttribute("aria-pressed", "false");
    chip.dataset.tid = String(t.id ?? "");
    const dot = document.createElement("span");
    dot.className = "chip-dot";
    setSafeBackground(dot, t.icon_color);
    chip.appendChild(dot);
    chip.appendChild(textEl("span", "chip-name", t.name));
    chip.appendChild(textEl("span", "chip-cat", cat));
    els.wizTemplateChips.appendChild(chip);
  }
}

function selectWizTemplate(id) {
  els.wizTemplate.value = id;
  els.wizTemplateChips.querySelectorAll(".chip").forEach((c) => {
    const on = c.getAttribute("data-tid") === id;
    c.classList.toggle("sel", on);
    c.setAttribute("aria-pressed", on ? "true" : "false");
  });
  onWizTemplate();
}

function onWizTemplate() {
  const t = tplById(els.wizTemplate.value);
  if (!t) return;
  els.wizName.value = t.name;
  // 把「新建不自动生效」放进顶部常驻提示（默认窗口下反馈区首屏可能在折叠线下，见 #6）。
  els.wizTplHint.textContent = sourceHint(t) + " 新建后需在列表点「设为当前」才生效。";
  if (t.base_url_editable) {
    // 预设：预填官方默认地址（仍可改到套餐 / 区域端点）；真·自定义：留空 + 占位提示。
    els.wizBase.value = t.base_url || "";
    els.wizBase.readOnly = false;
    els.wizBase.placeholder = t.api_format === "openai_chat" || t.api_format === "openai_responses"
      ? "https://open.bigmodel.cn/api/paas/v4"
      : "https://your-relay/claude";
    els.wizBaseHint.textContent = t.base_url
      ? "官方默认地址，可改到 token 套餐 / 区域端点（如小米 token plan）。"
      : (t.api_format === "openai_chat"
        ? "OpenAI 兼容 base root，代理自动补 /chat/completions 与 /models。"
        : t.api_format === "openai_responses"
        ? "OpenAI 兼容 base root，代理自动补 /responses 与 /models。"
        : "自定义端点根地址（自动补 /v1/messages 与 /v1/models）。");
  } else {
    els.wizBase.value = t.base_url;
    els.wizBase.readOnly = true;
    els.wizBaseHint.textContent = "模板地址已填好（只读）。";
  }
  applyModelCapability(t, {
    info: els.wizModelInfo, sel: els.wizModel, hint: els.wizModelHint, fetchBtn: els.wizFetchBtn,
  }, "");
  refreshWizGate();
}

function refreshWizGate() {
  const t = tplById(els.wizTemplate ? els.wizTemplate.value : "");
  const need = t && t.requires_model_override;
  els.wizSaveBtn.disabled = busy || !!(need && !els.wizModel.value.trim());
}

async function wizFetch() {
  const t = tplById(els.wizTemplate.value);
  if (!t) return;
  const base = t.base_url_editable ? els.wizBase.value.trim() : t.base_url;
  if (!base) { setMsg("请先填写 base_url。", "err"); return; }
  const baseErr = openaiCustomAnthropicBaseMessage(t, base);
  if (baseErr) { setMsg(baseErr, "err"); return; }
  const key = els.wizKey.value.trim();
  if (!key) { setMsg("请先填 key 再获取模型。", "err"); return; }
  setBusy(true);
  setMsg("获取模型中：起临时代理探 /v1/models…");
  try {
    const r = await call("fetch_models", { req: { template_id: t.id, base_url: base, key } });
    applyFetchResult(els.wizModel, t.requires_model_override, r);
  } catch (e) {
    setMsg("获取模型失败：" + e, "err");
  } finally {
    setBusy(false);
    refreshWizGate();
  }
}

async function wizSave() {
  const t = tplById(els.wizTemplate.value);
  if (!t) { setMsg("模板未加载。", "err"); return; }
  const name = els.wizName.value.trim() || t.name;
  const model = els.wizModel.value.trim();
  if (t.requires_model_override && !model) {
    setMsg("该来源需要选一个模型才能创建。", "err");
    return;
  }
  const args = { templateId: t.id, name, key: els.wizKey.value.trim(), model };
  if (t.base_url_editable) {
    const base = els.wizBase.value.trim();
    if (!base) { setMsg("请先填写 base_url。", "err"); return; }
    const baseErr = openaiCustomAnthropicBaseMessage(t, base);
    if (baseErr) { setMsg(baseErr, "err"); return; }
    args.baseUrl = base;
  }
  setBusy(true);
  setMsg("创建中…");
  try {
    await call("create_profile", args);
    els.wizKey.value = "";
    await loadConfig();
    setMsg("已创建「" + name + "」。可在列表点「设为当前」启用。", "ok");
  } catch (e) {
    setMsg("创建失败：" + e, "err");
  } finally {
    setBusy(false);
  }
}

// ── C3：连接编辑（base_url/model/key）+ 清 key ──
function currentConn() {
  const id = els.connSec.dataset.id;
  return (state.profiles || []).find((x) => x.id === id) || null;
}

function openConn(id) {
  const p = (state.profiles || []).find((x) => x.id === id);
  if (!p) return;
  const t = tplById(p.template_id);
  const editable = t ? t.base_url_editable : true;
  const active = id === state.active_id;
  els.connSec.dataset.id = id;
  els.connTitle.textContent = "编辑连接 · " + p.name + (active ? "（当前生效）" : "");
  els.connBase.value = p.base_url || (t ? t.base_url : "");
  els.connBase.readOnly = !editable;
  els.connBase.placeholder = t && (t.api_format === "openai_chat" || t.api_format === "openai_responses")
    ? "https://open.bigmodel.cn/api/paas/v4"
    : "https://your-relay/claude";
  // native（deepseek/qwen）隐藏「获取模型」按钮，别再提示一个不存在的操作（修 #5）。
  els.connBaseHint.textContent = editable
    ? (t && t.base_url
        ? "官方默认地址，可改到 token 套餐 / 区域端点。"
        : (t && t.api_format === "openai_chat"
          ? "OpenAI 兼容 base root，代理自动补 /chat/completions。"
          : t && t.api_format === "openai_responses"
          ? "OpenAI 兼容 base root，代理自动补 /responses。"
          : "自定义端点根地址。"))
    : (modelCapability(t) === CAP.NATIVE
        ? "模板地址（只读），模型由内置映射自动选择。"
        : "模板地址（只读）。填 key 后可「获取模型」。");
  applyModelCapability(t, {
    info: els.connModelInfo, sel: els.connModel, hint: els.connModelHint, fetchBtn: els.connFetchBtn,
  }, p.model || "");
  els.connKey.value = "";
  els.connKey.placeholder = p.key ? "已存：" + p.key + "（留空＝不改）" : "粘贴 key（只存本地）";
  showView("conn");
  refreshConnGate();
  setMsg(active
    ? "编辑当前生效配置：保存会先校验→切换，失败自动回退到原配置（不谎报生效）。"
    : "编辑连接后点「保存连接」。");
}

function refreshConnGate() {
  const p = currentConn();
  const t = p ? tplById(p.template_id) : null;
  const need = t && t.requires_model_override;
  els.connSaveBtn.disabled = busy || !!(need && !els.connModel.value.trim());
}

async function connFetch() {
  const p = currentConn();
  if (!p) return;
  const t = tplById(p.template_id);
  const editable = t ? t.base_url_editable : true;
  const base = editable ? els.connBase.value.trim() : (t ? t.base_url : els.connBase.value.trim());
  if (!base) { setMsg("请先填写 base_url。", "err"); return; }
  const baseErr = openaiCustomAnthropicBaseMessage(t, base);
  if (baseErr) { setMsg(baseErr, "err"); return; }
  setBusy(true);
  setMsg("获取模型中：起临时代理探 /v1/models…");
  try {
    const key = els.connKey.value.trim(); // 有新 key 带上；空则后端用已存 key（profileId）
    const r = await call("fetch_models", {
      req: { template_id: p.template_id, base_url: base, key, profile_id: p.id },
    });
    applyFetchResult(els.connModel, t ? t.requires_model_override : true, r);
  } catch (e) {
    setMsg("获取模型失败：" + e, "err");
  } finally {
    setBusy(false);
    refreshConnGate();
  }
}

async function connSave() {
  const p = currentConn();
  if (!p) { setMsg("配置不存在。", "err"); return; }
  const t = tplById(p.template_id);
  const req = t ? t.requires_model_override : true;
  const model = els.connModel.value.trim();
  if (req && !model) { setMsg("该来源需要选一个模型才能保存。", "err"); return; }
  const editable = t ? t.base_url_editable : true;
  const base = editable ? els.connBase.value.trim() : (t ? t.base_url : els.connBase.value.trim());
  // 可编辑地址的模板都是中转/自定义端点，必须带 base_url；清空后保存会得到不可用连接（激活必失败）。
  // 保存前就拦（后端也有同款守卫兜底，修 P2）。
  if (editable && !base) { setMsg("中转 / 自定义端点必须填写连接地址（base_url）。", "err"); return; }
  const baseErr = openaiCustomAnthropicBaseMessage(t, base);
  if (baseErr) { setMsg(baseErr, "err"); return; }
  const active = p.id === state.active_id;
  // key 留空＝不改（后端语义）；base_url/model 照传。api_format 不在此改（保留模板值）。
  const args = { id: p.id, baseUrl: base, model, key: els.connKey.value.trim() };
  setBusy(true);
  setMsg(active ? "校验中→切换中…（保存当前生效配置的新连接）" : "保存连接中…");
  try {
    const r = await call("update_profile_connection", args);
    els.connKey.value = "";
    await loadConfig();
    // 非 active：后端如实回传 validated，连不通/native 也保存，但据实说明未校验（修 P2-d truthful-save）。
    if (active) {
      setMsg("已保存并应用新连接。", "ok");
    } else if (r && r.validated) {
      setMsg("已保存连接（已通过上游校验）。", "ok");
    } else {
      setMsg("已保存连接（未能连通上游校验，激活时会再验）。", "ok");
    }
  } catch (e) {
    // 后端错误文案已如实说明回滚/代理状态（可能是「已回滚到原配置」或「回滚未成功：代理当前已停」），
    // 前端不再盲目追加「仍在用原配置运行」，避免与「代理已停」相互矛盾。修 GPT 三轮 P2
    setMsg("连接未保存：" + e, "err");
  } finally {
    setBusy(false);
    await refreshStatus();
  }
}

// 清 key（行内 / 连接表单都可触发）：二次确认后 clear_profile_key。
function clearKey(id) {
  const p = (state.profiles || []).find((x) => x.id === id);
  const nm = p ? p.name : id;
  confirmAction("clearkey:" + id, "将清除「" + nm + "」的 API key（需重填才能用）", () => doClearKey(id));
}
async function doClearKey(id) {
  const wasActive = id === state.active_id;
  setBusy(true);
  setMsg("清除 key 中…");
  try {
    await call("clear_profile_key", { id });
    await loadConfig();
    setMsg(
      wasActive
        ? "已清除 key（该配置是当前生效，链路已断，请重新填 key 再「设为当前」）。"
        : "已清除 key。",
      "ok"
    );
  } catch (e) {
    setMsg("清除失败：" + e, "err");
  } finally {
    setBusy(false);
    await refreshStatus();
  }
}

// ── C4：改名/备注 + 删除 + 设为当前 ──
function openMeta(id) {
  const p = (state.profiles || []).find((x) => x.id === id);
  if (!p) return;
  els.metaSec.dataset.id = id;
  els.metaName.value = p.name;
  els.metaNotes.value = p.notes || "";
  showView("meta");
  setMsg("改名 / 备注不影响运行中的代理。");
}
async function metaSave() {
  const id = els.metaSec.dataset.id;
  const name = els.metaName.value.trim();
  if (!name) { setMsg("名称不能为空。", "err"); return; }
  const notes = els.metaNotes.value.trim();
  setBusy(true);
  setMsg("保存中…");
  try {
    await call("update_profile_metadata", { id, name, notes });
    await loadConfig();
    setMsg("已保存。", "ok");
  } catch (e) {
    setMsg("保存失败：" + e, "err");
  } finally {
    setBusy(false);
  }
}

function del(id) {
  const p = (state.profiles || []).find((x) => x.id === id);
  const nm = p ? p.name : id;
  confirmAction("delete:" + id, "将删除配置「" + nm + "」", () => doDelete(id));
}
async function doDelete(id) {
  const wasActive = id === state.active_id;
  setBusy(true);
  setMsg("删除中…");
  try {
    await call("delete_profile", { id });
    await loadConfig();
    setMsg(
      wasActive
        ? "已删除。删掉的是当前生效配置，请重新选择一条并「设为当前」。"
        : "已删除。",
      "ok"
    );
  } catch (e) {
    setMsg("删除失败：" + e, "err");
  } finally {
    setBusy(false);
    await refreshStatus();
  }
}

// 设为当前：走后端切换事务（校验→起正式→健康才提交）。
// 返回体 committed:true=已生效；committed:false=未生效（可能可 skip）；抛错=回滚/中止。
async function activate(id, skipVerify) {
  hideSkip();
  setBusy(true);
  setMsg(skipVerify ? "跳过验证，切换中…" : "校验中→切换中…");
  try {
    const r = await call("set_active_profile", { id, skipVerify: !!skipVerify });
    if (r && r.committed) {
      await loadConfig();
      setMsg(r.hint || "已设为当前生效。", "ok");
    } else {
      await loadConfig(); // 反映未变（仍是原 active）
      setMsg((r && r.hint) || "校验未通过，未切换。", "err");
      if (r && r.can_skip) { pendingSkipActivateId = id; showSkip(); }
    }
  } catch (e) {
    await loadConfig();
    setMsg("设为当前失败：" + e, "err");
  } finally {
    setBusy(false);
    await refreshStatus();
  }
}

// ── 一键开始：读 active profile。无生效则引导先建/选一条（不再对旧 provider 槽落未提交输入）。──
async function oneClick() {
  if (!state.active_id) {
    setMsg("还没有「当前生效」的配置。请先「＋ 新建」或在列表点「设为当前」选一条，再一键开始。", "err");
    return;
  }
  setBusy(true);
  setMsg("一键开始：起代理 → 起沙箱 → 探活…");
  try {
    const r = await call("one_click_login");
    // 透传后端据实回传的 msg（已重开 / 已用新配置重启 / 沿用原对话 / 已启动 / 打开失败请手动打开）。
    setMsg((r.msg || "已就绪，正在打开面板…") + "\n" + (r.url || ""), "ok");
    await refreshStatus();
  } catch (e) {
    setMsg("一键开始失败：" + e, "err");
  } finally {
    setBusy(false);
  }
}

async function stopAll() {
  setBusy(true);
  setMsg("停止中…");
  try {
    await call("stop_all");
    setMsg("已停止代理与沙箱。", "ok");
    await refreshStatus();
  } catch (e) {
    setMsg("停止失败：" + e, "err");
  } finally {
    setBusy(false);
  }
}

async function openBrowser() {
  try {
    await call("open_url");
  } catch (e) {
    setMsg("打开浏览器失败：" + e, "err");
  }
}

async function runDoctor() {
  setMsg("自检中…");
  try {
    const out = await call("run_doctor");
    setMsg(out, out.includes("失败 0") ? "ok" : null);
  } catch (e) {
    setMsg("自检失败：" + e, "err");
  }
}

function renderUpdateStatus(info, announce) {
  if (!info || !els.updateBtn) return;
  lastUpdateCheck = info;
  const cur = info.current_version || "";
  const latest = info.latest_version || "";
  els.updateBtn.classList.toggle("update-available", !!info.update_available);
  els.updateBtn.textContent = info.update_available ? "有新版本可用" : "检查更新";
  els.updateBtn.title = info.update_available && latest
    ? "有新版本可用：v" + latest
    : "检查 GitHub Releases 最新版本";
  if (els.verLabel && cur) {
    els.verLabel.textContent = info.update_available && latest
      ? "v" + cur + " → v" + latest
      : "v" + cur;
  }
  if (announce && info.update_available) {
    setMsg("有新版本可用：v" + latest + "（当前 v" + cur + "）。", "ok");
  }
}

async function pollUpdateStatus(announce) {
  try {
    const info = await call("check_updates");
    renderUpdateStatus(info, !!announce);
    return info;
  } catch (e) {
    if (announce) {
      setMsg("无法自动检查更新（多为网络或代理限制）。已打开 Releases 页，请手动查看。", "err");
    }
    return null;
  }
}

async function checkUpdate() {
  setMsg("检查更新中…");
  const info = await pollUpdateStatus(true);
  if (!info) {
    try { await call("open_release_page"); } catch (_) {}
    return;
  }
  if (info.update_available) {
    try { await call("open_release_page"); } catch (_) {}
  } else {
    setMsg("已是最新版本（v" + (info.current_version || "") + "）。", "ok");
  }
}

async function refreshStatus() {
  try {
    const s = await call("status");
    setLight(els.ltProxy, s.proxy);
    setLight(els.ltSandbox, s.sandbox);
    setLight(els.ltUpstream, s.upstream);
    setRuntimeText(els.proxyStateText, s.proxy);
    setRuntimeText(els.sandboxStateText, s.sandbox);
    setRuntimeText(els.upstreamStateText, s.upstream);
    els.brandDot.className = "dot" + (s.proxy === "green" ? "" : " amber");
  } catch (e) {
    [els.ltProxy, els.ltSandbox, els.ltUpstream].forEach((l) => setLight(l, "amber"));
    [els.proxyStateText, els.sandboxStateText, els.upstreamStateText]
      .forEach((el) => setRuntimeText(el, "amber"));
  }
}

function setResearchStatus(text, kind) {
  const el = els.researchStatus;
  if (!el) return;
  el.textContent = text || "";
  el.className = "research-status" + (kind ? " " + kind : "");
  const consoleEl = el.closest(".launch-console");
  if (consoleEl) {
    consoleEl.classList.toggle("is-error", kind === "err");
    consoleEl.classList.toggle("is-ready", kind === "ok");
    consoleEl.classList.toggle("is-warning", kind === "warn");
  }
}

function syncResearchContext() {
  const active = (state.profiles || []).find((p) => p.id === state.active_id);
  if (els.activeProfileLabel) {
    els.activeProfileLabel.textContent = mode === "official"
      ? "官方 Claude"
      : (active && active.name) || "未连接";
    els.activeProfileLabel.title = mode === "official"
      ? ""
      : (active && active.name) || "";
  }
  if (els.packStateLabel) {
    if (mode === "official") {
      els.packStateLabel.textContent = "官方模式不装配";
    } else {
      const enabled = _packState && _packState.enabled
        ? Object.values(_packState.enabled).filter(Boolean).length
        : 0;
      els.packStateLabel.textContent = enabled ? `${enabled} 个工具包` : "按任务加载";
    }
  }
  if (els.privacyStateLabel) {
    els.privacyStateLabel.textContent = mode === "official"
      ? "由官方应用管理"
      : (_packState && _packState.sensitive_mode ? "敏感模式" : "标准模式");
  }
}

function showSettings() {
  if (els.researchHome) els.researchHome.hidden = true;
  if (els.settingsPage) els.settingsPage.hidden = false;
  if (els.panel) els.panel.classList.add("settings-open");
  if (els.settingsHeading) els.settingsHeading.focus();
}

function showResearchHome() {
  showView("list");
  if (els.settingsPage) els.settingsPage.hidden = true;
  if (els.researchHome) els.researchHome.hidden = false;
  if (els.panel) els.panel.classList.remove("settings-open");
  const heading = document.getElementById("researchHeading");
  if (heading) heading.focus();
}

async function launchWorkflow(button) {
  const task = button.dataset.workflow;
  const packs = (button.dataset.packs || "").split(",").filter(Boolean);
  const title = button.querySelector(".workflow-title").textContent;
  const blocker = workflowLaunchBlocker(mode, state.active_id);
  if (blocker === "official-mode") {
    setResearchStatus("当前为官方 Claude 模式。请在“连接与设置”中打开官方 Science，或切回第三方模式后装配研究工作流。", "err");
    els.settingsBtn.focus();
    return;
  }
  if (blocker === "missing-profile") {
    setResearchStatus("尚未连接研究引擎。请先打开左侧“连接与设置”，添加并激活一个模型连接。", "err");
    els.settingsBtn.focus();
    return;
  }
  setBusy(true);
  button.classList.add("is-launching");
  button.setAttribute("aria-busy", "true");
  setResearchStatus(`01 / 03 · 正在为“${title}”确认任务路由…`);
  try {
    await call("set_task_route", { task, profileId: state.active_id });
    setResearchStatus(`02 / 03 · 正在装配 ${packs.length} 组专业工具与证据规则…`);
    const packWarnings = [];
    let appliedPacks = new Set();
    for (const id of packs) {
      // 即使已勾选也重新装配：Science 只在启动时读取配置，工作流入口必须验证
      // 这一次的工具链确实落盘，而不是把历史勾选状态误报成“已就绪”。
      const result = await call("toggle_pack", { id, enabled: true });
      (result.warnings || []).forEach((warning) => packWarnings.push(String(warning)));
      appliedPacks = new Set(result.applied || []);
    }
    await loadPacks();
    const packResult = classifyWorkflowPackResult(
      packs,
      [...appliedPacks],
      packWarnings,
    );
    if (packResult.missing.length || packResult.blockingWarnings.length) {
      const details = [...new Set([
        ...(packResult.missing.length ? [`未装配：${packResult.missing.join(", ")}`] : []),
        ...packResult.blockingWarnings,
      ])];
      throw new Error("关键研究工具未完整装配。" + details.join("；"));
    }
    setResearchStatus("03 / 03 · 工具装配完成，正在打开隔离研究工作区…");
    const r = await call("one_click_login");
    const nonBlockingWarnings = packResult.warnings;
    if (nonBlockingWarnings.length) {
      setResearchStatus(`${title}已打开，但工具装配有提示：${nonBlockingWarnings.join("；")}`, "warn");
    } else {
      setResearchStatus(`${title}已就绪。${r.msg || "研究工作区已打开。"}`, "ok");
    }
    await refreshStatus();
  } catch (e) {
    setResearchStatus("准备中断；已完成的任务路由或工具包设置可能保留，可重试或进入设置检查。原因：" + e, "err");
  } finally {
    button.classList.remove("is-launching");
    button.removeAttribute("aria-busy");
    setBusy(false);
  }
}

function wire() {
  [
    "oneClickBtn", "stopBtn", "ltProxy", "ltSandbox", "ltUpstream",
    "msg", "brandDot", "openBrowserBtn", "doctorBtn", "updateBtn", "verLabel",
    "reportBtn", "logsBtn", "quitBtn", "settingsBtn", "homeBtn", "researchHome", "settingsPage", "settingsHeading", "researchStatus", "modeSeg", "proxyPort", "sandboxPort", "advSec",
    "proxyStateText", "sandboxStateText", "upstreamStateText", "activeProfileLabel", "packStateLabel", "privacyStateLabel",
    "listSec", "profileList", "newBtn", "skipActivateBtn",
    "wizSec", "wizTemplate", "wizTemplateChips", "wizTplLabel", "wizTplHint", "wizName", "wizBase", "wizBaseHint",
    "wizFetchBtn", "wizModelInfo", "wizModel", "wizModelHint", "wizKey", "wizSaveBtn", "wizCancelBtn",
    "connSec", "connTitle", "connBase", "connBaseHint", "connFetchBtn",
    "connModelInfo", "connModel", "connModelHint", "connKey", "connSaveBtn", "connClearBtn", "connCancelBtn",
    "metaSec", "metaName", "metaNotes", "metaSaveBtn", "metaCancelBtn",
  ].forEach((id) => (els[id] = $(id)));
  els.panel = document.querySelector(".panel");

  els.modeSeg.querySelectorAll(".seg-btn").forEach((b) =>
    b.addEventListener("click", () => switchMode(b.dataset.mode))
  );

  els.proxyPort.addEventListener("change", persistPorts);
  els.sandboxPort.addEventListener("change", persistPorts);

  // 列表行内操作（事件委托；忙碌时忽略）。
  els.profileList.addEventListener("click", (e) => {
    if (busy) return;
    const btn = e.target.closest("[data-act]");
    const row = e.target.closest("[data-id]");
    if (!btn || !row) return;
    const id = row.getAttribute("data-id");
    const act = btn.getAttribute("data-act");
    if (act === "activate") activate(id, false);
    else if (act === "editconn") openConn(id);
    else if (act === "editmeta") openMeta(id);
    else if (act === "clearkey") clearKey(id);
    else if (act === "delete") del(id);
  });

  els.newBtn.addEventListener("click", openWizard);
  els.skipActivateBtn.addEventListener("click", () => {
    const id = pendingSkipActivateId;
    if (id) activate(id, true);
  });

  els.wizTemplateChips.addEventListener("click", (e) => {
    if (busy) return;
    const chip = e.target.closest(".chip");
    if (chip) selectWizTemplate(chip.getAttribute("data-tid"));
  });
  els.wizModel.addEventListener("input", refreshWizGate); // input：键入即刷新保存门（#9 P1-b）
  els.wizFetchBtn.addEventListener("click", wizFetch);
  els.wizSaveBtn.addEventListener("click", wizSave);
  els.wizCancelBtn.addEventListener("click", cancelForm);

  els.connModel.addEventListener("input", refreshConnGate); // input：键入即刷新保存门（#9 P1-b）
  els.connFetchBtn.addEventListener("click", connFetch);
  els.connSaveBtn.addEventListener("click", connSave);
  els.connClearBtn.addEventListener("click", () => clearKey(els.connSec.dataset.id));
  els.connCancelBtn.addEventListener("click", cancelForm);

  els.metaSaveBtn.addEventListener("click", metaSave);
  els.metaCancelBtn.addEventListener("click", cancelForm);

  els.oneClickBtn.addEventListener("click", heroClick);
  els.stopBtn.addEventListener("click", stopAll);
  els.openBrowserBtn.addEventListener("click", openBrowser);
  els.doctorBtn.addEventListener("click", runDoctor);
  els.updateBtn.addEventListener("click", checkUpdate);
  els.reportBtn.addEventListener("click", () =>
    call("report_bug").catch((e) => setMsg("打开反馈页失败：" + e, "err"))
  );
  els.logsBtn.addEventListener("click", () =>
    call("open_logs").catch((e) => setMsg("打开日志失败：" + e, "err"))
  );
  els.quitBtn.addEventListener("click", () => call("quit_app").catch(() => {}));
  els.settingsBtn.addEventListener("click", showSettings);
  els.homeBtn.addEventListener("click", showResearchHome);
  document.querySelectorAll("[data-workflow]").forEach((card) =>
    card.addEventListener("click", () => launchWorkflow(card))
  );
}

// ═══════════════════════ 科研工具包 / 隐私 / 任务路由（bio-* 扩展）═══════════════════════

let _packState = null;    // list_packs 结果缓存
let _tasksState = null;   // list_biomed_tasks 结果缓存

function _msg(id, text, kind) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text || "";
  el.className = "msg" + (kind ? " " + kind : "");
}
async function loadPacks() {
  try { _packState = await call("list_packs"); }
  catch (e) { _msg("packMsg", "读取工具包列表失败：" + e, "err"); return; }
  renderPacks();
  renderPrivacy();
  _setVerifyChips(_packState && _packState.verification);
  _renderExperimentalBanner();
}

function _renderExperimentalBanner() {
  const el = document.getElementById("verifyStatusHint");
  if (!el || !_packState) return;
  const v = _packState.verification || {};
  const exp = v.is_experimental !== false;
  clearChildren(el);
  if (v.stale) {
    const reasons = (v.stale_reasons || []).join("；");
    appendText(el, "⚠ ");
    el.appendChild(textEl("strong", "", "验证结果可能过期"));
    appendText(el, `：${reasons}。建议重跑 canary 验证。`);
    return;
  }
  const fp = v.fingerprint || {};
  const fpNote = (fp.science_version_at_pass && fp.science_version_at_pass !== "unknown")
    ? `（验证时 Science ${fp.science_version_at_pass}）` : "";
  if (exp) {
    appendText(el, "pack 机制的两个关键路径（");
    el.appendChild(textEl("code", "", "mcp-servers.json"));
    appendText(el, " 与 ");
    el.appendChild(textEl("code", "", "skills/"));
    appendText(el, "）");
    el.appendChild(textEl("strong", "", "尚未确认"));
    appendText(el, "。所有 pack 目前按");
    el.appendChild(textEl("strong", "", "实验状态"));
    appendText(el, "装配。跑一次 canary smoke test 即可确认。");
  } else {
    el.textContent = `✓ 路径已验证。pack 装配可信度：高。${fpNote}`;
  }
}

function renderPacks() {
  const list = document.getElementById("packList");
  const envList = document.getElementById("packEnvList");
  if (!list || !envList || !_packState) return;
  const { packs, enabled, env_set } = _packState;
  clearChildren(list);
  for (const p of packs || []) {
    const on = !!(enabled || {})[p.id];
    const row = document.createElement("div");
    row.className = "packrow";
    const missing = (p.requires_env || []).filter((k) => !(env_set || {})[k]);
    // 未通过 phase-1 验证时统一标"实验中"
    const isExp = ((_packState.verification || {}).is_experimental !== false);

    const label = document.createElement("label");
    label.className = "packchk";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.pack = String(p.id ?? "");
    input.checked = on;
    label.appendChild(input);
    label.appendChild(textEl("span", "packname", p.name));

    const deps = (p.dependencies && p.dependencies.length) ? p.dependencies : (p.depends_on || []);
    if (deps.length) {
      label.appendChild(textEl("span", "chip", `依赖 ${deps.join(", ")}`));
    }
    if (missing.length) {
      const missingChip = textEl("span", "chip warn", `缺 ${missing.join(", ")}`);
      missingChip.title = "缺环境变量";
      label.appendChild(missingChip);
    }
    if (on && isExp) {
      const expChip = textEl("span", "chip warn", "实验中");
      expChip.title = "MCP / Skill 路径尚未验证，请去『验证』折叠区跑 canary";
      label.appendChild(expChip);
    }

    row.appendChild(label);
    row.appendChild(textEl("div", "packdesc", p.description));
    list.appendChild(row);
  }
  list.querySelectorAll('input[type="checkbox"][data-pack]').forEach((cb) =>
    cb.addEventListener("change", () => togglePack(cb.dataset.pack, cb.checked, cb)));

  const seen = new Set();
  clearChildren(envList);
  for (const p of (packs || [])) {
    for (const oe of p.optional_env || []) {
      if (seen.has(oe.name)) continue;
      seen.add(oe.name);
      const row = document.createElement("div");
      row.className = "packenvrow";
      const has = !!(env_set || {})[oe.name];
      const label = textEl("label", "packenvlabel", oe.label || oe.name);
      if (oe.url) {
        const link = textEl("a", "link", "申请");
        if (applySafeExternalLink(link, oe.url)) {
          appendText(label, " ");
          label.appendChild(link);
        }
      }
      const formRow = document.createElement("div");
      formRow.className = "row";
      const input = document.createElement("input");
      input.type = "password";
      input.dataset.env = String(oe.name ?? "");
      input.placeholder = has ? "已存（末位掩码）" : "留空清除";
      input.autocomplete = "off";
      input.spellcheck = false;
      const btn = document.createElement("button");
      btn.className = "btn small";
      btn.dataset.envSave = String(oe.name ?? "");
      btn.textContent = "保存";
      formRow.appendChild(input);
      formRow.appendChild(btn);
      row.appendChild(label);
      row.appendChild(formRow);
      envList.appendChild(row);
    }
  }
  envList.querySelectorAll("button[data-env-save]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const name = btn.dataset.envSave;
      const inp = envList.querySelector(`input[data-env="${CSS.escape(name)}"]`);
      savePackEnv(name, inp ? inp.value : "");
    }));
  syncResearchContext();
}

async function togglePack(id, enabled, cb) {
  cb.disabled = true;
  _msg("packMsg", `${enabled ? "启用" : "停用"} ${id}…`);
  try {
    const r = await call("toggle_pack", { id, enabled });
    if (_packState) _packState.enabled[id] = enabled;
    const parts = [];
    if (r.note) parts.push(r.note);
    if (r.warnings && r.warnings.length) parts.push("警告：" + r.warnings.join("; "));
    if (r.sandbox_restarted) parts.push("为让配置生效，已停沙箱，请再点「一键开始」。");
    _msg("packMsg", parts.join("\n") || "已保存。", r.warnings && r.warnings.length ? "err" : "ok");
    renderPrivacy();  // 因为 bio-privacy 状态可能改了
    await refreshStatus();
  } catch (e) {
    cb.checked = !enabled;
    _msg("packMsg", `切换 ${id} 失败：${e}`, "err");
  } finally {
    cb.disabled = false;
  }
}

async function savePackEnv(name, value) {
  _msg("packMsg", `保存 ${name}…`);
  try {
    await call("set_pack_env", { name, value });
    if (_packState) _packState.env_set[name] = !!value;
    _msg("packMsg", value ? `${name} 已保存。` : `${name} 已清除。`, "ok");
    renderPacks();
  } catch (e) { _msg("packMsg", `保存 ${name} 失败：${e}`, "err"); }
}

// ---- 隐私 / 合规 ----
function renderPrivacy() {
  if (!_packState) return;
  const chk = document.getElementById("sensitiveModeChk");
  const chip = document.getElementById("sensitiveWarnChip");
  const ta = document.getElementById("localHostsTa");
  const upHost = document.getElementById("upstreamHost");
  if (chk) chk.checked = !!_packState.sensitive_mode;
  if (ta) ta.value = (_packState.local_endpoint_hosts || []).join("\n");
  if (upHost) upHost.textContent = _packState.current_upstream_host || "—";
  const bioPrivacyOn = !!(_packState.enabled || {})["bio-privacy"];
  if (chip) chip.style.display = (_packState.sensitive_mode && !bioPrivacyOn) ? "" : "none";
  syncResearchContext();
}

async function setSensitiveMode(enabled) {
  _msg("privacyMsg", `${enabled ? "启用" : "关闭"}敏感模式…`);
  try {
    const r = await call("set_sensitive_mode", { enabled });
    if (_packState) _packState.sensitive_mode = !!r.sensitive_mode;
    const bits = [];
    if (r.sandbox_stopped) bits.push("当前 provider 不在白名单，已停沙箱。");
    if (r.suggest_enable_bio_privacy) bits.push("建议同时启用 bio-privacy pack（PHI 扫描 + 审计）。");
    _msg("privacyMsg", bits.join(" ") || "已保存。", r.sandbox_stopped ? "err" : "ok");
    renderPrivacy();
    await refreshStatus();
  } catch (e) {
    const chk = document.getElementById("sensitiveModeChk");
    if (chk) chk.checked = !enabled;
    _msg("privacyMsg", "切换敏感模式失败：" + e, "err");
  }
}

async function saveLocalHosts(confirmPublic) {
  const ta = document.getElementById("localHostsTa");
  if (!ta) return;
  const hosts = ta.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  _msg("privacyMsg", "保存白名单…");
  try {
    const r = await call("set_local_endpoint_hosts", { hosts, confirmPublic: !!confirmPublic });
    if (r && r.ok === false && r.needs_confirm && r.needs_confirm.length) {
      // 公网域名待用户二次确认（"用户明确确认的机构域名"）
      const ok = confirm(
        (r.hint || "以下是公网域名，敏感模式默认只允许 localhost / 私网。") + "\n\n" +
        r.needs_confirm.join("\n") + "\n\n确认这些是你机构的受控端点？"
      );
      if (ok) { await saveLocalHosts(true); return; }
      _msg("privacyMsg", "已取消：公网域名未加入白名单。", "err");
      return;
    }
    if (_packState) _packState.local_endpoint_hosts = r.hosts || [];
    const invNote = (r.invalid && r.invalid.length) ? `（忽略无法解析：${r.invalid.join(", ")}）` : "";
    _msg("privacyMsg", `已保存 ${(r.hosts || []).length} 个受信 host。${invNote}`, "ok");
    renderPrivacy();
  } catch (e) { _msg("privacyMsg", "保存失败：" + e, "err"); }
}

// ---- 任务级模型路由 ----
async function loadTasks() {
  try { _tasksState = await call("list_biomed_tasks"); }
  catch (e) { _msg("tasksMsg", "读取任务列表失败：" + e, "err"); return; }
  renderTasks();
}

function renderTasks() {
  const list = document.getElementById("taskList");
  if (!list || !_tasksState) return;
  const profiles = (state && state.profiles) || [];
  const routes = _tasksState.routes || {};
  const active = _tasksState.active_id || "";
  const probes = _tasksState.probes || {};
  clearChildren(list);
  for (const t of _tasksState.tasks || []) {
    const routedTo = routes[t.id] || "";
    const currentId = routedTo || active;
    const currentName = ((profiles.find((p) => p.id === currentId) || {}).name) || "(无生效)";
    const row = document.createElement("div");
    row.className = "taskrow";

    const head = document.createElement("div");
    head.className = "taskhd";
    head.appendChild(textEl("span", "taskname", t.label));
    head.appendChild(textEl("span", "taskprofile", `当前：${currentName}`));
    row.appendChild(head);
    row.appendChild(textEl("div", "taskhint", t.hint));

    const formRow = document.createElement("div");
    formRow.className = "row";
    const select = document.createElement("select");
    select.dataset.task = String(t.id ?? "");
    const defaultOption = document.createElement("option");
    defaultOption.value = "";
    defaultOption.textContent = "(默认，跟随生效)";
    select.appendChild(defaultOption);
    for (const p of profiles) {
      const option = document.createElement("option");
      option.value = String(p.id ?? "");
      option.selected = routedTo === p.id;
      option.textContent = String(p.name ?? "");
      select.appendChild(option);
    }
    formRow.appendChild(select);
    row.appendChild(formRow);

    const chips = document.createElement("div");
    chips.className = "taskchips";
    let hasChip = false;
    for (const probe of ["tool_use", "long_ctx", "json_stable",
                         "bio_eval_lit_review", "bio_eval_clinical_trials", "bio_eval_evidence_audit"]) {
      const key = `${currentId}:${probe}`;
      const raw = probes[key];
      if (!raw) continue;
      const verdict = raw.verdict || "?";
      const chip = textEl(
        "span",
        "chip" + (verdict === "ok" || verdict === "✓" || verdict === "✓✓" ? "" : " warn"),
        `${probe}: ${verdict}`,
      );
      chip.title = String(raw.reason || "");
      chips.appendChild(chip);
      hasChip = true;
    }
    if (!hasChip) chips.appendChild(textEl("span", "chip", "尚无探针数据"));
    row.appendChild(chips);
    list.appendChild(row);
  }
  list.querySelectorAll("select[data-task]").forEach((sel) => {
    sel.addEventListener("change", () => setTaskRoute(sel.dataset.task, sel.value));
  });
}

async function setTaskRoute(task, profileId) {
  _msg("tasksMsg", `保存 ${task} 路由…`);
  try {
    await call("set_task_route", { task, profileId });
    if (_tasksState) {
      if (profileId) _tasksState.routes[task] = profileId;
      else delete _tasksState.routes[task];
    }
    _msg("tasksMsg", "已保存。", "ok");
    renderTasks();
  } catch (e) { _msg("tasksMsg", "保存失败：" + e, "err"); }
}

async function runProbes() {
  if (!state || !state.active_id) {
    _msg("tasksMsg", "请先激活一个 profile。", "err");
    return;
  }
  const btn = document.getElementById("probeRunBtn");
  if (btn) btn.disabled = true;
  _msg("tasksMsg", "跑探针中…（tool_use / long_ctx / json_stable，各 1 次 minimal 请求）");
  try {
    const r = await call("run_probes", {
      req: { profile_id: state.active_id, probes: ["tool_use", "long_ctx", "json_stable"] },
    });
    const parts = [];
    for (const [k, v] of Object.entries(r.results || {})) {
      parts.push(`${k}: ${v.verdict} (${v.reason})`);
    }
    _msg("tasksMsg", "探针完成：\n" + parts.join("\n"), "ok");
    await loadTasks();
  } catch (e) { _msg("tasksMsg", "跑探针失败：" + e, "err"); }
  finally { if (btn) btn.disabled = false; }
}

function wireBioExtensions() {
  const sensChk = document.getElementById("sensitiveModeChk");
  if (sensChk) sensChk.addEventListener("change", () => setSensitiveMode(sensChk.checked));
  const hostsBtn = document.getElementById("localHostsSaveBtn");
  if (hostsBtn) hostsBtn.addEventListener("click", () => saveLocalHosts(false));
  const probeBtn = document.getElementById("probeRunBtn");
  if (probeBtn) probeBtn.addEventListener("click", runProbes);
  // phase-1 验证按钮
  const b1 = document.getElementById("verifyStartBtn");
  if (b1) b1.addEventListener("click", startVerify);
  const b2 = document.getElementById("verifyPollBtn");
  if (b2) b2.addEventListener("click", pollVerify);
  const b3 = document.getElementById("verifySkillOkBtn");
  if (b3) b3.addEventListener("click", () => confirmSkill(true));
  const b4 = document.getElementById("verifySkillFailBtn");
  if (b4) b4.addEventListener("click", () => confirmSkill(false));
  const b5 = document.getElementById("verifyCleanupBtn");
  if (b5) b5.addEventListener("click", cleanupVerify);
}

// ---- phase-1 路径验证 ----
function _setVerifyChips(verification) {
  const mcpChip = document.getElementById("verifyMcpChip");
  const skillChip = document.getElementById("verifySkillChip");
  const map = {
    unverified: { text: "未验证", cls: "warn" },
    mcp_path_pending: { text: "已写 canary，待检查", cls: "warn" },
    mcp_path_ok: { text: "✓ 路径正确", cls: "" },
    mcp_path_fail: { text: "✗ 不通过", cls: "warn" },
    skill_path_ok: { text: "✓ 路径正确", cls: "" },
    skill_path_fail: { text: "✗ 不通过", cls: "warn" },
  };
  if (mcpChip && verification) {
    const m = map[verification.mcp_verdict] || map.unverified;
    mcpChip.textContent = m.text;
    mcpChip.className = "chip " + m.cls;
  }
  if (skillChip && verification) {
    const m = map[verification.skill_verdict] || map.unverified;
    skillChip.textContent = m.text;
    skillChip.className = "chip " + m.cls;
  }
}

async function startVerify() {
  _msg("verifyMsg", "写入 canary MCP + Skill…");
  try {
    const r = await call("start_smoke_verification");
    _msg("verifyMsg", `已写 canary（marker=${r.marker.slice(0, 8)}…）。${r.next_step}`, "ok");
    if (_packState) _packState.verification = _packState.verification || {};
    _packState.verification.mcp_verdict = "mcp_path_pending";
    _setVerifyChips(_packState.verification);
  } catch (e) { _msg("verifyMsg", "写 canary 失败：" + e, "err"); }
}
async function pollVerify() {
  _msg("verifyMsg", "检查 canary marker…");
  try {
    const r = await call("poll_smoke_verification");
    _msg("verifyMsg", r.reason, r.verdict === "mcp_path_ok" ? "ok" : "err");
    if (_packState) {
      _packState.verification = _packState.verification || {};
      _packState.verification.mcp_verdict = r.verdict;
      _setVerifyChips(_packState.verification);
    }
  } catch (e) { _msg("verifyMsg", "检查失败：" + e, "err"); }
}
async function confirmSkill(ok) {
  _msg("verifyMsg", ok ? "记录 skill 路径通过…" : "记录 skill 路径不通过…");
  try {
    const r = await call("confirm_skill_verified", { userConfirmed: ok });
    _msg("verifyMsg", r.reason, ok ? "ok" : "err");
    if (_packState) {
      _packState.verification = _packState.verification || {};
      _packState.verification.skill_verdict = r.verdict;
      _setVerifyChips(_packState.verification);
    }
  } catch (e) { _msg("verifyMsg", "保存失败：" + e, "err"); }
}
async function cleanupVerify() {
  _msg("verifyMsg", "清理 canary…");
  try {
    await call("cleanup_smoke_verification");
    _msg("verifyMsg", "已清理 canary MCP + Skill。可以重启沙箱继续正常使用。", "ok");
  } catch (e) { _msg("verifyMsg", "清理失败：" + e, "err"); }
}

// ═══════════════════════════════════════════════════════════════════════════════

window.addEventListener("DOMContentLoaded", async () => {
  wire();
  wireBioExtensions();
  await loadConfig();
  await loadPacks();
  await loadTasks();
  try { els.verLabel.textContent = "v" + (await call("app_version")); } catch (e) {}
  pollUpdateStatus(false);
  await refreshStatus();
  if (PREVIEW) {
    setMsg("预览模式：仅看界面，按钮不连后端（真实 app 里会连进程管家）。");
  } else {
    statusTimer = setInterval(refreshStatus, 2500);
    updateTimer = setInterval(() => pollUpdateStatus(false), UPDATE_CHECK_INTERVAL_MS);
  }
});
