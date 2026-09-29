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
        if detail.get("mode"):
            action = "切换到" + MODE_LABELS.get(detail["mode"], str(detail["mode"]))
        elif detail.get("op") in DEVICE_LABELS:
            on = (detail.get("devices") or {}).get(detail["op"])
            action = f"{'开启' if on else '关闭'}{DEVICE_LABELS[detail['op']]}"
        elif detail.get("op"):
            action = str(detail["op"])
        else:
            action = "设备操作"
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


def duration_text(ms) -> str:
    """毫秒 → 「7 分钟」这种粗粒度说法：复盘读的是量级，不是毫秒。"""
    total_min = int(ms) // 60000
    if total_min < 1:
        return "不足 1 分钟"
    if total_min < 60:
        return f"{total_min} 分钟"
    hours, minutes = divmod(total_min, 60)
    return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"


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
def render_report(records, stats, skipped, csv_path: Path, png_name: str, events=None) -> str:
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

    report_path = outdir / "report.html"
    report_path.write_text(
        render_report(records, stats, skipped, csv_path, png_path.name, events),
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
