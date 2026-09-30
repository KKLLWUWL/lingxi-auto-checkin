# -*- coding: utf-8 -*-
"""
极简 CDP（Chrome DevTools Protocol）客户端 —— 纯标准库实现
========================================================

为什么不用 playwright / selenium？
    做「每天开机自动签到」这种常驻任务，依赖越少越不容易坏。
    本模块只用 Python 标准库（socket / json / base64 / hashlib），
    免安装任何第三方包，拷到任何一台装了 Python 的 Windows 上都能跑。

实现的功能：
    * WebSocket 握手 + 文本帧收发（带分片合并、ping/pong、掩码）
    * 列举调试目标（/json/list）
    * Runtime.evaluate 执行 JS（支持 awaitPromise）
    * Page.captureScreenshot 截图
    * Page.navigate 导航

不支持 HTTPS + wss（本地 CDP 都是明文 ws://127.0.0.1，够用）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.request
from typing import Any, Iterator

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CdpError(RuntimeError):
    """CDP 返回错误，或协议层面出错。"""


# --------------------------------------------------------------------------
# WebSocket 最小实现
# --------------------------------------------------------------------------
class WebSocket:
    """够 CDP 用的最小 WebSocket 客户端（仅客户端掩码帧，仅文本帧读取）。"""

    def __init__(self, url: str, timeout: float = 20.0):
        self.url = url
        self._sock: socket.socket | None = None
        self._timeout = timeout
        self._buf = bytearray()
        self._connect()

    # ---------------- 连接
    def _connect(self) -> None:
        assert self.url.startswith("ws://"), f"不支持的协议: {self.url}"
        rest = self.url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, _, port_s = hostport.partition(":")
        port = int(port_s or 80)

        sock = socket.create_connection((host, port), timeout=self._timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(req.encode())

        # 读握手响应头
        resp = bytearray()
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise CdpError("WebSocket 握手失败：连接被关闭")
            resp += chunk
        head = resp.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        if "101" not in head.splitlines()[0]:
            raise CdpError(f"WebSocket 握手失败: {head.splitlines()[0]}")
        # 余下的数据可能已经是数据帧，放回缓冲
        tail = resp.split(b"\r\n\r\n", 1)[1]
        self._buf += tail
        self._sock = sock

    # ---------------- 帧收发
    @staticmethod
    def _recv_more(sock: socket.socket, n: int = 65536) -> bytes:
        """读一段数据。注意：不能要求收满 n，否则对端只发几十字节时会一直阻塞。"""
        chunk = sock.recv(n)
        if not chunk:
            raise CdpError("WebSocket 连接中断")
        return chunk

    def _read_one_frame(self) -> tuple[int, bool, bytes]:
        """读单个 WebSocket 帧，返回 (opcode, fin, payload)。"""
        if self._sock is None:
            raise CdpError("WebSocket 未连接")
        while len(self._buf) < 2:
            self._buf += self._recv_more(self._sock)
        b0, b1 = self._buf[0], self._buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        offset = 2
        if length == 126:
            while len(self._buf) < offset + 2:
                self._buf += self._recv_more(self._sock)
            length = struct.unpack(">H", self._buf[offset:offset + 2])[0]
            offset += 2
        elif length == 127:
            while len(self._buf) < offset + 8:
                self._buf += self._recv_more(self._sock)
            length = struct.unpack(">Q", self._buf[offset:offset + 8])[0]
            offset += 8
        if masked:  # 服务端通常不掩码，这里做防御性兼容
            while len(self._buf) < offset + 4:
                self._buf += self._recv_more(self._sock)
            offset += 4

        while len(self._buf) < offset + length:
            need = offset + length - len(self._buf)
            self._buf += self._recv_more(self._sock, max(need, 65536))
        payload = bytes(self._buf[offset:offset + length])
        del self._buf[:offset + length]
        return opcode, fin, payload

    def _next_frame(self) -> tuple[int, bytes]:
        """读一条完整消息（自动合并分片），返回 (opcode, payload)。

        关键点：分帧消息的后续帧 opcode 为 0x0（continuation），
        只有最后一帧的 FIN 为 1，因此在读到 FIN 前必须持续拼接。
        """
        opcode = None
        chunks: list[bytes] = []
        while True:
            op, fin, payload = self._read_one_frame()
            if op == 0x9:                       # ping -> pong
                self._send_raw(0xA, payload)
                continue
            if op == 0xA:                       # pong，忽略
                continue
            if op == 0x8:                       # close
                raise CdpError("WebSocket 被对端关闭")
            if op != 0x0:                       # 首帧才有真实 opcode
                opcode = op
            chunks.append(payload)
            if fin:
                return (opcode or 0x0), b"".join(chunks)

    def _send_raw(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        header = bytearray([0x80 | opcode])
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        assert self._sock is not None
        self._sock.sendall(bytes(header) + masked)

    # ---------------- 文本帧
    def send_text(self, text: str) -> None:
        self._send_raw(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        while True:
            opcode, payload = self._next_frame()
            if opcode == 0x1:
                return payload.decode("utf-8", errors="replace")
            if opcode == 0x2:      # 二进制帧，CDP 一般用不到
                continue
            raise CdpError(f"意外的 opcode: {opcode}")

    def close(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        finally:
            self._sock = None


# --------------------------------------------------------------------------
# CDP 会话
# --------------------------------------------------------------------------
class CdpSession:
    """到一个调试目标的 CDP 会话。"""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        self._ws = WebSocket(ws_url, timeout=timeout)
        self._id = 0
        self._timeout = timeout

    def close(self) -> None:
        self._ws.close()

    def call(self, method: str, params: dict | None = None,
             timeout: float | None = None) -> dict:
        """同步调用一个 CDP 方法，返回结果 dict。"""
        self._id += 1
        msg_id = self._id
        self._ws.send_text(json.dumps(
            {"id": msg_id, "method": method, "params": params or {}},
            ensure_ascii=False,
        ))
        deadline = time.time() + (timeout or self._timeout)
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise CdpError(f"CDP {method} 超时")
            raw = self._ws.recv_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != msg_id:
                continue                       # 事件通知，跳过
            if "error" in msg:
                err = msg["error"]
                raise CdpError(
                    f"CDP {method} 失败: {err.get('message')} "
                    f"(code={err.get('code')})"
                )
            return msg.get("result") or {}

    # ---------------- 常用操作
    def evaluate(self, expression: str, await_promise: bool = False,
                 return_by_value: bool = True) -> Any:
        """执行 JS 并返回结果值。表达式抛错时抛出 CdpError。"""
        res = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": return_by_value,
            "awaitPromise": await_promise,
            "userGesture": True,          # 让 click() 之类的用户手势生效
            "allowUnsafeEvalBlockedByCSP": True,
        })
        details = res.get("exceptionDetails")
        if details:
            raise CdpError("JS 执行异常: " + json.dumps(details, ensure_ascii=False)[:400])
        remote = res.get("result", {})
        if "value" in remote:
            return remote["value"]
        if remote.get("type") == "undefined":
            return None
        return remote.get("description", remote.get("type"))

    def screenshot(self, path: str) -> str:
        res = self.call("Page.captureScreenshot", {"format": "png"})
        data = res.get("data", "")
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path

    def enable_dom(self) -> None:
        for m in ("Runtime.enable", "Page.enable"):
            try:
                self.call(m)
            except CdpError:
                pass


# --------------------------------------------------------------------------
# HTTP 部分
# --------------------------------------------------------------------------
def http_json(endpoint_port: int, path: str, timeout: float = 5.0) -> Any:
    url = f"http://127.0.0.1:{endpoint_port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def iter_page_targets(endpoint_port: int) -> Iterator[dict]:
    for t in http_json(endpoint_port, "/json/list"):
        if t.get("type") == "page":
            yield t


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19222
    print(json.dumps(http_json(port, "/json/version"), ensure_ascii=False, indent=2))
    for t in iter_page_targets(port):
        print(f"[{t.get('type')}] {t.get('title')!r} {t.get('url')[:100]}")
