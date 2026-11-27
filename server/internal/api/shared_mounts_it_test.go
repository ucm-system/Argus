package api

// 平台级统一共享挂载管理面集成测试（D-65；真 PG）。

import (
	"context"
	"net/http"
	"strings"
	"testing"

	"github.com/ucm-system/testplane/server/internal/store"
)

// TestAdminSharedMounts_CRUDAndAudit 覆盖 4 个 admin 端点 + 只读视图 + 审计。
func TestAdminSharedMounts_CRUDAndAudit(t *testing.T) {
	e := newAdminTestEnv(t)
	name := uniqueAdminName("sm")
	path := "/shared-" + name

	// 新建（默认只读、默认启用）。
	rec := e.do(http.MethodPost, "/api/v1/admin/shared-mounts",
		`{"name":"`+name+`","mount_path":"`+path+`","claim_name":"weights-pvc"}`, "admin")
	created := requireStatus(t, rec, http.StatusCreated)
	if created["read_only"] != true || created["enabled"] != true {
		t.Fatalf("默认应为 read_only=true enabled=true: %v", created)
	}
	if created["claim_name"] != "weights-pvc" {
		t.Fatalf("claim_name = %v", created["claim_name"])
	}

	// 列表（admin）能看到。
	rec = e.do(http.MethodGet, "/api/v1/admin/shared-mounts", "", "admin")
	list := requireStatus(t, rec, http.StatusOK)
	items, _ := list["items"].([]any)
	if len(items) == 0 {
		t.Fatal("admin 列表为空")
	}

	// 只读视图（viewer 可读）能看到启用中的。
	rec = e.do(http.MethodGet, "/api/v1/shared-mounts", "", "viewer")
	pub := requireStatus(t, rec, http.StatusOK)
	pubItems, _ := pub["items"].([]any)
	found := false
	for _, it := range pubItems {
		m := it.(map[string]any)
		if m["name"] == name {
			found = true
		}
	}
	if !found {
		t.Fatalf("只读视图缺少 %s: %v", name, pubItems)
	}

	// 改名不允许。
	rec = e.do(http.MethodPut, "/api/v1/admin/shared-mounts/"+name,
		`{"name":"other-name"}`, "admin")
	requireStatus(t, rec, http.StatusBadRequest)

	// 更新 claim_name / read_only / enabled。
	rec = e.do(http.MethodPut, "/api/v1/admin/shared-mounts/"+name,
		`{"claim_name":"weights-pvc-v2","read_only":false}`, "admin")
	updated := requireStatus(t, rec, http.StatusOK)
	if updated["claim_name"] != "weights-pvc-v2" || updated["read_only"] != false {
		t.Fatalf("更新未生效: %v", updated)
	}

	// 停用后不出现在只读视图（它回答"我这次 Run 会挂什么"）。
	rec = e.do(http.MethodPut, "/api/v1/admin/shared-mounts/"+name, `{"enabled":false}`, "admin")
	requireStatus(t, rec, http.StatusOK)
	rec = e.do(http.MethodGet, "/api/v1/shared-mounts", "", "viewer")
	pub = requireStatus(t, rec, http.StatusOK)
	pubItems, _ = pub["items"].([]any)
	for _, it := range pubItems {
		m := it.(map[string]any)
		if m["name"] == name {
			t.Fatalf("停用后不应出现在只读视图: %v", m)
		}
	}

	// 审计：register / update / delete 都记了。
	var actions []string
	for _, a := range e.auditRows(store.AuditFilter{ObjectType: "shared_mount", ObjectID: name}) {
		actions = append(actions, a.Action)
	}
	joined := strings.Join(actions, ",")
	for _, want := range []string{"register_shared_mount", "update_shared_mount"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("审计缺 %s（实际 %s）", want, joined)
		}
	}

	// 删除。
	rec = e.do(http.MethodDelete, "/api/v1/admin/shared-mounts/"+name, "", "admin")
	if rec.Code != http.StatusNoContent {
		t.Fatalf("DELETE status = %d，期望 204\nbody=%s", rec.Code, rec.Body.String())
	}
	if _, err := e.st.SharedMounts.Get(context.Background(), nil, name); err == nil {
		t.Fatal("删除后仍能读到")
	}
	// 再删 → 404。
	rec = e.do(http.MethodDelete, "/api/v1/admin/shared-mounts/"+name, "", "admin")
	requireError(t, rec, http.StatusNotFound, "NOT_FOUND")
}

// TestAdminSharedMounts_Validation 覆盖 400 校验：绝对路径、保留路径、claim 必填、
// name/path 冲突（issue code 用 CONFLICT）。
func TestAdminSharedMounts_Validation(t *testing.T) {
	e := newAdminTestEnv(t)
	name := uniqueAdminName("smv")
	path := "/shared-v-" + name
	rec := e.do(http.MethodPost, "/api/v1/admin/shared-mounts",
		`{"name":"`+name+`","mount_path":"`+path+`","claim_name":"pvc-a"}`, "admin")
	requireStatus(t, rec, http.StatusCreated)

	cases := []struct {
		label string
		body  string
		field string
	}{
		{"相对路径", `{"name":"rel-x","mount_path":"models","claim_name":"pvc-a"}`, "mount_path"},
		{"保留路径", `{"name":"reserved-x","mount_path":"/workspace/output","claim_name":"pvc-a"}`, "mount_path"},
		{"claim 缺失", `{"name":"noclaim-x","mount_path":"/noclaim-x","claim_name":""}`, "claim_name"},
		{"path 重复", `{"name":"dup-path-x","mount_path":"` + path + `","claim_name":"pvc-a"}`, "mount_path"},
		{"name 重复", `{"name":"` + name + `","mount_path":"/other-path-x","claim_name":"pvc-a"}`, "name"},
	}
	for _, c := range cases {
		rec := e.do(http.MethodPost, "/api/v1/admin/shared-mounts", c.body, "admin")
		body := requireError(t, rec, http.StatusBadRequest, "VALIDATION_ERROR")
		detail := detailOf(t, body)
		issues, _ := detail["issues"].([]any)
		if len(issues) == 0 {
			t.Fatalf("%s: 400 但没有 detail.issues: %v", c.label, body)
		}
		ok := false
		for _, it := range issues {
			m, _ := it.(map[string]any)
			if m["path"] == c.field && (m["code"] == "CONFLICT" || m["code"] == "REQUIRED") {
				ok = true
			}
		}
		if !ok {
			t.Fatalf("%s: issues 里没有 %s + CONFLICT/REQUIRED: %v", c.label, c.field, issues)
		}
	}
}

// TestAdminSharedMounts_AliasPaths 覆盖别名字段（D-63 一卷多挂载点）：
// CRUD 往返、省略=保留旧值、显式 []=清空、跨行冲突 400。
func TestAdminSharedMounts_AliasPaths(t *testing.T) {
	e := newAdminTestEnv(t)
	name := uniqueAdminName("sma")

	// 1) 新建带别名（尾部斜杠归一）。
	rec := e.do(http.MethodPost, "/api/v1/admin/shared-mounts",
		`{"name":"`+name+`","mount_path":"/alias-m-`+name+`","alias_paths":["/mnt/`+name+`/","/mnt/w-`+name+`"],"claim_name":"weights-pvc"}`, "admin")
	created := requireStatus(t, rec, http.StatusCreated)
	alias, _ := created["alias_paths"].([]any)
	if len(alias) != 2 || alias[0] != "/mnt/"+name {
		t.Fatalf("alias_paths = %v, want 归一化后的两个别名", alias)
	}

	// 2) PUT 省略 alias_paths = 保留旧值。
	rec = e.do(http.MethodPut, "/api/v1/admin/shared-mounts/"+name, `{"enabled":false}`, "admin")
	updated := requireStatus(t, rec, http.StatusOK)
	if alias, _ := updated["alias_paths"].([]any); len(alias) != 2 {
		t.Fatalf("省略 alias_paths 应保留旧值: %v", updated["alias_paths"])
	}

	// 3) PUT 显式 [] = 清空。
	rec = e.do(http.MethodPut, "/api/v1/admin/shared-mounts/"+name, `{"alias_paths":[]}`, "admin")
	updated = requireStatus(t, rec, http.StatusOK)
	if alias, _ := updated["alias_paths"].([]any); len(alias) != 0 {
		t.Fatalf("显式 [] 应清空: %v", updated["alias_paths"])
	}

	// 4) 跨行冲突：A 的别名撞 B 的 mount_path / 别名 → 400 CONFLICT。
	other := uniqueAdminName("smb")
	rec = e.do(http.MethodPost, "/api/v1/admin/shared-mounts",
		`{"name":"`+other+`","mount_path":"/b-`+other+`","alias_paths":["/bx-`+other+`"],"claim_name":"pvc-b"}`, "admin")
	requireStatus(t, rec, http.StatusCreated)
	for _, tc := range []struct{ label, body, field string }{
		{"别名撞其它行 mount_path", `{"name":"c1-` + other + `","mount_path":"/c1-` + other + `","alias_paths":["/b-` + other + `"],"claim_name":"pvc"}`, "alias_paths[0]"},
		{"别名撞其它行别名", `{"name":"c2-` + other + `","mount_path":"/c2-` + other + `","alias_paths":["/bx-` + other + `"],"claim_name":"pvc"}`, "alias_paths[0]"},
		{"mount_path 撞其它行别名", `{"name":"c3-` + other + `","mount_path":"/bx-` + other + `","claim_name":"pvc"}`, "mount_path"},
	} {
		rec := e.do(http.MethodPost, "/api/v1/admin/shared-mounts", tc.body, "admin")
		body := requireError(t, rec, http.StatusBadRequest, "VALIDATION_ERROR")
		issues, _ := detailOf(t, body)["issues"].([]any)
		hit := false
		for _, it := range issues {
			m, _ := it.(map[string]any)
			if m["path"] == tc.field && m["code"] == "CONFLICT" {
				hit = true
			}
		}
		if !hit {
			t.Fatalf("%s: issues 里没有 %s+CONFLICT: %v", tc.label, tc.field, issues)
		}
	}

	// 5) 自身校验：别名相对路径 / 撞保留路径 / 等于自身 mount_path。
	for _, tc := range []struct{ label, body, field string }{
		{"相对路径", `{"name":"av1-` + other + `","mount_path":"/av1-` + other + `","alias_paths":["mnt/x"],"claim_name":"pvc"}`, "alias_paths[0]"},
		{"撞保留路径", `{"name":"av2-` + other + `","mount_path":"/av2-` + other + `","alias_paths":["/tmp"],"claim_name":"pvc"}`, "alias_paths[0]"},
		{"等于自身 mount_path", `{"name":"av3-` + other + `","mount_path":"/av3-` + other + `","alias_paths":["/av3-` + other + `"],"claim_name":"pvc"}`, "alias_paths[0]"},
	} {
		rec := e.do(http.MethodPost, "/api/v1/admin/shared-mounts", tc.body, "admin")
		body := requireError(t, rec, http.StatusBadRequest, "VALIDATION_ERROR")
		issues, _ := detailOf(t, body)["issues"].([]any)
		if len(issues) == 0 {
			t.Fatalf("%s: 400 但没有 issues: %v", tc.label, body)
		}
	}
}

// TestSharedMounts_EffectiveForRendering 验证"管理面写入 → 渲染来源立即变化"
// 以及"删除后不再注入"（D-65 的核心行为，走 api 的运行时来源）。
func TestSharedMounts_EffectiveForRendering(t *testing.T) {
	e := newAdminTestEnv(t)
	ctx := context.Background()
	if _, err := e.st.Pool().Exec(ctx, `DELETE FROM shared_mounts`); err != nil {
		t.Fatalf("清表: %v", err)
	}
	specs, err := e.st.SharedMounts.EffectiveSpecs(ctx, nil)
	if err != nil || len(specs) != 0 {
		t.Fatalf("清表后有效挂载应为空: %v %v", specs, err)
	}

	name := uniqueAdminName("eff")
	rec := e.do(http.MethodPost, "/api/v1/admin/shared-mounts",
		`{"name":"`+name+`","mount_path":"/eff-`+name+`","claim_name":"pvc-eff"}`, "admin")
	requireStatus(t, rec, http.StatusCreated)
	specs, _ = e.st.SharedMounts.EffectiveSpecs(ctx, nil)
	if len(specs) != 1 || specs[0].ClaimName != "pvc-eff" {
		t.Fatalf("写入后应立即出现在渲染来源: %+v", specs)
	}

	rec = e.do(http.MethodDelete, "/api/v1/admin/shared-mounts/"+name, "", "admin")
	if rec.Code != http.StatusNoContent {
		t.Fatalf("DELETE status = %d", rec.Code)
	}
	specs, _ = e.st.SharedMounts.EffectiveSpecs(ctx, nil)
	if len(specs) != 0 {
		t.Fatalf("删除后不应再注入: %+v", specs)
	}
}
