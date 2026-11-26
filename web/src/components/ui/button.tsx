import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'dangerGhost'
type Size = 'sm' | 'md'

/**
 * 按钮外观（对齐 shadcn Button，见 docs/research/shadcn-visual-specs.md §2.1）：
 *  - 高度：md = h-9（36）/ sm = h-8（32），**可点面积都 ≥32px**；
 *  - 字：text-sm font-medium（md）/ text-xs（sm）；
 *  - hover **只动颜色**（`/90` 或 `bg-accent`），不缩放、不加阴影（shadcn 没有 `active:scale`）；
 *  - 焦点：`ring-[3px] ring-ring/50` + `border-ring`（全站统一）；
 *  - 禁用：`pointer-events-none opacity-50`；
 *  - 图标：尺寸由组件兜底（16px），调用点只写 `data-icon="inline-start|inline-end"`。
 *
 * 变体层级（设计约束，用户 2026-11-13 两轮裁决 + UX 改造 #111~#117）：
 *  - `primary`  页面或局部区域的**唯一**主行动（新建 Task / 提交 / 查看报告 / 查看失败日志）。
 *               统一用**蓝色强调**（--color-primary）：蓝是本产品唯一的行动色，
 *               墨色只保留给正文与框架，不再当主按钮底色；
 *  - `secondary` 次级但独立的动作（页头入口、失败横幅里的「立即重试」一类）；
 *  - `ghost`    行内轻操作 —— **所有「详情」一律 ghost 文字**（RunnersPage / 提交页 runner 行 / 服务来源 / precheck 同款；
 *               用户原话：描边「太丑了……原来 runner 那个挺好的」）；
 *  - `danger` / `dangerGhost` 破坏性动作（删除 / 取消 Task）。
 */
const VARIANTS: Record<Variant, string> = {
  // 主按钮 = 行动强调色（蓝）：链接 / 选中 / 焦点环 / 主按钮同源，一屏只有一个
  primary:
    'border border-primary bg-primary text-primary-fg shadow-xs hover:bg-primary/90 hover:border-primary/90',
  secondary:
    'border border-line bg-surface text-fg-2 shadow-xs hover:bg-line-soft hover:text-fg',
  ghost: 'border border-transparent text-fg-2 hover:bg-line-soft hover:text-fg',
  danger: 'border border-danger bg-danger text-white shadow-xs hover:bg-danger/90',
  dangerGhost: 'border border-transparent text-danger-text hover:bg-danger-weak',
}

const SIZES: Record<Size, string> = {
  sm: 'h-8 gap-1.5 px-3 text-xs has-[>svg]:px-2.5',
  md: 'h-9 gap-2 px-4 text-sm has-[>svg]:px-3',
}

/** 按钮外观（供 <a> 等非 button 元素复用，保证链接与按钮同款）。 */
export function buttonClassName({
  variant = 'secondary',
  size = 'md',
  className,
}: {
  variant?: Variant
  size?: Size
  className?: string
}): string {
  return cn(
    'inline-flex shrink-0 select-none items-center justify-center whitespace-nowrap rounded-md font-medium',
    'outline-none transition-all duration-150 ease-out',
    'focus-visible:outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50',
    'disabled:pointer-events-none disabled:opacity-50',
    '[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*="size-"])]:size-4',
    VARIANTS[variant],
    SIZES[size],
    className,
  )
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  loading?: boolean
  icon?: ReactNode
}

export function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  icon,
  className,
  children,
  disabled,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={buttonClassName({ variant, size, className })}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <Loader2 className="size-4 animate-spin" aria-hidden data-icon="inline-start" /> : icon}
      {children}
    </button>
  )
}
