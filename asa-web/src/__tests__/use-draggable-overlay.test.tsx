import { describe, expect, it } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { useDraggableOverlay } from '../shared/useDraggableOverlay'

function Harness() {
  const { overlayRef, panelRef, dragProps } = useDraggableOverlay()
  return (
    <div className="overlay" ref={overlayRef} data-testid="overlay">
      <article ref={panelRef} className="detail-panel" style={{ width: 600, height: 400 }}>
        <header data-testid="header" {...dragProps}><button data-testid="headerBtn">返回</button></header>
      </article>
    </div>
  )
}

describe('useDraggableOverlay', () => {
  it('拖动 header 移动整个 overlay（增量平移）', () => {
    const { getByTestId } = render(<Harness />)
    const overlay = getByTestId('overlay') as HTMLElement
    const header = getByTestId('header') as HTMLElement
    fireEvent.pointerDown(header, { button: 0, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(header, { clientX: 140, clientY: 160 })
    const style = overlay.getAttribute('style') || ''
    const left = Number(style.match(/left:\s*(-?\d+)px/)?.[1] ?? 0)
    const top = Number(style.match(/top:\s*(-?\d+)px/)?.[1] ?? 0)
    // 拖动 140-100=40, 160-100=60；jsdom 布局差异允许 ±12px（body 默认 margin 8px）
    expect(Math.abs(left - 40)).toBeLessThanOrEqual(12)
    expect(Math.abs(top - 60)).toBeLessThanOrEqual(12)
  })

  it('拖动被钳制在视口内（不会完全拖丢）', () => {
    const { getByTestId } = render(<Harness />)
    const overlay = getByTestId('overlay') as HTMLElement
    const header = getByTestId('header') as HTMLElement
    // 向视口外猛拖：left/top 应被钳制到 minX/minY（= -panel + 48）
    fireEvent.pointerDown(header, { button: 0, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(header, { clientX: 10000, clientY: 10000 })
    const style = overlay.getAttribute('style') || ''
    const left = Number(style.match(/left:\s*(-?\d+)px/)?.[1] ?? 0)
    const top = Number(style.match(/top:\s*(-?\d+)px/)?.[1] ?? 0)
    expect(left).toBeLessThanOrEqual(window.innerWidth - 48)
    expect(left).toBeGreaterThanOrEqual(-600 + 48)
    expect(top).toBeGreaterThanOrEqual(-400 + 48)
  })

  it('非左键按下不触发拖动', () => {
    const { getByTestId } = render(<Harness />)
    const overlay = getByTestId('overlay') as HTMLElement
    const header = getByTestId('header') as HTMLElement
    const before = overlay.getBoundingClientRect()
    fireEvent.pointerDown(header, { button: 2, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(header, { clientX: 200, clientY: 200 })
    const after = overlay.getBoundingClientRect()
    expect(Math.round(after.left - before.left)).toBe(0)
    expect(Math.round(after.top - before.top)).toBe(0)
  })

  it('header 内按钮按下不触发拖动', () => {
    const { getByTestId } = render(<Harness />)
    const overlay = getByTestId('overlay') as HTMLElement
    const btn = getByTestId('headerBtn') as HTMLElement
    // 真实场景：pointerdown 从按钮冒泡到 header，event.target 是按钮
    fireEvent.pointerDown(btn, { button: 0, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(btn, { clientX: 200, clientY: 200 })
    expect((overlay.getAttribute('style') || '').includes('left')).toBe(false)
  })
})
