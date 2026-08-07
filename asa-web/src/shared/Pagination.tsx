import { ChevronLeft, ChevronRight } from 'lucide-react'

export type PageInfo = { pageCount: number; currentPage: number; from: number; to: number }

/** 纯函数分页信息：总数变化时自动把页码夹回有效范围，总数为 0 时范围显示为 0。 */
export function pageInfo(total: number, pageSize: number, page: number): PageInfo {
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const currentPage = Math.min(page, pageCount - 1)
  const from = total === 0 ? 0 : currentPage * pageSize + 1
  const to = Math.min(total, (currentPage + 1) * pageSize)
  return { pageCount, currentPage, from, to }
}

export function PageBar({ page, pageCount, from, to, total, label, onPage }: {
  page: number
  pageCount: number
  from: number
  to: number
  total: number
  label: string
  onPage: (page: number) => void
}) {
  return <footer className="workbench-pagination" aria-label={label}>
    <span>第 {page + 1} / {pageCount} 页 · 显示 {from}–{to} / 共 {total} 个</span>
    <div>
      <button type="button" className="icon-btn" title="上一页" aria-label="上一页" disabled={page === 0} onClick={() => onPage(page - 1)}><ChevronLeft /></button>
      <button type="button" className="icon-btn" title="下一页" aria-label="下一页" disabled={page >= pageCount - 1} onClick={() => onPage(page + 1)}><ChevronRight /></button>
    </div>
  </footer>
}
