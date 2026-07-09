const RELEASE_LATEST_API: &str = "https://api.github.com/repos/HERRY423/BioCSSwitch/releases/latest";

const RELEASE_LATEST_PAGE: &str = "https://github.com/HERRY423/BioCSSwitch/releases/latest";

fn normalize_version_tag(tag: &str) -> String {
    tag.trim()
        .trim_start_matches(|c| c == 'v' || c == 'V')
        .split(|c| c == '-' || c == '+')
        .next()
        .unwrap_or("")
        .trim()
        .to_string()
}

fn version_parts(version: &str) -> Vec<u64> {
    normalize_version_tag(version)
        .split('.')
        .map(|part| {
            part.chars()
                .take_while(|c| c.is_ascii_digit())
                .collect::<String>()
                .parse::<u64>()
                .unwrap_or(0)
        })
        .collect()
}

fn version_is_newer(latest: &str, current: &str) -> bool {
    let mut a = version_parts(latest);
    let mut b = version_parts(current);
    let n = a.len().max(b.len());
    a.resize(n, 0);
    b.resize(n, 0);
    a > b
}

fn parse_latest_release_json(body: &str) -> Result<(String, String, Option<String>), String> {
    let v: serde_json::Value =
        serde_json::from_str(body).map_err(|e| format!("GitHub releases/latest JSON 解析失败：{e}"))?;
    let tag = v
        .get("tag_name")
        .and_then(|x| x.as_str())
        .map(str::trim)
        .filter(|x| !x.is_empty())
        .ok_or("GitHub releases/latest 缺少 tag_name")?
        .to_string();
    let url = v
        .get("html_url")
        .and_then(|x| x.as_str())
        .map(str::trim)
        .filter(|x| !x.is_empty())
        .unwrap_or(RELEASE_LATEST_PAGE)
        .to_string();
    let name = v
        .get("name")
        .and_then(|x| x.as_str())
        .map(str::trim)
        .filter(|x| !x.is_empty())
        .map(|x| x.to_string());
    Ok((tag, url, name))
}

fn fetch_latest_release_json() -> Result<String, String> {
    let out = Command::new("curl")
        .args([
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            "8",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "User-Agent: BioCSSwitch-update-check",
            RELEASE_LATEST_API,
        ])
        .output()
        .map_err(|e| format!("无法运行 curl 检查更新：{e}"))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr).trim().to_string();
        return Err(format!(
            "GitHub releases/latest 请求失败：{}",
            if err.is_empty() {
                format!("curl exit {:?}", out.status.code())
            } else {
                err
            }
        ));
    }
    String::from_utf8(out.stdout).map_err(|e| format!("GitHub releases/latest 不是 UTF-8：{e}"))
}

#[tauri::command]
fn check_updates() -> Result<serde_json::Value, String> {
    let current = app_version();
    let body = fetch_latest_release_json()?;
    let (latest_tag, release_url, release_name) = parse_latest_release_json(&body)?;
    let latest_version = normalize_version_tag(&latest_tag);
    Ok(json!({
        "ok": true,
        "current_version": current,
        "latest_version": latest_version,
        "latest_tag": latest_tag,
        "release_name": release_name,
        "release_url": release_url,
        "update_available": version_is_newer(&latest_version, &current),
    }))
}

/// 打开 GitHub Releases 页（检查更新时用系统浏览器打开，浏览器走用户自己的代理）。
#[tauri::command]
fn open_release_page() -> Result<(), String> {
    open_in_browser(RELEASE_LATEST_PAGE)
}

/// 打开「报 bug」页（预填 bug 模板）；用系统浏览器，走用户自己的代理。
#[tauri::command]
fn report_bug() -> Result<(), String> {
    open_in_browser("https://github.com/HERRY423/BioCSSwitch/issues/new?template=bug_report.yml")
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

// ---------- 科研工具包 / 敏感模式 / 任务路由（bio-* 扩展）----------
