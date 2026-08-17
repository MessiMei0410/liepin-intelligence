import React, { useEffect, useMemo } from 'react'
import { ArrowUpDown, Search } from 'lucide-react'
import { Job } from '../api'
import { date, parseDate } from '../shared/format'
import { pageInfo, PageBar } from '../shared/Pagination'
import { usePageFilterState } from '../shared/pageFilterState'

const PAGE_SIZE = 20
type Mode = 'p0' | 'pipeline' | 'all'
type SortKey = 'updated' | 'active' | 'name'
const PIPELINE_STAGES = ['sourcing', 'published', 'active_pipeline', 'client_feedback', 'offer']
const SORT_OPTIONS: Array<{ key: SortKey; label: string }> = [
  { key: 'updated', label: '更新时间（新→旧）' },
  { key: 'active', label: '活跃人选（多→少）' },
  { key: 'name', label: '客户 / 岗位（A→Z）' },
]

/** 稳定排序：比较值相同时按 id 倒序兜底，保证结果顺序确定、不随渲染抖动。 */
const compareJobs = (a: Job, b: Job, key: SortKey): number => {
  let result: number
  if (key === 'name') {
    result =
      (a.client || '').localeCompare(b.client || '', 'zh-Hans-CN') ||
      (a.title || '').localeCompare(b.title || '', 'zh-Hans-CN')
  } else if (key === 'active') {
    result = (b.active_candidate_count || 0) - (a.active_candidate_count || 0)
  } else {
    result = (parseDate(b.updated_at) || 0) - (parseDate(a.updated_at) || 0)
  }
  return result || b.id - a.id
}

const matchesQuery = (job: Job, keyword: string): boolean => {
  if (!keyword) return true
  const haystack = [
    String(job.id),
    job.client,
    job.title,
    job.location,
    job.status,
    job.lifecycle_stage,
    job.priority,
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
  return haystack.includes(keyword)
}

export function Jobs({ items, onSelect }: { items: Job[]; onSelect: (id: number) => void }) {
  // 筛选/排序/页码跨 tab 切换与刷新保持：用户调好的视图不因组件卸载而重置。
  const [mode, setMode] = usePageFilterState<Mode>('jobs.mode', 'p0')
  const [query, setQuery] = usePageFilterState<string>('jobs.query', '')
  const [sortKey, setSortKey] = usePageFilterState<SortKey>('jobs.sort', 'updated')
  const [page, setPage] = usePageFilterState<number>('jobs.page', 0)

  const keyword = query.trim().toLowerCase()

  const searched = useMemo(
    // 运行时防御：外部传入空/缺省列表时与空态同路径处理，不抛错。
    () => (items ?? []).filter(job => matchesQuery(job, keyword)),
    [items, keyword],
  )

  const counts = useMemo(
    () => ({
      p0: searched.filter(job => (job.priority || '').includes('P0')).length,
      pipeline: searched.filter(job => PIPELINE_STAGES.includes(job.lifecycle_stage || '')).length,
      all: searched.length,
    }),
    [searched],
  )

  const filtered = useMemo(() => {
    const inMode = (job: Job) =>
      mode === 'all' ||
      (mode === 'p0' ? (job.priority || '').includes('P0') : PIPELINE_STAGES.includes(job.lifecycle_stage || ''))
    return [...searched].filter(inMode).sort((a, b) => compareJobs(a, b, sortKey))
  }, [searched, mode, sortKey])

  const { pageCount, currentPage, from, to } = pageInfo(filtered.length, PAGE_SIZE, page)
  // 页码钳制：恢复的持久化页码或数据收缩后夹回有效范围，列表回涨时不跳旧页。
  useEffect(() => {
    setPage(current => Math.min(current, Math.max(0, pageCount - 1)))
  }, [pageCount, setPage])
  const shown = filtered.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE)
  const selectMode = (next: Mode) => {
    setMode(next)
    setPage(0)
  }
  const open = (event: React.KeyboardEvent, id: number) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onSelect(id)
    }
  }
  const ariaSort = (key: SortKey) => (sortKey === key ? (key === 'name' ? 'ascending' : 'descending') : undefined)

  return (
    <section className="section table-section">
      <div className="section-head jobs-head">
        <h2>岗位</h2>
        <label className="jobs-search">
          <Search aria-hidden="true" />
          <span className="sr-only">搜索岗位</span>
          <input
            type="search"
            value={query}
            onChange={event => {
              setQuery(event.target.value)
              setPage(0)
            }}
            placeholder="搜索岗位、客户、地点或阶段"
            aria-label="搜索岗位"
          />
        </label>
        <label className="jobs-sort">
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
        <div className="detail-actions" role="group" aria-label="岗位范围">
          <button type="button" className={`button ${mode === 'p0' ? 'primary' : ''}`} aria-pressed={mode === 'p0'} aria-label={`P0 ${counts.p0}`} onClick={() => selectMode('p0')}>
            <span>P0</span><span className="mode-count">{counts.p0}</span>
          </button>
          <button type="button" className={`button ${mode === 'pipeline' ? 'primary' : ''}`} aria-pressed={mode === 'pipeline'} aria-label={`在推 ${counts.pipeline}`} onClick={() => selectMode('pipeline')}>
            <span>在推</span><span className="mode-count">{counts.pipeline}</span>
          </button>
          <button type="button" className={`button ${mode === 'all' ? 'primary' : ''}`} aria-pressed={mode === 'all'} aria-label={`全部 ${counts.all}`} onClick={() => selectMode('all')}>
            <span>全部</span><span className="mode-count">{counts.all}</span>
          </button>
        </div>
        <span className="jobs-count" role="status">共 {filtered.length} 个结果</span>
      </div>
      <div className="table-wrap data-table-scroll" role="region" tabIndex={0} aria-label="岗位列表，可横向滚动">
        <table>
          <caption className="sr-only">岗位列表</caption>
          <thead>
            <tr>
              <th scope="col">客户 / 岗位</th>
              <th scope="col">优先级 / 阶段</th>
              <th scope="col">地点</th>
              <th scope="col" className="num" aria-sort={ariaSort('active')}>活跃人选</th>
              <th scope="col" aria-sort={ariaSort('updated')}>更新</th>
            </tr>
          </thead>
          <tbody>
            {shown.map(job => (
              <tr key={job.id} tabIndex={0} aria-label={`打开岗位 ${job.client} ${job.title}`} onKeyDown={event => open(event, job.id)} onClick={() => onSelect(job.id)}>
                <td><b>{job.title}</b><small>{job.client}</small></td>
                <td>{job.priority?.includes('P0-最急') && <span className="tag warn">P0 最急</span>}{job.filter_model_missing && <span className="tag warn" title="该岗位有活跃人选，但暂无确定性筛选模型：过滤/分级请求将失败关闭，需先补岗位证据模型">无筛选模型</span>}<small>{job.status || job.lifecycle_stage || '待启动'}</small></td>
                <td>{job.location || '-'}</td>
                <td className="num">{job.active_candidate_count || 0}</td>
                <td>{date(job.updated_at)}</td>
              </tr>
            ))}
            {shown.length === 0 && (
              <tr>
                <td colSpan={5}><div className="empty">没有符合当前条件的岗位。</div></td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {filtered.length > PAGE_SIZE && (
        <PageBar page={currentPage} pageCount={pageCount} from={from} to={to} total={filtered.length} label="岗位列表分页" onPage={setPage} />
      )}
    </section>
  )
}
