//! BioCSSwitch 桌面 app 后端（进程管家）。
//!
//! 职责：管理「翻译代理」与「沙箱 Science」两个子进程的生命周期；读写
//! `~/.csswitch/config.json`（多 profile 形态）；把第三方 key 以【环境变量】注入代理子进程
//! （绝不进 argv）；探活；把沙箱 URL 交系统浏览器打开。已验证的越权/翻译逻辑仍留在
//! Python/Node/shell 里被当作子进程调用，以保住铁律护栏与已验证行为。
//!
//! 运行行为由生效 profile 的 `template_id` 经 [`templates`] 注册表派生出 adapter
//! （deepseek | qwen | relay | openai-custom | openai-responses），再传给 python 代理 `--provider`。
//!
//! 铁律相关：key 只在内存与 0600 的 config.json；回显前端只给掩码；沙箱端口/目录护栏
//! 由被调脚本负责（对 8765 与真实目录失败关闭）；退 app 默认停代理、保留沙箱。

mod config;
mod config_legacy;
mod lifecycle;
mod netcanon;
mod oauth_forge;
mod packs;
mod proc;
#[allow(dead_code)]
mod proxy;
mod scratch;
mod state;
mod templates;
mod verification;

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use serde::Deserialize;
use serde_json::json;
use state::{key_fingerprint, kill_child, lock, AppState};
use tauri::{Manager, State};

const SCIENCE_BIN: &str = "/Applications/Claude Science.app/Contents/Resources/bin/claude-science";

// ---------- adapter / profile 运行元信息 ----------
/// adapter → 该 adapter 期望的 key 环境变量名（python 代理侧 PROVIDERS[...]["key_env"]）。
fn key_env_for_adapter(adapter: &str) -> &'static str {
    match adapter {
        "deepseek" => "DEEPSEEK_API_KEY",
        "qwen" => "DASHSCOPE_API_KEY",
        "openai-custom" | "openai-responses" => "CSSWITCH_OPENAI_KEY",
        _ => "CSSWITCH_RELAY_KEY", // relay / 兜底
    }
}

/// 从一条 profile 派生出起代理需要的全部参数（纯函数，便于测试）。
struct ProxyLaunch {
    adapter: String,
    base_url: String,
    model: String,
    key: String,
    key_env: &'static str,
    thinking_policy: &'static str,
}

fn proxy_args_for(p: &config::Profile) -> ProxyLaunch {
    let adapter = templates::adapter_for(&p.template_id).to_string();
    let key_env = key_env_for_adapter(&adapter);
    ProxyLaunch {
        adapter,
        base_url: p.base_url.clone(),
        model: p.model.clone(),
        key: p.api_key.clone(),
        key_env,
        thinking_policy: templates::thinking_policy_for(&p.template_id),
    }
}

fn proxy_fingerprint(p: &config::Profile, launch: &ProxyLaunch) -> u64 {
    key_fingerprint(&format!(
        "{}\n{}\n{}\n{}\n{}\n{}\n{}",
        p.template_id,
        p.api_format,
        launch.adapter,
        launch.base_url,
        launch.model,
        launch.thinking_policy,
        launch.key
    ))
}

/// 本轨支持 anthropic / openai_chat / openai_responses；其余进 schema 但激活拒绝（待轨道 2：Rust 代理）。
fn assert_format_supported(p: &config::Profile) -> Result<(), String> {
    match p.api_format.as_str() {
        "anthropic" | "openai_chat" | "openai_responses" => Ok(()),
        other => Err(format!(
            "api_format `{other}` 暂不支持（待 Rust 代理），请选 anthropic、openai_chat 或 openai_responses。"
        )),
    }
}

fn looks_like_anthropic_endpoint(base_url: &str) -> bool {
    let u = base_url.trim().trim_end_matches('/').to_ascii_lowercase();
    u.contains("/anthropic")
}

fn reject_openai_custom_anthropic_base(template_id: &str, base_url: &str) -> Result<(), String> {
    if matches!(template_id, "custom-openai" | "custom-openai-responses")
        && looks_like_anthropic_endpoint(base_url)
    {
        Err("这个地址看起来是 Anthropic 兼容端点。请改选「自定义 Anthropic」，或使用 OpenAI 兼容 base root（如 https://api.moonshot.cn/v1）。".to_string())
    } else {
        Ok(())
    }
}

/// deepseek/qwen 走各自固定官方端点（python 侧硬编码）；其余 = relay 家族，需带 base_url。
fn is_native_adapter(adapter: &str) -> bool {
    adapter == "deepseek" || adapter == "qwen"
}

fn is_openai_adapter(adapter: &str) -> bool {
    matches!(adapter, "openai-custom" | "openai-responses")
}

/// 上游主机名（供 status 上游灯做 TCP 可达性探测）。relay 家族从其 base_url 解析。
fn upstream_host(adapter: &str, base_url: &str) -> String {
    match adapter {
        "deepseek" => "api.deepseek.com".to_string(),
        "qwen" => "dashscope.aliyuncs.com".to_string(),
        _ => parse_host(base_url).unwrap_or_default(),
    }
}

/// 从 `http(s)://host[:port]/path` 里抽出 host。解析不出返回 None（不引 url crate）。
fn parse_host(url: &str) -> Option<String> {
    let rest = url
        .strip_prefix("https://")
        .or_else(|| url.strip_prefix("http://"))?;
    let host = rest
        .split(['/', ':', '?', '#'])
        .next()
        .unwrap_or("")
        .to_string();
    if host.is_empty() {
        None
    } else {
        Some(host)
    }
}

/// 判断模型 id 是否会平铺进 Science 选择器主列表（claude-{opus|sonnet|haiku}-<数字…>）。
/// 仅用于「获取模型」结果排序（主列表项排前），非鉴权路径。
fn is_main_list_model(id: &str) -> bool {
    for fam in ["claude-opus-", "claude-sonnet-", "claude-haiku-"] {
        if let Some(rest) = id.strip_prefix(fam) {
            return rest
                .chars()
                .next()
                .map(|c| c.is_ascii_digit())
                .unwrap_or(false);
        }
    }
    false
}

// ---------- 路径与日志 ----------
/// 定位 CSSwitch 仓库根（含 proxy/csswitch_proxy.py）。优先 CSSWITCH_REPO，
/// 否则从可执行文件逐级上溯。找不到返回 None。
fn repo_root() -> Option<PathBuf> {
    let marker = Path::new("proxy/csswitch_proxy.py");
    if let Some(r) = std::env::var_os("CSSWITCH_REPO") {
        if let Ok(p) = std::fs::canonicalize(PathBuf::from(r)) {
            if p.join(marker).is_file() {
                return Some(p);
            }
        }
    }
    // 只从【可执行文件位置】上溯。刻意不看 current_dir：启动目录可被影响，
    // 若据此找到别处的 csswitch_proxy.py，会把带 key 的环境交给来路不明的脚本。
    if let Ok(exe) = std::env::current_exe() {
        let mut dir: Option<&Path> = exe.parent();
        while let Some(d) = dir {
            if d.join(marker).is_file() {
                return Some(d.to_path_buf());
            }
            dir = d.parent();
        }
    }
    None
}

/// 定位「资源根」（含 proxy/、scripts/）。打包成 .app 后 bundle 进 `Contents/Resources`；
/// 开发态则回退到仓库根。找不到返回 None。
fn asset_root(app: &tauri::AppHandle) -> Option<PathBuf> {
    let marker = Path::new("proxy/csswitch_proxy.py");
    if let Ok(res) = app.path().resource_dir() {
        if res.join(marker).is_file() {
            return Some(res);
        }
    }
    repo_root()
}

/// 沙箱可写工作目录（独立 HOME）：`~/.csswitch/sandbox/home`。
fn sandbox_home() -> PathBuf {
    config::default_dir().join("sandbox").join("home")
}

fn log_path(name: &str) -> PathBuf {
    config::default_dir().join("logs").join(name)
}

/// `O_NOFOLLOW` 的平台常量（本项目不引 libc）。macOS/BSD=0x0100，Linux=0x20000。
const fn libc_o_nofollow() -> i32 {
    if cfg!(target_os = "linux") {
        0x2_0000
    } else {
        0x0100
    }
}

/// 打开（truncate）一个子进程日志文件，父目录 0700、文件 0600（防同机其它用户读到 secret 尾巴）。
fn open_log(name: &str) -> std::io::Result<std::fs::File> {
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
    let p = log_path(name);
    if let Some(parent) = p.parent() {
        config::assert_not_symlink(parent)?;
        std::fs::create_dir_all(parent)?;
        let _ = std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700));
    }
    config::assert_not_symlink(&p)?;
    let f = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .custom_flags(libc_o_nofollow())
        .open(&p)?;
    let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o600));
    Ok(f)
}

/// 把字符串里的 secret 明文替换成 ****，用于任何要回显给前端的错误尾巴。
fn redact(s: &str, secret: &str) -> String {
    if secret.is_empty() {
        s.to_string()
    } else {
        s.replace(secret, "****")
    }
}

fn tail_file(path: &Path, max: usize) -> String {
    match std::fs::read(path) {
        Ok(b) => {
            let start = b.len().saturating_sub(max);
            String::from_utf8_lossy(&b[start..]).trim().to_string()
        }
        Err(_) => String::new(),
    }
}

/// 用系统浏览器打开 URL（macOS `open`）。校验退出码：非零视为失败（P2c）。
fn open_in_browser(url: &str) -> Result<(), String> {
    let st = Command::new("open")
        .arg(url)
        .status()
        .map_err(|e| format!("打开浏览器失败：{e}"))?;
    if !st.success() {
        return Err(format!("open 非零退出（{:?}）", st.code()));
    }
    Ok(())
}

// ---------- 代理生命周期核心 ----------
/// 转义 ERE 元字符，让路径按字面参与 `pkill -f` 匹配。
fn ere_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for c in s.chars() {
        if "\\.^$*+?()[]{}|".contains(c) {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

/// 本次 ensure_proxy 对代理做了什么（供一键据实提示）。
#[derive(Clone, Copy, PartialEq)]
enum ProxyAction {
    Reused,    // 端口+adapter+key 指纹一致且健康，原样复用
    Restarted, // 首次起 / 换 key / 换 profile / 不健康，重起了代理
}

/// 切换事务的提交/回滚决策（纯函数，spec §7）。live 路径难做确定性单测，故把决策抽出单独测。
#[derive(Debug, PartialEq)]
enum SwitchOutcome {
    Commit,           // scratch 校验过 + 正式代理探活健康 → 提交 active_id
    RollbackToOld,    // scratch 过但正式代理起/探活失败 → 杀候选、恢复旧代理、不提交
    AbortBeforeStart, // scratch 校验失败 → 根本没起正式代理、旧态零改动
}

/// 给定「候选 scratch 校验结果」与「正式代理探活结果」，决定切换事务走向。
fn decide_switch(scratch_ok: bool, real_healthy: bool) -> SwitchOutcome {
    if !scratch_ok {
        return SwitchOutcome::AbortBeforeStart;
    }
    if real_healthy {
        SwitchOutcome::Commit
    } else {
        SwitchOutcome::RollbackToOld
    }
}

/// 探活结束回锁后是否可写回 `st.proxy`：generation 未被取代【且】secret 仍是本次启动的。
/// 抽成纯函数便于确定性单测（gen 同/异 × secret 同/异 4 组合）。
/// secret 合取防「冷启动双起、两个不同 secret、generation 却相等」的窄窗：另起若用不同 secret
/// 重置了槽位，本次就不该拿旧 child 覆盖它（起代理前会把 `st.secret` 预置成本次 secret，故合法启动上恒真）。
fn should_write_back(gen_captured: u64, gen_now: u64, st_secret: &str, my_secret: &str) -> bool {
    gen_captured == gen_now && st_secret == my_secret
}

/// 确保代理在跑且健康；返回 (端口, secret, 本次动作)。幂等：已健康则复用。
/// 读【生效 profile】派生 adapter/base_url/model/key，委托 [`start_proxy_for`]。
fn ensure_proxy(
    app: &tauri::AppHandle,
    state: &State<'_, Mutex<AppState>>,
    lifecycle: &lifecycle::Lifecycle,
) -> Result<(u16, String, ProxyAction), String> {
    let cfg = config::load_from(&config::default_dir()).map_err(|e| e.to_string())?;
    let profile = cfg
        .active_profile()
        .cloned()
        .ok_or("未配置生效 profile，请先在面板选择或新建一条配置。")?;
    start_proxy_for(app, state, lifecycle, &profile)
}

/// 探活超时的原因措辞（纯函数，修真机 P2）：本地 `/health` 不验上游 key，故探活超时与 key 有效性
/// 无关。日志出现绑定失败（Address already in use / EADDRINUSE）→ 明确报端口占用；否则报「探活超时」
/// （多为 python 依赖缺失 / 脚本异常），绝不再含糊说「或 key 无效」。
fn health_timeout_reason(port: u16, tail: &str) -> String {
    let occupied = tail.contains("Address already in use")
        || tail.contains("EADDRINUSE")
        || tail.contains("Errno 48") // macOS EADDRINUSE
        || tail.contains("Errno 98"); // Linux EADDRINUSE
    if occupied {
        format!("端口 {port} 已被占用，换个端口或先停掉占用进程后重试。")
    } else {
        format!(
            "代理起后探活超时（端口 {port}）：多为 python 依赖缺失或代理脚本异常，请查看代理日志。"
        )
    }
}

/// 用【给定 profile】（不读 active）起代理并探活；返回 (端口, secret, 动作)。
///
/// 并发正确性（spec §8.1）：
/// - **读-spawn 原子**：复用判定 / 清残留 / spawn 都在同一把 AppState 锁内；新 child 先握本地。
/// - **探活锁外**：探活刻意在 AppState 锁外做，不阻塞 status 等命令。
/// - **generation token**：spawn 前抓 `gen`；探活健康后回锁校验 `current_generation()==gen`，
///   若期间被清 key/停/切 bump 过 → 杀掉自己刚起的 child、**不写回 st.proxy**（不拿旧配置复活）。
///
/// 本函数**绝不取串行器锁**（调用方命令才取），故与命令层的 `with_serialized` 不会自死锁。
fn start_proxy_for(
    app: &tauri::AppHandle,
    state: &State<'_, Mutex<AppState>>,
    lifecycle: &lifecycle::Lifecycle,
    profile: &config::Profile,
) -> Result<(u16, String, ProxyAction), String> {
    assert_format_supported(profile)?;
    let launch = proxy_args_for(profile);
    if launch.key.is_empty() {
        return Err(format!(
            "「{}」还没填 API key，请先在面板填写并保存。",
            profile.name
        ));
    }
    let native = is_native_adapter(&launch.adapter);
    if !native && launch.base_url.is_empty() {
        return Err(
            "该配置需要填 base_url（如 https://your-relay/claude），请先在面板填写并保存。".into(),
        );
    }
    // 换任一协议语义或上游字段都触发代理重启，避免不同配置切换时复用旧进程。
    let key_fp = proxy_fingerprint(profile, &launch);
    let dir = config::default_dir();
    let cfg = config::load_from(&dir).map_err(|e| e.to_string())?;
    let port = cfg.proxy_port;
    let root = asset_root(app)
        .ok_or("找不到代理脚本 proxy/csswitch_proxy.py（打包资源或仓库根均未命中）。开发态可设 CSSWITCH_REPO。")?;
    let py = proc::find_exe("python3")
        .ok_or("缺少依赖 python3（起翻译代理需要）。已查 PATH、常见目录与登录 shell 仍未找到；macOS 一般自带 /usr/bin/python3（装 Xcode 命令行工具：xcode-select --install）。")?;
    proc::check_python_version(&py)?;

    // path-secret：**持久化复用**（已在跑的沙箱把该 secret 嵌进了 ANTHROPIC_BASE_URL，
    // 若每次起代理都换 secret，代理一重启沙箱就会拿旧 secret 打到新代理 → 全部 403）。
    let secret = if !cfg.secret.is_empty() {
        cfg.secret.clone()
    } else {
        let s = proc::gen_secret().map_err(|e| format!("无法生成安全 secret：{e}"))?;
        let s2 = s.clone();
        config::update(&dir, move |c| c.secret = s2).map_err(|e| e.to_string())?;
        s
    };

    // generation token：**spawn 前**抓当前号；探活后回锁比对，防被更晚操作取代还写回。
    let gen = lifecycle.current_generation();

    // 「检查复用 → 清残留 → 起进程」在同一把 AppState 锁内完成（读-spawn 原子）。
    // 但新 child 只握在本地，**探活健康 + generation 未变**才写回 st.proxy。
    let child = {
        let mut st = lock(state);
        // 幂等：已在跑且健康、且【端口 + adapter + key 指纹】都一致才复用。
        if st.proxy.is_some()
            && st.proxy_port == port
            && st.provider == launch.adapter
            && st.key_fp == key_fp
            && proc::http_health(port, Some(&st.secret), 500)
        {
            return Ok((port, st.secret.clone(), ProxyAction::Reused));
        }
        // 端口要让给新进程 → 先杀掉旧占用者（st.proxy）与同端口孤儿；期间 st.proxy=None。
        kill_child(&mut st.proxy);
        st.provider.clear();
        st.key_fp = 0;
        // 预置 st.secret = 本次 secret（persistent path-secret）：使探活后写回门的 secret 合取
        // 在合法启动上恒真；只有并发另起用「不同 secret」重置了它，才会挡下写回（冷启动双起窄窗防御）。
        st.secret = secret.clone();
        let script = root.join("proxy/csswitch_proxy.py");
        // 再清掉上次会话遗留、绑在同端口上的孤儿代理（匹配本安装的绝对脚本路径 + 端口）。
        let pat = format!("{}.*--port {port}", ere_escape(&script.to_string_lossy()));
        let _ = Command::new("pkill").arg("-f").arg(&pat).status();

        let logf = open_log("proxy.log").map_err(|e| format!("建日志失败：{e}"))?;
        let logf2 = logf.try_clone().map_err(|e| e.to_string())?;
        let mut cmd = Command::new(&py);
        cmd.arg(&script)
            .arg("--provider")
            .arg(&launch.adapter)
            .arg("--port")
            .arg(port.to_string())
            .arg("--auth-token")
            .arg(&secret)
            .env(
                "CSSWITCH_CONFIG_PATH",
                config::default_dir().join("config.json"),
            )
            // key 经环境变量注入，绝不作为命令行参数（避免 ps 泄露）。
            .env(launch.key_env, &launch.key);
        if cfg.agent_mode != "normal" {
            cmd.env("CSSWITCH_ULTRA_MODE", &cfg.agent_mode);
        }
        // relay 家族：base_url + 选中模型经环境变量交给代理（均非密钥，但与 key 一致走 env）。
        if !native {
            if is_openai_adapter(&launch.adapter) {
                cmd.env("CSSWITCH_OPENAI_BASE_URL", &launch.base_url);
                if !launch.model.is_empty() {
                    cmd.env("CSSWITCH_OPENAI_MODEL", &launch.model);
                }
            } else {
                cmd.env("CSSWITCH_RELAY_BASE_URL", &launch.base_url);
                if !launch.model.is_empty() {
                    cmd.env("CSSWITCH_RELAY_MODEL", &launch.model);
                }
                if !launch.thinking_policy.is_empty() {
                    cmd.env("CSSWITCH_RELAY_THINKING", launch.thinking_policy);
                }
            }
        }
        cmd.stdout(Stdio::from(logf))
            .stderr(Stdio::from(logf2))
            .spawn()
            .map_err(|e| format!("启动代理失败：{e}"))?
        // 注意：child 未写入 st.proxy——探活通过且 generation 未变时才回锁写回。
    };

    // 探活最多 ~4s（AppState 锁外，不阻塞 status 等命令）。
    let mut ok = false;
    for _ in 0..40 {
        std::thread::sleep(Duration::from_millis(100));
        if proc::http_health(port, Some(&secret), 400) {
            ok = true;
            break;
        }
    }
    if !ok {
        // 探活失败：杀掉自己刚起的 child（它从未写入 st.proxy，绝不留孤儿）。
        let mut c = child;
        let _ = c.kill();
        let _ = c.wait();
        let tail = redact(&tail_file(&log_path("proxy.log"), 500), &secret);
        // 本地 /health 不验上游 key，故探活超时与 key 有效性无关：按日志区分端口占用 vs 依赖/脚本异常
        // （修真机 P2：旧措辞含糊说「或 key 无效」会误导用户去查 key）。
        return Err(format!("{}\n{tail}", health_timeout_reason(port, &tail)));
    }

    // 健康 → 回 AppState 锁，校验 generation 未被 bump 且 secret 仍是本次的（未被清 key/停/切/并发另起取代）才写回。
    {
        let mut st = lock(state);
        if !should_write_back(gen, lifecycle.current_generation(), &st.secret, &secret) {
            // 被更晚的操作取代（generation 变）或被并发另起用不同 secret 占了槽：
            // 杀掉自己刚起的 child、不写回 st.proxy（不拿旧配置复活、不覆盖他人的槽）。
            let mut c = child;
            let _ = c.kill();
            let _ = c.wait();
            return Err("代理启动期间配置已变更（被更晚的操作取代），本次启动未生效。".into());
        }
        st.proxy = Some(child);
        st.proxy_port = port;
        st.secret = secret.clone();
        st.provider = launch.adapter.clone();
        st.key_fp = key_fp;
    }
    Ok((port, secret, ProxyAction::Restarted))
}

include!("commands/settings_cmd.rs");
include!("commands/profile_store.rs");
include!("commands/profile_cmd.rs");
include!("commands/profile_switch.rs");
include!("commands/proxy_cmd.rs");
include!("commands/sandbox_runtime.rs");
include!("commands/sandbox_cmd.rs");
include!("commands/update_cmd.rs");
include!("commands/pack_cmd.rs");
include!("commands/sensitive_cmd.rs");
include!("commands/task_cmd.rs");
include!("commands/verify_cmd.rs");
include!("commands/research_cmd.rs");

// ---------- 入口 ----------
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(Mutex::new(AppState::default()))
        .manage(lifecycle::Lifecycle::new())
        .invoke_handler(tauri::generate_handler![
            get_config,
            list_templates,
            set_settings,
            set_mode,
            set_agent_mode,
            open_official,
            create_profile,
            update_profile_metadata,
            update_profile_connection,
            clear_profile_key,
            delete_profile,
            set_active_profile,
            start_proxy,
            verify_key,
            fetch_models,
            stop_all,
            one_click_login,
            status,
            open_url,
            run_doctor,
            app_version,
            check_updates,
            open_release_page,
            report_bug,
            open_logs,
            quit_app,
            list_packs,
            toggle_pack,
            set_pack_env,
            set_sensitive_mode,
            set_local_endpoint_hosts,
            list_biomed_tasks,
            set_task_route,
            run_probes,
            start_smoke_verification,
            poll_smoke_verification,
            confirm_skill_verified,
            cleanup_smoke_verification,
            compile_research_brief,
            finalize_research_brief,
        ])
        .setup(|app| {
            // 正常桌面应用：进 Dock、走常规应用生命周期。窗口在 tauri.conf.json 里配了
            // decorations + visible + center，启动即居中弹出、可拖动。托盘图标已移除。

            // 启动即触发一次 load：若是旧 v1 固定槽文件，这里完成 v1→v2 迁移 + 落盘 + 留 .v1.bak；
            // 悬空 active 归一化为空。迁移逻辑并入 config::load_from（不再单独跑 relay_presets）。
            let _ = config::load_from(&config::default_dir());

            // 关窗即退出：与「退出」按钮一致 —— 停代理、清 secret，保留沙箱运行（spec §5.1）。
            if let Some(win) = app.get_webview_window("main") {
                let handle = app.handle().clone();
                win.on_window_event(move |ev| {
                    if let tauri::WindowEvent::CloseRequested { .. } = ev {
                        let state = handle.state::<Mutex<AppState>>();
                        let mut st = lock(&state);
                        kill_child(&mut st.proxy);
                        st.secret.clear();
                    }
                });
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests;
