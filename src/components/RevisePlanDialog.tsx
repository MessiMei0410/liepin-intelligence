import { useEffect, useState } from 'react'
import { SquarePen, X } from 'lucide-react'

// 工作流“修改计划”内联对话框：替代原 window.prompt，样式复用候选人操作确认层（.action-dialog）。
// 空输入禁止提交；Esc 或点击遮罩取消。提交时回调已 trim 的修改意见并交由调用方发 action('revise')。
export function RevisePlanDialog({ onCancel, onSubmit }: { onCancel: () => void; onSubmit: (instruction: string) => void }) {
  const [instruction, setInstruction] = useState('')
  const valid = instruction.trim().length > 0
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onCancel() }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onCancel])
  return (
    <div className="action-dialog-backdrop" role="presentation" onClick={onCancel}>
      <section className="action-dialog" role="dialog" aria-modal="true" aria-labelledby="revise-plan-title" onClick={event => event.stopPropagation()}>
        <header>
          <span className="action-dialog-icon"><SquarePen /></span>
          <div><small>工作流</small><h3 id="revise-plan-title">修改计划</h3></div>
          <button className="icon-btn" onClick={onCancel} title="取消" aria-label="取消"><X /></button>
        </header>
        <div className="action-dialog-body">
          <label>
            <span>修改意见（必填）</span>
            <textarea value={instruction} onChange={event => setInstruction(event.target.value)} placeholder="例如：优先补充华东区域的候选人，提高学历门槛" rows={4} autoFocus />
          </label>
        </div>
        <footer>
          <button className="button" onClick={onCancel}>取消</button>
          <button className="button primary" disabled={!valid} onClick={() => { if (valid) onSubmit(instruction.trim()) }}>确认修改</button>
        </footer>
      </section>
    </div>
  )
}
