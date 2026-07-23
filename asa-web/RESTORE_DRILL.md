# ASA v3 备份恢复演练记录（PRD R13）

每次演练把备份还原到系统临时目录（绝不覆盖正式库），校验 `PRAGMA integrity_check`
并对比关键表行数（jobs / candidates / job_candidates / candidate_events）。

- 备份脚本：`scripts/asa_v3_backup.py`（LaunchAgent `ai.hermes.asa-v3-backup` 每日执行）
- 演练命令：`python3 scripts/asa_v3_restore_drill.py --fresh`（先备后演，行数对比不受备份后写入影响）
- 备份目录：`~/.hermes/backups/asa_v3/`（独立于项目目录，不纳入 git）

## 2026-07-22 23:08:24 — 通过

```json
{
  "ok": true,
  "ts": "2026-07-22 23:08:24",
  "backup": "asa_v3_20260722_230803_manual.db",
  "restored_to": "/var/folders/k7/vglgry_n0lx90ph75lngmm600000gn/T/asa_v3_restore_drill_qk0xodcs/asa_v3_20260722_230803_manual.db",
  "integrity_check": "ok",
  "row_counts_match": true,
  "live": {
    "jobs": 137,
    "candidates": 114,
    "job_candidates": 114,
    "candidate_events": 524
  },
  "restored": {
    "jobs": 137,
    "candidates": 114,
    "job_candidates": 114,
    "candidate_events": 524
  }
}
```
