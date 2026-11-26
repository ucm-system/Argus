import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, ChevronRight, Download, FileText } from 'lucide-react'
import { API_BASE, api } from '@/api'
import { toApiError } from '@/api/client'
import type { ApiErrorBody, Artifact, RunnerExecution, TaskDetail } from '@/api/types'
import { RunnerResultPanel } from '@/components/app/RunnerResultPanel'
import { Badge } from '@/components/ui/badge'
import { Button, buttonClassName } from '@/components/ui/button'
import { Card, CardBody, CardHeader } from '@/components/ui/card'
import { EmptyState, ErrorState, LoadingBlock } from '@/components/ui/states'
import { Table, TBody, TD, TH, THead, TR, TableWrap } from '@/components/ui/table'
import { TaskStatusBadge } from '@/components/app/StatusBadge'
import { useAsync } from '@/hooks/useAsync'
import { ARTIFACT_GROUPS, groupArtifacts, relativeArtifactPath } from '@/lib/artifact-groups'
import { artifactViewKindOf } from '@/components/app/ArtifactViewer'
import { formatBytes } from '@/lib/download'
import { stripErrorCodePrefix } from '@/lib/error-format'
import { errorCopy } from '@/lib/error-copy'
import { endedCount } from '@/lib/status'
import { formatDateTime, formatDuration, runDuration } from '@/lib/utils'

/**
 * 终态 Task 的「结果」视图（#115 / #116：结论必须带**下一步主动作**）：
 *
 *  1) **结论**：成功/失败一句话（失败要回答：哪个阶段失败、对后续 runner 的影响、下一步点哪里），
 *     并按真实产物能力给出主动作 —— 有报告进报告、只有 result 产物进结果、什么都没有就说明原因；
 *  2) **runner 结果**：每个 runner 的 result.json / result.html / result.csv 三选一内联渲染（D-74）；
 *  3) **产物下载**：清单显式列全，「查看」进整页查看器；打包下载是次级动作。
 *
 * 原始错误码 / 退出码保留为可复制的诊断信息，但不再重复占据结论主体（#116）。
 * 「报告」页签已取消（report.json 只是平台产物之一，D-74）；管道时间线 / attempt / 日志 / 事件在「执行细节」页签。
 */

const KIND_LABEL: Record<Artifact['kind'], string> = {
  report: '报告',
  result: '结果',
  log: '日志',
  raw: '原始数据',
  export: 'exports',
  other: '其它',
}

const KIND_TONE: Record<Artifact['kind'], 'info' | 'ok' | 'muted' | 'warn'> = {
  report: 'info',
  result: 'ok',
  log: 'muted',
  raw: 'warn',
  export: 'info',
  other: 'muted',
}

/** 整页查看器地址（`path` 用 artifacts 返回的 task 目录相对路径；attempt / seq 用于定位）。 */
function viewerUrl(taskId: string, path: string, attempt?: number | null, seq?: number | null): string {
  const params = new URLSearchParams({ path })
  if (attempt != null) params.set('attempt', String(attempt))
  if (seq != null) params.set('seq', String(seq))
  return `/tasks/${encodeURIComponent(taskId)}/file?${params.toString()}`
}

/** `/files?path=` 的直链下载地址（带鉴权走 cookie；`download` 属性触发保存）。 */
function artifactFileUrl(taskId: string, path: string): string {
  return `${API_BASE}/tasks/${encodeURIComponent(taskId)}/files?path=${encodeURIComponent(path)}`
}

/** 失败阶段的展示名：runner 名 + 角色说明，不拿错误码当结论（#116）。 */
function stageOf(runnerName: string, role: string): string {
  switch (role) {
    case 'deploy':
      return `服务部署（${runnerName}）`
    case 'pre_check':
      return `就绪探测 precheck（${runnerName}）`
    case 'clean':
      return `资源清理（${runnerName}）`
    default:
      return `测试 runner「${runnerName}」`
  }
}

export function TaskResultView({ task }: { task: TaskDetail }) {
  const taskId = task.id
  const navigate = useNavigate()
  const artifacts = useAsync(() => api.listTaskArtifacts(taskId), [taskId])
  const [downloadError, setDownloadError] = useState<ApiErrorBody | null>(null)
  const [downloadNotice, setDownloadNotice] = useState<string | null>(null)

  const items = artifacts.data?.items ?? []
  // 当前 attempt 的产物（重试后历史 attempt 的文件按 attempt_no 过滤掉，只展示最新一次）
  const currentItems = useMemo(
    () => items.filter((item) => item.attempt_no == null || item.attempt_no === task.current_attempt),
    [items, task.current_attempt],
  )
  const groups = groupArtifacts(currentItems)
  const runners = task.runners ?? []
  const counts = task.runner_counts

  /* -------- 失败定位（#116：哪个阶段失败 → 失败日志一步可达） -------- */
  const failedRunner = useMemo(() => runners.find((runner) => runner.status === 'failed') ?? null, [runners])
  const failedLogUrl = failedRunner
    ? viewerUrl(taskId, failedRunner.log_ref ?? `attempts/${task.current_attempt}/runners/${failedRunner.seq}/logs/stdout.log`, failedRunner.attempt_no, failedRunner.seq)
    : null

  /* -------- 成功后的主动作（#115：按真实产物能力给入口，不做死按钮） -------- */
  const reportArtifact = useMemo(
    () =>
      currentItems.find((item) => item.kind === 'report' && item.path.toLowerCase().endsWith('report.md')) ??
      currentItems.find((item) => item.kind === 'report') ??
      null,
    [currentItems],
  )
  const resultArtifact = useMemo(() => currentItems.find((item) => item.kind === 'result') ?? null, [currentItems])
  const primaryResultAction = useMemo(() => {
    const target = reportArtifact ?? resultArtifact
    if (!target) return null
    return {
      label: reportArtifact ? '查看报告' : '查看结果',
      href: viewerUrl(taskId, target.path, target.attempt_no, target.runner_seq),
    }
  }, [reportArtifact, resultArtifact, taskId])
  const hasTestRunner = runners.some((runner) => runner.role === 'test' || runner.role === 'pre_check')
  const noResultReason = hasTestRunner
    ? '没有可展示的报告或结果文件：runner 未产出结果产物；原始文件见下方产物下载。'
    : '本次只有部署 / 清理类 runner，没有测试结果产物；部署描述与日志见下方产物下载。'

  /** 打包下载：带鉴权 fetch → Blob → a[download]（dev/OIDC 下裸链会 401，用户只会看到一段 JSON）。 */
  const downloadBundle = async () => {
    setDownloadError(null)
    setDownloadNotice(null)
    try {
      const blob = await api.downloadBundle(taskId)
      const name = `${taskId}.zip`
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = name
      anchor.click()
      URL.revokeObjectURL(url)
      setDownloadNotice(`${name} · ${formatBytes(blob.size)} 已开始下载`)
    } catch (e) {
      setDownloadError(toApiError(e))
    }
  }

  const verdict = verdictOf(task, failedRunner)
  const ended = endedCount(counts)

  return (
    <>
      {/* 1) 结论：一句话 + 影响 + 下一步 + 主动作（#115 / #116） */}
      <Card data-testid="task-result-verdict" className={verdict.tone === 'ok' ? 'border-success/25' : verdict.tone === 'err' ? 'border-danger/25' : ''}>
        <div className={verdict.tone === 'ok' ? 'bg-success-weak/70' : verdict.tone === 'err' ? 'bg-danger-weak/70' : 'bg-surface-2'}>
          <div className="flex flex-wrap items-start gap-4 px-5 py-5">
            <span className={`grid size-10 shrink-0 place-items-center rounded-full ${verdict.tone === 'ok' ? 'bg-success-weak text-success-text' : verdict.tone === 'err' ? 'bg-danger-weak text-danger-text' : 'bg-surface-3 text-fg-3'}`}>
              {verdict.tone === 'ok' ? <CheckCircle2 className="size-5" aria-hidden /> : <AlertTriangle className="size-5" aria-hidden />}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-lg font-semibold text-fg">{verdict.headline}</h2>
                <TaskStatusBadge status={task.status} />
              </div>
              {verdict.explain ? <p className="mt-1 text-sm leading-[22px] text-fg-2">{verdict.explain}</p> : null}
              {verdict.next ? <p className="mt-1 text-xs leading-[20px] text-fg-2">{verdict.next}</p> : null}
              <p className="mt-2 text-2xs text-fg-3">Attempt #{task.current_attempt} · 耗时 {formatDuration(runDuration(task))} · {formatDateTime(task.finished_at)}</p>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2" data-testid="result-actions">
              {verdict.tone === 'ok' && primaryResultAction ? <Button variant="primary" data-testid="result-primary-action" icon={<FileText className="size-4" aria-hidden />} onClick={() => navigate(primaryResultAction.href)}>{primaryResultAction.label}</Button> : null}
              {verdict.tone === 'err' && failedLogUrl ? <Button variant="primary" data-testid="result-primary-action" icon={<FileText className="size-4" aria-hidden />} onClick={() => navigate(failedLogUrl)}>查看失败日志</Button> : null}
              <Button variant="secondary" data-testid="download-bundle" icon={<Download className="size-4" aria-hidden />} onClick={() => void downloadBundle()}>打包下载</Button>
            </div>
          </div>
          {verdict.tone === 'ok' && !primaryResultAction && !artifacts.loading ? <p className="px-5 pb-4 text-xs text-fg-3" data-testid="result-no-action-reason">{noResultReason}</p> : null}
          {verdict.tone === 'err' && !failedLogUrl ? <p className="px-5 pb-4 text-xs text-fg-3" data-testid="result-no-action-reason">没有可定位的失败 runner；部署与 runner 日志在「执行细节」页签。</p> : null}
          {verdict.diagnostic ? (
            <details className="mx-5 mb-4 rounded-md border border-line-soft bg-surface/70 px-3 py-2 text-xs">
              <summary className="cursor-pointer text-fg-3">原始诊断信息</summary>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Badge tone="muted" className="font-mono">{verdict.diagnostic.code}</Badge>
                {verdict.diagnostic.exitCode != null ? <span className="font-mono text-fg-2">exit {verdict.diagnostic.exitCode}</span> : null}
                {verdict.diagnostic.retryable ? <Badge tone="warn">可重试</Badge> : null}
                <span className="min-w-0 flex-1 break-all text-fg-2">{stripErrorCodePrefix(verdict.diagnostic.code, verdict.diagnostic.message)}</span>
              </div>
            </details>
          ) : null}
        </div>
        <dl className="grid grid-cols-2 border-t border-line-soft sm:grid-cols-4">
          {[
            ['已结束', `${ended} / ${counts?.total ?? 0}`],
            ['成功', counts?.succeeded ?? 0],
            ['失败 / 跳过', `${counts?.failed ?? 0} / ${counts?.skipped ?? 0}`],
            ['总耗时', formatDuration(runDuration(task))],
          ].map(([label, value]) => (
            <div key={String(label)} className="border-r border-line-soft px-5 py-4 last:border-r-0">
              <dt className="text-xs text-fg-3">{label}</dt><dd className="mt-1 text-xl font-semibold tabular-nums leading-none text-fg">{value}</dd>
            </div>
          ))}
        </dl>
      </Card>

      {/* 2) runner 结果（deploy/precheck 也要有详情）：全部 runner 一个面板 */}
      <Card data-testid="task-result-render">
        <CardHeader tone="info" title="runner 结果" />
        {runners.length === 0 ? (
          <CardBody>
            <p className="text-xs text-fg-3">还没有 runner 执行记录。</p>
          </CardBody>
        ) : artifacts.error ? (
          <CardBody>
            <ErrorState error={artifacts.error} onRetry={artifacts.reload} compact />
          </CardBody>
        ) : artifacts.loading && !artifacts.data ? (
          <LoadingBlock label="加载产物清单…" />
        ) : (
          <div className="flex flex-col px-5 py-1">
            {runners.map((runner) => (
              <RunnerResultPanel key={runner.seq} taskId={taskId} runner={runner} attemptNo={task.current_attempt} artifacts={currentItems} />
            ))}
          </div>
        )}
      </Card>

      {/* 3) 产物下载：显式列全（report.json / report.md / metadata.json / events.jsonl / 各 runner 日志与 exports / 部署 Pod 日志） */}
      <Card data-testid="task-result-artifacts">
        <CardHeader
          tone="info"
          title="产物下载"
          sub={artifacts.data ? `${currentItems.length} 个文件 · 共 ${formatBytes(artifacts.data.total_size_bytes ?? 0)}` : undefined}
        />
        {downloadError ? (
          <CardBody className="pb-0">
            <ErrorState error={downloadError} onRetry={() => void downloadBundle()} compact />
          </CardBody>
        ) : null}
        {downloadNotice ? (
          <p className="px-5 pt-3 text-2xs text-fg-3" role="status">
            {downloadNotice}
          </p>
        ) : null}
        {artifacts.error ? (
          <CardBody>
            <ErrorState error={artifacts.error} onRetry={artifacts.reload} compact />
          </CardBody>
        ) : artifacts.loading && !artifacts.data ? (
          <LoadingBlock label="加载产物清单…" />
        ) : currentItems.length === 0 ? (
          <CardBody>
            <EmptyState title="还没有产物" description="Task 结束后产物会出现在这里。" />
          </CardBody>
        ) : (
          <div className="flex flex-col gap-2 px-5 py-3" data-testid="artifact-groups">
            {ARTIFACT_GROUPS.map((group) => {
              const rows = groups[group.key]
              // 默认折叠：三组加起来动辄十几个文件，展开会把下面内容全推下去
              return (
                <details
                  key={group.key}
                  data-testid={`artifact-group-${group.key}`}
                  className="group rounded-md border border-line-soft"
                >
                  <summary className="flex list-none cursor-pointer flex-wrap items-center gap-2 px-3 py-2 text-xs">
                    <ChevronRight className="size-3 text-fg-3 transition-transform group-open:rotate-90" aria-hidden />
                    <span className="font-medium text-fg">{group.label}</span>
                    <Badge tone="muted">{rows.length}</Badge>
                  </summary>
                  {rows.length === 0 ? (
                    <p className="border-t border-line-soft px-3 py-2 text-2xs text-fg-3">这一组没有文件</p>
                  ) : (
                    <TableWrap>
                      <Table>
                        <THead>
                          <TR className="hover:bg-transparent">
                            <TH>文件</TH>
                            <TH>类型</TH>
                            <TH>大小</TH>
                            <TH className="text-right">下载</TH>
                          </TR>
                        </THead>
                        <TBody>
                          {rows.map((item) => (
                            <TR key={item.path}>
                              <TD className="break-all font-mono text-2xs text-fg">
                                {relativeArtifactPath(item.path)}
                              </TD>
                              <TD>
                                <Badge tone={KIND_TONE[item.kind]}>{KIND_LABEL[item.kind]}</Badge>
                              </TD>
                              <TD className="tabular-nums text-2xs">{formatBytes(item.size_bytes)}</TD>
                              <TD className="text-right">
                                {/* 整页查看（不弹窗/裸链）：json 树 / csv 表 / html sandbox / 文本（≤2MB） */}
                                {artifactViewKindOf(item.path) !== 'binary' ? (
                                  <button
                                    type="button"
                                    data-testid="artifact-view"
                                    className={buttonClassName({ size: 'sm', variant: 'ghost' })}
                                    onClick={() => navigate(viewerUrl(taskId, item.path, item.attempt_no, item.runner_seq))}
                                  >
                                    查看
                                  </button>
                                ) : null}
                                <a
                                  data-testid="artifact-download"
                                  className={buttonClassName({ size: 'sm', variant: 'ghost' })}
                                  href={artifactFileUrl(taskId, item.path)}
                                  download
                                >
                                  下载
                                </a>
                              </TD>
                            </TR>
                          ))}
                        </TBody>
                      </Table>
                    </TableWrap>
                  )}
                </details>
              )
            })}
          </div>
        )}
      </Card>
    </>
  )
}

/* ---------------------------------------------------------------- 内部 */

interface Verdict {
  tone: 'ok' | 'err' | 'muted'
  headline: string
  explain: string | null
  next: string | null
  diagnostic: { code: string; message: string; retryable: boolean; exitCode: number | null } | null
}

/** 结论行文案（纯函数）：按终态与失败定位给出「阶段 / 影响 / 下一步」（#116），不发明后端没有的根因。 */
function verdictOf(task: TaskDetail, failedRunner: RunnerExecution | null): Verdict {
  const counts = task.runner_counts
  const skipped = counts?.skipped ?? 0
  const impact =
    skipped > 0 ? `对后续 runner 的影响：跳过 ${skipped} 个（前序失败，未执行，不是 0 或成功）。` : null
  switch (task.status) {
    case 'succeeded':
      return {
        tone: 'ok',
        headline: `全部 ${counts?.total ?? 0} 个 runner 通过（attempt #${task.current_attempt}）。`,
        explain: null,
        next: null,
        diagnostic: null,
      }
    case 'failed':
    case 'service_unavailable': {
      const code = task.error?.code ?? failedRunner?.error?.code ?? failedRunner?.error_code ?? 'RUNNER_NONZERO_EXIT'
      const message = task.error?.message ?? failedRunner?.error?.message ?? ''
      const copy = errorCopy(code)
      const stage = failedRunner ? stageOf(failedRunner.name, failedRunner.role) : copy.stage
      const retryable = task.error?.retryable ?? failedRunner?.error?.retryable ?? false
      return {
        tone: 'err',
        headline: `${stage}失败，Task 以「${task.status === 'failed' ? '失败' : '服务不可用'}」结束。`,
        explain: [copy.explain, impact].filter(Boolean).join(' ') || null,
        next: copy.next,
        diagnostic: message
          ? { code, message, retryable, exitCode: failedRunner?.exit_code ?? null }
          : null,
      }
    }
    case 'canceled': {
      const canceledCount = counts?.canceled ?? 0
      return {
        tone: 'muted',
        headline: 'Task 被取消，结果不完整。',
        explain: `${canceledCount > 0 ? `${canceledCount} 个 runner 被终止；` : ''}未执行的 runner 不会补跑；已部署资源由 service-clean 收口。`,
        next: '需要完整结果时用「重试」重新提交（生成新 Task，原 Task 保留）。',
        diagnostic: null,
      }
    }
    default:
      return {
        tone: 'muted',
        headline: `当前状态 ${task.status}。`,
        explain: null,
        next: null,
        diagnostic: null,
      }
  }
}
