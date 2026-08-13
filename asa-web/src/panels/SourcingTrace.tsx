import { Search } from 'lucide-react'
import type { CandidateDetail } from '../api'
import { date, sourceLabel } from '../shared/format'
import { formatFeedbackScore } from './overviewFormat'

type Attribution = CandidateDetail['sourcing_attributions'][number]
type Recall = NonNullable<CandidateDetail['sourcing_recalls']>[number]

const sourceKey = (channel: string, query: string) =>
  `${sourceLabel(channel)}\u0000${query.trim().replace(/\s+/g, ' ').toLocaleLowerCase()}`

const textValue = (value: unknown) => typeof value === 'string' ? value.trim() : ''

const provenanceText = (recall: Recall) => {
  const labels = (recall.query_provenance || []).flatMap(entry => {
    const kind = textValue(entry.kind)
    const kindLabel = {
      keyword_group: '关键词组',
      company_keyword: '公司定向',
      company_pool: '目标公司',
    }[kind] || ''
    const parts = [
      textValue(entry.tier),
      textValue(entry.group),
      textValue(entry.company),
      textValue(entry.targets),
      textValue(entry.path),
    ].filter(Boolean)
    return parts.length ? [`${kindLabel ? `${kindLabel} · ` : ''}${parts.join(' · ')}`] : []
  })
  if (labels.length) return [...new Set(labels)].join('；')
  const families = (recall.query_family_ids || []).map(textValue).filter(Boolean)
  return families.length ? `查询族 · ${[...new Set(families)].join(' · ')}` : ''
}

function LearningSignals({ item }: { item: Attribution }) {
  const signals = [
    ['通过', item.review_pass_count],
    ['联系', item.contacted_count],
    ['推荐', item.recommended_count],
    ['停止', item.stopped_count],
    ['客户正向', item.client_positive_count],
    ['客户否决', item.client_rejected_count],
  ].filter(([, count]) => Number(count || 0) > 0)
  return <div className="learning-signals">{signals.length
    ? signals.map(([label, count]) => <span key={String(label)}>{label} {Number(count)}</span>)
    : <span>暂无后续业务反馈</span>}
  </div>
}

function RecallHistory({ recalls }: { recalls: Recall[] }) {
  if (!recalls.length) return <small className="sourcing-lineage-missing">本次来源尚无精确执行记录</small>
  return <div className="sourcing-recall-list" aria-label="精确寻访执行">
    {recalls.map(recall => {
      const provenance = provenanceText(recall)
      const hasStrategyLineage = Boolean(recall.strategy_hash || recall.strategy_artifact_id || recall.strategy_revision)
      return <article className="sourcing-recall" key={recall.recall_id}>
        <div className="sourcing-recall-head">
          <b>执行 {recall.run_id}</b>
          <time dateTime={recall.created_at}>{date(recall.created_at)}</time>
        </div>
        {provenance && <p>{provenance}</p>}
        <small>
          {hasStrategyLineage
            ? `${recall.strategy_revision ? `策略 revision ${recall.strategy_revision}` : '已批准策略'}${recall.query_cell_id ? ` · 单元 ${recall.query_cell_id}` : ''}`
            : `历史记录未保存策略版本${recall.query_cell_id ? ` · 单元 ${recall.query_cell_id}` : ''}`}
        </small>
      </article>
    })}
  </div>
}

export function SourcingTrace({ value }: { value: CandidateDetail }) {
  const attributions = value.sourcing_attributions || []
  const recalls = value.sourcing_recalls || []
  if (!attributions.length && !recalls.length) return null

  const attributionKeys = new Set(attributions.map(item => sourceKey(item.channel, item.source_query)))
  const unmatchedRecalls = recalls.filter(recall => !attributionKeys.has(sourceKey(recall.channel, recall.source_query)))

  return <section className="sourcing-trace">
    <div className="sourcing-trace-head">
      <Search />
      <div><span>寻访来源 · {recalls.length} 次执行</span><b>怎么找到他的</b></div>
    </div>
    {attributions.map(item => {
      const matches = recalls.filter(recall => sourceKey(recall.channel, recall.source_query) === sourceKey(item.channel, item.source_query))
      const score = formatFeedbackScore(item.learning_score)
      return <div className="sourcing-trace-row" key={item.id}>
        <div className="trace-main">
          <span>{sourceLabel(item.channel)} · {item.source_round || '寻访查询'}</span>
          <b>{item.source_query}</b>
          <small>{item.source_purpose || '根据岗位策略生成'}</small>
          <RecallHistory recalls={matches} />
        </div>
        <div className="trace-side">
          <span className={`feedback-score ${score.tone}`}>{score.text}</span>
          <LearningSignals item={item} />
        </div>
      </div>
    })}
    {unmatchedRecalls.map(recall => <div className="sourcing-trace-row sourcing-trace-row-recall-only" key={recall.recall_id}>
      <div className="trace-main">
        <span>{sourceLabel(recall.channel)} · 执行记录</span>
        <b>{recall.source_query || '未记录查询词'}</b>
        <RecallHistory recalls={[recall]} />
      </div>
      <div className="trace-side"><span className="feedback-score muted">待汇总效果</span></div>
    </div>)}
  </section>
}
