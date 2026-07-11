import os
import socket
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _capability import loopback_available

HERE = os.path.dirname(__file__)
PROXY = os.path.join(HERE, "..", "proxy", "csswitch_proxy.py")
SEC = "streamtok"

# 这里覆盖两个流式代理边界：
#   1) 上游首帧很小且不断开时，代理必须按 SSE 行及时转发，不能等 EOF/攒满缓冲；
#   2) 上游在响应头已发之后中断时，代理必须以干净 SSE error + chunked 终止块收尾。
#
# http.client 对「Content-Length 声明过大、提前断连」这种截断走的是 readinto 静默 EOF
# 路径（不抛异常，见 CPython http/client.py HTTPResponse.readinto），无法用来逼出
# 「头已发出后流中途异常」这个场景；chunked 传输编码则不同：分块框架一旦被截断，
# http.client 的 _readinto_chunked/_safe_readinto 会确定性抛 IncompleteRead
# （或底层 socket 层的 ConnectionResetError，取决于 close 时机，两者都是普通
# Exception，代理侧都会被同一个 except 分支捕获）。
# 因此中断测试改用 chunked：先发一个完整合法首块，让代理正常发出 200 响应头；
# 再发一个声明了长度却只给一小段就断连的第二块，逼代理循环里的第二次
# readline 在「头已发出」之后抛异常，从而命中被测的「流中断」收尾分支。


def read_http_request(conn):
    """Consume one complete HTTP request before a fake upstream responds.

    Windows may reset a TCP connection when this fixture closes after only one
    ``recv`` while the proxy is still sending a request body.  That turns a
    deterministic upstream status test into a local ``WinError 10054`` race.
    Reading the declared body mirrors a real HTTP peer and keeps the fixture's
    response semantics stable across platforms.
    """
    conn.settimeout(2)
    received = bytearray()
    while b"\r\n\r\n" not in received:
        try:
            chunk = conn.recv(4096)
        except OSError:
            return
        if not chunk:
            return
        received.extend(chunk)
    head, body = bytes(received).split(b"\r\n\r\n", 1)
    content_length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                content_length = int(value.strip() or b"0")
            except ValueError:
                content_length = 0
            break
    while len(body) < content_length:
        try:
            chunk = conn.recv(min(4096, content_length - len(body)))
        except OSError:
            return
        if not chunk:
            return
        body += chunk


def start_dropping_upstream():
    """假上游：chunked 传输，首块完整合法（让代理先正常发出 200 响应头），
    随后声明第二块但提前断连，逼代理在头已发出后的下一次 readline 抛异常。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                read_http_request(c)
                payload = ("event: content_block_delta\n"
                           "data: {\"type\":\"content_block_delta\"}\n\n")
                pad = ":" + "p" * (4096 - len(payload) - 2) + "\n"  # SSE 注释行，纯 padding
                payload += pad
                assert len(payload) == 4096, len(payload)
                head = ("HTTP/1.1 200 OK\r\n"
                        "Content-Type: text/event-stream\r\n"
                        "Transfer-Encoding: chunked\r\n\r\n")
                chunk1 = hex(len(payload))[2:] + "\r\n" + payload + "\r\n"
                chunk2_partial = "1f4\r\n" + "0123456789"  # 声明 500 字节，只发 10 字节就断
                try:
                    c.sendall((head + chunk1 + chunk2_partial).encode())
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{port}/up", srv


def start_slow_first_frame_upstream():
    """假上游：立刻发一个很小的 SSE 行，然后延迟收尾。
    旧的 read(4096) 会等到 EOF/更多字节；line-based 读应马上把首行转给下游。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                read_http_request(c)
                head = ("HTTP/1.1 200 OK\r\n"
                        "Content-Type: text/event-stream\r\n"
                        "Transfer-Encoding: chunked\r\n\r\n")
                first = b"event: message_start\n"
                try:
                    c.sendall(head.encode() + hex(len(first))[2:].encode() + b"\r\n" + first + b"\r\n")
                    time.sleep(1.2)
                    last = b"data: {\"type\":\"message_start\"}\n\n"
                    c.sendall(hex(len(last))[2:].encode() + b"\r\n" + last + b"\r\n0\r\n\r\n")
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{port}/up", srv


def start_delayed_response_upstream():
    """假上游：接到请求后延迟一段时间才发响应头和首帧。
    代理应先给 Science 打开下游 SSE 响应，避免客户端在上游 TTFT 较长时先超时断开。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                read_http_request(c)
                time.sleep(1.2)
                head = ("HTTP/1.1 200 OK\r\n"
                        "Content-Type: text/event-stream\r\n"
                        "Transfer-Encoding: chunked\r\n\r\n")
                first = b"event: message_start\n"
                try:
                    c.sendall(head.encode() + hex(len(first))[2:].encode() + b"\r\n" + first + b"\r\n0\r\n\r\n")
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{port}/up", srv


def start_status_upstream(status, body=b'{"error":"bad key"}'):
    """Fake upstream that immediately returns an HTTP error status."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                read_http_request(c)
                head = (f"HTTP/1.1 {status} Upstream Error\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "Connection: close\r\n\r\n").encode()
                try:
                    c.sendall(head + body)
                    c.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{port}/up", srv


def raw_post(host, port, path, body):
    s = socket.create_connection((host, port), timeout=5)
    req = (f"POST {path} HTTP/1.1\r\nHost: {host}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
           f"Connection: close\r\n\r\n").encode() + body
    s.sendall(req)
    chunks = []
    while True:
        d = s.recv(65536)
        if not d:
            break
        chunks.append(d)
    s.close()
    return b"".join(chunks)


def raw_post_until(host, port, path, body, needle, timeout=0.7):
    s = socket.create_connection((host, port), timeout=5)
    s.settimeout(timeout)
    req = (f"POST {path} HTTP/1.1\r\nHost: {host}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
           f"Connection: close\r\n\r\n").encode() + body
    t0 = time.monotonic()
    s.sendall(req)
    chunks = []
    try:
        while needle not in b"".join(chunks):
            d = s.recv(65536)
            if not d:
                break
            chunks.append(d)
    except socket.timeout:
        pass
    finally:
        elapsed = time.monotonic() - t0
        s.close()
    return b"".join(chunks), elapsed


@unittest.skipUnless(loopback_available(), "env-blocked: loopback bind/connect not permitted")
class StreamInterruption(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up_url, cls.up_sock = start_dropping_upstream()
        cls.port = 18973  # S0 全局唯一端口：StreamInterruption
        env = dict(os.environ, DEEPSEEK_API_KEY="fake",
                   CSSWITCH_UPSTREAM_URL=cls.up_url)
        cls.proc = subprocess.Popen(
            [sys.executable, PROXY, "--provider", "deepseek",
             "--port", str(cls.port), "--auth-token", SEC],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.up_sock.close()

    def test_midstream_error_yields_clean_sse_not_spliced_json(self):
        body = b'{"model":"claude-opus-4-8","max_tokens":10,"stream":true,' \
               b'"messages":[{"role":"user","content":"hi"}]}'
        raw = raw_post("127.0.0.1", self.port, f"/{SEC}/v1/messages", body)
        head, _, tail = raw.partition(b"\r\n\r\n")
        self.assertIn(b"200", head.split(b"\r\n")[0])          # 头已发 200
        self.assertIn(b"event: error", tail)                   # 干净的 SSE 错误帧
        self.assertTrue(raw.rstrip().endswith(b"0"))           # chunked 终止块
        self.assertNotIn(b"HTTP/1.1 502", tail)                # 未把 502 拼进流


@unittest.skipUnless(loopback_available(), "env-blocked: loopback bind/connect not permitted")
class StreamFlushesFirstLine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up_url, cls.up_sock = start_slow_first_frame_upstream()
        cls.port = 18974  # S0 全局唯一端口：StreamFlushesFirstLine
        env = dict(os.environ, DEEPSEEK_API_KEY="fake",
                   CSSWITCH_UPSTREAM_URL=cls.up_url)
        cls.proc = subprocess.Popen(
            [sys.executable, PROXY, "--provider", "deepseek",
             "--port", str(cls.port), "--auth-token", SEC],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.up_sock.close()

    def test_first_sse_line_is_forwarded_before_upstream_eof(self):
        body = b'{"model":"claude-opus-4-8","max_tokens":10,"stream":true,' \
               b'"messages":[{"role":"user","content":"hi"}]}'
        raw, elapsed = raw_post_until(
            "127.0.0.1", self.port, f"/{SEC}/v1/messages", body,
            b"event: message_start", timeout=0.7)
        self.assertIn(b"200", raw.split(b"\r\n", 1)[0])
        self.assertIn(b"event: message_start", raw)
        self.assertLess(elapsed, 0.7)


@unittest.skipUnless(loopback_available(), "env-blocked: loopback bind/connect not permitted")
class StreamHeadersOpenBeforeUpstreamTtft(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up_url, cls.up_sock = start_delayed_response_upstream()
        cls.port = 18975  # S0 全局唯一端口：StreamHeadersOpenBeforeUpstreamTtft
        env = dict(os.environ, DEEPSEEK_API_KEY="fake",
                   CSSWITCH_UPSTREAM_URL=cls.up_url)
        cls.proc = subprocess.Popen(
            [sys.executable, PROXY, "--provider", "deepseek",
             "--port", str(cls.port), "--auth-token", SEC],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.up_sock.close()

    def test_downstream_sse_keepalive_does_not_wait_for_upstream_first_byte(self):
        body = b'{"model":"claude-opus-4-8","max_tokens":10,"stream":true,' \
               b'"messages":[{"role":"user","content":"hi"}]}'
        raw, elapsed = raw_post_until(
            "127.0.0.1", self.port, f"/{SEC}/v1/messages", body,
            b": csswitch-keepalive", timeout=1.1)
        self.assertIn(b"HTTP/1.1 200", raw)
        self.assertIn(b"Content-Type: text/event-stream", raw)
        self.assertIn(b": csswitch-keepalive", raw)
        self.assertLess(elapsed, 1.1)


@unittest.skipUnless(loopback_available(), "env-blocked: loopback bind/connect not permitted")
class StreamUpstreamStatusAfterHeaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up_url, cls.up_sock = start_status_upstream(401)
        cls.port = 18976  # S0 全局唯一端口：StreamUpstreamStatusAfterHeaders
        env = dict(os.environ, DEEPSEEK_API_KEY="fake",
                   CSSWITCH_UPSTREAM_URL=cls.up_url)
        cls.proc = subprocess.Popen(
            [sys.executable, PROXY, "--provider", "deepseek",
             "--port", str(cls.port), "--auth-token", SEC],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.up_sock.close()

    def test_stream_upstream_http_error_is_sse_error_after_early_200(self):
        body = b'{"model":"claude-opus-4-8","max_tokens":10,"stream":true,' \
               b'"messages":[{"role":"user","content":"hi"}]}'
        raw = raw_post("127.0.0.1", self.port, f"/{SEC}/v1/messages", body)
        head, _, tail = raw.partition(b"\r\n\r\n")
        self.assertIn(b"HTTP/1.1 200", head)
        self.assertIn(b"Content-Type: text/event-stream", head)
        self.assertIn(b"event: error", tail)
        self.assertIn(b"upstream 401", tail)
        self.assertNotIn(b"HTTP/1.1 401", raw)


if __name__ == "__main__":
    unittest.main()
