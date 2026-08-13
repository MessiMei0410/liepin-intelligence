import { ClipboardEvent, DragEvent, FormEvent, Fragment, KeyboardEvent, lazy, Suspense, useEffect, useReducer, useRef, useState } from 'react'
import { Activity, Archive, Banknote, BookPlus, Building2, ChevronDown, ClipboardCopy, FileText, Filter, History, ListChecks, LoaderCircle, Map, MessageSquareText, PanelRightClose, PanelRightOpen, Paperclip, Pencil, Plus, Radar, RefreshCw, Search, Send, Settings2, Square, Unlink, Users, X } from 'lucide-react'
import { api, AgentMessage, AgentSessionSummary, AnalysisTemplate, FloatingBridgeContext, Job, Workbench, WorkbenchItem, WorkbenchLane, workbenchLaneCount } from '../api'
import { AgentObjectEmbed } from './AgentObjectEmbed'
import { AgentMessageContent, AgentThinking } from './AgentMessageContent'
import { AgentPageContextBar } from './AgentPageContextBar'
import { ModelAuditPanel } from './ModelAuditPanel'
import { SourcingResultCard, type SourcingResultCardData } from '../workflows/SourcingResultCard'
import type { CandidateListCardData } from '../workflows/CandidateListCard'
import { CandidateListDialog } from './CandidateListDialog'
import { useDialogFocus } from '../shared/useDialogFocus'
import { CANDIDATE_UPDATED_EVENT, CandidateUpdatedDetail } from '../shared/candidateEvents'
import { updateCandidateListDialogData } from './candidateListDialogUpdate'
import { FULL_OBJECT_CLOSED_EVENT } from './navigation'
import { useCandidateListUpdates } from './useCandidateListUpdates'
import { agentConversationReducer, initialAgentConversationState } from './conversationState'
import { AgentContext, AgentReference, AgentTurn, createAgentTurn, streamAgentTurn } from './transport'
import { AGENT_ATTACHMENT_ACCEPT, AGENT_ATTACHMENT_MAX_COUNT, formatAttachmentSize, QueuedAgentAttachment, uploadAgentAttachment, UploadedAgentAttachment, validateAgentAttachment } from './attachments'
import { CandidateIntentConfirmation, ExecutionReceipt, SuggestedActionBar, UnderstandingCard } from './AgentInteractionCards'

const ACTIVE_SESSION_KEY = 'asaAgentSessionId'
const RadarPage = lazy(() => import('../pages/Radar').then(module => ({ default: module.RadarPage })))
const KnowledgeProposalsPanel = lazy(() => import('../panels/KnowledgeProposals').then(module => ({ default: module.KnowledgeProposalsPanel })))
const CompanyCalibrationPanel = lazy(() => import('../panels/CompanyCalibration').then(module => ({ default: module.CompanyCalibrationPanel })))
// 与 api.agentSession 默认 limit 对齐：拉满即视为历史被截断。
const SESSION_MESSAGE_LIMIT = 100

// 任务相对时间：刚刚/N 分钟前/N 小时前/N 天前，超过 7 天显示日期。
const relativeTime = (value?: string) => {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const diff = Date.now() - date.getTime()
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric' }).format(date)
}

// 跨天日期分隔标签：与上一条消息不同天时显示 今天/昨天/M月d日 周X。
const dayLabelFor = (value?: string, previous?: string) => {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const prev = previous ? new Date(previous) : null
  if (prev && !Number.isNaN(prev.getTime()) && date.toDateString() === prev.toDateString()) return ''
  const now = new Date()
  if (date.toDateString() === now.toDateString()) return '今天'
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (date.toDateString() === yesterday.toDateString()) return '昨天'
  return new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'short' }).format(date)
}

// 消息时间戳：同日仅显示时刻，跨天补日期，避免会话时间线模糊。
const formatMessageTime = (value: string) => {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const now = new Date()
  const sameDay = date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth()
    && date.getDate() === now.getDate()
  return new Intl.DateTimeFormat('zh-CN', {
    month: sameDay ? undefined : 'numeric',
    day: sameDay ? undefined : 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

const uploadedAttachments = (context?: AgentContext): Array<Partial<UploadedAgentAttachment>> => (
  Array.isArray(context?.uploaded_attachments)
    ? context.uploaded_attachments.filter(item => item && typeof item === 'object') as Array<Partial<UploadedAgentAttachment>>
    : []
)

const isArchiveAllTasksCommand = (value: string) => {
  const compact = value.replace(/[\s，。！？!?、,.]/g, '')
  return /^(?:请|帮我|麻烦)?(?:把)?(?:右侧|右边|任务栏(?:里|中|里的|中的)?)?(?:所有|全部)(?:历史)?任务(?:都)?归档(?:掉)?$/.test(compact)
    || /^(?:请|帮我|麻烦)?归档(?:右侧|右边|任务栏(?:里|中|里的|中的)?)?(?:所有|全部)(?:历史)?任务(?:掉)?$/.test(compact)
    || /^(?:请|帮我|麻烦)?清空(?:右侧|右边)?任务栏$/.test(compact)
}

const focusLabel = (focus?: Record<string, unknown> | null) => {
  if (!focus) return ''
  const job = focus.job && typeof focus.job === 'object' ? focus.job as Record<string, unknown> : {}
  const candidate = focus.candidate && typeof focus.candidate === 'object' ? focus.candidate as Record<string, unknown> : {}
  return String(candidate.name || [focus.client, job.title].filter(Boolean).join(' / ') || focus.client || '')
}

const contextLabel = (context: AgentContext) => String(
  context.candidate || [context.client, context.job].filter(Boolean).join(' / ') || context.client || context.job ||
  (context.type === 'workflow' && context.id ? `工作流 ${context.id}` : ''),
)

const focusContext = (focus?: Record<string, unknown> | null): AgentContext | undefined => {
  const value = focus?.context
  if (!value || typeof value !== 'object' || typeof (value as Record<string, unknown>).type !== 'string') return undefined
  const job = focus?.job && typeof focus.job === 'object' ? focus.job as Record<string, unknown> : {}
  const candidate = focus?.candidate && typeof focus.candidate === 'object' ? focus.candidate as Record<string, unknown> : {}
  const context = value as AgentContext
  return {
    ...context,
    client: context.client || focus?.client,
    job: context.job || job.title,
    candidate: context.candidate || candidate.name,
  }
}

const messageReferences = (message: AgentMessage): AgentReference[] => {
  const refs: AgentReference[] = []
  const normalize = (value: unknown) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase()
  const concreteWorkflowLabel = (label: unknown) => {
    const value = normalize(label)
    return value && !['查看计划', '工作流进度', '打开对象', '相关对象'].includes(value)
  }
  const genericReferenceLabel = (label: unknown) => {
    const value = normalize(label)
    return ['打开岗位', '打开人选', '打开工作流', '打开对象', '查看计划', '工作流进度', '相关对象'].includes(value)
  }
  const keyFor = (reference: AgentReference) => {
    const type = reference.type === 'job_candidate' ? 'candidate' : reference.type
    const id = String(reference.id ?? '').trim()
    if (type === 'workflow' && id) return `${type}:${id}`
    if (type === 'candidate' && id) return `${type}:${id}`
    if (type === 'job') return `${type}:${normalize(reference.label)}:${normalize(reference.subtitle)}`
    return `${type}:${id || normalize(reference.label)}`
  }
  const addRef = (reference: AgentReference) => {
    if (reference.id === undefined || reference.id === null || String(reference.id).trim() === '') return
    const next = { ...reference, type: reference.type === 'job_candidate' ? 'candidate' : reference.type }
    const key = keyFor(next)
    const existingIndex = refs.findIndex(item => keyFor(item) === key)
    if (existingIndex < 0) {
      refs.push(next)
      return
    }
    const existing = refs[existingIndex]
    if (next.type === 'workflow' && concreteWorkflowLabel(next.label) && !concreteWorkflowLabel(existing.label)) {
      refs[existingIndex] = { ...existing, ...next }
    } else if (!existing.subtitle && next.subtitle) {
      refs[existingIndex] = { ...existing, subtitle: next.subtitle }
    }
  }
  ;(message.references || []).forEach(addRef)
  for (const action of message.suggested_actions || []) {
    const type = String(action.type || '')
    if (!['open_candidate', 'open_job', 'open_workflow'].includes(type) || action.id === undefined) continue
    const refType = type.replace('open_', '')
    addRef({ type: refType, id: String(action.id), label: String(action.label || action.title || '打开对象') })
  }
  const card = message.action_card || {}
  const cardContext = card.context && typeof card.context === 'object' ? card.context as Record<string, unknown> : {}
  if (cardContext.type && cardContext.id !== undefined) {
    addRef({ type: String(cardContext.type), id: String(cardContext.id), label: String(cardContext.title || card.title || card.label || '相关对象') })
  }
  if (message.workflow_id) {
    addRef({ type: 'workflow', id: message.workflow_id, label: String(cardContext.title || card.title || '工作流进度') })
  }
  const hasConcreteWorkflow = refs.some(item => item.type === 'workflow' && concreteWorkflowLabel(item.label))
  const workflowRefs = hasConcreteWorkflow
    ? refs.filter(item => item.type === 'workflow' && concreteWorkflowLabel(item.label))
    : refs.filter(item => item.type === 'workflow')
  if (workflowRefs.length) {
    const workflowText = workflowRefs.map(item => `${normalize(item.label)} ${normalize(item.subtitle)}`).join(' ')
    const workflowBacked = Boolean(message.workflow_id || cardContext.type === 'workflow')
    return refs.filter(item => {
      if (item.type === 'workflow') return !hasConcreteWorkflow || concreteWorkflowLabel(item.label)
      // 工作流回复的对象卡以 action_card.context 指定的工作流为唯一主对象。
      // Core 可能同时返回岗位历史中的候选人引用；这些引用不属于本轮任务卡，
      // 若继续渲染会把寻访方案误显示成多张人选评估卡。
      if (workflowBacked) return false
      if (genericReferenceLabel(item.label)) return false
      if (item.type === 'job' && (workflowText.includes(normalize(item.label)) || workflowText.includes(normalize(item.subtitle)))) return false
      return true
    })
  }
  return refs.filter(item => !genericReferenceLabel(item.label) || !refs.some(other => other !== item && other.type === item.type && !genericReferenceLabel(other.label)))
}

export function AgentWorkspace({ jobs = [], workbench, templates, context, onOpenAnalysis, onRunTemplate, onManageTemplate, onCreateTemplate, onWorkbenchAction, onOpenFullObject }: {
  jobs?: Job[]; workbench: Workbench; templates: AnalysisTemplate[]; context: AgentContext;
  onOpenAnalysis: (id: string) => void; onRunTemplate: (id: string) => void;
  onManageTemplate: (template: AnalysisTemplate) => void; onCreateTemplate: () => void;
  onWorkbenchAction: (item: WorkbenchItem) => void; onOpenFullObject: (reference: AgentReference) => void;
}) {
  const [sessions, setSessions] = useState<AgentSessionSummary[]>([])
  const [sessionId, setSessionId] = useState(() => localStorage.getItem(ACTIVE_SESSION_KEY) || '')
  const [conversation, dispatch] = useReducer(agentConversationReducer, initialAgentConversationState)
  const [focus, setFocus] = useState<Record<string, unknown> | null>(null)
  const [attachedContext, setAttachedContext] = useState<AgentContext>(context)
  const [bridgeContext, setBridgeContext] = useState<FloatingBridgeContext>()
  const [draft, setDraft] = useState('')
  const [attachments, setAttachments] = useState<QueuedAgentAttachment[]>([])
  const [attachmentNotice, setAttachmentNotice] = useState('')
  const [attachmentDragActive, setAttachmentDragActive] = useState(false)
  const [lastTurn, setLastTurn] = useState<AgentTurn>()
  const [candidateListDialog, setCandidateListDialog] = useState<CandidateListCardData | null>(null)
  // 名单浮窗层级高于详情面板（--z-float 60 > --z-overlay 50），不关闭会盖在详情上方吞掉
  // 滚轮/指针事件。打开详情前暂存名单并关闭，详情关闭后自动恢复，避免"点个人选名单就没了"。
  const candidateListStashRef = useRef<CandidateListCardData | null>(null)
  useEffect(() => {
    const restore = () => {
      if (!candidateListStashRef.current) return
      setCandidateListDialog(candidateListStashRef.current)
      candidateListStashRef.current = null
    }
    window.addEventListener(FULL_OBJECT_CLOSED_EVENT, restore)
    return () => window.removeEventListener(FULL_OBJECT_CLOSED_EVENT, restore)
  }, [])
  const [refreshingCardJob, setRefreshingCardJob] = useState<number | null>(null)
  const [cardRefreshError, setCardRefreshError] = useState('')
  const [historyOpen, setHistoryOpen] = useState(false)
  const [modelAuditOpen, setModelAuditOpen] = useState(false)
  // 任务栏折叠偏好持久化：默认折叠（无偏好即折叠），用户展开后记住选择。
  const [taskRailCollapsed, setTaskRailCollapsed] = useState(() => localStorage.getItem('asaTaskRailCollapsed') !== '0')
  const setRailCollapsed = (collapsed: boolean) => {
    setTaskRailCollapsed(collapsed)
    localStorage.setItem('asaTaskRailCollapsed', collapsed ? '1' : '0')
  }
  const [taskQuery, setTaskQuery] = useState('')
  const [renamingId, setRenamingId] = useState('')
  const [renameValue, setRenameValue] = useState('')
  const [archiveConfirmId, setArchiveConfirmId] = useState('')
  const [bulkArchiveOpen, setBulkArchiveOpen] = useState(false)
  const [bulkArchiveBusy, setBulkArchiveBusy] = useState(false)
  const [bulkArchiveError, setBulkArchiveError] = useState('')
  const bulkArchiveDialogRef = useDialogFocus<HTMLElement>(bulkArchiveOpen)
  const [taskBusy, setTaskBusy] = useState('')
  const [taskError, setTaskError] = useState('')
  const [focusError, setFocusError] = useState('')
  const [focusBusy, setFocusBusy] = useState(false)
  const [turnProgress, setTurnProgress] = useState('')
  const [historyTruncated, setHistoryTruncated] = useState(false)
  const [searchUnavailable, setSearchUnavailable] = useState(false)
  const [taskMenu, setTaskMenu] = useState<{ sessionId: string; x: number; y: number } | undefined>(undefined)
  const [interactionError, setInteractionError] = useState('')
  const [structuredActionBusy, setStructuredActionBusy] = useState('')
  const [contextConflict, setContextConflict] = useState<{ incoming: AgentContext; restored?: AgentContext; label: string }>()
  const controllerRef = useRef<AbortController | undefined>(undefined)
  const generationRef = useRef(0)
  const searchedRef = useRef(false)

  // 名单弹窗打开时，监听候选人变更事件并就地刷新列表状态。
  useEffect(() => {
    if (!candidateListDialog) return undefined
    const onUpdate = (event: Event) => {
      const detail = (event as CustomEvent<CandidateUpdatedDetail>).detail
      if (!detail) return
      setCandidateListDialog(prev => (prev ? updateCandidateListDialogData(prev, detail) : prev))
    }
    window.addEventListener(CANDIDATE_UPDATED_EVENT, onUpdate)
    return () => window.removeEventListener(CANDIDATE_UPDATED_EVENT, onUpdate)
  }, [candidateListDialog])

  // 名单弹窗打开时，轮询服务端候选人变更（跨窗口/跨 webview 兜底）。
  useCandidateListUpdates(candidateListDialog, updater => setCandidateListDialog(prev => (prev ? updater(prev) : prev)))

  const searchSeqRef = useRef(0)
  const attachedContextRef = useRef(attachedContext)
  const endRef = useRef<HTMLDivElement>(null)
  const attachmentInputRef = useRef<HTMLInputElement>(null)
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const attachmentsRef = useRef<QueuedAgentAttachment[]>([])
  const attachmentGenerationRef = useRef(0)
  const { messages, phase, error } = conversation
  const loading = phase === 'streaming'
  const restoring = phase === 'restoring'

  const refreshSessions = async () => {
    try {
      const result = await api.agentSessions()
      setSessions(Array.isArray(result.sessions) ? result.sessions : [])
    } catch { /* Core upgrade compatibility: chat still works. */ }
  }
  // 任务搜索走服务端 q 参数；请求失败时回落本地过滤，不打断输入。
  const searchSessions = async (query: string, seq: number) => {
    try {
      const result = await api.agentSessions(30, query)
      if (seq !== searchSeqRef.current) return
      setSessions(Array.isArray(result.sessions) ? result.sessions : [])
      setSearchUnavailable(false)
    } catch {
      if (seq === searchSeqRef.current) setSearchUnavailable(true)
    }
  }
  const restoreSession = async (id: string) => {
    controllerRef.current?.abort()
    attachmentGenerationRef.current += 1; attachmentsRef.current = []; setAttachments([]); setAttachmentNotice('')
    const generation = ++generationRef.current
    setContextConflict(undefined)
    setTurnProgress('')
    setHistoryTruncated(false)
    dispatch({ type: 'restore_started' })
    try {
      const result = await api.agentSession(id)
      if (generation !== generationRef.current) return
      const restoredFocus = result.business_focus || null
      setSessionId(result.session_id); dispatch({ type: 'restore_succeeded', messages: result.messages }); setFocus(restoredFocus)
      setHistoryTruncated(result.messages.length >= SESSION_MESSAGE_LIMIT)
      const restoredContext = focusContext(restoredFocus)
      const incoming = attachedContextRef.current
      const incomingIsBusiness = Boolean(incoming.type && incoming.type !== 'page')
      if (restoredContext && incomingIsBusiness
        && (incoming.type !== restoredContext.type || String(incoming.id ?? '') !== String(restoredContext.id ?? ''))) {
        // 带着新业务上下文进入已有任务：不静默覆盖，交给用户选择。
        setContextConflict({ incoming, restored: restoredContext, label: focusLabel(restoredFocus) || contextLabel(restoredContext) || '已恢复任务' })
      } else if (restoredContext) {
        setAttachedContext(restoredContext)
      } else if (!incomingIsBusiness) {
        setAttachedContext({ type: 'page', page: 'agent' })
      }
      localStorage.setItem(ACTIVE_SESSION_KEY, result.session_id); setHistoryOpen(false)
    } catch (value) {
      if (generation === generationRef.current) {
        localStorage.removeItem(ACTIVE_SESSION_KEY); setSessionId('')
        dispatch({ type: 'restore_failed', error: value instanceof Error ? value.message : String(value) })
      }
    }
  }
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refreshSessions()
    if (sessionId) void restoreSession(sessionId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    if (context.type && context.type !== 'page') {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      attachmentGenerationRef.current += 1; attachmentsRef.current = []; setAttachments([]); setAttachmentNotice('')
      setAttachedContext(context)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(context)])
  useEffect(() => {
    attachedContextRef.current = attachedContext
  }, [attachedContext])
  // 搜索输入 300ms 防抖后请求服务端；清空时恢复默认列表。
  useEffect(() => {
    const query = taskQuery.trim()
    if (!query) {
      if (searchedRef.current) {
        searchedRef.current = false
        setSearchUnavailable(false)
        void refreshSessions()
      }
      return
    }
    searchedRef.current = true
    const seq = ++searchSeqRef.current
    const timer = window.setTimeout(() => void searchSessions(query, seq), 300)
    return () => window.clearTimeout(timer)
  }, [taskQuery])
  useEffect(() => {
    // 空任务首页是工作台，不是对话记录；首次进入必须从顶部开始阅读。
    if ((messages.length || loading) && typeof endRef.current?.scrollIntoView === 'function') {
      endRef.current.scrollIntoView({ block: 'end' })
    }
  }, [messages, loading])
  useEffect(() => () => controllerRef.current?.abort(), [])

  const startTaskWithContext = (next: AgentContext) => {
    controllerRef.current?.abort(); generationRef.current += 1; setSessionId(''); dispatch({ type: 'task_reset' }); setFocus(null); setDraft(''); setLastTurn(undefined)
    attachmentGenerationRef.current += 1; attachmentsRef.current = []; setAttachmentNotice('')
    setAttachedContext(next); setAttachments([]); setContextConflict(undefined); setFocusError(''); setTurnProgress(''); setHistoryTruncated(false)
    localStorage.removeItem(ACTIVE_SESSION_KEY); setHistoryOpen(false)
  }
  const replaceAttachments = (update: (current: QueuedAgentAttachment[]) => QueuedAgentAttachment[]) => {
    const next = update(attachmentsRef.current)
    attachmentsRef.current = next
    setAttachments(next)
  }
  const addAttachments = (files: File[]) => {
    if (loading || restoring || !files.length) return
    const available = Math.max(0, AGENT_ATTACHMENT_MAX_COUNT - attachmentsRef.current.length)
    const accepted = files.slice(0, available)
    setAttachmentNotice(files.length > available ? `最多同时添加 ${AGENT_ATTACHMENT_MAX_COUNT} 个附件` : '')
    const generation = attachmentGenerationRef.current
    const queuedItems = accepted.map(file => {
      const key = `${file.name}:${file.size}:${file.lastModified}:${crypto.randomUUID()}`
      const validationError = validateAgentAttachment(file)
      return { file, queued: {
        key, fileName: file.name, sizeBytes: file.size,
        state: validationError ? 'error' : 'uploading', error: validationError || undefined,
      } satisfies QueuedAgentAttachment }
    })
    replaceAttachments(current => [...current, ...queuedItems.map(item => item.queued)])
    for (const { file, queued } of queuedItems) {
      const { key } = queued
      const validationError = queued.error
      if (validationError) continue
      void uploadAgentAttachment(file).then(attachment => {
        if (generation !== attachmentGenerationRef.current) return
        replaceAttachments(current => current.map(item => item.key === key ? { ...item, state: 'ready', attachment } : item))
      }).catch(value => {
        if (generation !== attachmentGenerationRef.current) return
        const uploadError = value instanceof Error ? value.message : String(value)
        replaceAttachments(current => current.map(item => item.key === key ? { ...item, state: 'error', error: uploadError } : item))
      })
    }
  }
  const onAttachmentPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData.files || [])
    if (!files.length) return
    event.preventDefault()
    addAttachments(files)
  }
  const onAttachmentDrop = (event: DragEvent<HTMLFormElement>) => {
    event.preventDefault(); setAttachmentDragActive(false)
    addAttachments(Array.from(event.dataTransfer.files || []))
  }
  const newTask = () => startTaskWithContext(context.type && context.type !== 'page' ? context : { type: 'page', page: 'agent' })
  const resolveConflictWithNewTask = () => {
    if (contextConflict) startTaskWithContext(contextConflict.incoming)
  }
  const resolveConflictKeepCurrent = () => {
    if (!contextConflict) return
    setAttachedContext(contextConflict.restored || { type: 'page', page: 'agent' })
    setContextConflict(undefined)
  }
  const runTurn = async (turn: AgentTurn, retrying = false, continuation = false) => {
    const controller = new AbortController()
    const generation = generationRef.current
    controllerRef.current = controller; setLastTurn(turn); setTurnProgress(''); setInteractionError('')
    dispatch({ type: 'turn_started', requestId: turn.requestId, message: turn.message, context: turn.context, retry: retrying, continuation })
    let completed = false
    let streamFailed = false
    let contextSessionId = ''
    try {
      await streamAgentTurn(turn, controller.signal, event => {
        if (generation !== generationRef.current || streamFailed) return
        if (event.type === 'context') {
          if (!contextSessionId) contextSessionId = event.data.session_id
          setSessionId(event.data.session_id); localStorage.setItem(ACTIVE_SESSION_KEY, event.data.session_id)
        } else if (event.type === 'progress') {
          // 受理/阶段进度提示：临时状态行，首个 text/done/error 到达后清除。
          setTurnProgress(event.data.message)
        } else if (event.type === 'text') {
          setTurnProgress('')
          dispatch({ type: 'turn_text', requestId: turn.requestId, content: event.data.content })
        } else if (event.type === 'done') {
          setTurnProgress('')
          if (event.data.ok === false) {
            streamFailed = true
            dispatch({ type: 'turn_failed', requestId: turn.requestId, error: event.data.error || 'Agent 处理失败，请重试' })
            return
          }
          if (contextSessionId && event.data.session_id !== contextSessionId) {
            streamFailed = true
            dispatch({ type: 'turn_failed', requestId: turn.requestId, error: 'Agent 返回的会话与本轮不一致，已放弃写入' })
            return
          }
          completed = true
          setSessionId(event.data.session_id); setFocus(event.data.business_focus || null)
          dispatch({ type: 'turn_done', requestId: turn.requestId, result: event.data })
          localStorage.setItem(ACTIVE_SESSION_KEY, event.data.session_id)
          // 查询型名单直答：自动弹出完整名单弹窗（非消息内嵌卡）。
          if (event.data.action_card && (event.data.action_card as CandidateListCardData).type === 'candidate_list') {
            setCandidateListDialog(event.data.action_card as CandidateListCardData)
          }
        } else {
          setTurnProgress('')
          streamFailed = true
          dispatch({ type: 'turn_failed', requestId: turn.requestId, error: event.data.error })
        }
      })
      if (!completed && !streamFailed && !controller.signal.aborted) dispatch({ type: 'turn_failed', requestId: turn.requestId, error: 'Agent 响应提前结束，请重试' })
      if (completed) { void refreshSessions(); setLastTurn(undefined) }
    } catch (value) {
      if (generation !== generationRef.current) return
      if (controller.signal.aborted) dispatch({ type: 'turn_stopped', requestId: turn.requestId })
      else dispatch({ type: 'turn_failed', requestId: turn.requestId, error: value instanceof Error ? value.message : String(value) })
    } finally {
      if (controllerRef.current === controller) controllerRef.current = undefined
      if (generation === generationRef.current) setTurnProgress('')
    }
  }
  const sendMessage = (message: string) => {
    const readyAttachments = attachments.flatMap(item => item.state === 'ready' && item.attachment ? [item.attachment] : [])
    const uploadInProgress = attachments.some(item => item.state === 'uploading')
    const uploadFailed = attachments.some(item => item.state === 'error')
    if (!message || loading || restoring || uploadInProgress || uploadFailed) return
    if (isArchiveAllTasksCommand(message)) {
      setDraft(''); setBulkArchiveError(''); setBulkArchiveOpen(true); setArchiveConfirmId(''); setRenamingId('')
      return
    }
    const turnContext: AgentContext = bridgeContext
      ? { ...attachedContext, source: 'asa_floating', display_mode: 'workspace', bridge: bridgeContext }
      : attachedContext
    const contextWithAttachments = readyAttachments.length
      ? { ...turnContext, uploaded_attachments: readyAttachments }
      : turnContext
    attachmentGenerationRef.current += 1; attachmentsRef.current = []; setAttachmentNotice('')
    setDraft(''); setAttachments([]); void runTurn(createAgentTurn(sessionId, message, contextWithAttachments))
  }
  const submit = (event?: FormEvent) => {
    event?.preventDefault()
    const readyAttachments = attachments.flatMap(item => item.state === 'ready' && item.attachment ? [item.attachment] : [])
    const message = draft.trim() || (readyAttachments.length ? `请读取并分析附件：${readyAttachments.map(item => item.file_name).join('、')}` : '')
    sendMessage(message)
  }
  // 空态快捷指令：直接以指令文本发起会话，聚焦输入框便于即时微调。
  const runQuickCommand = (command: string) => {
    if (loading || restoring) return
    setDraft(command)
    sendMessage(command)
  }
  // 打开工作台自动聚焦输入框：首次挂载与会话恢复完成后各聚焦一次，之后不抢焦点。
  const composerFocusedRef = useRef(false)
  useEffect(() => {
    if (composerFocusedRef.current || restoring) return
    composerFocusedRef.current = true
    composerRef.current?.focus()
  }, [restoring])
  // 输入框随内容自动增高（上限 160px，超出滚动），发送清空后回落初始高度。
  useEffect(() => {
    const el = composerRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [draft])
  // Cmd/Ctrl+K 随时聚焦输入框（消息流滚动后快速回到输入）。
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        composerRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  // 消息复制：复制助手消息原文（markdown 原样），短时反馈"已复制"。
  const [copiedMessageKey, setCopiedMessageKey] = useState('')
  const copyMessage = async (key: string, content: string) => {
    try {
      await navigator.clipboard.writeText(content)
      setCopiedMessageKey(key)
      window.setTimeout(() => setCopiedMessageKey(current => current === key ? '' : current), 1600)
    } catch {
      // 剪贴板不可用时静默：复制是辅助能力，不打断对话。
    }
  }
  // 消息流滚动远离底部时显示"回到底部"。
  const messagesRef = useRef<HTMLDivElement>(null)
  const [showJumpBottom, setShowJumpBottom] = useState(false)
  useEffect(() => {
    const el = messagesRef.current
    if (!el) return
    const onScroll = () => setShowJumpBottom(el.scrollHeight - el.scrollTop - el.clientHeight > 240)
    onScroll()
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])
  const jumpToBottom = () => endRef.current?.scrollIntoView({ behavior: 'smooth' })
  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit() }
  }
  const retry = () => {
    if (!lastTurn || loading) return
    void runTurn(lastTurn.retry(), true)
  }
  const stop = () => {
    if (!conversation.activeRequestId) return
    controllerRef.current?.abort()
    dispatch({ type: 'turn_stopped', requestId: conversation.activeRequestId })
  }
  const selectAmbiguousObject = (card: Record<string, unknown>, option: Record<string, unknown>) => {
    const message = String(card.message || '').trim()
    const type = String(option.type || '').trim()
    const id = option.id
    if (!message || !type || id === undefined || id === null || loading) return
    const explicitContext: AgentContext = {
      type, id: typeof id === 'number' || typeof id === 'string' ? id : String(id), client: String(option.client || ''),
      ...(type === 'job' ? { job: String(option.label || '') } : { candidate: String(option.label || '') }),
      clarification_binding: true,
    }
    // 继续原始回合：对象绑定是澄清结果，不重复把原指令插入 transcript。
    void runTurn(createAgentTurn(sessionId, message, explicitContext), false, true)
  }
  const runStructuredAction = async (action: Record<string, unknown>) => {
    const type = String(action.type || '').trim()
    const target = action.target && typeof action.target === 'object' ? action.target as Record<string, unknown> : {}
    const id = action.id ?? target.id
    const actionId = String(action.action_id || `${type}:${String(id || '')}`)
    setInteractionError('')
    setStructuredActionBusy(actionId)
    try {
      if (['open_candidate', 'open_job', 'open_workflow'].includes(type) && id !== undefined && id !== null) {
        onOpenFullObject({ type: type.replace('open_', ''), id: String(id), label: String(action.label || action.title || '打开对象') })
        return
      }
      if (type === 'open_analysis' && id !== undefined && id !== null) { onOpenAnalysis(String(id)); return }
      if (type === 'start_workflow' && id !== undefined && id !== null) {
        // 写入/外部动作统一回到 Agent 预检链，按钮本身不直接启动工作流。
        const planRef = action.plan_ref && typeof action.plan_ref === 'object' ? action.plan_ref as Record<string, unknown> : {}
        const label = String(action.label || '开始执行本次任务')
        const continuation = createAgentTurn(sessionId, `请预检并确认：${label}`, {
          type: 'workflow', id: String(id), clarification_binding: true,
          plan_ref: planRef, constraints: action.constraints || [], confirmation_requested: true,
        })
        void runTurn(continuation, false)
        return
      }
      if (type === 'floating_action') {
        await api.floatingAction(String(id || action.action || ''), { target, constraints: action.constraints || [] })
        return
      }
      if (['view_a_candidates', 'compare_top_candidates', 'continue_sourcing', 'relax_search', 'generate_contact_queue', 'generate_contact_script', 'confirm_advance', 'confirm_stop', 'end_round'].includes(type)) {
        const label = String(action.label || '继续当前动作')
        const actionContext: AgentContext = {
          ...attachedContext, ...(target.type ? target : {}),
          structured_action: {
            action_id: actionId, capability_id: action.capability_id || type, type,
            target, constraints: action.constraints || [], risk_level: action.risk_level,
            preflight_required: action.preflight_required === true,
            confirmation_required: action.confirmation_required === true,
            post_check: action.post_check,
          },
        }
        await runTurn(createAgentTurn(sessionId, label, actionContext), false, true)
      }
    } catch (value) { setInteractionError(value instanceof Error ? value.message : String(value)) }
    finally { setStructuredActionBusy('') }
  }
  const copySessionId = async (id: string) => {
    if (!id || !navigator.clipboard?.writeText) return
    try {
      await navigator.clipboard.writeText(id)
    } catch {
      // 复制失败保持静默：任务右键菜单中的低频操作，无需打断用户。
    }
  }
  useEffect(() => {
    if (!taskMenu) return
    const closeOnEscape = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape') setTaskMenu(undefined) }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [taskMenu])
  const clearFocus = async () => {
    if (focusBusy) return
    const previousContext = attachedContext
    const previousFocus = focus
    setFocusBusy(true); setFocusError('')
    setAttachedContext({ type: 'page', page: 'agent' }); setFocus(null)
    try {
      if (sessionId) await api.updateAgentSession(sessionId, { clear_focus: true })
    } catch (value) {
      setAttachedContext(previousContext); setFocus(previousFocus)
      // 焦点操作失败不得清空消息列表：走独立的非破坏性错误状态。
      setFocusError(value instanceof Error ? value.message : String(value))
    } finally { setFocusBusy(false) }
  }
  const renameTask = async (event: FormEvent, item: AgentSessionSummary) => {
    event.preventDefault()
    const title = renameValue.trim()
    if (!title || taskBusy) return
    setTaskBusy(item.session_id); setTaskError('')
    try {
      const result = await api.updateAgentSession(item.session_id, { title })
      setSessions(value => value.map(task => task.session_id === item.session_id ? { ...task, title: result.title } : task))
      setRenamingId('')
    } catch (value) { setTaskError(value instanceof Error ? value.message : String(value)) }
    finally { setTaskBusy('') }
  }
  const archiveTask = async (item: AgentSessionSummary) => {
    setTaskBusy(item.session_id); setTaskError('')
    try {
      await api.updateAgentSession(item.session_id, { archived: true })
      setSessions(value => value.filter(task => task.session_id !== item.session_id))
      setArchiveConfirmId('')
      if (item.session_id === sessionId) {
        localStorage.removeItem(ACTIVE_SESSION_KEY)
        newTask()
      }
    } catch (value) { setTaskError(value instanceof Error ? value.message : String(value)) }
    finally { setTaskBusy('') }
  }
  const archiveAllTasks = async () => {
    if (bulkArchiveBusy) return
    setBulkArchiveBusy(true); setBulkArchiveError('')
    try {
      await api.archiveAllAgentSessions()
      setSessions([]); setTaskQuery(''); setBulkArchiveOpen(false)
      localStorage.removeItem(ACTIVE_SESSION_KEY)
      newTask()
    } catch (value) { setBulkArchiveError(value instanceof Error ? value.message : String(value)) }
    finally { setBulkArchiveBusy(false) }
  }
  // 名单卡刷新：重新按库内最新状态生成 candidate_list 卡片，同步更新消息流 + 弹窗。
  const refreshCandidateList = async (card: CandidateListCardData) => {
    const jobId = card.context?.type === 'job' ? Number(card.context.id) : 0
    if (!jobId || !Number.isFinite(jobId) || refreshingCardJob === jobId) return
    const bonder = Array.isArray(card.groups) && card.groups.some(group => group.key === 'bonder')
    setRefreshingCardJob(jobId); setCardRefreshError('')
    try {
      const result = await api.candidateListRefresh(jobId, bonder)
      dispatch({ type: 'card_refreshed', jobId, content: result.answer, action_card: result.card as Record<string, unknown> })
      setCandidateListDialog(prev => prev && prev.context?.type === 'job' && Number(prev.context.id) === jobId ? result.card : prev)
    } catch (value) {
      setCardRefreshError(value instanceof Error ? value.message : String(value))
    } finally {
      setRefreshingCardJob(null)
    }
  }
  // 焦点以服务端 business_focus 为准；仅服务端无焦点时才回落到本地附着上下文文案。
  const serverFocusLabel = focusLabel(focus)
  const currentFocus = serverFocusLabel
    || (attachedContext.type && attachedContext.type !== 'page' ? contextLabel(attachedContext) : '')
  const focusNeedsClarification = Boolean(focus?.needs_clarification || (Array.isArray(focus?.conflicts) && focus.conflicts.length))
  const trimmedQuery = taskQuery.trim()
  // 服务端搜索请求失败时回落到本地过滤；其余情况列表即服务端结果。
  const visibleSessions = trimmedQuery && searchUnavailable
    ? sessions.filter(item => `${item.title} ${item.preview}`.toLowerCase().includes(trimmedQuery.toLowerCase()))
    : sessions

  return <div className={`agent-workspace ${taskRailCollapsed ? 'rail-collapsed' : ''}`}>
    <section className={`agent-conversation ${currentFocus ? 'has-focus' : ''}`} aria-label="Agent 对话">
      <header className="agent-conversation-head"><div><MessageSquareText/><span><b>{sessions.find(item => item.session_id === sessionId)?.title || '新任务'}</b><small>{currentFocus || '通用 ASA 对话'}</small>{sessionId && <small className="agent-session-id">会话 ID：{sessionId}</small>}</span></div><div><button className="icon-btn" title="模型输出审计" aria-label="模型输出审计" onClick={() => setModelAuditOpen(value => !value)}><Activity/></button><button className="icon-btn agent-history-toggle" title="任务历史" aria-label="任务历史" onClick={() => { setHistoryOpen(true); setRailCollapsed(false) }}><PanelRightOpen/></button><button className="button" onClick={newTask}><Plus/>新任务</button></div></header>
      {contextConflict && <div className="agent-context-conflict" role="alert"><span>你正带着新的业务上下文进入，当前任务焦点为 {contextConflict.label}</span><div><button className="button primary" onClick={resolveConflictWithNewTask}>以新上下文新建任务</button><button className="button" onClick={resolveConflictKeepCurrent}>继续当前任务</button></div></div>}
      {currentFocus && <div className={`agent-focus-bar ${focusNeedsClarification ? 'conflict' : ''}`} role="status" aria-label="当前任务焦点"><span><b>{focusNeedsClarification ? '焦点需要确认' : '当前焦点'}</b>{currentFocus}</span><button className="icon-btn" title="解除任务焦点" aria-label="解除任务焦点" disabled={focusBusy} onClick={() => void clearFocus()}><Unlink/></button></div>}
      {focusError && <div className="agent-error"><span>{focusError}</span></div>}
      <div ref={messagesRef} className="agent-messages">
        {historyTruncated && <p className="agent-truncated">仅显示最近 100 条消息</p>}
        {!messages.length && !restoring && <AgentHome jobs={jobs} workbench={workbench} templates={templates} onAction={onWorkbenchAction} onOpenAnalysis={onOpenAnalysis} onRunTemplate={onRunTemplate} onManageTemplate={onManageTemplate} onCreateTemplate={onCreateTemplate} onQuickCommand={runQuickCommand} />}
        {restoring && <div className="agent-loading"><LoaderCircle className="spin"/>恢复任务</div>}
        {messages.map((message, index) => {
          const messageKey = `${index}:${message.created_at || ''}`
          const dayLabel = dayLabelFor(message.created_at, messages[index - 1]?.created_at)
          return <Fragment key={messageKey}>
            {dayLabel && <div className="agent-day-divider" role="separator"><span>{dayLabel}</span></div>}
            <div className={`agent-message ${message.role} ${message.invalidated ? 'is-invalidated' : ''}`}>
          <span className="agent-message-role">{message.role === 'user' ? '你' : 'ASA'}</span>{message.created_at && <time className="agent-message-time" dateTime={message.created_at}>{formatMessageTime(message.created_at)}</time>}{message.role === 'assistant' && message.content && <button className={`agent-message-copy ${copiedMessageKey === `${index}:${message.created_at || ''}` ? 'copied' : ''}`} aria-label="复制消息内容" title="复制消息内容" onClick={() => void copyMessage(`${index}:${message.created_at || ''}`, message.content)}>{copiedMessageKey === `${index}:${message.created_at || ''}` ? <span>已复制</span> : <ClipboardCopy size={11}/>}</button>}<div className="agent-message-content">{message.role === 'assistant' && message.model_participation && <small className={`agent-model-participation ${String(message.model_participation.mode || 'rules')}`} title={String(message.model_participation.model || '')}>{String(message.model_participation.label || '规则生成')}</small>}{message.content
            ? <AgentMessageContent content={message.content}/>
            : loading && index === messages.length - 1
              ? <AgentThinking label={turnProgress ? `正在处理：${turnProgress}` : 'ASA 正在思考'}/>
              : null}</div>
          {message.role === 'user' && uploadedAttachments(message.context).length > 0 && <div className="agent-message-attachments" aria-label="消息附件">{uploadedAttachments(message.context).map((item, attachmentIndex) => <span key={`${item.attachment_id || item.file_name}:${attachmentIndex}`}><FileText/><b>{item.file_name || '附件'}</b><small>{item.status || '已读取'}</small></span>)}</div>}
          {message.invalidated && <p className="agent-invalidated-notice">本卡已因后续纠正失效{message.invalidated_reason ? `：${message.invalidated_reason}` : ''}</p>}
          {message.role === 'assistant' && !message.invalidated && <UnderstandingCard card={message.understanding_card} onSelectCandidate={option => selectAmbiguousObject(message.understanding_card || {}, option)} onReenter={() => composerRef.current?.focus()}/>}
          {message.role === 'assistant' && !message.invalidated && <CandidateIntentConfirmation intent={message.pending_intent} sessionId={sessionId}/>}
          {message.role === 'assistant' && !message.invalidated && !message.pending_intent && <SuggestedActionBar actions={message.suggested_actions} busy={structuredActionBusy} onAction={action => void runStructuredAction(action)}/>}
          {message.role === 'assistant' && <ExecutionReceipt receipt={message.execution_receipt}/>}
          {message.role === 'assistant' && interactionError && index === messages.length - 1 && <p className="agent-card-error" role="alert">{interactionError}</p>}
          {message.role === 'assistant' && message.action_card && (message.action_card as SourcingResultCardData).type === 'sourcing_result' && (
            <SourcingResultCard
              data={message.action_card as SourcingResultCardData}
              compact
              onOpenCandidate={jobCandidateId => {
                const candidate = (message.action_card as SourcingResultCardData).summary?.top_candidates?.find(c => c.job_candidate_id === jobCandidateId)
                onOpenFullObject({ type: 'candidate', id: jobCandidateId, label: candidate?.name || '人选' })
              }}
              onAction={(actionType, context)=>{
                if(actionType==='review_candidates' && context?.type==='workflow'){
                  onOpenFullObject({type:'workflow',id:context.id,label:'寻访结果'})
                } else if(actionType==='archive' && context?.type==='workflow') {
                  void (async () => {
                    try {
                      setStructuredActionBusy(`archive:${context.id}`)
                      await api.workflowAction(String(context.id), 'archive')
                      await api.workflow(String(context.id))
                    } catch (value) { setInteractionError(value instanceof Error ? value.message : String(value)) }
                    finally { setStructuredActionBusy('') }
                  })()
                } else if(actionType==='discuss_strategy' || actionType==='continue_sourcing'){
                  const workflowId = (message.action_card as SourcingResultCardData).summary?.workflow_id
                  if(workflowId) window.dispatchEvent(new CustomEvent('asa:open-agent',{detail:{type:'workflow',id:workflowId,mode:'strategy_revision'}}))
                }
              }}
            />
          )}
          {message.role === 'assistant' && message.action_card && (message.action_card as CandidateListCardData).type === 'candidate_list' && (
            <div className="candidate-list-trigger-row">
              <button className="candidate-list-trigger" onClick={() => setCandidateListDialog(message.action_card as CandidateListCardData)}>
                <Users size={14} />
                <span>查看完整名单（{(message.action_card as CandidateListCardData).summary?.total ?? ''} 人）</span>
              </button>
              <button
                className="candidate-list-refresh"
                onClick={() => void refreshCandidateList(message.action_card as CandidateListCardData)}
                disabled={refreshingCardJob === Number((message.action_card as CandidateListCardData).context?.id)}
                title="重新按库内最新状态生成名单"
              >
                {refreshingCardJob === Number((message.action_card as CandidateListCardData).context?.id) ? <LoaderCircle className="spin" size={14}/> : <RefreshCw size={14}/>}
                <span>{refreshingCardJob === Number((message.action_card as CandidateListCardData).context?.id) ? '刷新中' : '刷新'}</span>
              </button>
            </div>
          )}
          {message.role === 'assistant' && !['sourcing_result', 'candidate_list'].includes(String((message.action_card as SourcingResultCardData | undefined)?.type)) && messageReferences(message).map(reference => <AgentObjectEmbed key={`${reference.type}:${reference.id}`} reference={reference} workflowProgress={reference.type === 'workflow' ? message.workflow_progress : undefined} actionCard={reference.type === 'workflow' ? message.action_card : undefined} onOpenFull={onOpenFullObject} />)}
          </div>
          </Fragment>
        })}
        {(error || (phase === 'stopped' && lastTurn)) && <div className="agent-error"><span>{error || '已停止生成'}</span>{lastTurn && <button className="button" onClick={retry}>重试</button>}</div>}
        <div ref={endRef}/>
        {showJumpBottom && <button className="agent-jump-bottom" aria-label="回到底部" title="回到底部" onClick={jumpToBottom}><ChevronDown size={13}/><span>回到底部</span></button>}
      </div>
      <div className="agent-composer-stack">
        <AgentPageContextBar onOpenFullObject={onOpenFullObject} onBridgeContextChange={setBridgeContext}/>
        <form className={`agent-composer ${attachmentDragActive ? 'drag-active' : ''}`} onSubmit={submit} onDragEnter={event => { event.preventDefault(); setAttachmentDragActive(true) }} onDragOver={event => event.preventDefault()} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setAttachmentDragActive(false) }} onDrop={onAttachmentDrop}>
          {attachments.length > 0 && <div className="agent-attachment-list" aria-label="待发送附件" role="list" aria-live="polite">{attachments.map(item => <span className={item.state} key={item.key} role="listitem"><FileText/><span><b>{item.fileName}</b><small role={item.state === 'error' ? 'alert' : undefined}>{item.state === 'uploading' ? '正在读取…' : item.state === 'ready' ? `${formatAttachmentSize(item.sizeBytes)} · ${item.attachment?.status || '已读取'}` : item.error}</small></span><button type="button" className="icon-btn" aria-label={`移除附件：${item.fileName}`} title="移除附件" onClick={() => replaceAttachments(current => current.filter(attachment => attachment.key !== item.key))}>{item.state === 'uploading' ? <LoaderCircle className="spin"/> : <X/>}</button></span>)}</div>}
          {attachmentNotice && <p className="agent-attachment-notice" role="alert">{attachmentNotice}</p>}
          <input ref={attachmentInputRef} className="agent-attachment-input" type="file" multiple accept={AGENT_ATTACHMENT_ACCEPT} aria-label="选择附件" onChange={event => { addAttachments(Array.from(event.target.files || [])); event.target.value = '' }}/>
          <button className="icon-btn agent-attach" type="button" disabled={restoring || loading || attachments.length >= AGENT_ATTACHMENT_MAX_COUNT} aria-label="添加附件" title="添加附件" onClick={() => attachmentInputRef.current?.click()}><Paperclip/></button>
          <textarea ref={composerRef} rows={1} value={draft} onChange={event => setDraft(event.target.value)} onPaste={onAttachmentPaste} onKeyDown={keyDown} disabled={restoring} placeholder="告诉 ASA 你要推进的目标…（或点上方快捷指令）" aria-label="Agent 消息"/>
          <button className="icon-btn agent-send" type={loading ? 'button' : 'submit'} disabled={restoring || (!loading && (attachments.some(item => item.state !== 'ready') || (!draft.trim() && !attachments.some(item => item.state === 'ready'))))} onClick={loading ? stop : undefined} aria-label={loading ? '停止生成' : '发送'} title={loading ? '停止生成' : '发送'}>{loading ? <Square/> : <Send/>}</button>
        </form>
      </div>
      <ModelAuditPanel open={modelAuditOpen} onClose={() => setModelAuditOpen(false)}/>
    </section>
    <aside className={`agent-task-rail ${historyOpen ? 'open' : ''} ${taskRailCollapsed ? 'collapsed' : ''}`} aria-label="任务历史">{taskRailCollapsed ? <span className="agent-rail-collapsed-wrap">{sessions.length > 0 && <span className="agent-rail-badge" aria-label={`${sessions.length} 个任务`}>{sessions.length > 99 ? '99+' : sessions.length}</span>}<button className="icon-btn agent-rail-reopen" title="显示任务栏" aria-label="显示任务栏" onClick={() => setRailCollapsed(false)}><PanelRightOpen/></button></span> : <><header><div><History/><b>任务{sessions.length > 0 ? `（${sessions.length}）` : ''}</b></div><div><button className="icon-btn" title="归档全部任务" aria-label="归档全部任务" onClick={() => { setBulkArchiveError(''); setBulkArchiveOpen(true); setArchiveConfirmId(''); setRenamingId('') }}><Archive/></button><button className="icon-btn agent-rail-collapse" title="隐藏任务栏" aria-label="隐藏任务栏" onClick={() => setRailCollapsed(true)}><PanelRightClose/></button><button className="icon-btn agent-history-close" aria-label="关闭任务历史" onClick={() => setHistoryOpen(false)}><X/></button></div></header><label className="agent-task-search"><Search/><input aria-label="搜索任务" value={taskQuery} onChange={event => setTaskQuery(event.target.value)} placeholder="搜索任务"/></label>{taskError && <p className="agent-task-error">{taskError}</p>}<div>{visibleSessions.map(item => <article key={item.session_id} className={item.session_id === sessionId ? 'active' : ''} onContextMenu={event => { if (renamingId !== item.session_id) { event.preventDefault(); const menuWidth = 224; const menuHeight = 112; setTaskMenu({ sessionId: item.session_id, x: Math.max(4, Math.min(event.clientX, window.innerWidth - menuWidth)), y: Math.max(4, Math.min(event.clientY, window.innerHeight - menuHeight)) }) } }}>
      {renamingId === item.session_id ? <form aria-label="重命名任务" onSubmit={event => void renameTask(event, item)}><input aria-label="任务名称" value={renameValue} onChange={event => setRenameValue(event.target.value)} autoFocus/><button className="button" type="submit" disabled={taskBusy === item.session_id}>保存</button></form> : <button className="agent-task-main" onClick={() => void restoreSession(item.session_id)}><b>{item.title}</b><span>最近：{item.preview}</span><small>{item.message_count} 条消息{relativeTime(item.updated_at) && ` · ${relativeTime(item.updated_at)}`}</small></button>}
      <div className="agent-task-actions"><button className="icon-btn" disabled={!!taskBusy} aria-label={`重命名任务：${item.title}`} title="重命名任务" onClick={() => { setRenamingId(item.session_id); setRenameValue(item.title); setArchiveConfirmId(''); setTaskError('') }}><Pencil/></button>{archiveConfirmId === item.session_id ? <button className="button danger" disabled={taskBusy === item.session_id} aria-label={`确认归档任务：${item.title}`} onClick={() => void archiveTask(item)}>确认归档</button> : <button className="icon-btn" disabled={!!taskBusy} aria-label={`归档任务：${item.title}`} title="归档任务" onClick={() => { setArchiveConfirmId(item.session_id); setRenamingId(''); setTaskError('') }}><Archive/></button>}</div>
    </article>)}{!visibleSessions.length && <p>{taskQuery ? '没有匹配任务' : '暂无历史任务'}</p>}</div></>}</aside>
    {taskMenu && <div className="agent-task-menu-backdrop" role="presentation" onClick={() => setTaskMenu(undefined)} onContextMenu={event => { event.preventDefault(); setTaskMenu(undefined) }}>
      <div className="agent-task-menu" role="menu" aria-label="任务操作" style={{ left: taskMenu.x, top: taskMenu.y }}>
        <button role="menuitem" aria-label={`复制会话 ID：${taskMenu.sessionId}`} onClick={() => { void copySessionId(taskMenu.sessionId); setTaskMenu(undefined) }}><ClipboardCopy/><span>复制会话 ID<small>{taskMenu.sessionId}</small></span></button>
      </div>
    </div>}
    {bulkArchiveOpen && <div className="action-dialog-backdrop" role="presentation" onClick={() => { if (!bulkArchiveBusy) setBulkArchiveOpen(false) }}><section ref={bulkArchiveDialogRef} className="action-dialog agent-bulk-archive-dialog" role="alertdialog" aria-modal="true" aria-labelledby="bulk-archive-title" onClick={event => event.stopPropagation()}>
      <header><span className="action-dialog-icon danger"><Archive/></span><div><small>任务管理</small><h3 id="bulk-archive-title">归档全部任务</h3></div><button className="icon-btn" aria-label="关闭" disabled={bulkArchiveBusy} onClick={() => setBulkArchiveOpen(false)}><X/></button></header>
      <div className="action-dialog-body"><dl><div><dt>范围</dt><dd>全部未归档任务</dd></div><div><dt>归档后</dt><dd>从右侧任务栏隐藏</dd></div><div><dt>保留内容</dt><dd>消息、业务焦点与审计记录</dd></div></dl><p>这是批量操作。归档后任务不会被删除，后端记录仍会完整保留。</p>{bulkArchiveError && <div className="action-dialog-error">{bulkArchiveError}</div>}</div>
      <footer><button className="button" disabled={bulkArchiveBusy} onClick={() => setBulkArchiveOpen(false)}>取消</button><button className="button danger-fill" disabled={bulkArchiveBusy} onClick={() => void archiveAllTasks()}>{bulkArchiveBusy ? '正在归档' : '确认全部归档'}</button></footer>
    </section></div>}
    {candidateListDialog && (
      <CandidateListDialog
        data={candidateListDialog}
        onOpenCandidate={jobCandidateId => {
          const candidate = (candidateListDialog.groups || []).flatMap(group => group.candidates || []).find(item => item.id === jobCandidateId)
          // 暂存名单，详情关闭后恢复（见 candidateListStashRef）。
          candidateListStashRef.current = candidateListDialog
          setCandidateListDialog(null)
          onOpenFullObject({ type: 'candidate', id: jobCandidateId, label: candidate?.name || '人选' })
        }}
        onOpenJob={jobId => {
          // 岗位详情同样是 overlay，同样暂存后关闭。
          candidateListStashRef.current = candidateListDialog
          setCandidateListDialog(null)
          onOpenFullObject({ type: 'job', id: jobId, label: candidateListDialog.title })
        }}
        onClose={() => setCandidateListDialog(null)}
        onRefresh={() => void refreshCandidateList(candidateListDialog)}
        refreshing={refreshingCardJob === Number(candidateListDialog.context?.id)}
      />
    )}
    {cardRefreshError && <div className="agent-error" role="alert"><span>名单刷新失败：{cardRefreshError}</span><button className="button" onClick={() => setCardRefreshError('')}>关闭</button></div>}
  </div>
}

const agentHomeLanes: Array<{ lane: WorkbenchLane; label: string; empty: string }> = [
  { lane: 'decision', label: '待判断', empty: '当前没有待判断事项' },
  { lane: 'running', label: '运行中', empty: '当前没有运行中任务' },
  { lane: 'waiting_client', label: '待客户', empty: '当前没有待客户反馈的人选对' },
  { lane: 'risk', label: '风险/逾期', empty: '当前没有风险或逾期事项' },
  { lane: 'delivered', label: '最近交付', empty: '最近还没有交付物' },
]

// 空态快捷指令：一句话触发 Copilot 常用能力，点击即以该指令发起新会话。
const agentHomeQuickCommands: Array<{ label: string; command: string; icon: React.ReactNode }> = [
  { label: '今日待办', command: '今天有哪些待办？', icon: <ListChecks/> },
  { label: '过滤名单', command: '过滤候选人名单', icon: <Filter/> },
  { label: '人才地图', command: '查一下人才地图', icon: <Map/> },
  { label: '薪酬对标', command: '薪酬对标一下', icon: <Banknote/> },
  { label: '推荐报告', command: '生成推荐报告', icon: <FileText/> },
  { label: '继续寻访', command: '继续寻访', icon: <RefreshCw/> },
]

function AgentHomeLane({ lane, label, empty, workbench, loading, onAction }: {
  lane: WorkbenchLane; label: string; empty: string; workbench: Workbench; loading: boolean;
  onAction: (item: WorkbenchItem) => void;
}) {
  const [visibleLimit, setVisibleLimit] = useState(4)
  const [collapsed, setCollapsed] = useState(false)
  const items = workbench.items.filter(item => item.lane === lane)
  const visibleItems = items.slice(0, visibleLimit)
  const total = workbenchLaneCount(workbench, lane)
  // 服务端序列化窗口有上限（analytics.workbench 统一封顶 300）；总数以 summary 为准，
  // 超出窗口时由"共 N 项 + 再显示"分页自然承接，不再展示调试式加载窗口细节。
  const summaryLabel = loading ? '加载中…' : `共 ${total} 项`
  const hasMore = visibleItems.length < items.length
  const nextBatchSize = Math.min(20, items.length - visibleItems.length)
  return <section className={`agent-home-band ${collapsed ? 'lane-collapsed' : ''}`} aria-label={label}><header><button className="agent-home-lane-toggle" aria-label={`${collapsed ? '展开' : '收起'}${label}`} aria-expanded={!collapsed} onClick={() => setCollapsed(value => !value)}><ChevronDown size={12}/><h3>{label}</h3></button><span>{summaryLabel}</span></header>{!collapsed && <>{visibleItems.map(item => <button key={item.item_key} onClick={() => onAction(item)}><span><b>{item.title}</b><small>{item.reason || item.subtitle}</small></span><em>{item.status_label}</em></button>)}{(hasMore || visibleLimit > 4) && <div className="agent-home-pagination">{hasMore && <button className="agent-home-more" onClick={() => setVisibleLimit(value => Math.min(items.length, value + 20))}>再显示 {nextBatchSize} 项</button>}{visibleLimit > 4 && <button className="agent-home-collapse" onClick={() => setVisibleLimit(4)}>收起</button>}</div>}{!items.length && <p>{loading ? '正在加载…' : empty}</p>}</>}</section>
}

function AgentHome({ jobs, workbench, templates, onAction, onOpenAnalysis, onRunTemplate, onManageTemplate, onCreateTemplate, onQuickCommand }: {
  jobs: Job[]; workbench: Workbench; templates: AnalysisTemplate[]; onAction: (item: WorkbenchItem) => void;
  onOpenAnalysis: (id: string) => void; onRunTemplate: (id: string) => void;
  onManageTemplate: (template: AnalysisTemplate) => void; onCreateTemplate: () => void;
  onQuickCommand: (command: string) => void;
}) {
  const [radarOpen, setRadarOpen] = useState(false)
  const [knowledgeOpen, setKnowledgeOpen] = useState(false)
  const [calibrationOpen, setCalibrationOpen] = useState(false)
  // App 层在首次成功拉取前注入 version='loading' 占位；此时未知是否为空，不得渲染成“没有待办”。
  const workbenchLoading = workbench.version === 'loading'
  // 空 lane 不占位渲染：加载完成后仅展示有真实待办的板块；计数以 summary 为准（不受 300 窗口截断影响）。
  const visibleLanes = workbenchLoading
    ? agentHomeLanes
    : agentHomeLanes.filter(config => workbenchLaneCount(workbench, config.lane) > 0)
  return <div className="agent-home"><header><h2>今天从哪里开始？</h2><p>ASA 已连接岗位、人选和工作流上下文。</p></header><section className="agent-home-quick" aria-label="快捷指令">{agentHomeQuickCommands.map(item => <button key={item.command} title={item.command} onClick={() => onQuickCommand(item.command)}>{item.icon}<b>{item.label}</b><small>{item.command}</small></button>)}</section>{visibleLanes.map(config => <AgentHomeLane key={config.lane} lane={config.lane} label={config.label} empty={config.empty} workbench={workbench} loading={workbenchLoading} onAction={onAction} />)}<section className="agent-home-band agent-home-analyses"><header><h3>固定分析</h3><button className="icon-btn" title="新建固定分析" aria-label="新建固定分析" onClick={onCreateTemplate}><Plus/></button></header>{templates.slice(0, 4).map(item => <div className="agent-analysis-row" key={item.template_id}><button onClick={() => item.last_run_id ? onOpenAnalysis(item.last_run_id) : onRunTemplate(item.template_id)}><span><b>{item.name}</b><small>{item.last_result?.headline || item.question || '尚未运行'}</small></span><em>{item.last_run_id ? '查看' : '运行'}</em></button><button className="icon-btn" title={`管理固定分析：${item.name}`} aria-label={`管理固定分析：${item.name}`} onClick={() => onManageTemplate(item)}><Settings2/></button></div>)}{!templates.length && <p>暂无固定分析</p>}</section><section className="agent-home-tools"><button className="button" aria-expanded={radarOpen} onClick={() => setRadarOpen(value => !value)}><Radar/>{radarOpen ? '收起人才雷达' : '人才雷达'}</button><button className="button" aria-expanded={knowledgeOpen} onClick={() => setKnowledgeOpen(value => !value)}><BookPlus/>{knowledgeOpen ? '收起知识增补提案' : '知识增补提案'}</button><button className="button" aria-expanded={calibrationOpen} onClick={() => setCalibrationOpen(value => !value)}><Building2/>{calibrationOpen ? '收起核心公司校准' : '核心公司校准'}</button></section>{radarOpen && <section className="agent-home-radar" aria-label="人才雷达"><Suspense fallback={<div className="empty"><LoaderCircle className="spin"/>人才雷达加载中…</div>}><RadarPage jobs={jobs}/></Suspense></section>}{knowledgeOpen && <section className="agent-home-radar" aria-label="知识增补提案"><Suspense fallback={<div className="empty"><LoaderCircle className="spin"/>知识增补提案加载中…</div>}><KnowledgeProposalsPanel/></Suspense></section>}{calibrationOpen && <section className="agent-home-radar" aria-label="核心公司校准"><Suspense fallback={<div className="empty"><LoaderCircle className="spin"/>核心公司校准加载中…</div>}><CompanyCalibrationPanel/></Suspense></section>}</div>
}
