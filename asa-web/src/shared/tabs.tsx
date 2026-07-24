import React from 'react'
import { CircleGauge, BriefcaseBusiness, ListChecks, UsersRound, Radar } from 'lucide-react'

export type Tab = 'overview' | 'jobs' | 'progress' | 'candidates' | 'radar'

export const tabs: Array<[Tab, string, React.ReactNode]> = [
  ['overview', '总览', <CircleGauge />], ['jobs', '岗位看板', <BriefcaseBusiness />],
  ['progress', '人选进度', <ListChecks />], ['candidates', '人选列表', <UsersRound />],
  ['radar', '人才雷达', <Radar />],
]
