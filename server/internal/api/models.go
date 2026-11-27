package api

// 模型目录列表（GET /api/v1/models）：提交页"模型路径"下拉框的数据源。
//
// 语义（07 §5.3）：
//   - 数据来自**统一共享挂载**里挂到 /models（或其别名）的 PVC——平台不登记模型，
//     目录列举用一次性探针 Job（modelprobe）在 profile.namespace 里现做；
//   - `?profile=` 必填：claim 是 namespace 作用域对象，探针要在 runner Job 的
//     同一 namespace 里跑才能看到同一个 PVC；
//   - 没有 /models 挂载、探针未接线、探针失败 → 一律 200 `available:false`：
//     下拉框是**增强**不是依赖，前端拿不到列表就回退手输框，绝不挡提交。
//
// 边界（§10.4：路径对平台是不透明字符串）：这里只列举目录名，不校验模型内容、
// 不解释模型格式；`model_path` 的语义归 Runner。

import (
	"context"
	"errors"
	"net/http"

	"github.com/ucm-system/testplane/server/internal/domain"
	"github.com/ucm-system/testplane/server/internal/modelprobe"
)

// ModelProber 列出 namespace 里 claim 根目录下的模型目录（实现：modelprobe.Prober）。
// 结果类型用 modelprobe.Listing（ProbedAt 供前端展示新鲜度）。
type ModelProber interface {
	List(ctx context.Context, namespace, claimName string) (modelprobe.Listing, error)
}

// modelsMountPath 是模型目录在容器内的约定挂载点（D-63 默认值）。
const modelsMountPath = "/models"

// handleListModels 是 GET /api/v1/models?profile=<name>（viewer+）。
func (s *Server) handleListModels(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	profileName := r.URL.Query().Get("profile")
	if profileName == "" {
		s.rejectValidation(w, r, "list_models", "model_listing", "",
			"缺少 ?profile=：模型 PVC 是 namespace 作用域对象，需要用它定位探针的 namespace",
			map[string]any{"field": "profile"})
		return
	}
	prof, err := s.st.Profiles.Get(ctx, nil, profileName)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			s.rejectValidation(w, r, "list_models", "model_listing", profileName,
				"profile 不存在（先在管理台登记）", map[string]any{"field": "profile", "profile": profileName})
			return
		}
		s.failErr(w, r, err)
		return
	}

	// 找挂到 /models 的共享挂载：mount_path 精确命中优先，其次别名命中。
	spec, mountPath := s.resolveModelsMount(ctx)
	if spec == nil {
		writeJSON(w, http.StatusOK, modelsUnavailable("model_dir_not_mounted",
			"没有挂载点（或别名）指向 "+modelsMountPath+"，提交页不渲染下拉框"))
		return
	}
	if s.modelProbe == nil {
		writeJSON(w, http.StatusOK, modelsUnavailable("probe_unavailable",
			"探针未接线（控制面没拿到 K8s 客户端或未启用 controller）"))
		return
	}

	listing, err := s.modelProbe.List(ctx, prof.Namespace, spec.ClaimName)
	if err != nil {
		// 探针失败不挡表单：记日志 + available:false，前端回退手输框。
		s.logger.Warn("模型目录探针失败（下拉框降级为手输）",
			"err", err, "profile", profileName, "namespace", prof.Namespace,
			"claim_name", spec.ClaimName)
		writeJSON(w, http.StatusOK, modelsUnavailable("probe_failed",
			"探针 Job 失败（PVC 不存在 / 镜像拉不动 / 超时），详情看控制面日志"))
		return
	}
	if listing.Models == nil {
		listing.Models = []string{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"available":  true,
		"profile":    profileName,
		"namespace":  prof.Namespace,
		"claim_name": spec.ClaimName,
		"mount_path": mountPath,
		"models":     listing.Models,
		"probed_at":  listing.ProbedAt.UTC(),
	})
}

// resolveModelsMount 找挂到 /models 的启用挂载：mount_path 命中优先，其次别名命中。
// 返回 (命中 spec, 命中路径)——别名命中时用别名（model_path 按 runner 容器内实际可见的路径拼）。
func (s *Server) resolveModelsMount(ctx context.Context) (*domain.SharedMountSpec, string) {
	specs := s.sharedMountsSpecs(ctx)
	var byAlias *domain.SharedMountSpec
	byAliasPath := ""
	for i := range specs {
		if specs[i].MountPath == modelsMountPath {
			return &specs[i], specs[i].MountPath
		}
		if byAlias == nil {
			for _, a := range specs[i].AliasPaths {
				if a == modelsMountPath {
					byAlias = &specs[i]
					byAliasPath = a
					break
				}
			}
		}
	}
	return byAlias, byAliasPath
}

// modelsUnavailable 组一个 available:false 的应答（reason + 人类可读说明）。
func modelsUnavailable(reason, message string) map[string]any {
	return map[string]any{
		"available": false,
		"reason":    reason,
		"message":   message,
	}
}
