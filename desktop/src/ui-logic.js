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
