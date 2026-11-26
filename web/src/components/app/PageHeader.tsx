import type { ReactNode } from 'react'

/** 页面主标题与唯一页面级动作；副标题只说明用户能在这里完成什么。 */
export function PageHeader({ title, description, actions }: { title: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start gap-5 pb-1" data-testid="page-heading">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold leading-tight tracking-[-0.025em] text-fg">{title}</h1>
        {description ? <p className="mt-1.5 text-sm leading-[22px] text-fg-3">{description}</p> : null}
      </div>
      {actions ? <div className="ml-auto flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  )
}
