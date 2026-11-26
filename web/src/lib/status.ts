/**
 * 状态 → 中文标签 + 语义色（schema 2.0 的状态机；§7.1/§7.4）。
 *
 * 纪律：
 *  - 状态 token 一律小写（跟 API 一致），UI 展示中文；
 *  - 状态不只靠颜色（design/impl/web-and-delivery.md §6）：Badge 一律「文字 + 色」。
 *  - v2：`completed_with_errors` 已并入 `failed`（D-73）；Task 没有 deploying/testing/cleanup 平台阶段。
 */
import type { AttemptStatus, DeployState, RunnerStatus, TaskStatus, WaitingReason } from '@/api/types'
import { formatDuration } from '@/lib/utils'

export type Tone = 'ok' | 'warn' | 'err' | 'info' | 'primary' | 'muted'

export interface StatusMeta {
  label: string
  tone: Tone
}

export const TASK_STATUS_META: Record<TaskStatus, StatusMeta> = {
  submitted: { label: '已提交', tone: 'muted' },
  queued: { label: '排队中', tone: 'muted' },
  running: { label: '执行中', tone: 'info' },
  succeeded: { label: '成功', tone: 'ok' },
  failed: { label: '失败', tone: 'err' },
  service_unavailable: { label: '服务不可用', tone: 'err' },
  cancelling: { label: '取消中', tone: 'warn' },
  canceled: { label: '已取消', tone: 'muted' },
}

/** runner 状态机 8 值（§7.1，D-68/D-69）：FAILED 是终态，没有重试回路。 */
export const RUNNER_STATUS_META: Record<RunnerStatus, StatusMeta> = {
  pending: { label: '待执行', tone: 'muted' },
  queued: { label: '排队中', tone: 'muted' },
  preparing: { label: '准备中', tone: 'info' },
  running: { label: '执行中', tone: 'info' },
  succeeded: { label: '成功', tone: 'ok' },
  failed: { label: '失败', tone: 'err' },
  skipped: { label: '跳过', tone: 'warn' },
  canceled: { label: '已取消', tone: 'muted' },
}

/** attempt 聚合状态（Task 级；同一 Task 至多一个活跃 attempt）。 */
export const ATTEMPT_STATUS_META: Record<AttemptStatus, StatusMeta> = {
  running: { label: '执行中', tone: 'info' },
  succeeded: { label: '成功', tone: 'ok' },
  failed: { label: '失败', tone: 'err' },
  canceled: { label: '已取消', tone: 'muted' },
}

/** deploy-state.json 的 phase（D-70）：queued/accepted 是平台推断的阶段，以 Genet 实际进度为准。 */
export const DEPLOY_STATE_META: Record<DeployState, StatusMeta> = {
  queued: { label: '排队中', tone: 'muted' },
  accepted: { label: '已受理', tone: 'info' },
  deploying: { label: '部署中', tone: 'info' },
  ready: { label: '就绪', tone: 'ok' },
  blocked: { label: '被阻塞', tone: 'err' },
}

/** 排队原因的中文枚举（v2 4 值，D-32/D-69：precheck_gate / global_limit 已取消）。 */
export const WAITING_REASON_CN: Record<WaitingReason, string> = {
  slot_waiting: '端点被占用或平台并发已满',
  queue_full: '队列已满',
  insufficient_resources: '资源不足',
  gang_pending: 'gang 未满足',
}

export function waitingReasonLabel(reason: string | null | undefined): string {
  if (!reason) return ''
  return WAITING_REASON_CN[reason as WaitingReason] ?? reason
}

export function taskStatusMeta(status: string): StatusMeta {
  return TASK_STATUS_META[status as TaskStatus] ?? { label: status, tone: 'muted' }
}

export function runnerStatusMeta(status: string): StatusMeta {
  return RUNNER_STATUS_META[status as RunnerStatus] ?? { label: status, tone: 'muted' }
}

export function attemptStatusMeta(status: string): StatusMeta {
  return ATTEMPT_STATUS_META[status as AttemptStatus] ?? { label: status, tone: 'muted' }
}

export function deployStateMeta(state: string | null | undefined): StatusMeta {
  if (!state) return { label: '未启动', tone: 'muted' }
  return DEPLOY_STATE_META[state as DeployState] ?? { label: state, tone: 'muted' }
}

/** Task 是否终态（§7.4：succeeded / failed / service_unavailable / canceled；终态后不再订阅事件、不显示取消按钮）。 */
export function isTerminalTask(status: string): boolean {
  return ['succeeded', 'failed', 'service_unavailable', 'canceled'].includes(status)
}

/* ---------------------------------------------------------------- 进度口径（#114） */

/** Task 投影里的 runner 计数摘要（列表项与详情共用）。 */
export type RunnerCountsLike = {
  total: number
  succeeded: number
  failed: number
  pending?: number
  running?: number
  skipped?: number
  canceled?: number
}

/**
 * 已结束（已执行完毕）的 runner 数 = succeeded + failed + skipped + canceled。
 * 这是「执行进度」的分子（#114）：与领域状态机的终态一一对应，详情页与列表同口径。
 */
export function endedCount(counts: RunnerCountsLike | null | undefined): number {
  if (!counts) return 0
  return counts.succeeded + counts.failed + (counts.skipped ?? 0) + (counts.canceled ?? 0)
}

/** 结果段：非零才出现，顺序按扫读优先级（失败最先）。 */
export interface ResultSegment {
  key: 'failed' | 'succeeded' | 'skipped' | 'canceled' | 'running' | 'pending'
  label: string
  count: number
}

/** 把 runner 计数拆成「执行结果」段（#114：结果 ≠ 进度，失败必须直接可见）。 */
export function resultSegments(counts: RunnerCountsLike | null | undefined): ResultSegment[] {
  if (!counts) return []
  const segs: ResultSegment[] = [
    { key: 'failed', label: '失败', count: counts.failed },
    { key: 'succeeded', label: '成功', count: counts.succeeded },
    { key: 'skipped', label: '跳过', count: counts.skipped ?? 0 },
    { key: 'canceled', label: '取消', count: counts.canceled ?? 0 },
    { key: 'running', label: '执行中', count: counts.running ?? 0 },
    { key: 'pending', label: '待执行', count: counts.pending ?? 0 },
  ]
  return segs.filter((segment) => segment.count > 0)
}

/**
 * 执行进度主文案（列表「进度 / 结果」列与详情 KV 共用，#114）：
 *  - 运行中优先暴露「卡在哪一层」：部署排队（deploy phase=queued）> 执行中 > 未开始；
 *  - 终态给「已结束 E / T」；没有已结束 runner 时不显示 `0 / N`（避免误读成执行了一半）。
 */
export function progressHeadline(input: {
  status: string
  counts?: RunnerCountsLike | null
  deployPhase?: string | null
  deployWaitedSeconds?: number | null
}): string {
  const { status, counts, deployPhase, deployWaitedSeconds } = input
  if (!counts || counts.total === 0) {
    return status === 'running' ? '还没有 runner 执行记录' : '未开始'
  }
  const deployQueued = status === 'running' && deployPhase === 'queued'
  if (deployQueued) {
    const waited = deployWaitedSeconds != null ? ` · 已等待 ${formatDuration(deployWaitedSeconds)}` : ''
    return `部署排队中${waited}`
  }
  const ended = endedCount(counts)
  if (ended === 0) {
    if (status === 'running') {
      // 管道已启动但还没有 runner 落终态（首个 runner 在跑 / 在准备）
      return '执行中 · 尚无 runner 结束'
    }
    return '未开始'
  }
  const endedText = `已结束 ${ended} / ${counts.total}`
  if (isTerminalTask(status)) return endedText
  if (status === 'running') return `${endedText} · 执行中`
  if (status === 'cancelling') return `${endedText} · 取消中`
  return endedText
}

/** Task 可否重试（§7.8：前置为终态；取消收口中的 cancelling 不行）。 */
export function isRetryableTask(status: string): boolean {
  return isTerminalTask(status)
}

/** 终态语义说明（列表页右栏图例）。 */
export const TERMINAL_LEGEND: Array<{ status: TaskStatus; desc: string }> = [
  { status: 'succeeded', desc: 'runner 管道全部走完且无失败' },
  { status: 'failed', desc: '有 runner 失败' },
  { status: 'service_unavailable', desc: '服务没起来，找部署方' },
  { status: 'canceled', desc: '人主动停的，不算失败' },
]

/** 事件类型 → 中文（事件时间线用；07 §7 / §12.6 的 v2 集合）。 */
export const EVENT_TYPE_CN: Record<string, string> = {
  submitted: 'Task 提交',
  state_changed: 'Task 状态变化',
  runner_started: 'runner 开始执行',
  runner_finished: 'runner 执行结束',
  exports_harvested: 'exports 已收集',
  clean_finished: '清理完成',
  cleanup_failed: '清理失败',
  task_finished: 'Task 结束',
  'report.written': '报告已写',
  resync_required: '需要重建',
}
