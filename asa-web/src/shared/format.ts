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

export const sourceLabel=(v='')=>{const source=v.toLowerCase();return source.includes('mapping')?'Mapping 直挖':source.includes('xsaas')||source.includes('x-saas')||v.includes('X-SaaS')?'X-SaaS':source.includes('liepin')||v.includes('猎聘')?'猎聘':'人才库'}
export const sourceLinkLabel=(v='')=>sourceLabel(v)==='Mapping 直挖'?'Mapping 公开资料':sourceLabel(v)==='X-SaaS'?'X-SaaS档案':sourceLabel(v)==='猎聘'?'猎聘简历':'来源档案'
export const eventStatusLabel=(v='')=>({pending_review:'待复核',completed:'已完成',open:'待处理',done:'已完成',verified:'已核验',failed:'失败',stopped:'已停止',
  // 生命周期一等事件状态（面试/Offer/入职）
  scheduled:'已安排',passed:'通过',extended:'已发出',accepted:'已接受',declined:'已拒绝',recorded:'已记录',cancelled:'已取消',withdrawn:'已撤回',
  // 旧 client_feedback 口径（event_status 承载反馈类型），保留可读
  approved:'客户认可',interview:'安排面试',rejected:'客户否决',hold:'暂缓推进',other:'其他反馈',interviewing:'进入面试',interview_passed:'面试通过',interview_failed:'面试未通过',offer:'进入 Offer',hired:'确认入职',
}[v]||v.replaceAll('_',' ')||'已记录')
// 生命周期一等事件（面试/Offer/入职）：event_type → 中文标签与时间线圆点色调（空串=非生命周期事件）。
export const lifecycleEventLabel=(v='')=>({interview_scheduled:'面试安排',interview_completed:'面试完成',offer_extended:'Offer 发出',offer_accepted:'Offer 已接受',offer_declined:'Offer 已拒绝',onboarded:'确认入职'}[v]||'')
export const lifecycleEventTone=(v='')=>v.startsWith('interview_')?'tone-interview':v.startsWith('offer_')?'tone-offer':v==='onboarded'?'tone-onboard':''
