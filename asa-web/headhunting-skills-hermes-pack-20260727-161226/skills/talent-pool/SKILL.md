---
name: talent-pool
description: "Save searched candidates to a local SQLite talent database. Insert, query, update status, track iterations, client feedback, and anchor candidates. Persistent cross-project candidate pool with full iteration lifecycle."
version: 2.1.0
author: Hermes Agent
---

# Talent Pool — 本地人才库 (SQLite v2.0)

Trigger: after completing a 猎聘 search, or when the user asks to "存到人才库" / "查人才库" / "更新状态" / "标记已推荐" / "记录客户反馈".

## 岗位库优先硬规则

所有涉及岗位的查询、统计、归因、推荐、触达复盘，必须先读标准岗位库：

```sql
SELECT * FROM positions WHERE client = ? AND status = 'open';
SELECT * FROM position_profiles WHERE client = ?;
```

- `positions.title` 和 `position_profiles.position` 是标准岗位名。
- `position_profiles.source_position_ids_json` 是画像与 `positions.id` 的绑定。
- `candidates.position` 和 `candidate_clients.position_tag` 是历史执行归属，可能存在旧合并岗位名；不能直接作为当前岗位方向口径。
- 发现候选人挂在旧合并岗位名下时，先输出归一化审计清单，再更新归属；不要静默改库。

## When to use

- After `liepin-cdp-search` → save candidates with iteration number
- When E简历 plugin captures a resume (via CDP Fetch interceptor) → auto-save to pool
- User marks candidates as "已推荐给客户" → update status + recommended_to_client date
- Client gives feedback → update client_feedback, elimination_reason
- Re-search same position → auto-increment iteration, auto-exclude already-recommended
- "找更多像张**这样的人" → mark as anchor_candidate, extract profile for new search

## Data sources

| Source | Method | Auto-save |
|--------|--------|-----------|
| 猎聘 CDP 搜索 | `liepin-cdp-search` → 直接 INSERT | Yes |
| E简历插件导入 | CDP Fetch 拦截 → 解析 HTML → INSERT | Yes (via `resume_interceptor.py`) |
| 手动录入 | 用户口述 → `salary-negotiation-feedback` 生成 .md | Manual |

## Database

Path: `~/.hermes/talent_pool.db`

### Schema v2.0 (不存储 res_id)

res_id 在猎聘每次搜索中都会变化（会话级），**不能存储**。候选人去重用 `UNIQUE(name, company, client, position)`。

但可持久化保存**候选人链接文件**（HTML 含 res_id_encode URL）到 `~/Desktop/客户项目/{客户}/候选人链接_{岗位}_{date}.html`。后续生成多渠道搜索报告时，从这些本地链接文件中按 **岗位 + 公司名** 匹配候选人行，补上可点击的简历链接。详见 `multi-channel-search` skill 的 Step 4b。

```sql
candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    company TEXT, title TEXT, education TEXT, experience TEXT,
    skills TEXT, level TEXT,
    city TEXT, client TEXT, position TEXT,
    search_date TEXT, status TEXT DEFAULT 'new', notes TEXT,
    iteration INTEGER DEFAULT 1,
    recommended_to_client TEXT, client_feedback TEXT,
    elimination_reason TEXT, anchor_candidate INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT,
    UNIQUE(name, company, client, position)  -- v2: 姓名+公司去重
)
```

res_id 在每次生成 HTML 报告时从搜索结果现场抓取（通过 window.open 拦截），不存入数据库。
去重用 `name + company + client + position` 组合键。

### 岗位统计查询 (v2.1)

```sql
-- 查询某个客户下各岗位的候选人数量
SELECT position, COUNT(*) as cnt FROM candidates 
WHERE client = ? AND position IS NOT NULL AND position != '' 
GROUP BY position ORDER BY cnt DESC
```

在 Swift DataManager 中封装为：
```swift
func positionCounts(for client: String) -> [(String, Int)]
```

用于侧边栏树形结构展示客户→岗位层级。

### Status values (v2.0)

```
new → recommended → client_approved → contacted → interviewing → offered → hired
new → recommended → client_rejected (→ elimination_reason filled)
new → passed (→ elimination_reason filled)
backup
```

---

## Operations

### 0. Candidate-Client M-M 关联 (v2.2)

一个候选人在不同搜索中可能与多个客户关联（如应用材料的设备工程师 → 微导纳米 + 鹏新旭都想要）。需要 M-M 桥表：

```sql
CREATE TABLE IF NOT EXISTS candidate_clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_name TEXT NOT NULL,
    candidate_company TEXT,
    client TEXT NOT NULL,
    source TEXT,
    position_tag TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(candidate_name, candidate_company, client)
);
```

插入时先写 `candidates` 表（按 name+company 去重），再写 `candidate_clients`（按 name+company+client 去重）：

```python
cur.execute("INSERT OR IGNORE INTO candidates (name,company,...) VALUES (?,?,...)")
cur.execute("INSERT OR IGNORE INTO candidate_clients (candidate_name,candidate_company,client,source,position_tag) VALUES (?,?,?,?,?)",
           (name, company, client, 'liepin', position))
```

查询某客户的候选人：
```sql
SELECT cc.*, c.education, c.experience, c.city
FROM candidate_clients cc
LEFT JOIN candidates c ON c.name = cc.candidate_name
WHERE cc.client = '微导纳米';
```

#### 自动同步触发器（v2.3）

`candidates` 表 INSERT 后，通过触发器自动写入 `candidate_clients`，**不再需要手动双写**：

```sql
-- 唯一索引（防重复）
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_clients_unique
    ON candidate_clients(candidate_name, candidate_company, client);

-- 触发器：candidates INSERT → candidate_clients 自动同步
CREATE TRIGGER IF NOT EXISTS trg_candidates_after_insert
AFTER INSERT ON candidates
BEGIN
    INSERT OR IGNORE INTO candidate_clients
        (candidate_name, candidate_company, client, source, position_tag)
    VALUES (NEW.name, NEW.company, NEW.client, NEW.source, NEW.position);
END;
```

字段映射：`candidates.name→candidate_name`, `.company→candidate_company`, `.client→client`, `.source→source`, `.position→position_tag`。

**使用方式**：搜索入库时只需 `INSERT INTO candidates`，触发器自动同步 `candidate_clients`。`DataManager.swift` 的 `insertCandidate()` 无需修改。

### 1. Profile-based strategy building (人才画像 → 寻访策略)

When talent pool already has candidates for a client/position, use them to build a profile BEFORE searching:

1. Query all candidates for the client/position
2. Extract patterns: education backgrounds, previous companies, experience years, skills
3. Build a "候选人画像" section with hard requirements derived from real data
4. Map Tier 1/2/3 target companies based on where existing talent came from
5. Feed this into `headhunting-search-strategy` as the research foundation

This is the fastest path to a data-driven search strategy — real candidates tell you where to look.

For semiconductor TME / FAE / 技术市场 / 产品市场 profiles, extract and store three fit layers in `candidate_profiles` / `candidate_intelligence` whenever evidence exists:

- Product line: the concrete product family handled by the candidate.
- Application market: the market/application the product served.
- Customer scene: customer type and engagement mode.

Do not let benchmark-company membership alone drive `level='A'` or high `fit_score`. If company/title matches but the product line or application market mismatches, downgrade to B/C and add risk tags such as `benchmark_company_only`, `product_line_mismatch`, `market_segment_mismatch`. If the candidate was already contacted before the mismatch was discovered, preserve `status='contacted'` but set `recommendation_decision='contacted_but_misaligned'` in `candidate_intelligence`.

### 1. Insert candidates

```python
for c in candidates:
    conn.execute("""
        INSERT OR IGNORE INTO candidates 
        (name, company, title, education, experience, skills, city, client, position, search_date, status, iteration)
        VALUES (?,?,?,?,?,?,?,?,?,date('now'),'new',?)
    """, (c['name'], c['company'], c['title'], c['education'], c['experience'],
          c['skills'], c['city'], c['client'], c['position'], iteration))
```

### 2. Mark candidates as recommended to client

```python
res_ids = [...]  # list of res_ids being recommended
conn.executemany(
    "UPDATE candidates SET status='recommended', recommended_to_client=date('now'), updated_at=datetime('now','localtime') WHERE res_id=?",
    [(rid,) for rid in res_ids])
conn.commit()
```

### 3. Record client feedback

```python
# Client approved the direction
conn.execute("""
    UPDATE candidates SET status='client_approved', client_feedback=?, updated_at=datetime('now','localtime') 
    WHERE res_id=?
""", (feedback, res_id))

# Client rejected — record reason
conn.execute("""
    UPDATE candidates SET status='client_rejected', client_feedback=?, elimination_reason=?, updated_at=datetime('now','localtime') 
    WHERE res_id=?
""", (feedback, reason, res_id))
```

### 4. Auto-exclude already-recommended on re-search (已推荐排除 — 优化5)

Before starting a new search iteration, get the exclusion list:

```python
# Get res_ids already recommended for this position
excluded = conn.execute(
    "SELECT res_id FROM candidates WHERE client=? AND position=? AND status IN ('recommended','client_approved','client_rejected')",
    (client, position)).fetchall()
excluded_ids = [r[0] for r in excluded]

# During new search evaluation, skip any candidate whose res_id is in excluded_ids
```

### 5. Anchor-based search (方向锚定 — 优化6)

When user says "找更多像{name}这样的人":

```python
# Get anchor candidate profile
anchor = conn.execute(
    "SELECT company, title, skills, education, city FROM candidates WHERE name LIKE ?",
    (f"%{name}%",)).fetchone()

# Mark as anchor
conn.execute("UPDATE candidates SET anchor_candidate=1 WHERE name LIKE ?", (f"%{name}%",))

# Extract search template:
# - Company → add to Tier 1 target companies
# - Skills → generate new keyword combinations
# - Education/background → refine candidate profile
```

### 6. Iteration summary query

```sql
-- Candidates by iteration
SELECT iteration, status, COUNT(*) FROM candidates 
WHERE client='集萃苏科思' AND position='运动台产品经理'
GROUP BY iteration, status ORDER BY iteration;

-- New candidates in latest iteration (not in previous)
SELECT * FROM candidates 
WHERE client=? AND position=? AND iteration=(SELECT MAX(iteration) FROM candidates WHERE client=? AND position=?)
AND res_id NOT IN (SELECT res_id FROM candidates WHERE client=? AND position=? AND iteration < (SELECT MAX(iteration) FROM candidates WHERE client=? AND position=?));

-- Recommended rate
SELECT COUNT(*) as recommended, 
       SUM(CASE WHEN status='client_approved' THEN 1 ELSE 0 END) as approved,
       SUM(CASE WHEN status='client_rejected' THEN 1 ELSE 0 END) as rejected
FROM candidates WHERE client=? AND position=? AND recommended_to_client IS NOT NULL;
```

---

## Desktop App Integration

人才库数据通过原生 macOS App（鹏新旭猎头工作站）展示，路径 `~/Desktop/客户项目/鹏新旭/pnx_app/`。
- `DataManager.shared.open()` → 只读打开 DB
- `DataManager.shared.candidates(for:)` → 查询含 source/xsaasId/talentPool
- `DataManager.shared.updateStatus(id:status:)` → 读写独立连接更新
- `DataManager.shared.positionCounts(for:)` → 岗位分组统计
- App 通过 DispatchSource 监控 DB 文件变化，搜索写入后自动刷新表格

## Pitfalls

- `name + company + client + position` 是唯一去重键——不再用 res_id
- res_id 每次猎聘会话都变，不能存入数据库。但每次搜索后可把匹配到的 res_id 链接保存为本地 HTML 文件（`候选人链接_{岗位}_{date}.html`），后续报告生成时从这些本地文件匹配。
- 同一候选人不同轮次迭代用 INSERT OR IGNORE 跳过，不更新 iteration
- **App 读 candidates.client，必须同步写入** — 桌面 App 直连 `talent_pool.db`，侧边栏客户筛选 SQL `SELECT DISTINCT client FROM candidates`、职位筛选 SQL `SELECT position, COUNT(*) FROM candidates WHERE client=?` 都从 `candidates` 表读取。搜索结果入库时，**必须同时写入 `candidates.client`**，不能只写 `candidate_clients` 表。两表通过 `candidate_clients.candidate_company = candidates.company AND candidate_clients.candidate_name = candidates.name` 关联，但 App 只查 `candidates` 表。
- **半导体 TME/FAE 评级要穿透三层** — 产品线、应用市场、客户场景必须进入画像或智能评估；对标公司只算扩池证据。发现“公司对但产品线/应用市场错”时，不要删除历史触达，改用 `contacted_but_misaligned`、降级并写明 `elimination_reason`。
