import { useState } from 'react'
import { Archive, Ban, Pause, Play, TriangleAlert } from 'lucide-react'
import { DialogModal } from '../shared/Dialog'
import type { ConfirmableWorkflowAction } from './useWorkflowWriteActions'

// 工作流写操作确认卡（P7）：归档/暂停/继续/停止不再点击即执行，与候选人操作的
// 预检确认链对齐。cancel/pause/resume 的原因说明（note）是 Core actions/preflight
// 的必填项，随确认走 preflight → UI 激活 → 执行；archive 无后端闸门，仅前端确认。
const ACTION_META: Record<ConfirmableWorkflowAction, {
  title: string
  confirmLabel: string
  impact: string
  needsNote: boolean
  danger: boolean
  notePlaceholder: string
  icon: typeof Pause
}> = {
  pause: {
    title: '暂停寻访',
    confirmLabel: '确认暂停',
    impact: '渠道会在当前查询单元结束后停止；之后可随时继续寻访。',
    needsNote: true,
    danger: false,
    notePlaceholder: '例如：客户要求暂停一周，等反馈后再继续',
    icon: Pause,
  },
  resume: {
    title: '继续寻访',
    confirmLabel: '确认继续',
    impact: '工作流将重新进入执行队列，从暂停点继续推进。',
    needsNote: true,
    danger: false,
    notePlaceholder: '例如：客户反馈已到位，恢复寻访',
    icon: Play,
  },
  cancel: {
    title: '立即停止寻访',
    confirmLabel: '确认停止',
    impact: '进行中的步骤将全部取消，待审批项一并作废；该动作不可撤销。',
    needsNote: true,
    danger: true,
    notePlaceholder: '例如：岗位已关闭，停止本轮寻访',
    icon: Ban,
  },
  archive: {
    title: '归档工作流',
    confirmLabel: '确认归档',
    impact: '归档后工作流从列表隐藏，历史记录与产物保留可查。',
    needsNote: false,
    danger: false,
    notePlaceholder: '',
    icon: Archive,
  },
}

export function WorkflowActionConfirmDialog({
  action,
  workflowTitle,
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  action: ConfirmableWorkflowAction
  workflowTitle: string
  busy: boolean
  error?: string
  onConfirm: (note: string) => void
  onCancel: () => void
}) {
  const meta = ACTION_META[action]
  const Icon = meta.icon
  const [note, setNote] = useState('')
  const [noteError, setNoteError] = useState('')

  const confirm = () => {
    const trimmed = note.trim()
    if (meta.needsNote && !trimmed) {
      setNoteError('请填写原因说明：预检必填，并记入统一审计。')
      return
    }
    onConfirm(trimmed)
  }

  return (
    <DialogModal
      onClose={onCancel}
      title={meta.title}
      titleId="workflow-action-confirm-title"
      icon={meta.danger ? <TriangleAlert /> : <Icon />}
      eyebrow="工作流"
      alert
      closeDisabled={busy}
      initialFocus={meta.needsNote ? 'textarea' : undefined}
      footer={
        <>
          <button className="button" disabled={busy} onClick={onCancel}>取消</button>
          <button className={`button ${meta.danger ? 'danger' : 'primary'}`} disabled={busy} onClick={confirm}>
            {busy ? '提交中…' : meta.confirmLabel}
          </button>
        </>
      }
    >
      <dl>
        <div><dt>工作流</dt><dd>{workflowTitle}</dd></div>
        <div><dt>操作</dt><dd>{meta.title}</dd></div>
      </dl>
      <p>{meta.impact}</p>
      {meta.needsNote && (
        <label>
          <span>原因说明（必填，记入审计）</span>
          <textarea
            value={note}
            aria-label="原因说明"
            placeholder={meta.notePlaceholder}
            onChange={event => { setNote(event.target.value); setNoteError('') }}
          />
        </label>
      )}
      {noteError && <p className="action-dialog-error" role="alert">{noteError}</p>}
      {error && <p className="action-dialog-error" role="alert">{error}</p>}
    </DialogModal>
  )
}
