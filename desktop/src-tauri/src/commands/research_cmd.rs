const MAX_RESEARCH_QUESTION_CHARS: usize = 4_000;
const MAX_RESEARCH_RPC_BYTES: usize = 256 * 1024;
const RESEARCH_BRIEF_SCHEMA: &str = "biocsswitch/research-brief/1";
const RESEARCH_BRIEF_SCHEMA_VERSION: u64 = 1;
const COMPILE_RESEARCH_TOOL: &str = "compile_research_question";
const FINALIZE_RESEARCH_TOOL: &str = "finalize_research_brief";
const RESEARCH_WORKFLOW_ALLOWLIST: &[&str] = &[
    "lit-review",
    "omics-code",
    "experimental-design",
    "crossmodal-discovery",
];

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CompileResearchBriefReq {
    question: String,
    workflow_hint: String,
    #[serde(default = "default_research_language")]
    language: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FinalizeResearchBriefReq {
    draft: serde_json::Value,
    #[serde(default)]
    answers: std::collections::BTreeMap<String, String>,
}

fn default_research_language() -> String {
    "zh".to_string()
}

/// Validate the only free-text compile inputs before they reach the compiler process.
/// Error messages deliberately describe the field, never echo its value.
fn validate_research_question(question: &str) -> Result<(), String> {
    if question.trim().is_empty() {
        return Err("研究问题不能为空。".into());
    }
    if question.chars().count() > MAX_RESEARCH_QUESTION_CHARS {
        return Err(format!(
            "研究问题过长；最多允许 {MAX_RESEARCH_QUESTION_CHARS} 个字符。"
        ));
    }
    Ok(())
}

fn validate_research_workflow(workflow: &str) -> Result<(), String> {
    if !RESEARCH_WORKFLOW_ALLOWLIST.contains(&workflow) {
        return Err("workflow 不在允许的科研任务清单中。".into());
    }
    Ok(())
}

fn validate_research_language(language: &str) -> Result<(), String> {
    if !matches!(language, "zh" | "en") {
        return Err("language 仅允许 zh 或 en。".into());
    }
    Ok(())
}

fn validate_compile_research_req(req: &CompileResearchBriefReq) -> Result<(), String> {
    validate_research_question(&req.question)?;
    validate_research_workflow(&req.workflow_hint)?;
    validate_research_language(&req.language)
}

fn validate_research_brief_draft(draft: &serde_json::Value) -> Result<(), String> {
    if draft.get("schema").and_then(|v| v.as_str()) != Some(RESEARCH_BRIEF_SCHEMA)
        || draft.get("schema_version").and_then(|v| v.as_u64())
            != Some(RESEARCH_BRIEF_SCHEMA_VERSION)
    {
        return Err("任务书 schema 或 schema_version 无效。".into());
    }
    let question = draft
        .get("raw_question")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "任务书缺少 raw_question。".to_string())?;
    let workflow = draft
        .get("workflow_hint")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "任务书缺少 workflow_hint。".to_string())?;
    let language = draft
        .get("language")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "任务书缺少 language。".to_string())?;
    validate_research_question(question)?;
    validate_research_workflow(workflow)?;
    validate_research_language(language)
}

/// Build the exact newline-delimited JSON-RPC record written to stdin and cap that complete
/// record, not merely the user-controlled subfield. The payload is never placed in argv.
fn encode_research_mcp_call(tool: &str, arguments: serde_json::Value) -> Result<Vec<u8>, String> {
    let request = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": arguments,
        },
    });
    let mut encoded =
        serde_json::to_vec(&request).map_err(|_| "无法序列化研究编译请求。".to_string())?;
    encoded.push(b'\n');
    if encoded.len() > MAX_RESEARCH_RPC_BYTES {
        return Err(format!(
            "研究编译请求过大；序列化后最多允许 {} KiB。",
            MAX_RESEARCH_RPC_BYTES / 1024
        ));
    }
    Ok(encoded)
}

/// Parse only the documented MCP result shape. Protocol errors and malformed output are
/// intentionally summarized without copying Python stderr, JSON-RPC error messages, or payloads
/// back to the UI, because any of those may contain the submitted research text.
fn parse_research_mcp_response(stdout: &[u8]) -> Result<serde_json::Value, String> {
    let text = std::str::from_utf8(stdout)
        .map_err(|_| "研究问题编译器返回了非 UTF-8 数据。".to_string())?;
    let mut lines = text.lines().filter(|line| !line.trim().is_empty());
    let line = lines
        .next()
        .ok_or_else(|| "研究问题编译器没有返回结果。".to_string())?;
    if lines.next().is_some() {
        return Err("研究问题编译器返回了多条意外响应。".into());
    }

    let envelope: serde_json::Value =
        serde_json::from_str(line).map_err(|_| "无法解析研究问题编译器响应。".to_string())?;
    if envelope.get("jsonrpc").and_then(|v| v.as_str()) != Some("2.0")
        || envelope.get("id").and_then(|v| v.as_u64()) != Some(1)
    {
        return Err("研究问题编译器返回了无效的 JSON-RPC 信封。".into());
    }
    if envelope.get("error").is_some() {
        return Err("研究问题编译器拒绝了该请求。".into());
    }

    let result = envelope
        .get("result")
        .ok_or_else(|| "研究问题编译器响应缺少 result。".to_string())?;
    if result.get("isError").and_then(|v| v.as_bool()) == Some(true) {
        return Err("研究问题编译器未能生成任务书。".into());
    }
    let payload = result
        .get("content")
        .and_then(|v| v.as_array())
        .and_then(|items| items.first())
        .and_then(|item| item.get("text"))
        .and_then(|v| v.as_str())
        .ok_or_else(|| "研究问题编译器响应缺少 content[0].text。".to_string())?;
    let parsed: serde_json::Value = serde_json::from_str(payload)
        .map_err(|_| "研究问题编译器返回的任务书不是有效 JSON。".to_string())?;
    if !parsed.is_object() {
        return Err("研究问题编译器返回的任务书不是 JSON 对象。".into());
    }
    Ok(parsed)
}

fn run_research_compiler(
    app: &tauri::AppHandle,
    encoded_request: &[u8],
) -> Result<serde_json::Value, String> {
    let root = asset_root(app)
        .ok_or("找不到研究问题编译器资源 packs/bio-compiler/question_compiler_server.py。")?;
    let script = root.join("packs/bio-compiler/question_compiler_server.py");
    if !script.is_file() {
        return Err(
            "找不到研究问题编译器资源 packs/bio-compiler/question_compiler_server.py。".into(),
        );
    }
    let python = proc::find_exe("python3")
        .ok_or("缺少依赖 python3（运行离线研究问题编译器需要）；请安装 Python 3.10 或更高版本。")?;
    proc::check_python_version(&python)?;

    // Direct exec only: the script path is the sole argv payload. Research text is written as one
    // JSON-RPC line to stdin, then stdin is explicitly closed so the stdio MCP server exits.
    let mut child = Command::new(&python)
        .arg(&script)
        .current_dir(&root)
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("无法启动研究问题编译器：{e}"))?;

    let write_result = child
        .stdin
        .take()
        .ok_or_else(|| "无法打开研究问题编译器 stdin。".to_string())
        .and_then(|mut stdin| {
            std::io::Write::write_all(&mut stdin, encoded_request)
                .map_err(|_| "无法向研究问题编译器写入请求。".to_string())?;
            std::io::Write::flush(&mut stdin)
                .map_err(|_| "无法刷新研究问题编译器请求。".to_string())
        });
    if let Err(err) = write_result {
        let _ = child.kill();
        let _ = child.wait();
        return Err(err);
    }

    let output = child
        .wait_with_output()
        .map_err(|e| format!("等待研究问题编译器失败：{e}"))?;
    if !output.status.success() {
        return Err(match output.status.code() {
            Some(code) => format!("研究问题编译器运行失败（退出码 {code}）。"),
            None => "研究问题编译器被系统终止。".to_string(),
        });
    }
    parse_research_mcp_response(&output.stdout)
}

#[tauri::command]
fn compile_research_brief(
    app: tauri::AppHandle,
    req: CompileResearchBriefReq,
) -> Result<serde_json::Value, String> {
    validate_compile_research_req(&req)?;
    let arguments = json!({
        "question": req.question,
        "language": req.language,
        "workflow_hint": req.workflow_hint,
    });
    let encoded = encode_research_mcp_call(COMPILE_RESEARCH_TOOL, arguments)?;
    let brief = run_research_compiler(&app, &encoded)?;
    validate_research_brief_draft(&brief)?;
    Ok(brief)
}

#[tauri::command]
fn finalize_research_brief(
    app: tauri::AppHandle,
    req: FinalizeResearchBriefReq,
) -> Result<serde_json::Value, String> {
    let arguments = json!({
        "draft": req.draft,
        "answers": req.answers,
    });
    // Encode (and therefore enforce the complete 256 KiB stdin cap) before inspecting nested
    // fields, so oversized attacker-controlled drafts fail at the outermost boundary first.
    let encoded = encode_research_mcp_call(FINALIZE_RESEARCH_TOOL, arguments.clone())?;
    validate_research_brief_draft(
        arguments
            .get("draft")
            .expect("finalize arguments always contain draft"),
    )?;
    let brief = run_research_compiler(&app, &encoded)?;
    validate_research_brief_draft(&brief)?;
    Ok(brief)
}

#[cfg(test)]
mod research_cmd_tests {
    use super::*;

    fn compile_req(question: &str, workflow: &str, language: &str) -> CompileResearchBriefReq {
        CompileResearchBriefReq {
            question: question.to_string(),
            workflow_hint: workflow.to_string(),
            language: language.to_string(),
        }
    }

    fn valid_draft() -> serde_json::Value {
        json!({
            "schema": RESEARCH_BRIEF_SCHEMA,
            "schema_version": RESEARCH_BRIEF_SCHEMA_VERSION,
            "raw_question": "EGFR 在 GBM 中是否仍有靶点价值？",
            "workflow_hint": "crossmodal-discovery",
            "language": "zh",
        })
    }

    #[test]
    fn compile_validation_accepts_allowlisted_workflows_and_languages() {
        assert!(validate_compile_research_req(&compile_req(
            "EGFR 在 GBM 中是否仍有靶点价值？",
            "crossmodal-discovery",
            "zh"
        ))
        .is_ok());
        assert!(validate_compile_research_req(&compile_req(
            "Does EGFR retain target value in GBM?",
            "lit-review",
            "en"
        ))
        .is_ok());
    }

    #[test]
    fn compile_request_accepts_workflow_hint_on_the_wire() {
        let req: CompileResearchBriefReq = serde_json::from_value(json!({
            "question": "Does EGFR retain target value in GBM?",
            "workflow_hint": "crossmodal-discovery",
            "language": "en",
        }))
        .unwrap();
        assert_eq!(req.workflow_hint, "crossmodal-discovery");
        assert!(validate_compile_research_req(&req).is_ok());
    }

    #[test]
    fn compile_validation_rejects_untrusted_fields_without_echoing_them() {
        let oversized = "问".repeat(MAX_RESEARCH_QUESTION_CHARS + 1);
        let err =
            validate_compile_research_req(&compile_req(&oversized, "crossmodal-discovery", "zh"))
                .unwrap_err();
        assert!(!err.contains(&oversized));

        let workflow = "not-allowed-and-sensitive";
        let err = validate_compile_research_req(&compile_req("safe", workflow, "zh")).unwrap_err();
        assert!(!err.contains(workflow));
        assert!(
            validate_compile_research_req(&compile_req("safe", "target-discovery", "zh")).is_err()
        );

        let language = "zh-with-private-text";
        let err = validate_compile_research_req(&compile_req("safe", "lit-review", language))
            .unwrap_err();
        assert!(!err.contains(language));
    }

    #[test]
    fn encoded_call_caps_the_complete_stdin_record() {
        let ok = encode_research_mcp_call(
            COMPILE_RESEARCH_TOOL,
            json!({"question": "q", "language": "zh", "workflow_hint": "lit-review"}),
        )
        .unwrap();
        assert!(ok.ends_with(b"\n"));
        assert!(ok.len() <= MAX_RESEARCH_RPC_BYTES);
        let envelope: serde_json::Value = serde_json::from_slice(&ok).unwrap();
        assert_eq!(envelope["method"], "tools/call");
        assert_eq!(envelope["params"]["name"], COMPILE_RESEARCH_TOOL);
        assert_eq!(
            envelope["params"]["arguments"]["workflow_hint"],
            "lit-review"
        );

        let too_large = "x".repeat(MAX_RESEARCH_RPC_BYTES);
        assert!(encode_research_mcp_call(
            FINALIZE_RESEARCH_TOOL,
            json!({"draft": {"schema": RESEARCH_BRIEF_SCHEMA, "schema_version": RESEARCH_BRIEF_SCHEMA_VERSION}, "answers": {"x": too_large}}),
        )
        .is_err());
    }

    #[test]
    fn finalize_requires_the_exact_brief_schema() {
        assert!(validate_research_brief_draft(&valid_draft()).is_ok());
        assert!(validate_research_brief_draft(&json!({
            "schema": "biocsswitch/research-brief/2",
            "schema_version": RESEARCH_BRIEF_SCHEMA_VERSION,
        }))
        .is_err());
        assert!(validate_research_brief_draft(&json!({
            "schema": RESEARCH_BRIEF_SCHEMA,
            "schema_version": "1",
        }))
        .is_err());
        assert!(validate_research_brief_draft(&json!({})).is_err());
    }

    #[test]
    fn finalize_revalidates_question_workflow_and_language_guards() {
        let mut draft = valid_draft();
        draft["raw_question"] = json!("x".repeat(MAX_RESEARCH_QUESTION_CHARS + 1));
        assert!(validate_research_brief_draft(&draft).is_err());

        let mut draft = valid_draft();
        draft["workflow_hint"] = json!("target-discovery");
        assert!(validate_research_brief_draft(&draft).is_err());

        let mut draft = valid_draft();
        draft["language"] = json!("fr");
        assert!(validate_research_brief_draft(&draft).is_err());
    }

    #[test]
    fn parses_content_zero_text_as_json() {
        let stdout = br#"{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"schema\":\"biocsswitch/research-brief/1\",\"schema_version\":1,\"gaps\":[]}"}],"isError":false}}
"#;
        let parsed = parse_research_mcp_response(stdout).unwrap();
        assert_eq!(
            parsed.get("schema").and_then(|v| v.as_str()),
            Some(RESEARCH_BRIEF_SCHEMA)
        );
        assert_eq!(
            parsed.get("schema_version").and_then(|v| v.as_u64()),
            Some(RESEARCH_BRIEF_SCHEMA_VERSION)
        );
    }

    #[test]
    fn protocol_errors_never_echo_python_error_text() {
        let sensitive = "完整且不应回显的研究问题";
        let stdout = format!(
            r#"{{"jsonrpc":"2.0","id":1,"error":{{"code":-32000,"message":"{sensitive}"}}}}
"#
        );
        let err = parse_research_mcp_response(stdout.as_bytes()).unwrap_err();
        assert!(!err.contains(sensitive));
    }
}
