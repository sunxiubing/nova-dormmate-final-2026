# -*- coding: utf-8 -*-
"""DormMate 温湿度数据分析与报告生成

读取 M1 网页导出的 dormmate.csv，统计总体情况与温湿度极值，
复用 M1 的判定规则统计各状态数量、提取异常记录，
绘制趋势图并生成 report.html 报告。

用法：
    python analysis/analysis.py                    # 自动查找 dormmate.csv
    python analysis/analysis.py --csv 路径.csv      # 指定 CSV
    python analysis/analysis.py --csv 新数据.csv     # 换数据一键重生成报告
"""

from __future__ import annotations

import argparse
import csv
import html
import math
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无界面后端，便于在脚本/CI 中运行
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

# ---------------------------------------------------------------- 判定规则
# 与 web/script.js 中的规则保持一致，改动时需同步两侧
TEMP_COLD_BELOW = 18.0
TEMP_HOT_AT = 30.0
HUMIDITY_WET_AT = 75.0

# 有效量程：超出的读数视为明显异常，与 M1 网页端一致
TEMP_MIN, TEMP_MAX = -20.0, 60.0
HUMIDITY_MIN, HUMIDITY_MAX = 0.0, 100.0

CSV_NAME = "dormmate.csv"
REQUIRED_COLUMNS = ("time", "temperature", "humidity", "status")

TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M")

# 与 web/script.js 的 NUMBER_PATTERN 保持一致
NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")

STATE_NORMAL = "正常"
TEMP_LABELS = {"cold": "偏冷", "hot": "偏热", "normal": STATE_NORMAL}
HUMIDITY_LABELS = {"wet": "偏湿", "normal": STATE_NORMAL}

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTDIR = ROOT / "report"

# 未显式指定 --csv 时的查找顺序
CSV_CANDIDATES = (
    ROOT / "data" / CSV_NAME,
    ROOT / "web" / CSV_NAME,
    ROOT / CSV_NAME,
    Path.home() / "Downloads" / CSV_NAME,
)


# ---------------------------------------------------------------- 规则实现
def judge_temperature(value: float) -> str:
    """温度 < 18 偏冷；温度 >= 30 偏热；其余正常。"""
    if value < TEMP_COLD_BELOW:
        return "cold"
    if value >= TEMP_HOT_AT:
        return "hot"
    return "normal"


def judge_humidity(value: float) -> str:
    """湿度 >= 75 偏湿；其余正常。"""
    return "wet" if value >= HUMIDITY_WET_AT else "normal"


def build_status(temp_state: str, humidity_state: str) -> str:
    """组合成单一状态标签，与网页端历史徽标/CSV status 列一致。"""
    abnormal = []
    if temp_state != "normal":
        abnormal.append(TEMP_LABELS[temp_state])
    if humidity_state != "normal":
        abnormal.append(HUMIDITY_LABELS[humidity_state])
    return "+".join(abnormal) if abnormal else STATE_NORMAL


# ---------------------------------------------------------------- 读取 CSV
def parse_time(raw: str):
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_number(raw, label: str):
    """返回 (数值, 错误信息)。正则与 web/script.js 完全一致。

    这里不用裸 float()：它会接受 "1e3"、"nan"、"inf"，而网页端会拒绝，
    两边校验结果必须保持一致。
    """
    text = str(raw if raw is not None else "").strip()

    if text == "":
        return None, f"{label}不能为空"

    if not NUMBER_PATTERN.match(text):
        return None, f"{label}不是数字：「{text}」"

    value = float(text)
    if not math.isfinite(value):
        return None, f"{label}数值无效：「{text}」"

    return value, None


def locate_csv(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise SystemExit(f"[错误] 找不到 CSV 文件：{path}")
        return path

    for candidate in CSV_CANDIDATES:
        if candidate.is_file():
            return candidate

    tried = "\n".join(f"  - {p}" for p in CSV_CANDIDATES)
    raise SystemExit(
        "[错误] 未找到 dormmate.csv，请先在网页中点击「导出 CSV」，\n"
        "       或使用 --csv 指定文件路径。已尝试以下位置：\n" + tried
    )


def count_csv_rows(path: Path) -> int:
    """数一遍 CSV 的数据行数（不含表头、不计空行），用来和有效记录数对照。

    用 csv.reader 而不是数换行符：字段里带换行会被数成两行。
    """
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)  # 表头
        return sum(1 for row in reader if any((cell or "").strip() for cell in row))


def load_records(path: Path):
    """返回 (有效记录, 被跳过的行)；跳过原因镜像 M1 网页端的输入校验。

    records 是本函数的局部变量，每次调用都从空列表开始。脚本跑完进程就退出，
    不存在跨次运行累加的可能——报告里的总数对不上时，问题一定在「读了哪个文件」
    或「报告是哪一次生成的」，不在这里。
    """
    records, skipped = [], []

    # utf-8-sig 同时兼容带 BOM（网页导出默认带 BOM）与不带 BOM 的文件
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)

        if reader.fieldnames is None:
            raise SystemExit(f"[错误] CSV 为空：{path}")

        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise SystemExit(
                f"[错误] CSV 缺少必需列：{', '.join(missing)}\n"
                f"       实际列：{', '.join(reader.fieldnames)}\n"
                f"       需要：{', '.join(REQUIRED_COLUMNS)}"
            )

        for line_no, row in enumerate(reader, start=2):
            raw_time = (row.get("time") or "").strip()
            raw_temp = (row.get("temperature") or "").strip()
            raw_hum = (row.get("humidity") or "").strip()

            if not raw_time and not raw_temp and not raw_hum:
                continue  # 跳过空行

            if not raw_time:
                skipped.append((line_no, "时间不能为空"))
                continue

            when = parse_time(raw_time)
            if when is None:
                skipped.append((line_no, f"时间格式无法识别：「{raw_time}」"))
                continue

            temp, error = parse_number(raw_temp, "温度")
            if error:
                skipped.append((line_no, error))
                continue

            hum, error = parse_number(raw_hum, "湿度")
            if error:
                skipped.append((line_no, error))
                continue

            if not (TEMP_MIN <= temp <= TEMP_MAX):
                skipped.append(
                    (line_no, f"温度超出有效范围（{TEMP_MIN:g} ~ {TEMP_MAX:g} ℃）：{temp:g}")
                )
                continue

            if not (HUMIDITY_MIN <= hum <= HUMIDITY_MAX):
                skipped.append(
                    (line_no, f"湿度超出有效范围（{HUMIDITY_MIN:g} ~ {HUMIDITY_MAX:g} %）：{hum:g}")
                )
                continue

            temp_state = judge_temperature(temp)
            hum_state = judge_humidity(hum)
            status = build_status(temp_state, hum_state)

            # CSV 里的 status 列仅作对照，统计一律以规则重算结果为准
            csv_status = (row.get("status") or "").strip()

            records.append(
                {
                    "line": line_no,
                    "time": when,
                    "time_text": when.strftime("%Y-%m-%d %H:%M:%S"),
                    "temperature": temp,
                    "humidity": hum,
                    "temp_state": temp_state,
                    "humidity_state": hum_state,
                    "status": status,
                    "csv_status": csv_status,
                    "status_match": csv_status == status,
                }
            )

    records.sort(key=lambda r: r["time"])
    return records, skipped


# ---------------------------------------------------------------- 统计分析
def compute_stats(records):
    temps = [r["temperature"] for r in records]
    hums = [r["humidity"] for r in records]

    temp_min = min(records, key=lambda r: r["temperature"])
    temp_max = max(records, key=lambda r: r["temperature"])
    hum_min = min(records, key=lambda r: r["humidity"])
    hum_max = max(records, key=lambda r: r["humidity"])

    abnormal = [r for r in records if r["status"] != STATE_NORMAL]

    return {
        "count": len(records),
        "temp_min": temp_min,
        "temp_max": temp_max,
        "hum_min": hum_min,
        "hum_max": hum_max,
        "temp_avg": sum(temps) / len(temps),
        "hum_avg": sum(hums) / len(hums),
        "status_counts": Counter(r["status"] for r in records),
        "temp_counts": Counter(TEMP_LABELS[r["temp_state"]] for r in records),
        "humidity_counts": Counter(HUMIDITY_LABELS[r["humidity_state"]] for r in records),
        "abnormal": abnormal,
        "mismatched": [r for r in records if not r["status_match"]],
    }


# ---------------------------------------------------------------- 绘图
def setup_chinese_font():
    """按可用性挑选中文字体，避免图表出现方块。"""
    preferred = ["Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC", "DejaVu Sans"]
    available = {f.name for f in font_manager.fontManager.ttflist}
    chosen = [name for name in preferred if name in available]
    plt.rcParams["font.sans-serif"] = chosen or ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def draw_trend(records, stats, out_path: Path):
    setup_chinese_font()

    times = [r["time"] for r in records]
    temps = [r["temperature"] for r in records]
    hums = [r["humidity"] for r in records]

    fig, ax_temp = plt.subplots(figsize=(11, 5.5), dpi=150)
    ax_hum = ax_temp.twinx()

    # 温度（左轴）
    ax_temp.plot(times, temps, color="#dd4b32", linewidth=2, marker="o",
                 markersize=5, label="温度 (℃)")
    ax_temp.axhline(TEMP_COLD_BELOW, color="#2f7df6", linestyle="--", linewidth=1.2,
                    label=f"偏冷阈值 {TEMP_COLD_BELOW:g} ℃")
    ax_temp.axhline(TEMP_HOT_AT, color="#dd4b32", linestyle=":", linewidth=1.2,
                    label=f"偏热阈值 {TEMP_HOT_AT:g} ℃")
    ax_temp.set_ylabel("温度 (℃)", color="#dd4b32")
    ax_temp.tick_params(axis="y", labelcolor="#dd4b32")

    # 湿度（右轴）
    ax_hum.plot(times, hums, color="#7c53e0", linewidth=2, marker="s",
                markersize=4, label="湿度 (%)")
    ax_hum.axhline(HUMIDITY_WET_AT, color="#7c53e0", linestyle="--", linewidth=1.2,
                   label=f"偏湿阈值 {HUMIDITY_WET_AT:g} %")
    ax_hum.set_ylabel("湿度 (%)", color="#7c53e0")
    ax_hum.tick_params(axis="y", labelcolor="#7c53e0")

    # 异常点高亮
    ab_times = [r["time"] for r in stats["abnormal"]]
    ab_temps = [r["temperature"] for r in stats["abnormal"]]
    if ab_times:
        ax_temp.scatter(ab_times, ab_temps, s=120, facecolors="none",
                        edgecolors="#e8543f", linewidths=2, zorder=5,
                        label=f"异常记录 ({len(ab_times)} 条)")

    # 极值标注：靠近右边界时把标签甩到点的左侧，避免被画布裁掉
    x_min, x_max = times[0], times[-1]
    span = (x_max - x_min).total_seconds() or 1.0

    def annotate_extreme(record, label, color, dy):
        ratio = (record["time"] - x_min).total_seconds() / span
        on_right = ratio > 0.8
        ax_temp.annotate(
            label,
            xy=(record["time"], record["temperature"]),
            xytext=(-8 if on_right else 8, dy),
            textcoords="offset points",
            ha="right" if on_right else "left",
            color=color,
            fontsize=9,
        )

    # 只有一条记录时 min/max 是同一个点，标两次会重叠
    if stats["temp_min"] is stats["temp_max"]:
        annotate_extreme(stats["temp_max"],
                         f"温度 {stats['temp_max']['temperature']:g}℃", "#dd4b32", 10)
    else:
        annotate_extreme(stats["temp_max"],
                         f"最高 {stats['temp_max']['temperature']:g}℃", "#dd4b32", 8)
        annotate_extreme(stats["temp_min"],
                         f"最低 {stats['temp_min']['temperature']:g}℃", "#2f7df6", -14)

    # 单条记录时日期轴会被 matplotlib 撑到数年，手动收窄到 ±30 分钟
    if len(records) == 1:
        pad = timedelta(minutes=30)
        ax_temp.set_xlim(times[0] - pad, times[0] + pad)

    ax_temp.set_title(f"DormMate 温湿度趋势（共 {len(records)} 条记录）", fontsize=13, pad=14)
    ax_temp.set_xlabel("时间")
    ax_temp.grid(alpha=0.25, linestyle=":")

    handles_l, labels_l = ax_temp.get_legend_handles_labels()
    handles_r, labels_r = ax_hum.get_legend_handles_labels()
    ax_temp.legend(handles_l + handles_r, labels_l + labels_r,
                   loc="upper left", fontsize=8.5, framealpha=0.9)

    fig.autofmt_xdate(rotation=30, ha="right")
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- 报告
def render_report(records, stats, skipped, csv_path: Path, png_name: str) -> str:
    esc = html.escape
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def stat_card(label, value, sub=""):
        sub_html = f'<span class="stat__sub">{esc(sub)}</span>' if sub else ""
        return (
            f'<div class="stat"><span class="stat__label">{esc(label)}</span>'
            f'<span class="stat__value">{esc(value)}</span>{sub_html}</div>'
        )

    cards = [
        stat_card("总记录数", f"{stats['count']} 条",
                  f"数据源 {csv_path.name}"),
        stat_card("温度范围",
                  f"{stats['temp_min']['temperature']:g} ~ {stats['temp_max']['temperature']:g} ℃",
                  f"均值 {stats['temp_avg']:.1f} ℃"),
        stat_card("湿度范围",
                  f"{stats['hum_min']['humidity']:g} ~ {stats['hum_max']['humidity']:g} %",
                  f"均值 {stats['hum_avg']:.1f} %"),
        stat_card("异常记录", f"{len(stats['abnormal'])} 条",
                  f"占比 {len(stats['abnormal']) / stats['count'] * 100:.1f}%"),
    ]

    def counter_rows(counter, total):
        if not counter:
            return '<tr><td colspan="3" class="muted">无数据</td></tr>'
        rows = []
        for name, num in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])):
            cls = "" if name == STATE_NORMAL else " class=\"warn\""
            rows.append(
                f"<tr><td{cls}>{esc(name)}</td><td>{num}</td>"
                f"<td>{num / total * 100:.1f}%</td></tr>"
            )
        return "".join(rows)

    if stats["abnormal"]:
        ab_rows = "".join(
            "<tr>"
            f"<td>{esc(r['time_text'])}</td>"
            f"<td>{r['temperature']:g}</td>"
            f"<td>{r['humidity']:g}</td>"
            f'<td class="warn">{esc(r["status"])}</td>'
            "</tr>"
            for r in stats["abnormal"]
        )
        abnormal_section = f"""
      <table>
        <thead><tr><th>时间</th><th>温度 (℃)</th><th>湿度 (%)</th><th>状态</th></tr></thead>
        <tbody>{ab_rows}</tbody>
      </table>"""
    else:
        abnormal_section = '<p class="muted">本次数据未发现异常记录，温湿度全程处于正常区间。</p>'

    skip_note = ""
    if skipped:
        items = "".join(f"<li>第 {n} 行：{esc(reason)}</li>" for n, reason in skipped)
        skip_note = f"""
      <h2>已跳过的异常数据（{len(skipped)} 行）</h2>
      <p class="muted">以下行未通过输入校验，已排除在统计之外：</p>
      <ul class="skipped">{items}</ul>"""

    mismatch_note = ""
    if stats["mismatched"]:
        items = "".join(
            f"<li>{esc(r['time_text'])}：CSV 记录 <b>{esc(r['csv_status'])}</b>，"
            f"按规则重算为 <b>{esc(r['status'])}</b></li>"
            for r in stats["mismatched"]
        )
        mismatch_note = f"""
      <h2>状态列不一致（{len(stats['mismatched'])} 条）</h2>
      <p class="muted">报告统计以规则重算结果为准：</p>
      <ul class="skipped">{items}</ul>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>DormMate 温湿度分析报告</title>
<style>
  :root {{
    --text: #182034; --muted: #6b7793; --border: #e2e7f1;
    --accent: #2f6df6; --warn: #dd4b32; --ok: #12996b; --surface: #fff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px 20px 56px; background: #eef1f8; color: var(--text);
    font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
    line-height: 1.6;
  }}
  .wrap {{ max-width: 980px; margin: 0 auto; }}
  header {{ margin-bottom: 22px; }}
  h1 {{ margin: 0 0 6px; font-size: 22px; }}
  h2 {{ margin: 28px 0 12px; font-size: 15px; letter-spacing: .06em;
        text-transform: uppercase; color: var(--muted); }}
  .meta {{ margin: 0; color: var(--muted); font-size: 13px; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }}
  .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
           padding: 14px 16px; }}
  .stat__label {{ display: block; color: var(--muted); font-size: 12px; }}
  .stat__value {{ display: block; font-size: 20px; font-weight: 650;
                  font-variant-numeric: tabular-nums; }}
  .stat__sub {{ display: block; color: var(--muted); font-size: 12px; }}
  .panel {{ background: var(--surface); border: 1px solid var(--border);
            border-radius: 14px; padding: 18px; }}
  .cols {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }}
  table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
  th, td {{ padding: 9px 10px; text-align: left; border-bottom: 1px solid var(--border);
            font-size: 13.5px; }}
  th {{ color: var(--muted); font-weight: 600; font-size: 12px;
        text-transform: uppercase; letter-spacing: .04em; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  td.warn {{ color: var(--warn); font-weight: 600; }}
  .muted {{ color: var(--muted); font-size: 13.5px; }}
  .skipped {{ margin: 0; padding-left: 20px; color: var(--muted); font-size: 13px; }}
  img.trend {{ display: block; width: 100%; height: auto; border-radius: 10px; }}
  footer {{ margin-top: 26px; color: var(--muted); font-size: 12.5px; text-align: center; }}
  code {{ background: #eef1f8; padding: 1px 6px; border-radius: 5px; font-size: 12.5px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>DormMate 温湿度分析报告</h1>
    <p class="meta">
      数据源：<code>{esc(str(csv_path))}</code> ·
      生成时间：{esc(generated_at)} ·
      规则：温度 &lt; {TEMP_COLD_BELOW:g} ℃ 偏冷，温度 ≥ {TEMP_HOT_AT:g} ℃ 偏热，湿度 ≥ {HUMIDITY_WET_AT:g} % 偏湿
    </p>
  </header>

  <h2>摘要</h2>
  <div class="stats">{"".join(cards)}</div>

  <div class="cols" style="margin-top: 14px;">
    <div class="panel">
      <h2 style="margin-top:0">综合状态分布</h2>
      <table>
        <thead><tr><th>状态</th><th>数量</th><th>占比</th></tr></thead>
        <tbody>{counter_rows(stats["status_counts"], stats["count"])}</tbody>
      </table>
    </div>
    <div class="panel">
      <h2 style="margin-top:0">温度状态分布</h2>
      <table>
        <thead><tr><th>状态</th><th>数量</th><th>占比</th></tr></thead>
        <tbody>{counter_rows(stats["temp_counts"], stats["count"])}</tbody>
      </table>
      <h2>湿度状态分布</h2>
      <table>
        <thead><tr><th>状态</th><th>数量</th><th>占比</th></tr></thead>
        <tbody>{counter_rows(stats["humidity_counts"], stats["count"])}</tbody>
      </table>
    </div>
  </div>

  <h2>异常关注记录</h2>
  <div class="panel">{abnormal_section}</div>

  <h2>趋势图</h2>
  <div class="panel">
    <img class="trend" src="{esc(png_name)}" alt="温湿度趋势图" />
  </div>
{skip_note}{mismatch_note}
  <footer>由 analysis/analysis.py 自动生成 · 共 {stats["count"]} 条有效记录</footer>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------- 主流程
def configure_stdout():
    """输出被重定向/管道捕获时强制 UTF-8。

    中文 Windows 下 Python 对非终端 stdout 会用 GBK 编码，字节流到 UTF-8
    的终端或工具里就成乱码；真正接在控制台时保持默认（Python 走宽字符 API）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(
        description="读取 DormMate 导出的 CSV，生成温湿度分析报告。"
    )
    parser.add_argument("--csv", help="CSV 文件路径，缺省时自动查找 dormmate.csv")
    parser.add_argument("--outdir", default=str(DEFAULT_OUTDIR),
                        help=f"报告输出目录，默认 {DEFAULT_OUTDIR}")
    args = parser.parse_args(argv)

    csv_path = locate_csv(args.csv)

    # 先打「读了哪个文件、里面有多少行」，再解析、再生成。
    # 报告是覆盖写的，但如果脚本因为路径写错压根没跑起来（比如在仓库根目录敲
    # python analysis.py，而文件其实在 analysis/ 子目录里），磁盘上留着的就是
    # 上一次的旧报告，里面的总数当然是旧的。看到数字对不上时先看这几行，
    # 再看报告页脚里的「生成时间」，就能分清是数据变了还是报告没更新。
    print(f"数据源：{csv_path.resolve()}")
    print(f"CSV 数据行：{count_csv_rows(csv_path)} 行（不含表头）")

    records, skipped = load_records(csv_path)
    print(f"有效记录：{len(records)} 条" + (f"，跳过 {len(skipped)} 行" if skipped else ""))

    if not records:
        print(f"[错误] {csv_path} 中没有可用的有效记录，已终止，未生成报告。", file=sys.stderr)
        for line_no, reason in skipped:
            print(f"        第 {line_no} 行：{reason}", file=sys.stderr)
        return 1

    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    stats = compute_stats(records)

    png_path = outdir / "trend.png"
    draw_trend(records, stats, png_path)

    report_path = outdir / "report.html"
    report_path.write_text(
        render_report(records, stats, skipped, csv_path, png_path.name),
        encoding="utf-8",
    )

    # 控制台摘要（数据源与行数已在读取阶段打印，这里不重复）
    print(f"时间范围：{records[0]['time_text']} ~ {records[-1]['time_text']}")
    print(f"温度：{stats['temp_min']['temperature']:g} ~ {stats['temp_max']['temperature']:g} ℃"
          f"（均值 {stats['temp_avg']:.1f}）")
    print(f"湿度：{stats['hum_min']['humidity']:g} ~ {stats['hum_max']['humidity']:g} %"
          f"（均值 {stats['hum_avg']:.1f}）")
    print("状态分布：" + "、".join(
        f"{name} {num}" for name, num in
        sorted(stats["status_counts"].items(), key=lambda kv: (-kv[1], kv[0]))
    ))
    print(f"异常记录：{len(stats['abnormal'])} 条")
    print(f"\n已生成：\n  {png_path}\n  {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
