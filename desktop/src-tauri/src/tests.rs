    use super::{
        assert_format_supported, build_get_config, build_list_templates, clear_profile_key_inner,
        create_profile_inner, decide_switch, delete_profile_inner, first_http_url,
        health_timeout_reason, is_main_list_model, key_env_for_adapter, key_fingerprint,
        merge_and_sort_models, nonactive_probe_verdict, normalize_version_tag,
        parse_host, parse_latest_release_json, probe_kind_for, probe_kind_for_model,
        proxy_args_for, proxy_fingerprint, redact,
        reject_openai_custom_anthropic_base, relay_missing_base_url, relay_missing_model,
        rollback_status_clause, sandbox_home, settings_change_needs_teardown,
        should_scratch_candidate, should_write_back, skip_scratch_verify,
        update_profile_connection_inner, update_profile_metadata_inner, upstream_host,
        version_is_newer, ConnectionEdit, SwitchOutcome,
    };
    use crate::config;

    /// 每个测试用独立临时 `.csswitch` 目录（进程 id + 线程 id + 随机后缀），互不干扰。
    fn tmpdir_lib() -> std::path::PathBuf {
        let base = std::env::temp_dir().join(format!("csswitch-lib-test-{}", std::process::id()));
        let d = base.join(format!(
            "{:?}-{}",
            std::thread::current().id(),
            config::new_id()
        ));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d.join(".csswitch")
    }

    // ---------- B2: proxy_args_for / assert_format_supported ----------
    #[test]
    fn proxy_args_derive_adapter_and_key_env() {
        use crate::config::Profile;
        let ds = Profile {
            template_id: "deepseek".into(),
            api_format: "anthropic".into(),
            base_url: "https://api.deepseek.com/anthropic".into(),
            api_key: "sk-ds".into(),
            ..Default::default()
        };
        let a = proxy_args_for(&ds);
        assert_eq!(a.adapter, "deepseek");
        assert_eq!(a.key_env, "DEEPSEEK_API_KEY");

        let glm = Profile {
            template_id: "glm".into(),
            api_format: "anthropic".into(),
            base_url: "https://open.bigmodel.cn/api/anthropic".into(),
            api_key: "gk".into(),
            model: "glm-5".into(),
            ..Default::default()
        };
        let b = proxy_args_for(&glm);
        assert_eq!(b.adapter, "relay");
        assert_eq!(b.key_env, "CSSWITCH_RELAY_KEY");
        assert_eq!(b.base_url, "https://open.bigmodel.cn/api/anthropic");
        assert_eq!(b.model, "glm-5");

        let custom_openai = Profile {
            template_id: "custom-openai".into(),
            api_format: "openai_chat".into(),
            base_url: "https://open.bigmodel.cn/api/paas/v4".into(),
            api_key: "ok".into(),
            model: "glm-4.5".into(),
            ..Default::default()
        };
        let c = proxy_args_for(&custom_openai);
        assert_eq!(c.adapter, "openai-custom");
        assert_eq!(c.key_env, "CSSWITCH_OPENAI_KEY");
        assert_eq!(c.base_url, "https://open.bigmodel.cn/api/paas/v4");
        assert_eq!(c.model, "glm-4.5");

        let custom_responses = Profile {
            template_id: "custom-openai-responses".into(),
            api_format: "openai_responses".into(),
            base_url: "https://api.openai.com/v1".into(),
            api_key: "ok".into(),
            model: "gpt-5.2".into(),
            ..Default::default()
        };
        let d = proxy_args_for(&custom_responses);
        assert_eq!(d.adapter, "openai-responses");
        assert_eq!(d.key_env, "CSSWITCH_OPENAI_KEY");
        assert_eq!(d.base_url, "https://api.openai.com/v1");
        assert_eq!(d.model, "gpt-5.2");
    }

    #[test]
    fn unsupported_api_format_is_rejected() {
        use crate::config::Profile;
        let p = Profile {
            template_id: "custom".into(),
            api_format: "gemini_native".into(),
            base_url: "https://x/y".into(),
            api_key: "k".into(),
            ..Default::default()
        };
        assert!(assert_format_supported(&p).is_err());
        let ok = Profile {
            api_format: "anthropic".into(),
            ..p.clone()
        };
        assert!(assert_format_supported(&ok).is_ok());
        let ok2 = Profile {
            api_format: "openai_chat".into(),
            ..p
        };
        assert!(assert_format_supported(&ok2).is_ok());
        let ok3 = Profile {
            api_format: "openai_responses".into(),
            ..ok2
        };
        assert!(assert_format_supported(&ok3).is_ok());
    }

    #[test]
    fn custom_openai_rejects_anthropic_base_url() {
        let err = reject_openai_custom_anthropic_base(
            "custom-openai",
            "https://api.moonshot.cn/anthropic",
        )
        .unwrap_err();
        assert!(err.contains("自定义 Anthropic"));
        assert!(
            reject_openai_custom_anthropic_base("custom-openai", "https://api.moonshot.cn/v1",)
                .is_ok()
        );
        assert!(reject_openai_custom_anthropic_base(
            "custom-openai-responses",
            "https://api.moonshot.cn/anthropic",
        )
        .is_err());
        assert!(
            reject_openai_custom_anthropic_base("custom", "https://api.moonshot.cn/anthropic",)
                .is_ok()
        );
    }

    #[test]
    fn key_env_for_adapter_maps_adapters() {
        assert_eq!(key_env_for_adapter("deepseek"), "DEEPSEEK_API_KEY");
        assert_eq!(key_env_for_adapter("qwen"), "DASHSCOPE_API_KEY");
        assert_eq!(key_env_for_adapter("openai-custom"), "CSSWITCH_OPENAI_KEY");
        assert_eq!(
            key_env_for_adapter("openai-responses"),
            "CSSWITCH_OPENAI_KEY"
        );
        assert_eq!(key_env_for_adapter("relay"), "CSSWITCH_RELAY_KEY");
        assert_eq!(key_env_for_adapter("anything-else"), "CSSWITCH_RELAY_KEY");
    }

    #[test]
    fn update_version_compare_handles_tags_and_padding() {
        assert_eq!(normalize_version_tag("v0.3.7"), "0.3.7");
        assert!(version_is_newer("v0.3.10", "0.3.9"));
        assert!(version_is_newer("0.4.0-beta.1", "0.3.99"));
        assert!(!version_is_newer("v0.3.6", "0.3.6"));
        assert!(!version_is_newer("0.3", "0.3.1"));
    }

    #[test]
    fn latest_release_json_parser_reads_tag_and_url() {
        let body = r#"{
            "tag_name": "v0.3.7",
            "name": "BioCSSwitch v0.3.7",
            "html_url": "https://github.com/HERRY423/BioCSSwitch/releases/tag/v0.3.7"
        }"#;
        let (tag, url, name) = parse_latest_release_json(body).unwrap();
        assert_eq!(tag, "v0.3.7");
        assert_eq!(
            url,
            "https://github.com/HERRY423/BioCSSwitch/releases/tag/v0.3.7"
        );
        assert_eq!(name.as_deref(), Some("BioCSSwitch v0.3.7"));
        assert!(parse_latest_release_json(r#"{"name":"missing tag"}"#).is_err());
    }

    #[test]
    fn proxy_fingerprint_includes_protocol_semantics() {
        use crate::config::Profile;
        let mut p = Profile {
            template_id: "kimi".into(),
            api_format: "anthropic".into(),
            base_url: "https://same.example/anthropic".into(),
            api_key: "same-key".into(),
            model: "same-model".into(),
            ..Default::default()
        };
        let kimi_launch = proxy_args_for(&p);
        let kimi_fp = proxy_fingerprint(&p, &kimi_launch);

        p.template_id = "custom".into();
        let custom_launch = proxy_args_for(&p);
        let custom_fp = proxy_fingerprint(&p, &custom_launch);
        assert_ne!(
            kimi_fp, custom_fp,
            "同 adapter/base/model/key 但模板语义不同，必须重启代理"
        );
    }

    // ---------- P1-c: 端口变更是否需拆链路（纯函数，4 组合） ----------
    #[test]
    fn settings_teardown_when_any_port_changes() {
        assert!(
            !settings_change_needs_teardown(18991, 18991, 8990, 8990),
            "端口未变 → 不拆链路"
        );
        assert!(
            settings_change_needs_teardown(18991, 19000, 8990, 8990),
            "代理端口变 → 拆（旧代理绑旧端口、沙箱烘旧 URL）"
        );
        assert!(
            settings_change_needs_teardown(18991, 18991, 8990, 9000),
            "沙箱端口变 → 拆（旧沙箱在旧端口成孤儿）"
        );
        assert!(
            settings_change_needs_teardown(18991, 19000, 8990, 9000),
            "都变 → 拆"
        );
    }

    // ---------- P2-e: 回滚措辞如实（恢复失败不得谎称已回滚） ----------
    #[test]
    fn rollback_clause_tells_truth_when_restore_failed() {
        assert!(
            rollback_status_clause(true).contains("已回滚"),
            "恢复成功 → 说已回滚"
        );
        let failed = rollback_status_clause(false);
        assert!(
            !failed.contains("已回滚到原配置"),
            "恢复失败不得谎称已回滚到原配置"
        );
        assert!(failed.contains("代理当前已停"), "如实说明代理已停");
    }

    // ---------- P2-d: 非 active「如实标记后保存」裁决（明确拒绝才拦；200=已校验；含糊/无响应=落盘但未校验） ----------
    #[test]
    fn nonactive_probe_verdict_maps_outcomes() {
        use crate::scratch::ProbeOutcome;
        assert!(
            nonactive_probe_verdict(&ProbeOutcome::Auth(401))
                .unwrap_err()
                .contains("401"),
            "401 明确鉴权失败 → 拦下不落盘"
        );
        assert!(
            nonactive_probe_verdict(&ProbeOutcome::ModelError(404))
                .unwrap_err()
                .contains("404"),
            "404 模型不被接受 → 拦下不落盘"
        );
        assert_eq!(
            nonactive_probe_verdict(&ProbeOutcome::Ok),
            Ok(true),
            "200 → 落盘且【已校验】"
        );
        assert_eq!(
            nonactive_probe_verdict(&ProbeOutcome::Ambiguous(Some(429))),
            Ok(false),
            "含糊(429) → best-effort 落盘但【未校验】"
        );
        assert_eq!(
            nonactive_probe_verdict(&ProbeOutcome::NoResponse),
            Ok(false),
            "无响应 → best-effort 落盘但【未校验】"
        );
    }

    // ---------- B3: 切换事务决策（纯函数，3 分支） ----------
    #[test]
    fn transaction_commits_only_when_healthy() {
        // scratch ok + real ok → 提交
        assert_eq!(decide_switch(true, true), SwitchOutcome::Commit);
        // scratch 校验失败 → 不起正式、不提交、旧态不动
        assert_eq!(decide_switch(false, false), SwitchOutcome::AbortBeforeStart);
        assert_eq!(decide_switch(false, true), SwitchOutcome::AbortBeforeStart);
        // scratch ok 但正式起/探活失败 → 杀候选、恢复旧、不提交
        assert_eq!(decide_switch(true, false), SwitchOutcome::RollbackToOld);
    }

    // ---------- MP-2 fix [3]: 写回门纯函数（gen 同/异 × secret 同/异 4 组合） ----------
    #[test]
    fn should_write_back_requires_both_gen_and_secret() {
        // gen 同 + secret 同 → 写回（合法启动，未被取代）
        assert!(should_write_back(5, 5, "sekret", "sekret"));
        // gen 同 + secret 异 → 不写回（被并发另起用不同 secret 占了槽，冷启动双起窄窗）
        assert!(!should_write_back(5, 5, "other", "sekret"));
        // gen 异 + secret 同 → 不写回（被清 key/停/切 bump 取代）
        assert!(!should_write_back(5, 6, "sekret", "sekret"));
        // gen 异 + secret 异 → 不写回
        assert!(!should_write_back(5, 6, "other", "sekret"));
    }

    // ---------- MP-2 fix [1]: 连接编辑 validate-before-persist 的字段应用逻辑（内存/落盘共用） ----------
    #[test]
    fn connection_edit_apply_only_changes_provided_fields() {
        use crate::config::Profile;
        let mut p = Profile {
            base_url: "old-url".into(),
            api_format: "anthropic".into(),
            model: "old-model".into(),
            api_key: "old-key".into(),
            ..Default::default()
        };
        let edit = ConnectionEdit {
            base_url: Some("new-url".into()),
            api_format: None, // None = 不改
            model: Some("new-model".into()),
            key: Some(String::new()), // 空 key = 不改（留占位不覆盖已存 key）
        };
        edit.apply(&mut p);
        assert_eq!(p.base_url, "new-url");
        assert_eq!(p.api_format, "anthropic", "None 字段不改");
        assert_eq!(p.model, "new-model");
        assert_eq!(p.api_key, "old-key", "空 key 不覆盖已存 key");

        // 非空 key 覆盖；其余 None 不动。
        let edit2 = ConnectionEdit {
            key: Some("new-key".into()),
            ..Default::default()
        };
        edit2.apply(&mut p);
        assert_eq!(p.api_key, "new-key", "非空 key 覆盖");
        assert_eq!(p.base_url, "new-url", "None 字段不改");
        assert_eq!(p.model, "new-model", "None 字段不改");
    }

    // ---------- B4: profile CRUD *_inner ----------
    #[test]
    fn create_profile_from_template_prefills() {
        let d = tmpdir_lib();
        let id =
            create_profile_inner(&d, "glm", "我的 GLM", Some("gk"), None, Some("glm-5.2")).unwrap();
        let cfg = config::load_from(&d).unwrap();
        let p = cfg.profile_by_id(&id).unwrap();
        assert_eq!(p.template_id, "glm");
        assert_eq!(p.name, "我的 GLM");
        assert_eq!(p.api_format, "anthropic");
        assert_eq!(p.base_url, "https://open.bigmodel.cn/api/anthropic");
        assert_eq!(p.api_key, "gk");
        assert_eq!(cfg.active_id, "", "新建不自动生效");
    }

    #[test]
    fn create_relay_without_model_is_rejected() {
        // 修 #9 P1-a：后端命令层直接创建 relay/自定义端点空 model 也被拦（不变量不可绕过）。
        let d = tmpdir_lib();
        let e = create_profile_inner(&d, "glm", "GLM", Some("gk"), None, None);
        assert!(e.is_err(), "relay 空 model 应拒绝创建");
        assert!(e.unwrap_err().contains("模型"));
        // native 不受约束（model 可空）。
        assert!(create_profile_inner(&d, "deepseek", "DS", Some("gk"), None, None).is_ok());
    }

    #[test]
    fn update_metadata_does_not_touch_key() {
        let d = tmpdir_lib();
        let id =
            create_profile_inner(&d, "glm", "GLM", Some("secret9"), None, Some("glm-5.2")).unwrap();
        update_profile_metadata_inner(&d, &id, "改名", Some("备注")).unwrap();
        let cfg = config::load_from(&d).unwrap();
        let p = cfg.profile_by_id(&id).unwrap();
        assert_eq!(p.name, "改名");
        assert_eq!(p.notes.as_deref(), Some("备注"));
        assert_eq!(p.api_key, "secret9", "元数据编辑不动 key");
    }

    #[test]
    fn clear_key_empties_key_and_drops_backup() {
        let d = tmpdir_lib();
        let id = create_profile_inner(&d, "glm", "GLM", Some("secretTAIL"), None, Some("glm-5.2"))
            .unwrap();
        config::write_rolling_backup(&d).ok();
        clear_profile_key_inner(&d, &id).unwrap();
        let cfg = config::load_from(&d).unwrap();
        assert_eq!(cfg.profile_by_id(&id).unwrap().api_key, "");
        assert!(!d.join("config.json.bak").exists(), "清 key 后净化滚动备份");
    }

    #[test]
    fn delete_active_clears_active() {
        let d = tmpdir_lib();
        let id = create_profile_inner(&d, "glm", "GLM", Some("k"), None, Some("glm-5.2")).unwrap();
        config::update(&d, |c| c.active_id = id.clone()).unwrap();
        delete_profile_inner(&d, &id).unwrap();
        let cfg = config::load_from(&d).unwrap();
        assert!(cfg.profile_by_id(&id).is_none());
        assert_eq!(cfg.active_id, "", "删 active → 置空");
    }

    #[test]
    fn update_connection_rejects_unsupported_format() {
        let d = tmpdir_lib();
        let id =
            create_profile_inner(&d, "custom", "C", None, Some("https://x/y"), Some("m")).unwrap();
        let e = update_profile_connection_inner(
            &d,
            &id,
            Some("https://x/y"),
            Some("gemini_native"),
            None,
            None,
        );
        assert!(e.is_err());
    }

    // ---------- MP-2 Minor [4]: 未命中 id → Err（不静默 Ok） ----------
    #[test]
    fn update_metadata_unknown_id_errors() {
        let d = tmpdir_lib();
        create_profile_inner(&d, "glm", "GLM", Some("k"), None, Some("glm-5.2")).unwrap();
        let e = update_profile_metadata_inner(&d, "no-such-id", "改名", None);
        assert!(e.is_err(), "未命中 id 应报错，而非静默成功");
        assert!(e.unwrap_err().contains("找不到 profile"));
    }

    #[test]
    fn update_connection_unknown_id_errors() {
        let d = tmpdir_lib();
        create_profile_inner(&d, "glm", "GLM", Some("k"), None, Some("glm-5.2")).unwrap();
        let e = update_profile_connection_inner(
            &d,
            "no-such-id",
            Some("https://x/y"),
            None,
            None,
            None,
        );
        assert!(e.is_err(), "未命中 id 应报错，而非静默成功");
        assert!(e.unwrap_err().contains("找不到 profile"));
    }

    // ---------- B5: build_get_config / build_list_templates ----------
    #[test]
    fn get_config_masks_keys_and_lists_profiles() {
        let d = tmpdir_lib();
        let id = create_profile_inner(
            &d,
            "glm",
            "GLM",
            Some("sk-longsecret9999"),
            None,
            Some("glm-5.2"),
        )
        .unwrap();
        let v = build_get_config(&d).unwrap();
        assert_eq!(v["schema_version"], 2);
        let arr = v["profiles"].as_array().unwrap();
        let p = arr.iter().find(|p| p["id"] == id).unwrap();
        assert!(p["key"].as_str().unwrap().ends_with("9999"));
        assert!(
            !p["key"].as_str().unwrap().contains("longsecret"),
            "只回掩码"
        );
        assert!(
            p.get("api_key").is_none() || p["api_key"].is_null(),
            "全 key 不出后端"
        );
    }

    #[test]
    fn get_config_returns_notes_so_rename_does_not_wipe_them() {
        // M1 回归：build_get_config 必须回传 notes，否则前端读到空、下次改名把备注静默清掉。
        let d = tmpdir_lib();
        let id = create_profile_inner(&d, "glm", "GLM", Some("k"), None, Some("glm-5.2")).unwrap();
        update_profile_metadata_inner(&d, &id, "GLM", Some("我的备注")).unwrap();
        let v = build_get_config(&d).unwrap();
        let p = v["profiles"]
            .as_array()
            .unwrap()
            .iter()
            .find(|p| p["id"] == id)
            .unwrap();
        assert_eq!(p["notes"], "我的备注", "notes 必须随 get_config 回传");
    }

    #[test]
    fn list_templates_has_eleven() {
        let v = build_list_templates();
        assert_eq!(v.len(), 11);
        assert!(v.iter().any(|t| t["id"] == "custom"));
        assert!(v.iter().any(|t| t["id"] == "custom-openai"));
        assert!(v.iter().any(|t| t["id"] == "custom-openai-responses"));
        assert!(v.iter().any(|t| t["id"] == "kimi"));
        assert!(v.iter().any(|t| t["id"] == "minimax"));
    }

    // ---------- 既有纯逻辑不变量（保留） ----------
    #[test]
    fn first_http_url_takes_only_first_valid_url() {
        let multi = "http://127.0.0.1:8990/setup?nonce=abc123\n\
                     This is a single-use link, expires in 60 seconds.";
        assert_eq!(
            first_http_url(multi).as_deref(),
            Some("http://127.0.0.1:8990/setup?nonce=abc123"),
        );
        let inline = "https://x.example/y?z=1  (single-use)";
        assert_eq!(
            first_http_url(inline).as_deref(),
            Some("https://x.example/y?z=1")
        );
        let lead = "Open this link in your browser:\nhttp://127.0.0.1:8990/a";
        assert_eq!(
            first_http_url(lead).as_deref(),
            Some("http://127.0.0.1:8990/a")
        );
        assert_eq!(first_http_url("no url here\nnor here"), None);
        assert_eq!(
            first_http_url("http://127.0.0.1:8990").as_deref(),
            Some("http://127.0.0.1:8990")
        );
    }

    #[test]
    fn parse_host_extracts_host_from_relay_base_url() {
        assert_eq!(
            parse_host("https://byteswarm.ai/claude").as_deref(),
            Some("byteswarm.ai")
        );
        assert_eq!(
            parse_host("http://127.0.0.1:8080/v1").as_deref(),
            Some("127.0.0.1")
        );
        assert_eq!(
            parse_host("https://relay.example.com:8443").as_deref(),
            Some("relay.example.com")
        );
        assert_eq!(parse_host("byteswarm.ai/claude"), None);
        assert_eq!(parse_host(""), None);
    }

    #[test]
    fn upstream_host_by_adapter() {
        assert_eq!(upstream_host("deepseek", ""), "api.deepseek.com");
        assert_eq!(upstream_host("qwen", ""), "dashscope.aliyuncs.com");
        assert_eq!(
            upstream_host("openai-custom", "https://open.bigmodel.cn/api/paas/v4"),
            "open.bigmodel.cn"
        );
        assert_eq!(
            upstream_host("relay", "https://open.bigmodel.cn/api/anthropic"),
            "open.bigmodel.cn"
        );
        assert_eq!(upstream_host("", ""), "", "无生效配置 → 空（灯显黄）");
    }

    #[test]
    fn main_list_model_matches_family_plus_digit() {
        assert!(is_main_list_model("claude-opus-4-8"));
        assert!(is_main_list_model("claude-sonnet-5"));
        assert!(is_main_list_model("claude-haiku-4-5-20251001"));
        assert!(!is_main_list_model("claude-3-5-sonnet-20241022"));
        assert!(!is_main_list_model("claude-fable-5"));
        assert!(!is_main_list_model("gpt-4o"));
    }

    #[test]
    fn redact_scrubs_secret_and_is_noop_when_empty() {
        assert_eq!(
            redact("推理指向 http://127.0.0.1:18991/abcd1234 尾巴", "abcd1234"),
            "推理指向 http://127.0.0.1:18991/**** 尾巴"
        );
        assert_eq!(redact("原样返回", ""), "原样返回");
        assert!(!redact("leak abcd1234 leak abcd1234", "abcd1234").contains("abcd1234"));
    }

    #[test]
    fn key_fingerprint_stable_and_distinct() {
        assert_eq!(key_fingerprint("sk-aaaa"), key_fingerprint("sk-aaaa"));
        assert_ne!(key_fingerprint("sk-aaaa"), key_fingerprint("sk-bbbb"));
        assert_ne!(key_fingerprint(""), key_fingerprint("x"));
    }

    #[test]
    fn sandbox_home_is_writable_under_config_dir() {
        let h = sandbox_home();
        assert!(h.ends_with("sandbox/home"), "应以 sandbox/home 结尾：{h:?}");
        assert!(
            h.to_string_lossy().contains(".csswitch"),
            "应在 .csswitch 下：{h:?}"
        );
    }

    #[test]
    fn merge_and_sort_prefers_tools_then_dedupes_builtin() {
        let live = vec![
            ("m-notools".to_string(), Some(false)),
            ("m-tools".to_string(), Some(true)),
            ("m-unknown".to_string(), None),
        ];
        let out = merge_and_sort_models(live, &["m-tools", "m-builtin-only"]);
        let ids: Vec<String> = out
            .iter()
            .map(|v| v.get("id").unwrap().as_str().unwrap().to_string())
            .collect();
        assert_eq!(ids[0], "m-tools");
        assert!(ids.contains(&"m-builtin-only".to_string()));
        assert_eq!(ids.iter().filter(|i| *i == "m-tools").count(), 1, "去重");
        assert_eq!(ids.last().unwrap(), "m-notools");
    }

    #[test]
    fn probe_kind_picks_message_when_model_set() {
        assert!(matches!(
            probe_kind_for_model("mimo-v2.5-pro"),
            crate::scratch::ProbeKind::Message
        ));
        assert!(matches!(
            probe_kind_for_model(""),
            crate::scratch::ProbeKind::Models
        ));
    }

    // ---------- 修真机 P1：native adapter 上游校验（GPT 验收报告 RM-06） ----------

    #[test]
    fn native_probe_uses_message_since_native_models_is_static() {
        // native 的 /v1/models 是静态列表、探不出坏 key，故一律用 Message（打上游 /v1/messages）。
        assert!(matches!(
            probe_kind_for("deepseek", ""),
            crate::scratch::ProbeKind::Message
        ));
        assert!(matches!(
            probe_kind_for("qwen", ""),
            crate::scratch::ProbeKind::Message
        ));
        // relay：空 model 用 Models（/v1/models 回源即验鉴权）；选了 model 用 Message 验该模型。
        assert!(matches!(
            probe_kind_for("relay", ""),
            crate::scratch::ProbeKind::Models
        ));
        assert!(matches!(
            probe_kind_for("relay", "m1"),
            crate::scratch::ProbeKind::Message
        ));
    }

    #[test]
    fn native_adapter_no_longer_bypasses_upstream_verify() {
        // 只有显式 skip_verify 才跳过；native 不再是豁免条件（旧行为的核心漏洞）。
        assert!(
            !skip_scratch_verify(true, false),
            "native 不得再豁免上游校验"
        );
        assert!(!skip_scratch_verify(false, false));
        assert!(skip_scratch_verify(false, true), "显式 skip_verify 才跳");
        assert!(skip_scratch_verify(true, true));
    }

    #[test]
    fn native_candidate_is_upstream_validated_even_without_base_url() {
        // 非 active 编辑：native 即便 base_url 空也要验（走硬编码官方端点）。
        assert!(should_scratch_candidate("deepseek", "sk-x", ""));
        assert!(should_scratch_candidate("qwen", "sk-x", ""));
        // relay 仍需 base_url；空 key 一律免验。
        assert!(!should_scratch_candidate("relay", "sk-x", ""));
        assert!(should_scratch_candidate("relay", "sk-x", "https://r"));
        assert!(!should_scratch_candidate("deepseek", "", ""));
    }

    #[test]
    fn relay_empty_base_url_is_rejected_before_save() {
        // 修 P2：relay/自定义端点空（或纯空白）base_url → 拦下，不落盘。
        assert!(relay_missing_base_url("relay", ""));
        assert!(relay_missing_base_url("glm", "   "));
        assert!(relay_missing_base_url("custom", ""));
        // 带地址的 relay 放行。
        assert!(!relay_missing_base_url("relay", "https://r"));
        // native 走硬编码端点，空 base_url 无妨 → 不拦。
        assert!(!relay_missing_base_url("deepseek", ""));
        assert!(!relay_missing_base_url("qwen", ""));
    }

    #[test]
    fn relay_empty_model_is_rejected() {
        // 修 #9 P1-a：relay/自定义端点空（或纯空白）model → 拦下（无 model 则无 force → 退回 passthrough）。
        assert!(relay_missing_model("relay", ""));
        assert!(relay_missing_model("glm", "   "));
        assert!(relay_missing_model("custom", ""));
        assert!(!relay_missing_model("relay", "glm-5.2"));
        // native 走内置映射/硬编码端点，model 可空 → 不拦。
        assert!(!relay_missing_model("deepseek", ""));
        assert!(!relay_missing_model("qwen", ""));
    }

    #[test]
    fn health_timeout_reason_flags_port_conflict_and_never_blames_key() {
        // 端口占用：明确报占用、带端口号，绝不提「key 无效」。
        let occ = health_timeout_reason(18991, "OSError: [Errno 48] Address already in use");
        assert!(occ.contains("18991"));
        assert!(occ.contains("占用"), "应明确报端口占用：{occ}");
        assert!(!occ.contains("key"), "端口占用不该扯上 key：{occ}");
        // 其它探活失败（依赖缺失等）：本地探活与 key 有效性无关，不得说「key 无效」。
        let generic = health_timeout_reason(18991, "ModuleNotFoundError: No module named 'x'");
        assert!(
            !generic.contains("key 无效"),
            "本地探活超时与 key 有效性无关：{generic}"
        );
    }
