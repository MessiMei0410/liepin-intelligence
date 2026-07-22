import { ShieldAlert } from 'lucide-react'

export function Diagnostics({error,retry}:{error:string,retry:()=>void}) { return <main className="diagnostics"><ShieldAlert/><h1>ASA Core 无法连接</h1><p>{error}</p><dl><dt>服务地址</dt><dd>http://127.0.0.1:8765/api/v1/health</dd><dt>数据策略</dt><dd>诊断期间不会显示演示数据或缓存人选。</dd></dl><button className="button primary" onClick={retry}>重新连接</button></main> }
