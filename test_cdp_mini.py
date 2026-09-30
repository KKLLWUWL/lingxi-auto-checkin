# -*- coding: utf-8 -*-
"""
cdp_mini 自测：用最小 WebSocket Mock Server 验证协议实现是否正确。

为什么要有这个测试：
    我们自己实现了 WebSocket 握手与帧解析，涉及掩码、长度扩展、分片、ping/pong，
    靠肉眼 review 不可靠。有了它，以后改动 cdp_mini 也能一键回归。

运行：
    python test_cdp_mini.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import cdp_mini
from cdp_mini import CdpSession, iter_page_targets


# ---------------------------------------------------------------- Mock 服务端
class MockCDPServer(threading.Thread):
    """极简 WebSocket 服务端，只会回 JSON-RPC 应答。"""

    daemon = True

    def __init__(self):
        super().__init__()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.clients: list[socket.socket] = []
        self.frames_sent = 0
        self._stop = False

    # ---- 服务端 -> 客户端 帧（无掩码）
    @staticmethod
    def _frame(payload: bytes) -> bytes:
        n = len(payload)
        head = bytearray([0x81])
        if n < 126:
            head.append(n)
        elif n < (1 << 16):
            head.append(126)
            head += struct.pack(">H", n)
        else:
            head.append(127)
            head += struct.pack(">Q", n)
        return bytes(head) + payload

    def run(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.clients.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def stop(self) -> None:
        self._stop = True
        for c in self.clients:
            try:
                c.close()
            except OSError:
                pass
        try:
            self.srv.close()
        except OSError:
            pass

    def _serve(self, conn: socket.socket) -> None:
        # 握手
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        key = b""
        for line in buf.decode("latin-1").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip().encode()
        accept = base64.b64encode(hashlib.sha1(key + cdp_mini.WS_GUID).digest())
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        rest = buf.split(b"\r\n\r\n", 1)[1]

        # 处理帧
        data = rest
        while True:
            msg = self._read_client_frame(conn, data)
            if msg is None:
                return
            data, text = msg
            try:
                req = json.loads(text)
            except json.JSONDecodeError:
                continue
            method = req.get("method")
            rid = req.get("id")
            if method == "Runtime.evaluate":
                expr = req["params"]["expression"]
                if "FAKE_FRAGMENTED" in expr:
                    # 把一条应答切成 3 帧（首帧 + 2 个 continuation 帧），
                    # 专门覆盖「分片合并」这条最容易写错的分支
                    payload = json.dumps({
                        "id": rid,
                        "result": {"result": {
                            "type": "string",
                            "value": "立即签到FRAGMENTED_OK",
                        }},
                    }).encode()
                    a, b, c = payload[:10], payload[10:60], payload[60:]
                    conn.sendall(bytes([0x01, len(a)]) + a)   # FIN=0 text
                    time.sleep(0.05)
                    conn.sendall(bytes([0x00, len(b)]) + b)   # FIN=0 cont
                    time.sleep(0.05)
                    conn.sendall(bytes([0x80, len(c)]) + c)   # FIN=1 cont
                    self.frames_sent += 1
                    continue
                # 让不同的表达式返回不同结果，方便断言
                if "FAKE_TASK_DONE" in expr:
                    value = "距离下次签到 05:12:33"
                elif "FAKE_TASK_TODO" in expr:
                    value = "立即签到"
                elif "FAKE_ERR" in expr:
                    conn.sendall(self._frame(json.dumps({
                        "id": rid,
                        "error": {"code": -32000, "message": "boom"},
                    }).encode()))
                    continue
                else:
                    value = expr
                conn.sendall(self._frame(json.dumps({
                    "id": rid,
                    "result": {"result": {"type": "string", "value": value}},
                }).encode()))
            elif method == "Page.captureScreenshot":
                conn.sendall(self._frame(json.dumps({
                    "id": rid,
                    "result": {"data": base64.b64encode(b"\x89PNG-fake").decode()},
                }).encode()))
            else:
                conn.sendall(self._frame(json.dumps({
                    "id": rid, "result": {},
                }).encode()))
            self.frames_sent += 1

    @staticmethod
    def _read_client_frame(conn: socket.socket, data: bytes):
        """读取一个客户端（掩码）文本帧，返回 (剩余数据, 文本)。"""
        buf = bytearray(data)
        while True:
            if len(buf) < 2:
                c = conn.recv(4096)
                if not c:
                    return None
                buf += c
                continue
            b0, b1 = buf[0], buf[1]
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            masked = b1 & 0x80
            off = 2
            if length == 126:
                while len(buf) < off + 2:
                    buf += conn.recv(4096)
                length = struct.unpack(">H", buf[off:off + 2])[0]
                off += 2
            elif length == 127:
                while len(buf) < off + 8:
                    buf += conn.recv(4096)
                length = struct.unpack(">Q", buf[off:off + 8])[0]
                off += 8
            mask = b"\x00\x00\x00\x00"
            if masked:
                while len(buf) < off + 4:
                    buf += conn.recv(4096)
                mask = bytes(buf[off:off + 4])
                off += 4
            while len(buf) < off + length:
                need = off + length - len(buf)
                buf += conn.recv(max(need, 4096))
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(buf[off:off + length]))
            del buf[:off + length]
            if opcode in (0x8,):
                return None
            if opcode == 0x9:                      # ping -> pong
                conn.sendall(MockCDPServer._frame_type(0xA, payload))
                continue
            return bytes(buf), payload.decode("utf-8", "replace")

    @staticmethod
    def _frame_type(opcode: int, payload: bytes) -> bytes:
        head = bytearray([0x80 | opcode, len(payload)])
        return bytes(head) + payload


# ---------------------------------------------------------------- 断言
def run_tests() -> int:
    srv = MockCDPServer()
    srv.start()
    time.sleep(0.3)

    passed = 0
    failed = []

    def check(name: str, cond: bool, extra: str = ""):
        nonlocal passed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed.append(name)
            print(f"  [FAIL] {name} {extra}")

    sess = None
    try:
        sess = CdpSession(f"ws://127.0.0.1:{srv.port}/devtools/page/FAKE1", timeout=5)

        print("\n-- WebSocket 握手与基础收发 --")
        r = sess.evaluate("1+1")
        check("evaluate 返回字符串透传", r == "1+1", f"got={r!r}")

        r = sess.evaluate("FAKE_TASK_TODO")
        check("取出'立即签到'文案", r == "立即签到", f"got={r!r}")

        r = sess.evaluate("FAKE_TASK_DONE")
        check("取出'距离下次签到'文案", str(r).startswith("距离下次签到"), f"got={r!r}")

        print("\n-- 错误处理 --")
        try:
            sess.evaluate("FAKE_ERR")
            check("CDP error 抛 CdpError", False, "没有抛异常")
        except cdp_mini.CdpError as e:
            check("CDP error 抛 CdpError", "boom" in str(e), str(e))

        print("\n-- 截图（二进制 base64 解码）--")
        import tempfile, os
        p = os.path.join(tempfile.gettempdir(), "_cdp_fake_shot.png")
        sess.screenshot(p)
        check("截图文件已写出", os.path.exists(p) and os.path.getsize(p) > 0)
        check("截图内容正确", open(p, "rb").read() == b"\x89PNG-fake")
        os.remove(p)

        print("\n-- 分片消息（续帧合并；CDP 大响应常见）--")
        r = sess.evaluate("FAKE_FRAGMENTED")
        check("3 帧分片合并正确", str(r).endswith("FRAGMENTED_OK"), f"got={r!r}")

        print("\n-- 大消息（>64KB，走长度扩展）--")
        big = "X" * 200000
        r = sess.evaluate(big)
        check("200KB 消息往返无误", r == big, f"len={len(str(r))}")

        print("\n-- enable_dom 容错 --")
        sess.enable_dom()
        check("enable_dom 不抛异常", True)

    finally:
        if sess:
            sess.close()
        srv.stop()

    print(f"\n结果: {passed} 项通过, {len(failed)} 项失败")
    if failed:
        print("失败项:", ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run_tests())
