#!/usr/bin/env python3
"""简历存档守护 — CDP 注入按钮 + 轮询保存"""
import json, struct, socket, base64, os, time, subprocess, re, sys
from urllib.parse import urlparse
from datetime import datetime

CDP_PORT = 9222
SAVE_DIR = os.path.expanduser("~/Desktop/客户项目")
DB_PATH = os.path.expanduser("~/.hermes/talent_pool.db")

INJECT_JS = '''
(function() {
  if (document.getElementById("__rs_btn")) return "already";
  
  var btn = document.createElement("div");
  btn.id = "__rs_btn";
  btn.textContent = "💾 存本地";
  btn.style.cssText = "position:fixed;bottom:20px;right:20px;z-index:999999;background:#1a478a;color:#fff;padding:10px 20px;border-radius:8px;cursor:pointer;font:bold 14px system-ui;box-shadow:0 2px 12px rgba(0,0,0,.3);";
  btn.onmouseenter = function() { btn.style.background = "#2563eb"; };
  btn.onmouseleave = function() { btn.style.background = "#1a478a"; };
  btn.onclick = function() {
    btn.textContent = "⏳";
    btn.style.background = "#f59e0b";
    window.__rs_pending = {
      html: document.documentElement.outerHTML,
      url: window.location.href,
      time: new Date().toISOString()
    };
  };
  document.body.appendChild(btn);
  return "injected";
})()
'''

def quick_eval(ws_url, expr, timeout=6):
    try:
        u = urlparse(ws_url)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(4)
        sock.connect((u.hostname, u.port))
        key = base64.b64encode(os.urandom(16)).decode()
        req = f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp: resp += sock.recv(4096)
        if b"101" not in resp.split(b"\r\n")[0]: sock.close(); return None
        
        msg = json.dumps({"id":1, "method":"Runtime.evaluate", "params":{"expression":expr, "returnByValue":True}})
        data = msg.encode(); l = len(data)
        hdr = bytearray([0x81])
        if l < 126: hdr.append(0x80|l)
        else: hdr.append(0x80|126); hdr.extend(struct.pack('>H',l))
        mask = os.urandom(4)
        masked = bytearray(b ^ mask[i%4] for i,b in enumerate(data))
        sock.sendall(bytes(hdr)+mask+masked)
        
        sock.settimeout(timeout)
        deadline = time.time()+timeout
        result = None
        while time.time() < deadline:
            try:
                h2 = sock.recv(2)
                if len(h2)<2: break
                rl = h2[1]&0x7F
                if rl==126: rl=struct.unpack('>H',sock.recv(2))[0]
                elif rl==127: rl=struct.unpack('>Q',sock.recv(8))[0]
                mk=sock.recv(4); rd=b""
                while len(rd)<rl:
                    c=sock.recv(min(rl-len(rd),65536))
                    if not c: break; rd+=c
                rd=bytes(b^mk[i%4] for i,b in enumerate(rd))
                m=json.loads(rd.decode())
                if m.get("id")==1: result=m.get("result",{}).get("result",{}).get("value","");break
            except socket.timeout: break
            except: break
        sock.close()
        return result
    except: return None

def get_liepin_tabs():
    try:
        r = subprocess.run(["curl","-s",f"http://127.0.0.1:{CDP_PORT}/json/list"], capture_output=True, text=True, timeout=5)
        tabs = json.loads(r.stdout)
        resume_pages = [(t["webSocketDebuggerUrl"], t.get("url","")) for t in tabs if t.get("type")=="page" and "resume/showresumedetail" in t.get("url","")]
        if resume_pages: return resume_pages
        return [(t["webSocketDebuggerUrl"], t.get("url","")) for t in tabs if t.get("type")=="page" and "liepin.com" in t.get("url","")]
    except: return []

def main():
    print("💾 简历存档守护启动"); sys.stdout.flush()
    injected = set()
    
    while True:
        tabs = get_liepin_tabs()
        for ws, url in tabs:
            tid = ws.split("/")[-1]
            
            if tid not in injected:
                r = quick_eval(ws, INJECT_JS, timeout=5)
                if r in ("injected", "already"):
                    injected.add(tid)
                    if "resume/showresumedetail" in url:
                        print(f"✅ 简历页已就绪"); sys.stdout.flush()
            
            pending = quick_eval(ws, "JSON.stringify(window.__rs_pending || null)", timeout=4)
            if pending and pending != "null":
                try:
                    data = json.loads(pending)
                    html = data.get("html","")
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    
                    text = re.sub(r'<[^>]+>',' ', html[:8000])
                    text = re.sub(r'\s+',' ', text)
                    name = ""
                    m = re.search(r'(?:姓\s*名|姓名)[：:\s]*([\u4e00-\u9fa5]{2,4})', text)
                    if m: name = m.group(1)
                    
                    fname = f"resume_{name or 'unknown'}_{ts}.html"
                    fpath = os.path.join(SAVE_DIR, fname)
                    with open(fpath, 'w', encoding='utf-8') as f:
                        f.write(html)
                    
                    print(f"\n📥 已保存: {name or '?'} → {fname}"); sys.stdout.flush()
                    
                    quick_eval(ws, 'document.getElementById("__rs_btn").textContent="✅";document.getElementById("__rs_btn").style.background="#22c55e";setTimeout(function(){document.getElementById("__rs_btn").textContent="💾 存本地";document.getElementById("__rs_btn").style.background="#1a478a"},2000);window.__rs_pending=null', timeout=3)
                except Exception as e:
                    print(f"[Error] {e}"); sys.stdout.flush()
        
        time.sleep(1.5)

if __name__ == "__main__":
    main()
