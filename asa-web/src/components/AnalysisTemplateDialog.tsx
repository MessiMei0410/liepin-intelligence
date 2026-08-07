import { useEffect, useMemo, useState } from 'react'
import { CalendarClock, History, LoaderCircle, Pin, Save, Trash2, X } from 'lucide-react'
import { api } from '../api'
import type { AnalysisCatalogItem, AnalysisTemplate, AnalysisTemplateInput, AnalysisTemplateRun } from '../api'
import { useDialogFocus } from '../shared/useDialogFocus'

const fieldLabels: Record<string, string> = {
  days: '统计周期（天）', job_id: '岗位 ID', company: '公司', title: '职位', city: '城市',
  stage: '阶段', limit: '结果条数', candidate_ids: '候选人 ID', workflow_id: '工作流 ID',
}
const numberFields = new Set(['days', 'job_id', 'limit'])
const weekdayLabels = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const statusLabels: Record<AnalysisTemplateRun['status'], string> = {
  running: '运行中', completed: '已完成', failed: '失败', skipped: '已跳过',
}
const displayTime = (value: string) => new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
}).format(new Date(value))

const initialScope = (template?: AnalysisTemplate) => Object.fromEntries(
  Object.entries(template?.scope || {}).map(([key, value]) => [key, Array.isArray(value) ? value.join(', ') : String(value)]),
)

function parseScope(fields: string[], values: Record<string, string>): Record<string, unknown> {
  const scope: Record<string, unknown> = {}
  for (const field of fields) {
    const raw = (values[field] || '').trim()
    if (!raw) continue
    if (numberFields.has(field)) scope[field] = Number(raw)
    else if (field === 'candidate_ids') scope[field] = raw.split(/[,，\s]+/).filter(Boolean).map(Number)
    else scope[field] = raw
  }
  return scope
}

export function AnalysisTemplateDialog({ catalogs, template, busy, onCancel, onSave, onDelete }: {
  catalogs: AnalysisCatalogItem[]; template?: AnalysisTemplate; busy?: boolean;
  onCancel: () => void; onSave: (input: AnalysisTemplateInput) => Promise<void>; onDelete?: () => Promise<void>;
}) {
  const [name, setName] = useState(template?.name || '')
  const [catalogId, setCatalogId] = useState(template?.catalog_id || catalogs[0]?.catalog_id || 'operations_overview')
  const [question, setQuestion] = useState(template?.question || '')
  const [scopeValues, setScopeValues] = useState<Record<string, string>>(() => initialScope(template))
  const [scheduleKind, setScheduleKind] = useState<AnalysisTemplate['schedule_kind']>(template?.schedule_kind || 'manual')
  const [scheduleEnabled, setScheduleEnabled] = useState(template?.schedule_enabled || false)
  const [scheduleTime, setScheduleTime] = useState(template?.schedule_time || '09:00')
  const [scheduleWeekday, setScheduleWeekday] = useState(template?.schedule_weekday || 0)
  const [runs, setRuns] = useState<AnalysisTemplateRun[]>([])
  const [runsState, setRunsState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [deleteArmed, setDeleteArmed] = useState(false)
  const dialogRef = useDialogFocus<HTMLElement>(true)
  const selectedCatalog = useMemo(
    () => catalogs.find(item => item.catalog_id === catalogId), [catalogId, catalogs],
  )
  const scopeFields = selectedCatalog?.allowed_scope_fields || []
  const valid = name.trim().length > 0 && !!catalogId

  useEffect(() => {
    if (!template) return
    let active = true
    api.analyticsTemplateRuns(template.template_id)
      .then(result => { if (active) { setRuns(result.items.slice(0, 6)); setRunsState('ready') } })
      .catch(() => { if (active) { setRuns([]); setRunsState('error') } })
    return () => { active = false }
  }, [template])

  const retryRuns = () => {
    if (!template) return
    setRunsState('loading')
    api.analyticsTemplateRuns(template.template_id)
      .then(result => { setRuns(result.items.slice(0, 6)); setRunsState('ready') })
      .catch(() => { setRuns([]); setRunsState('error') })
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape' && !busy) onCancel() }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [busy, onCancel])

  const submit = () => {
    if (!valid || busy) return
    void onSave({
      name: name.trim(), catalog_id: catalogId, question: question.trim(),
      scope: parseScope(scopeFields, scopeValues), schedule_kind: scheduleKind,
      schedule_enabled: scheduleKind === 'manual' ? false : scheduleEnabled,
      schedule_time: scheduleTime, schedule_weekday: scheduleWeekday, timezone: template?.timezone || 'Asia/Shanghai',
    })
  }

  return <div className="action-dialog-backdrop" role="presentation" onClick={() => { if (!busy) onCancel() }}>
    <section ref={dialogRef} className="action-dialog analysis-template-dialog" role="dialog" aria-modal="true" aria-labelledby="analysis-template-title" onClick={event => event.stopPropagation()}>
      <header>
        <span className="action-dialog-icon"><Pin /></span>
        <div><small>固定分析</small><h3 id="analysis-template-title">{template ? '管理固定分析' : '新建固定分析'}</h3></div>
        <button className="icon-btn" disabled={busy} onClick={onCancel} title="关闭" aria-label="关闭"><X /></button>
      </header>
      <div className="action-dialog-body template-dialog-body">
        <div className="template-form-grid">
          <label><span>名称</span><input value={name} onChange={event => setName(event.target.value)} placeholder="例如：每日经营概览" /></label>
          <label><span>分析类型</span><select value={catalogId} onChange={event => { setCatalogId(event.target.value); setScopeValues({}) }}>
            {catalogs.map(item => <option key={item.catalog_id} value={item.catalog_id}>{item.label}</option>)}
          </select></label>
        </div>
        <label><span>关注问题（选填）</span><textarea value={question} onChange={event => setQuestion(event.target.value)} placeholder="这份分析需要回答什么？" rows={2} /></label>
        {!!scopeFields.length && <fieldset className="template-scope"><legend>分析范围</legend><div className="template-form-grid">
          {scopeFields.map(field => <label key={field}><span>{fieldLabels[field] || field}</span><input
            type={numberFields.has(field) ? 'number' : 'text'} min={numberFields.has(field) ? 1 : undefined}
            value={scopeValues[field] || ''} onChange={event => setScopeValues(values => ({ ...values, [field]: event.target.value }))}
            placeholder={field === 'candidate_ids' ? '多个 ID 用逗号分隔' : undefined}
          /></label>)}
        </div></fieldset>}
        <fieldset className="template-schedule"><legend>执行计划</legend>
          <div className="segmented schedule-segments" aria-label="执行频率">
            {(['manual', 'daily', 'weekly'] as const).map(kind => <button type="button" key={kind} className={scheduleKind === kind ? 'active' : ''} aria-pressed={scheduleKind === kind} onClick={() => { setScheduleKind(kind); if (kind === 'manual') setScheduleEnabled(false) }}>
              {kind === 'manual' ? '手动' : kind === 'daily' ? '每天' : '每周'}
            </button>)}
          </div>
          {scheduleKind !== 'manual' && <div className="schedule-controls">
            <label className="switch-row"><input type="checkbox" checked={scheduleEnabled} onChange={event => setScheduleEnabled(event.target.checked)} /><span>启用自动执行</span></label>
            {scheduleKind === 'weekly' && <label><span>执行日</span><select value={scheduleWeekday} onChange={event => setScheduleWeekday(Number(event.target.value))}>{weekdayLabels.map((label, index) => <option key={label} value={index}>{label}</option>)}</select></label>}
            <label><span>执行时间</span><input type="time" value={scheduleTime} onChange={event => setScheduleTime(event.target.value)} /></label>
            <span className="schedule-zone"><CalendarClock />北京时间</span>
          </div>}
        </fieldset>
        {template && <section className="template-run-history" aria-label="运行记录">
          <header><History /><b>运行记录</b><span>{runsState === 'ready' ? `最近 ${runs.length} 次` : ''}</span></header>
          {runsState === 'loading' && <p role="status">正在读取运行记录…</p>}
          {runsState === 'error' && <div className="run-history-error"><span>运行记录加载失败，可稍后重试。</span><button className="button" onClick={retryRuns}>重新加载运行记录</button></div>}
          {runsState === 'ready' && runs.map(run => <div key={run.template_run_id}>
            <span className={`run-status ${run.status}`}>{statusLabels[run.status]}</span>
            <div><b>{run.headline || (run.trigger === 'schedule' ? '自动执行' : '手动执行')}</b><small>{displayTime(run.started_at)} · {run.trigger === 'schedule' ? '自动' : '手动'}</small>{run.error && <small className="run-error">{run.error}</small>}</div>
          </div>)}
          {runsState === 'ready' && !runs.length && <p>尚无运行记录</p>}
        </section>}
        {deleteArmed && <div className="action-dialog-error"><Trash2 />删除后不会影响已生成的历史分析结果。请再次确认。</div>}
      </div>
      <footer>
        {template && onDelete && <button className={`button ${deleteArmed ? 'danger-fill' : ''}`} disabled={busy} onClick={() => deleteArmed ? void onDelete() : setDeleteArmed(true)}><Trash2 />{deleteArmed ? '确认删除' : '删除'}</button>}
        <span className="dialog-footer-spacer" />
        <button className="button" disabled={busy} onClick={onCancel}>取消</button>
        <button className="button primary" disabled={!valid || busy} onClick={submit}>{busy ? <LoaderCircle className="spin" /> : <Save />}{template ? '保存' : '创建'}</button>
      </footer>
    </section>
  </div>
}
