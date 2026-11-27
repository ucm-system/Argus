package api

// GET /api/v1/models 集成测试（真 PG + 假探针；没设 AG_TEST_DATABASE_URL 就 skip）。
//
// 探针本体（modelprobe.Prober）需要真 K8s，这里用 fake 验证**解析层**：
// profile 参数、/models 挂载解析（mount_path 命中优先于别名）、探针失败/未接线
// 的 available:false 降级——下拉框是增强，不是依赖。

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"testing"
	"time"

	"github.com/ucm-system/testplane/server/internal/domain"
	"github.com/ucm-system/testplane/server/internal/modelprobe"
)

// fakeProber 是 ModelProber 的假实现（记录调用并回放预设结果）。
type fakeProber struct {
	listing modelprobe.Listing
	err     error
	calls   int
}

func (f *fakeProber) List(ctx context.Context, namespace, claimName string) (modelprobe.Listing, error) {
	f.calls++
	return f.listing, f.err
}

func TestListModels_ParamAndResolution(t *testing.T) {
	e := newAdminTestEnvWith(t, nil)
	subject := "admin"

	// 造一个 Profile（挂在种子集群上，namespace=argus）。
	prof := uniqueAdminName("mlm-prof")
	rec := e.do(http.MethodPost, "/api/v1/admin/profiles",
		fmt.Sprintf(`{"name":%q,"cluster":"cluster-01","namespace":"argus"}`, prof), subject)
	requireStatus(t, rec, http.StatusCreated)

	// 1) 缺 ?profile= → 400。
	rec = e.do(http.MethodGet, "/api/v1/models", "", subject)
	requireError(t, rec, http.StatusBadRequest, "VALIDATION_ERROR")

	// 2) profile 不存在 → 400。
	rec = e.do(http.MethodGet, "/api/v1/models?profile=no-such-profile", "", subject)
	body := requireError(t, rec, http.StatusBadRequest, "VALIDATION_ERROR")
	if detailOf(t, body)["field"] != "profile" {
		t.Fatalf("detail.field 应为 profile: %v", body["detail"])
	}

	// 3) 没有 /models 挂载 → 200 available:false（不是错误）。
	rec = e.do(http.MethodGet, "/api/v1/models?profile="+prof, "", subject)
	got := requireStatus(t, rec, http.StatusOK)
	if got["available"] != false || got["reason"] != "model_dir_not_mounted" {
		t.Fatalf("无挂载应 available:false + model_dir_not_mounted: %v", got)
	}
}

func TestListModels_WithProbe(t *testing.T) {
	probe := &fakeProber{listing: modelprobe.Listing{
		Models:   []string{"Qwen3-32B", "Qwen3-0.6B"},
		ProbedAt: time.Date(2026, 11, 27, 10, 0, 0, 0, time.UTC),
	}}
	mounts := []domain.SharedMountSpec{
		{Name: "data", MountPath: "/data", ClaimName: "data-pvc"},
		{Name: "models", MountPath: "/models", AliasPaths: []string{"/mnt/model"}, ClaimName: "models-pvc"},
	}
	e := newAdminTestEnvWith(t, func(d *Deps) {
		d.ModelProbe = probe
		d.SharedMountsFn = func(ctx context.Context) ([]domain.SharedMountSpec, error) { return mounts, nil }
	})
	subject := "admin"

	prof := uniqueAdminName("mlp-prof")
	rec := e.do(http.MethodPost, "/api/v1/admin/profiles",
		fmt.Sprintf(`{"name":%q,"cluster":"cluster-01","namespace":"argus"}`, prof), subject)
	requireStatus(t, rec, http.StatusCreated)

	rec = e.do(http.MethodGet, "/api/v1/models?profile="+prof, "", subject)
	got := requireStatus(t, rec, http.StatusOK)
	if got["available"] != true {
		t.Fatalf("available 应为 true: %v", got)
	}
	if got["claim_name"] != "models-pvc" || got["mount_path"] != "/models" {
		t.Fatalf("应回显命中的挂载: %v", got)
	}
	models, _ := got["models"].([]any)
	if len(models) != 2 || models[0] != "Qwen3-32B" {
		t.Fatalf("models = %v", models)
	}
	if probe.calls != 1 {
		t.Fatalf("探针应被调用一次，实际 %d", probe.calls)
	}
}

func TestListModels_ProbeFailureDegrades(t *testing.T) {
	probe := &fakeProber{err: errors.New("boom")}
	mounts := []domain.SharedMountSpec{
		{Name: "models", MountPath: "/models", ClaimName: "models-pvc"},
	}
	e := newAdminTestEnvWith(t, func(d *Deps) {
		d.ModelProbe = probe
		d.SharedMountsFn = func(ctx context.Context) ([]domain.SharedMountSpec, error) { return mounts, nil }
	})
	subject := "admin"

	prof := uniqueAdminName("mlf-prof")
	rec := e.do(http.MethodPost, "/api/v1/admin/profiles",
		fmt.Sprintf(`{"name":%q,"cluster":"cluster-01","namespace":"argus"}`, prof), subject)
	requireStatus(t, rec, http.StatusCreated)

	// 探针失败 → 200 available:false（下拉框降级手输，不挡提交）。
	rec = e.do(http.MethodGet, "/api/v1/models?profile="+prof, "", subject)
	got := requireStatus(t, rec, http.StatusOK)
	if got["available"] != false || got["reason"] != "probe_failed" {
		t.Fatalf("探针失败应 available:false + probe_failed: %v", got)
	}
}
