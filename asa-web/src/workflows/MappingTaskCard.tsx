import { useCallback, useEffect, useState } from 'react'
import { LoaderCircle, MapPinned, RefreshCw, X } from 'lucide-react'
import { api } from '../api'
import type { MappingCandidate, MappingCandidateStatus, MappingTaskPayload, MappingTaskStats } from '../api'
import { humanizeActionError } from '../shared/errors'
import { date } from '../shared/format'
import {
  groupMappingTeams,
  humanizeMappingFailure,
  mappingClueRate,
  mappingConfidenceLabel,
  mappingStatusLabel,
} from './mappingTask'

// S5-2 Mapping 任务卡视图（工作流详情内区块，不另起页面）：
// 左侧目标团队树（公司→团队→把握），右侧人选卡（职务/来源链接/推荐理由/七态/备注）。
// 写操作全部走后端幂等接口（PATCH 状态机 + 重新生成开场白 + 入库动作），响应直接回写本地；
// 已淘汰人选折叠到底部（软删可见）；红线：开场白要点只展示不发送，发送永远由顾问本人执行。

const STATUS_TAG_TONE: Record<string, string> = {
  pending: 'warn',
  confirmed: 'ok',
  intaken: 'ok',
  parked: 'muted',
  rejected: 'muted',
}

// 各状态下可点的动作（合法迁移由后端再校验，409 中文原因直接透出）。
const STATUS_ACTIONS: Record<string, Array<{ key: string; label: string; status: MappingCandidateStatus; primary?: boolean; danger?: boolean; title?: string }>> = {
  pending: [
    { key: 'confirm', label: '确认', status: 'confirmed', primary: true },
    { key: 'reject', label: '删除', status: 'rejected', danger: true, title: '软删：人选折叠到底部，不物理删除' },
  ],
  confirmed: [
    { key: 'contact', label: '已接触', status: 'contacted', primary: true },
    { key: 'park', label: '搁置', status: 'parked' },
    { key: 'reject', label: '淘汰', status: 'rejected', danger: true },
  ],
  contacted: [{ key: 'reply', label: '已回复', status: 'replied', primary: true }],
  parked: [
    { key: 'restore', label: '恢复待确认', status: 'pending' },
    { key: 'reject', label: '淘汰', status: 'rejected', danger: true },
  ],
}

export function MappingTaskCard({ jobId, artifactId, openCandidate, onClose }: {
  jobId: number
  artifactId: string
  openCandidate: (id: number) => void
  onClose: () => void
}) {
  const [payload, setPayload] = useState<MappingTaskPayload | null>(null)
  const [loadError, setLoadError] = useState('')
  const [busy, setBusy] = useState('')
  const [cardErrors, setCardErrors] = useState<Record<number, string>>({})
  const [icebreakerErrors, setIcebreakerErrors] = useState<Record<number, string[]>>({})
  const [statsOverride, setStatsOverride] = useState<MappingTaskStats | null>(null)

  const load = useCallback(async () => {
    setLoadError('')
    try {
      setPayload(await api.mappingTask(jobId, artifactId))
    } catch (error) {
      setLoadError(humanizeActionError(error, '任务卡加载失败，请重试。'))
    }
  }, [jobId, artifactId])
  useEffect(() => { void load() }, [load])

  const applyCandidate = (index: number, candidate: MappingCandidate) => {
    setPayload(prev => prev ? {
      ...prev,
      mapping_task: {
        ...prev.mapping_task,
        candidates: (prev.mapping_task.candidates || []).map((item, position) => position === index ? candidate : item),
      },
    } : prev)
  }

  const run = async (index: number, actionKey: string, call: () => Promise<void>) => {
    setBusy(`${index}:${actionKey}`)
    setCardErrors(prev => ({ ...prev, [index]: '' }))
    try {
      await call()
    } catch (error) {
      setCardErrors(prev => ({ ...prev, [index]: humanizeActionError(error, '操作失败，请重试。') }))
    } finally {
      setBusy('')
    }
  }

  const patchCandidate = (index: number, body: { status?: MappingCandidateStatus; consultant_note?: string }, actionKey: string) =>
    run(index, actionKey, async () => {
      const result = await api.patchMappingCandidate(artifactId, index, body)
      applyCandidate(index, result.candidate)
      if (result.stats) setStatsOverride(result.stats)
      // 确认后破冰素材质量不合格不阻断状态变更，原因透在 icebreaker_errors，展示原因不显示素材。
      if (body.status) setIcebreakerErrors(prev => ({ ...prev, [index]: result.icebreaker_errors || [] }))
    })

  const regenerateIcebreaker = (index: number) =>
    run(index, 'icebreaker', async () => {
      const result = await api.regenerateMappingIcebreaker(artifactId, index)
      applyCandidate(index, result.candidate)
      setIcebreakerErrors(prev => ({ ...prev, [index]: [] }))
    })

  const intake = (index: number) =>
    run(index, 'intake', async () => {
      const result = await api.intakeMappingCandidate(artifactId, index)
      const current = (payload?.mapping_task.candidates || [])[index]
      if (current) {
        applyCandidate(index, {
          ...current,
          status: 'intaken',
          intake: {
            job_candidate_id: result.job_candidate_id,
            candidate_id: result.candidate_id,
            person_id: result.person_id,
            intaken_at: result.intaken_at,
            relation_existed: result.relation_existed,
          },
        })
      }
      if (result.stats) setStatsOverride(result.stats)
    })

  const saveNote = (index: number, value: string) => {
    const current = (payload?.mapping_task.candidates || [])[index]
    if (!current || value === (current.consultant_note || '')) return
    void patchCandidate(index, { consultant_note: value }, 'note')
  }

  const doc = payload?.mapping_task
  const candidates = doc?.candidates || []
  const stats = statsOverride || doc?.stats || {}
  const rate = mappingClueRate(stats)
  const failures = stats.failures || []
  const teamGroups = groupMappingTeams(doc?.target_teams || [])
  const active = candidates.map((candidate, index) => ({ candidate, index })).filter(item => item.candidate.status !== 'rejected')
  const rejected = candidates.map((candidate, index) => ({ candidate, index })).filter(item => item.candidate.status === 'rejected')

  return <section className="workflow-insight mapping-card" aria-label="Mapping 任务卡">
    <header>
      <span className="insight-icon"><MapPinned /></span>
      <div>
        <span>Mapping 任务卡</span>
        <b>{payload?.title || '加载中…'}</b>
        {doc && <small>{[doc.client, doc.job_title].filter(Boolean).join(' · ')}{doc.generated_at ? ` · 生成于 ${date(doc.generated_at)}` : ''}</small>}
      </div>
      <button className="button" onClick={onClose} title="收起任务卡"><X />收起</button>
    </header>
    {loadError && <div className="insight-empty">
      <span>{loadError}</span>
      <button className="button" onClick={() => void load()}><RefreshCw />重新加载</button>
    </div>}
    {!payload && !loadError && <div className="insight-empty"><LoaderCircle className="spin" />任务卡加载中…</div>}
    {doc && <>
      <section className="mapping-stats" aria-label="这份名单的效果">
        <div className="review-diffs-head">
          <b>这份名单的效果</b>
          <span>
            目标团队 {stats.teams ?? 0} 个 · 名单 {stats.candidates ?? candidates.length} 人
            {(stats.banned_filtered ?? 0) > 0 ? ` · 禁挖过滤 ${stats.banned_filtered}` : ''}
            {(stats.rejected_no_source ?? 0) > 0 ? ` · 无来源拒收 ${stats.rejected_no_source}` : ''}
          </span>
        </div>
        <div className="funnel-line">
          <span>采集线索 <b>{rate.clues}</b></span><i>→</i>
          <span>已确认 <b>{rate.confirmed}</b></span><i>→</i>
          <span>已入库 <b>{stats.intaken ?? 0}</b></span><i>→</i>
          <span>线索有效率 <b>{rate.percent === null ? '暂无线索' : `${rate.percent}%`}</b></span>
        </div>
        {failures.length > 0 && <ul className="mapping-failures">
          {failures.map((failure, index) => <li key={index}>{humanizeMappingFailure(failure)}</li>)}
        </ul>}
      </section>
      <div className="mapping-body">
        <section className="mapping-teams" aria-label="目标团队">
          <div className="review-diffs-head"><b>目标团队</b><span>{teamGroups.reduce((sum, group) => sum + group.teams.length, 0)} 个团队 · {teamGroups.length} 家公司</span></div>
          {teamGroups.map(group => <div className="mapping-company" key={group.company}>
            <b>{group.company}</b>
            {group.teams.map((team, position) => <div className="mapping-team" key={position}>
              <div>
                <span>{team.team || '团队待确认'}</span>
                {team.tier && <span className="tag muted">{team.tier}</span>}
              </div>
              <small>{[team.location, mappingConfidenceLabel(team.confidence)].filter(Boolean).join(' · ')}</small>
            </div>)}
          </div>)}
        </section>
        <section className="mapping-candidates" aria-label="候选目标人">
          <div className="review-diffs-head"><b>候选目标人</b><span>{active.length} 人在跟进{rejected.length > 0 ? ` · ${rejected.length} 人已淘汰` : ''}</span></div>
          {active.map(({ candidate, index }) => <CandidateCard
            key={index}
            candidate={candidate}
            index={index}
            busy={busy}
            error={cardErrors[index] || ''}
            icebreakerErrors={icebreakerErrors[index] || []}
            onPatch={patchCandidate}
            onRegenerate={regenerateIcebreaker}
            onIntake={intake}
            onSaveNote={saveNote}
            openCandidate={openCandidate}
          />)}
          {rejected.length > 0 && <details className="mapping-rejected">
            <summary>已淘汰（{rejected.length}）· 软删保留，不物理删除</summary>
            {rejected.map(({ candidate, index }) => <div className="mapping-candidate rejected" key={index}>
              <div className="mapping-candidate-head">
                <b>{candidate.name || '姓名待补充'}</b>
                <span className="tag muted">{mappingStatusLabel(candidate.status)}</span>
              </div>
              <span>{candidate.current_role || '职务待补充'}</span>
              {candidate.reason && <small>{candidate.reason}</small>}
            </div>)}
          </details>}
        </section>
      </div>
    </>}
  </section>
}

function CandidateCard({ candidate, index, busy, error, icebreakerErrors, onPatch, onRegenerate, onIntake, onSaveNote, openCandidate }: {
  candidate: MappingCandidate
  index: number
  busy: string
  error: string
  icebreakerErrors: string[]
  onPatch: (index: number, body: { status?: MappingCandidateStatus; consultant_note?: string }, actionKey: string) => Promise<void>
  onRegenerate: (index: number) => Promise<void>
  onIntake: (index: number) => Promise<void>
  onSaveNote: (index: number, value: string) => void
  openCandidate: (id: number) => void
}) {
  const cardBusy = busy.startsWith(`${index}:`)
  const actions = STATUS_ACTIONS[candidate.status] || []
  const sources = candidate.source_urls || []
  const icebreaker = candidate.icebreaker
  const receipt = candidate.intake
  return <div className="mapping-candidate">
    <div className="mapping-candidate-head">
      <b>{candidate.name || '姓名待补充'}</b>
      <span className={`tag ${STATUS_TAG_TONE[candidate.status] || ''}`}>{mappingStatusLabel(candidate.status)}</span>
      {candidate.confidence && <small>{mappingConfidenceLabel(candidate.confidence)}</small>}
    </div>
    <span>{candidate.current_role || '职务待补充'}</span>
    {candidate.reason && <small>{candidate.reason}</small>}
    {sources.length > 0 && <div className="mapping-sources">
      {sources.map((url, position) => <a key={position} href={url} target="_blank" rel="noreferrer">来源 {position + 1}</a>)}
    </div>}
    {candidate.status === 'intaken' && receipt && <div className="mapping-intake">
      <b>已入库{receipt.intaken_at ? ` · ${date(receipt.intaken_at)}` : ''}</b>
      {receipt.relation_existed && <small>库里已有该人选与岗位的关系，复用原条目，未重复建档</small>}
      <button className="button" onClick={() => openCandidate(receipt.job_candidate_id)}>查看候选人</button>
    </div>}
    {icebreaker && <div className="mapping-icebreaker">
      <div className="mapping-icebreaker-head">
        <b>开场白要点</b>
        <span className="tag">{icebreaker.angle}</span>
        <small>只读不发送，电话/微信由顾问本人执行</small>
      </div>
      <ul>{icebreaker.hooks.map((hook, position) => <li key={position}>{hook}</li>)}</ul>
      {icebreaker.source_ref && <small>素材依据：{icebreaker.source_ref}</small>}
    </div>}
    {icebreakerErrors.length > 0 && <div className="tag warn">开场白要点没生成：{icebreakerErrors.join('；')}</div>}
    <div className="mapping-note">
      <input
        key={`${index}:${candidate.consultant_note || ''}`}
        defaultValue={candidate.consultant_note || ''}
        placeholder="顾问备注（失焦自动保存）"
        aria-label={`${candidate.name || `人选 ${index + 1}`} 的顾问备注`}
        disabled={cardBusy}
        onBlur={event => onSaveNote(index, event.currentTarget.value)}
      />
      {busy === `${index}:note` && <LoaderCircle className="spin" />}
    </div>
    {(actions.length > 0 || candidate.status === 'confirmed') && <div className="mapping-actions">
      {actions.map(action => <button
        key={action.key}
        className={`button${action.primary ? ' primary' : ''}${action.danger ? ' danger' : ''}`}
        title={action.title}
        disabled={cardBusy}
        onClick={() => void onPatch(index, { status: action.status }, action.key)}
      >{busy === `${index}:${action.key}` && <LoaderCircle className="spin" />}{action.label}</button>)}
      {candidate.status === 'confirmed' && <>
        <button className="button primary" disabled={cardBusy} onClick={() => void onIntake(index)}>
          {busy === `${index}:intake` && <LoaderCircle className="spin" />}入库
        </button>
        <button className="button" disabled={cardBusy} onClick={() => void onRegenerate(index)}>
          {busy === `${index}:icebreaker` && <LoaderCircle className="spin" />}重新生成开场白
        </button>
      </>}
    </div>}
    {error && <span className="tag warn">{error}</span>}
  </div>
}
