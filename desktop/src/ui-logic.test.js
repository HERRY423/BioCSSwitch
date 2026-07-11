import { describe, expect, test } from "vitest";
import {
  CAP,
  classifyWorkflowPackResult,
  formatResearchBriefHandoff,
  isNativeAdapter,
  modelCapability,
  openaiCustomAnthropicBaseMessage,
  requiredWorkflowPacks,
  sourceHint,
  validateResearchQuestion,
  workflowLaunchBlocker,
  workflowSetupAction,
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

describe("research intent setup bridge", () => {
  test("keeps research intent while guiding first-time connection setup", () => {
    expect(workflowSetupAction("proxy", "", 0)).toBe("create-profile");
    expect(workflowSetupAction("proxy", "", 2)).toBe("activate-profile");
    expect(workflowSetupAction("proxy", "profile-1", 2)).toBe("launch");
    expect(workflowSetupAction("official", "profile-1", 2)).toBe("switch-to-proxy");
  });

  test("always includes the deterministic compiler exactly once", () => {
    expect(requiredWorkflowPacks(["bio-audit", "bio-compiler", "bio-audit"]))
      .toEqual(["bio-compiler", "bio-audit"]);
  });
});

describe("research brief intake and handoff", () => {
  test("rejects empty and underspecified research questions", () => {
    expect(validateResearchQuestion("   ").code).toBe("required");
    expect(validateResearchQuestion("EGFR?").code).toBe("too-short");
    expect(validateResearchQuestion("EGFR 在 GBM 中是否仍有可成药的靶点价值？").ok).toBe(true);
  });

  test("serializes a ready brief with an explicit task marker and audit JSON", () => {
    const brief = {
      schema_version: "research-brief/v1",
      status: "ready",
      workflow_hint: "crossmodal-discovery",
      brief_id: "rb_1234",
      raw_question: "EGFR 在 GBM 中是否仍有靶点价值？",
      resolved_context: { population_or_tissue: "GBM" },
    };
    const text = formatResearchBriefHandoff(brief, {
      evidenceMode: "rigorous",
      scope: "仅纳入 2020 年后的研究",
    });
    expect(text).toContain("BioCSSwitch-Task-ID: crossmodal-discovery");
    expect(text).toContain("Evidence-Mode: 严格核验");
    expect(text).toContain('"brief_id": "rb_1234"');
    expect(text).toContain("未查到视为未知");
  });

  test("refuses to hand off a draft with unresolved clarifications", () => {
    expect(() => formatResearchBriefHandoff({
      status: "needs_clarification",
      workflow_hint: "lit-review",
    })).toThrow(/finalized/);
  });
});
