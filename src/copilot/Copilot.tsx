import { useEffect, useState } from 'react'
import { Check, ExternalLink, MessageSquareText, Send, SquarePen, X } from 'lucide-react'
import { api, BusinessFocus } from '../api'
import { copilotText } from '../shared/text'
import { tabs } from '../shared/tabs'

type CopilotAction = { type: string; id?: string | number; label?: string }
type ChatMessage = { role: string; text: string; actions?: CopilotAction[] }

export function Copilot({context,openWorkflow,standalone=false}:{context:Record<string,unknown>,openWorkflow:(id:string)=>void|Promise<void>,standalone?:boolean}) {
  const [messages,setMessages]=useState<ChatMessage[]>([])
  const [text,setText]=useState('')
  const [busy,setBusy]=useState(false)
  const [actionBusy,setActionBusy]=useState('')
  const [sessionId,setSessionId]=useState(()=>{try{return localStorage.getItem('asa-copilot-session-id')||''}catch{return ''}})
  const [focus,setFocus]=useState<BusinessFocus>()
  const supportedAction=(action:CopilotAction)=>['start_workflow','open_workflow'].includes(action.type) && !!action.id
  const runAction=async(action:CopilotAction)=>{
    if(!supportedAction(action) || actionBusy)return
    const id=String(action.id)
    setActionBusy(`${action.type}:${id}`)
    try{
      if(action.type==='start_workflow'){
        await api.workflowAction(id,'start')
        setMessages(m=>[...m,{role:'asa',text:'已启动工作流，正在打开计划面板。'}])
      }
      await openWorkflow(id)
    }catch(e){
      setMessages(m=>[...m,{role:'asa',text:copilotText(e)||'工作流操作失败，请重试。'}])
    }finally{
      setActionBusy('')
    }
  }
  const actionsFrom=(value:unknown):CopilotAction[]=>Array.isArray(value)?value.filter((item):item is CopilotAction=>!!item&&typeof item==='object'&&supportedAction(item as CopilotAction)):[]
  useEffect(()=>{
    if(!sessionId)return
    let active=true
    api.copilotSession(sessionId).then(history=>{
      if(!active)return
      setMessages((history.messages||[]).map(item=>({role:item.role==='assistant'?'asa':'user',text:copilotText(item.content),actions:actionsFrom(item.suggested_actions)})))
      setFocus(history.business_focus)
    }).catch(()=>undefined)
    return()=>{active=false}
  },[sessionId])
  const newSession=()=>{
    setSessionId('');setMessages([]);setFocus(undefined);setText('')
    try{localStorage.removeItem('asa-copilot-session-id')}catch{/* Storage can be disabled. */}
  }
  const send=async()=>{
    if(!text.trim()||busy)return
    const q=text
    setText('')
    setMessages(m=>[...m,{role:'user',text:q}])
    setBusy(true)
    try{
      const r=await api.copilot(q,context,sessionId)
      const nextSession=String(r.session_id||sessionId)
      if(nextSession&&nextSession!==sessionId){setSessionId(nextSession);try{localStorage.setItem('asa-copilot-session-id',nextSession)}catch{/* Storage can be disabled. */}}
      setFocus(r.business_focus)
      const answer=copilotText(r.answer) || copilotText(r.message) || copilotText(r.response) || copilotText(r.summary)
      setMessages(m=>[...m,{role:'asa',text:answer||'已完成分析，请查看关联工作流与产物。',actions:actionsFrom(r.suggested_actions)}])
    }catch(e){
      setMessages(m=>[...m,{role:'asa',text:copilotText(e)||'Copilot 请求失败，请稍后重试。'}])
    }finally{
      setBusy(false)
    }
  }
  const contextLabel = context.type === 'candidate' ? `候选人 #${context.id}` : context.type === 'workflow' ? `工作流 ${context.id}` : `ASA Agent · ${tabs.find(item => item[0] === context.page)?.[1] || '总览'}`
  const focusLabel=focus?.candidate?.name||[focus?.client,focus?.job?.title].filter(Boolean).join(' / ')||focus?.client||''
  const actionLabel:Record<string,string>={job_archive:'归档岗位',job_split:'拆分岗位',job_publish:'发布岗位',candidate_sourcing:'寻访人选',candidate_outreach:'触达人选',candidate_review:'复核人选',recommendation:'客户推荐',salary:'谈薪处理'}
  return <aside className={`copilot ${standalone?'standalone':''}`}><header><MessageSquareText/><div><b>ASA Copilot</b><span>{contextLabel}</span></div><button className="icon-btn copilot-new" onClick={newSession} title="新建会话" aria-label="新建会话"><SquarePen/></button>{standalone&&<button className="icon-btn copilot-close" onClick={()=>window.close()} title="关闭浮窗" aria-label="关闭浮窗"><X/></button>}</header>{focusLabel&&<div className={`business-focus ${focus?.needs_clarification?'conflict':''}`}><span>{focus?.needs_clarification?'需要确认':'当前焦点'}</span><b>{focusLabel}</b>{focus?.action&&<small>{actionLabel[focus.action]||focus.action}{focus.directions?.length?` · ${focus.directions.join(' / ')}`:''}</small>}</div>}<div className="chat" aria-live="polite">{messages.length===0?<div className="chat-empty"><b>可以直接开始</b><span>询问当前岗位、人选或工作流。</span></div>:messages.map((m,i)=><div className={`message ${m.role}`} key={i}>{m.text}{!!m.actions?.length&&<div className="message-actions">{m.actions.map((action,index)=><button className={`button ${action.type==='start_workflow'?'primary':''}`} disabled={!!actionBusy} onClick={()=>runAction(action)} key={`${action.type}-${action.id}-${index}`}>{action.type==='start_workflow'?<Check/>:<ExternalLink/>}{action.label||'打开'}</button>)}</div>}</div>)}</div><div className="composer"><textarea value={text} onChange={e=>setText(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}}} placeholder="问 ASA…" aria-label="向 ASA 提问"/><button disabled={busy||!text.trim()} onClick={send} title="发送" aria-label="发送"><Send/></button></div></aside>
}
