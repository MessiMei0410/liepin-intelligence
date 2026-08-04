import { FormEvent, KeyboardEvent, useEffect, useReducer, useRef, useState } from 'react'
import { Archive, History, LoaderCircle, MessageSquareText, PanelRightOpen, Pencil, Plus, Search, Send, Settings2, Square, Unlink, X } from 'lucide-react'
import { api, AgentMessage, AgentSessionSummary, AnalysisTemplate, Dashboard, Workbench, WorkbenchItem } from '../api'
import { AgentObjectEmbed } from './AgentObjectEmbed'
import { agentConversationReducer, initialAgentConversationState } from './conversationState'
import { AgentContext, AgentReference, AgentTurn, createAgentTurn, streamAgentTurn } from './transport'

const ACTIVE_SESSION_KEY = 'asaAgentSessionId'
// 与 api.agentSession 默认 limit 对齐：拉满即视为历史被截断。
const SESSION_MESSAGE_LIMIT = 100

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
  const refs = [...(message.references || [])]
  for (const action of message.suggested_actions || []) {
    const type = String(action.type || '')
    if (!['open_candidate', 'open_job', 'open_workflow'].includes(type) || action.id === undefined) continue
    const refType = type.replace('open_', '')
    if (!refs.some(item => item.type === refType && String(item.id) === String(action.id))) refs.push({ type: refType, id: String(action.id), label: String(action.label || action.title || '打开对象') })
  }
  const card = message.action_card || {}
  const cardContext = card.context && typeof card.context === 'object' ? card.context as Record<string, unknown> : {}
  if (cardContext.type && cardContext.id !== undefined && !refs.some(item => item.type === cardContext.type && String(item.id) === String(cardContext.id))) {
    refs.push({ type: String(cardContext.type), id: String(cardContext.id), label: String(card.title || card.label || '相关对象') })
  }
  if (message.workflow_id && !refs.some(item => item.type === 'workflow' && String(item.id) === message.workflow_id)) {
    refs.push({ type: 'workflow', id: message.workflow_id, label: String(card.title || '工作流进度') })
  }
  return refs
}

export function AgentWorkspace({ dashboard, workbench, templates, context, onOpenAnalysis, onRunTemplate, onManageTemplate, onCreateTemplate, onWorkbenchAction, onOpenFullObject }: {
  dashboard?: Dashboard; workbench: Workbench; templates: AnalysisTemplate[]; context: AgentContext;
  onOpenAnalysis: (id: string) => void; onRunTemplate: (id: string) => void;
  onManageTemplate: (template: AnalysisTemplate) => void; onCreateTemplate: () => void;
  onWorkbenchAction: (item: WorkbenchItem) => void; onOpenFullObject: (reference: AgentReference) => void;
}) {
  const [sessions, setSessions] = useState<AgentSessionSummary[]>([])
  const [sessionId, setSessionId] = useState(() => localStorage.getItem(ACTIVE_SESSION_KEY) || '')
  const [conversation, dispatch] = useReducer(agentConversationReducer, initialAgentConversationState)
  const [focus, setFocus] = useState<Record<string, unknown> | null>(null)
  const [attachedContext, setAttachedContext] = useState<AgentContext>(context)
  const [draft, setDraft] = useState('')
  const [lastTurn, setLastTurn] = useState<AgentTurn>()
  const [historyOpen, setHistoryOpen] = useState(false)
  const [taskQuery, setTaskQuery] = useState('')
  const [renamingId, setRenamingId] = useState('')
  const [renameValue, setRenameValue] = useState('')
  const [archiveConfirmId, setArchiveConfirmId] = useState('')
  const [taskBusy, setTaskBusy] = useState('')
  const [taskError, setTaskError] = useState('')
  const [focusError, setFocusError] = useState('')
  const [focusBusy, setFocusBusy] = useState(false)
  const [turnProgress, setTurnProgress] = useState('')
  const [historyTruncated, setHistoryTruncated] = useState(false)
  const [searchUnavailable, setSearchUnavailable] = useState(false)
  const [contextConflict, setContextConflict] = useState<{ incoming: AgentContext; restored?: AgentContext; label: string }>()
  const controllerRef = useRef<AbortController | undefined>(undefined)
  const generationRef = useRef(0)
  const searchedRef = useRef(false)
  const searchSeqRef = useRef(0)
  const attachedContextRef = useRef(attachedContext)
  const endRef = useRef<HTMLDivElement>(null)
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
    void refreshSessions()
    if (sessionId) void restoreSession(sessionId)
  }, [])
  useEffect(() => {
    if (context.type && context.type !== 'page') setAttachedContext(context)
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
    if (typeof endRef.current?.scrollIntoView === 'function') endRef.current.scrollIntoView({ block: 'end' })
  }, [messages, loading])
  useEffect(() => () => controllerRef.current?.abort(), [])

  const startTaskWithContext = (next: AgentContext) => {
    controllerRef.current?.abort(); generationRef.current += 1; setSessionId(''); dispatch({ type: 'task_reset' }); setFocus(null); setDraft(''); setLastTurn(undefined)
    setAttachedContext(next); setContextConflict(undefined); setFocusError(''); setTurnProgress(''); setHistoryTruncated(false)
    localStorage.removeItem(ACTIVE_SESSION_KEY); setHistoryOpen(false)
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
  const runTurn = async (turn: AgentTurn, retrying = false) => {
    const controller = new AbortController()
    const generation = generationRef.current
    controllerRef.current = controller; setLastTurn(turn); setTurnProgress('')
    dispatch({ type: 'turn_started', requestId: turn.requestId, message: turn.message, context: turn.context, retry: retrying })
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
  const submit = (event?: FormEvent) => {
    event?.preventDefault()
    const message = draft.trim()
    if (!message || loading || restoring) return
    setDraft(''); void runTurn(createAgentTurn(sessionId, message, attachedContext))
  }
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
      if (item.session_id === sessionId) newTask()
    } catch (value) { setTaskError(value instanceof Error ? value.message : String(value)) }
    finally { setTaskBusy('') }
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

  return <div className="agent-workspace">
    <section className={`agent-conversation ${currentFocus ? 'has-focus' : ''}`} aria-label="Agent 对话">
      <header className="agent-conversation-head"><div><MessageSquareText/><span><b>{sessions.find(item => item.session_id === sessionId)?.title || '新任务'}</b><small>{currentFocus || '通用 ASA 对话'}</small></span></div><div><button className="icon-btn agent-history-toggle" title="任务历史" aria-label="任务历史" onClick={() => setHistoryOpen(true)}><PanelRightOpen/></button><button className="button" onClick={newTask}><Plus/>新任务</button></div></header>
      {contextConflict && <div className="agent-context-conflict" role="alert"><span>你正带着新的业务上下文进入，当前任务焦点为 {contextConflict.label}</span><div><button className="button primary" onClick={resolveConflictWithNewTask}>以新上下文新建任务</button><button className="button" onClick={resolveConflictKeepCurrent}>继续当前任务</button></div></div>}
      {currentFocus && <div className={`agent-focus-bar ${focusNeedsClarification ? 'conflict' : ''}`} role="status" aria-label="当前任务焦点"><span><b>{focusNeedsClarification ? '焦点需要确认' : '当前焦点'}</b>{currentFocus}</span><button className="icon-btn" title="解除任务焦点" aria-label="解除任务焦点" disabled={focusBusy} onClick={() => void clearFocus()}><Unlink/></button></div>}
      {focusError && <div className="agent-error"><span>{focusError}</span></div>}
      <div className="agent-messages">
        {historyTruncated && <p className="agent-truncated">仅显示最近 100 条消息</p>}
        {!messages.length && !restoring && <AgentHome dashboard={dashboard} workbench={workbench} templates={templates} onAction={onWorkbenchAction} onOpenAnalysis={onOpenAnalysis} onRunTemplate={onRunTemplate} onManageTemplate={onManageTemplate} onCreateTemplate={onCreateTemplate} />}
        {restoring && <div className="agent-loading"><LoaderCircle className="spin"/>恢复任务</div>}
        {messages.map((message, index) => <div className={`agent-message ${message.role}`} key={`${index}:${message.created_at || ''}`}>
          <span className="agent-message-role">{message.role === 'user' ? '你' : 'ASA'}</span><div className="agent-message-content">{message.content || (loading && index === messages.length - 1 ? <LoaderCircle className="spin"/> : null)}</div>
          {message.role === 'assistant' && messageReferences(message).map(reference => <AgentObjectEmbed key={`${reference.type}:${reference.id}`} reference={reference} onOpenFull={onOpenFullObject} />)}
        </div>)}
        {turnProgress && <div className="agent-progress" role="status"><LoaderCircle className="spin"/><span>正在处理：{turnProgress}</span></div>}
        {(error || (phase === 'stopped' && lastTurn)) && <div className="agent-error"><span>{error || '已停止生成'}</span>{lastTurn && <button className="button" onClick={retry}>重试</button>}</div>}
        <div ref={endRef}/>
      </div>
      <form className="agent-composer" onSubmit={submit}><textarea rows={1} value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={keyDown} disabled={restoring} placeholder="告诉 ASA 你要推进的目标..." aria-label="Agent 消息"/><button className="icon-btn agent-send" type={loading ? 'button' : 'submit'} disabled={restoring} onClick={loading ? stop : undefined} aria-label={loading ? '停止生成' : '发送'} title={loading ? '停止生成' : '发送'}>{loading ? <Square/> : <Send/>}</button></form>
    </section>
    <aside className={`agent-task-rail ${historyOpen ? 'open' : ''}`} aria-label="任务历史"><header><div><History/><b>任务</b></div><button className="icon-btn agent-history-close" aria-label="关闭任务历史" onClick={() => setHistoryOpen(false)}><X/></button></header><button className="button primary agent-new-task" aria-label="在任务栏新建任务" onClick={newTask}><Plus/>新任务</button><label className="agent-task-search"><Search/><input aria-label="搜索任务" value={taskQuery} onChange={event => setTaskQuery(event.target.value)} placeholder="搜索任务"/></label>{taskError && <p className="agent-task-error">{taskError}</p>}<div>{visibleSessions.map(item => <article key={item.session_id} className={item.session_id === sessionId ? 'active' : ''}>
      {renamingId === item.session_id ? <form aria-label="重命名任务" onSubmit={event => void renameTask(event, item)}><input aria-label="任务名称" value={renameValue} onChange={event => setRenameValue(event.target.value)} autoFocus/><button className="button" type="submit" disabled={taskBusy === item.session_id}>保存</button></form> : <button className="agent-task-main" onClick={() => void restoreSession(item.session_id)}><b>{item.title}</b><span>最近：{item.preview}</span><small>{item.message_count} 条消息</small></button>}
      <div className="agent-task-actions"><button className="icon-btn" disabled={!!taskBusy} aria-label={`重命名任务：${item.title}`} title="重命名任务" onClick={() => { setRenamingId(item.session_id); setRenameValue(item.title); setArchiveConfirmId(''); setTaskError('') }}><Pencil/></button>{archiveConfirmId === item.session_id ? <button className="button danger" disabled={taskBusy === item.session_id} aria-label={`确认归档任务：${item.title}`} onClick={() => void archiveTask(item)}>确认归档</button> : <button className="icon-btn" disabled={!!taskBusy} aria-label={`归档任务：${item.title}`} title="归档任务" onClick={() => { setArchiveConfirmId(item.session_id); setRenamingId(''); setTaskError('') }}><Archive/></button>}</div>
    </article>)}{!visibleSessions.length && <p>{taskQuery ? '没有匹配任务' : '暂无历史任务'}</p>}</div></aside>
  </div>
}

function AgentHome({ dashboard, workbench, templates, onAction, onOpenAnalysis, onRunTemplate, onManageTemplate, onCreateTemplate }: {
  dashboard?: Dashboard; workbench: Workbench; templates: AnalysisTemplate[]; onAction: (item: WorkbenchItem) => void;
  onOpenAnalysis: (id: string) => void; onRunTemplate: (id: string) => void;
  onManageTemplate: (template: AnalysisTemplate) => void; onCreateTemplate: () => void;
}) {
  return <div className="agent-home"><header><h2>今天从哪里开始？</h2><p>ASA 已连接岗位、人选和工作流上下文。</p></header><section className="agent-home-summary" aria-label="今日概况"><div><span>待处理</span><b>{workbench.summary.pending}</b></div><div><span>运行中</span><b>{workbench.summary.running}</b></div><div><span>已交付</span><b>{workbench.summary.delivered}</b></div><div><span>开放岗位</span><b>{dashboard?.counts?.active_jobs ?? '-'}</b></div></section><section className="agent-home-band"><header><h3>优先事项</h3></header>{workbench.items.filter(item => item.lane === 'pending').slice(0, 4).map(item => <button key={item.item_key} onClick={() => onAction(item)}><span><b>{item.title}</b><small>{item.reason || item.subtitle}</small></span><em>{item.status_label}</em></button>)}{!workbench.items.some(item => item.lane === 'pending') && <p>当前没有待处理事项</p>}</section><section className="agent-home-band agent-home-analyses"><header><h3>固定分析</h3><button className="icon-btn" title="新建固定分析" aria-label="新建固定分析" onClick={onCreateTemplate}><Plus/></button></header>{templates.slice(0, 4).map(item => <div className="agent-analysis-row" key={item.template_id}><button onClick={() => item.last_run_id ? onOpenAnalysis(item.last_run_id) : onRunTemplate(item.template_id)}><span><b>{item.name}</b><small>{item.last_result?.headline || item.question || '尚未运行'}</small></span><em>{item.last_run_id ? '查看' : '运行'}</em></button><button className="icon-btn" title={`管理固定分析：${item.name}`} aria-label={`管理固定分析：${item.name}`} onClick={() => onManageTemplate(item)}><Settings2/></button></div>)}{!templates.length && <p>暂无固定分析</p>}</section></div>
}
