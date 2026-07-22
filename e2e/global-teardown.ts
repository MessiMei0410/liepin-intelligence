import { existsSync, readFileSync, rmSync } from 'node:fs'
import { STATE_FILE } from './support/paths'

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

const alive = (pid: number): boolean => {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

export default async function globalTeardown(): Promise<void> {
  if (!existsSync(STATE_FILE)) return
  let state: { pid?: number; rundir?: string } = {}
  try {
    state = JSON.parse(readFileSync(STATE_FILE, 'utf8')) as { pid?: number; rundir?: string }
  } catch {
    // 状态文件损坏也继续清理
  }
  rmSync(STATE_FILE, { force: true })

  if (state.pid && alive(state.pid)) {
    try {
      process.kill(state.pid, 'SIGTERM')
    } catch {
      // 已退出
    }
    for (let i = 0; i < 20 && alive(state.pid); i++) await sleep(250)
    if (alive(state.pid)) {
      try {
        process.kill(state.pid, 'SIGKILL')
      } catch {
        // 已退出
      }
    }
  }
  if (state.rundir) rmSync(state.rundir, { recursive: true, force: true })
  console.log('[e2e] 隔离 Core 已停止，临时 DB 副本已删除')
}
