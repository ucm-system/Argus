import { useMemo } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '@/api'
import type { Artifact } from '@/api/types'
import { ArtifactViewerDialog, artifactViewKindOf } from '@/components/app/ArtifactViewer'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { EmptyState } from '@/components/ui/states'
import { useAsync } from '@/hooks/useAsync'
import { relativeArtifactPath } from '@/lib/artifact-groups'
import { TaskDetailPage } from '@/pages/TaskDetailPage'

/**
 * Task 上下文的文件弹窗（`/tasks/:taskId/file?path=&attempt=&seq=`）。
 *
 * 保留原 URL，因而刷新、收藏和分享链接仍可用；背景始终是该 Task 的详情，而不是孤立页面。
 * 所有现有 `/file` 链接因此自动获得相同的弹窗行为。关闭只退出文件上下文，不丢失 Task。
 */
export function FileViewerPage() {
  const { taskId = '' } = useParams<{ taskId: string }>()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const path = searchParams.get('path') ?? ''
  const attempt = searchParams.get('attempt')
  const seq = searchParams.get('seq')
  const close = () => navigate(`/tasks/${encodeURIComponent(taskId)}`, { replace: true })

  const artifacts = useAsync(() => api.listTaskArtifacts(taskId), [taskId])
  const artifact: Artifact = useMemo(() => {
    const found = (artifacts.data?.items ?? []).find((item) => relativeArtifactPath(item.path) === relativeArtifactPath(path))
    if (found) return found
    return {
      path,
      kind: artifactViewKindOf(path) === 'binary' ? 'other' : 'raw',
      size_bytes: 0,
      attempt_no: attempt != null && attempt !== '' ? Number(attempt) : null,
      runner_seq: seq != null && seq !== '' ? Number(seq) : null,
    }
  }, [artifacts.data, path, attempt, seq])

  return (
    <>
      <TaskDetailPage />
      {path.trim() ? (
        <ArtifactViewerDialog taskId={taskId} artifact={artifact} onClose={close} />
      ) : (
        <Dialog open onClose={close} title="无法查看文件" description="链接中缺少 path 参数。">
          <EmptyState
            title="没有指定文件"
            action={<Button variant="primary" onClick={close}>返回 Task 详情</Button>}
          />
        </Dialog>
      )}
    </>
  )
}
