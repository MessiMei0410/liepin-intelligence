#!/usr/bin/env python3
"""猎头工作流浮窗 — 项目管线 + 今日活动 + 技能覆盖"""
import tkinter as tk
import sqlite3
import os
import re
import json

DB_PATH = os.path.expanduser("~/.hermes/talent_pool.db")
TALENT_DIR = os.path.expanduser("~/Desktop/人才库")
SUMMARY_PATH = os.path.expanduser("~/.hermes/scripts/headhunt_summary.json")
WINDOW_W = 280

# GitHub 风格配色
BG = "#0d1117"
CARD_BG = "#161b22"
FG_WHITE = "#c9d1d9"
FG_DIM = "#8b949e"
FG_GREEN = "#3fb950"
FG_ORANGE = "#d2991d"
FG_BLUE = "#58a6ff"
FG_PRIMARY = "#58a6ff"
BORDER = "#21262d"

# ─── 数据层 ─────────────────────────────────────
def get_positions():
    positions = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT client, position, count(*) as n FROM candidates GROUP BY client, position"
        ).fetchall()
        conn.close()
        for client, pos, n in rows:
            key = f"{client}|{pos}"
            positions[key] = {"client": client, "position": pos, "count": n, "phase": "search", "candidate": ""}
    except:
        pass

    if os.path.exists(TALENT_DIR):
        for fname in os.listdir(TALENT_DIR):
            path = os.path.join(TALENT_DIR, fname)
            if not fname.endswith(".md"):
                continue
            try:
                with open(path) as f:
                    content = f.read()
                name_m = re.search(r"\*\*姓名\*\*[：:]\s*(.+?)$", content, re.M)
                client_m = re.search(r"目标企业[：:]\s*(.+?)$", content, re.M)
                pos_m = re.search(r"目标岗位[：:]\s*(.+?)$", content, re.M)
                name = name_m.group(1).strip() if name_m else ""
                client = client_m.group(1).strip() if client_m else ""
                pos_name = pos_m.group(1).strip() if pos_m else ""

                phase = None
                if "谈薪反馈" in fname: phase = "negotiate"
                elif "Offer确认" in fname: phase = "offer"
                elif "入职" in fname: phase = "hired"
                elif "终面" in fname: phase = "final"

                if phase and client:
                    matched = False
                    for key in positions:
                        if positions[key]["client"] == client and pos_name and pos_name in positions[key]["position"]:
                            positions[key]["phase"] = phase
                            positions[key]["candidate"] = name
                            matched = True
                            break
                    if not matched and pos_name:
                        key = f"{client}|{pos_name}"
                        if key not in positions:
                            positions[key] = {"client": client, "position": pos_name, "count": 0, "phase": phase, "candidate": name}
            except:
                pass
    return positions

def phase_info(phase):
    return {
        "search":    ("🔍 寻访", FG_BLUE),
        "final":     ("🎤 终面", FG_ORANGE),
        "negotiate": ("💰 谈薪", FG_ORANGE),
        "offer":     ("📝 Offer", FG_GREEN),
        "hired":     ("✅ 入职", FG_GREEN),
    }.get(phase, ("· 待定", FG_DIM))

def load_summary():
    if os.path.exists(SUMMARY_PATH):
        try:
            with open(SUMMARY_PATH) as f:
                return json.load(f)
        except:
            pass
    return {}

# 技能列表
SKILLS = [
    ("📋 需求分析",     "headhunting-search-strategy", True),
    ("🔍 寻访搜索",     "liepin-cdp-search",           True),
    ("🗄️ 人才库",       "talent-pool",                 True),
    ("📊 推荐报告",     "jiashi-recommendation",       True),
    ("💰 谈薪反馈",     "salary-negotiation-feedback", True),
    ("📱 小红书笔记",   "headhunt-note-generator",     True),
    ("📚 知识库",       "knowledge-base-save",         True),
]

# ─── GUI ──────────────────────────────────────────
positions = get_positions()
clients = {}
for key, p in positions.items():
    c = p["client"]
    clients.setdefault(c, []).append(p)

WINDOW_H = min(650, 120 + sum(len(v) for v in clients.values()) * 42 + len(SKILLS) * 22)

root = tk.Tk()
root.title("猎头工作流")
root.overrideredirect(True)
root.attributes("-topmost", True)
root.geometry(f"{WINDOW_W}x{WINDOW_H}+{root.winfo_screenwidth()-WINDOW_W-16}+320")
root.configure(bg=BG)

# 拖拽
dx = dy = 0
def start_drag(e):
    global dx, dy; dx, dy = e.x, e.y
def do_drag(e):
    root.geometry(f"+{root.winfo_x()+e.x-dx}+{root.winfo_y()+e.y-dy}")

# 标题栏
title_bar = tk.Frame(root, bg=BG, height=28)
title_bar.pack(fill="x")
title_bar.pack_propagate(False)
title_bar.bind("<Button-1>", start_drag)
title_bar.bind("<B1-Motion>", do_drag)

tk.Label(title_bar, text="🎯 猎头工作流", font=("Menlo", 11, "bold"),
         fg=FG_PRIMARY, bg=BG).pack(side="left", padx=(10, 0), pady=(5, 0))
tk.Label(title_bar, text="✕", font=("", 13), fg=FG_DIM, bg=BG, cursor="arrow").pack(side="right", padx=(0, 10), pady=(5, 0))
title_bar.winfo_children()[-1].bind("<Button-1>", lambda e: root.destroy())

tk.Frame(root, bg=BORDER, height=1).pack(fill="x", padx=8, pady=(2, 4))

# 可滚动内容
canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
scrollbar = tk.Scrollbar(root, orient="vertical", command=canvas.yview)
scrollbar.pack(side="right", fill="y")
canvas.pack(side="left", fill="both", expand=True)
canvas.configure(yscrollcommand=scrollbar.set)

body = tk.Frame(canvas, bg=BG)
win_id = canvas.create_window((0, 0), window=body, anchor="nw", width=WINDOW_W-18)
def _resize(e):
    canvas.itemconfig(win_id, width=e.width)
canvas.bind("<Configure>", _resize)

# ─── 岗位管线 ────────────────────────────────────
def section(parent, text):
    tk.Label(parent, text=text, font=("Menlo", 9, "bold"),
             fg=FG_DIM, bg=BG, anchor="w").pack(fill="x", padx=10, pady=(8, 3))

if clients:
    section(body, "📁 项目管线")
    for client_name, pos_list in clients.items():
        total = sum(p["count"] for p in pos_list)
        ch = tk.Frame(body, bg=BG)
        ch.pack(fill="x", padx=10, pady=(4, 0))
        tk.Label(ch, text=client_name, font=("Menlo", 9, "bold"),
                 fg=FG_WHITE, bg=BG).pack(side="left")
        tk.Label(ch, text=f"{total}人", font=("Menlo", 8), fg=FG_DIM, bg=BG).pack(side="right")

        for p in pos_list:
            badge_text, badge_color = phase_info(p["phase"])
            row = tk.Frame(body, bg=CARD_BG)
            row.pack(fill="x", padx=8, pady=1)
            tk.Label(row, text="●", font=("Menlo", 9), fg=badge_color, bg=CARD_BG).pack(side="left", padx=(6, 5))
            info = tk.Frame(row, bg=CARD_BG)
            info.pack(side="left", fill="x", expand=True, pady=3)
            title = p["position"]
            if p["candidate"]:
                title += f"  · {p['candidate']}"
            tk.Label(info, text=title, font=("Menlo", 9), fg=FG_WHITE, bg=CARD_BG, anchor="w",
                     wraplength=WINDOW_W-120).pack(anchor="w")
            right = tk.Frame(row, bg=CARD_BG)
            right.pack(side="right", padx=(0, 6))
            tk.Label(right, text=f"{p['count']}人", font=("Menlo", 8), fg=FG_DIM, bg=CARD_BG).pack(side="left", padx=(0, 4))
            tk.Label(right, text=badge_text, font=("Menlo", 8, "bold"), fg=badge_color, bg=CARD_BG).pack(side="left")

# ─── 今日活动 ────────────────────────────────────
summary = load_summary()
today = summary.get("today_activities", [])
if today:
    tk.Frame(body, bg=BORDER, height=1).pack(fill="x", padx=8, pady=(8, 4))
    section(body, "📌 今日活动")
    for act in today:
        tk.Label(body, text=f"▸ {act}", font=("Menlo", 8),
                 fg=FG_DIM, bg=BG, anchor="w", wraplength=WINDOW_W-30).pack(fill="x", padx=14, pady=1)

# ─── 技能覆盖 ────────────────────────────────────
tk.Frame(body, bg=BORDER, height=1).pack(fill="x", padx=8, pady=(8, 4))
section(body, "🧩 已实现技能")
for icon_name, skill_name, ok in SKILLS:
    sf = tk.Frame(body, bg=BG)
    sf.pack(fill="x", padx=10)
    status = "✓" if ok else "○"
    color = FG_GREEN if ok else FG_DIM
    tk.Label(sf, text=f"  {status} {icon_name}", font=("Menlo", 9),
             fg=color, bg=BG).pack(side="left")
    tk.Label(sf, text=skill_name, font=("Menlo", 8), fg=FG_DIM, bg=BG).pack(side="right")

tk.Frame(body, bg=BG, height=6).pack(fill="x")

# 滚动
body.update_idletasks()
canvas.configure(scrollregion=canvas.bbox("all"))

root.bind("<Escape>", lambda e: root.destroy())
root.bind("<Button-3>", lambda e: root.destroy())
body.bind("<Button-3>", lambda e: root.destroy())
body.bind("<Button-1>", start_drag)
body.bind("<B1-Motion>", do_drag)
canvas.bind("<Button-1>", start_drag)
canvas.bind("<B1-Motion>", do_drag)

root.mainloop()
