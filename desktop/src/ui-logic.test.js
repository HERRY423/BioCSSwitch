import { describe, expect, test } from "vitest";
import {
  CAP,
  classifyWorkflowPackResult,
  isNativeAdapter,
  modelCapability,
  openaiCustomAnthropicBaseMessage,
  sourceHint,
  workflowLaunchBlocker,
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

describe("research workflow launch gate", () => {
  test("blocks task assembly while official Claude mode is active", () => {
    expect(workflowLaunchBlocker("official", "profile-1")).toBe("official-mode");
  });

  test("requires an active research engine in proxy mode", () => {
    expect(workflowLaunchBlocker("proxy", "")).toBe("missing-profile");
  });

  test("allows a configured proxy workflow", () => {
    expect(workflowLaunchBlocker("proxy", "profile-1")).toBe("");
  });
});

describe("research workflow pack readiness", () => {
  test("reports requested packs that were not applied", () => {
    expect(classifyWorkflowPackResult(
      ["bio-workflows", "bio-audit"],
      ["bio-audit"],
      [],
    ).missing).toEqual(["bio-workflows"]);
  });

  test("treats missing environment and install failures as blocking", () => {
    const result = classifyWorkflowPackResult(
      ["bio-crossmodal"],
      ["bio-crossmodal"],
      ["bio-drug 已勾选，但缺必填环境变量 CHEMBL_KEY，未装配", "bio-crossmodal: Skill 装配失败：disk full"],
    );
    expect(result.blockingWarnings).toHaveLength(2);
  });

  test("keeps non-blocking alias warnings visible without blocking launch", () => {
    const result = classifyWorkflowPackResult(
      ["bio-audit"],
      ["bio-audit"],
      ["bio-audit: 别名 pubmed 与既有 MCP 冲突，跳过"],
    );
    expect(result.blockingWarnings).toEqual([]);
    expect(result.warnings).toHaveLength(1);
  });
});
