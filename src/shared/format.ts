import { Candidate } from '../api'

export const stageTone = (value = '') => value.includes('停止') || value.includes('淘汰') ? 'muted' : value.includes('待') || value.includes('核验') ? 'warn' : 'ok'
export const candidateStopped = (candidate: Candidate) => ['初筛不通过','停止','淘汰','关闭'].some(token => (candidate.clean_stage || '').includes(token)) || ['screen_rejected','xsaas_review_stop','rejected','stopped','closed'].includes((candidate.raw_status || '').toLowerCase())
export const date = (value?: string) => value ? value.replace('T', ' ').slice(0, 16) : '暂无记录'
export const parseDate = (value?: string) => value ? new Date(value.includes('T') ? value : value.replace(' ', 'T')).getTime() : 0
export const elapsed = (started?: string, finished?: string, now = Date.now()) => {
  const from = parseDate(started)
  if (!from) return ''
  const seconds = Math.max(0, Math.floor(((parseDate(finished) || now) - from) / 1000))
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  return minutes < 60 ? `${minutes} 分 ${seconds % 60} 秒` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
}

export const sourceLabel=(v='')=>v.toLowerCase().includes('xsaas')||v.toLowerCase().includes('x-saas')?'X-SaaS':v.toLowerCase().includes('liepin')?'猎聘':'人才库'
export const sourceLinkLabel=(v='')=>sourceLabel(v)==='X-SaaS'?'X-SaaS档案':sourceLabel(v)==='猎聘'?'猎聘简历':'来源档案'
export const eventStatusLabel=(v='')=>({pending_review:'待复核',completed:'已完成',open:'待处理',done:'已完成',verified:'已核验',failed:'失败',stopped:'已停止'}[v]||v.replaceAll('_',' ')||'已记录')
