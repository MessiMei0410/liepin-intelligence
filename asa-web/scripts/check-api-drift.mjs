#!/usr/bin/env node
// R5 契约漂移检查：Core (8765) 的 openapi.json 重新生成后与 src/generated/api.d.ts 逐字节比对。
// 不一致 → 退出码 1（提示运行 npm run generate:api）。默认模式 Core 不可达 → 打印警告
// 并退出码 0（本地开发不误伤 lint/test/build）；CI 走严格模式（见下）。
//
// 严格模式（ASA_API_DRIFT_STRICT=1，CI 使用）：
// - Core 可达 → 与实时契约比对（同默认行为），漂移即失败；
// - Core 不可达 → 回退到仓库内快照 scripts/openapi.snapshot.json 比对：
//   快照缺失或漂移都退出码 1，避免 CI 上该步骤永远静默通过。
//   快照由 npm run generate:api 同步刷新（生成 api.d.ts 时一并落盘）。
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { createRequire } from 'node:module'

const CORE_URL = process.env.ASA_CORE_URL || 'http://127.0.0.1:8765'
const STRICT = process.env.ASA_API_DRIFT_STRICT === '1'
// 与 check-api-drift.mjs 同目录的快照文件（generate:api 时同步刷新）。
const SNAPSHOT_PATH = new URL('./openapi.snapshot.json', import.meta.url)

let spec
let specSource = 'Core'
try {
  const response = await fetch(`${CORE_URL}/openapi.json`, { signal: AbortSignal.timeout(5000) })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  spec = await response.text()
} catch (error) {
  if (!STRICT) {
    console.warn(`[check-api-drift] Core 不可达（${CORE_URL}），跳过漂移检查：${error instanceof Error ? error.message : String(error)}`)
    process.exit(0)
  }
  try {
    spec = readFileSync(SNAPSHOT_PATH, 'utf8')
    specSource = '仓库快照 scripts/openapi.snapshot.json'
  } catch {
    console.error(`[check-api-drift] Core 不可达（${CORE_URL}）且仓库快照 ${SNAPSHOT_PATH.pathname} 缺失。`)
    console.error('[check-api-drift] 请本地启动 Core 后运行 npm run generate:api（会同步刷新快照），并提交快照与 api.d.ts。')
    process.exit(1)
  }
}

const dir = mkdtempSync(join(tmpdir(), 'asa-api-drift-'))
const specPath = join(dir, 'openapi.json')
const regeneratedPath = join(dir, 'api.d.ts')
writeFileSync(specPath, spec)

const require = createRequire(import.meta.url)
const packageJsonPath = require.resolve('openapi-typescript/package.json')
const { bin } = require('openapi-typescript/package.json')
const cliPath = join(dirname(packageJsonPath), typeof bin === 'string' ? bin : bin['openapi-typescript'])
// 与 npm run generate:api 同一 CLI 二进制，仅从临时文件读 spec，保证输出可比。
execFileSync(process.execPath, [cliPath, specPath, '-o', regeneratedPath], { stdio: 'pipe' })

const regenerated = readFileSync(regeneratedPath, 'utf8')
const current = readFileSync(new URL('../src/generated/api.d.ts', import.meta.url), 'utf8')
if (regenerated !== current) {
  console.error(`[check-api-drift] src/generated/api.d.ts 与 ${specSource} 已漂移。请运行 npm run generate:api 重新生成并提交（CI 严格模式下快照也需同步提交）。`)
  process.exit(1)
}
console.log(`[check-api-drift] src/generated/api.d.ts 与 ${specSource} 契约一致。`)
