import { describe, expect, test } from "vitest";
import {
  CAP,
  isNativeAdapter,
  modelCapability,
  openaiCustomAnthropicBaseMessage,
  sourceHint,
} from "./ui-logic.js";

describe("model capability policy", () => {
  test.each(["deepseek", "qwen"])("%s is native", (adapter) => {
    expect(isNativeAdapter(adapter)).toBe(true);
    expect(modelCapability({ adapter })).toBe(CAP.NATIVE);
  });

  test("unknown templates fail closed and relay modes stay distinct", () => {
    expect(modelCapability()).toBe(CAP.FIXED);
    expect(modelCapability({ adapter: "relay", requires_model_override: true }))
      .toBe(CAP.FIXED);
    expect(modelCapability({ adapter: "relay", requires_model_override: false }))
      .toBe(CAP.FOLLOW);
  });
});

describe("source hints", () => {
  test("describes each custom OpenAI protocol precisely", () => {
    expect(sourceHint({
      base_url_editable: true,
      base_url: "",
      api_format: "openai_chat",
    })).toContain("Chat Completions");
    expect(sourceHint({
      base_url_editable: true,
      base_url: "",
      api_format: "openai_responses",
    })).toContain("Responses");
  });

  test("does not call Qwen a native protocol endpoint", () => {
    expect(sourceHint({ adapter: "qwen" })).toContain("转换协议");
    expect(sourceHint({ adapter: "deepseek" })).toContain("无需转换");
  });

  test("distinguishes following Science from a fixed model", () => {
    expect(sourceHint({
      adapter: "relay",
      base_url: "https://relay.example",
      base_url_editable: false,
      requires_model_override: false,
    })).toContain("跟随 Science");
    expect(sourceHint({
      adapter: "relay",
      base_url: "https://relay.example",
      base_url_editable: false,
      requires_model_override: true,
    })).toContain("选一个模型");
  });
});

describe("custom endpoint validation", () => {
  test.each(["custom-openai", "custom-openai-responses"])(
    "rejects Anthropic paths for %s",
    (id) => {
      expect(openaiCustomAnthropicBaseMessage(
        { id },
        "HTTPS://api.example.test/Anthropic/",
      )).toContain("Anthropic 兼容端点");
    },
  );

  test("allows OpenAI roots and custom Anthropic templates", () => {
    expect(openaiCustomAnthropicBaseMessage(
      { id: "custom-openai" },
      "https://api.example.test/v1",
    )).toBe("");
    expect(openaiCustomAnthropicBaseMessage(
      { id: "custom" },
      "https://api.example.test/anthropic",
    )).toBe("");
  });
});
