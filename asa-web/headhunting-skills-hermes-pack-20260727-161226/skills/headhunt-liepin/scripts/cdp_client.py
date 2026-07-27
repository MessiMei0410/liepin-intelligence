#!/usr/bin/env python3
"""Minimal CDP client — send commands to Chrome via raw WebSocket (stdlib only).

No pip dependencies needed. Uses only Python stdlib: json, struct, socket,
hashlib, base64, os, sys, time.

Usage:
  python3 cdp_client.py <ws_url> [method] [params_json]

Examples:
  # Get browser version
  python3 cdp_client.py "ws://127.0.0.1:9222/devtools/browser/XXX" "Browser.getVersion"

  # Navigate to URL
  python3 cdp_client.py "ws://127.0.0.1:9222/devtools/page/XXX" "Page.navigate" '{"url":"https://example.com"}'

  # Execute JavaScript
  python3 cdp_client.py "ws://127.0.0.1:9222/devtools/page/XXX" "Runtime.evaluate" '{"expression":"document.title","returnByValue":true}'

  # Take screenshot
  python3 cdp_client.py "ws://127.0.0.1:9222/devtools/page/XXX" "Page.captureScreenshot" '{"format":"png"}'
"""
import json, struct, socket, hashlib, base64, os, sys, time
from urllib.parse import urlparse

class CDP:
    def __init__(self, ws_url):
        u = urlparse(ws_url)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((u.hostname, u.port))
        self._id = 0
        # WebSocket handshake
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {u.path} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{u.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        if b"101" not in resp.split(b"\r\n")[0]:
            status_line = resp.split(b"\r\n")[0]
            raise Exception(f"Handshake failed: {status_line}")

    def send(self, method, params=None):
        self._id += 1
        msg = json.dumps({"id": self._id, "method": method, "params": params or {}})
        frame = self._make_frame(msg)
        self.sock.sendall(frame)
        return self._recv()

    def _make_frame(self, text):
        data = text.encode()
        length = len(data)
        header = bytearray([0x81])  # FIN + text opcode
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        mask = os.urandom(4)
        masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(data))
        return bytes(header) + mask + masked

    def _recv(self, timeout=5):
        self.sock.settimeout(timeout)
        result = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                hdr = self.sock.recv(2)
                if len(hdr) < 2:
                    break
                opcode = hdr[0] & 0x0F
                masked = hdr[1] & 0x80
                length = hdr[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self.sock.recv(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self.sock.recv(8))[0]
                if masked:
                    mask_key = self.sock.recv(4)
                data = b""
                while len(data) < length:
                    chunk = self.sock.recv(min(length - len(data), 65536))
                    if not chunk:
                        break
                    data += chunk
                if masked:
                    data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
                msg = json.loads(data.decode())
                if opcode == 0x01:  # text
                    if msg.get("id") == self._id:
                        result = msg
                        break
                elif opcode == 0x08:  # close
                    break
            except socket.timeout:
                break
            except Exception as e:
                print(f"Recv error: {e}", file=sys.stderr)
                break
        return result

    def close(self):
        try:
            self.sock.close()
        except:
            pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: cdp_client.py <ws_url> [method] [params_json]")
        print("Example: cdp_client.py 'ws://127.0.0.1:9222/devtools/page/XXX' 'Browser.getVersion'")
        sys.exit(1)

    ws_url = sys.argv[1]
    method = sys.argv[2] if len(sys.argv) > 2 else "Browser.getVersion"
    params = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}

    cdp = CDP(ws_url)
    result = cdp.send(method, params)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    cdp.close()
