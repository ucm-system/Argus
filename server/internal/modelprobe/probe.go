// Package modelprobe 用一次性探针 Job 列出共享挂载 PVC 里的模型目录。
//
// 为什么是探针 Job：模型目录在集群 PVC 里，控制面本身不挂这些卷（§10.4：平台不创建
// PV/PVC，也不解析存储协议），所以"列出 /models 下已有哪些模型"只能在集群里做——
// 起一个几十秒生命周期的 Job（RO 挂 claim，ls 一把，读 Pod 日志），用完即弃。
//
// 与 storage-smoke（deploy/k8s/storage-smoke-job.yaml）同一套安全上下文：restricted
// PSA 下非 root、drop ALL、seccomp RuntimeDefault。探针 Job **不带** `argus.io/managed`
// label —— GC 只回收 managed 对象，探针靠 ttlSecondsAfterFinished 自清（gc.go §6.3）。
package modelprobe

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

const (
	// JobPrefix 是探针 Job 名前缀；不含 argus.io/managed label，GC 不会碰它。
	JobPrefix = "argus-model-probe"
	// probeDeadline 是从建 Job 到读到日志的总预算（下拉框是交互请求，不能久等）。
	probeDeadline = 45 * time.Second
	// pollInterval 是 Job 终态轮询间隔。
	pollInterval = 600 * time.Millisecond
	// ttlAfterFinished 让探针 Job 在终态后 2 分钟自清（兜底，正常路径读完即删）。
	ttlAfterFinishedSeconds = 120
	// probeMountPath 是 claim 在探针容器内的挂载点（与业务 mount_path 无关）。
	probeMountPath = "/mnt/probe"
	// probeContainer 是探针容器名。
	probeContainer = "probe"
)

// Listing 是一次目录列举的结果（ProbedAt 取探测完成时间，缓存命中时可能早于本次请求）。
type Listing struct {
	Models   []string  `json:"models"`
	ProbedAt time.Time `json:"probed_at"`
}

// Prober 列出 namespace 里 claim 根目录下的**子目录名**（模型约定一个目录一个模型）。
// 带 TTL 缓存 + 单飞：模型目录很少变化，同 (namespace, claim) 短时间内的重复请求不重建 Job。
type Prober struct {
	client kubernetes.Interface
	image  string
	ttl    time.Duration
	logger *slog.Logger
	now    func() time.Time

	mu       sync.Mutex
	cache    map[string]Listing
	inflight map[string]*probeCall
}

// New 构造探针。image 为空用默认 alpine:3.20（与 storage-smoke 同源）；ttl ≤ 0 用 5m。
func New(client kubernetes.Interface, image string, ttl time.Duration, logger *slog.Logger) *Prober {
	if image == "" {
		image = "alpine:3.20"
	}
	if ttl <= 0 {
		ttl = 5 * time.Minute
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &Prober{
		client:   client,
		image:    image,
		ttl:      ttl,
		logger:   logger,
		now:      time.Now,
		cache:    map[string]Listing{},
		inflight: map[string]*probeCall{},
	}
}

// probeCall 是同 key 的并发请求共享物（单飞：只建一个 Job）。
type probeCall struct {
	done chan struct{}
	list Listing
	err  error
}

// cacheKey 是缓存键：claim 名在 namespace 内才唯一。
func cacheKey(namespace, claimName string) string {
	return namespace + "/" + claimName
}

// List 返回 claim 根目录下的子目录名。缓存命中直接返回；未命中走单飞探测。
func (p *Prober) List(ctx context.Context, namespace, claimName string) (Listing, error) {
	key := cacheKey(namespace, claimName)

	p.mu.Lock()
	if hit, ok := p.cache[key]; ok && p.now().Sub(hit.ProbedAt) < p.ttl {
		p.mu.Unlock()
		return hit, nil
	}
	if call, ok := p.inflight[key]; ok {
		p.mu.Unlock()
		select {
		case <-call.done:
			return call.list, call.err
		case <-ctx.Done():
			return Listing{}, ctx.Err()
		}
	}
	call := &probeCall{done: make(chan struct{})}
	p.inflight[key] = call
	p.mu.Unlock()

	list, err := p.probe(ctx, namespace, claimName)

	p.mu.Lock()
	delete(p.inflight, key)
	if err == nil {
		p.cache[key] = list
	}
	p.mu.Unlock()

	// 先赋值再关 done：等待方醒来时结果必须已经可见。
	call.list = list
	call.err = err
	close(call.done)
	return list, err
}

// probe 建 Job → 等终态 → 读日志 → 解析目录名 → 删 Job（尽力而为）。
func (p *Prober) probe(ctx context.Context, namespace, claimName string) (Listing, error) {
	ctx, cancel := context.WithTimeout(ctx, probeDeadline)
	defer cancel()

	job := p.jobManifest(namespace, claimName)
	jobs := p.client.BatchV1().Jobs(namespace)
	// 幂等：同名残Job（上次异常退出没删干净）先删再建，保证读到的是本轮日志。
	delErr := jobs.Delete(ctx, job.Name, metav1.DeleteOptions{})
	if delErr != nil && !apierrors.IsNotFound(delErr) {
		return Listing{}, fmt.Errorf("modelprobe: delete stale job: %w", delErr)
	}
	if _, err := jobs.Create(ctx, job, metav1.CreateOptions{}); err != nil {
		// 重试一遍：Delete 是最终一致传播，刚删完立刻建可能 409。
		if _, err2 := jobs.Create(ctx, job, metav1.CreateOptions{}); err2 != nil {
			return Listing{}, fmt.Errorf("modelprobe: create job: %w", err2)
		}
	}
	defer func() {
		bg := context.WithoutCancel(ctx)
		if err := jobs.Delete(bg, job.Name, metav1.DeleteOptions{}); err != nil && !apierrors.IsNotFound(err) {
			p.logger.Warn("modelprobe: 清理探针 Job 失败（ttlSecondsAfterFinished 会兜底）",
				"job", job.Name, "err", err)
		}
	}()

	// 等终态（轮询；探针是低频交互请求，不引入 watch）。
	deadline := p.now().Add(probeDeadline)
	for {
		j, err := jobs.Get(ctx, job.Name, metav1.GetOptions{})
		if err != nil {
			return Listing{}, fmt.Errorf("modelprobe: get job: %w", err)
		}
		if complete, failed := jobTerminal(j); complete || failed {
			if failed {
				return Listing{}, fmt.Errorf("modelprobe: probe job failed（PVC 不存在或镜像拉不动，看 Job 事件）")
			}
			break
		}
		if p.now().After(deadline) {
			return Listing{}, fmt.Errorf("modelprobe: probe job 超时（>%s）", probeDeadline)
		}
		select {
		case <-ctx.Done():
			return Listing{}, ctx.Err()
		case <-time.After(pollInterval):
		}
	}

	models, err := p.readModels(ctx, namespace, job.Name)
	if err != nil {
		return Listing{}, err
	}
	return Listing{Models: models, ProbedAt: p.now().UTC()}, nil
}

// jobTerminal 投影 Job 终态（Complete / Failed condition）。
func jobTerminal(j *batchv1.Job) (complete, failed bool) {
	for _, c := range j.Status.Conditions {
		switch c.Type {
		case batchv1.JobComplete:
			if c.Status == corev1.ConditionTrue {
				complete = true
			}
		case batchv1.JobFailed:
			if c.Status == corev1.ConditionTrue {
				failed = true
			}
		}
	}
	return complete, failed
}

// readModels 按 job-name label 找探针 Pod 并读日志（每行一个目录名）。
func (p *Prober) readModels(ctx context.Context, namespace, jobName string) ([]string, error) {
	pods, err := p.client.CoreV1().Pods(namespace).List(ctx, metav1.ListOptions{
		LabelSelector: "job-name=" + jobName,
	})
	if err != nil {
		return nil, fmt.Errorf("modelprobe: list probe pods: %w", err)
	}
	if len(pods.Items) == 0 {
		return nil, fmt.Errorf("modelprobe: probe pod 未找到（job=%s）", jobName)
	}
	req := p.client.CoreV1().Pods(namespace).GetLogs(pods.Items[0].Name, &corev1.PodLogOptions{
		Container: probeContainer,
	})
	stream, err := req.Stream(ctx)
	if err != nil {
		return nil, fmt.Errorf("modelprobe: read probe logs: %w", err)
	}
	defer stream.Close()

	models := make([]string, 0, 16)
	scanner := bufio.NewScanner(stream)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line != "" {
			models = append(models, line)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("modelprobe: scan probe logs: %w", err)
	}
	return models, nil
}

// jobManifest 渲染探针 Job。安全上下文与 Runner Job 对齐（restricted：非 root、
// drop ALL、seccomp）；listing 命令只列子目录（busybox sh 的 glob 不含点开头项）。
func (p *Prober) jobManifest(namespace, claimName string) *batchv1.Job {
	name := JobPrefix + "-" + jobSuffix(namespace, claimName)
	automount := false
	ro := true
	ttl := int32(ttlAfterFinishedSeconds)
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
			Labels: map[string]string{
				"app.kubernetes.io/name":    JobPrefix,
				"app.kubernetes.io/part-of": "argus",
				"argus.io/component":        "model-probe",
				"argus.io/claim":            claimName,
			},
		},
		Spec: batchv1.JobSpec{
			BackoffLimit:            new(int32), // 0：失败快暴露，不做重试
			TTLSecondsAfterFinished: &ttl,
			ActiveDeadlineSeconds:   ptr(int64(probeDeadline.Seconds())),
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: map[string]string{
						"app.kubernetes.io/name":    JobPrefix,
						"app.kubernetes.io/part-of": "argus",
						"argus.io/component":        "model-probe",
					},
				},
				Spec: corev1.PodSpec{
					RestartPolicy:                 corev1.RestartPolicyNever,
					AutomountServiceAccountToken:  &automount,
					EnableServiceLinks:            new(bool),
					TerminationGracePeriodSeconds: ptr(int64(10)),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot: ptr(true),
						RunAsUser:    ptr(int64(10001)),
						RunAsGroup:   ptr(int64(10001)),
						FSGroup:      ptr(int64(10001)),
						SeccompProfile: &corev1.SeccompProfile{
							Type: corev1.SeccompProfileTypeRuntimeDefault,
						},
					},
					Containers: []corev1.Container{{
						Name:            probeContainer,
						Image:           p.image,
						ImagePullPolicy: corev1.PullIfNotPresent,
						Command:         []string{"/bin/sh", "-c"},
						Args: []string{
							`for entry in ` + probeMountPath + `/* ; do [ -d "$entry" ] && basename "$entry" ; done`,
						},
						SecurityContext: &corev1.SecurityContext{
							AllowPrivilegeEscalation: ptr(false),
							Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
							RunAsNonRoot:             ptr(true),
							RunAsUser:                ptr(int64(10001)),
						},
						VolumeMounts: []corev1.VolumeMount{{
							Name: "models", MountPath: probeMountPath, ReadOnly: ro,
						}},
					}},
					Volumes: []corev1.Volume{{
						Name: "models",
						VolumeSource: corev1.VolumeSource{
							PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
								ClaimName: claimName,
								ReadOnly:  ro,
							},
						},
					}},
				},
			},
		},
	}
}

// jobSuffix 从 (namespace, claim) 派生 8 位稳定后缀：同 key 同名（幂等），不同 key 不撞。
func jobSuffix(namespace, claimName string) string {
	sum := sha256.Sum256([]byte(namespace + "\x00" + claimName))
	return hex.EncodeToString(sum[:])[:8]
}

func ptr[T any](v T) *T { return &v }
