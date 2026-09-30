# -*- coding: utf-8 -*-
"""C1/C2 模拟数据生成器：为每个宿舍节点造一份「平时」的历史，再手写几个待判样本

C1 要的是「让模型先认识这间房平时什么样」，那就得先有「平时」。仓库里现有的
温湿度 CSV（data/dormmate.csv、data/b3/dormmate.csv）只有一个总体的时间序列，
既没有宿舍列、也不是按房间分的 —— 直接拿来训练，三间房会共用一条基线，
「dorm-b 平时 21 ℃」这种房间自己的习惯就丢了。所以这里按节点各生成一份。

两份数据严格分开，这是 C1 的硬要求（待判的新数据不能先混进训练历史再判断自己）：

  data/c12/dorm-a_history.csv   ← 训练用：40 条/节点，正态抽样，每小时一条
  data/c12/dorm-b_history.csv
  data/c12/dorm-c_history.csv
  data/c12/new_samples.csv      ← 待判用：10 条手写样本，覆盖三类对照情形

历史由脚本生成、新样本手写，两者的 source 列都写「模拟」——报告里读到的每一个
数字都能一眼看出不是真实采集的。随机数用固定 SEED，同一份代码重复跑出来的文件
逐字节相同（改数据就改不出第二种结果），C1 的「可复现」才立得住。

用法：
    python analysis/gen_c12_data.py              # 生成/覆盖 data/c12 下的 4 个 CSV
    python analysis/gen_c12_data.py --outdir 别的目录
"""

from __future__ import annotations

import argparse
import sys
import zlib
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# 固定种子：同一份代码任何时候跑出来的历史数据都一样。
# 换个种子 = 换一批历史，「ML 认不认得出异常」的结论也会跟着变 ——
# 所以它是个要写进报告的参数，不是随手一个常数
SEED = 42

# 每个节点的历史条数。需求给的是 30~50 条，取中间值：
# 太少（<20）模型见过的「平时」太窄，正常波动都判成异常；太多就不是轻量演示了
N_HISTORY = 40

ROOT = Path(__file__).resolve().parent.parent
C12_DIR = ROOT / "data" / "c12"

# 历史起始时刻。固定在过去的某一天，不用 datetime.now()：
# 报告每次重新生成时读到的历史必须一模一样，带上「现在」就不再可复现了
HISTORY_START = datetime(2026, 9, 20, 0, 0, 0)
SAMPLE_START = datetime(2026, 9, 28, 8, 0, 0)
SAMPLE_INTERVAL_MIN = 30

SOURCE_SIMULATED = "模拟"

# 每个节点「平时」的画像：(温度均值, 温度标准差), (湿度均值, 湿度标准差)
# 三间房刻意各不相同 —— 三个节点共用一条基线的话，C1 要的「这个宿舍平时什么样」
# 就退化成「宿舍楼平时什么样」，dorm-b 那 24.0 ℃ / 35 % 的新样本也就测不出东西
NODE_PROFILES = {
    "dorm-a": ((25.0, 0.8), (60.0, 3.0)),
    "dorm-b": ((21.0, 0.8), (50.0, 3.0)),
    "dorm-c": ((27.0, 0.8), (65.0, 3.0)),
}

# 待判的新样本，手写不抽样，为的是让 C2 的三类情形都露面：
#   规则正常 · ML 正常   —— 对照组，证明模型不是见谁都喊异常
#   规则异常 · ML 异常   —— 越过了固定阈值，两边都看得见
#   规则正常 · ML 异常   —— C2 真正要找的那一类：两维各自都在阈值内，
#                          固定规则一路放行，但跟这间房自己的「平时」比明显不对
NEW_SAMPLES = [
    # (node, 温度, 湿度, 这一类想覆盖的情形)
    ("dorm-a", 25.2, 60.4),   # 规则正常 · ML 正常（贴着 dorm-a 平时的中心）
    ("dorm-a", 31.5, 62.0),   # 规则偏热 · ML 异常
    ("dorm-a", 29.0, 72.0),   # 规则正常 · ML 异常（作业里举的那个例子）
    ("dorm-a", 25.5, 78.0),   # 规则偏湿 · ML 异常
    ("dorm-b", 21.1, 50.3),   # 规则正常 · ML 正常
    ("dorm-b", 15.8, 55.0),   # 规则偏冷 · ML 异常
    ("dorm-b", 24.0, 35.0),   # 规则正常 · ML 异常（两维都还合规，但都不像 dorm-b）
    ("dorm-c", 27.1, 65.2),   # 规则正常 · ML 正常
    ("dorm-c", 33.0, 66.0),   # 规则偏热 · ML 异常
    ("dorm-c", 22.0, 50.0),   # 规则正常 · ML 异常（用在 dorm-a/b 身上恰好是常态）
]


def build_history(node: str, rng: np.random.Generator, n_rows: int = N_HISTORY) -> pd.DataFrame:
    """一个节点的历史：按画像正态抽样，每小时一条，全部标记为模拟数据。

    n_rows 可调是为了 C4：把历史砍到几条，看模型的判断会不会变。
    抽样仍是同一支 rng、同一个顺序，所以 n_rows=8 出来的正是 n_rows=40 那份的
    前 8 行 —— 两份历史只有长度不同，没有别的变量，C4 实验才说得清是长度的锅
    """
    (temp_mean, temp_std), (hum_mean, hum_std) = NODE_PROFILES[node]
    temperature = rng.normal(temp_mean, temp_std, n_rows)
    humidity = rng.normal(hum_mean, hum_std, n_rows)
    return pd.DataFrame({
        "time": [(HISTORY_START + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S")
                 for i in range(n_rows)],
        # 保留 1 位小数：和 M1/M4 落盘的精度一致，也让报告里引用的
        # 「平时约 25.0 ± 0.8 ℃」跟 CSV 里的数字对得上
        "temperature": np.round(temperature, 1),
        "humidity": np.round(humidity, 1),
        "source": SOURCE_SIMULATED,
    })


def build_samples() -> pd.DataFrame:
    """待判的新样本。一行一个报文，node 列说明它属于哪个节点。"""
    return pd.DataFrame({
        "node": [n for n, _, _ in NEW_SAMPLES],
        "time": [(SAMPLE_START + timedelta(minutes=SAMPLE_INTERVAL_MIN * i))
                 .strftime("%Y-%m-%d %H:%M:%S") for i in range(len(NEW_SAMPLES))],
        "temperature": [t for _, t, _ in NEW_SAMPLES],
        "humidity": [h for _, _, h in NEW_SAMPLES],
        "source": SOURCE_SIMULATED,
    })


def write_csv(df: pd.DataFrame, path: Path) -> None:
    """统一出口：不带索引列，编码与仓库其它 CSV 一致（utf-8，读的一方容 BOM）。"""
    df.to_csv(path, index=False, encoding="utf-8", float_format="%.1f")
    print(f"  {path}  ({len(df)} 行)")


def count_rows(path: Path) -> int:
    """数一个 CSV 的数据行数（不含表头）。只用来在结尾回显实测样本攒了多少条。"""
    with path.open(encoding="utf-8-sig") as fh:
        return max(sum(1 for _ in fh) - 1, 0)


def configure_stdout():
    """与 analysis.py 同一处理：输出被重定向时强制 UTF-8，避免中文在管道里成乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(
        description="生成 C1/C2 用的模拟历史数据与待判新样本。"
    )
    parser.add_argument("--outdir", default=str(C12_DIR),
                        help=f"输出目录，默认 {C12_DIR}")
    parser.add_argument("--rows", type=int, default=N_HISTORY,
                        help=f"每个节点的历史条数，默认 {N_HISTORY}。C4 要演示"
                             f"「历史太少会怎样」时把它调小（例如 --rows 5），"
                             f"配合 analysis.py --ml-history-dir 换一份历史重跑")
    args = parser.parse_args(argv)

    if args.rows < 1:
        print(f"历史条数至少 1 条，收到 {args.rows}")
        return 2

    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"输出目录：{outdir}")
    # 每个节点各用一个从 SEED 派生的生成器，而不是三间房共用一个：
    # 共用一个的话，dorm-a 抽多少个数会影响 dorm-b 抽到什么，往中间插一个节点
    # 就会把后面所有节点的历史全改掉。按节点名派生（crc32 对同一个名字永远同一个数），
    # 各房间的数据互不牵连，节点增删也只影响它自己
    for node in NODE_PROFILES:
        rng = np.random.default_rng([SEED, zlib.crc32(node.encode("utf-8"))])
        write_csv(build_history(node, rng, args.rows), outdir / f"{node}_history.csv")

    write_csv(build_samples(), outdir / "new_samples.csv")

    # 实时样本表：只建空表，绝不覆盖。这份文件由 app.py 收到 MQTT 报文时往里追加，
    # 是实测数据 —— 重跑一次生成器就把用户攒了半天的记录清掉，是不可接受的副作用。
    # 建它的目的是让 ml_anomaly 一开始就有个位置可读，而不是「文件不存在」和
    # 「文件是空的」两种状态各写一套分支
    live_path = outdir / "live_samples.csv"
    live_kept = live_path.exists()
    if live_kept:
        print(f"  {live_path}  (已存在，保留不动 —— 里面是 app.py 记下的实时报文)")
    else:
        live_path.write_text("node,time,temperature,humidity,source\n", encoding="utf-8")
        print(f"  {live_path}  (新建空表，等 app.py 订阅 MQTT 后往里追加)")

    print("\n以上数据均为模拟数据，仅用于 C1/C2 机器学习与固定规则的对照演示，"
          "不是真实采集记录（live_samples.csv 例外：那份是实测的）。")
    if live_kept:
        print(f"其中 live_samples.csv 现有 {count_rows(live_path)} 条实测报文，未被本次生成改动。")
    print(f"历史每条 {args.rows} 行"
          + ("（非默认值：仅供 C4「历史太少」实验使用）" if args.rows != N_HISTORY else ""))
    print("历史（_history.csv）用于训练，new_samples.csv 用于待判 —— 两者分开，"
          "新样本不会混进训练集。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
