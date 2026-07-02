//! 进程管家用到的纯 std 辅助：探活、依赖定位、一次性 secret 生成、上游可达性。
//! 无第三方依赖，便于单测；有状态的子进程编排放在 lib.rs（持 Child 句柄）。

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::time::Duration;

/// 对本地回环代理做 HTTP 探活：`GET /<secret>/health`，响应状态行含 200 即视为健康。
/// 代理带 path-secret 鉴权时必须带上 secret，否则会拿到 403。
pub fn http_health(port: u16, secret: Option<&str>, timeout_ms: u64) -> bool {
    let addr = match ("127.0.0.1", port).to_socket_addrs().ok().and_then(|mut a| a.next()) {
        Some(a) => a,
        None => return false,
    };
    let dur = Duration::from_millis(timeout_ms);
    let mut stream = match TcpStream::connect_timeout(&addr, dur) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(dur));
    let _ = stream.set_write_timeout(Some(dur));
    let path = match secret {
        Some(s) if !s.is_empty() => format!("/{s}/health"),
        _ => "/health".to_string(),
    };
    let req = format!("GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = Vec::new();
    // 只需读到状态行；读上限防呆。
    let mut chunk = [0u8; 1024];
    while buf.len() < 8192 {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
            Err(_) => break,
        }
        if buf.windows(4).any(|w| w == b"\r\n\r\n") {
            break;
        }
    }
    let head = String::from_utf8_lossy(&buf);
    let status_line = head.lines().next().unwrap_or("");
    // 形如 "HTTP/1.1 200 OK"：严格取第二段等于 200，避免 contains 误配 reason phrase。
    status_line.split_whitespace().nth(1) == Some("200")
}

/// 向本地回环代理 POST 一段 JSON（`POST /<secret><path>`），返回 HTTP 响应状态码；
/// 连不上 / 无响应返回 None。用于「存 key 后用最小请求真正验一次 key」——
/// 请求经代理打到上游，200=可用，401/403=key 被拒。回环明文，无需 TLS。
/// timeout_ms 要给足（代理要转发上游），建议调用方传 ~15000。
pub fn http_post_status(
    port: u16,
    secret: Option<&str>,
    path_suffix: &str,
    body: &[u8],
    timeout_ms: u64,
) -> Option<u16> {
    let addr = ("127.0.0.1", port).to_socket_addrs().ok()?.next()?;
    let dur = Duration::from_millis(timeout_ms);
    let mut stream = TcpStream::connect_timeout(&addr, dur).ok()?;
    let _ = stream.set_read_timeout(Some(dur));
    let _ = stream.set_write_timeout(Some(dur));
    let path = match secret {
        Some(s) if !s.is_empty() => format!("/{s}{path_suffix}"),
        _ => path_suffix.to_string(),
    };
    let req = format!(
        "POST {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(req.as_bytes()).ok()?;
    stream.write_all(body).ok()?;
    // 只需读到状态行（非流式响应，状态行随首个头块到达）；读上限防呆。
    let mut buf = Vec::new();
    let mut chunk = [0u8; 1024];
    while buf.len() < 8192 {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
            Err(_) => break,
        }
        if buf.windows(4).any(|w| w == b"\r\n\r\n") {
            break;
        }
    }
    let head = String::from_utf8_lossy(&buf);
    let status_line = head.lines().next().unwrap_or("");
    status_line.split_whitespace().nth(1).and_then(|s| s.parse::<u16>().ok())
}

/// 上游主机可达性（仅 TCP 连通，不校验 key）。绿灯=可达，黄灯=不可达。
pub fn tcp_reachable(host: &str, port: u16, timeout_ms: u64) -> bool {
    let dur = Duration::from_millis(timeout_ms);
    match (host, port).to_socket_addrs() {
        Ok(addrs) => {
            for a in addrs {
                if TcpStream::connect_timeout(&a, dur).is_ok() {
                    return true;
                }
            }
            false
        }
        Err(_) => false,
    }
}

/// 在 PATH 里找可执行文件（简易 which）。找不到返回 None。
pub fn which(name: &str) -> Option<PathBuf> {
    // 绝对/相对路径直接判定。
    let p = PathBuf::from(name);
    if p.is_absolute() {
        return if is_exec(&p) { Some(p) } else { None };
    }
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let cand = dir.join(name);
        if is_exec(&cand) {
            return Some(cand);
        }
    }
    None
}

fn is_exec(p: &std::path::Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    match std::fs::metadata(p) {
        Ok(md) => md.is_file() && (md.permissions().mode() & 0o111 != 0),
        Err(_) => false,
    }
}

/// 生成一次性 path-secret：从 /dev/urandom 取 16 字节，hex 编码为 32 字符。
/// 失败关闭：urandom 不可用时返回 Err，绝不退回可猜的弱 secret（宁可起代理失败）。
pub fn gen_secret() -> std::io::Result<String> {
    use std::fs::File;
    let mut b = [0u8; 16];
    let mut f = File::open("/dev/urandom")?;
    f.read_exact(&mut b)?;
    Ok(hex(&b))
}

fn hex(bytes: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for &byte in bytes {
        s.push(H[(byte >> 4) as usize] as char);
        s.push(H[(byte & 0xf) as usize] as char);
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn health_false_when_nothing_listening() {
        // 一个几乎肯定没人监听的高端口。
        assert!(!http_health(59999, None, 300));
    }

    #[test]
    fn which_finds_sh() {
        let sh = which("sh");
        assert!(sh.is_some(), "PATH 里应能找到 sh");
        assert!(sh.unwrap().is_absolute());
    }

    #[test]
    fn which_absent_returns_none() {
        assert!(which("definitely-not-a-real-binary-xyzzy").is_none());
    }

    #[test]
    fn gen_secret_is_32_hex_and_varies() {
        let a = gen_secret().unwrap();
        let b = gen_secret().unwrap();
        assert_eq!(a.len(), 32, "urandom 路径应是 32 hex 字符");
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b, "两次生成应不同");
    }

    #[test]
    fn hex_encodes_known_bytes() {
        assert_eq!(hex(&[0x00, 0x0f, 0xff, 0xa5]), "000fffa5");
    }
}
