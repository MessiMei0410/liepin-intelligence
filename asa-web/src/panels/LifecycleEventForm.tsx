import { useEffect, useState } from 'react'
import { CircleCheck, LoaderCircle, TriangleAlert } from 'lucide-react'
import { api } from '../api'
import { copilotText } from '../shared/text'
import { setDirtyForm } from '../shared/dirtyForm'

// 生命周期一等事件（面试/Offer/入职）记录表单：类型+时间+备注。
// 枚举是后端契约常量（asa_core/service.py LIFECYCLE_EVENT_TYPES），硬编码省去额外请求与加载态。
const eventTypeOptions = [
  { value: 'interview_scheduled', label: '面试安排' },
  { value: 'interview_completed', label: '面试完成' },
  { value: 'offer_extended', label: 'Offer 发出' },
  { value: 'offer_accepted', label: 'Offer 已接受' },
  { value: 'offer_declined', label: 'Offer 已拒绝' },
  { value: 'onboarded', label: '确认入职' },
]

export function LifecycleEventForm({candidateId, onRecorded}:{candidateId:number; onRecorded:()=>void|Promise<void>}) {
  const [eventType,setEventType]=useState('interview_scheduled')
  const [occurredAt,setOccurredAt]=useState('')
  const [notes,setNotes]=useState('')
  const [busy,setBusy]=useState(false)
  const [receipt,setReceipt]=useState('')
  const [error,setError]=useState('')
  // 脏状态登记：填了时间或备注还没提交时，切 tab/进 Agent/关面板先确认再丢弃。
  const dirty=Boolean(occurredAt.trim()||notes.trim())
  useEffect(()=>{
    setDirtyForm(`lifecycle:${candidateId}`,dirty)
    return ()=>setDirtyForm(`lifecycle:${candidateId}`,false)
  },[candidateId,dirty])
  const submit=async()=>{
    if(busy)return
    setBusy(true);setError('');setReceipt('')
    try{
      const result=await api.recordLifecycleEvent(candidateId,{event_type:eventType,occurred_at:occurredAt?occurredAt.replace('T',' '):'',notes:notes.trim()})
      const label=result.event?.event_type_label||eventTypeOptions.find(option=>option.value===eventType)?.label||'事件'
      const repeated=result.already_recorded||result.receipt?.idempotent_replay
      setReceipt(repeated?`${label}此前已记录，未重复写入（事件 #${result.event_id}）。`:`${label}已记录（事件 #${result.event_id}）${result.followup_task_id?`，已生成跟进待办 #${result.followup_task_id}`:''}。`)
      setNotes('')
      await Promise.resolve(onRecorded()).catch(()=>undefined)
    }catch(e){
      setError(copilotText(e)||'记录失败，请重试。')
    }finally{setBusy(false)}
  }
  return <form className="lifecycle-form" onSubmit={event=>{event.preventDefault();void submit()}}>
    <div className="lifecycle-form-grid">
      <label><span>事件类型</span><select value={eventType} onChange={event=>setEventType(event.target.value)}>{eventTypeOptions.map(option=><option key={option.value} value={option.value}>{option.label}</option>)}</select></label>
      <label><span>发生时间（选填，默认现在）</span><input type="datetime-local" value={occurredAt} onChange={event=>setOccurredAt(event.target.value)}/></label>
    </div>
    <label><span>备注（选填）</span><textarea value={notes} onChange={event=>setNotes(event.target.value)} placeholder="如：一面通过，等待二面安排…" rows={2}/></label>
    <div className="lifecycle-form-actions">
      <button type="submit" className="button primary" disabled={busy}>{busy?<LoaderCircle className="spin"/>:null}记录事件</button>
      <small>只写入时间线并生成跟进待办，不会自动对外沟通。</small>
    </div>
    {error&&<p className="lifecycle-form-message error" role="alert"><TriangleAlert/>{error}</p>}
    {receipt&&<p className="lifecycle-form-message success" role="status"><CircleCheck/>{receipt}</p>}
  </form>
}
