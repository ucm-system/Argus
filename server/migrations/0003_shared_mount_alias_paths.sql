-- 0003: shared_mounts.alias_paths —— 同一卷的 Pod 内别名挂载点（D-63）
--
-- 背景（用户实测）：部分存储后端（NFS）同一卷不允许被挂两次 —— 两条挂载点各自
-- 渲染一条 volume（同一 claim 出现两次）时 Pod 可能永远 ContainerCreating。
-- 正确形态是"一卷多挂载点"：一条记录带 alias_paths，渲染层对同一条 volume
-- 生成多条 volumeMount（与 output/input 复用同一卷声明的先例一致，§6.8）。
--
-- 例：mount_path=/models，alias_paths=['/mnt/model'] → /mnt/model 映射到 /models。
-- 冲突规则（绝对路径、保留路径、全局唯一）与 mount_path 一致，由 API/渲染层校验；
-- jsonb 无法表达跨行唯一约束，DB 层只保证它是字符串数组。

ALTER TABLE shared_mounts ADD COLUMN alias_paths jsonb NOT NULL DEFAULT '[]';
ALTER TABLE shared_mounts ADD CONSTRAINT shared_mounts_alias_ck
  CHECK (jsonb_typeof(alias_paths) = 'array');
