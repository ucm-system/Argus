/**
 * 失败错误码 → 人类可读说明（#116）。
 *
 * 规则（按任务约束）：
 *  - 只依据**后端已有的错误类型**（附录 D / api/types.ts 的 ErrorCode）给出说明，
 *    不猜测"鉴权 / 网络 / 配置就是根因"——后端没给的结论一个字都不编；
 *  - 原始错误码、退出码、trace_id 保留为可复制诊断信息，但不占据结论主体；
 *  - `explain` 与 `next` 是两段独立文案：前者回答"发生了什么"，后者回答"下一步点什么"。
 */

export interface ErrorCopy {
  /** 一句话结论（跟随阶段名拼接后进主结论行） */
  stage: string
  /** 发生了什么（含"对后续 runner 的影响"由调用方按 skipped 数拼接） */
  explain: string
  /** 下一步动作（一句话，指向页面上真实存在的入口） */
  next: string
}

const COPY: Record<string, ErrorCopy> = {
  DEPLOY_FAILED: {
    stage: '服务部署失败',
    explain: '部署 runner 以失败退出，服务没有就绪，本次测试没有执行。',
    next: '先看部署 runner 日志里的失败原因（部署方返回 / 参数问题），修正部署描述后重试。',
  },
  DEPLOY_BLOCKED: {
    stage: '服务部署被阻塞',
    explain: '部署方长时间没有给出可用服务（排队超窗或被阻塞），本次测试没有执行。',
    next: '看部署节点上的阻塞原因与水位；确认资源窗口后重试，或先取消避免占位。',
  },
  ENDPOINT_UNREACHABLE: {
    stage: '被测服务不可达',
    explain: '就绪探测在等待窗口内一直连不上服务，后续 runner 未执行。',
    next: '确认服务地址与网络连通后重试；如果是部署模式，部署日志里有服务实际状态。',
  },
  PRECHECK_FAILED: {
    stage: '就绪探测（precheck）未通过',
    explain: '服务没有通过可用性检查，测试 runner 未执行。日志里的 HTTP 状态能区分"还没就绪"与"配置/鉴权不对"。',
    next: '查看失败日志：503 / 超时 → 等服务就绪后重试；401 / 403 → 检查凭据引用与鉴权配置。',
  },
  RUNNER_NONZERO_EXIT: {
    stage: '测试 runner 异常退出',
    explain: 'runner 进程以非零码退出（执行中出错），失败策略之后的 runner 可能被跳过。',
    next: '查看该 runner 的失败日志定位报错；修正输入或环境后重试。',
  },
  RUNNER_TIMEOUT: {
    stage: '测试 runner 超时',
    explain: '执行超过时限被平台终止，结果不完整。',
    next: '查看日志确认卡在哪一步；调大 timeout_seconds 或减小负载后重试。',
  },
  RESULT_MISSING: {
    stage: '结果产物缺失',
    explain: 'runner 正常退出但没有产出结果文件，无法生成报告。',
    next: '查看日志确认 runner 的输出目录行为；检查 Runner 版本后重试。',
  },
  RESULT_INVALID: {
    stage: '结果产物不合法',
    explain: 'runner 产出了结果文件，但内容不符合结果 schema，无法进入报告。',
    next: '查看日志与产物原文；对照附录 A 修正 Runner 输出后重试。',
  },
  EXPORT_INVALID: {
    stage: '导出（exports）不合规',
    explain: 'runner 退出码为 0，但导出行解析失败，下游没有拿到注入参数。',
    next: '查看该 runner 日志里的导出内容；修正 exports 格式后重试。',
  },
  IMAGE_PULL_FAILED: {
    stage: '镜像拉取失败',
    explain: '执行环境拿不到 runner 镜像，这个 runner 没有跑起来。',
    next: '确认镜像引用与仓库凭据后重试；持续失败联系平台管理员。',
  },
  MOUNT_UNAVAILABLE: {
    stage: '存储挂载不可用',
    explain: '执行环境声明的挂载不存在，runner 无法启动。',
    next: '检查执行环境 Profile 的挂载配置；修正后重新提交。',
  },
  EXECUTOR_UNAVAILABLE: {
    stage: '执行层暂不可用',
    explain: '平台暂时无法在集群上创建或查询执行任务，这是平台侧故障，不是测试失败。',
    next: '稍后重试；持续出现联系平台管理员（附 trace_id）。',
  },
  EXECUTOR_POD_ERROR: {
    stage: '执行 Pod 异常',
    explain: 'runner Pod 在集群侧异常退出（如被驱逐 / OOM），不是测试用例本身的失败。',
    next: '查看日志与集群事件确认原因后重试。',
  },
  CANCELED: {
    stage: '已被取消',
    explain: '有人主动取消了这个 Task，未执行的 runner 不会补跑。',
    next: '需要结果时用「重试」重新提交（生成新 Task）。',
  },
  INTERNAL_ERROR: {
    stage: '平台内部错误',
    explain: '平台侧出现未分类错误，状态以事件流为准。',
    next: '复制 trace_id 反馈给平台管理员排查。',
  },
}

/** 兜底：后端出现了映射表之外的码 —— 只说事实，不猜根因。 */
const FALLBACK: ErrorCopy = {
  stage: '执行失败',
  explain: 'runner 管道在执行中出现失败，具体原因见诊断信息与日志。',
  next: '查看失败日志定位原因；需要时重试或联系平台管理员。',
}

export function errorCopy(code: string | null | undefined): ErrorCopy {
  if (code && COPY[code]) return COPY[code]!
  return FALLBACK
}
