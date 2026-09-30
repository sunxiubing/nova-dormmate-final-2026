# -*- coding: utf-8 -*-
"""DormMate 温湿度数据分析与报告生成

读取 M1 网页导出的 dormmate.csv，统计总体情况与温湿度极值，
复用 M1 的判定规则统计各状态数量、提取异常记录，
绘制趋势图并生成 report.html 报告。

用法：
    python analysis/analysis.py                    # 自动查找 dormmate.csv
    python analysis/analysis.py --csv 路径.csv      # 指定 CSV
    python analysis/analysis.py --csv 新数据.csv     # 换数据一键重生成报告

A4 事件复盘：报告里的【事件复盘】板块读的是事件日志（默认 --csv 同目录的
events.csv，由 app.py 的 /api/eventLog 写入）。没有这个文件时板块显示
「暂无事件」，不影响正常报告生成。

B3 今日摘要：报告开头的【今日摘要】把事件日志里最新一天的内容整理成一段人话
（谁出了问题、做了什么、结果怎样），同样只读事件日志 —— 摘要里的每一句都是
按事件数据现算的，没有写死的文案，换一份事件日志跑一次就整段变。
"""

from __future__ import annotations

import argparse
import csv
import html
import json
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

# A1~A4 事件日志。列定义在 app.py 的 EVENT_HEADER，这里按列名取用，不做校验：
# 事件日志坏了也不该拖垮整份报告，缺列的行会走到「未归组事件」区照原样列出来
EVENT_CSV_NAME = "events.csv"

# 事件类型的中文标签，报告里直接引用，文案只此一份
EVENT_TYPE_LABELS = {
    "anomaly_start": "异常开始",
    "priority": "优先处理理由",
    "action": "处置操作",
    "reading": "数据变化",
    "verdict": "最终结果",
}

# A1 的三条优先级规则在报告里的说法。逐字照抄 a1/priority.js 的 REASON_TEXT：
# 同一件事在页面和报告里两种措辞，读的人得先做一次翻译才知道说的是同一条规则
PRIORITY_REASON_LABELS = {
    "only": "唯一异常宿舍",
    "duration": "持续异常时间更长",
    "count": "异常次数更多",
    "order": "按宿舍编号顺序（时长与次数相同）",
}

VERDICT_LABELS = {
    "recovered": "已恢复",
    "attention": "仍需关注",
    "natural": "自然恢复（未处置）",
}
STATE_PENDING = "未完结"

# 设备的显示名。通风和灯已经不再有手动按钮（灯由模式控制），但旧日志里还有这两种操作 ——
# 认不出设备名就只能把英文键名原样印出来，复盘读起来像在念代码
DEVICE_LABELS = {"fan": "风扇", "dehumidifier": "除湿机", "light": "灯", "vent": "通风"}
MODE_LABELS = {"study": "学习模式", "sleep": "睡眠模式", "away": "离寝模式"}

TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M")

# 与 web/script.js 的 NUMBER_PATTERN 保持一致
NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")

STATE_NORMAL = "正常"
TEMP_LABELS = {"cold": "偏冷", "hot": "偏热", "normal": STATE_NORMAL}
HUMIDITY_LABELS = {"wet": "偏湿", "normal": STATE_NORMAL}

# ---------------- 今日摘要（B3）----------------
# 摘要按宿舍逐条点名，所以需要一份「系统监测哪些宿舍」的名单。
# 与 app.py 的 EVENT_NODES 一致：那份名单管着哪些 node 能写进事件日志，
# 这里管着摘要里出现哪几行，改动时要同步。
KNOWN_NODES = ("dorm-a", "dorm-b", "dorm-c")

# 一天的时段划分。摘要里说的是「下午发生 1 次」，不是「14:12 发生 1 次」——
# 摘要是给人一眼扫过去用的，时刻留给下面的明细表。每个时段取 [起, 止) 小时。
DAY_PARTS = ((0, 6, "凌晨"), (6, 12, "上午"), (12, 18, "下午"), (18, 24, "晚间"))

# 异常类型在摘要里的说法。状态原文五种取值（见 app.py 的 compute_status）：
# 偏冷 / 偏热 / 偏湿 / 偏冷+偏湿 / 偏热+偏湿。
# 温度类前面缀「持续」、湿度类不缀，是照着需求给的示例文案定的：
# 「持续偏热」「偏湿」—— 同一句摘要里两种说法并存不是笔误
SUMMARY_CONDITIONS = {
    "偏冷": "持续偏冷",
    "偏热": "持续偏热",
    "偏湿": "偏湿",
    "偏冷+偏湿": "持续偏冷偏湿",
    "偏热+偏湿": "持续偏热偏湿",
}

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


# ---------------------------------------------------------------- 事件日志（A1~A4）
def locate_events(explicit: str | None, csv_path: Path) -> Path:
    """事件日志路径：显式指定优先，否则取 --csv 同目录的 events.csv。

    这里刻意不做全局自动查找（和 locate_csv 不同）。事件日志和它要复盘的那份
    数据必须来自同一次运行，顺手从别的目录抓一个 events.csv，会把两份互不相干的
    时间线拼在一份报告里 —— 看起来更「全」，实际上是在编故事。
    """
    if explicit:
        return Path(explicit).expanduser()
    return csv_path.parent / EVENT_CSV_NAME


def load_events(path: Path):
    """读取 A1~A4 事件日志，返回按时间排好序的事件列表。

    文件不存在直接返回 []：还没跑过处置闭环不是错误，报告照常生成。

    按列位置读，不看表头里写了什么名字。事件日志升到 6 列之后，表头是唯一还
    停在旧版本的东西 —— app.py 只在文件不存在时写表头，所以升级前就存在的
    日志会一直顶着那行 4 列的老表头，后面追加的全是 6 列的行。照名字取字段的话，
    这些新行会整体错位：detail 那一格拿到的是 alert，op_text 落进多出来的列被丢掉。

    detail 解析失败的行不丢、不报错，原样挂到 detail_raw 上。复盘报告的全部价值
    就在「这段时间到底发生了什么」，静默丢掉一行等于篡改时间线 —— 宁可显示一行
    看不懂的原始文本，也不能让一份不完整的时间线冒充完整。
    """
    if not path.is_file():
        return []

    events = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)  # 表头：名字可能是旧的，位置才是真的

        for line_no, row in enumerate(reader, start=2):
            cells = [cell.strip() for cell in row]
            if len(cells) >= 6:
                raw_time, node, event_type, alert, op_text, raw_detail = cells[:6]
            else:
                # 老版本（4 列）的行：time,node,event_type,detail。那一版还没有
                # alert / op_text 两列，第 4 格装的是 detail 本身 —— 按新列序硬读，
                # 这批历史事件的 JSON 会顶到「异常类型」那一格，详情反倒空掉
                raw_time, node, event_type, raw_detail = (cells + [""] * 4)[:4]
                alert = op_text = ""

            if not (raw_time or node or event_type or alert or op_text or raw_detail):
                continue  # 空行

            detail = None
            try:
                parsed = json.loads(raw_detail)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                detail = parsed

            events.append(
                {
                    "line": line_no,
                    "time": parse_time(raw_time),
                    "time_text": raw_time or "（无时间）",
                    "node": node or "（未知宿舍）",
                    "event_type": event_type,
                    "label": EVENT_TYPE_LABELS.get(event_type, event_type or "未知事件"),
                    # 人读的两列：异常类型、用户操作。4 列的老日志里没有这两列，
                    # 是空串 —— 缺列不该让一个文件读不出来，只是那几行少两格信息
                    "alert": alert,
                    "op_text": op_text,
                    "detail": detail,
                    "detail_raw": "" if detail is not None else raw_detail,
                }
            )

    # 稳定排序：时间解析不出来的行沉到最后（组内保持文件顺序）。
    # 报告是照着时间线读的，顺序错了整段复盘就没有意义。
    events.sort(key=lambda e: (e["time"] is None, e["time"] or datetime.min))
    return events


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


# ---------------------------------------------------------------- 事件复盘
def event_detail_text(event) -> str:
    """把一条事件的对象 detail 翻译成一句人话。

    解读不了的类型不猜文案，退回原始 JSON —— 猜错比不显示更糟，复盘报告里
    一句看着像那么回事、其实张冠李戴的描述，会被当成事实读下去。
    """
    detail = event["detail"]
    if detail is None:
        text = event["detail_raw"]
        return f"无法解析：{text}" if text else "（无 detail）"

    kind = event["event_type"]

    if kind == "anomaly_start":
        parts = []
        if "temp" in detail:
            parts.append(f"{detail['temp']:g} ℃")
        if "hum" in detail:
            parts.append(f"{detail['hum']:g} %")
        head = "触发读数 " + " / ".join(parts) if parts else "触发读数未知"
        if detail.get("status"):
            head += f"，判定「{detail['status']}」"
        return head

    if kind == "priority":
        reason = detail.get("reason")
        text = "原因：" + PRIORITY_REASON_LABELS.get(reason, str(reason or "未知"))
        extra = []
        if isinstance(detail.get("durationMs"), (int, float)):
            # 数字前留空格（「已持续 7 分钟」），中文短语前不留（「已持续不足 1 分钟」）
            span = duration_text(detail["durationMs"])
            extra.append("已持续" + (" " + span if span[0].isdigit() else span))
        if isinstance(detail.get("count"), int):
            extra.append(f"异常报文 {detail['count']} 条")
        if extra:
            text += "（" + " · ".join(extra) + "）"
        return text

    if kind == "action":
        action = action_text(detail)
        devices = devices_text(detail.get("devices"))
        text = f"{action} · 当前：{devices}"
        if detail.get("src") == "external":
            text += " · 外部指令"
        return text

    if kind == "reading":
        bits = []
        if isinstance(detail.get("seq"), int):
            bits.append(f"第 {detail['seq']} 条")
        if "temp" in detail:
            bits.append(f"{detail['temp']:g} ℃")
        if "hum" in detail:
            bits.append(f"{detail['hum']:g} %")
        if detail.get("status"):
            bits.append(f"「{detail['status']}」")
        return "　".join(bits) if bits else "一条新报文"

    if kind == "verdict":
        result = detail.get("result")
        count = detail.get("readings")
        if result == "recovered":
            suffix = f"处置后连续 {count} 组报文均正常" if isinstance(count, int) and count else "处置后报文均正常"
            return f"已恢复（{suffix}，系统自动判定）"
        if result == "attention":
            return "仍需关注（处置后仍有异常报文，系统自动判定）"
        if result == "natural":
            return "自然恢复（异常自行结束，用户未做处置）"
        return VERDICT_LABELS.get(result, str(result or "未知结论"))

    return json.dumps(detail, ensure_ascii=False)


def action_text(detail) -> str:
    """一条处置操作的 detail → 「开启风扇」这样一句短语。

    复盘板块和今日摘要都从这里取文案：同一次操作在两处写成两个样子，
    读的人得先做一次翻译才知道说的是同一件事
    """
    if detail.get("mode"):
        return "切换到" + MODE_LABELS.get(detail["mode"], str(detail["mode"]))
    if detail.get("op") in DEVICE_LABELS:
        on = (detail.get("devices") or {}).get(detail["op"])
        return f"{'开启' if on else '关闭'}{DEVICE_LABELS[detail['op']]}"
    if detail.get("op"):
        return str(detail["op"])
    return "设备操作"


def duration_text(ms) -> str:
    """毫秒 → 「7 分钟」这种粗粒度说法：复盘读的是量级，不是毫秒。"""
    total_min = int(ms) // 60000
    if total_min < 1:
        return "不足 1 分钟"
    if total_min < 60:
        return f"{total_min} 分钟"
    hours, minutes = divmod(total_min, 60)
    return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"


# ---------------------------------------------------------------- 今日摘要（B3）
def day_of(events):
    """事件日志里最新的一天（date）。没有一条能解析出时间的就返回 None。

    「今日」由数据决定，不取系统当前日期 —— 报告读的是哪份日志，摘要就写哪一天。
    取系统时钟的话，隔天再打开同一份报告，摘要会声称「今天」什么都没发生
    """
    days = [e["time"].date() for e in events if e["time"] is not None]
    return max(days) if days else None


def part_of_day(when) -> str:
    """小时 → 时段名。DAY_PARTS 从 0 点铺到 24 点，取不到只可能是这张表被改出了空档"""
    for start, end, name in DAY_PARTS:
        if start <= when.hour < end:
            return name
    return "时间不详时段"


def episode_condition(ep) -> str:
    """这一段的异常类型说法。取不到就返回「异常」，不猜具体类型。

    优先用 anomaly_start 里记的 status —— 那是异常发生当时服务端算出的判定，
    比后面拿处置动作去倒推准确。缺了它才退回第一条 reading 的 status
    """
    heads = [e for e in ep["events"] if e["event_type"] == "anomaly_start"]
    if not heads:
        heads = [e for e in ep["events"] if e["event_type"] == "reading"]
    for event in heads:
        status = (event["detail"] or {}).get("status")
        # 「正常」不能当异常类型用：处置后的 reading 记的就是正常，
        # 放它过去就会写出「发生 1 次正常」这种句子。认不出的类型照原样印，
        # 结构变了要能看出来，而不是被悄悄换成「异常」
        if status and status != STATE_NORMAL:
            return SUMMARY_CONDITIONS.get(status, str(status))
    return "异常"


def episode_action(ep):
    """这一段里用户做了什么，返回 (动作说法, 该动作发生的时刻)。

    返回时刻是为了算「开启风扇后 40 分钟恢复」的用时 —— 需求示例里的时长是从
    动作算起的，不是从异常开始算起。用户花多久才处置不该记在处置效果的账上。

    多个动作时按时间取最早的那个：总结一句「做了什么」够了，把三次操作
    全列出来就成了流水账，完整过程在下面的明细表里
    """
    actions = []
    for event in ep["events"]:
        if event["event_type"] != "action":
            continue
        detail = event["detail"]
        if detail is None:
            # detail 坏掉的行还有 op_text 这一列人读文本可用，兜一下。
            # 「· 外部指令」是 dashboard 拼上去的来源后缀（见 onOpsMessage），
            # 摘要要的是一句通顺的话，这个后缀塞在句子里会读成「外部指令后 40 分钟恢复」
            text = event["op_text"].split(" · ")[0].strip()
            if text:
                actions.append((event["time"], text))
            continue
        actions.append((event["time"], action_text(detail)))

    if not actions:
        return None, None
    actions.sort(key=lambda item: (item[0] is None, item[0] or datetime.min))
    return actions[0][1], actions[0][0]


def span_text(ms) -> str:
    """时长接在中文后面：数字前留空格（「后 40 分钟恢复」），
    「不足 1 分钟」这种中文开头的说法不留（「后不足 1 分钟恢复」）。
    与 event_detail_text 里「已持续」那处的处理一致。"""
    span = duration_text(ms)
    return (" " + span) if span[0].isdigit() else span


def episode_facts(ep):
    """一段异常事件的全部结论：摘要正文和明细表都读这一份，不各算各的。

    两处各算一遍的下场是同一段事件在正文里说「40 分钟恢复」、在表里写「42 分钟」，
    读的人只会怀疑整份报告。
    """
    state, cls = episode_state(ep)
    action, action_time = episode_action(ep)
    condition = episode_condition(ep)

    verdicts = [e for e in ep["events"] if e["event_type"] == "verdict"]
    last = verdicts[-1] if verdicts else None
    result = (last["detail"] or {}).get("result") if last else None

    # 用时：已恢复的按「处置动作 → 恢复」算（需求示例里的 40 分钟就是这么来的），
    # 其余按「异常开始 → 结论」算。用户花多久才动手，不该记在处置效果的账上
    span_ms = None
    if last and last["time"]:
        base = action_time if (cls == "ok" and action_time) else ep["start"]
        if base and last["time"] >= base:
            span_ms = (last["time"] - base).total_seconds() * 1000

    if result == "natural" or cls == "idle":
        ending = "，未处置自行恢复"
    elif cls == "ok":
        # 已经判定恢复的，「处置后多久恢复」是这一段最值得记住的数字
        ending = (f"，{action}后{span_text(span_ms)}恢复" if action and span_ms is not None
                  else "，已恢复（未记录处置动作）")
    elif cls == "warn":
        ending = f"，{action}后仍未恢复" if action else "，仍未恢复"
    elif cls == "pending":
        ending = f"，{action}后仍在处置中" if action else "，仍在处置中"
    else:
        ending = f"，结果未知（{state}）"

    return {
        "node": ep["node"],
        "start": ep["start"],
        "bucket": part_of_day(ep["start"]) if ep["start"] else "时间不详时段",
        "condition": condition,
        "action": action,
        "state": state,
        "cls": cls,
        # 表格里用不带前导空格的时长：单元格自己会撑开，「 40 分钟」前面多一格空白
        # 看着像没对齐。前导空格是给正文里接在中文后面的（见 span_text）
        "span": duration_text(span_ms) if span_ms is not None else "",
        # 已恢复的用时是「处置后」用时，其余是「全程」用时 —— 表头得说清是哪个，
        # 否则同一个数字在两行里代表两段不同的时间
        "span_label": "处置后用时" if (cls == "ok" and action_time) else "全程用时",
        "ending": ending,
    }


def episode_sentence(facts) -> str:
    """一段异常事件 → 摘要里的一句话，例如
    「下午发生 1 次持续偏热，开启风扇后 40 分钟恢复」。"""
    return f"{facts['bucket']}发生 1 次{facts['condition']}{facts['ending']}"


def episodes_of_day(events, day):
    """挑出属于这一天的 episode，并剔除日志里没有任何内容、只剩空壳的段。"""
    day_events = [e for e in events if e["time"] is not None and e["time"].date() == day]
    episodes, ungrouped = group_episodes(day_events)

    # episode 的存在只依赖 eid 这一个字段，所以日志里的手改行、坏行也会各自撑起一段。
    # 一段里连一条能读懂的事件都没有的话，写进摘要就是凭空多报了一次事件
    real = [ep for ep in episodes if any(e["event_type"] in EVENT_TYPE_LABELS for e in ep["events"])]
    dropped = len(episodes) - len(real)
    return real, ungrouped, dropped


def build_daily_summary(events):
    """把一天的事件记录整理成「今日摘要」，返回结构化结果，供报告和控制台共用。

    没有事件时 summary 为空串而不是「今日无事」—— 那份事件日志里可能一行都没有，
    说不清是这一天太平还是数据压根没记，把话留给调用方，别在这里替数据下结论
    """
    day = day_of(events)
    if day is None:
        # 键与下面的正常返回保持一致：调用方不必为「没有事件」这一种情况多加判断
        return {"day": None, "summary": "", "lines": [], "nodes": [],
                "total": 0, "need_attention": 0, "natural": 0, "ungrouped": 0, "dropped": 0}

    episodes, ungrouped, dropped = episodes_of_day(events, day)

    def by_start(ep):
        return (ep["start"] is None, ep["start"] or datetime.min)

    # 名单外的宿舍（日志里出现过但不属于监测名单）照样列出来，只是排在名单之后。
    # 只认名单的话，一个没登记进 EVENT_NODES 的宿舍会整段消失 —— 摘要漏报比多报严重
    order = list(KNOWN_NODES) + sorted({ep["node"] for ep in episodes} - set(KNOWN_NODES))

    facts = [episode_facts(ep) for ep in sorted(episodes, key=by_start)]
    by_node = {}
    for item in facts:
        by_node.setdefault(item["node"], []).append(item)

    # 自行恢复的那几段不需要关注，不计入收尾的「N 次」——
    # 需求示例里的 2 次，对应的是两次真正需要人去管的事件
    natural = sum(1 for item in facts if item["cls"] == "idle")

    lines, nodes = [], []
    for node in order:
        items = by_node.get(node, [])
        if not items:
            lines.append(f"{node} 全天整体正常")
        else:
            lines.extend(f"{node} {episode_sentence(item)}" for item in items)
        nodes.append({"node": node, "healthy": not items, "items": items})

    need_attention = len(facts) - natural
    lines.append(f"今日共发生 {need_attention} 次需要关注的环境事件。" if need_attention
                 else "今日无需要关注的环境事件。")

    return {
        "day": day,
        "summary": "；".join(lines),
        "lines": lines,
        "nodes": nodes,
        "total": len(facts),
        "need_attention": need_attention,
        "natural": natural,
        "ungrouped": len(ungrouped),
        "dropped": dropped,
    }


def devices_text(devices) -> str:
    if not isinstance(devices, dict):
        return "设备状态未知"
    # 措辞与 Dashboard 的状态章（opsSummary）保持一致，同一件事两种说法容易让人以为不是一个状态
    on = [DEVICE_LABELS[k] + "已开启"
          for k in ("fan", "dehumidifier", "light", "vent") if devices.get(k)]
    if not on:
        return "设备均未开启"
    return "、".join(on)


def group_episodes(events):
    """按 (宿舍, eid) 把事件归成一个个 episode，返回 (episodes, ungrouped)。

    eid 是异常段起始毫秒，同一段的所有事件共用，多宿舍交错也不会串组。
    没有 eid 的行（手改过的、detail 坏掉的）按同宿舍 episode 的时间区间就近归组；
    仍然归不进去的进 ungrouped，由调用方单列一区显示 —— 任何一行都不静默消失。
    """
    episodes, orphans = {}, []

    for event in events:
        detail = event["detail"] or {}
        eid = detail.get("eid")
        # bool 是 int 的子类，真值判断会把它当数字用，显式排掉
        if isinstance(eid, bool) or not isinstance(eid, (int, float)):
            orphans.append(event)
            continue
        episodes.setdefault((event["node"], eid), []).append(event)

    grouped = {}
    for key, items in episodes.items():
        times = [e["time"] for e in items if e["time"] is not None]
        grouped[key] = {
            "node": key[0],
            "eid": key[1],
            "events": items,
            "start": min(times) if times else None,
            "end": max(times) if times else None,
        }

    ungrouped = []
    for event in orphans:
        best_key, best_dist = None, None
        for key, ep in grouped.items():
            if key[0] != event["node"] or event["time"] is None:
                continue
            lo, hi = ep["start"] or ep["end"], ep["end"] or ep["start"]
            if lo is None or hi is None:
                continue
            dist = timedelta(0) if lo <= event["time"] <= hi else min(
                abs(event["time"] - lo), abs(event["time"] - hi)
            )
            if best_dist is None or dist < best_dist or (dist == best_dist and key[1] < best_key[1]):
                best_key, best_dist = key, dist
        if best_key is None:
            ungrouped.append(event)
        else:
            grouped[best_key]["events"].append(event)

    ordered = sorted(
        grouped.values(),
        key=lambda ep: (ep["start"] is None, ep["start"] or datetime.min, ep["node"], ep["eid"]),
    )
    for ep in ordered:
        ep["events"].sort(key=lambda e: (e["time"] is None, e["time"] or datetime.min))
    return ordered, ungrouped


def episode_state(ep):
    """episode 的终态 = 该 episode 最后一条 verdict；没有就是未完结。"""
    verdicts = [e for e in ep["events"] if e["event_type"] == "verdict"]
    if not verdicts:
        return STATE_PENDING, "pending"
    result = (verdicts[-1]["detail"] or {}).get("result")
    if result == "recovered":
        return VERDICT_LABELS["recovered"], "ok"
    if result == "attention":
        return VERDICT_LABELS["attention"], "warn"
    if result == "natural":
        return VERDICT_LABELS["natural"], "idle"
    return STATE_PENDING, "pending"


def render_events_section(events) -> str:
    """A4：【事件复盘】板块。支持回看 —— 一次问题一条时间线，从上往下就是全过程。"""
    esc = html.escape

    if not events:
        return """
  <h2>事件复盘</h2>
  <div class="panel"><p class="muted">暂无事件。在 Dashboard 上跑完一次「发现 → 处置 → 验证」
  闭环后重新生成报告，这里会按宿舍逐条列出整段事件时间线。</p></div>"""

    episodes, ungrouped = group_episodes(events)

    def cell(value):
        """异常类型 / 操作两格。没内容时显示一个「—」而不是空白：
        空白看着像是这一列没渲染出来，一个占位符才说明「这一条确实没有这一项」。"""
        return f"<td>{esc(value)}</td>" if value else '<td class="muted">—</td>'

    def event_rows(items):
        rows = []
        for e in items:
            rows.append(
                "<tr>"
                f"<td class=\"nowrap\">{esc(e['time_text'])}</td>"
                f"<td>{esc(e['label'])}</td>"
                f"{cell(e['alert'])}"
                f"{cell(e['op_text'])}"
                f"<td>{esc(event_detail_text(e))}</td>"
                "</tr>"
            )
        return "".join(rows)

    blocks = []
    for ep in episodes:
        state, cls = episode_state(ep)
        span = ""
        if ep["start"] and ep["end"] and ep["end"] > ep["start"]:
            span = f" · 跨度 {duration_text((ep['end'] - ep['start']).total_seconds() * 1000)}"
        count = sum(1 for e in ep["events"] if e["event_type"] == "reading")
        blocks.append(f"""
      <div class="episode">
        <h3>{esc(ep['node'])} · {esc(ep['start'].strftime('%Y-%m-%d %H:%M:%S') if ep['start'] else '时间未知')}
          <span class="tag tag--{cls}">{esc(state)}</span></h3>
        <p class="muted">事件 {len(ep['events'])} 条 · 处置后数据变化 {count} 条{esc(span)}</p>
        <table>
          <thead><tr><th>时间</th><th>事件</th><th>异常类型</th><th>操作</th><th>详情</th></tr></thead>
          <tbody>{event_rows(ep['events'])}</tbody>
        </table>
      </div>""")

    if ungrouped:
        # 单独攒出这些行再拼进 f-string：把 "".join(...) 直接写进 f-string 的表达式里，
        # 那串嵌套引号在 Python 3.12 之前是语法错误（PEP 701 才放开）。
        # 报告是给人看的交付物，不该依赖某个小版本的解释器才打得开
        un_rows = "".join(
            "<tr>"
            f"<td class=\"nowrap\">{esc(e['time_text'])}</td>"
            f"<td>{esc(e['node'])}</td>"
            f"<td>{esc(e['label'])}</td>"
            f"{cell(e['alert'])}"
            f"{cell(e['op_text'])}"
            f"<td>{esc(event_detail_text(e))}</td>"
            "</tr>"
            for e in ungrouped
        )
        blocks.append(f"""
      <div class="episode">
        <h3>未归组事件（{len(ungrouped)} 条）<span class="tag tag--pending">无 eid</span></h3>
        <p class="muted">这些行没有 episode 标识（detail 里缺 eid 或解析失败），
        无法挂到上面任何一段异常上，原样列出以免漏掉：</p>
        <table>
          <thead><tr><th>时间</th><th>宿舍</th><th>事件</th><th>异常类型</th><th>操作</th><th>详情</th></tr></thead>
          <tbody>{un_rows}</tbody>
        </table>
      </div>""")

    settled = sum(1 for ep in episodes if episode_state(ep)[0] != STATE_PENDING)
    return f"""
  <h2>事件复盘</h2>
  <div class="panel">
    <p class="muted">共 {len(events)} 条事件记录，归为 {len(episodes)} 段异常事件，
    其中已得出结论 {settled} 段。每段从异常开始、优先处理理由、用户处置、后续数据变化
    到最终结果，按时间顺序完整回看。</p>
{"".join(blocks)}
  </div>"""


# ---------------------------------------------------------------- 报告
def render_summary_section(summary) -> str:
    """B3：【今日摘要】板块。放在报告最前面 —— 读者先要知道今天出没出事，
    再决定要不要往下翻细节。正文写的每一句，下面那张表里都有出处。"""
    esc = html.escape

    if summary["day"] is None:
        return """
  <h2>今日摘要</h2>
  <div class="panel"><p class="muted">事件日志里没有一条能解析出时间的事件，
  生成不了今日摘要。在 Dashboard 上跑完一次「发现 → 处置 → 验证」闭环后
  重新生成报告即可。</p></div>"""

    rows = []
    for item in summary["nodes"]:
        if item["healthy"]:
            rows.append(
                "<tr>"
                f'<td class="nowrap">{esc(item["node"])}</td>'
                '<td colspan="5" class="muted">全天整体正常（当日无异常事件记录）</td>'
                "</tr>"
            )
            continue
        for fact in item["items"]:
            when = fact["start"].strftime("%H:%M:%S") if fact["start"] else "时间未知"
            span = f"{fact['span']}（{fact['span_label']}）" if fact["span"] else "—"
            rows.append(
                "<tr>"
                f'<td class="nowrap">{esc(fact["node"])}</td>'
                f'<td class="nowrap">{esc(when)}</td>'
                f'<td>{esc(fact["condition"])}</td>'
                f'<td>{esc(fact["action"] or "—")}</td>'
                f'<td><span class="tag tag--{esc(fact["cls"])}">{esc(fact["state"])}</span></td>'
                f'<td class="nowrap">{esc(span)}</td>'
                "</tr>"
            )

    # 归不了组、以及被剔除的空壳段在这里报个数。摘要只认归好组的段，
    # 但「有几条没能归类」得让人看见 —— 悄悄不提，读的人会以为日志是干净的
    caveats = []
    if summary["ungrouped"]:
        caveats.append(f"另有 {summary['ungrouped']} 条事件没有 episode 标识（detail 里缺 eid 或解析失败），"
                       "无法归入具体某次异常，未计入上面的次数，原样列在【事件复盘】的「未归组事件」里")
    if summary["dropped"]:
        caveats.append(f"另有 {summary['dropped']} 段只带 episode 标识、不含任何可读事件，已忽略")
    caveat_html = "".join(f"<li>{esc(text)}</li>" for text in caveats)
    caveat_block = f'<ul class="skipped">{caveat_html}</ul>' if caveats else ""

    return f"""
  <h2>今日摘要（{esc(summary["day"].strftime("%Y-%m-%d"))}）</h2>
  <div class="panel">
    <p class="headline">{esc(summary["summary"])}</p>
    <table>
      <thead><tr><th>宿舍</th><th>时间</th><th>异常类型</th><th>处置</th><th>结果</th><th>用时</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <p class="muted">上面这段话由 analysis/analysis.py 依事件日志自动生成，不是写死的文案：
    换一份事件日志重新运行，摘要会整段重写。表中「处置」列取该段最早的一次操作，
    完整操作序列见下方【事件复盘】；「用时」在已恢复的行里是处置动作到判定的间隔，
    其余行是异常开始到判定的间隔。全天整体正常的宿舍，依据是当日没有异常事件记录
    —— 节点停了不发报文，在这里也会显示为正常。</p>
    {caveat_block}
  </div>"""


def render_report(records, stats, skipped, csv_path: Path, png_name: str, events=None,
                  summary=None) -> str:
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

    events_section = render_events_section(events or [])
    summary_section = render_summary_section(summary) if summary else ""

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
  .headline {{ margin: 0 0 14px; padding-left: 12px; border-left: 3px solid var(--accent);
               font-size: 15.5px; line-height: 1.75; }}
  .skipped {{ margin: 0; padding-left: 20px; color: var(--muted); font-size: 13px; }}
  img.trend {{ display: block; width: 100%; height: auto; border-radius: 10px; }}
  .episode {{ border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px;
              margin-top: 14px; background: #fbfcfe; }}
  .episode h3 {{ margin: 0 0 4px; font-size: 14px; }}
  .episode .muted {{ margin: 0 0 8px; }}
  .tag {{ display: inline-block; margin-left: 6px; padding: 1px 9px; border-radius: 999px;
          font-size: 12px; font-weight: 600; vertical-align: 1px; }}
  .tag--ok {{ background: #e2f6ee; color: var(--ok); }}
  .tag--warn {{ background: #fdeae6; color: var(--warn); }}
  .tag--pending {{ background: #fdeae6; color: var(--warn); }}
  .tag--idle {{ background: #eef1f8; color: var(--muted); }}
  td.nowrap {{ white-space: nowrap; color: var(--muted); }}
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

{summary_section}

  <h2>数据概况</h2>
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
{events_section}
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
    parser.add_argument("--events", help="A1~A4 事件日志 CSV，缺省为 --csv 同目录的 events.csv")
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

    # 事件日志缺失不算错误：没有文件 = 还没跑过处置闭环，报告照常出（复盘板块显示「暂无事件」）。
    # 但显式传了 --events 却找不到，多半是路径写错了，得让人看见 —— 否则报告里那句
    # 「暂无事件」会被当成「确实没发生过事件」，其实是拿错了路径。
    events_path = locate_events(args.events, csv_path)
    events = load_events(events_path)
    if events_path.is_file():
        print(f"事件日志：{len(events)} 条（{events_path.resolve()}）")
    elif args.events:
        print(f"[警告] 找不到事件日志：{events_path}，报告中的【事件复盘】将显示「暂无事件」。",
              file=sys.stderr)
    else:
        print(f"事件日志：{events_path} 不存在，【事件复盘】显示「暂无事件」")

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

    # B3 今日摘要：只由事件日志决定，和 CSV 的统计各算各的 ——
    # 摘要讲的是「谁出了问题、做了什么、结果怎样」，那是事件日志里的事，
    # 温湿度 CSV 里没有宿舍、也没有处置动作
    summary = build_daily_summary(events)

    report_path = outdir / "report.html"
    report_path.write_text(
        render_report(records, stats, skipped, csv_path, png_path.name, events, summary),
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

    # 摘要打到控制台上，是为了让「这段话是程序生成的」当场可见：
    # 报告是覆盖写的，屏幕上这一行和 report.html 里那一句必然同源同次
    print(f"\n今日摘要（{summary['day'] or '事件日志里没有可用时间'}）：")
    print(f"  {summary['summary'] or '（无事件记录，未生成摘要）'}")

    print(f"\n已生成：\n  {png_path}\n  {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
