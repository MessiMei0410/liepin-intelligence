import { TriangleAlert, X } from 'lucide-react'
import { useDialogFocus } from '../shared/useDialogFocus'

// 离开确认对话框：存在未提交的表单内容时，由 App 在丢弃型导航前弹出。
// 遵循全仓约定：禁止 prompt/confirm/alert，统一 React 对话框。
export function LeaveConfirmDialog({ dirtyCount, onConfirm, onCancel }: {
  dirtyCount: number
  onConfirm: () => void
  onCancel: () => void
}) {
  const dialogRef = useDialogFocus<HTMLElement>(true)
  return <div className="action-dialog-backdrop" role="presentation" onClick={onCancel}>
    <section ref={dialogRef} className="action-dialog" role="alertdialog" aria-modal="true" aria-labelledby="leave-confirm-title" onClick={event => event.stopPropagation()}>
      <header>
        <span className="action-dialog-icon danger"><TriangleAlert /></span>
        <div><small>未保存的内容</small><h3 id="leave-confirm-title">离开当前页面？</h3></div>
        <button className="icon-btn" aria-label="关闭" onClick={onCancel}><X /></button>
      </header>
      <div className="action-dialog-body">
        <p>{dirtyCount > 1 ? `当前有 ${dirtyCount} 处填写中的内容尚未提交` : '当前有填写中的内容尚未提交'}，离开后将丢失这些内容。</p>
      </div>
      <footer>
        <button className="button" onClick={onCancel}>继续编辑</button>
        <button className="button danger-fill" data-dialog-initial-focus onClick={onConfirm}>放弃并离开</button>
      </footer>
    </section>
  </div>
}
