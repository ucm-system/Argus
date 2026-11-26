import type { ReactNode } from 'react'
import { Activity, Boxes, CheckCircle2, Clock3, CircleAlert, Circle } from 'lucide-react'
import { cn } from '@/lib/utils'

const VALUE_TONE = {
  blue: 'text-fg', green: 'text-fg', amber: 'text-fg', red: 'text-fg', ink: 'text-fg', muted: 'text-fg',
} as const

const ICON_TONE = {
  blue: 'bg-primary-weak text-primary-text',
  green: 'bg-success-weak text-success-text',
  amber: 'bg-warn-weak text-warn-text',
  red: 'bg-danger-weak text-danger-text',
  ink: 'bg-surface-2 text-fg-2',
  muted: 'bg-surface-2 text-fg-3',
} as const

const ICONS = { blue: Activity, green: CheckCircle2, amber: Clock3, red: CircleAlert, ink: Boxes, muted: Circle }

export interface KpiCardProps {
  label: ReactNode
  value: ReactNode
  hint?: ReactNode
  tone?: keyof typeof VALUE_TONE
  className?: string
}

/** 紧凑 KPI：图标帮助识别，数字保持近黑；语义色只用于所属指标，不制造视觉噪音。 */
export function KpiCard({ label, value, hint, tone = 'muted', className }: KpiCardProps) {
  const Icon = ICONS[tone]
  return (
    <div className={cn('flex min-w-0 items-center gap-4 rounded-[10px] border border-line bg-surface px-5 py-4 shadow-xs', className)}>
      <span className={cn('grid size-10 shrink-0 place-items-center rounded-[9px]', ICON_TONE[tone])} aria-hidden>
        <Icon className="size-[18px]" />
      </span>
      <div className="min-w-0">
        <div className="text-xs font-medium text-fg-3">{label}</div>
        <div className="mt-0.5 flex flex-wrap items-baseline gap-x-3">
          <p className={cn('text-[26px] font-semibold leading-none tabular-nums tracking-tight', VALUE_TONE[tone])}>{value}</p>
          {hint ? <p className="text-2xs leading-[18px] text-fg-3">{hint}</p> : null}
        </div>
      </div>
    </div>
  )
}
