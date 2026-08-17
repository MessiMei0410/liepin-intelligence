import { arrayValue, recordValue } from '../shared/records'

const scopeLabels: Record<string, string> = {
  all_active: '全部在推进关系',
  nonmatching: '规则判定不匹配关系',
}

export function ApprovalSnapshot({ preflight }: { preflight?: Record<string, unknown> }) {
  const snapshot = recordValue(preflight)
  const rawBatchSize = Number(snapshot.batch_size)
  const hasBatchSize = Number.isFinite(rawBatchSize) && rawBatchSize >= 0
  const batchSize = hasBatchSize ? Math.trunc(rawBatchSize) : 0
  const exactContent = String(snapshot.exact_content || '').trim()
  const scopeMode = String(snapshot.scope_mode || '').trim()
  const items = arrayValue(snapshot.items).map(recordValue).slice(0, 3)
  const candidateRecordsPreserved = snapshot.candidate_records_preserved === true
  const batchUnit = scopeMode || candidateRecordsPreserved ? '岗位关系' : '操作'

  if (!hasBatchSize && !exactContent && items.length === 0) return null

  return <div className="approval-snapshot" aria-label="审批锁定范围">
    <div className="approval-snapshot-head">
      <b>{hasBatchSize ? `锁定 ${batchSize} 条${batchUnit}` : '本次锁定范围'}</b>
      {scopeMode && <span>{scopeLabels[scopeMode] || scopeMode}</span>}
      {candidateRecordsPreserved && <span>人才主档保留</span>}
    </div>
    {exactContent && <p className="approval-snapshot-exact">{exactContent}</p>}
    {items.length > 0 && <ul className="approval-snapshot-items" aria-label="审批锁定明细">
      {items.map((item, index) => {
        const candidate = String(item.candidate || item.name || `操作 ${index + 1}`).trim()
        const context = [item.company, item.title, item.channel].map(value => String(value || '').trim()).filter(Boolean).join(' · ')
        const reason = String(item.reason || '').trim()
        return <li key={`${String(item.job_candidate_id || item.id || candidate)}-${index}`}>
          <b>{candidate}</b>
          {context && <span>{context}</span>}
          {reason && <small>{reason}</small>}
        </li>
      })}
    </ul>}
    {hasBatchSize && batchSize > items.length && items.length > 0 && <small className="approval-snapshot-more">显示 {items.length} / {batchSize} 条锁定明细</small>}
  </div>
}
