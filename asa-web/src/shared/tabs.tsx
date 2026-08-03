import React from 'react'
import { MessageSquareText, BriefcaseBusiness, ListChecks, UsersRound } from 'lucide-react'

export type Tab = 'agent' | 'jobs' | 'progress' | 'candidates'

export const tabs: Array<[Tab, string, React.ReactNode]> = [
  ['agent', 'Agent', <MessageSquareText />], ['jobs', '岗位看板', <BriefcaseBusiness />],
  ['progress', '人选进度', <ListChecks />], ['candidates', '人选列表', <UsersRound />],
]
