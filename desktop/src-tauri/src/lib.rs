//! CSSwitch 菜单栏 app 后端（进程管家）。
//!
//! 职责：管理「翻译代理」与「沙箱 Science」两个子进程的生命周期；读写
//! `~/.csswitch/config.json`；把第三方 key 以【环境变量】注入代理子进程（绝不进 argv）；
//! 探活；把沙箱 URL 交系统浏览器打开。已验证的越权/翻译逻辑仍留在 Python/Node/shell
//! 里被当作子进程调用，以保住铁律护栏与已验证行为。
//!
//! 铁律相关：key 只在内存与 0600 的 config.json；回显前端只给掩码；沙箱端口/目录护栏
//! 由被调脚本负责（对 8765 与真实目录失败关闭）；退 app 默认停代理、保留沙箱。

mod config;
mod proc;

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use serde::Deserialize;
use serde_json::json;
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, State};

const SCIENCE_BIN: &str = "/Applications/Claude Science.app/Contents/Resources/bin/claude-science";

#[derive(Default)]
struct AppState {
    proxy: Option<Child>,
    proxy_port: u16,
    secret: String,
    provider: String,
    /// 当前代理进程所用 key 的非加密指纹（仅内存、绝不落盘/打印）。
    /// 换 key 后指纹变化 → 触发重启，避免复用带旧 key 的代理。
    key_fp: u64,
    sandbox: Option<Child>,
    sandbox_port: u16,
    sandbox_url: Option<String>,
}

/// key 的非加密指纹（SipHash），只用于判断「key 是否变了」。绝不打印、绝不落盘。
fn key_fingerprint(s: &str) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    s.hash(&mut h);
    h.finish()
}

// ---------- provider 元信息 ----------
fn key_env(provider: &str) -> &'static str {
    match provider {
        "qwen" => "DASHSCOPE_API_KEY",
        _ => "DEEPSEEK_API_KEY",
    }
}

fn upstream_host(provider: &str) -> &'static str {
    match provider {
        "qwen" => "dashscope.aliyuncs.com",
        _ => "api.deepseek.com",
    }
}

// ---------- 路径与日志 ----------
/// 定位 CSSwitch 仓库根（含 proxy/csswitch_proxy.py）。优先 CSSWITCH_REPO，
/// 否则从可执行文件与当前目录逐级上溯。找不到返回 None。
fn repo_root() -> Option<PathBuf> {
    let marker = Path::new("proxy/csswitch_proxy.py");
    // 显式指定优先：规范化后再判定，避免相对/软链歧义。
    if let Some(r) = std::env::var_os("CSSWITCH_REPO") {
        if let Ok(p) = std::fs::canonicalize(PathBuf::from(r)) {
            if p.join(marker).is_file() {
                return Some(p);
            }
        }
    }
    // 否则只从【可执行文件位置】上溯。刻意不看 current_dir：启动目录可被影响，
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

/// 定位「资源根」（含 proxy/、scripts/）。打包成 .app 后，proxy/ 与 scripts/ 被
/// bundle 进 `Contents/Resources`；开发态则回退到仓库根。找不到返回 None。
/// 这样从 Finder 启动的正式 .app 也能找到代理脚本（修 P1-1）。
fn asset_root(app: &tauri::AppHandle) -> Option<PathBuf> {
    let marker = Path::new("proxy/csswitch_proxy.py");
    // 打包态：Tauri 资源目录。
    if let Ok(res) = app.path().resource_dir() {
        if res.join(marker).is_file() {
            return Some(res);
        }
    }
    // 开发态：从可执行文件位置上溯（见 repo_root 注释，刻意不看 current_dir）。
    repo_root()
}

/// 沙箱可写工作目录（独立 HOME）：`~/.csswitch/sandbox/home`。
/// 打包后资源目录只读，沙箱状态（虚拟登录、克隆运行时、钥匙串）必须落在可写处；
/// 该路径同时交给 launch/stop 脚本（`SANDBOX_HOME` 环境变量）与取 URL 逻辑，三者一致。
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
    // 日志路径不许是符号链接：否则 truncate+写会覆盖链接目标文件（修 P2-1）。
    config::assert_not_symlink(&p)?;
    let f = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        // O_NOFOLLOW：即便在 lstat 与 open 之间被换成软链，也拒绝跟随。
        .custom_flags(libc_o_nofollow())
        .open(&p)?;
    // 文件已存在时 mode() 不复位，显式再夹一次。
    let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o600));
    Ok(f)
}

/// 把字符串里的 secret 明文替换成 ****，用于任何要回显给前端的错误尾巴。
fn redact<'a>(s: &str, secret: &str) -> String {
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

fn kill_child(slot: &mut Option<Child>) {
    if let Some(mut c) = slot.take() {
        let _ = c.kill();
        let _ = c.wait();
    }
}

/// 取锁并从 poison 中恢复：某线程持锁时 panic 不应把整个 app 卡死。
fn lock(m: &Mutex<AppState>) -> std::sync::MutexGuard<'_, AppState> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// 用系统浏览器打开 URL（macOS `open`）。
fn open_in_browser(url: &str) -> Result<(), String> {
    Command::new("open")
        .arg(url)
        .status()
        .map_err(|e| format!("打开浏览器失败：{e}"))?;
    Ok(())
}

// ---------- 代理生命周期核心 ----------
/// 确保代理在跑且健康；返回 (端口, secret)。幂等：已健康则复用。
fn ensure_proxy(
    app: &tauri::AppHandle,
    state: &State<'_, Mutex<AppState>>,
) -> Result<(u16, String), String> {
    let dir = config::default_dir();
    let cfg = config::load_from(&dir).map_err(|e| e.to_string())?;
    let provider = cfg.provider.clone();
    let key = cfg
        .key_for(&provider)
        .ok_or_else(|| format!("缺少 {provider} 的 API key，请先在面板填写并保存。"))?;
    let key_fp = key_fingerprint(&key);
    let port = cfg.proxy_port;
    let root = asset_root(app)
        .ok_or("找不到代理脚本 proxy/csswitch_proxy.py（打包资源或仓库根均未命中）。开发态可设 CSSWITCH_REPO。")?;
    let py = proc::which("python3").ok_or("缺少依赖 python3。")?;

    // path-secret：**持久化复用**。已在跑的沙箱把该 secret 嵌进了 ANTHROPIC_BASE_URL，
    // 若每次起代理都换 secret，代理一重启（换 key/换 provider/重开 app）沙箱就会拿旧 secret
    // 打到新代理 → 全部 403（修 P1：代理重启后沙箱失联）。故从 config 读稳定 secret，
    // 首次为空才生成一次并写回，之后所有代理进程都复用它。
    let secret = if !cfg.secret.is_empty() {
        cfg.secret.clone()
    } else {
        let s = proc::gen_secret().map_err(|e| format!("无法生成安全 secret：{e}"))?;
        let s2 = s.clone();
        config::update(&dir, move |c| c.secret = s2).map_err(|e| e.to_string())?;
        s
    };

    // 整个「检查 → 清残留 → 起进程 → 记账」在同一把锁内完成，避免并发双击时
    // 两路都判定「没健康代理」各起一个、后者覆盖前者的 Child 句柄导致前者被孤儿泄漏。
    {
        let mut st = lock(state);
        // 幂等：已在跑且健康、且【端口 + provider + key 指纹】都一致才复用。
        // 只比端口会在「换 provider / 换 key」后误用带旧配置的代理（修 P1-2）。
        if st.proxy.is_some()
            && st.proxy_port == port
            && st.provider == provider
            && st.key_fp == key_fp
            && proc::http_health(port, Some(&st.secret), 500)
        {
            return Ok((port, st.secret.clone()));
        }
        // 清残留（换端口/换 provider/换 key/不健康）。
        kill_child(&mut st.proxy);
        // 再清掉上次会话遗留、绑在同端口上的孤儿代理：崩溃或强退不会触发本进程的 kill，
        // 孤儿仍占着端口 → 新代理绑不上（Errno 48）→ 探活超时。按「脚本名 + 端口」精确匹配，
        // 只杀我们自己的代理，绝不误伤其它进程。配合代理侧的绑定重试彻底消除竞态。
        let _ = Command::new("pkill")
            .arg("-f")
            .arg(format!("csswitch_proxy\\.py.*--port {port}"))
            .status();

        let script = root.join("proxy/csswitch_proxy.py");
        let logf = open_log("proxy.log").map_err(|e| format!("建日志失败：{e}"))?;
        let logf2 = logf.try_clone().map_err(|e| e.to_string())?;
        let child = Command::new(&py)
            .arg(&script)
            .arg("--provider")
            .arg(&provider)
            .arg("--port")
            .arg(port.to_string())
            .arg("--auth-token")
            .arg(&secret)
            // key 经环境变量注入，绝不作为命令行参数（避免 ps 泄露）。
            .env(key_env(&provider), &key)
            .stdout(Stdio::from(logf))
            .stderr(Stdio::from(logf2))
            .spawn()
            .map_err(|e| format!("启动代理失败：{e}"))?;
        st.proxy = Some(child);
        st.proxy_port = port;
        st.secret = secret.clone();
        st.provider = provider;
        st.key_fp = key_fp;
    }

    // 探活最多 ~4s（锁外，不阻塞 status 等命令）。
    let mut ok = false;
    for _ in 0..40 {
        std::thread::sleep(Duration::from_millis(100));
        if proc::http_health(port, Some(&secret), 400) {
            ok = true;
            break;
        }
    }
    if !ok {
        let mut st = lock(state);
        // 只在仍是本次起的代理时才清（secret 匹配），避免误杀并发重启起来的新代理。
        if st.secret == secret {
            kill_child(&mut st.proxy);
        }
        let tail = redact(&tail_file(&log_path("proxy.log"), 500), &secret);
        return Err(format!(
            "代理起后探活超时（端口 {port} 可能被占用，或 key 无效）。\n{tail}"
        ));
    }
    Ok((port, secret))
}

/// 停沙箱。返回 Err 表示 stop 脚本非零退出（Science 可能没停干净），
/// 调用方据此如实报告，不再无条件报「已停止」（修 P1 停止虚假成功）。
fn stop_sandbox_inner(app: &tauri::AppHandle, st: &mut AppState) -> Result<(), String> {
    // 沙箱由脚本以 --detached 起 Science，本进程持有的是脚本 child（已退出）。
    // 真正停 Science 要调 stop 脚本（按 data-dir，绝不碰真实 8765）。
    let mut err = None;
    if let Some(root) = asset_root(app) {
        let stop = root.join("scripts/stop-science-sandbox.sh");
        if stop.is_file() {
            match Command::new("zsh") // stop 脚本是 #!/bin/zsh（用了 ${VAR:A} realpath）
                .arg(&stop)
                // 与 launch 时一致的可写沙箱 HOME，stop 才能按同一 data-dir 停对进程。
                .env("SANDBOX_HOME", sandbox_home())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
            {
                Ok(s) if s.success() => {}
                Ok(s) => err = Some(format!("停止沙箱脚本非零退出（{:?}）。", s.code())),
                Err(e) => err = Some(format!("调用停止沙箱脚本失败：{e}")),
            }
        }
    }
    kill_child(&mut st.sandbox);
    st.sandbox_url = None;
    match err {
        Some(e) => Err(e),
        None => Ok(()),
    }
}

// ---------- Tauri commands ----------
#[tauri::command]
fn get_config() -> Result<serde_json::Value, String> {
    let dir = config::default_dir();
    let cfg = config::load_from(&dir).map_err(|e| e.to_string())?;
    let mut keys = serde_json::Map::new();
    for p in ["deepseek", "qwen"] {
        let masked = cfg.key_for(p).map(|k| config::mask(&k)).unwrap_or_default();
        keys.insert(p.to_string(), serde_json::Value::String(masked));
    }
    Ok(json!({
        "provider": cfg.provider,
        "proxy_port": cfg.proxy_port,
        "sandbox_port": cfg.sandbox_port,
        "keys": keys,
    }))
}

#[derive(Deserialize)]
struct UiSettings {
    provider: String,
    proxy_port: u16,
    sandbox_port: u16,
}

#[tauri::command]
fn set_config(cfg: UiSettings) -> Result<(), String> {
    // 铁律防御：代理/沙箱端口都不许用真实实例保留端口 8765。
    if cfg.proxy_port == 8765 || cfg.sandbox_port == 8765 {
        return Err("端口 8765 是真实 Science 实例保留端口，不能用。".into());
    }
    // 只认已实现的 provider，避免存进未知值后起代理时才失败（修 P2-3）。
    if cfg.provider != "deepseek" && cfg.provider != "qwen" {
        return Err(format!("未知 provider：{}（只支持 deepseek / qwen）。", cfg.provider));
    }
    // 端口 0 非法（无法监听/探活）。
    if cfg.proxy_port == 0 || cfg.sandbox_port == 0 {
        return Err("端口不能为 0。".into());
    }
    // 代理与沙箱不能同端口，否则互相抢占。
    if cfg.proxy_port == cfg.sandbox_port {
        return Err("代理端口与沙箱端口不能相同。".into());
    }
    let dir = config::default_dir();
    config::update(&dir, move |c| {
        c.provider = cfg.provider;
        c.proxy_port = cfg.proxy_port;
        c.sandbox_port = cfg.sandbox_port;
    })
    .map(|_| ())
    .map_err(|e| e.to_string())
}

#[tauri::command]
fn save_provider_key(provider: String, key: String) -> Result<String, String> {
    let dir = config::default_dir();
    let key2 = key.clone();
    config::update(&dir, move |c| {
        c.providers.entry(provider).or_default().key = key2;
    })
    .map_err(|e| e.to_string())?;
    Ok(config::mask(&key))
}

#[tauri::command]
fn start_proxy(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
) -> Result<serde_json::Value, String> {
    let (port, _secret) = ensure_proxy(&app, &state)?;
    Ok(json!({ "port": port }))
}

/// 「存 key 即验证」：确保代理在跑，再经代理向上游发一个**最小**请求
/// （`max_tokens:1`，一句 "ping"），据响应状态码判断 key 是否真的可用。
/// 返回 `{ok, hint}`：ok=true 表示上游接受（key 有效）；ok=false 表示上游拒绝或异常，
/// hint 给人话。彻底避免「只看绿灯（代理起来了）≠ key 真能用」。
#[tauri::command]
fn verify_key(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
) -> Result<serde_json::Value, String> {
    let (port, secret) = ensure_proxy(&app, &state)?;
    // 走稳定模型 id（代理内部映射到当前 provider 的真实模型），非流式、只要 1 个 token。
    let body = br#"{"model":"claude-opus-4-8","max_tokens":1,"messages":[{"role":"user","content":"ping"}]}"#;
    match proc::http_post_status(port, Some(&secret), "/v1/messages", body, 15000) {
        Some(200) => Ok(json!({ "ok": true, "hint": "key 有效，上游已接受。" })),
        Some(code @ (401 | 403)) => {
            Ok(json!({ "ok": false, "hint": format!("上游拒绝（{code}），key 可能无效或无权限。") }))
        }
        Some(code) => Ok(json!({
            "ok": false,
            "hint": format!("上游返回 {code}，可能是 key 无效、额度不足或上游异常。")
        })),
        None => Err("验证请求无响应（多为网络或上游不通）。".to_string()),
    }
}

#[tauri::command]
fn stop_all(app: tauri::AppHandle, state: State<'_, Mutex<AppState>>) -> Result<(), String> {
    let mut st = lock(&state);
    // 先停沙箱并记录结果；代理无论如何都杀。沙箱没停干净则如实返错，不虚报成功。
    let sandbox_res = stop_sandbox_inner(&app, &mut st);
    kill_child(&mut st.proxy);
    st.secret.clear();
    sandbox_res.map_err(|e| format!("代理已停；但{e}真实实例 8765 未受影响。"))
}

#[tauri::command]
fn one_click_login(
    app: tauri::AppHandle,
    state: State<'_, Mutex<AppState>>,
) -> Result<serde_json::Value, String> {
    // 1~3. 确保代理在跑且健康（内部已查 key、探活）。
    let (pport, secret) = ensure_proxy(&app, &state)?;

    let dir = config::default_dir();
    let cfg = config::load_from(&dir).map_err(|e| e.to_string())?;
    let sport = cfg.sandbox_port;
    let root = asset_root(&app).ok_or("找不到 scripts/launch-virtual-sandbox.sh（打包资源或仓库根均未命中）。")?;

    if proc::which("node").is_none() {
        return Err("缺少依赖 node（写虚拟登录需要）。".into());
    }
    let launch = root.join("scripts/launch-virtual-sandbox.sh");
    if !launch.is_file() {
        return Err("找不到 scripts/launch-virtual-sandbox.sh。".into());
    }

    // 4. 起沙箱：脚本内部写虚拟 OAuth 并以 --detached 起 Science，然后返回。
    let proxy_url = format!("http://127.0.0.1:{pport}/{secret}");
    let logf = open_log("sandbox.log").map_err(|e| format!("建日志失败：{e}"))?;
    let logf2 = logf.try_clone().map_err(|e| e.to_string())?;
    let status = Command::new("zsh") // launch 脚本是 #!/bin/zsh（用了 ${VAR:A} realpath）
        .arg(&launch)
        .arg("--port")
        .arg(sport.to_string())
        .arg("--proxy-url")
        .arg(&proxy_url)
        // 沙箱状态落在可写目录（打包后资源目录只读），launch/stop/取 URL 三处同一路径。
        .env("SANDBOX_HOME", sandbox_home())
        .stdout(Stdio::from(logf))
        .stderr(Stdio::from(logf2))
        .status()
        .map_err(|e| format!("起沙箱失败：{e}"))?;
    if !status.success() {
        let tail = redact(&tail_file(&log_path("sandbox.log"), 600), &secret);
        return Err(format!("起沙箱脚本失败。\n{tail}"));
    }

    // 5. 轮询沙箱 /health 直到就绪或超时（~8s）。
    let mut ok = false;
    for _ in 0..80 {
        std::thread::sleep(Duration::from_millis(100));
        if proc::http_health(sport, None, 400) {
            ok = true;
            break;
        }
    }
    if !ok {
        let tail = redact(&tail_file(&log_path("sandbox.log"), 600), &secret);
        // 探活超时：脚本已把 Science 以 --detached 起在后台，必须停掉，
        // 否则留一个孤儿沙箱进程（修 P2-2）。
        {
            let mut st = lock(&state);
            let _ = stop_sandbox_inner(&app, &mut st); // best-effort 清理，结果不影响这里的报错
        }
        return Err(format!("沙箱起后探活超时（端口 {sport}）。已尝试停掉刚起的沙箱。\n{tail}"));
    }

    // 6. 取 UI URL（登录态），交系统浏览器打开。
    let url = sandbox_url(sport);
    {
        let mut st = lock(&state);
        st.sandbox_port = sport;
        st.sandbox_url = Some(url.clone());
    }
    let _ = open_in_browser(&url);
    Ok(json!({ "url": url }))
}

/// 取沙箱 UI 链接：`<bin> url --data-dir <home>/.claude-science`，HOME 指向沙箱 HOME。
/// 失败退回 http://127.0.0.1:<port>。沙箱 HOME 用 [`sandbox_home`]（与 launch 时一致）。
fn sandbox_url(port: u16) -> String {
    let home = sandbox_home();
    let data_dir = home.join(".claude-science");
    if Path::new(SCIENCE_BIN).is_file() {
        if let Ok(out) = Command::new(SCIENCE_BIN)
            .arg("url")
            .arg("--data-dir")
            .arg(&data_dir)
            .env("HOME", &home)
            .output()
        {
            let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if s.starts_with("http") {
                return s;
            }
        }
    }
    format!("http://127.0.0.1:{port}")
}

#[tauri::command]
fn status(state: State<'_, Mutex<AppState>>) -> serde_json::Value {
    // 只在锁内取值，锁外做阻塞探活。
    let (pport, secret, sport, provider) = {
        let st = lock(&state);
        let cfg = config::load_from(&config::default_dir()).unwrap_or_default();
        let pport = if st.proxy_port != 0 { st.proxy_port } else { cfg.proxy_port };
        let sport = if st.sandbox_port != 0 { st.sandbox_port } else { cfg.sandbox_port };
        (pport, st.secret.clone(), sport, cfg.provider)
    };
    let proxy = if !secret.is_empty() && proc::http_health(pport, Some(&secret), 300) {
        "green"
    } else {
        "amber"
    };
    let sandbox = if proc::http_health(sport, None, 300) { "green" } else { "amber" };
    let upstream = if proc::tcp_reachable(upstream_host(&provider), 443, 500) {
        "green"
    } else {
        "amber"
    };
    json!({ "proxy": proxy, "sandbox": sandbox, "upstream": upstream })
}

#[tauri::command]
fn open_url(state: State<'_, Mutex<AppState>>) -> Result<(), String> {
    let url = { lock(&state).sandbox_url.clone() };
    let url = url.ok_or("还没有沙箱 URL，请先「一键越过登录」。")?;
    open_in_browser(&url)
}

#[tauri::command]
fn run_doctor(app: tauri::AppHandle) -> Result<String, String> {
    let root = asset_root(&app).ok_or("找不到 scripts/doctor.sh（打包资源或仓库根均未命中）。")?;
    let cfg = config::load_from(&config::default_dir()).unwrap_or_default();
    let doctor = root.join("scripts/doctor.sh");
    let mut cmd = Command::new("bash");
    cmd.arg(&doctor)
        .env("CSSWITCH_PROVIDER", &cfg.provider)
        .env("CSSWITCH_PROXY_PORT", cfg.proxy_port.to_string())
        .env("CSSWITCH_SANDBOX_PORT", cfg.sandbox_port.to_string());
    // doctor 只做 -n 判空来报 key 有无。只让它知道「存在」，绝不把真实 key 传进其环境。
    if cfg.key_for(&cfg.provider).is_some() {
        cmd.env(key_env(&cfg.provider), "***present***");
    }
    let out = cmd.output().map_err(|e| e.to_string())?;
    let mut text = String::from_utf8_lossy(&out.stdout).to_string();
    let err = String::from_utf8_lossy(&out.stderr);
    if !err.trim().is_empty() {
        text.push_str("\n[stderr] ");
        text.push_str(err.trim());
    }
    Ok(text)
}

/// 当前 app 版本（供前端「检查更新」与页脚版本号用）。
#[tauri::command]
fn app_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

/// 打开 GitHub Releases 页（检查更新时用系统浏览器打开，浏览器走用户自己的代理）。
#[tauri::command]
fn open_release_page() -> Result<(), String> {
    open_in_browser("https://github.com/SuperJJ007/CSswitch/releases/latest")
}

/// 打开「报 bug」页（预填 bug 模板）；用系统浏览器，走用户自己的代理。
#[tauri::command]
fn report_bug() -> Result<(), String> {
    open_in_browser("https://github.com/SuperJJ007/CSswitch/issues/new?template=bug_report.yml")
}

/// 在访达里打开日志目录 `~/.csswitch/logs`，方便用户附到 bug 反馈里（先自查有无密钥）。
#[tauri::command]
fn open_logs() -> Result<(), String> {
    let dir = config::default_dir().join("logs");
    let _ = std::fs::create_dir_all(&dir);
    Command::new("open")
        .arg(&dir)
        .status()
        .map_err(|e| format!("打开日志目录失败：{e}"))?;
    Ok(())
}

#[tauri::command]
fn quit_app(app: tauri::AppHandle, state: State<'_, Mutex<AppState>>) -> Result<(), String> {
    // 默认：退 app 停代理、保留沙箱运行（spec §5.1）。
    {
        let mut st = lock(&state);
        kill_child(&mut st.proxy);
        st.secret.clear();
    }
    app.exit(0);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{key_fingerprint, redact, sandbox_home};

    #[test]
    fn redact_scrubs_secret_and_is_noop_when_empty() {
        assert_eq!(redact("推理指向 http://127.0.0.1:18991/abcd1234 尾巴", "abcd1234"),
                   "推理指向 http://127.0.0.1:18991/**** 尾巴");
        assert_eq!(redact("原样返回", ""), "原样返回");
        assert!(!redact("leak abcd1234 leak abcd1234", "abcd1234").contains("abcd1234"));
    }

    #[test]
    fn key_fingerprint_stable_and_distinct() {
        // 同 key 稳定、异 key 不同：这是「换 key 触发代理重启」判断的基础（P1-2）。
        assert_eq!(key_fingerprint("sk-aaaa"), key_fingerprint("sk-aaaa"));
        assert_ne!(key_fingerprint("sk-aaaa"), key_fingerprint("sk-bbbb"));
        assert_ne!(key_fingerprint(""), key_fingerprint("x"));
    }

    #[test]
    fn sandbox_home_is_writable_under_config_dir() {
        // 沙箱状态目录必须在可写的 ~/.csswitch 下（不在只读的 .app 资源里）——P1-1。
        let h = sandbox_home();
        assert!(h.ends_with("sandbox/home"), "应以 sandbox/home 结尾：{h:?}");
        assert!(h.to_string_lossy().contains(".csswitch"), "应在 .csswitch 下：{h:?}");
    }
}

/// 把面板锚到主屏右上角、菜单栏正下方（菜单栏 app 的下拉位置）。
/// 无边框窗口默认开在屏幕正中，离菜单栏很远且拖不动，故每次显示前重定位。
fn anchor_top_right(win: &tauri::WebviewWindow) {
    if let (Ok(Some(mon)), Ok(win_size)) = (win.primary_monitor(), win.outer_size()) {
        let mon_pos = mon.position();
        let mon_size = mon.size();
        let scale = mon.scale_factor();
        let margin = (10.0 * scale) as i32;
        let menubar = (26.0 * scale) as i32;
        let x = mon_pos.x + mon_size.width as i32 - win_size.width as i32 - margin;
        let y = mon_pos.y + menubar;
        let _ = win.set_position(tauri::PhysicalPosition::new(x, y));
    }
}

// ---------- 入口 ----------
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(Mutex::new(AppState::default()))
        .invoke_handler(tauri::generate_handler![
            get_config,
            set_config,
            save_provider_key,
            start_proxy,
            verify_key,
            stop_all,
            one_click_login,
            status,
            open_url,
            run_doctor,
            app_version,
            open_release_page,
            report_bug,
            open_logs,
            quit_app
        ])
        .setup(|app| {
            // 菜单栏 app：从 Dock 隐藏（macOS）。
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            // 托盘图标：用专门的单色 template 剪影（只有开关，无文字），
            // 缩到 18px 仍清晰，且随菜单栏明暗自动重着色（修 P3）。
            // 完整暖橘应用图标留给 Dock/Finder/关于窗，不塞进 18px 菜单栏。
            let tray_icon = tauri::image::Image::new(
                include_bytes!("../icons/tray_template.rgba"),
                44,
                44,
            );
            let _tray = TrayIconBuilder::new()
                .icon(tray_icon)
                .icon_as_template(true)
                .tooltip("CSSwitch")
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(win) = app.get_webview_window("main") {
                            if win.is_visible().unwrap_or(false) {
                                let _ = win.hide();
                            } else {
                                anchor_top_right(&win); // 锚到菜单栏下方，别开在屏幕正中
                                let _ = win.show();
                                let _ = win.set_focus();
                            }
                        }
                    }
                })
                .build(app)?;

            // 面板失焦即隐藏（点面板外自动收起）。
            if let Some(win) = app.get_webview_window("main") {
                let w2 = win.clone();
                win.on_window_event(move |ev| {
                    if let tauri::WindowEvent::Focused(false) = ev {
                        let _ = w2.hide();
                    }
                });
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
