#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aisbench-sla 的**人看报告**：sla_summary.json → 自包含 result.html（Runner 自有代码，非上游）。

定位：SLA 搜索天然是多组数据（预检 + 阶梯 + 二分，每点一行），平台长表（result.json）
适合机器消费，但「哪一点 PASS / 哪一点被降级 / 搜出来的最大并发是多少」在长表里不直观。
本报告给三个东西：

1. **SLA 约束与结论卡**：阈值口径、max_ok_concurrency（配置口径）与
   max_ok_concurrency_measured（实测口径，评审 #62 硬问题 2）分开呈现；
2. **阶梯总表**：每个探测点一行（kind/verdict 徽标/降级标注/实测并发/延迟/吞吐/命中率），
   搜出来的最大可行并发行**高亮**；
3. **趋势图**：TTFT / TPOT / 输出吞吐 / 命中率随并发变化（内联 SVG，零外部依赖），
   SLA 阈值画成虚线——越线一眼可见。

自包含约束同 aisbench-prefix 的 report.py：不引任何 CDN/JS/字体；工具的 -1/None 哨兵
（= 没解析到）渲染成「—」，不编数。数据源是 tool 的 sla_summary.json（run.sh 已校验过
其存在与结构），本脚本**只读不改**它。
"""
from __future__ import annotations

import html
import json
import os

# 工具的哨兵：-1 / None = 该统计量没解析到（parse.py 的 DEFAULTS）
_MISSING = {-1, -1.0, None, ""}


def _fmt(value):
    if value is None:
        return "—"
    if isinstance(value, str):
        return html.escape(value) if value not in _MISSING else "—"
    if value in _MISSING:
        return "—"
    text = f"{value:,.2f}" if isinstance(value, float) else str(value)
    return text.rstrip("0").rstrip(".") if "." in text else text


def _cell(value):
    if value is None or value in _MISSING:
        return '<td class="missing">—</td>'
    return f"<td>{_fmt(value)}</td>"


def _fmt_ms(value):
    """延迟列：哨兵 → —；正常值带单位由列表头表达，这里只给数。"""
    if value is None or value in _MISSING:
        return None
    return value


def _svg_chart(title, unit, series, rows, sla):
    """series: [(key, label, color)]；x = 探测点序（含 preflight/bisect）。

    每条序列的 SLA 阈值直接查 sla dict（键 = 工具的规范键，如 ttft_avg_ms；
    输出吞吐的行键与规范键不同，单独映射），阈值画红色虚线——越线一眼可见。
    """
    pts_all = [(r, key) for r in rows for key, _l, _c in series
               if isinstance(r.get(key), (int, float))]
    if len(rows) < 2 or len(pts_all) < 2:
        return ""
    width, height, margin = 720, 240, 56
    plot_w, plot_h = width - margin * 2, height - margin * 2
    # 阈值映射：行键 → sla 规范键（output_token_throughput 行键来自
    # parse.py 的总吞吐，sla 规范键是 *_avg）
    thr_of = {}
    for key, _label, _color in series:
        sla_key = key if key in sla else {
            "output_token_throughput": "output_token_throughput_avg"}.get(key)
        if sla_key is not None and isinstance(sla.get(sla_key), (int, float)):
            thr_of[key] = float(sla[sla_key])
    values = [r[key] for r, key in pts_all] + list(thr_of.values())
    ymax, ymin = max(values), min(values)
    span = (ymax - ymin) or (ymax or 1.0)
    ymax_p = ymax + span * 0.1
    ymin_p = max(0.0, ymin - span * 0.1) if ymin >= 0 else ymin - span * 0.1
    yspan = (ymax_p - ymin_p) or 1.0

    def xy(idx, value):
        x = margin + (plot_w * idx / (len(rows) - 1))
        y = margin + plot_h * (1 - (value - ymin_p) / yspan)
        return f"{x:.1f},{y:.1f}"

    parts = [f'<div class="chart"><h3>{html.escape(title)}'
             f'<span class="unit">（{html.escape(unit)}）</span></h3>']
    parts.append(f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">')
    for i in range(5):
        gy = margin + plot_h * i / 4
        val = ymax_p - yspan * i / 4
        parts.append(f'<line x1="{margin}" y1="{gy:.1f}" x2="{width - margin}" y2="{gy:.1f}" class="grid"/>')
        parts.append(f'<text x="{margin - 8}" y="{gy + 4:.1f}" class="tick" text-anchor="end">{_fmt(round(val, 2))}</text>')
    for key, thr in thr_of.items():
        gy = margin + plot_h * (1 - (thr - ymin_p) / yspan)
        parts.append(f'<line x1="{margin}" y1="{gy:.1f}" x2="{width - margin}" y2="{gy:.1f}" class="threshold"/>')
        parts.append(f'<text x="{width - margin}" y="{gy - 6:.1f}" class="tick threshold-text" text-anchor="end">'
                     f'SLA 阈值 {_fmt(thr)}</text>')
    for idx, r in enumerate(rows):
        cx = margin + plot_w * idx / (len(rows) - 1)
        verdict = str(r.get("verdict") or "")
        color = {"PASS": "#166534", "FAIL": "#b91c1c"}.get(verdict, "#6b7280")
        kind = html.escape(str(r.get("kind") or ""))
        conc = html.escape(str(r.get("concurrency")))
        parts.append(f'<text x="{cx:.1f}" y="{height - margin + 18}" class="tick" text-anchor="middle" '
                     f'style="fill:{color}">{kind[:3]} c={conc}</text>')
    for key, label, color in series:
        pts = [xy(i, r[key]) for i, r in enumerate(rows) if isinstance(r.get(key), (int, float))]
        if not pts:
            continue
        parts.append(f'<polyline points="{" ".join(pts)}" class="line" style="stroke:{color}"/>')
        for i, r in enumerate(rows):
            if isinstance(r.get(key), (int, float)):
                px, py = xy(i, r[key]).split(",")
                parts.append(f'<circle cx="{px}" cy="{py}" r="3.5" style="fill:{color}">'
                             f'<title>{html.escape(label)} {kind_of(r)} c={html.escape(str(r.get("concurrency")))}: '
                             f'{_fmt(r[key])} {html.escape(unit)}</title></circle>')
    legend = " · ".join(f'<tspan style="fill:{color}">■</tspan> {label}' for _k, label, color in series)
    if thr_of:
        legend += f' · <tspan class="threshold-text">– –</tspan> SLA 阈值'
    parts.append(f'<text x="{margin}" y="18" class="legend">{legend}</text>')
    parts.append("</svg></div>")
    return "".join(parts)


def kind_of(row):
    return html.escape(str(row.get("kind") or ""))


LADDER_COLUMNS = [
    "kind", "concurrency", "verdict", "measured_concurrency", "measured_concurrency_ratio",
    "ttft_avg_ms", "ttft_p90_ms", "tpot_avg_ms", "tpot_p90_ms", "e2el_avg_ms", "e2el_p90_ms",
    "output_token_throughput", "request_throughput_qps", "total_requests",
    "hbm_hit_rate", "ext_hit_rate", "elapsed_s",
]
_COLUMN_LABELS = {
    "kind": "点类型", "concurrency": "目标并发", "verdict": "判定", "measured_concurrency": "实测并发",
    "measured_concurrency_ratio": "实测/目标", "ttft_avg_ms": "TTFT avg (ms)", "ttft_p90_ms": "TTFT P90 (ms)",
    "tpot_avg_ms": "TPOT avg (ms)", "tpot_p90_ms": "TPOT P90 (ms)", "e2el_avg_ms": "E2EL avg (ms)",
    "e2el_p90_ms": "E2EL P90 (ms)", "output_token_throughput": "输出吞吐 (tok/s)",
    "request_throughput_qps": "QPS (req/s)", "total_requests": "请求数",
    "hbm_hit_rate": "命中率 HBM", "ext_hit_rate": "命中率 Ext", "elapsed_s": "耗时 (s)",
}
_KIND_LABELS = {"preflight": "预检(c=1)", "probe": "阶梯探测", "bisect": "二分探测", "warmup": "预热"}
_HBM_IS_RATIO = True   # hbm_hit_rate 在工具里是 0~1 的比例


def _verdict_cell(row):
    verdict = str(row.get("verdict") or "")
    cls = {"PASS": "v-pass", "FAIL": "v-fail"}.get(verdict, "v-unknown")
    mark = ' <span class="degraded" title="%s">降级</span>' % html.escape(str(row.get("degraded_reason") or "")) \
        if row.get("degraded") else ""
    return f'<td><span class="{cls}">{html.escape(verdict)}</span>{mark}</td>'


def _kind_cell(row):
    kind = str(row.get("kind") or "")
    return f'<td>{_KIND_LABELS.get(kind, html.escape(kind))}</td>'


def _table(points, effective_max, note):
    head = "".join(f'<th>{_COLUMN_LABELS[c]}</th>' for c in LADDER_COLUMNS)
    body = []
    for r in points:
        cells = [_kind_cell(r), _cell(r.get("concurrency")), _verdict_cell(r)]
        for col in LADDER_COLUMNS[3:]:
            v = r.get(col)
            if col == "hbm_hit_rate" and _HBM_IS_RATIO and isinstance(v, (int, float)) and v not in _MISSING:
                v = round(v * 100.0, 2)   # 工具给的是 0~1 比例 → 报告里按百分数显示
            if col == "ext_hit_rate" and _HBM_IS_RATIO and isinstance(v, (int, float)) and v not in _MISSING:
                v = round(v * 100.0, 2)
            cells.append(_cell(v))
        cls = ""
        if (effective_max is not None and r.get("concurrency") == effective_max
                and str(r.get("kind")) != "warmup" and str(r.get("verdict")) == "PASS"
                and not r.get("degraded")):
            cls = ' class="chosen"'
        body.append(f"<tr{cls}>" + "".join(cells) + "</tr>")
    return (f'<p class="table-note">{html.escape(note)}</p>'
            f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def write_result_html(out, summary, meta):
    """→ 写出的 result.html 路径。summary 是已校验的 sla_summary.json dict。"""
    status = str(meta.get("status") or "unknown")
    status_cls = "ok" if status == "succeeded" else "bad"
    points = [p for p in (summary.get("points") or []) if isinstance(p, dict)]
    # 与 run.sh 的 PYRESULT 同一口径：PASS 且非 warmup 且实测并发达标 → 计入最大可行并发
    ratio_min = summary.get("measured_concurrency_min_ratio") or 0.8
    verified = [p for p in points
                if str(p.get("verdict")) == "PASS" and str(p.get("kind")) != "warmup"
                and not p.get("degraded")]
    effective_max = max((p.get("concurrency") for p in verified
                         if isinstance(p.get("concurrency"), (int, float))), default=None)
    probe_rows = [p for p in points if str(p.get("kind")) != "warmup"]
    sla = summary.get("sla") or {}
    sla_labels = summary.get("sla_labels") or []
    throughput = summary.get("throughput") or {}
    peak = throughput.get("peak") or {}
    best_ok = throughput.get("best_within_sla") or {}

    status_line = (f'<p><span class="badge {status_cls}">{status}</span> '
                   f'<span class="muted">task={html.escape(str(meta.get("task_id") or "local"))} · '
                   f'runner v{html.escape(str(meta.get("runner_version") or "?"))} · '
                   f'tool {html.escape(str(summary.get("tool") or "?"))} · '
                   f'耗时 {_fmt(summary.get("duration_s"))} s</span></p>')
    if str(summary.get("note") or "").strip():
        status_line += f'<p class="muted">结论：{html.escape(str(summary.get("note")))}</p>'
    if str(summary.get("error") or "").strip():
        status_line += (f'<p class="bad-text">工具报错：'
                        f'{html.escape(str(summary.get("error")))}</p>')

    concl = [
        ("最大可行并发（配置口径 max_ok_concurrency）", _fmt(summary.get("max_ok_concurrency"))),
        ("最大可行并发（实测口径 max_ok_concurrency_measured）", _fmt(summary.get("max_ok_concurrency_measured"))),
        ("实测/目标达标线", f"{_fmt(round(float(ratio_min) * 100, 1))}%"),
        ("输出吞吐峰值", f"{_fmt(peak.get('value'))} tok/s"
         + (f"（c={_fmt(peak.get('concurrency'))}）" if peak.get("concurrency") is not None else "")),
        ("SLA 内最佳吞吐", f"{_fmt(best_ok.get('value'))} tok/s"
         + (f"（c={_fmt(best_ok.get('concurrency'))}）" if best_ok.get("concurrency") is not None else "")),
    ]
    concl_html = "".join(f"<tr><th>{html.escape(k)}</th><td>{v}</td></tr>" for k, v in concl)
    sla_rows = "".join(
        f"<tr><td>{html.escape(str(label))}</td><td><code>{html.escape(str(key))}</code></td>"
        f"<td>{_fmt(limit)}</td></tr>"
        for key, limit in sorted(sla.items())
        for label in [next((lb for lb in sla_labels if key.split('_')[0] in lb.lower()
                            or (key.startswith('output') and '吞吐' in lb)), key)])
    if not sla_rows:
        sla_rows = "".join(f"<tr><td>{html.escape(str(label))}</td><td colspan='2' class='missing'>—</td></tr>"
                           for label in sla_labels)

    ladder_note = ("一行 = 一个探测点；高亮行 = 搜出的最大可行并发（PASS 且实测并发达标）。"
                   "降级 = 实测并发未达目标的 %s%%，不参与取值。" % _fmt(round(float(ratio_min) * 100, 1)))
    charts = []
    for title, unit, series in (
            ("TTFT 延迟", "ms", [("ttft_avg_ms", "avg", "#2563eb"), ("ttft_p90_ms", "P90", "#dc2626")]),
            ("TPOT 延迟", "ms", [("tpot_avg_ms", "avg", "#2563eb"), ("tpot_p90_ms", "P90", "#dc2626")]),
            ("E2EL 延迟", "ms", [("e2el_avg_ms", "avg", "#2563eb"), ("e2el_p90_ms", "P90", "#dc2626")]),
            ("输出吞吐", "tok/s", [("output_token_throughput", "输出吞吐", "#059669")]),
            ("命中率 HBM", "%", [("hbm_hit_rate", "命中率", "#d97706")])):
        if title == "命中率 HBM":
            # 工具给 0~1 比例：画图前换算成百分数
            scaled = [dict(p, hbm_hit_rate=(round(p["hbm_hit_rate"] * 100.0, 2)
                                            if isinstance(p.get("hbm_hit_rate"), (int, float))
                                            and p["hbm_hit_rate"] not in _MISSING else p["hbm_hit_rate"]))
                      for p in probe_rows]
            chart = _svg_chart(title, unit, series, scaled, sla)
        else:
            chart = _svg_chart(title, unit, series, probe_rows, sla)
        if chart:
            charts.append(chart)

    degraded = summary.get("degraded_points") or []
    warnings = summary.get("warnings") or []
    parts = ["""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aisbench-sla · SLA 搜索报告</title>
<style>
:root{--ink:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#f8fafc}
*{box-sizing:border-box}
body{font:14px/1.55 system-ui,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--ink);margin:0;background:var(--bg)}
main{max-width:1080px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:16px;margin:26px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:14px;margin:14px 0 4px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:12px}
.badge.ok{background:#dcfce7;color:#166534}.badge.bad{background:#fee2e2;color:#991b1b}
.muted{color:var(--muted)}
.bad-text{color:#991b1b}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:right;white-space:nowrap}
th{background:#f1f5f9;text-align:center;font-weight:600}
tbody tr:hover{background:#eff6ff}
tr.chosen{background:#ecfdf5;outline:2px solid #059669;outline-offset:-2px}
td.missing{color:#cbd5e1;text-align:center}
.workload td{text-align:left}
.v-pass{background:#dcfce7;color:#166534;padding:1px 8px;border-radius:999px;font-weight:600}
.v-fail{background:#fee2e2;color:#991b1b;padding:1px 8px;border-radius:999px;font-weight:600}
.degraded{color:#b45309;font-weight:600;cursor:help}
.tablewrap{overflow-x:auto;border:1px solid var(--line);background:#fff}
.table-note{color:var(--muted);font-size:12px;margin:8px 0 6px}
.chart{background:#fff;border:1px solid var(--line);padding:12px;margin:12px 0}
.chart svg{width:100%;height:auto}
.grid{stroke:#f1f5f9;stroke-width:1}
.threshold{stroke:#b91c1c;stroke-width:1.5;stroke-dasharray:6 4}
.threshold-text{fill:#b91c1c}
.line{fill:none;stroke-width:2;stroke-linejoin:round}
.tick{font-size:11px;fill:var(--muted)}
.legend{font-size:12px}
.unit{color:var(--muted);font-weight:400;font-size:12px}
code{background:#f1f5f9;padding:1px 6px;border-radius:4px}
ul.notes{margin:6px 0;padding-left:20px}
ul.notes li{margin:3px 0}
footer{margin-top:30px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:10px}
</style></head><body><main>"""]
    parts.append("<h1>aisbench-sla · SLA 搜索报告</h1>")
    parts.append(status_line)
    parts.append('<h2>SLA 约束口径</h2><div class="tablewrap"><table><thead><tr><th>约束</th>'
                 '<th>规范键</th><th>阈值</th></tr></thead><tbody>' + sla_rows + "</tbody></table></div>")
    parts.append('<h2>搜索结论</h2><div class="tablewrap"><table class="workload">'
                 f"<tbody>{concl_html}</tbody></table></div>")
    parts.append("<h2>阶梯总表（全部探测点）</h2>")
    parts.append(_table(probe_rows, effective_max, ladder_note))
    parts.append("<h2>趋势图</h2>")
    parts.append("".join(charts) if charts else '<p class="table-note">探测点不足 2 个，画不出趋势图。</p>')
    parts.append('<p class="table-note">同一份明细的数据面：<a href="sla_out/sla_points.csv">sla_out/sla_points.csv</a>'
                 '（逐点全列）；平台消费面：<code>result.json</code>。</p>')
    if degraded or warnings:
        parts.append("<h2>降级与警告</h2><ul class=notes>")
        for d in degraded:
            parts.append(f'<li>降级点 c={html.escape(str(d.get("concurrency")))}：实测 '
                         f'{html.escape(str(d.get("measured_concurrency")))}'
                         f'（{html.escape(str(d.get("measured_concurrency_ratio")))}）——'
                         f'{html.escape(str(d.get("reason") or ""))}</li>')
        for w in warnings:
            parts.append(f"<li>警告：{html.escape(str(w))}</li>")
        parts.append("</ul>")
    parts.append('<footer>数据来源：tool 的 sla_summary.json（run.sh 已校验），由 report.py 渲染；'
                 '本报告为自包含 HTML（无外部依赖），可直接归档或分享。</footer>')
    parts.append("</main></body></html>")

    path = os.path.join(out, "result.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("".join(parts))
    return path


def _main(argv=None):
    """CLI：report.py --summary sla_summary.json --out <AG_OUTPUT_DIR>。

    元信息走环境变量（AG_STATUS/AG_CODE/AG_REASON/AG_TASK_ID/AG_ATTEMPT/
    AG_RUNNER_SEQ/AG_RUNNER_VERSION/AG_DURATION_MS），由 run.sh 注入。
    """
    import argparse

    parser = argparse.ArgumentParser(description="sla_summary.json → result.html")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    with open(args.summary, "r", encoding="utf-8") as handle:
        summary = json.load(handle)
    duration = os.environ.get("AG_DURATION_MS", "").strip()
    meta = {
        "status": os.environ.get("AG_STATUS", "unknown"),
        "task_id": os.environ.get("AG_TASK_ID", "local"),
        "runner_version": os.environ.get("AG_RUNNER_VERSION", "unknown"),
        "duration_ms": int(duration) if duration.isdigit() else None,
    }
    path = write_result_html(args.out, summary, meta)
    print(f"[runner] result.html: {path}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
