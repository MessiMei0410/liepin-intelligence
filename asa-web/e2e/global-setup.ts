import { execFileSync, execSync, spawn } from 'node:child_process'
import { closeSync, existsSync, mkdirSync, mkdtempSync, openSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import {
  BACKEND_APP,
  BACKEND_SCRIPTS,
  E2E_BASE_URL,
  E2E_PORT,
  PRODUCTION_DB,
  PYTHON,
  REPO_DIST,
  STATE_FILE,
  TMP_PREFIX,
} from './support/paths'

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

// 只读打开正式库（mode=ro），用 SQLite 在线备份 API 导出一致副本；对源库零写入。
const BACKUP_SCRIPT = [
  'import sqlite3, sys',
  "src = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)",
  'dst = sqlite3.connect(sys.argv[2])',
  'src.backup(dst)',
  'dst.close(); src.close()',
].join('\n')

type Health = { ok?: boolean; db?: string }

async function fetchHealth(): Promise<Health | undefined> {
  try {
    const response = await fetch(`${E2E_BASE_URL}/api/v1/health`, { signal: AbortSignal.timeout(1000) })
    if (!response.ok) return undefined
    return (await response.json()) as Health
  } catch {
    return undefined
  }
}

function portPids(): number[] {
  try {
    const out = execSync(`lsof -nP -tiTCP:${E2E_PORT} -sTCP:LISTEN`, { encoding: 'utf8' }).trim()
    return out ? out.split('\n').map(Number).filter(Boolean) : []
  } catch {
    return []
  }
}

// 端口管理：8876 是本套件专用。若被上次崩溃遗留的隔离 Core 占用（health.db 带 /tmp 前缀），
// 回收之；若占用者不是本套件实例，直接报错拒绝继续，绝不误杀其他服务。
async function reclaimPort(): Promise<void> {
  const health = await fetchHealth()
  if (!health) return
  const db = String(health.db || '')
  if (!db.includes(`/${TMP_PREFIX}`)) {
    throw new Error(`[e2e] 端口 ${E2E_PORT} 被非本套件服务占用（db=${db || '未知'}），请先释放该端口。`)
  }
  console.warn(`[e2e] 回收上次遗留的隔离 Core（db=${db}）`)
  for (const pid of portPids()) {
    try {
      process.kill(pid, 'SIGTERM')
    } catch {
      // 已退出
    }
  }
  for (let i = 0; i < 20 && portPids().length > 0; i++) await sleep(250)
  for (const pid of portPids()) {
    try {
      process.kill(pid, 'SIGKILL')
    } catch {
      // 已退出
    }
  }
}

export default async function globalSetup(): Promise<void> {
  const missing: string[] = []
  if (!existsSync(PYTHON)) missing.push(`Python 解释器 ${PYTHON}`)
  if (!existsSync(BACKEND_APP)) missing.push(`后端仓库 ${BACKEND_APP}`)
  if (!existsSync(PRODUCTION_DB)) missing.push(`正式库只读来源 ${PRODUCTION_DB}`)
  if (!existsSync(path.join(REPO_DIST, 'index.html'))) missing.push(`前端构建产物 dist/index.html（先运行 npm run build）`)
  if (missing.length > 0) {
    process.env.ASA_E2E_SKIP = `E2E 降级跳过，本机缺少依赖：${missing.join('；')}`
    console.warn(`\n[e2e] ${process.env.ASA_E2E_SKIP}\n`)
    return
  }

  await reclaimPort()

  const rundir = mkdtempSync(path.join(tmpdir(), TMP_PREFIX))
  const dbCopy = path.join(rundir, 'talent_system_v3_e2e.db')
  execFileSync(PYTHON, ['-c', BACKUP_SCRIPT, PRODUCTION_DB, dbCopy], { stdio: ['ignore', 'ignore', 'inherit'] })
  mkdirSync(path.join(rundir, 'outputs'), { recursive: true })

  const logFd = openSync(path.join(rundir, 'core.log'), 'a')
  const child = spawn(
    PYTHON,
    ['-m', 'asa_core.app', '--host', '127.0.0.1', '--port', String(E2E_PORT), '--db', dbCopy],
    {
      env: {
        ...process.env,
        PYTHONPATH: BACKEND_SCRIPTS,
        // 关键隔离：legacy 运行时读 A_SYSTEM_DB 而非 --db，两个都指向副本，正式库零接触。
        A_SYSTEM_DB: dbCopy,
        A_SYSTEM_LIEPIN_OUTPUTS: path.join(rundir, 'outputs'),
        ASA_WEB_DIST: REPO_DIST,
      },
      detached: true,
      stdio: ['ignore', logFd, logFd],
    },
  )
  closeSync(logFd)
  child.unref()
  writeFileSync(
    STATE_FILE,
    JSON.stringify({ pid: child.pid, rundir, dbCopy, port: E2E_PORT, startedAt: new Date().toISOString() }),
  )

  const deadline = Date.now() + 45_000
  for (;;) {
    const health = await fetchHealth()
    // 校验 db 路径确为本次副本：保证浏览器打的不是别的实例。
    if (health?.ok && health.db === dbCopy) break
    if (child.exitCode !== null) {
      throw new Error(`[e2e] 隔离 Core 提前退出（code=${child.exitCode}），日志：${rundir}/core.log`)
    }
    if (Date.now() > deadline) {
      try {
        if (child.pid) process.kill(child.pid, 'SIGKILL')
      } catch {
        // 已退出
      }
      throw new Error(`[e2e] 隔离 Core 健康检查超时（45s），日志：${rundir}/core.log`)
    }
    await sleep(500)
  }

  process.env.ASA_E2E_BASE_URL = E2E_BASE_URL
  console.log(`[e2e] 隔离 Core 就绪：${E2E_BASE_URL}（pid=${child.pid}，db=${dbCopy}）`)
}
