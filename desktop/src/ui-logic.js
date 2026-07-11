// Pure UI policy helpers. Keeping these DOM-free makes behavior independently
// testable while main.js remains a small Tauri/browser integration layer.

export const CAP = Object.freeze({
  NATIVE: "native",
  FOLLOW: "follow",
  FIXED: "fixed",
});

export function isNativeAdapter(adapter) {
  return adapter === "deepseek" || adapter === "qwen";
}

export function modelCapability(template) {
  if (!template) return CAP.FIXED;
  if (isNativeAdapter(template.adapter)) return CAP.NATIVE;
  return template.requires_model_override ? CAP.FIXED : CAP.FOLLOW;
}

export function sourceHint(template) {
  if (!template) return "选择来源后按提示填写。";
  if (
    template.base_url_editable &&
    !template.base_url &&
    template.api_format === "openai_chat"
  ) {
    return "自定义 OpenAI Chat Completions 兼容端点：填 base root、key 与模型，经代理转换协议。";
  }
  if (
    template.base_url_editable &&
    !template.base_url &&
    template.api_format === "openai_responses"
  ) {
    return "自定义 OpenAI Responses 兼容端点：填 base root、key 与模型，经代理转换协议。";
  }
  if (template.base_url_editable && !template.base_url) {
    return "自定义 Anthropic 兼容端点：填地址与 key，用「获取模型」列出并选一个。";
  }

  const capability = modelCapability(template);
  if (capability === CAP.NATIVE) {
    return template.adapter === "qwen"
      ? "官方端点（经代理转换协议）：填 API Key 即可，地址与模型都已内置。"
      : "官方原生端点（无需转换）：填 API Key 即可，地址与模型都已内置。";
  }
  const address = template.base_url_editable
    ? "地址已预填官方默认（套餐 / 区域端点可改）"
    : "地址已预设";
  if (capability === CAP.FOLLOW) {
    return `填 API Key 即可，${address}，模型默认跟随 Science。`;
  }
  return `填 API Key 并选一个模型，${address}。`;
}

export function openaiCustomAnthropicBaseMessage(template, base) {
  const isOpenAI =
    template &&
    (template.id === "custom-openai" ||
      template.id === "custom-openai-responses");
  if (isOpenAI && String(base || "").trim().toLowerCase().includes("/anthropic")) {
    return "这个地址看起来是 Anthropic 兼容端点。请改选「自定义 Anthropic」，或填写 OpenAI 兼容 base root（如 https://api.moonshot.cn/v1）。";
  }
  return "";
}

export function workflowLaunchBlocker(mode, activeId) {
  if (mode === "official") return "official-mode";
  if (!String(activeId || "").trim()) return "missing-profile";
  return "";
}

export function classifyWorkflowPackResult(requested, applied, warnings) {
  const appliedSet = new Set(applied || []);
  const uniqueWarnings = [...new Set((warnings || []).map((warning) => String(warning)))];
  return {
    missing: (requested || []).filter((id) => !appliedSet.has(id)),
    blockingWarnings: uniqueWarnings.filter((warning) =>
      /未装配|装配失败|缺必填/.test(warning)),
    warnings: uniqueWarnings,
  };
}

const WORKFLOW_PACK_BASE = Object.freeze(["bio-compiler"]);

const EVIDENCE_MODE_LABELS = Object.freeze({
  rigorous: "严格核验",
  exploratory: "探索性假设",
  clinical: "临床边界",
});

/**
 * The home page is an intent-first surface. This helper keeps the setup
 * decision explicit so a missing connection never silently drops that intent.
 */
export function workflowSetupAction(mode, activeId, profileCount) {
  if (mode === "official") return "switch-to-proxy";
  if (String(activeId || "").trim()) return "launch";
  return Number(profileCount || 0) > 0 ? "activate-profile" : "create-profile";
}

/** Always install the local deterministic compiler before a workflow skill. */
export function requiredWorkflowPacks(packs) {
  return [...new Set([
    ...WORKFLOW_PACK_BASE,
    ...(packs || []).map((id) => String(id || "").trim()).filter(Boolean),
  ])];
}

export function validateResearchQuestion(question) {
  const value = String(question || "").replace(/\r\n/g, "\n").trim();
  const meaningfulLength = Array.from(value.replace(/\s/g, "")).length;
  if (!value) {
    return { ok: false, code: "required", message: "请先写下要解决的核心研究问题。", value };
  }
  if (meaningfulLength < 6) {
    return { ok: false, code: "too-short", message: "研究问题过短；请补充研究对象、场景或目标。", value };
  }
  if (Array.from(value).length > 4000) {
    return { ok: false, code: "too-long", message: "研究问题最多 4000 个字符；请把背景移到补充边界。", value };
  }
  return { ok: true, code: "", message: "", value };
}

/**
 * Serialize the confirmed brief as a plain-text handoff. The explicit marker
 * is readable by humans and future routing layers; JSON remains the audit
 * source of truth and is never interpolated as HTML.
 */
export function formatResearchBriefHandoff(brief, options = {}) {
  if (!brief || brief.status !== "ready") {
    throw new Error("research brief must be finalized before handoff");
  }
  const taskId = String(
    brief.workflow_hint || (brief.route && brief.route.task_id) || options.taskId || "",
  ).trim();
  if (!taskId) throw new Error("research brief is missing task id");
  const evidenceMode = EVIDENCE_MODE_LABELS[options.evidenceMode] || EVIDENCE_MODE_LABELS.rigorous;
  const scope = String(options.scope || "").trim();
  const integrityRules = [
    "先复述任务书与适用边界；若仍有未决条件，停止检索并向研究者确认。",
    "实证声明必须绑定可核验来源；未查到视为未知，不得当作反证。",
    "显式区分事实、推断与假设，并保留冲突证据和不确定性。",
    "结束时给出证据矩阵/可复现产物、缺失数据与下一步区分性验证。",
  ];
  return [
    "# BioCSSwitch 已确认研究任务书",
    `BioCSSwitch-Task-ID: ${taskId}`,
    `Evidence-Mode: ${evidenceMode}`,
    scope ? `Operator-Scope: ${scope}` : "Operator-Scope: 未额外限定",
    "",
    "## 执行协议",
    ...integrityRules.map((rule, index) => `${index + 1}. ${rule}`),
    "",
    "## 结构化任务书（审计源）",
    "```json",
    JSON.stringify(brief, null, 2),
    "```",
  ].join("\n");
}
