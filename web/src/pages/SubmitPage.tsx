import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { AlignLeft, CheckCircle2, Circle, Link2, ListChecks, Play, Plus, Server, Trash2, Wand2 } from 'lucide-react'
import { parse as parseYaml } from 'yaml'
import { api, writeBlockedReason } from '@/api'
import { toApiError } from '@/api/client'
import type { ApiErrorBody, ModelListing, Runner, RunnerSpec, TaskManifest, ValidationIssue } from '@/api/types'
import { RunnerDrawer } from '@/components/app/RunnerDrawer'
import { Badge, Tag } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardBody, CardHeader } from '@/components/ui/card'
import { Field, Input, Select } from '@/components/ui/input'
import { YamlEditor, type YamlEditorError } from '@/components/ui/yaml-editor'
import { JsonlEditor } from '@/components/ui/jsonl-editor'
import { Segmented } from '@/components/ui/segmented'
import { Banner } from '@/components/ui/states'
import { FormErrorSummary } from '@/components/app/FormErrorSummary'
import { PageHeader } from '@/components/app/PageHeader'
import { useToast } from '@/components/ui/toast'
import { useAsync } from '@/hooks/useAsync'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import { useIdentity } from '@/hooks/useIdentity'
import { inputFormatLabel } from '@/lib/runner-format'
import { validateJsonlInput, validateYamlInput, formatJsonl, type InputIssue } from '@/lib/input-validate'
import { cn } from '@/lib/utils'

/**
 * 服务来源两形态（deploy/clean 解耦裁决，XOR 由提交校验把关）——**在 runner 管道卡里作为第 0 步**
 * （用户 2026-11-11：deploy 也是 runner，应放进 runner 列表的框里；选项做成卡片，genet/existing/mock 一眼可见）：
 *  - deploy：**runner 引用**（与 runners[] 同构）——目录里 role=deploy 的 Runner 每个一张卡
 *    （genet-deploy / mock-deploy / existing-deploy / 未来新增，平台不感知具体名字），
 *    输入框按该 runner 的 input_format 给对应编辑器（D-67）；clean 是否注入由该 runner 的
 *    runner.yaml（clean_runner 声明）决定，提交体不出现 clean。
 *  - service：不部署·直接注入（service 块静态三元组 = 原"外接"模式）。
 *
 * 重试 = 带参重新提交（D-76）：`/submit?retry=<taskId>` 把提交原文（GET /tasks/{id}/manifest）
 * 回填进本页，可改后提交 → **生成新 Task（新 ID）**，原 Task 不动（历史 attempt 语义仍在 API 侧，§7.8）。
 */
type PrecheckMode = 'auto' | 'off'

/** runner 列表的一项草稿：**只有一个输入框**（placeholder 来自 runner.input_format 对应片段，D-67）。 */
interface RunnerDraft {
  key: string
  name: string
  runner: string
  input: string
}

interface KeyValueRow {
  key: string
  value: string
}

let draftKey = 0
function newKey(): string {
  draftKey += 1
  return `r${draftKey}`
}

/** D-58：name 类字段上限按 **rune**（中文名 200 个字算 200，不是 600）。 */
const NAME_MAX_RUNES = 200

function runeLength(value: string): number {
  return [...value].length
}

/** 重试回填的名字后缀截断：保住 200 rune 上限（D-58）。 */
function retryName(name: string): string {
  const suffix = '-retry'
  const budget = NAME_MAX_RUNES - [...suffix].length
  const base = [...name].slice(0, Math.max(0, budget)).join('')
  return `${base}${suffix}`
}

/** 把一条被删掉的记录塞回**原位置**（撤销用）。 */
function insertAt<T>(list: readonly T[], index: number, item: T): T[] {
  const next = [...list]
  next.splice(Math.min(Math.max(index, 0), next.length), 0, item)
  return next
}

/**
 * 把 YAML 文本解析成 deploy runner 的输入 —— **只做语法层**校验：
 * 平台不解析部署描述的语义（§10.1），前端也只保证"它是一段合法 YAML 映射"。
 */
function parseDeployInput(text: string): { error: string | null; pos: YamlEditorError | null } {
  if (!text.trim()) return { error: null, pos: null } // 空输入交给后端按 runner 契约校验（400）
  try {
    const parsed = parseYaml(text)
    if (parsed == null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { error: '部署描述必须是一个 YAML 映射（model / replicas / resources…）', pos: { line: 1, col: 1 } }
    }
    return { error: null, pos: null }
  } catch (e) {
    // YAMLParseError 带 linePos[0] = { line, col }：编辑器靠它画红色波浪 + gutter 圆点
    const first = (e as { linePos?: Array<{ line: number; col: number }> }).linePos?.[0]
    return {
      error: `YAML 语法错误：${(e as Error).message}`,
      pos: first ? { line: first.line, col: first.col } : { line: 1, col: 1 },
    }
  }
}

export function SubmitPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const { auth } = useIdentity()
  /** viewer 只读：写按钮置灰 + title 提示（不靠后端 403 兜底） */
  const blocked = writeBlockedReason(auth)

  /** 重试 = 带参重新提交（D-76）：?retry=<taskId> 回填提交原文 */
  const [params] = useSearchParams()
  const retryId = params.get('retry')

  const runners = useAsync(() => api.listRunners(), [])
  const runnerList = runners.data ?? []
  const defaultRunner = runnerList.find((r) => r.role === 'test')?.name ?? ''

  /**
   * 默认值一律留空，只给 placeholder（§11.2：平台不渲染表单、不预填）：
   * 空表单点提交会被**前端**拦下并给字段级错误。
   */
  const [name, setName] = useState('')
  /**
   * deploy runner 选择：null = runner 列表未到（未初始化）；'' = 用户显式选「外接」；
   * 非空 = deploy 模式，提交体是 deploy:{runner, version, input}（runner 引用，与 runners[] 同构）。
   * 列表到达后默认选第一个 input_format=yaml 的 deploy runner（保持"默认部署"入口）。
   */
  const [deployRunnerName, setDeployRunnerName] = useState<string | null>(null)
  /**
   * **每个 deploy runner 一份输入草稿**（互不串台）：切 genet → mock → 回 genet，各自原文都在。
   * 键用 runner 名；未写过的 runner 缺省为空串（不预填，§11.2）。
   */
  const [deployInputs, setDeployInputs] = useState<Record<string, string>>({})
  const [serviceUrl, setServiceUrl] = useState('')
  const [modelName, setModelName] = useState('')
  const [modelPath, setModelPath] = useState('')
  /**
   * 模型路径下拉框（GET /models，2026-11-27）：/models 共享挂载下已有哪些模型。
   * `available:false`（没有挂载点 / 探针失败）不是错误——不渲染下拉框，保留手输框。
   */
  const [modelListing, setModelListing] = useState<ModelListing | null>(null)
  /** 用户显式选了「手动输入…」后不再弹下拉（点了「从列表选择」才回来）。 */
  const [manualPathMode, setManualPathMode] = useState(false)
  const [precheck, setPrecheck] = useState<PrecheckMode>('auto')
  /** 执行环境 profile：RunnerSpec.execution.profile 必填；MVP 没有 /profiles 端点，给可编辑默认值 */
  const [profile, setProfile] = useState('k8s-npu')
  /** 默认 0 行：点「+ 环境变量 / + 凭据引用」才出现（§12.4：Task 级填一次，注入全部 runner） */
  const [envRows, setEnvRows] = useState<KeyValueRow[]>([])
  const [secretRows, setSecretRows] = useState<KeyValueRow[]>([])
  const [runnerDrafts, setRunnerDrafts] = useState<RunnerDraft[]>([{ key: newKey(), name: '', runner: '', input: '' }])

  const [issues, setIssues] = useState<ValidationIssue[]>([])
  const [error, setError] = useState<ApiErrorBody | null>(null)
  const [dryRunning, setDryRunning] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [dryRunOk, setDryRunOk] = useState<boolean | null>(null)

  /** 重试回填状态：成功给一条横幅（可跳原 Task），失败给一条错误横幅 */
  const [retrySource, setRetrySource] = useState<{ id: string; name: string } | null>(null)
  const [retryError, setRetryError] = useState<string | null>(null)
  const retryPrefilled = useRef(false)

  /**
   * 高级配置折叠区（#111）：默认收起；有已填内容（重试回填 / 用户加过行）时自动展开。
   * `advancedTouched` 防止自动展开覆盖用户手动收起的状态。
   */
  const advancedRef = useRef<HTMLDetailsElement | null>(null)
  const advancedTouched = useRef(false)
  useEffect(() => {
    if (advancedTouched.current) return
    if ((envRows.length > 0 || secretRows.length > 0 || precheck === 'off') && advancedRef.current) {
      advancedRef.current.open = true
    }
  }, [envRows.length, secretRows.length, precheck])

  /**
   * 重试回填（D-76）：等 runner 目录到了再填（避免目录未到时 defaultRunner 乱指）。
   * 只回填一次（prefilledRef）；表单之后随便改，提交时生成**新 Task**。
   */
  useEffect(() => {
    if (!retryId || retryPrefilled.current || runners.loading) return
    retryPrefilled.current = true
    api
      .getTaskManifest(retryId)
      .then((snap) => {
        const t = snap.manifest.task
        // D-78：name 可选——原提交没写名字时留空，重试提交同样走平台默认名；
        // 原提交写了名字则回填并追加 -retry 后缀（避免同名混淆，保住 200 rune 上限）。
        setName(t.name ? retryName(t.name) : '')
        if (t.deploy) {
          setDeployInputs((prev) => ({ ...prev, [t.deploy!.runner]: t.deploy!.input ?? '' }))
          setDeployRunnerName(t.deploy!.runner)
        } else {
          // 外接（或历史数据无 deploy）：回填 service 三元组；两者都没有时落外接模式
          setDeployRunnerName('')
          setServiceUrl(t.service?.url ?? '')
          setModelName(t.service?.model_name ?? '')
          setModelPath(t.service?.model_path ?? '')
        }
        setPrecheck(t.precheck === 'off' ? 'off' : 'auto')
        setEnvRows(Object.entries(t.env ?? {}).map(([key, value]) => ({ key, value: String(value ?? '') })))
        setSecretRows(Object.entries(t.secrets ?? {}).map(([key, value]) => ({ key, value: String(value ?? '') })))
        setRunnerDrafts(
          (t.runners ?? []).map((r: RunnerSpec) => ({
            key: newKey(),
            name: r.name ?? '',
            runner: r.runner,
            input: r.input ?? '',
          })),
        )
        const prof = t.runners?.[0]?.execution?.profile
        if (prof) setProfile(prof)
        setRetrySource({ id: snap.task_id, name: t.name ?? '' })
      })
      .catch((e: unknown) => {
        const err = toApiError(e)
        setRetryError(`${err.code}：${err.message}`)
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [retryId, runners.loading])

  /**
   * 模型列表（GET /models）：只在「已有服务」（外接）模式拉取；profile 变更后防抖重拉。
   * 探针按 profile.namespace 定位 PVC，`available:false`（没有挂载点/探针失败）不是错误——
   * 下拉框不渲染，保留手输框。
   */
  const serviceMode = deployRunnerName === ''
  const debouncedProfile = useDebouncedValue(profile.trim(), 400)
  useEffect(() => {
    if (!serviceMode || !debouncedProfile) {
      setModelListing(null)
      return
    }
    let alive = true
    api
      .listModels(debouncedProfile)
      .then((listing) => {
        if (alive) setModelListing(listing)
      })
      .catch(() => {
        if (alive) setModelListing(null)
      })
    return () => {
      alive = false
    }
  }, [serviceMode, debouncedProfile])

  /** 有可选模型且当前值能对上列表（或为空）才渲染下拉；手输模式/取不到列表时回退 Input。 */
  const modelOptions = modelListing?.available ? (modelListing.models ?? []) : []
  const modelMountPath = modelListing?.mount_path ?? ''
  const modelOptionValues = modelOptions.map((m) => `${modelMountPath}/${m}`)
  const showModelSelect =
    !manualPathMode && modelOptions.length > 0 && (modelPath === '' || modelOptionValues.includes(modelPath))

  /**
   * 右栏说明针对**当前 runner**：点 runner 卡片 / 选 Runner / 添加 runner 都会把它设为当前。
   */
  const [activeKey, setActiveKey] = useState<string | null>(null)
  const activeIndex = Math.max(
    0,
    runnerDrafts.findIndex((draft) => draft.key === (activeKey ?? runnerDrafts[0]?.key)),
  )
  const activeDraft = runnerDrafts[activeIndex]

  /** Runner 详情抽屉（与 `/runners?open=` 同一个组件） */
  const [detailRunner, setDetailRunner] = useState<string | null>(null)

  /**
   * 客户端**即时校验**（dry-run 性质，不替代提交时服务端校验，D-67）：
   *  - jsonl：按 Runner 的 parameters 逐行校验类型 + required（+ parameters 里带的 enum 提示），行号级报错；
   *  - yaml：只做语法校验；text：不校验（语义完全归 Runner）。
   */
  const draftIssues = useMemo(() => {
    const map: Record<string, InputIssue[]> = {}
    for (const draft of runnerDrafts) {
      const runner = runnerList.find((r) => r.name === (draft.runner || defaultRunner))
      if (!runner) {
        map[draft.key] = []
        continue
      }
      if (runner.input_format === 'jsonl') map[draft.key] = validateJsonlInput(draft.input, runner.parameters)
      else if (runner.input_format === 'yaml') map[draft.key] = validateYamlInput(draft.input)
      else map[draft.key] = []
    }
    return map
  }, [runnerDrafts, runnerList, defaultRunner])

  /** 「校验」按钮：无问题给一条成功 toast（有问题就地列表 + 编辑器标行）。 */
  const validateDraftInput = (issues: InputIssue[]) => {
    if (issues.length === 0) {
      toast({
        title: '输入校验通过',
        description: '与服务端同一套规则',
        tone: 'ok',
      })
      return
    }
    const first = issues[0]!
    toast({
      title: `发现 ${issues.length} 条输入问题`,
      description: `第 ${first.line} 行${first.field ? ` · ${first.field}` : ''}：${first.message}`,
      tone: 'err',
    })
  }

  const fillDeployExample = () => {
    if (!deployRunnerObj?.placeholder) return
    const previous = deployInput
    setDeployInput(deployRunnerObj.placeholder)
    toast({
      title: '已填入示例', tone: 'ok', durationMs: 7000,
      action: previous.trim() ? { label: '撤销', onClick: () => setDeployInput(previous) } : undefined,
    })
  }

  /** 「填入示例」：覆盖当前输入；已有内容用撤销 toast 兜底。 */
  const fillExample = (draft: RunnerDraft, placeholder: string) => {
    const previous = draft.input
    setRunnerDraft(draft.key, { input: placeholder })
    toast({
      title: '已填入示例',
      tone: 'ok',
      durationMs: 7000,
      action: previous.trim() ? { label: '撤销', onClick: () => setRunnerDraft(draft.key, { input: previous }) } : undefined,
    })
  }

  /** JSONL 逐行格式化 / 压缩（保持一条记录一行；解析失败的行原样保留并提示）。 */
  const rewriteJsonl = (draft: RunnerDraft) => {
    const result = formatJsonl(draft.input)
    setRunnerDraft(draft.key, { input: result.text })
    toast({
      title: '已逐行格式化',
      description: result.keptInvalid > 0 ? `${result.keptInvalid} 行不是合法 JSON，已原样保留` : '全部行重排成功',
      tone: result.keptInvalid > 0 ? 'warn' : 'ok',
    })
  }

  const runnerOptions = runnerList.filter((r) => r.role === 'test')
  const precheckRunner = runnerList.find((r) => r.role === 'pre_check')
  /** deploy 角色的 runner 目录（服务来源下拉的选项；平台不感知具体名字，列表即真相） */
  const deployRunners = runnerList.filter((r) => r.role === 'deploy')
  /** 默认服务来源：genet-deploy（用户裁决 Genet 默认）；目录没有时取第一个 deploy runner */
  const defaultDeployRunnerName = deployRunners.some((r) => r.name === 'genet-deploy')
    ? 'genet-deploy'
    : (deployRunners[0]?.name ?? '')
  const deployRunnerObj = deployRunners.find((r) => r.name === deployRunnerName)
  /** yaml 输入（genet-deploy）→ YAML 编辑器；jsonl（existing-deploy / mock-deploy）→ 普通输入框 */
  const deployIsYaml = deployRunnerObj?.input_format !== 'jsonl'

  /** 当前 deploy runner 的输入草稿（没写过的 = 空串；切 runner 不丢各自草稿） */
  const deployInput = deployRunnerName ? (deployInputs[deployRunnerName] ?? '') : ''
  const setDeployInput = (text: string) => {
    if (!deployRunnerName) return
    setDeployInputs((prev) => ({ ...prev, [deployRunnerName]: text }))
  }

  /** deploy 输入的语法校验只在 yaml 格式的 runner 上做（jsonl 走逐行 schema 校验，D-67） */
  const deployParse = useMemo(
    () => (deployIsYaml ? parseDeployInput(deployInput) : { error: null as string | null, pos: null as YamlEditorError | null }),
    [deployIsYaml, deployInput],
  )
  /** jsonl 格式的 deploy runner（existing-deploy / mock-deploy）逐行校验（与 runner 输入框同规则，D-67） */
  const deployJsonlIssues = useMemo(
    () => (deployIsYaml || !deployRunnerObj ? [] : validateJsonlInput(deployInput, deployRunnerObj.parameters)),
    [deployIsYaml, deployInput, deployRunnerObj],
  )

  /** deploy 输入示例：placeholder 的首行才是纯 jsonl（后几行是人话注释，textarea 灰字只给一行） */
  const deployPlaceholderFirstLine = (deployRunnerObj?.placeholder ?? '').split('\n')[0] ?? ''

  /**
   * 服务来源两形态（#111）：deploy（平台部署）XOR service（已有服务）。
   * 用 Segmented 让互斥关系显式可见；切换时**各自草稿保留**（deployInputs / service 三元组互不清空），
   * 回到平台部署时恢复上一次选中的 deploy runner。
   */
  const lastDeployRef = useRef('')
  useEffect(() => {
    if (deployRunnerName) lastDeployRef.current = deployRunnerName
  }, [deployRunnerName])
  const sourceMode: 'deploy' | 'service' = deployRunnerName === '' ? 'service' : 'deploy'
  const switchSourceMode = (mode: 'deploy' | 'service') => {
    if (mode === 'service') {
      setDeployRunnerName('')
    } else {
      setDeployRunnerName(lastDeployRef.current || defaultDeployRunnerName)
    }
  }

  /**
   * 提交检查区（#111）：三枚 chip 让"还差什么"在长页面任何位置都可见——
   * 服务配置是否完成 / Runner 输入有没有问题 / 校验是否跑过。
   */
  const runnerIssueCount = Object.values(draftIssues).reduce((sum, list) => sum + list.length, 0)
  const deployIssueCount = deployIsYaml ? (deployParse.error ? 1 : 0) : deployJsonlIssues.length
  const serviceReady =
    deployRunnerName === ''
      ? Boolean(serviceUrl.trim())
      : Boolean(deployRunnerObj) && Boolean(deployInput.trim()) && deployIssueCount === 0
  const runnerReady = runnerDrafts.every((draft) => draft.input.trim().length > 0) && runnerIssueCount === 0
  const validateState: 'ok' | 'bad' | 'idle' = dryRunOk === true ? 'ok' : dryRunOk === false ? 'bad' : 'idle'

  // 列表到达后的默认选择：Genet（用户裁决默认服务来源）；没有 deploy runner 时回外接。
  useEffect(() => {
    if (deployRunnerName !== null) return
    if (runners.loading) return
    setDeployRunnerName(defaultDeployRunnerName)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runners.loading, runnerList])

  /**
   * 编辑器标红/滚行用**防抖后**的位置：边打字边滚会把光标甩走。
   * 校验结论（是否可提交）仍用即时的 `deployParse`，所以「停手 250ms」后标注与结论一致。
   */
  const [lintPos, setLintPos] = useState<YamlEditorError | null>(null)
  useEffect(() => {
    const timer = window.setTimeout(() => setLintPos(deployParse.pos), deployParse.pos ? 250 : 0)
    return () => window.clearTimeout(timer)
  }, [deployParse])

  const issueFor = (path: string): string | undefined => issues.find((issue) => issue.path === path)?.message

  /**
   * A4 / B10：两种「点下去也一定失败」的情况，直接在本页拦住（不靠后端 400）：
   *  - 0 个 runner（D-77：允许，只跑 deploy/precheck/clean；注入行不计入）；
   *  - 目录里没有 role=test 的 Runner（deploy / clean 角色不可在 runners[] 引用，D-70）。
   * 目录还在加载时**不算**「没有」：`loading` 期间不置灰，避免把加载中误判成空目录。
   */
  const noRunners = runnerDrafts.length === 0
  const noRunnersRegistered = !runners.loading && runnerOptions.length === 0

  /** C8：按 rune 计数（中文名 200 个字算 200，不是 600） */
  const nameRunes = runeLength(name)
  const nameTooLong = nameRunes > NAME_MAX_RUNES
  const nameError = nameTooLong ? `name 超过 ${NAME_MAX_RUNES} 字符（当前 ${nameRunes}）` : issueFor('task.name')

  /**
   * `task.runners` 这类「整个 runner 列表」级的 issue：就地显示在 runner 区，
   * 而不是只出现在顶部摘要（后端 400 的 path 就是 `task.runners`）。
   */
  const runnerAreaIssues = issues.filter((issue) => /(^|\.)runners$/.test(issue.path))

  const submitBlockers: string[] = []
  if (blocked) submitBlockers.push(blocked)
  if (nameTooLong) submitBlockers.push(`Task 名称超过 ${NAME_MAX_RUNES} 字符（当前 ${nameRunes}）`)
  const submitDisabled = submitBlockers.length > 0
  const submitTitle = submitDisabled ? submitBlockers.join('；') : undefined

  /**
   * 字段级错误：把 `task.name` / `task.runners[0].execution.profile` 这类 path 映射到真实输入框，
   * 用于「摘要里点定位 → 滚过去并聚焦」。
   */
  const fieldRefs = useRef<Record<string, HTMLElement | null>>({})
  const registerField = (path: string) => (el: HTMLElement | null) => {
    fieldRefs.current[path] = el
  }
  const focusField = (path: string) => {
    const el =
      fieldRefs.current[path] ??
      // 退一步：按前缀找（例如 `task.runners[0].runner` 找不到就落回该 runner 那块）
      Object.entries(fieldRefs.current).find(([key]) => key.startsWith(path.split('.').slice(0, 2).join('.')))?.[1]
    if (!el) return
    el.scrollIntoView({ block: 'center', behavior: 'smooth' })
    window.setTimeout(() => el.focus({ preventScroll: true }), 150)
  }

  const setRunnerDraft = (key: string, patch: Partial<RunnerDraft>) => {
    setRunnerDrafts((prev) => prev.map((draft) => (draft.key === key ? { ...draft, ...patch } : draft)))
  }

  /** 复制/点参数 schema：一律追加到「当前 runner」的输入框，已有内容时换行接上（不覆盖）。 */
  const appendToInput = (draftKey: string | undefined, snippet: string) => {
    if (!draftKey) return
    setRunnerDrafts((prev) =>
      prev.map((draft) => {
        if (draft.key !== draftKey) return draft
        const base = draft.input.trimEnd()
        const joined = base
          ? `${base}\n${snippet}`
          : snippet
        return { ...draft, input: joined }
      }),
    )
  }

  const addRunner = () => {
    const key = newKey()
    const nextIndex = runnerDrafts.length + 1
    setRunnerDrafts((prev) => [...prev, { key, name: '', runner: defaultRunner, input: '' }])
    setActiveKey(key)
    window.requestAnimationFrame(() => {
      const row = document.querySelector<HTMLElement>(`[data-testid="runner-row-${nextIndex}"]`)
      row?.scrollIntoView({ block: 'center', behavior: 'smooth' })
      row?.querySelector<HTMLElement>('select')?.focus({ preventScroll: true })
    })
  }

  /**
   * C5：破坏性动作不弹二次确认，删掉后给一个 **undo toast**（7s）。
   * 最后一个 runner 被删时 A4 的置灰立刻生效，撤销回来后按钮也随之恢复。
   */
  const removeKeyValueRow = (kind: 'env' | 'secret', index: number) => {
    const rows = kind === 'env' ? envRows : secretRows
    const removed = rows[index]
    if (!removed) return
    if (kind === 'env') setEnvRows((prev) => prev.filter((_, i) => i !== index))
    else setSecretRows((prev) => prev.filter((_, i) => i !== index))
    const label = removed.key.trim() || (kind === 'env' ? '环境变量' : '凭据引用')
    toast({
      title: `已删除 ${label}`,
      tone: 'warn',
      durationMs: 7000,
      action: {
        label: '撤销',
        onClick: () => {
          if (kind === 'env') setEnvRows((prev) => insertAt(prev, index, removed))
          else setSecretRows((prev) => insertAt(prev, index, removed))
        },
      },
    })
  }

  const removeRunner = (index: number) => {
    const removed = runnerDrafts[index]
    if (!removed) return
    setRunnerDrafts((prev) => prev.filter((item) => item.key !== removed.key))
    toast({
      title: `已删除 runner ${removed.name.trim() || `#${index + 1}`}`,
      tone: 'warn',
      durationMs: 7000,
      action: {
        label: '撤销',
        onClick: () =>
          setRunnerDrafts((prev) =>
            prev.some((item) => item.key === removed.key) ? prev : insertAt(prev, index, removed),
          ),
      },
    })
  }

  const buildManifest = (): TaskManifest | null => {
    if (deployIsYaml && deployParse.error) return null
    const env: Record<string, string> = {}
    for (const row of envRows) {
      if (row.key.trim()) env[row.key.trim()] = row.value
    }
    const secrets: Record<string, string> = {}
    for (const row of secretRows) {
      if (row.key.trim() && row.value.trim()) secrets[row.key.trim()] = row.value.trim()
    }
    // 三元组：外接（service 静态注入）用（url 必填，model_name / model_path 给了才带）
    const serviceTriple = () => ({
      url: serviceUrl.trim(),
      ...(modelName.trim() ? { model_name: modelName.trim() } : {}),
      ...(modelPath.trim() ? { model_path: modelPath.trim() } : {}),
    })

    return {
      task: {
        // D-78：Task 名称可选——留空时不带 name 字段，平台用 Task ID 作默认名
        ...(name.trim() ? { name: name.trim() } : {}),
        // 服务来源的提交体映射（deploy XOR service 由 assemble 校验，§10.1）：
        //   选了 deploy runner → deploy:{runner, version, input}（runner 引用，与 runners[] 同构；
        //     clean 是否注入由该 runner 的 clean_runner 声明决定，提交体不出现 clean）；
        //   外接 → service:{...}（顶层 service = "序号 0 的导出"，静态注入）。
        // 切换形态时各自草稿保留（per-runner 草稿表），只提交当前形态。
        ...(deployRunnerName
          ? {
              deploy: {
                runner: deployRunnerName,
                version: deployRunnerObj?.version ?? '',
                ...(deployInput.trim() ? { input: deployInput } : {}),
              },
            }
          : { service: serviceTriple() }),
        precheck,
        env,
        secrets,
        runners: runnerDrafts.map((draft) => {
          const runnerName = draft.runner || defaultRunner
          const runner = runnerList.find((r) => r.name === runnerName)
          return {
            // D-78：runner 名称可选——留空时不带 name 字段，平台按注册表 Runner 名派生行名
            ...(draft.name.trim() ? { name: draft.name.trim() } : {}),
            runner: runnerName,
            version: runner?.version ?? '',
            execution: { profile: profile.trim() || 'k8s-npu' },
            // 输入原文：按 Runner 的 input_format 原样落档（平台校验后注入 AG_PARAMS_FILE，D-66）
            ...(draft.input.trim() ? { input: draft.input } : {}),
          }
        }),
      },
    }
  }

  const runDryRun = async (manifest: TaskManifest): Promise<boolean> => {
    const result = await api.dryRun(manifest)
    setIssues(result.issues ?? [])
    setDryRunOk(result.valid)
    // 校验结果不走 toast：问题清单进表单内摘要（用户要看到"是哪 N 个问题"）
    return result.valid
  }

  /** 前端校验：同样产出 path 化的 issues，与后端 400 共用一套渲染 */
  const frontendIssues = (): ValidationIssue[] => {
    const found: ValidationIssue[] = []
    if (deployRunnerName) {
      // deploy 模式：runner 引用 + 输入校验（issue.path 与后端一致：task.deploy.runner / task.deploy.input）
      if (!deployRunnerObj) {
        found.push({
          path: 'task.deploy.runner',
          code: 'REQUIRED',
          message: '必须选择 deploy runner',
        })
      } else {
        if (deployIsYaml && deployParse.error) {
          found.push({ path: 'task.deploy.input', code: 'INVALID_YAML', message: deployParse.error })
        }
        const firstJsonlIssue = deployJsonlIssues[0]
        if (firstJsonlIssue) {
          found.push({
            path: 'task.deploy.input',
            code: 'INPUT_INVALID',
            message: `第 ${firstJsonlIssue.line} 行${firstJsonlIssue.field ? ` · ${firstJsonlIssue.field}` : ''}：${firstJsonlIssue.message}`,
          })
        }
      }
    } else if (!serviceUrl.trim()) {
      // 外接模式：service 三元组（后端按同 path 回错）
      found.push({ path: 'task.service.url', code: 'REQUIRED', message: '服务 base URL 必填' })
    }
    // D-58：上限 200 rune（不是字节、不是 UTF-16 单元）；name 本身可选（D-78），留空走平台默认名
    if (runeLength(name.trim()) > NAME_MAX_RUNES) {
      found.push({
        path: 'task.name',
        code: 'TOO_LONG',
        message: `name 超过 ${NAME_MAX_RUNES} 字符（当前 ${runeLength(name.trim())}）`,
      })
    }
    if (!profile.trim()) {
      found.push({
        path: 'task.runners[0].execution.profile',
        code: 'REQUIRED',
        message: 'execution.profile 必填',
      })
    }
    runnerDrafts.forEach((draft, index) => {
      if (!draft.runner && !defaultRunner) {
        found.push({ path: `task.runners[${index}].runner`, code: 'REQUIRED', message: '必须选择 Runner' })
      }
    })
    return found
  }

  const onDryRun = async () => {
    const manifest = buildManifest()
    setError(null)
    const local = frontendIssues()
    if (local.length > 0) {
      setIssues(local)
      setDryRunOk(false)
      return
    }
    if (!manifest) {
      setDryRunOk(false)
      return
    }
    setDryRunning(true)
    try {
      await runDryRun(manifest)
    } catch (e) {
      setError(toApiError(e))
    } finally {
      setDryRunning(false)
    }
  }

  const onSubmit = async () => {
    const manifest = buildManifest()
    setError(null)
    setIssues([])
    const local = frontendIssues()
    if (local.length > 0) {
      setIssues(local)
      setDryRunOk(false)
      return
    }
    if (!manifest) {
      setDryRunOk(false)
      return
    }
    setSubmitting(true)
    try {
      // 提交前先 dry-run：有错就停在表单上，不弹一堆 toast
      const valid = await runDryRun(manifest)
      if (!valid) {
        setSubmitting(false)
        return
      }
      const result = await api.submitTask(manifest)
      /**
       * B8：提交成功**只弹一个** toast（校验通过与已受理不同时出现）。
       * 里面既有结果也有下一步动作：复制 ID / 跳详情。
       */
      const taskId = result.task.id
      toast({
        title: retrySource ? `已生成新 Task ${taskId}（重试自 ${retrySource.id}）` : `已受理 ${taskId}`,
        description: (
          <span className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              data-testid="toast-copy-task-id"
              className="inline-flex h-6 items-center rounded-sm bg-white/15 px-2 text-2xs text-white hover:bg-white/25"
              onClick={() => void navigator.clipboard?.writeText(taskId)}
            >
              复制 ID
            </button>
            <button
              type="button"
              data-testid="toast-open-task"
              className="inline-flex h-6 items-center rounded-sm bg-white/15 px-2 text-2xs text-white hover:bg-white/25"
              onClick={() => navigate(`/tasks/${taskId}`)}
            >
              查看详情
            </button>
          </span>
        ),
        tone: 'ok',
        // ≥8000ms：两个动作都要来得及点；可手动关闭（dismissible 默认 true）
        durationMs: 9000,
      })
      navigate(`/tasks/${taskId}`)
    } catch (e) {
      setError(toApiError(e))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      {/* 重试回填横幅（D-76）：参数来自原 Task 的提交原文，可改；提交生成新 Task，原 Task 不动 */}
      {retrySource ? (
        <div data-testid="retry-banner">
          <Banner
            tone="info"
            title={`重试 ${retrySource.id}：参数已回填，可修改`}
            actions={
              <Link to={`/tasks/${encodeURIComponent(retrySource.id)}`} data-testid="retry-open-source">
                <Button size="sm" variant="secondary">
                  查看原 Task
                </Button>
              </Link>
            }
          >
            提交后生成新 Task，原 Task 保留不动。
          </Banner>
        </div>
      ) : null}
      {retryError ? (
        <div data-testid="retry-banner-error">
          <Banner tone="err" title="重试回填失败">
            {retryError}（原 Task 可能已被清理）；也可以直接
            <Link to={`/tasks/${encodeURIComponent(retryId ?? '')}`} className="ml-1 underline">
              打开原 Task
            </Link>
            手动照抄。
          </Banner>
        </div>
      ) : null}

      {/* 提交/校验失败：表单内摘要（不自动消失、自己滚进视口并聚焦、trace_id 可复制） */}
      <FormErrorSummary error={error} issues={issues} onJumpTo={focusField} />

      {!error && issues.length === 0 && dryRunOk === true ? <Banner tone="info" title="dry-run 通过" /> : null}

      <PageHeader title="提交 Task" />
      <div className="grid min-w-0 items-start gap-5 xl:grid-cols-[minmax(0,1fr)_310px]">
        <div className="flex min-w-0 flex-col gap-5">

      {/* ① 服务来源：平台部署 XOR 已有服务（互斥由提交校验把关，§10.1）；切换保留各自草稿 */}
      <Card data-testid="submit-source-card">
        <CardHeader
          tone="primary"
          title={<span className="flex items-center gap-2"><span className="grid size-6 place-items-center rounded-full bg-primary-weak text-2xs text-primary-text">1</span>选择服务来源</span>}
        />
        <CardBody className="flex flex-col gap-3">
          <div className="grid grid-cols-2 gap-3" data-testid="source-mode" role="group" aria-label="服务来源形态">
            {([
              { mode: 'deploy' as const, title: '平台部署', description: '由 Runner 部署并管理生命周期', icon: Server },
              { mode: 'service' as const, title: '已有服务', description: '直接填写 URL，不执行部署', icon: Link2 },
            ]).map((option) => {
              const active = sourceMode === option.mode
              const Icon = option.icon
              return (
                <button
                  key={option.mode}
                  type="button"
                  aria-pressed={active}
                  onClick={() => switchSourceMode(option.mode)}
                  className={cn(
                    'flex items-start gap-3 rounded-lg border px-4 py-3.5 text-left transition-colors',
                    active ? 'border-primary bg-primary-weak text-fg shadow-xs' : 'border-line bg-surface hover:border-line-strong hover:bg-surface-2',
                  )}
                >
                  <Icon className={cn('mt-0.5 size-4 shrink-0', active ? 'text-primary-text' : 'text-fg-3')} aria-hidden />
                  <span><span className="block text-sm font-medium">{option.title}</span><span className="mt-0.5 block text-2xs text-fg-3">{option.description}</span></span>
                </button>
              )
            })}
          </div>

          <section
            data-testid="source-block"
            className="flex flex-col gap-4"
            aria-label="服务来源"
          >
            {deployRunnerName ? (
              <div className="flex flex-col gap-2">
                <div>
                  <Field label="部署 Runner" required htmlFor="deploy-runner-select" error={issueFor('task.deploy.runner')}>
                    <Select
                      id="deploy-runner-select"
                      aria-label="服务来源 Runner"
                      data-testid="deploy-runner"
                      value={deployRunnerName ?? ''}
                      onChange={(e) => setDeployRunnerName(e.target.value)}
                      className="h-10 w-full text-sm"
                    >
                      {runners.loading && deployRunners.length === 0 ? <option value="">（Runner 目录加载中…）</option> : null}
                      {deployRunners.map((runner: Runner) => (
                        <option key={runner.name} value={runner.name}>
                          {runner.display_name || runner.name}{runner.name === defaultDeployRunnerName ? '（默认）' : ''} · {runner.name} v{runner.version}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  {deployRunnerObj ? (
                    <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-fg-3">
                      <span className="font-mono">{deployRunnerObj.input_format}</span><span aria-hidden>·</span>
                      <span data-testid="deploy-clean-tag">{deployRunnerObj.clean_runner ? `完成后自动清理：${deployRunnerObj.clean_runner}` : '无 clean 阶段'}</span>
                      <Button size="sm" variant="ghost" className="ml-auto -my-1" data-testid="deploy-runner-detail" aria-label="查看服务来源的 Runner 详情" onClick={() => setDetailRunner(deployRunnerObj.name)}>参数与详情</Button>
                    </div>
                  ) : null}
                </div>

                <div className="mt-2 flex items-center gap-2">
                  <span className="text-xs font-medium text-fg-2">{deployIsYaml ? '部署描述 YAML' : 'deploy 输入（JSONL）'} <span className="text-danger-text">*</span></span>
                  {deployRunnerObj?.placeholder ? (
                    <Button size="sm" variant="ghost" className="ml-auto -my-1" data-testid="deploy-fill-example" icon={<Wand2 className="size-3" aria-hidden />} onClick={fillDeployExample}>填入示例</Button>
                  ) : null}
                </div>
                {deployIsYaml ? (
                  <>
                    {/* CodeMirror 6：真语法高亮 + 行号 + 横向滚动；语法错误按行列标行 */}
                    <YamlEditor
                      value={deployInput}
                      onChange={setDeployInput}
                      rows={7}
                      aria-label="部署描述 YAML"
                      title="deploy.input"
                      error={lintPos}
                    />
                    {deployParse.error && deployInput.trim() ? (
                      <p
                        data-testid="deploy-yaml-error"
                        data-state="error"
                        role="alert"
                        className="text-xs text-danger-text"
                      >
                        {deployParse.pos ? `第 ${deployParse.pos.line} 行第 ${deployParse.pos.col} 列：` : ''}
                        {deployParse.error}
                      </p>
                    ) : null}
                  </>
                ) : (
                  <>
                    <Field
                      error={issueFor('task.deploy.input')}
                    >
                      <JsonlEditor
                        value={deployInput}
                        onChange={setDeployInput}
                        rows={7}
                        aria-label="deploy runner 输入"
                        mirrorLabel="deploy runner 输入"
                        testId="deploy-jsonl-editor"
                        placeholder={deployPlaceholderFirstLine}
                        title="deploy.input"
                        error={deployJsonlIssues[0] ? { line: deployJsonlIssues[0].line } : null}
                      />
                    </Field>
                    {deployJsonlIssues.length > 0 ? (
                      <ul
                        data-testid="deploy-input-issues"
                        role="alert"
                        className="flex flex-col gap-0.5 rounded-md border border-danger/25 bg-danger-weak px-2.5 py-1.5"
                      >
                        {deployJsonlIssues.slice(0, 8).map((issue, issueIndex) => (
                          <li key={`${issue.line}-${issue.field ?? issue.kind}-${issueIndex}`}>
                            <span className="text-xs leading-[20px] text-danger-text">
                              第 {issue.line} 行
                              {issue.field ? <code className="mx-1 font-mono">{issue.field}</code> : null}· {issue.message}
                            </span>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </>
                )}
              </div>
            ) : (
              <div className="flex flex-col gap-4">
                <div className="grid gap-4 sm:grid-cols-3">
                <Field label="服务 base URL" required error={issueFor('task.service.url')} className="sm:col-span-2">
                  <Input
                    ref={registerField('task.service.url')}
                    value={serviceUrl}
                    onChange={(e) => setServiceUrl(e.target.value)}
                    placeholder="http://10.0.0.10:8000/v1"
                    aria-invalid={Boolean(issueFor('task.service.url'))}
                  />
                </Field>
                <Field label="模型名">
                  <Input value={modelName} onChange={(e) => setModelName(e.target.value)} placeholder="Qwen3-32B" />
                </Field>
                <Field
                  label="模型路径（可选）"
                  className="sm:col-span-3"
                  hint={
                    showModelSelect
                      ? `来自 ${modelMountPath} 挂载（探针 5 分钟缓存）；没有合适的也可以手动输入`
                      : undefined
                  }
                >
                  {showModelSelect ? (
                    <div className="flex items-center gap-2">
                      <Select
                        aria-label="选择 /models 下的模型"
                        data-testid="model-path-select"
                        value={modelOptionValues.includes(modelPath) ? modelPath : ''}
                        onChange={(e) => {
                          const value = e.target.value
                          if (value === '__manual__') {
                            setManualPathMode(true)
                            return
                          }
                          setModelPath(value)
                        }}
                      >
                        <option value="">选择已有模型…</option>
                        {modelOptions.map((m) => (
                          <option key={m} value={`${modelMountPath}/${m}`}>
                            {m}
                          </option>
                        ))}
                        <option value="__manual__">手动输入…</option>
                      </Select>
                      {modelPath ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          data-testid="model-path-clear"
                          aria-label="清空模型路径"
                          onClick={() => setModelPath('')}
                        >
                          清空
                        </Button>
                      ) : null}
                    </div>
                  ) : (
                    <div className="flex items-center gap-2">
                      <Input
                        value={modelPath}
                        onChange={(e) => setModelPath(e.target.value)}
                        placeholder="/models/Qwen3-32B"
                        aria-label="模型路径"
                      />
                      {modelOptions.length > 0 ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          data-testid="model-path-back-to-list"
                          onClick={() => setManualPathMode(false)}
                        >
                          从列表选择
                        </Button>
                      ) : null}
                    </div>
                  )}
                </Field>
                </div>
              </div>
            )}
          </section>
        </CardBody>
      </Card>

      {/*
        ② 测试配置（#111 的视觉主体）：Task 名称 + runner 行。
        管道顺序（deploy → precheck(auto) → runners → clean）作为说明保留；
        precheck / 执行环境 / env / 凭据归入③高级配置，不与主路径抢注意力。
      */}
      <Card data-testid="submit-runners-card">
        <CardHeader
          tone="primary"
          title={<span className="flex items-center gap-2"><span className="grid size-6 place-items-center rounded-full bg-primary-weak text-2xs text-primary-text">2</span>配置测试</span>}
        />
        <CardBody className="flex flex-col gap-3">
          {/*
            C8：maxlength=200（后端 D-58，按 rune 上限）+ 实时计数。
            粘贴可以绕过 maxlength，所以超限时这里也会立刻报红并把「提交」置灰（不等后端 400）。
          */}
          <Field
            label="Task 名称（可选）"
            htmlFor="task-name"
            error={nameError}
          >
            <div id="task-name-row" className="flex items-center gap-2">
              <Input
                id="task-name"
                ref={registerField('task.name')}
                value={name}
                maxLength={NAME_MAX_RUNES}
                onChange={(e) => setName(e.target.value)}
                placeholder="如 qwen3-32b-daily"
                aria-invalid={Boolean(nameError)}
                className="flex-1"
              />
              {nameRunes > 0 ? (
                <span
                  data-testid="task-name-counter"
                  aria-live="polite"
                  className={cn('shrink-0 font-mono text-2xs tabular-nums', nameTooLong ? 'font-medium text-danger-text' : 'text-fg-3')}
                >
                  已输入 {nameRunes} / {NAME_MAX_RUNES}
                </span>
              ) : null}
            </div>
          </Field>

          {/* ---------- runner 行 ---------- */}
          {/* A4：0 个 runner 时说清「为什么不能提交」，就在会被删空的那块下面 */}
          {noRunners ? (
            <p
              data-testid="runner-empty-hint"
              className="rounded-md border border-line-soft bg-surface-2 px-3 py-2 text-2xs leading-[16px] text-fg-3"
            >
              未添加测试 runner：本次只执行 服务来源 → precheck（auto 时）→ clean，适合验证服务能否启动
            </p>
          ) : null}

          {/* B10：只有内置角色时，下拉里只剩一句占位，提交/校验都不该放开 */}
          {noRunnersRegistered ? (
            <p
              data-testid="runner-empty-registered"
              className="rounded-md border border-line-soft bg-surface-2 px-3 py-2 text-2xs leading-[16px] text-fg-3"
            >
              目录里暂无测试类 Runner；可只提交服务来源 + precheck（auto 注入）
            </p>
          ) : null}

          {/* 后端仍可能返回 `task.runners` 级的 issue（并发/边界）：就地显示在 runner 区 */}
          {runnerAreaIssues.length > 0 ? (
            <div
              data-testid="runner-area-issues"
              role="alert"
              className="rounded-md border border-danger/30 bg-danger-weak px-3 py-2"
            >
              {runnerAreaIssues.map((issue, index) => (
                <p key={`${issue.path}-${index}`} className="text-2xs leading-[16px] text-danger-text">
                  <code className="mr-1.5 font-mono">{issue.path}</code>
                  {issue.message}
                </p>
              ))}
            </div>
          ) : null}

          {runnerDrafts.map((draft, index) => {
            const resolvedRunner = runnerList.find((runner) => runner.name === (draft.runner || defaultRunner))
            const nameIssue = issueFor(`task.runners[${index}].name`)
            return (
              <div
                key={draft.key}
                data-testid={`runner-row-${index + 1}`}
                className={cn(
                  'rounded-md border p-3 transition-colors duration-150',
                  activeDraft?.key === draft.key ? 'border-primary/40 bg-primary-weak/30' : 'border-line-soft',
                )}
                onClick={() => setActiveKey(draft.key)}
                onFocusCapture={() => setActiveKey(draft.key)}
              >
                <div className="mb-3 flex items-center gap-2">
                  <span className="grid size-5 shrink-0 place-items-center rounded-full bg-primary-weak text-2xs text-primary-text">{index + 1}</span>
                  <span className="text-xs font-medium text-fg-2">Runner {index + 1}</span>
                  {issues.some((issue) => issue.path.startsWith(`task.runners[${index}]`)) ? <Badge tone="err">有校验问题</Badge> : null}
                  <Button size="sm" variant="ghost" className="ml-auto -my-1" aria-label={`移除 runner ${index + 1}`} data-testid={`remove-runner-${index + 1}`} onClick={() => removeRunner(index)}>移除</Button>
                </div>
                <div className="grid items-end gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
                  <Field label="Runner 名称（可选）">
                    <Input
                      value={draft.name}
                      aria-label={`runner ${index + 1} 名称`}
                      aria-invalid={Boolean(nameIssue)}
                      title={nameIssue ?? undefined}
                      placeholder="如 accuracy-dataset-a"
                      onChange={(e) => setRunnerDraft(draft.key, { name: e.target.value })}
                    />
                  </Field>
                  <Field label="测试 Runner" required>
                    <Select
                      value={draft.runner || defaultRunner}
                      aria-label={`runner ${index + 1} Runner`}
                      onChange={(e) => {
                        setActiveKey(draft.key)
                        setRunnerDraft(draft.key, { runner: e.target.value })
                      }}
                    >
                      {runnerOptions.length === 0 ? <option value="">（还没有 Runner）</option> : null}
                      {runnerOptions.map((runner) => <option key={runner.name} value={runner.name}>{runner.name} v{runner.version}</option>)}
                    </Select>
                  </Field>
                  <Button
                    size="sm" variant="ghost" className="mb-0.5" data-testid="runner-runner-detail" data-runner-index={index + 1}
                    disabled={!resolvedRunner}
                    title={resolvedRunner ? `查看 ${resolvedRunner.name} 的参数与镜像` : '还没有可选的 Runner'}
                    aria-label={`查看 runner ${index + 1} 的 Runner 详情`}
                    onClick={() => { if (!resolvedRunner) return; setActiveKey(draft.key); setDetailRunner(resolvedRunner.name) }}
                  >参数与详情</Button>
                </div>
                {resolvedRunner ? <p className="mt-2 text-2xs text-fg-3">{resolvedRunner.input_format} · {resolvedRunner.on_failure}{resolvedRunner.exclusive ? ' · 独占 Endpoint' : ''}</p> : null}
                {nameIssue ? (
                  <p data-testid={`runner-name-error-${index + 1}`} role="alert" className="mt-1.5 text-2xs text-danger-text">
                    {nameIssue}
                  </p>
                ) : null}
                <div className="mt-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-2xs text-fg-3">
                      Runner 输入{resolvedRunner ? `（${inputFormatLabel(resolvedRunner.input_format)}）` : ''} <span className="text-danger-text">*</span>
                    </span>
                    {/* 就地小按钮组（校验 / 格式化 / 压缩）：点按就地生效、行内报错；仅客户端 dry-run，最终以提交时服务端校验为准 */}
                    <Button
                      size="sm"
                      variant="ghost"
                      data-testid={`input-validate-${index + 1}`}
                      icon={<ListChecks className="size-3" aria-hidden />}
                      onClick={() => validateDraftInput(draftIssues[draft.key] ?? [])}
                    >
                      校验
                    </Button>
                    {resolvedRunner?.input_format === 'jsonl' ? (
                      <>
                        <Button
                          size="sm"
                          variant="ghost"
                          data-testid={`input-format-${index + 1}`}
                          icon={<AlignLeft className="size-3" aria-hidden />}
                          onClick={() => rewriteJsonl(draft)}
                        >
                          格式化
                        </Button>
                      </>
                    ) : null}
                    {resolvedRunner?.placeholder ? (
                      <Button
                        size="sm" variant="ghost" data-testid={`input-${index + 1}-fill-example`}
                        icon={<Wand2 className="size-3" aria-hidden />}
                        onClick={() => fillExample(draft, resolvedRunner.placeholder ?? '')}
                      >填入示例</Button>
                    ) : null}
                  </div>
                  <Field
                    className="mt-1"
                    error={issueFor(`task.runners[${index}].input`) ?? issueFor(`task.runners[${index}].runner`)}
                  >
                    <JsonlEditor
                      value={draft.input}
                      onChange={(next) => setRunnerDraft(draft.key, { input: next })}
                      rows={7}
                      aria-label={`runner ${index + 1} 输入`}
                      testId={`jsonl-editor-${index + 1}`}
                      placeholder={resolvedRunner?.placeholder?.split('\n')[0] ?? '{"dataset":"/data/gsm8k.jsonl","limit":200}'}
                      title={`${draft.name.trim() || `runner ${index + 1}`} 的输入`}
                      error={draftIssues[draft.key]?.[0] ? { line: draftIssues[draft.key]![0]!.line } : null}
                    />
                  </Field>
                  {/* 行内报错（行号 + 字段名 + 原因）：点一条跳到那一行；规则与服务端 D-67 一致 */}
                  {(draftIssues[draft.key]?.length ?? 0) > 0 ? (
                    <ul
                      data-testid={`input-issues-${index + 1}`}
                      role="alert"
                      className="mt-1 flex flex-col gap-0.5 rounded-md border border-danger/25 bg-danger-weak px-2.5 py-1.5"
                    >
                      {(draftIssues[draft.key] ?? []).slice(0, 8).map((issue, issueIndex) => (
                        <li key={`${issue.line}-${issue.field ?? issue.kind}-${issueIndex}`}>
                          <span className="text-left text-2xs leading-[16px] text-danger-text">
                            第 {issue.line} 行
                            {issue.field ? <code className="mx-1 font-mono">{issue.field}</code> : null}· {issue.message}
                          </span>
                        </li>
                      ))}
                      {(draftIssues[draft.key]?.length ?? 0) > 8 ? (
                        <li className="text-2xs text-danger-text">还有 {(draftIssues[draft.key] ?? []).length - 8} 条…</li>
                      ) : null}
                    </ul>
                  ) : null}
                </div>
              </div>
            )
          })}
          <Button
            variant="secondary"
            data-testid="add-runner"
            className="mt-1 w-full border-dashed bg-surface-2/60 py-5 text-fg-3 hover:text-fg"
            icon={<Plus className="size-4" aria-hidden />}
            onClick={addRunner}
          >
            {runnerDrafts.length === 0 ? '添加 Runner' : '添加另一个 Runner'}
          </Button>
        </CardBody>
      </Card>

      {/*
        ③ 高级配置（#111）：环境变量、凭据、precheck、执行环境弱化为折叠分组。
        能力一个不少（§12.4 Task 级注入、precheck auto/off、execution.profile）；
        有已填内容（如重试回填）时自动展开，避免"藏了东西"的错觉。
      */}
      <Card data-testid="submit-advanced-card">
        <CardBody className="p-0">
          <details ref={advancedRef} onToggle={() => (advancedTouched.current = true)} data-testid="advanced-section">
            <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 px-5 py-4 text-sm font-medium text-fg-2 [&::-webkit-details-marker]:hidden">
              <span className="text-fg-3 transition-transform" aria-hidden>▸</span>
              高级配置
              {envRows.length + secretRows.length > 0 ? <Badge tone="muted">已填 {envRows.length + secretRows.length} 项</Badge> : null}
            </summary>

            <div className="flex flex-col gap-5 border-t border-line-soft px-5 py-5">
              {/* 环境变量与凭据：Task 粒度，注入该 Task 下全部 runner（§12.4） */}
              <section className="flex flex-col gap-2" aria-label="环境变量与凭据">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-sm font-semibold text-fg">环境变量与凭据</h3>
                  <span className="ml-auto flex items-center gap-2">
                    <Button
                      size="sm"
                      variant="secondary"
                      data-testid="add-env"
                      icon={<Plus className="size-3" aria-hidden />}
                      onClick={() => setEnvRows((p) => [...p, { key: '', value: '' }])}
                    >
                      环境变量
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      data-testid="add-secret"
                      icon={<Plus className="size-3" aria-hidden />}
                      onClick={() => setSecretRows((p) => [...p, { key: '', value: '' }])}
                    >
                      凭据引用
                    </Button>
                  </span>
                </div>
                {envRows.map((row, index) => (
                  <div key={`env-${index}`} className="flex items-center gap-2">
                    <Input
                      value={row.key}
                      aria-label={`env 键 ${index + 1}`}
                      placeholder="LOG_LEVEL"
                      className="w-48"
                      onChange={(e) =>
                        setEnvRows((prev) => prev.map((item, i) => (i === index ? { ...item, key: e.target.value } : item)))
                      }
                    />
                    <Input
                      value={row.value}
                      aria-label={`env 值 ${index + 1}`}
                      placeholder="info"
                      onChange={(e) =>
                        setEnvRows((prev) => prev.map((item, i) => (i === index ? { ...item, value: e.target.value } : item)))
                      }
                    />
                    <Button size="sm" variant="ghost" aria-label="删除该环境变量" onClick={() => removeKeyValueRow('env', index)}>
                      <Trash2 className="size-3.5" aria-hidden />
                    </Button>
                  </div>
                ))}
                {secretRows.map((row, index) => (
                  <div key={`secret-${index}`} className="flex items-center gap-2">
                    <Input
                      value={row.key}
                      aria-label={`secret 键 ${index + 1}`}
                      placeholder="ENDPOINT_API_KEY"
                      className="w-48"
                      onChange={(e) =>
                        setSecretRows((prev) => prev.map((item, i) => (i === index ? { ...item, key: e.target.value } : item)))
                      }
                    />
                    <Input
                      value={row.value}
                      aria-label={`secret 引用 ${index + 1}`}
                      placeholder="endpoint/prod-qwen"
                      onChange={(e) =>
                        setSecretRows((prev) => prev.map((item, i) => (i === index ? { ...item, value: e.target.value } : item)))
                      }
                    />
                    <Button size="sm" variant="ghost" aria-label="删除该凭据引用" onClick={() => removeKeyValueRow('secret', index)}>
                      <Trash2 className="size-3.5" aria-hidden />
                    </Button>
                  </div>
                ))}
              </section>

              {/* precheck 与执行环境（原卡头散件归拢到这里） */}
              <section className="flex flex-col gap-2" aria-label="precheck 与执行环境">
                <h3 className="text-sm font-semibold text-fg">precheck 与执行环境</h3>
                <div className="flex flex-wrap items-center gap-3">
                  <span className="flex items-center gap-2">
                    <span className="text-xs text-fg-3">precheck</span>
                    <Segmented<PrecheckMode>
                      ariaLabel="precheck 模式"
                      value={precheck}
                      onChange={setPrecheck}
                      options={[
                        { value: 'auto', label: 'auto 注入' },
                        { value: 'off', label: 'off' },
                      ]}
                    />
                  </span>
                  <span className="flex items-center gap-2">
                    <span className="text-xs text-fg-3">执行环境</span>
                    <Input
                      ref={registerField('task.runners[0].execution.profile')}
                      value={profile}
                      onChange={(e) => setProfile(e.target.value)}
                      aria-label="执行环境 profile"
                      aria-invalid={Boolean(issueFor('task.runners[0].execution.profile'))}
                      title={issueFor('task.runners[0].execution.profile') ?? undefined}
                      placeholder="k8s-npu"
                      className="h-9 w-40 font-mono text-xs"
                    />
                  </span>
                </div>
                {precheck === 'auto' ? (
                  <div className="rounded-md border border-line-soft bg-surface-2 px-3 py-2 text-xs leading-[20px] text-fg-2">
                    <span className="font-medium text-fg">precheck 自动注入：</span>
                    {precheckRunner ? (
                      <>
                        <Tag mono className="ml-2">
                          {precheckRunner.name} · {precheckRunner.version}
                        </Tag>
                        <Button
                          size="sm"
                          variant="ghost"
                          data-testid="precheck-detail"
                          aria-label="查看 precheck 的 Runner 详情"
                          className="ml-1"
                          onClick={() => setDetailRunner(precheckRunner.name)}
                        >
                          详情
                        </Button>
                      </>
                    ) : null}
                  </div>
                ) : (
                  <p className="text-xs leading-[20px] text-fg-3">precheck 关闭：跳过就绪探测，runner 直接对服务执行。</p>
                )}
              </section>
            </div>
          </details>
        </CardBody>
      </Card>

        </div>

        <Card data-testid="submit-footer" className="sticky top-4">
          <CardHeader title="提交检查" />
          <CardBody className="flex flex-col gap-4">
            {[
              {
                id: 'service', done: serviceReady, title: '服务配置',
                detail: deployRunnerName === ''
                  ? (serviceUrl.trim() ? '已有服务 · URL 已填写' : '已有服务 · URL 未填写')
                  : (serviceReady ? `平台部署 ${deployRunnerObj?.name ?? ''}` : '部署描述尚未填写'),
              },
              {
                id: 'runners', done: runnerReady, title: '测试输入',
                detail: runnerDrafts.length === 0 ? '未添加（只验证服务）' : !runnerReady && runnerIssueCount === 0 ? 'Runner 输入尚未填写' : `${runnerDrafts.length} 行${runnerIssueCount > 0 ? ` · ${runnerIssueCount} 条问题` : ' · 已填写'}`,
              },
              {
                id: 'validate', done: validateState === 'ok', title: '提交校验',
                detail: validateState === 'ok' ? '校验通过' : validateState === 'bad' ? `${issues.length} 条问题待处理` : '尚未校验',
              },
            ].map((item) => (
              <div key={item.id} data-testid={`submit-check-${item.id}`} className="flex items-start gap-2.5">
                {item.done ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success-text" aria-hidden /> : <Circle className="mt-0.5 size-4 shrink-0 text-fg-3" aria-hidden />}
                <div><p className="text-xs font-medium text-fg">{item.title}</p><p className="mt-0.5 text-2xs text-fg-3">{item.detail}</p></div>
              </div>
            ))}
            {blocked || submitDisabled ? <p className="text-2xs text-danger-text">{blocked ?? submitTitle}</p> : null}
            <div className="grid grid-cols-[1fr_1.35fr] gap-2 border-t border-line-soft pt-4">
              <Button data-testid="validate-task" variant="secondary" loading={dryRunning} disabled={submitDisabled} aria-disabled={submitDisabled} title={submitTitle} onClick={() => void onDryRun()}>校验</Button>
              <Button variant="primary" data-testid="submit-task" loading={submitting} disabled={submitDisabled} aria-disabled={submitDisabled} title={submitTitle} icon={<Play className="size-3.5" aria-hidden />} onClick={() => void onSubmit()}>提交 Task</Button>
            </div>
          </CardBody>
        </Card>
      </div>

      {/* 与 Runner 目录页是同一个抽屉组件：参数 schema 里也能直接复制进当前 runner 的输入框 */}
      <RunnerDrawer
        name={detailRunner}
        onClose={() => setDetailRunner(null)}
        onCopyParam={(snippet) => appendToInput(activeDraft?.key, snippet)}
      />
    </>
  )
}
