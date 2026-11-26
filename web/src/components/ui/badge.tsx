import type { HTMLAttributes, ReactNode } from 'react'
import { Check, Clock3, Minus, Play, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { Tone } from '@/lib/status'

/** 语义 Badge：图标 + 文字 + 柔和的语义底，颜色只用于表达状态。 */
const TONES: Record<Tone, string> = {
  ok: 'border-success/20 bg-success-chip text-success-text',
  warn: 'border-warn/20 bg-warn-chip text-warn-text',
  err: 'border-danger/20 bg-danger-chip text-danger-text',
  info: 'border-primary/20 bg-primary-chip text-primary-text',
  primary: 'border-primary/20 bg-primary-chip text-primary-text',
  muted: 'border-line bg-surface-2 text-fg-3',
}

const ICONS = { ok: Check, warn: Clock3, err: X, info: Play, primary: Play, muted: Minus }

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone
  /** 仅 Task / Runner / Deploy 状态开启；普通计数和元信息不画状态图标。 */
  showIcon?: boolean
  children: ReactNode
}

export function Badge({ tone = 'muted', showIcon = false, className, children, ...rest }: BadgeProps) {
  const Icon = ICONS[tone]
  return (
    <span
      className={cn(
        'inline-flex h-6 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-xs font-medium leading-none',
        '[&>svg]:pointer-events-none [&>svg]:size-3.5 [&>svg]:stroke-[2]',
        TONES[tone], className,
      )}
      data-tone={tone}
      {...rest}
    >
      {showIcon ? <Icon aria-hidden /> : null}
      {children}
    </span>
  )
}

export interface TagProps extends HTMLAttributes<HTMLSpanElement> {
  mono?: boolean
  children: ReactNode
}

/** 中性元信息标签：弱底而非一排灰白描边胶囊。 */
export function Tag({ mono = false, className, children, ...rest }: TagProps) {
  return (
    <span
      className={cn(
        'inline-flex h-6 shrink-0 items-center gap-1 whitespace-nowrap rounded-md border border-line/70 bg-surface-2 px-2 text-2xs leading-none text-fg-3',
        mono ? 'font-mono' : 'font-medium',
        className,
      )}
      {...rest}
    >
      {children}
    </span>
  )
}
