import React, { useEffect, useMemo } from 'react'
import { ArrowUpDown, Search } from 'lucide-react'
import { Candidate } from '../api'
import { candidateStopped, sourceLabel, stageTone, date, parseDate } from '../shared/format'
import { pageInfo, PageBar } from '../shared/Pagination'
import { usePageFilterState } from '../shared/pageFilterState'

const PAGE_SIZE = 20
type Mode = 'active' | 'all' | 'stopped'
type SortKey = 'updated' | 'name' | 'stage'
const SORT_OPTIONS: Array<{ key: SortKey; label: string }> = [
  { key: 'updated', label: '更新时间（新→旧）' },
  { key: 'name', label: '姓名（A→Z）' },
  { key: 'stage', label: '阶段（待复核优先）' },
]

/** 跨字段搜索：姓名、公司、职位、岗位、客户、阶段、渠道与 ID 均参与匹配。 */
const candidateSearchText = (candidate: Candidate) =>
  [
    String(candidate.id),
    candidate.name,
    candidate.current_company,
    candidate.current_title,
    candidate.job,
    candidate.client,
    candidate.clean_stage,
    candidate.flow_bucket,
    candidate.raw_status,
    sourceLabel(candidate.source_type),
    candidate.city,
    candidate.education,
    candidate.experience,
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()

const matchesQuery = (candidate: Candidate, keyword: string) => !keyword || candidateSearchText(candidate).includes(keyword)

/** 稳定排序：比较值相同时按 id 倒序兜底，结果顺序确定、不随刷新抖动。 */
const compareCandidates = (a: Candidate, b: Candidate, key: SortKey): number => {
  let result: number
  if (key === 'name') {
    result = (a.name || '').localeCompare(b.name || '', 'zh-Hans-CN')
  } else if (key === 'stage') {
    result =
      Number(candidateStopped(a)) - Number(candidateStopped(b)) ||
      (a.clean_stage || a.flow_bucket || '待复核').localeCompare(b.clean_stage || b.flow_bucket || '待复核', 'zh-Hans-CN')
  } else {
    result = (parseDate(b.updated_at) || 0) - (parseDate(a.updated_at) || 0)
  }
  return result || b.id - a.id
}

export function Candidates({ items, openCandidate, compact = false }: { items: Candidate[]; openCandidate: (id: number, navIds?: number[]) => void; compact?: boolean }) {
  // 筛选/排序/页码跨 tab 切换与刷新保持（compact 内嵌模式不渲染工具栏，不受影响）。
  const [mode, setMode] = usePageFilterState<Mode>('candidates.mode', 'active')
  const [query, setQuery] = usePageFilterState<string>('candidates.query', '')
  const [sortKey, setSortKey] = usePageFilterState<SortKey>('candidates.sort', 'updated')
  const [page, setPage] = usePageFilterState<number>('candidates.page', 0)

  const keyword = query.trim().toLowerCase()
  const searched = useMemo(() => (items ?? []).filter(candidate => matchesQuery(candidate, keyword)), [items, keyword])
  const counts = useMemo(
    () => ({
      active: searched.filter(candidate => !candidateStopped(candidate)).length,
      stopped: searched.filter(candidate => candidateStopped(candidate)).length,
      all: searched.length,
    }),
    [searched],
  )
  const filtered = useMemo(() => {
    // compact 模式保持调用方给定顺序与全量展示（概览最近更新人选）。
    if (compact) return items ?? []
    const inMode = (candidate: Candidate) =>
      mode === 'all' || (mode === 'stopped' ? candidateStopped(candidate) : !candidateStopped(candidate))
    return [...searched].filter(inMode).sort((a, b) => compareCandidates(a, b, sortKey))
  }, [searched, mode, sortKey, compact, items])

  const { pageCount, currentPage, from, to } = pageInfo(filtered.length, PAGE_SIZE, page)
  // 页码钳制：搜索/范围/外部数据变化后把内部页码状态也夹回有效范围，列表回涨时不会跳回旧页。
  useEffect(() => {
    setPage(current => Math.min(current, Math.max(0, pageCount - 1)))
  }, [pageCount, setPage])

  const shown = compact ? filtered : filtered.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE)
  const selectMode = (next: Mode) => {
    setMode(next)
    setPage(0)
  }
  // 打开详情时把当前筛选/排序后的完整顺序一并交给 App：详情页"上一位/下一位"严格按名单此刻的顺序切换。
  const openDetail = (id: number) => openCandidate(id, filtered.map(candidate => candidate.id))
  const open = (event: React.KeyboardEvent, id: number) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      openDetail(id)
    }
  }
  const ariaSort = (key: SortKey) => (sortKey === key ? (key === 'name' ? 'ascending' : 'descending') : undefined)

  return (
    <div className={compact ? 'table-wrap embedded' : 'section table-section'}>
      {!compact && (
        <div className="section-head candidates-head">
          <h2>候选人关系</h2>
          <label className="candidates-search">
            <Search aria-hidden="true" />
            <span className="sr-only">搜索候选人</span>
            <input
              type="search"
              value={query}
              onChange={event => {
                setQuery(event.target.value)
                setPage(0)
              }}
              placeholder="搜索姓名、公司、岗位、阶段或渠道"
              aria-label="搜索候选人"
            />
          </label>
          <label className="candidates-sort">
            <ArrowUpDown aria-hidden="true" />
            <span className="sr-only">排序方式</span>
            <select
              value={sortKey}
              aria-label="排序方式"
              onChange={event => {
                setSortKey(event.target.value as SortKey)
                setPage(0)
              }}
            >
              {SORT_OPTIONS.map(option => (
                <option key={option.key} value={option.key}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <div className="detail-actions" role="group" aria-label="候选人范围">
            <button type="button" className={`button ${mode === 'active' ? 'primary' : ''}`} aria-pressed={mode === 'active'} aria-label={`待处理 ${counts.active}`} onClick={() => selectMode('active')}>
              <span>待处理</span><span className="mode-count">{counts.active}</span>
            </button>
            <button type="button" className={`button ${mode === 'all' ? 'primary' : ''}`} aria-pressed={mode === 'all'} aria-label={`全部 ${counts.all}`} onClick={() => selectMode('all')}>
              <span>全部</span><span className="mode-count">{counts.all}</span>
            </button>
            <button type="button" className={`button ${mode === 'stopped' ? 'primary' : ''}`} aria-pressed={mode === 'stopped'} aria-label={`已停止 ${counts.stopped}`} onClick={() => selectMode('stopped')}>
              <span>已停止</span><span className="mode-count">{counts.stopped}</span>
            </button>
          </div>
          <span className="candidates-count" role="status">共 {filtered.length} 个结果</span>
        </div>
      )}
      <div className="table-wrap data-table-scroll" role="region" tabIndex={0} aria-label="候选人列表，可横向滚动">
        <table>
          <caption className="sr-only">候选人列表</caption>
          <thead>
            <tr>
              <th scope="col" aria-sort={ariaSort('name')}>候选人</th>
              <th scope="col">目标岗位</th>
              <th scope="col">渠道</th>
              <th scope="col" aria-sort={ariaSort('stage')}>阶段</th>
              <th scope="col" aria-sort={ariaSort('updated')}>更新</th>
            </tr>
          </thead>
          <tbody>
            {shown.map(candidate => (
              <tr key={candidate.id} tabIndex={0} aria-label={`打开候选人 ${candidate.name}`} onKeyDown={event => open(event, candidate.id)} onClick={() => openDetail(candidate.id)}>
                <td className="candidate-cell"><b>{candidate.name}</b><small>{candidate.current_company || '公司待补充'} · {candidate.current_title || '职位待补充'}</small></td>
                <td className="candidate-cell"><b>{candidate.job || '待关联岗位'}</b><small>{candidate.client}</small></td>
                <td>{sourceLabel(candidate.source_type)}</td>
                <td><span className={`tag ${stageTone(candidate.clean_stage)}`}>{candidate.clean_stage || candidate.flow_bucket || '待复核'}</span></td>
                <td>{date(candidate.updated_at)}</td>
              </tr>
            ))}
            {shown.length === 0 && (
              <tr>
                <td colSpan={5}><div className="empty">没有符合当前条件的候选人。</div></td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {!compact && filtered.length > PAGE_SIZE && (
        <PageBar page={currentPage} pageCount={pageCount} from={from} to={to} total={filtered.length} label="候选人列表分页" onPage={setPage} />
      )}
    </div>
  )
}
