import { useEffect, useRef } from 'react'
import { ExternalLink, LoaderCircle, RefreshCw, Users } from 'lucide-react'
import type { CandidateListCardData } from '../workflows/CandidateListCard'
import { candidateStageTone } from '../workflows/CandidateListCard'
import type { DragResizeAnchor } from '../shared/dialogDragResize'
import { DialogFloating } from '../shared/Dialog'
import { nativeBridge } from '../shared/nativeBridge'

const MIN_DIALOG_W = 320
const MIN_DIALOG_H = 240

export function CandidateListDialog({
  data,
  onOpenCandidate,
  onOpenJob,
  onClose,
  onRefresh,
  refreshing = false,
}: {
  data: CandidateListCardData
  onOpenCandidate: (jobCandidateId: number) => void
  onOpenJob?: (jobId: number) => void
  onClose: () => void
  onRefresh?: () => void
  refreshing?: boolean
}) {
  const dialogRef = useRef<HTMLElement>(null)
  const summary = data.summary || {}
  const groups = Array.isArray(data.groups) ? data.groups : []
  const total = Number(summary.total ?? groups.reduce((sum, group) => sum + (group.candidates?.length || 0), 0))
  const active = Number(summary.active ?? total)
  const stopped = Number(summary.stopped ?? 0)
  const bonderCount = Number(summary.bonder_count ?? groups.find(group => group.key === 'bonder')?.candidates?.length ?? 0)
  const jobId = data.context?.type === 'job' ? Number(data.context.id) : undefined

  const detachDialog = (anchor?: DragResizeAnchor): boolean => {
    const jobId = data.context?.type === 'job' ? Number(data.context.id) : 0
    const hasList = groups.some(group => (group.candidates || []).length > 0)
    if (!hasList && !jobId) return false // 无可弹出内容
    const payload: Record<string, unknown> = { title: data.title || '候选名单' }
    // 有名单数据时弹名单本身（原生注入数据，用同一组件渲染，UI 与应用内一致）；
    // 仅在没有名单数据时才退回打开岗位页。
    if (hasList) payload.list = { title: data.title, context: data.context, summary: data.summary, groups }
    else if (jobId) payload.url = `/asa-app#job=${encodeURIComponent(String(jobId))}&bare=1`
    const pos = anchor ?? (() => {
      const el = dialogRef.current
      const rect = el?.getBoundingClientRect()
      return rect ? { x: rect.left, y: rect.top, edge: 'center' } : undefined
    })()
    if (pos) payload.anchor = pos
    if (nativeBridge('openDetachedDialog', payload)) {
      onClose()
      return true
    }
    return false
  }

  // 拖出边界弹出独立窗口：detach 回调随渲染刷新，DialogFloating 内部以最新引用调用。
  const detachDialogRef = useRef(detachDialog)
  useEffect(() => {
    detachDialogRef.current = detachDialog
  })

  return (
    <DialogFloating
      ref={dialogRef}
      onClose={onClose}
      title={data.title}
      ariaLabel={data.title}
      icon={<Users size={18} />}
      eyebrow={`共 ${total} 人${bonderCount > 0 ? ` · 固晶/共晶/键合背景 ${bonderCount} 人` : ''} · 可推进 ${active} 人 · 已停止 ${stopped} 人`}
      headerActions={<>
        {onRefresh && <button className="icon-btn candidate-dialog-refresh" aria-label="刷新名单" title="重新按库内最新状态生成名单" disabled={refreshing} onClick={onRefresh}>{refreshing ? <LoaderCircle className="spin" size={16}/> : <RefreshCw size={16}/>}</button>}
        <button className="icon-btn candidate-dialog-detach" aria-label="弹出为独立窗口" title="弹出为独立窗口（可拖出屏幕）" onClick={() => detachDialog()}><ExternalLink size={16} /></button>
      </>}
      onDetach={anchor => detachDialogRef.current(anchor)}
      minWidth={MIN_DIALOG_W}
      minHeight={MIN_DIALOG_H}
      closeLabel="关闭名单"
      footer={jobId !== undefined && onOpenJob ? <button className="button" onClick={() => onOpenJob(jobId)}>打开岗位查看完整名单</button> : undefined}
    >
      {groups.map(group => {
        const candidates = group.candidates || []
        if (!candidates.length) return null
        return (
          <div className={`candidate-dialog-group ${group.priority ? 'priority' : ''}`} key={group.key}>
            <h4>{group.priority ? '⭐ ' : ''}{group.label}<em>{candidates.length} 人</em></h4>
            <ul>
              {candidates.map(candidate => (
                <li key={candidate.id}>
                  <button
                    className="candidate-dialog-row"
                    onClick={() => onOpenCandidate(candidate.id)}
                    title={`打开人选详情：${candidate.name}`}
                  >
                    <span className="candidate-list-row-main">
                      <b>{candidate.name}</b>
                      <small>{[candidate.company, candidate.title].filter(Boolean).join(' · ')}</small>
                    </span>
                    <em className={`candidate-list-stage tone-${candidateStageTone(candidate.stage)}`}>{candidate.stage || '—'}</em>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )
      })}
      {!groups.some(group => (group.candidates || []).length) && <p className="candidate-dialog-empty">当前候选池为空。</p>}
    </DialogFloating>
  )
}
