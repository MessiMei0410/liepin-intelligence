import { copilotText } from './text'

export const humanizeActionError = (error: unknown, fallback: string) => {
  const message=copilotText(error)
  if(message.includes('Internal Server Error'))return '操作未提交，ASA 已保留当前状态，请稍后重试。'
  return message||fallback
}
