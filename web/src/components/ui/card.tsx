import type { HTMLAttributes, ReactNode } from 'react'
import { cn } from '@/lib/utils'
import type { Tone } from '@/lib/status'

export interface CardHeaderProps {
  title: ReactNode
  sub?: ReactNode
  tone?: Tone
  actions?: ReactNode
  className?: string
  toneClass?: string
  id?: string
}

/** shadcn New York 卡片：标题和说明纵向排列，动作独立在右侧。 */
export function Card({ className, ...rest }: HTMLAttributes<HTMLElement>) {
  return (
    <section
      className={cn('min-w-0 overflow-hidden rounded-[10px] border border-line bg-surface text-fg shadow-xs', className)}
      {...rest}
    />
  )
}

export function CardHeader({ title, sub, actions, className, id }: CardHeaderProps) {
  return (
    <header className={cn('flex flex-wrap items-start gap-3 border-b border-line-soft px-5 py-4', className)}>
      <div className="min-w-0">
        <h2 id={id} className="text-base font-semibold leading-[22px] text-fg">{title}</h2>
        {sub ? <p className="mt-0.5 text-xs leading-[19px] text-fg-3">{sub}</p> : null}
      </div>
      {actions ? <div className="ml-auto flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  )
}

export function CardBody({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('px-5 py-4', className)} {...rest} />
}

export function CardFooter({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex flex-wrap items-center gap-2 border-t border-line-soft px-5 py-3 text-xs text-fg-3', className)} {...rest} />
}
