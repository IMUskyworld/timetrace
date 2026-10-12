"""单文件 HTML 报告（内联 CSS + 手写 SVG，零外部资源）。

单独一个模块的原因：模板里的长行天然会超过 110 列，把 E501 的豁免限制在这一个文件里，
比在 report.py 里到处加 noqa 更干净（见 pyproject.toml 的 per-file-ignores）。

窗口标题是任意外部输入，所以每个插值都过 `html.escape` —— 与 learntree 同一套纪律。
"""

from __future__ import annotations

import hashlib
from html import escape

from timetrace.domain import KIND_APP, KIND_GAP, KIND_IDLE
from timetrace.report import AppRow, ReportData
from timetrace.timeutil import format_duration, format_local, format_percent

HTML_TOP_APPS = 8
HTML_OTHER_COLOR = "#9aa4b0"


PALETTE = (
    "#2f6fd0",
    "#2f8f5b",
    "#c9762a",
    "#8a5cc4",
    "#c0463c",
    "#1f8a99",
    "#7a7f2f",
    "#b0508a",
)


def app_color(exe: str) -> str:
    """按进程名稳定取色：同一份数据每次渲染颜色一致，不同进程尽量分开。"""
    digest = hashlib.sha1(exe.encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]


def render_html(data: ReportData) -> str:
    rows = [row for row in data.apps if row.label == ""]
    ranked = [row for row in rows if not row.excluded]
    specials = [row for row in data.apps if row.label]
    headline = ranked[:HTML_TOP_APPS]
    peak = max((row.seconds for row in data.apps), default=0.0) or 1.0

    bars = []
    for row in headline + specials:
        width = max(1.0, row.seconds / peak * 100.0)
        color = app_color(row.exe) if not row.label else HTML_OTHER_COLOR
        bars.append(
            f'<div class="bar-row">'
            f'<span class="bar-name" title="{escape(row.display())}">{escape(row.display())}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{width:.2f}%;background:{color}"></span></span>'
            f'<span class="bar-value">{escape(format_duration(row.seconds))}'
            f'<span class="bar-share">{escape(format_percent(row.share))}</span></span>'
            f"</div>"
        )

    hourly = _hourly_chart(data, headline)
    blocks = "\n".join(
        f"<li><b>{escape(block.exe)}</b> {escape(format_duration(block.seconds))}"
        f'<span class="muted">（{escape(block.window())}）</span></li>'
        for block in data.focus[:12]
    )
    notes = "\n".join(f"<li>{escape(note)}</li>" for note in data.notes)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>timetrace · {escape(data.range_label)}</title>
<style>
:root {{
  --bg:#f6f7f9; --panel:#fff; --ink:#1c2126; --ink2:#5b6672; --ink3:#8a949f;
  --line:#dfe3e8; --line2:#c3cad2; --track:#eceff3;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#14181d; --panel:#1b2026; --ink:#e7ebef; --ink2:#a9b4c0; --ink3:#7d8794;
           --line:#2c333b; --line2:#3c454f; --track:#242b33; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.55 ui-sans-serif,"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif; }}
.wrap {{ max-width:56rem; margin:0 auto; padding:2rem 1.25rem 4rem; }}
h1 {{ font-size:1.35rem; margin:0 0 .25rem; }}
h2 {{ font-size:1rem; margin:1.8rem 0 .6rem; }}
.sub {{ color:var(--ink2); margin:0 0 1.25rem; }}
.cards {{ display:flex; flex-wrap:wrap; gap:.75rem; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:.7rem .9rem; min-width:9rem; flex:1 1 9rem; }}
.card .k {{ color:var(--ink3); font-size:.78rem; }}
.card .v {{ font-size:1.05rem; font-variant-numeric:tabular-nums; }}
.bar-row {{ display:grid; grid-template-columns:12rem 1fr 7.5rem; gap:.6rem; align-items:center;
  margin:.22rem 0; }}
.bar-name {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.bar-track {{ background:var(--track); border-radius:6px; height:.85rem; overflow:hidden; }}
.bar-fill {{ display:block; height:100%; border-radius:6px; }}
.bar-value {{ text-align:right; font-variant-numeric:tabular-nums; }}
.bar-share {{ color:var(--ink3); margin-left:.4rem; font-size:.8rem; }}
svg {{ width:100%; height:auto; background:var(--panel); border:1px solid var(--line); border-radius:10px; }}
ul {{ padding-left:1.1rem; }}
.muted {{ color:var(--ink3); }}
.quality {{ color:var(--ink2); font-size:.85rem; }}
footer {{ margin-top:2.5rem; color:var(--ink3); font-size:.8rem; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>timetrace · {escape(data.range_label)}</h1>
  <p class="sub">{escape(format_local(data.range_start))} → {escape(format_local(data.range_end))}
     · 生成于 {escape(format_local(data.generated_at))}</p>

  <div class="cards">
    <div class="card"><div class="k">覆盖率</div><div class="v">{escape(format_percent(data.coverage))}</div></div>
    <div class="card"><div class="k">应用时间</div><div class="v">{escape(format_duration(data.totals.get(KIND_APP, 0.0)))}</div></div>
    <div class="card"><div class="k">空闲</div><div class="v">{escape(format_duration(data.totals.get(KIND_IDLE, 0.0)))}</div></div>
    <div class="card"><div class="k">未记录</div><div class="v">{escape(format_duration(data.totals.get(KIND_GAP, 0.0)))}</div></div>
    <div class="card"><div class="k">切换次数</div><div class="v">{data.switches}</div></div>
  </div>

  <h2>应用排行</h2>
  {chr(10).join(bars) or '<p class="muted">这个区间里没有应用时间。</p>'}

  <h2>每小时在用什么</h2>
  {hourly}

  <h2>专注块（≥ {escape(format_duration(data.focus_min * 60))} 不切换）</h2>
  {f"<ul>{blocks}</ul>" if blocks else '<p class="muted">没有达到阈值的专注块。</p>'}

  <h2>账本可信度</h2>
  <p class="quality">
    采样 {int(data.quality["sample_count"])} 条 ·
    中位间隔 {escape(format_duration(data.quality["median_interval"]))} ·
    最大间隔 {escape(format_duration(data.quality["max_interval"]))} ·
    异常采样 {int(data.quality["errors"])} 条 ·
    丢弃 {int(data.quality["discarded"])} 条
  </p>
  {f"<ul>{notes}</ul>" if notes else ""}

  <footer>由 timetrace 生成。数据从未离开这台机器：没有网络请求、没有账号、没有遥测。</footer>
</div>
</body>
</html>
"""


def _hourly_chart(data: ReportData, headline: list[AppRow]) -> str:
    order = [row.exe for row in headline]
    shown = order[:5]
    width, height = 720, 180
    left, bottom = 34, 26
    plot_w = width - left - 8
    plot_h = height - bottom - 10
    slot = plot_w / 24
    peak = max((row.seconds for row in data.hourly), default=0.0) or 1.0

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="每小时应用时间">',
        f'<line x1="{left}" y1="{height - bottom}" x2="{width - 8}" y2="{height - bottom}" stroke="#c3cad2"/>',
    ]
    for row in data.hourly:
        if row.seconds <= 0:
            continue
        x = left + row.hour * slot + 2
        bar_w = max(3.0, slot - 4)
        cursor = height - bottom
        for name, seconds in sorted(row.by_app.items(), key=lambda item: -item[1]):
            chunk = seconds / peak * plot_h
            if chunk <= 0:
                continue
            cursor -= chunk
            color = app_color(name) if name in shown else HTML_OTHER_COLOR
            parts.append(
                f'<rect x="{x:.1f}" y="{cursor:.1f}" width="{bar_w:.1f}" height="{chunk:.1f}" '
                f'fill="{color}"><title>{escape(name)} {escape(format_duration(seconds))}</title></rect>'
            )
    for hour in range(0, 24, 3):
        text_x = left + hour * slot + slot / 2
        parts.append(
            f'<text x="{text_x:.1f}" y="{height - bottom + 14}" font-size="10" text-anchor="middle" '
            f'fill="#8a949f">{hour:02d}</text>'
        )
    peak_label = escape(format_duration(peak))
    parts.append(f'<text x="{left}" y="12" font-size="10" fill="#8a949f">峰值 {peak_label}／小时</text>')
    parts.append("</svg>")
    return "\n".join(parts)
