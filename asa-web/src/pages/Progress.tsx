import { useEffect, useMemo, useRef } from 'react'
import { Search, UserRoundSearch } from 'lucide-react'
import { Candidate } from '../api'
import { candidateStopped, parseDate, sourceLabel } from '../shared/format'
import { pageInfo, PageBar } from '../shared/Pagination'
import { usePageFilterState } from '../shared/pageFilterState'

const PAGE_SIZE = 10

const stageName = (candidate: Candidate) => candidate.flow_bucket || candidate.clean_stage || '待复核'

/** 跨字段搜索：姓名、公司、职位、岗位、阶段与渠道均参与匹配。 */
const progressSearchText = (candidate: Candidate) =>
  [
    candidate.name,
    candidate.current_company,
    candidate.current_title,
    candidate.job,
    candidate.client,
    sourceLabel(candidate.source_type),
    candidate.clean_stage,
    candidate.flow_bucket,
    candidate.raw_status,
    candidate.city,
    candidate.education,
    candidate.experience,
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()

const matchesQuery = (candidate: Candidate, keyword: string) => !keyword || progressSearchText(candidate).includes(keyword)

/** 组内稳定排序：更新时间新→旧、缺失最后，同值按 id 倒序兜底，顺序确定不随刷新抖动。 */
const compareCandidates = (a: Candidate, b: Candidate) =>
  (parseDate(b.updated_at) || 0) - (parseDate(a.updated_at) || 0) || b.id - a.id

/** 阶段排序：未停止在前、停止在后；同侧「待复核」兜底名优先，其余按阶段名排序。 */
const compareStages = (a: readonly [string, Candidate[]], b: readonly [string, Candidate[]]) => {
  const stoppedDiff = Number(candidateStopped(a[1][0])) - Number(candidateStopped(b[1][0]))
  if (stoppedDiff) return stoppedDiff
  if (a[0] === b[0]) return 0
  if (a[0] === '待复核') return -1
  if (b[0] === '待复核') return 1
  return a[0].localeCompare(b[0], 'zh-Hans-CN')
}

export function Progress({ items, openCandidate }: { items: Candidate[]; openCandidate: (id: number) => void }) {
  // 搜索词与各阶段页码跨 tab 切换与刷新保持。
  const [query, setQuery] = usePageFilterState<string>('progress.query', '')
  const [stagePages, setStagePages] = usePageFilterState<Record<string, number>>('progress.stagePages', {})

  const keyword = query.trim().toLowerCase()
  const searched = useMemo(() => (items ?? []).filter(candidate => matchesQuery(candidate, keyword)), [items, keyword])
  const groups = useMemo(() => {
    const grouped = new Map<string, Candidate[]>()
    for (const candidate of searched) {
      const key = stageName(candidate)
      const list = grouped.get(key)
      if (list) list.push(candidate)
      else grouped.set(key, [candidate])
    }
    return [...grouped.entries()]
      .map(([stage, list]) => [stage, [...list].sort(compareCandidates)] as const)
      .sort(compareStages)
  }, [searched])

  // 搜索变化回到各阶段第一页；数据收缩时把各阶段页码状态也夹回有效范围。
  // 恢复持久化搜索词的首挂载不重置页码（否则恢复动作自己清空了自己）。
  const previousKeywordRef = useRef(keyword)
  useEffect(() => {
    if (previousKeywordRef.current === keyword) return
    previousKeywordRef.current = keyword
    setStagePages({})
  }, [keyword, setStagePages])
  useEffect(() => {
    setStagePages(prev => {
      const clamped: Record<string, number> = {}
      for (const [stage, list] of groups) {
        clamped[stage] = Math.min(prev[stage] ?? 0, Math.max(0, Math.ceil(list.length / PAGE_SIZE) - 1))
      }
      return clamped
    })
  }, [groups, setStagePages])

  const goStagePage = (stage: string, next: number) => setStagePages(prev => ({ ...prev, [stage]: next }))

  return (
    <div className="stage-progress">
      {items.length > 0 && (
        <div className="progress-summary" role="status">共 {searched.length} 位人选 · {groups.length} 个阶段</div>
      )}
      <div className="progress-toolbar">
        <label className="jobs-search">
          <Search aria-hidden="true" />
          <span className="sr-only">搜索人选</span>
          <input
            type="search"
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder="搜索姓名、公司、岗位或阶段"
            aria-label="搜索人选进度"
          />
        </label>
      </div>
      {groups.length ? (
        <div className="stage-grid">
          {groups.map(([stage, list]) => {
            const { pageCount, currentPage, from, to } = pageInfo(list.length, PAGE_SIZE, stagePages[stage] ?? 0)
            const visible = list.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE)
            return (
              <section className="stage" key={stage} aria-label={stage}>
                <header><span>{stage}</span><b>{list.length}</b></header>
                {visible.map(candidate => (
                  <button key={candidate.id} onClick={() => openCandidate(candidate.id)} aria-label={`打开候选人 ${candidate.name}（${stage}）`}>
                    <UserRoundSearch aria-hidden="true" />
                    <span><b>{candidate.name}</b><small>{candidate.client} · {candidate.job}</small></span>
                  </button>
                ))}
                {list.length > PAGE_SIZE && (
                  <PageBar page={currentPage} pageCount={pageCount} from={from} to={to} total={list.length} label={`${stage}分页`} onPage={next => goStagePage(stage, next)} />
                )}
              </section>
            )
          })}
        </div>
      ) : (
        <div className="empty">没有符合当前条件的人选进度。</div>
      )}
    </div>
  )
}
