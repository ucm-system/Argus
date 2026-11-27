#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aisbench-prefix 的**人看报告**：sidecar 事件 → 自包含 result.html（非上游代码）。

定位（与另外两个观察面的分工）：

* `result.html`（本模块产出）——**多组数据的横向对比报告**：并发阶梯总表 +
  趋势图（内联 SVG，零外部依赖，离线/受限网络都能开）+ 命中率 + 负载口径；
  浏览器直接开，不用 Excel。
* `aisbench_result.csv` —— 上游列序的数据面（复制/二次处理用）；
* `result.json` —— 平台契约（附录 A 长表），UI/报告管线吃它。

自包含约束：Runner 产物会被下载到任意机器查看，所以**不引任何 CDN/JS/字体**，
样式全部内联，图用内联 SVG 画。缺值的单元格渲染成「—」并把工具哨兵（99999/9999）
当缺失处理（与 result.json 的口径一致，不编数）。
"""
from __future__ import annotations

import html
import os

from summarize import SENTINELS, UPSTREAM_CSV_COLUMNS, hit_percent, point_index  # noqa: E402

# 报告表格列（full 阶段逐点一行；prefix 阶段单独一张表）
LADDER_COLUMNS = [
    "point", "phase", "target_cc", "measured_cc", "requests",
    "ttft_avg", "ttft_p90", "tpot_avg", "tpot_p90", "e2el_avg", "e2el_p90",
    "output_throughput", "output_throughput_per_device", "total_token_throughput",
    "input_token_throughput", "prefill_token_throughput", "qps",
    "hit_rate_hbm", "hit_rate_external",
]

_COLUMN_LABELS = {
    "point": "并发点", "phase": "阶段", "target_cc": "目标并发", "measured_cc": "实测并发",
    "requests": "请求数", "ttft_avg": "TTFT avg (ms)", "ttft_p90": "TTFT P90 (ms)",
    "tpot_avg": "TPOT avg (ms)", "tpot_p90": "TPOT P90 (ms)",
    "e2el_avg": "E2EL avg (ms)", "e2el_p90": "E2EL P90 (ms)",
    "output_throughput": "输出吞吐 (tok/s)", "output_throughput_per_device": "单卡输出吞吐 (tok/s)",
    "total_token_throughput": "总吞吐 (tok/s)", "input_token_throughput": "输入吞吐 (tok/s)",
    "prefill_token_throughput": "Prefill 吞吐 (tok/s)", "qps": "QPS (req/s)",
    "hit_rate_hbm": "命中率 HBM (%)", "hit_rate_external": "命中率 External (%)",
}

_CHART_SERIES = [
    ("TTFT 延迟", "ms", [("ttft_avg", "avg", "#2563eb"), ("ttft_p90", "P90", "#dc2626")]),
    ("TPOT 延迟", "ms", [("tpot_avg", "avg", "#2563eb"), ("tpot_p90", "P90", "#dc2626")]),
    ("输出吞吐", "tok/s", [("output_throughput", "总吞吐", "#059669"),
                           ("output_throughput_per_device", "单卡", "#d97706")]),
]

WORKLOAD_LABELS = [
    ("target", "被测服务"), ("model_name", "模型名"), ("model_path", "权重路径"),
    ("input_len", "输入长度"), ("output_len", "输出长度"), ("data_num", "数据条数"),
    ("request_rate", "请求速率"), ("dataset_type", "数据集形态"), ("repeat_rate", "前缀重复率"),
    ("prefix_num", "前缀种类"), ("dp", "dp 域"), ("prefix_test", "前缀预热"),
    ("seed", "随机种子"), ("seed_per_point", "逐点换种子"), ("dataset", "现成数据集"),
    ("dataset_path", "数据集目录"), ("summarizer", "汇总器"), ("test_type", "测试形态"),
    ("npu_num", "卡数"), ("work_path", "aisbench 工作根"),
]


def _fmt(value):
    """表格/图例里的数值格式：缺 → —；小数保留 2 位并去尾零。"""
    if value is None:
        return "—"
    if isinstance(value, str):
        return html.escape(value) if value not in SENTINELS else "—"
    text = f"{value:,.2f}" if isinstance(value, float) else str(value)
    return text.rstrip("0").rstrip(".") if "." in text else text


def _cell(value):
    if value is None:
        return '<td class="missing">—</td>'
    if isinstance(value, str) and value in SENTINELS:
        return '<td class="missing">—</td>'
    return f"<td>{_fmt(value)}</td>"


def _rows_by_phase(events, hits):
    """→ {phase: [row, ...]}；row 是 LADDER_COLUMNS 的取值 dict（含命中率）。"""
    by_phase: dict = {"prefix": [], "full": []}
    for point, ev_rows in events:
        for ev in ev_rows:
            if ev.get("kind") != "metrics":
                continue
            phase = ev.get("phase") or "full"
            values = ev.get("values") or {}
            hit = hits.get((point, phase))
            hbm = ext = None
            if hit is not None:
                hbm, _ = hit_percent(hit, "hbm")
                ext, _ = hit_percent(hit, "external")
            target = values.get("max_cc")
            row = {
                "point": point_index(point), "phase": phase,
                "target_cc": None if target in SENTINELS else target,
                "measured_cc": values.get("cc"),
                "requests": values.get("total_req"),
                "ttft_avg": values.get("TTFT avg"), "ttft_p90": values.get("TTFT P90"),
                "tpot_avg": values.get("TPOT avg"), "tpot_p90": values.get("TPOT SLO_P90"),
                "e2el_avg": values.get("E2EL avg"), "e2el_p90": values.get("E2EL P90"),
                "output_throughput": values.get("output_throughput"),
                "output_throughput_per_device": values.get("single_output_throughput"),
                "total_token_throughput": values.get("E2E_throughput"),
                "input_token_throughput": values.get("input_token_throughput"),
                "prefill_token_throughput": values.get("prefill_token_throughput"),
                "qps": values.get("qps"),
                "hit_rate_hbm": None if hbm is None else round(hbm, 2),
                "hit_rate_external": None if ext is None else round(ext, 2),
            }
            row = {k: (None if v in SENTINELS else v) for k, v in row.items()}
            by_phase.setdefault(phase, []).append(row)
    for rows in by_phase.values():
        rows.sort(key=lambda r: (str(r["point"]), r["phase"]))
    return by_phase


def _svg_chart(title, unit, series, rows):
    """单指标折线图（内联 SVG）。series: [(key, label, color)]；x = full 行序。"""
    full = [r for r in rows if r["phase"] == "full"]
    if len(full) < 2:
        return ""
    width, height, margin = 720, 240, 56
    plot_w, plot_h = width - margin * 2, height - margin * 2
    points_all = [(r[key], r) for r in full for key, _label, _color in series
                  if isinstance(r.get(key), (int, float))]
    if not points_all:
        return ""
    ymax = max(v for v, _r in points_all)
    ymin = min(v for v, _r in points_all)
    span = (ymax - ymin) or (ymax or 1.0)
    ymax_p = ymax + span * 0.08
    ymin_p = max(0.0, ymin - span * 0.08) if ymin >= 0 else ymin - span * 0.08
    yspan = (ymax_p - ymin_p) or 1.0

    def xy(idx, value):
        x = margin + (plot_w * idx / (len(full) - 1))
        y = margin + plot_h * (1 - (value - ymin_p) / yspan)
        return f"{x:.1f},{y:.1f}"

    parts = [f'<div class="chart"><h3>{html.escape(title)}'
             f'<span class="unit">（{html.escape(unit)}，full 阶段）</span></h3>']
    parts.append(f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">')
    for i in range(5):
        gy = margin + plot_h * i / 4
        val = ymax_p - yspan * i / 4
        parts.append(f'<line x1="{margin}" y1="{gy:.1f}" x2="{width - margin}" y2="{gy:.1f}" class="grid"/>')
        parts.append(f'<text x="{margin - 8}" y="{gy + 4:.1f}" class="tick" text-anchor="end">{_fmt(round(val, 2))}</text>')
    for idx, r in enumerate(full):
        cx = margin + plot_w * idx / (len(full) - 1)
        parts.append(f'<text x="{cx:.1f}" y="{height - margin + 18}" class="tick" text-anchor="middle">'
                     f'c={html.escape(str(r["target_cc"]))}</text>')
    for key, label, color in series:
        pts = [xy(i, r[key]) for i, r in enumerate(full) if isinstance(r.get(key), (int, float))]
        if not pts:
            continue
        parts.append(f'<polyline points="{" ".join(pts)}" class="line" style="stroke:{color}"/>')
        for i, r in enumerate(full):
            if isinstance(r.get(key), (int, float)):
                px, py = xy(i, r[key]).split(",")
                parts.append(f'<circle cx="{px}" cy="{py}" r="3.5" style="fill:{color}">'
                             f'<title>{html.escape(label)} c={html.escape(str(r["target_cc"]))}: {_fmt(r[key])} {html.escape(unit)}</title></circle>')
    legend = " · ".join(f'<tspan style="fill:{color}">■</tspan> {label}' for key, label, color in series)
    parts.append(f'<text x="{margin}" y="18" class="legend">{legend}</text>')
    parts.append("</svg></div>")
    return "".join(parts)


def _table(rows, note):
    head = "".join(f'<th>{_COLUMN_LABELS[c]}</th>' for c in LADDER_COLUMNS)
    body = []
    for r in rows:
        cells = []
        for col in LADDER_COLUMNS:
            cells.append(_cell(r.get(col)))
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<p class="table-note">{html.escape(note)}</p>'
            f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def write_result_html(out, events, hits, config, meta):
    """→ 写出的 result.html 路径；一条 metrics 事件都没有时返回 None（不产空报告）。"""
    has_metrics = any(ev.get("kind") == "metrics" for _p, rows in events for ev in rows)
    if not has_metrics:
        return None
    by_phase = _rows_by_phase(events, hits)
    full_rows = by_phase.get("full", [])
    prefix_rows = by_phase.get("prefix", [])

    status = html.escape(str(meta.get("status") or "unknown"))
    status_cls = "ok" if status == "succeeded" else "bad"
    workload_rows = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{_fmt(meta.get('config', {}).get(key))}</td></tr>"
        for key, label in WORKLOAD_LABELS)

    parts = ["""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aisbench-prefix · 前缀缓存性能报告</title>
<style>
:root{--ink:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#f8fafc;--accent:#2563eb}
*{box-sizing:border-box}
body{font:14px/1.55 system-ui,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--ink);margin:0;background:var(--bg)}
main{max-width:1080px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:16px;margin:26px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:14px;margin:14px 0 4px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:12px}
.badge.ok{background:#dcfce7;color:#166534}.badge.bad{background:#fee2e2;color:#991b1b}
.muted{color:var(--muted)}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:right;white-space:nowrap}
th{background:#f1f5f9;text-align:center;font-weight:600}
tbody tr:hover{background:#eff6ff}
td.missing{color:#cbd5e1;text-align:center}
.workload td{text-align:left}
.tablewrap{overflow-x:auto;border:1px solid var(--line);background:#fff}
.table-note{color:var(--muted);font-size:12px;margin:8px 0 6px}
.chart{background:#fff;border:1px solid var(--line);padding:12px;margin:12px 0}
.chart svg{width:100%;height:auto}
.grid{stroke:#f1f5f9;stroke-width:1}
.line{fill:none;stroke-width:2;stroke-linejoin:round}
.tick{font-size:11px;fill:var(--muted)}
.legend{font-size:12px}
.unit{color:var(--muted);font-weight:400;font-size:12px}
ul.notes{margin:6px 0;padding-left:20px}
ul.notes li{margin:3px 0}
code{background:#f1f5f9;padding:1px 6px;border-radius:4px}
footer{margin-top:30px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:10px}
</style></head><body><main>"""]
    parts.append(f"<h1>aisbench-prefix · 前缀缓存性能报告</h1>"
                 f'<p><span class="badge {status_cls}">{status}</span> '
                 f'<span class="muted">task={html.escape(str(meta.get("task_id") or "local"))} · '
                 f'runner v{html.escape(str(meta.get("runner_version") or "?"))} · '
                 f'tool {html.escape(str(meta.get("tool_version") or "?"))} · '
                 f'ais_bench {html.escape(str(meta.get("aisbench_version") or "?"))} · '
                 f'耗时 {_fmt(meta.get("duration_ms"))} ms</span></p>')

    parts.append('<h2>负载口径</h2><div class="tablewrap"><table class="workload">'
                 f"<tbody>{workload_rows}</tbody></table></div>")

    parts.append('<h2>并发阶梯（全量数据集 · full）</h2>')
    if full_rows:
        parts.append(_table(full_rows, "一行 = 一个并发点的全量数据集测量；— = 工具没解析到（哨兵值），不是 0。"))
    else:
        parts.append('<p class="table-note">没有 full 阶段数据（压测未执行或失败）。</p>')

    if prefix_rows:
        parts.append('<h2>前缀预热（prefix 阶段，仅作证据）</h2>')
        parts.append(_table(prefix_rows, "预热阶段的统计：并发 = dp（dp=1 时 = prefix_num）；TPOT 对纯 prefill 无意义，缺失属预期。"))

    parts.append("<h2>趋势图</h2>")
    chart_any = False
    for title, unit, series in _CHART_SERIES:
        chart = _svg_chart(title, unit, series, full_rows)
        if chart:
            chart_any = True
            parts.append(chart)
    if not chart_any:
        parts.append('<p class="table-note">不足 2 个 full 数据点，画不出趋势图（阶梯至少给两个并发值）。</p>')
    if full_rows:
        parts.append('<p class="table-note">同一份明细的数据面：<a href="aisbench_result.csv">aisbench_result.csv</a>'
                     '（上游列序）；平台消费面：<code>result.json</code>。</p>')

    notes = meta.get("notes") or []
    missing = meta.get("missing") or []
    if notes or missing:
        parts.append("<h2>说明与缺失项</h2><ul class=notes>")
        for item in notes:
            parts.append(f"<li>{html.escape(str(item))}</li>")
        for item in missing:
            parts.append(f'<li class="muted">未写入的指标：{html.escape(str(item))}</li>')
        parts.append("</ul>")

    parts.append('<footer>数据来源：工具 sidecar 事件（prefix_out/points/point-*/events.jsonl），'
                 '由 summarize.py 汇出；本报告为自包含 HTML（无外部依赖），可直接归档或分享。</footer>')
    parts.append("</main></body></html>")

    path = os.path.join(out, "result.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("".join(parts))
    return path
