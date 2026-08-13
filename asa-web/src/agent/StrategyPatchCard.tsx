import { useMemo, useState } from 'react'
import { Check, CircleAlert, Filter, LoaderCircle, Search, Target, X } from 'lucide-react'
import { api, type StrategyItemEdit } from '../api'

type StrategyPatchChange = {
  type: string
  value: string
  clause?: string
  confidence?: number
}

type StrategyPatch = {
  workflow_id: string
  workflow_title?: string
  strategy_hash?: string
  changes: StrategyPatchChange[]
}

const asPatch = (value?: Record<string, unknown> | null): StrategyPatch | null => {
  if (!value || !Array.isArray(value.changes)) return null
  const workflowId = String(value.workflow_id || '').trim()
  const changes = value.changes.flatMap(item => {
    if (!item || typeof item !== 'object') return []
    const row = item as Record<string, unknown>
    const type = String(row.type || '').trim()
    const changeValue = String(row.value || '').trim()
    return type && changeValue ? [{
      type, value: changeValue, clause: String(row.clause || '').trim(), confidence: Number(row.confidence || 0),
    }] : []
  })
  return workflowId && changes.length ? {
    workflow_id: workflowId,
    workflow_title: String(value.workflow_title || '').trim(),
    strategy_hash: String(value.strategy_hash || '').trim(),
    changes,
  } : null
}

const changeLabels: Record<string, string> = {
  add_keyword: '关键词', add_scene: '场景词', add_company: '目标公司', add_filter: '排除条件',
}

const changeIcon = (type: string) => type === 'add_company' ? <Target size={13}/>
  : type === 'add_filter' ? <Filter size={13}/>
    : <Search size={13}/>

const editsFor = (changes: StrategyPatchChange[]): StrategyItemEdit[] => {
  const searchTerms = changes.filter(item => item.type === 'add_keyword').map(item => item.value)
  const sceneTerms = changes.filter(item => item.type === 'add_scene').map(item => item.value)
  return [
    ...(searchTerms.length ? [{ op: 'append_keyword_terms', group: '顾问对话确认', terms: searchTerms, targets: '顾问确认的核心能力词' }] : []),
    ...(sceneTerms.length ? [{ op: 'append_keyword_terms', group: '顾问场景确认', terms: sceneTerms, targets: '顾问确认的业务场景' }] : []),
    ...changes.filter(item => item.type === 'add_company').map(item => ({
      op: 'add_company', tier: 'T1', name: item.value, source: 'consultant_confirmed', confidence: 'high',
    })),
    ...changes.filter(item => item.type === 'add_filter').map(item => ({
      op: 'add_negative_rule', type: 'consultant_exclusion', rule: item.value,
    })),
  ]
}

export function StrategyPatchCard({ patch: rawPatch, sessionId, applied, appliedRevision, appliedCount }: {
  patch?: Record<string, unknown> | null
  sessionId: string
  applied?: boolean
  appliedRevision?: number | null
  appliedCount?: number | null
}) {
  const patch = useMemo(() => asPatch(rawPatch), [rawPatch])
  const [selected, setSelected] = useState<number[]>(() => patch?.changes.map((_, index) => index) || [])
  const [stage, setStage] = useState<'select' | 'confirm' | 'applied'>(applied ? 'applied' : 'select')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [preflight, setPreflight] = useState<{ token: string; strategyHash: string; impact: string }>()
  const [result, setResult] = useState<{ revision?: number; applied?: number; artifactId?: string }>()
  const [syncError, setSyncError] = useState('')
  const [syncing, setSyncing] = useState(false)
  if (!patch) return null

  const chosen = selected.flatMap(index => patch.changes[index] ? [patch.changes[index]] : [])
  const toggle = (index: number) => setSelected(current => current.includes(index)
    ? current.filter(value => value !== index)
    : [...current, index])
  const check = async () => {
    if (!chosen.length || busy) return
    setBusy(true); setError('')
    try {
      const response = await api.preflightStrategyEdits(patch.workflow_id, editsFor(chosen), patch.strategy_hash || '')
      setPreflight({ token: response.preflight_token, strategyHash: response.strategy_hash, impact: response.impact || '' })
      setStage('confirm')
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value))
    } finally { setBusy(false) }
  }
  const apply = async () => {
    if (!chosen.length || busy) return
    if (!preflight?.token) { setError('策略写入预检已失效，请重新检查写入内容'); return }
    setBusy(true); setError('')
    try {
      const response = await api.applyStrategyEdits(
        patch.workflow_id,
        editsFor(chosen),
        'ASA 主对话中由顾问逐项确认',
        preflight.strategyHash,
        preflight.token,
      )
      const count = Number(response.edit_count || chosen.length)
      const receipt = { revision: response.revision, applied: count, artifactId: response.artifact_id }
      setResult(receipt)
      setStage('applied')
      try {
        await syncReceipt(receipt)
      } catch (value) {
        setSyncError(value instanceof Error ? value.message : String(value))
      }
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value))
    } finally { setBusy(false) }
  }
  const syncReceipt = async (receipt: { revision?: number; applied?: number; artifactId?: string }) => {
    await api.recordCopilotEvent(sessionId, 'copilot_strategy_applied', {
      workflow_id: patch.workflow_id,
      revision: receipt.revision,
      artifact_id: receipt.artifactId,
      applied: receipt.applied,
      total: patch.changes.length,
    })
    setSyncError('')
  }
  const retrySync = async () => {
    if (!result || syncing) return
    setSyncing(true)
    try {
      await syncReceipt(result)
    } catch (value) {
      setSyncError(value instanceof Error ? value.message : String(value))
    } finally { setSyncing(false) }
  }

  if (stage === 'applied') {
    const revision = result?.revision ?? appliedRevision
    const count = result?.applied ?? appliedCount ?? patch.changes.length
    return <section className="agent-strategy-patch applied" aria-label="寻访策略建议">
      <header><span><Check size={14}/></span><div><b>策略已沉淀</b><small>{patch.workflow_title || patch.workflow_id}</small></div></header>
      <p>已将 {count} 项顾问确认写入策略{revision ? ` revision ${revision}` : ''}；下一轮寻访将消费该版本，R3 审批范围不会被绕过。</p>
      {syncError && <><p className="agent-card-error" role="alert"><CircleAlert size={13}/>策略已写入，但会话记录同步失败：{syncError}</p><footer><button type="button" disabled={syncing} onClick={() => void retrySync()}>{syncing ? <LoaderCircle className="spin" size={13}/> : null}重试同步会话记录</button></footer></>}
    </section>
  }

  return <section className="agent-strategy-patch" aria-label="寻访策略建议">
    <header><span><Search size={14}/></span><div><b>{stage === 'confirm' ? '确认写入策略' : '可沉淀的策略建议'}</b><small>{patch.workflow_title || patch.workflow_id}</small></div></header>
    {stage === 'select' ? <>
      <p>选择要纳入当前工作流的建议。未勾选项不会写入。</p>
      <div className="agent-strategy-changes">{patch.changes.map((change, index) => <label key={`${change.type}:${change.value}`}>
        <input type="checkbox" checked={selected.includes(index)} onChange={() => toggle(index)}/>
        <span>{changeIcon(change.type)}</span><b>{changeLabels[change.type] || '策略项'}</b><em>{change.value}</em>
      </label>)}</div>
      {error && <p className="agent-card-error" role="alert"><CircleAlert size={13}/>{error}</p>}
      <footer><button type="button" disabled={!chosen.length || busy} onClick={() => void check()}>{busy ? <LoaderCircle className="spin" size={13}/> : null}检查写入内容</button></footer>
    </> : <>
      <dl><div><dt>写入预检</dt><dd>预检通过，令牌有效期 5 分钟</dd></div><div><dt>目标工作流</dt><dd>{patch.workflow_title || patch.workflow_id}</dd></div><div><dt>写入内容</dt><dd>{chosen.map(item => item.clause || `${changeLabels[item.type] || '策略项'}「${item.value}」`).join('；')}</dd></div><div><dt>安全边界</dt><dd>{preflight?.impact || '仅更新策略结构，不启动寻访；策略或工作流状态变化时拒绝写入。'}</dd></div></dl>
      {error && <p className="agent-card-error" role="alert"><CircleAlert size={13}/>{error}</p>}
      <footer><button type="button" disabled={busy} onClick={() => { setStage('select'); setPreflight(undefined); setError('') }}><X size={13}/>返回调整</button><button type="button" className="primary" disabled={busy} onClick={() => void apply()}>{busy ? <LoaderCircle className="spin" size={13}/> : <Check size={13}/>}确认写入</button></footer>
    </>}
  </section>
}
