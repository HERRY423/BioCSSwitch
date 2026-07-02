// CSSwitch 菜单栏面板前端。只调用后端 Tauri command，绝不碰任何密钥落盘逻辑。
// 后端只把 key 的【掩码】回显给这里；完整 key 永不进前端。
//
// 预览兜底：在普通浏览器里打开（没有 Tauri 后端）时，用 mockInvoke 返回假数据，
// 让界面能完整渲染、不报错。真实 app 里 window.__TAURI__ 存在，走真后端，此兜底不生效。
const PREVIEW = !window.__TAURI__;
const invoke = PREVIEW
  ? (cmd, args) => mockInvoke(cmd, args)
  : window.__TAURI__.core.invoke;

function mockInvoke(cmd, args) {
  switch (cmd) {
    case "get_config":
      return Promise.resolve({ provider: "deepseek", proxy_port: 18991, sandbox_port: 8990, keys: { deepseek: "", qwen: "" } });
    case "status":
      return Promise.resolve({ proxy: "amber", sandbox: "amber", upstream: "amber" });
    case "save_provider_key":
      return Promise.resolve("••••••••••" + String((args && args.key) || "").slice(-4));
    case "start_proxy":
      return Promise.resolve({ port: 18991 });
    case "verify_key":
      return Promise.resolve({ ok: true, hint: "（预览模式：假装 key 有效）" });
    case "one_click_login":
      return Promise.resolve({ url: "http://127.0.0.1:8990" });
    case "run_doctor":
      return Promise.resolve("（预览模式：后端未运行，这里是占位文本）");
    case "app_version":
      return Promise.resolve("0.0.0-preview");
    case "open_release_page":
    case "report_bug":
    case "open_logs":
      return Promise.resolve(null);
    default:
      return Promise.resolve(null);
  }
}

const $ = (id) => document.getElementById(id);
const els = {};
let statusTimer = null;
let busy = false;

const KEY_LABELS = { deepseek: "DeepSeek API Key", qwen: "DashScope (通义千问) API Key" };

function setMsg(text, kind) {
  els.msg.textContent = text;
  els.msg.className = "msg" + (kind ? " " + kind : "");
}

function setLight(el, state) {
  // state: "green" | "amber" | "red"
  const cls = { green: "g", amber: "a", red: "r" }[state] || "a";
  el.className = "lt " + cls;
}

function setBusy(on) {
  busy = on;
  [els.oneClickBtn, els.stopBtn, els.saveKeyBtn].forEach((b) => (b.disabled = on));
}

async function call(cmd, args) {
  return await invoke(cmd, args);
}

async function loadConfig() {
  try {
    const cfg = await call("get_config");
    els.provider.value = cfg.provider || "deepseek";
    els.proxyPort.value = cfg.proxy_port ?? 18991;
    els.sandboxPort.value = cfg.sandbox_port ?? 8990;
    window._keys = cfg.keys || {};
    reflectProvider();
  } catch (e) {
    setMsg("读取配置失败：" + e, "err");
  }
}

function reflectProvider() {
  const p = els.provider.value;
  els.keyLabel.textContent = KEY_LABELS[p] || "API Key";
  const masked = (window._keys && window._keys[p]) || "";
  els.keyInput.value = "";
  els.keyInput.placeholder = masked ? "已存：" + masked : "粘贴第三方 key（只存本地）";
}

function currentSettings() {
  return {
    provider: els.provider.value,
    proxy_port: parseInt(els.proxyPort.value, 10) || 18991,
    sandbox_port: parseInt(els.sandboxPort.value, 10) || 8990,
  };
}

// 保存设置：失败会【抛出】，让调用方（起代理 / 一键登录）中止，
// 不再吞掉错误后拿旧配置继续、还误报成功（修 P1-4）。
async function persistSettings() {
  await call("set_config", { cfg: currentSettings() });
}

// 独立 UI 事件（改 provider / 端口）用的兜底版：失败只提示、不抛，避免未捕获拒绝。
async function persistSettingsSafe() {
  try {
    await persistSettings();
  } catch (e) {
    setMsg("保存设置失败：" + e, "err");
  }
}

async function saveKey() {
  const key = els.keyInput.value.trim();
  if (!key) {
    setMsg("请先粘贴 key。", "err");
    return;
  }
  setBusy(true);
  try {
    const masked = await call("save_provider_key", { provider: els.provider.value, key });
    window._keys[els.provider.value] = masked;
    reflectProvider();
    setMsg("已保存，正在启动代理并验证 key…", "ok");
    await persistSettings();
    // 存了 key 就自动起代理 + 用最小请求真验一次这把 key（不是「代理起来了」就当成功）。
    try {
      const v = await call("verify_key");
      if (v && v.ok) {
        setMsg("已保存，key 有效 ✓ 代理已就绪，点「一键越过登录」即可。", "ok");
      } else {
        setMsg("已保存，代理已起；但 key 未通过验证：" + ((v && v.hint) || "上游未接受") + " 可仍试「一键越过登录」。", "err");
      }
    } catch (ve) {
      // 代理没起来（缺依赖/端口占用），或验证请求发不出去（网络/上游不通）。
      setMsg("已保存；但未能验证 key：" + ve, "err");
    }
  } catch (e) {
    setMsg("保存失败：" + e, "err");
  } finally {
    setBusy(false);
    await refreshStatus();
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

async function oneClick() {
  setBusy(true);
  setMsg("一键越过登录：起代理 → 起沙箱 → 探活…");
  try {
    // 「粘贴 key → 直接一键越过登录」也要能走通：输入框里有新 key 就先存下，
    // 不强制用户先点「保存」（修 P1：oneClick 之前不读/不存输入框，导致无 key 起代理失败）。
    const key = els.keyInput.value.trim();
    if (key) {
      const masked = await call("save_provider_key", { provider: els.provider.value, key });
      window._keys[els.provider.value] = masked;
      els.keyInput.value = "";
      reflectProvider();
    }
    await persistSettings();
    const r = await call("one_click_login");
    setMsg("登录态就绪。正在打开浏览器面板…\n" + (r.url || ""), "ok");
    await refreshStatus();
  } catch (e) {
    setMsg("一键越登录失败：" + e, "err");
  } finally {
    setBusy(false);
  }
}

async function openBrowser() {
  try {
    await call("open_url", {});
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

// 简单 semver 比较：a 是否比 b 新。
function isNewer(a, b) {
  const pa = String(a).split(".").map((n) => parseInt(n, 10) || 0);
  const pb = String(b).split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0, y = pb[i] || 0;
    if (x !== y) return x > y;
  }
  return false;
}

// 轻量检查更新：查 GitHub 最新 Release 版本号，有新版就提示并打开下载页（不自动装）。
async function checkUpdate() {
  setMsg("检查更新中…");
  let cur = "";
  try { cur = await call("app_version"); } catch (e) {}
  try {
    const resp = await fetch(
      "https://api.github.com/repos/SuperJJ007/CSswitch/releases/latest",
      { headers: { Accept: "application/vnd.github+json" } }
    );
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    const latest = (data.tag_name || "").replace(/^v/, "");
    if (!latest) throw new Error("无版本信息");
    if (isNewer(latest, cur)) {
      setMsg("发现新版本 v" + latest + "（当前 v" + cur + "）。正在打开下载页…", "ok");
      try { await call("open_release_page"); } catch (_) {}
    } else {
      setMsg("已是最新版本（v" + cur + "）。", "ok");
    }
  } catch (e) {
    setMsg("无法自动检查更新（多为网络或代理限制）。已打开 Releases 页，请手动查看。", "err");
    try { await call("open_release_page"); } catch (_) {}
  }
}

async function refreshStatus() {
  try {
    const s = await call("status");
    setLight(els.ltProxy, s.proxy);
    setLight(els.ltSandbox, s.sandbox);
    setLight(els.ltUpstream, s.upstream);
    const anyGreen = s.proxy === "green" || s.sandbox === "green";
    els.brandDot.className = "dot" + (s.proxy === "green" ? "" : " amber");
  } catch (e) {
    // 状态探测失败不打断，静默降级为黄灯。
    [els.ltProxy, els.ltSandbox, els.ltUpstream].forEach((l) => setLight(l, "amber"));
  }
}

function wire() {
  [
    "provider", "keyLabel", "keyInput", "saveKeyBtn", "proxyPort", "sandboxPort",
    "oneClickBtn", "stopBtn", "ltProxy", "ltSandbox", "ltUpstream",
    "msg", "brandDot", "openBrowserBtn", "doctorBtn", "updateBtn", "verLabel",
    "reportBtn", "logsBtn", "quitBtn",
  ].forEach((id) => (els[id] = $(id)));

  els.provider.addEventListener("change", async () => {
    reflectProvider();
    await persistSettingsSafe();
  });
  els.proxyPort.addEventListener("change", persistSettingsSafe);
  els.sandboxPort.addEventListener("change", persistSettingsSafe);
  els.saveKeyBtn.addEventListener("click", saveKey);
  els.stopBtn.addEventListener("click", stopAll);
  els.oneClickBtn.addEventListener("click", oneClick);
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
}

window.addEventListener("DOMContentLoaded", async () => {
  wire();
  await loadConfig();
  try { els.verLabel.textContent = "v" + (await call("app_version")); } catch (e) {}
  await refreshStatus();
  if (PREVIEW) {
    setMsg("预览模式：仅看界面，按钮不连后端（真实 app 里会连进程管家）。");
  } else {
    statusTimer = setInterval(refreshStatus, 2500);
  }
});
