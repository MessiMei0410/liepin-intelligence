import { BriefcaseBusiness, CircleAlert, Crosshair, ListChecks, ShieldCheck } from 'lucide-react'
import type { JobDetail } from '../api'
import { recordValue, textList } from '../shared/records'

type BriefItem = { label: string; items: string[]; icon: typeof BriefcaseBusiness; empty: string }

const take = (items: string[], limit = 6) => items.slice(0, limit)

export function JobBrief({ job }: { job: JobDetail }) {
  const position = recordValue(job.position)
  const profile = recordValue(job.profile)
  const strategy = job.latest_effective_strategy
  const hardRequirements = textList(profile.hard_requirements, position.hard_requirements, job.hard_requirements)
  const targetCompanies = textList(
    strategy?.company_tiers.flatMap(tier => tier.companies), profile.target_companies,
    position.target_companies, job.target_companies, job.metric_target_companies,
  )
  const exclusions = textList(profile.exclusion_tags, position.exclusions, job.exclusions, job.exclude_terms)
  const missing = [
    !hardRequirements.length ? '硬性要求尚未确认' : '',
    !targetCompanies.length ? '目标公司池尚未确认' : '',
    !strategy ? '尚未形成生效寻访策略' : '',
  ].filter(Boolean)
  const items: BriefItem[] = [
    { label: '岗位要解决什么', items: textList(profile.jd_analysis_summary, job.summary).map(item => `目标：${item}`), icon: BriefcaseBusiness, empty: '岗位职责摘要待补充' },
    { label: '必须先满足', items: hardRequirements, icon: ShieldCheck, empty: '硬性条件待顾问确认' },
    { label: '优先从哪里找', items: targetCompanies, icon: Crosshair, empty: '目标公司池待确认' },
    { label: '明确不找什么', items: exclusions, icon: CircleAlert, empty: '暂无明确排除项' },
  ]

  return (
    <section className="job-detail-section job-brief" aria-label="岗位 Brief">
      <h3>岗位 Brief<span className="job-section-count">寻访前核对</span></h3>
      <p className="job-brief-intro"><ListChecks />此处只汇总已确认的岗位资料与当前生效策略；缺失项需先校准，避免把模糊 JD 直接变成搜索条件。</p>
      <div className="job-brief-grid">
        {items.map(item => {
          const Icon = item.icon
          const visible = take(item.items)
          return <div className="job-brief-card" key={item.label}>
            <h4><Icon />{item.label}</h4>
            {visible.length
              ? <ul>{visible.map(value => <li key={value}>{value}</li>)}{item.items.length > visible.length && <li className="job-brief-more">另有 {item.items.length - visible.length} 项，见下方详情</li>}</ul>
              : <p>{item.empty}</p>}
          </div>
        })}
      </div>
      {missing.length > 0 && <div className="job-brief-checks">
        <p className="job-brief-missing"><b>启动前待确认：</b>{missing.join('；')}</p>
      </div>}
    </section>
  )
}
