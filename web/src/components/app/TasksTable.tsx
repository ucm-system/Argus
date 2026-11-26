import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Search, X } from 'lucide-react'
import type { ColumnDef } from '@tanstack/react-table'
import { api } from '@/api'
import { toApiError } from '@/api/client'
import type { ApiErrorBody, Task, TaskFacets } from '@/api/types'
import { TaskStatusBadge } from '@/components/app/StatusBadge'
import { Button } from '@/components/ui/button'
import { Card, CardHeader } from '@/components/ui/card'
import { DataTable } from '@/components/ui/data-table'
import { FacetFilter } from '@/components/ui/facet-filter'
import { Input } from '@/components/ui/input'
import { PAGE_SIZES, Pagination } from '@/components/ui/pagination'
import { TableSkeleton } from '@/components/ui/skeleton'
import { EmptyState, ErrorState } from '@/components/ui/states'
import { progressHeadline, resultSegments, taskStatusMeta } from '@/lib/status'
import { formatDateTime, formatDuration, formatRelative, runDuration } from '@/lib/utils'

/**
 * 全量 Task 表（D-62：从运行列表搬到概览，与 KPI 同页共存）。
 *
 * v2（D-73）：数据来自 `GET /tasks`（Task 投影，计数取 `runner_counts`）；
 * facets 只有 status / submitter / runner 三个维度（`kind` 随占位 Run 概念取消）。
 *
 * D-61：真分页 + Excel 式表头列筛选。
 *  - `GET /tasks?page=&page_size=&status=a,b&submitter=dev,qa&runner=` → 一页 items + `total`
 *  - `GET /tasks/facets?同样过滤` → 表头下拉的候选值与计数（**每次筛选变化都重拉**）
 *
 * 状态全进 URL：`page` / `page_size` / `status` / `submitter` / `runner` / `q`。
 * 改筛选一律**重置到第 1 页**，否则会停在空页上。
 *
 * 搜索语义（#113）：`GET /tasks` 契约只有 status/runner/submitter 过滤，**没有名称/ID 查询**；
 * 在不改契约的前提下，`q` 是**页内查找**——输入框、结果计数与空态都必须把范围写在明面上，
 * 不伪装成全库搜索，也不能让"另一个页里存在的 ID"被读成"Task 不存在"。
 */

const DEFAULT_PAGE_SIZE = 20

type FacetKey = 'status' | 'submitter' | 'runner'

const FACET_LABEL: Record<FacetKey, string> = {
  status: '状态',
  submitter: '提交人',
  runner: 'Runner',
}

/** URL 里逗号分隔的多值 */
function parseList(raw: string | null): string[] {
  return (raw ?? '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

function pickPageSize(raw: string | null): number {
  const value = Number(raw)
  return (PAGE_SIZES as readonly number[]).includes(value) ? value : DEFAULT_PAGE_SIZE
}

/** 「进度 / 结果」两行单元格（#114：进度 = 已结束/总数；结果 = 失败/成功/跳过/取消/执行中/待执行分段）。 */
function ProgressCell({ task }: { task: Task }) {
  const headline = progressHeadline({
    status: task.status,
    counts: task.runner_counts,
    deployPhase: task.deploy?.phase ?? null,
    deployWaitedSeconds: task.deploy?.waited_seconds ?? null,
  })
  const segments = resultSegments(task.runner_counts)
  const SEGMENT_TONE: Record<string, string> = {
    failed: 'text-danger-text font-medium',
    succeeded: 'text-success-text',
    skipped: 'text-warn-text',
    canceled: 'text-fg-3',
    running: 'text-primary-text',
    pending: 'text-fg-3',
  }
  return (
    <div className="flex min-w-[9.5rem] flex-col gap-0.5" data-testid={`task-progress-${task.id}`}>
      <span className="whitespace-nowrap text-xs font-medium text-fg">{headline}</span>
      {segments.length > 0 ? (
        <span className="flex flex-wrap gap-x-2 whitespace-nowrap text-xs leading-[18px]">
          {segments.map((segment) => (
            <span key={segment.key} className={SEGMENT_TONE[segment.key]}>
              {segment.label} {segment.count}
            </span>
          ))}
        </span>
      ) : null}
    </div>
  )
}

export function TasksTable() {
  const [params, setParams] = useSearchParams()

  const page = Math.max(1, Number(params.get('page') ?? 1) || 1)
  const pageSize = pickPageSize(params.get('page_size'))
  const statusRaw = params.get('status')
  const submitterRaw = params.get('submitter')
  const runnerRaw = params.get('runner')
  const search = params.get('q') ?? ''

  const filters = useMemo(
    () => ({
      status: parseList(statusRaw),
      submitter: parseList(submitterRaw),
      runner: parseList(runnerRaw),
    }),
    [statusRaw, submitterRaw, runnerRaw],
  )

  /** 改 URL；`resetPage` 时把 page 清掉（回到第 1 页） */
  const updateQuery = useCallback(
    (patch: Record<string, string | null | undefined>, options?: { resetPage?: boolean }) => {
      const next = new URLSearchParams(params)
      for (const [key, value] of Object.entries(patch)) {
        if (value == null || value === '') next.delete(key)
        else next.set(key, value)
      }
      if (options?.resetPage) next.delete('page')
      setParams(next, { replace: true })
    },
    [params, setParams],
  )

  /** 多值列的「应用」：写回逗号串并回到第 1 页 */
  const applyFacet = (key: FacetKey, values: string[]) =>
    updateQuery({ [key]: values.length > 0 ? values.join(',') : null }, { resetPage: true })

  const [items, setItems] = useState<Task[]>([])
  const [total, setTotal] = useState(0)
  const [facets, setFacets] = useState<TaskFacets | null>(null)
  const [loading, setLoading] = useState(true)
  const [facetsLoading, setFacetsLoading] = useState(true)
  const [error, setError] = useState<ApiErrorBody | null>(null)
  const [reloadToken, setReloadToken] = useState(0)
  const requestId = useRef(0)

  const query = useMemo(
    () => ({
      status: filters.status.join(',') || undefined,
      submitter: filters.submitter.join(',') || undefined,
      runner: filters.runner.join(',') || undefined,
    }),
    [filters],
  )

  // 列表：page / page_size / 过滤条件任何一个变都重拉
  useEffect(() => {
    const id = ++requestId.current
    setLoading(true)
    setError(null)
    api
      .listTasks({ ...query, page, page_size: pageSize })
      .then((result) => {
        if (id !== requestId.current) return
        setItems(result.items)
        setTotal(result.total ?? result.items.length)
      })
      .catch((e) => {
        if (id !== requestId.current) return
        setError(toApiError(e))
        setItems([])
        setTotal(0)
      })
      .finally(() => {
        if (id === requestId.current) setLoading(false)
      })
  }, [query, page, pageSize, reloadToken])

  // 表头候选值：筛选变化就重拉（计数随其它列变化）
  useEffect(() => {
    let alive = true
    setFacetsLoading(true)
    api
      .listTaskFacets(query)
      .then((result) => {
        if (alive) setFacets(result)
      })
      .catch(() => {
        if (alive) setFacets(null)
      })
      .finally(() => {
        if (alive) setFacetsLoading(false)
      })
    return () => {
      alive = false
    }
  }, [query, reloadToken])

  /** 本页内的关键字查找（服务端没有全文搜索；文案已写清"在当前页内查找"） */
  const visible = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    if (!keyword) return items
    return items.filter((task) => `${task.id} ${task.name} ${task.submitted_by}`.toLowerCase().includes(keyword))
  }, [items, search])

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const outOfRange = total > 0 && page > totalPages
  const activeChips = (Object.keys(filters) as FacetKey[]).flatMap((key) =>
    filters[key].map((value) => ({ key, value })),
  )
  const anyFilter = activeChips.length > 0 || Boolean(search)

  const columns = useMemo<ColumnDef<Task, unknown>[]>(
    () => [
      {
        id: 'id',
        header: 'Task',
        meta: { className: 'whitespace-nowrap font-mono text-xs' },
        cell: ({ row }) => (
          <Link to={`/tasks/${row.original.id}`} className="text-primary-text hover:underline">
            {row.original.id}
          </Link>
        ),
      },
      {
        id: 'name',
        header: '名称',
        meta: { className: 'max-w-[14rem]' },
        cell: ({ row }) => (
          <span className="block max-w-[14rem] truncate" title={row.original.name}>
            {row.original.name}
          </span>
        ),
      },
      {
        id: 'status',
        header: FACET_LABEL.status,
        meta: {
          filter: (
            <FacetFilter
              label={FACET_LABEL.status}
              selected={filters.status}
              buckets={facets?.status ?? []}
              renderValue={(value) => taskStatusMeta(value).label}
              onApply={(values) => applyFacet('status', values)}
            />
          ),
        },
        cell: ({ row }) => <TaskStatusBadge status={row.original.status} />,
      },
      {
        // #114：进度（已结束/总数 + 当前卡点）与结果（失败/成功/跳过/取消分段）分开表达，
        // 不再用 `succeeded / total` 冒充"runner 进度"。
        id: 'runners',
        header: '进度 / 结果',
        meta: { className: 'whitespace-nowrap' },
        cell: ({ row }) => <ProgressCell task={row.original} />,
      },
      {
        id: 'attempt',
        header: 'attempt',
        meta: { className: 'whitespace-nowrap tabular-nums' },
        cell: ({ row }) => (row.original.current_attempt === 0 ? '—' : `#${row.original.current_attempt}`),
      },
      {
        id: 'duration',
        header: '耗时',
        meta: { className: 'whitespace-nowrap tabular-nums' },
        cell: ({ row }) => formatDuration(runDuration(row.original)),
      },
      {
        id: 'submitter',
        header: FACET_LABEL.submitter,
        meta: {
          className: 'whitespace-nowrap',
          filter: (
            <FacetFilter
              label={FACET_LABEL.submitter}
              selected={filters.submitter}
              buckets={facets?.submitter ?? []}
              onApply={(values) => applyFacet('submitter', values)}
            />
          ),
        },
        cell: ({ row }) => row.original.submitted_by,
      },
      {
        id: 'submitted_at',
        header: '提交时间',
        meta: { className: 'whitespace-nowrap text-2xs text-fg-3' },
        cell: ({ row }) => (
          <span title={formatRelative(row.original.submitted_at)}>{formatDateTime(row.original.submitted_at)}</span>
        ),
      },
    ],
    // facets / filters 变了要重建表头（下拉里的计数与勾选态跟着变）
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [facets, filters],
  )

  return (
    <>
      <Card>
        <CardHeader
          tone="ok"
          title="全部 Task"
          sub={
            loading
              ? '加载中…'
              : search
                ? `共 ${total} 条 · 在当前页 ${items.length} 条内查找「${search}」：命中 ${visible.length} 条`
                : `共 ${total} 条 · 第 ${Math.min(page, totalPages)} / ${totalPages} 页`
          }
          actions={
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <Search
                  className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-fg-3"
                  aria-hidden
                />
                <Input
                  value={search}
                  onChange={(event) => updateQuery({ q: event.target.value }, { resetPage: true })}
                  placeholder="在当前页查找名称 / ID"
                  aria-label="在当前页查找名称 / ID"
                  title="查找只在当前页内进行，不跨页"
                  className="h-9 w-64 pl-8"
                />
              </div>
            </div>
          }
        />

        {/* 按 Runner 筛选：候选值与计数来自 /tasks/facets */}
        <div className="flex flex-wrap items-center gap-2 border-b border-line-soft px-5 py-2 text-2xs text-fg-3">
          <FacetFilter
            label={FACET_LABEL.runner}
            selected={filters.runner}
            buckets={facets?.runner ?? []}
            onApply={(values) => applyFacet('runner', values)}
          />
          {filters.runner.length > 0 ? (
            <span className="font-mono text-fg-2">{filters.runner.join('、')}</span>
          ) : null}
        </div>

        {anyFilter ? (
          <div className="flex flex-wrap items-center gap-2 border-b border-line-soft bg-surface-2 px-5 py-2 text-2xs text-fg-3">
            <span>已应用的筛选：</span>
            {activeChips.map((chip) => (
              <button
                key={`${chip.key}-${chip.value}`}
                type="button"
                onClick={() => applyFacet(chip.key, filters[chip.key].filter((value) => value !== chip.value))}
                aria-label={`清除 ${FACET_LABEL[chip.key]} 过滤 ${chip.value}`}
                className="inline-flex h-6 items-center gap-1 rounded-full border border-line bg-surface px-2 text-fg-2 transition-colors hover:border-line-strong hover:text-fg"
              >
                <span className="font-mono">
                  {FACET_LABEL[chip.key]}：{chip.value}
                </span>
                <X className="size-3" aria-hidden />
              </button>
            ))}
            {search ? (
              <button
                type="button"
                onClick={() => updateQuery({ q: null })}
                aria-label="清除页内查找"
                className="inline-flex h-6 items-center gap-1 rounded-full border border-line bg-surface px-2 text-fg-2 transition-colors hover:border-line-strong hover:text-fg"
              >
                <span className="font-mono">当前页查找：{search}</span>
                <X className="size-3" aria-hidden />
              </button>
            ) : null}
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto"
              data-testid="clear-all-filters"
              onClick={() =>
                updateQuery({ status: null, submitter: null, runner: null, q: null }, { resetPage: true })
              }
            >
              清除全部
            </Button>
          </div>
        ) : null}

        {error ? (
          <div className="p-4">
            <ErrorState error={error} onRetry={() => setReloadToken((token) => token + 1)} />
          </div>
        ) : null}

        {loading && items.length === 0 ? (
          <TableSkeleton rows={6} cols={8} />
        ) : outOfRange ? (
          <EmptyState
            data-testid="page-out-of-range"
            title={`第 ${page} 页超出范围（共 ${totalPages} 页）`}
            action={
              <Button size="sm" data-testid="back-to-first" onClick={() => updateQuery({ page: null })}>
                回到第 1 页
              </Button>
            }
          />
        ) : (
          <>
            <DataTable
              columns={columns}
              data={visible}
              rowTestId={(task) => `task-row-${task.id}`}
              empty={
                /**
                 * 空态按条件组合分文案（#113）：
                 *  - 只有关键字：绝不提示"放宽列筛选"——范围说明讲清"只在当前页找，
                 *    该 Task 可能不在这一页"，并给翻页提示；
                 *  - 有关键字 + 列筛选：两种条件都说；
                 *  - 只有列筛选：保留"放宽列筛选"。
                 */
                <div className="flex flex-col items-center gap-2 py-4" data-testid="tasks-empty">
                  {search ? (
                    <>
                      <span className="text-sm font-medium text-fg-2">
                        当前页没有匹配「{search}」的 Task
                      </span>
                      <span className="max-w-md text-2xs leading-[18px] text-fg-3" data-testid="empty-search-scope">
                        查找只在当前页 {items.length} 条内进行，不跨页。要找的 Task 可能不在这一页：
                        翻页查找，或清除页内查找。
                        {activeChips.length > 0 ? '也可以放宽列筛选后再试。' : ''}
                      </span>
                    </>
                  ) : (
                    <>
                      <span className="text-sm font-medium text-fg-2">
                        {anyFilter ? '当前筛选条件下没有 Task' : '还没有 Task'}
                      </span>
                      <span className="text-2xs text-fg-3">
                        {anyFilter ? '放宽列筛选，或者清除全部条件。' : '去「提交 Task」创建第一个 Task。'}
                      </span>
                    </>
                  )}
                  {anyFilter ? (
                    <Button
                      size="sm"
                      data-testid="empty-clear-filters"
                      onClick={() =>
                        updateQuery(
                          search ? { q: null } : { status: null, submitter: null, runner: null },
                          { resetPage: true },
                        )
                      }
                    >
                      {search ? '清除页内查找' : '清除过滤'}
                    </Button>
                  ) : null}
                </div>
              }
            />
            <Pagination
              total={total}
              page={page}
              pageSize={pageSize}
              disabled={loading}
              onPageChange={(next) => updateQuery({ page: next === 1 ? null : String(next) })}
              onPageSizeChange={(size) =>
                updateQuery({ page_size: size === DEFAULT_PAGE_SIZE ? null : String(size) }, { resetPage: true })
              }
            />
          </>
        )}

        {search && items.length > 0 ? (
          <div className="border-t border-line-soft px-5 py-2 text-2xs text-fg-3" data-testid="search-scope-note">
            本页 {items.length} 条里命中 {visible.length} 条（页内查找，不跨页）
          </div>
        ) : null}

        {facetsLoading ? (
          <span className="sr-only" aria-live="polite">
            正在刷新列筛选候选值…
          </span>
        ) : null}
      </Card>
    </>
  )
}
