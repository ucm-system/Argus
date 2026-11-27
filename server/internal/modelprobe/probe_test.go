package modelprobe

// 探针 Job manifest 的单元测试（不需要真 K8s）。
//
// 关键不变量：
//   - **不带** `argus.io/managed` label —— GC 只回收 managed 对象，探针靠
//     ttlSecondsAfterFinished 自清（gc.go 规则 2 会把"managed 且库里无执行记录"的
//     Job 当孤儿删掉，探针绝不能被它误伤）；
//   - 安全上下文 restricted（非 root、drop ALL、seccomp）；
//   - 同 (namespace, claim) 派生同名 Job（幂等），不同 key 不撞名。

import (
	"strings"
	"testing"
)

func TestJobManifestShape(t *testing.T) {
	p := New(nil, "", 0, nil)
	job := p.jobManifest("argus", "models-pvc")

	if job.Name != JobPrefix+"-"+jobSuffix("argus", "models-pvc") {
		t.Fatalf("job name = %s", job.Name)
	}
	if job.Namespace != "argus" {
		t.Fatalf("namespace = %s", job.Namespace)
	}
	if _, managed := job.Labels["argus.io/managed"]; managed {
		t.Fatal("探针 Job 不许带 argus.io/managed label（GC 会把它当孤儿删除）")
	}
	if job.Spec.TTLSecondsAfterFinished == nil || *job.Spec.TTLSecondsAfterFinished != ttlAfterFinishedSeconds {
		t.Fatal("必须带 ttlSecondsAfterFinished 自清")
	}
	if job.Spec.BackoffLimit == nil || *job.Spec.BackoffLimit != 0 {
		t.Fatal("backoffLimit 应为 0（失败快暴露）")
	}
	pod := job.Spec.Template.Spec
	if pod.AutomountServiceAccountToken != nil && *pod.AutomountServiceAccountToken {
		t.Fatal("探针不需要 ServiceAccount token")
	}
	if len(pod.Volumes) != 1 || pod.Volumes[0].PersistentVolumeClaim == nil ||
		pod.Volumes[0].PersistentVolumeClaim.ClaimName != "models-pvc" {
		t.Fatalf("应 RO 挂 models-pvc: %+v", pod.Volumes)
	}
	c := pod.Containers[0]
	if c.Image != "alpine:3.20" {
		t.Fatalf("默认镜像 = %s", c.Image)
	}
	if !strings.Contains(strings.Join(c.Args, " "), probeMountPath) {
		t.Fatalf("listing 命令应指向 %s: %v", probeMountPath, c.Args)
	}
	if c.SecurityContext == nil || c.SecurityContext.RunAsNonRoot == nil || !*c.SecurityContext.RunAsNonRoot {
		t.Fatal("探针必须非 root（restricted PSA）")
	}
}

func TestJobSuffix(t *testing.T) {
	a := jobSuffix("argus", "models-pvc")
	b := jobSuffix("argus", "models-pvc")
	if a != b {
		t.Fatalf("同 key 应同名: %s vs %s", a, b)
	}
	if a == jobSuffix("argus", "other-pvc") || a == jobSuffix("ns2", "models-pvc") {
		t.Fatal("不同 key 不应撞名")
	}
	if len(a) != 8 {
		t.Fatalf("后缀长度 = %d, want 8", len(a))
	}
}
