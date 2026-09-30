# -*- coding: utf-8 -*-
"""C1/C2 机器学习辅助判定：每节点 IsolationForest 基线 + 固定规则并排对照

C1 —— 先让模型认识「平时」是什么样。
    读 data/c12/<node>_history.csv（模拟生成的、每个宿舍自己的历史温湿度），
    对每个节点单独训一个 IsolationForest，得到「这间房平时什么样」的基线。
    训练只用历史，待判的新样本一行都不混进去 —— 拿要判断的数据训练，
    再让它判断自己，任何一条都会被认成「见过」，这套东西就白做了。

C2 —— 固定规则没发现的情况，ML 能不能发现。
    同一批 new_samples.csv，固定规则（t < 18 偏冷 / t ≥ 30 偏热 / h ≥ 75 偏湿）
    和 ML 各判各的，结果并排放在一起。要找的是「规则说正常、ML 说跟这间房
    平时明显不一样」的那种样本：两维各自都在阈值内，固定规则一路放行，
    可它跟这间房自己的习惯差得很远。这类差异的原因在报告里逐条用真实数字
    说明（平时均值、标准差、这一条偏了几个标准差）。
    不调参凑结果：没出现就照实写「本次测试未出现」。

判定规则不在这里重抄一份。固定阈值在 analysis.py 里定义，调用时整包传进来
（见 analysis.py 的 ML_RULES），独立运行时惰性 `import analysis` 取同一份 ——
阈值散成两处，改了一处另一处不知道，两边对同一读数给出不同结论是迟早的事。

用法：
    python analysis/ml_anomaly.py                       # 用默认的 data/c12 数据
    python analysis/ml_anomaly.py --history-dir 别的目录  # 换一份历史重新跑（C1 验收）
    python analysis/ml_anomaly.py --samples 别的.csv      # 换一批待判样本
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd

# 与 analysis.KNOWN_NODES / app.py 的 EVENT_NODES 是同一份名单，改动时要同步
NODES = ("dorm-a", "dorm-b", "dorm-c")

# 参与判定的两列。IsolationForest 只看这两个维度 —— 也是 M1~M6 一直在判的两维
FEATURES = ["temperature", "humidity"]

# 模型参数。contamination="auto" 用固定偏移量当阈值（不是按比例砍），
# 40 条样本 / 100 棵树下结果稳定；random_state 固定成 42，
# 同一份历史重复跑判出来的结果是同一个，C1 要的「可复现」靠它
MODEL_PARAMS = {"n_estimators": 100, "contamination": "auto", "random_state": 42}

PRED_NORMAL = 1     # predict 返回 1：接近历史的样子
PRED_ANOMALY = -1   # predict 返回 -1：与历史明显不同

ML_NORMAL_TEXT = "接近平时"
ML_ANOMALY_TEXT = "与平时明显不同"

# 固定规则的口径。这里是给人读的一句话说明，判断本身仍只走
# analysis.py 传进来的那三个函数 —— 报告里说一套、实际算另一套是不行的
RULE_TEXT = "温度 < 18 ℃ 偏冷 / 温度 ≥ 30 ℃ 偏热 / 湿度 ≥ 75 % 偏湿"

ROOT = Path(__file__).resolve().parent.parent
C12_DIR = ROOT / "data" / "c12"
HISTORY_SUFFIX = "_history.csv"
SAMPLES_NAME = "new_samples.csv"

# 实时样本：app.py 订阅 MQTT 后，每收到一条报文就往这个文件追加一行。
# 和 new_samples.csv 的区别是数据来源 —— 那份是手写的模拟对照样本，
# 这份是 MQTTX 真发出来的读数，所以报告里两块分开列，不会混成一本账
LIVE_NAME = "live_samples.csv"
LIVE_SOURCE_REAL = "实时"
# 报告里实时对照表最多列这么多行。报文是持续来的，全列出来会把报告撑爆；
# 文件里留 120 行，报表只取最新的这一段
LIVE_DISPLAY_ROWS = 12

# ---------------------------------------------------------------- C3 导出
# 判定结果导出成 JSON 给 Dashboard 读。页面不重算模型 —— 作业里说的轻量做法
# 就是「Python 生成结果文件、再由现有页面读取展示」，在浏览器里再实现一遍
# IsolationForest 打分是把简单问题做复杂，还多一份会和这里对不上的实现
VERDICTS_DIR = ROOT / "dashboard"
VERDICTS_NAME = "ml_verdicts.json"
# 每间房在 JSON 里带几条最近的判定。页面拿当前读数和它们逐字比对：
# 对上了说明这条读数上次分析时已经判过，可以直接显示；对不上就退回最新一条
# 并标「上次分析」。带少了匹配率低，带多了文件白胖，8 条够用
VERDICTS_RECENT = 8

# ---------------------------------------------------------------- C4 实验
# C4 要主动找一个「判断不太理想」的例子。两个实验都只改「喂给模型的历史」，
# 样本、阈值、模型参数、随机种子全是 C1/C2 那一套 —— 结论必须能追溯到是
# 历史数据的问题，不能是因为我们把模型调坏了
C4_FEW_ROWS = 5              # 实验一：只留前 5 条历史，看判定会不会变
C4_POLLUTE_COUNT = 20        # 实验二：往历史里掺 20 条偏高读数
C4_POLLUTE_TEMP_DELTA = 2.0  # 掺入读数 = 该节点历史温度均值 + 2.0 ℃
C4_POLLUTE_HUM_DELTA = 6.0   # 掺入读数 = 该节点历史湿度均值 + 6.0 %
# 实验一还要看「同一条读数在不同历史条数下判成什么」——
# 只给一个条数的话，读者没法判断这个结论是稳定现象还是刚好撞上
C4_STABILITY_ROWS = (3, 5, 8, 12, 20, 40)


# ---------------------------------------------------------------- 读取数据
def load_history(node: str, history_dir: Path):
    """读一个节点的历史 CSV。文件在但列不对时抛 ValueError，让调用方记成一条原因。"""
    path = Path(history_dir) / f"{node}{HISTORY_SUFFIX}"
    if not path.is_file():
        raise FileNotFoundError(f"缺少历史数据 {path.name}")
    # utf-8-sig：容忍带 BOM 的 CSV（Excel 另存过的文件就带），仓库既有读法
    df = pd.read_csv(path, encoding="utf-8-sig")
    missing = [c for c in FEATURES if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name} 缺少列 {'、'.join(missing)}")
    df = df.dropna(subset=FEATURES)
    if df.empty:
        raise ValueError(f"{path.name} 里没有可用的温湿度记录")
    return df


def load_new_samples(samples_path: Path) -> pd.DataFrame:
    """读待判的新样本。列不对直接抛，这是调用方给错文件，不该悄悄跳过。"""
    df = pd.read_csv(Path(samples_path), encoding="utf-8-sig")
    missing = [c for c in ("node", *FEATURES) if c not in df.columns]
    if missing:
        raise ValueError(f"{Path(samples_path).name} 缺少列 {'、'.join(missing)}")
    return df.dropna(subset=FEATURES)


def profile_stats(history: pd.DataFrame) -> dict:
    """这间房「平时」的画像。报告里解释异常时要引用真实数字，不能只说「偏大」。"""
    return {
        "temp_mean": float(history["temperature"].mean()),
        "temp_std": float(history["temperature"].std()),
        "hum_mean": float(history["humidity"].mean()),
        "hum_std": float(history["humidity"].std()),
    }


# ---------------------------------------------------------------- 训练
def train_node_models(history_dir: Path, nodes=NODES):
    """每个节点各训一个模型：只吃自己的历史。返回 (models, profiles, counts, missing)。"""
    # sklearn 在这里才 import：没装它只该让 C1/C2 这一块空着，
    # 不该把整份报告一起拖下水（M2/B3 跟机器学习没有关系）
    try:
        from sklearn.ensemble import IsolationForest
    except ImportError:
        raise ImportError(
            "未安装 scikit-learn，无法运行 C1/C2。安装：pip install scikit-learn"
        )

    models, profiles, counts, missing = {}, {}, {}, []
    for node in nodes:
        try:
            history = load_history(node, history_dir)
        except (FileNotFoundError, ValueError) as exc:
            missing.append((node, str(exc)))
            continue
        # 关键：fit 只喂历史。待判的新样本一行都不在这里 ——
        # 混进去的话，模型会把「要判断的那条」当成见过的样子，异常就永远测不出来
        model = IsolationForest(**MODEL_PARAMS)
        model.fit(history[FEATURES])
        models[node] = model
        profiles[node] = profile_stats(history)
        counts[node] = len(history)
    return models, profiles, counts, missing


def evaluate(models, samples: pd.DataFrame, rules: dict) -> list:
    """固定规则与 ML 各判一次，一条样本产出一行对照结果。"""
    judge_temperature = rules["judge_temperature"]
    judge_humidity = rules["judge_humidity"]
    build_status = rules["build_status"]

    # 按节点分组一次性判：训练吃的是带列名的 DataFrame，推理也给同名的列
    # （列序由 FEATURES 定死）。喂裸列表 sklearn 会警告「feature names 对不上」，
    # 更要紧的是列序一旦写反它不报错，只会静静地把湿度当温度算
    verdicts = {}
    node_series = samples["node"].astype(str)
    for node, group in samples.groupby(node_series, sort=False):
        if node not in models:
            continue
        features = group[FEATURES]
        predictions = models[node].predict(features)
        # decision_function 给出「离平常有多远」的连续分数：负数越负越不像平时。
        # 光看 predict 的 ±1 只知道结论，看不出「差一点点」还是「差很远」
        scores = models[node].decision_function(features)
        for pos, pred, score in zip(group.index, predictions, scores):
            verdicts[pos] = (int(pred), float(score))

    rows = []
    for pos, s in samples.iterrows():
        if pos not in verdicts:
            continue
        node = str(s["node"])
        temperature = float(s["temperature"])
        humidity = float(s["humidity"])
        ml_pred, ml_score = verdicts[pos]

        # 固定规则：只看提前写死的阈值，跟这间房是谁没关系
        rule_status = build_status(judge_temperature(temperature), judge_humidity(humidity))

        rule_normal = rule_status == "正常"
        ml_anomaly = ml_pred == PRED_ANOMALY
        rows.append({
            "node": node,
            "time_text": str(s.get("time", "")),
            "temperature": temperature,
            "humidity": humidity,
            "rule_status": rule_status,
            "rule_normal": rule_normal,
            "ml_pred": ml_pred,
            "ml_text": ML_ANOMALY_TEXT if ml_anomaly else ML_NORMAL_TEXT,
            "ml_score": ml_score,
            "ml_anomaly": ml_anomaly,
            # 两边结论不一致：一个说正常、另一个说不正常
            "mismatch": rule_normal != (ml_pred == PRED_NORMAL),
        })
    return rows


# ---------------------------------------------------------------- C3 导出
def _verdict_cell(row: dict) -> dict:
    """一行判定里页面用得上的字段。整行塞进 JSON 会带上 rule_normal、mismatch
    这些内部标记，页面用不上，还容易哪天改了键名就把页面弄坏。"""
    return {
        "time": row["time_text"],
        "temperature": round(row["temperature"], 1),
        "humidity": round(row["humidity"], 1),
        "rule_status": row["rule_status"],
        "ml_text": row["ml_text"],
        "ml_score": round(row["ml_score"], 4),
        "ml_anomaly": bool(row["ml_anomaly"]),
    }


def build_verdicts_export(result) -> dict:
    """给 Dashboard 读的判定快照。

    页面不重算模型：这份 JSON 记的是「上一次跑分析时算出来的结论」，
    所以 generated_at 和 note 都写进去，页面也要照实标出来 ——
    读的人得知道这一列不是随报文实时变的，不然会把旧判定当成当前判定。
    """
    by_node = {}
    for row in result["live_rows"]:
        by_node.setdefault(row["node"], []).append(row)

    nodes = {}
    for node, rows in by_node.items():
        profile = result["profiles"].get(node)
        if profile is None:
            continue
        # 新 → 旧：页面从头往尾找匹配，最近的那条先被撞上
        recent = rows[-VERDICTS_RECENT:][::-1]
        nodes[node] = {
            "profile": {
                "temperature_mean": round(profile["temp_mean"], 2),
                "temperature_std": round(profile["temp_std"], 2),
                "humidity_mean": round(profile["hum_mean"], 2),
                "humidity_std": round(profile["hum_std"], 2),
            },
            "latest": _verdict_cell(recent[0]),
            "recent": [_verdict_cell(r) for r in recent],
        }

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": ("本文件由 python analysis/analysis.py 生成，是上一次分析时的判定结论，"
                 "不是随 MQTT 报文实时计算的。基线来自模拟历史数据。"),
        "history_dir": str(result["history_dir"]),
        "rule_text": RULE_TEXT,
        "ml_normal_text": ML_NORMAL_TEXT,
        "ml_anomaly_text": ML_ANOMALY_TEXT,
        "nodes": nodes,
    }


def write_verdicts(result) -> Path | None:
    """把判定快照写到 dashboard/ 下。写不进去（目录只读、文件被占）只提示一行，
    不能让整份报告因为这一份附属产物而生成失败。"""
    try:
        VERDICTS_DIR.mkdir(parents=True, exist_ok=True)
        path = VERDICTS_DIR / VERDICTS_NAME
        path.write_text(
            json.dumps(build_verdicts_export(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
    except OSError as exc:
        print(f"  （Dashboard 判定文件未能写出：{exc}）", file=sys.stderr)
        return None


# ---------------------------------------------------------------- C4 实验
def _train_on(history_map: dict):
    """按给定的历史重训一批模型。C4 的两个实验都要换一份历史重训，
    抽出来省得写三遍。sklearn 仍在这里才 import，缺依赖要能整块退化。"""
    try:
        from sklearn.ensemble import IsolationForest
    except ImportError:
        raise ImportError(
            "未安装 scikit-learn，无法运行 C4 实验。安装：pip install scikit-learn"
        )

    models = {}
    for node, history in history_map.items():
        model = IsolationForest(**MODEL_PARAMS)
        model.fit(history[FEATURES])
        models[node] = model
    return models


def _judge(models, node: str, points: list) -> list:
    """给一批 (温度, 湿度) 打分，返回 [(pred, score)]，与 points 同序。
    和 evaluate 一样传带列名的 DataFrame —— 列序写反 sklearn 不报错，
    只会静静地把湿度当温度算，那种错查起来最费劲。"""
    frame = pd.DataFrame({
        FEATURES[0]: [p[0] for p in points],
        FEATURES[1]: [p[1] for p in points],
    })
    preds = models[node].predict(frame)
    scores = models[node].decision_function(frame)
    return [(int(p), float(s)) for p, s in zip(preds, scores)]


def run_c4_experiments(history_dir, samples: pd.DataFrame) -> dict:
    """C4：主动找 ML 判得不理想的例子。

    两个探针来源：每间房自己历史温湿度的均值（一条谁看都说「普通」的读数），
    加上 C1/C2 那 10 条手写样本。均值那条最能把话说死 —— 连房间自己的平均值
    都被判成异常，就不剩「你这条样本本身就很怪」的余地了。

    纯内存计算，不落盘。random_state 固定，同一份历史每次跑出来的数字一样。
    没找到就如实返回空列表，报告里照写「本次实验未出现」。
    """
    result = {
        "available": False,
        "skip_reason": "",
        "few_rows": C4_FEW_ROWS,
        "pollute_count": C4_POLLUTE_COUNT,
        "pollute_temp_delta": C4_POLLUTE_TEMP_DELTA,
        "pollute_hum_delta": C4_POLLUTE_HUM_DELTA,
        "probes": [],
        "few": {"checked": 0, "flips": [], "stability": []},
        "polluted": {"checked": 0, "flips": [], "shifts": []},
    }

    full_history = {}
    for node in NODES:
        try:
            full_history[node] = load_history(node, history_dir)
        except (FileNotFoundError, ValueError):
            continue  # 某个节点缺历史就少测一间房，其余照测
    if not full_history:
        result["skip_reason"] = f"{history_dir} 下没有可用的历史数据，无法做 C4 实验。"
        return result

    try:
        full_models = _train_on(full_history)
    except ImportError as exc:
        result["skip_reason"] = str(exc)
        return result

    probes = []
    for node, history in full_history.items():
        profile = profile_stats(history)
        probes.append({
            "node": node,
            "temperature": round(profile["temp_mean"], 1),
            "humidity": round(profile["hum_mean"], 1),
            "note": "该宿舍历史上的均值",
        })
    for _, s in samples.iterrows():
        node = str(s["node"])
        if node in full_history:
            probes.append({
                "node": node,
                "temperature": round(float(s["temperature"]), 1),
                "humidity": round(float(s["humidity"]), 1),
                "note": "C1/C2 的待判样本",
            })
    result["probes"] = probes

    # 一次判完所有探针，再把结果按 (节点, 温度, 湿度) 摊平成字典 ——
    # 后面两个实验都要拿它当基准，逐条重判同一个点纯属浪费
    baseline = {}
    for node in full_history:
        points = [(p["temperature"], p["humidity"]) for p in probes if p["node"] == node]
        for point, verdict in zip(points, _judge(full_models, node, points)):
            baseline[(node, point[0], point[1])] = verdict

    # 实验一：历史太少。找「40 条判正常、砍到 5 条判异常」的翻转 ——
    # 这个方向才是「模型冤枉了一条普通读数」，反向的不是这个实验要问的问题
    few_history = {n: h.head(C4_FEW_ROWS) for n, h in full_history.items()}
    few_models = _train_on(few_history)
    few_profiles = {n: profile_stats(h) for n, h in few_history.items()}
    few_flips = []
    for (node, temperature, humidity), (full_pred, full_score) in baseline.items():
        few_pred, few_score = _judge(few_models, node, [(temperature, humidity)])[0]
        if full_pred == PRED_NORMAL and few_pred == PRED_ANOMALY:
            few_flips.append({
                "node": node,
                "temperature": temperature,
                "humidity": humidity,
                "full_score": full_score,
                "few_score": few_score,
                "few_profile": few_profiles[node],
                "few_rows": len(few_history[node]),
                # 完整历史有多少条。报告里要写「40 条时判什么」，不能把 40 写死 ——
                # 换一份历史重跑（C4 最低要求）条数就变了
                "full_rows": len(full_history[node]),
            })
    result["few"]["checked"] = len(baseline)
    result["few"]["flips"] = few_flips

    # 同一条读数在不同历史条数下分别判成什么。只报「5 条会翻」的话，
    # 读者没法判断这是稳定现象还是刚好撞上；扫一圈才看得出它有多不稳
    for flip in few_flips:
        points = [(flip["temperature"], flip["humidity"])]
        rows = []
        for size in C4_STABILITY_ROWS:
            trimmed = {n: h.head(size) for n, h in full_history.items()}
            pred, score = _judge(_train_on(trimmed), flip["node"], points)[0]
            rows.append({
                "size": min(size, len(full_history[flip["node"]])),
                "ml_anomaly": pred == PRED_ANOMALY,
                "score": score,
            })
        result["few"]["stability"].append({
            "node": flip["node"],
            "temperature": flip["temperature"],
            "humidity": flip["humidity"],
            "rows": rows,
        })

    # 实验二：历史被污染。掺进去的读数按各节点自己的均值加一个偏移量算，
    # 不是三间房统一写死一个数 —— 各房间基线不同，同一个绝对值对 dorm-a
    # 是轻微偏高、对 dorm-b 可能就是离谱，那样测出来的不是同一件事
    polluted_history = {}
    shifts = []
    for node, history in full_history.items():
        before = profile_stats(history)
        extra = pd.DataFrame({
            "temperature": [before["temp_mean"] + C4_POLLUTE_TEMP_DELTA] * C4_POLLUTE_COUNT,
            "humidity": [before["hum_mean"] + C4_POLLUTE_HUM_DELTA] * C4_POLLUTE_COUNT,
        })
        polluted_history[node] = pd.concat([history, extra], ignore_index=True)
        after = profile_stats(polluted_history[node])
        shifts.append({"node": node, "before": before, "after": after})
    result["polluted"]["shifts"] = shifts

    polluted_models = _train_on(polluted_history)
    polluted_flips = []
    for (node, temperature, humidity), (full_pred, full_score) in baseline.items():
        pol_pred, pol_score = _judge(polluted_models, node, [(temperature, humidity)])[0]
        # 这次关心的是反方向：本来抓得出来的异常，污染之后放行了 ——
        # 「数据本身有问题时，模型不会报警，只会把错误当成常态学走」
        if full_pred == PRED_ANOMALY and pol_pred == PRED_NORMAL:
            polluted_flips.append({
                "node": node,
                "temperature": temperature,
                "humidity": humidity,
                "full_score": full_score,
                "polluted_score": pol_score,
            })
    result["polluted"]["checked"] = len(baseline)
    result["polluted"]["flips"] = polluted_flips

    result["available"] = True
    return result


def run_pipeline(history_dir=None, samples_path=None, rules=None, write_dashboard=True) -> dict:
    """C1/C2 全流程。任何一步的数据缺失都收敛成 available=False + 一句原因，
    不抛异常 —— 报告里少一个板块，总好过整份报告出不来。"""
    history_dir = Path(history_dir) if history_dir else C12_DIR
    samples_path = Path(samples_path) if samples_path else history_dir / SAMPLES_NAME

    result = {
        "available": False,
        "skip_reason": "",
        "history_dir": history_dir,
        "samples_path": samples_path,
        "profiles": {},
        "counts": {},
        "missing_nodes": [],
        "rows": [],
        "n_mismatch": 0,
        "n_rule_normal_ml_anomaly": 0,
        "n_rule_abnormal_ml_normal": 0,
        "live_path": history_dir / LIVE_NAME,
        "live_rows": [],
        "n_live_mismatch": 0,
        "n_live_rule_normal_ml_anomaly": 0,
        "c4": {"available": False, "skip_reason": "流水线未跑完", "few": {}, "polluted": {}},
        "verdicts_path": None,
    }

    if not samples_path.is_file():
        result["skip_reason"] = (
            f"找不到待判样本 {samples_path}。先运行 "
            f"python analysis/gen_c12_data.py 生成模拟数据。"
        )
        return result

    try:
        samples = load_new_samples(samples_path)
    except (ValueError, pd.errors.ParserError) as exc:
        result["skip_reason"] = f"待判样本读取失败：{exc}"
        return result

    if rules is None:
        # 独立运行时的兜底：阈值仍然只认 analysis.py 那一份。
        # 这里写普通模块导入而不是 from analysis import ... —— 脚本直接运行时
        # sys.path[0] 就是 analysis/ 目录，import analysis 找到的正是同级那个
        # analysis.py；写成包导入反而找不到（仓库不是包，没有 __init__.py）
        import analysis
        rules = {
            "judge_temperature": analysis.judge_temperature,
            "judge_humidity": analysis.judge_humidity,
            "build_status": analysis.build_status,
        }

    try:
        models, profiles, counts, missing = train_node_models(history_dir)
    except ImportError as exc:
        result["skip_reason"] = str(exc)
        return result

    if not models:
        reasons = "；".join(f"{n}：{r}" for n, r in missing)
        result["skip_reason"] = (
            f"{history_dir} 下没有可用的历史数据（{reasons}）。先运行 "
            f"python analysis/gen_c12_data.py 生成模拟数据。"
        )
        return result

    rows = evaluate(models, samples, rules)
    if not rows:
        result["skip_reason"] = (
            f"待判样本里的宿舍（{'、'.join(sorted(set(samples['node'].astype(str))))}）"
            f"都没有对应的历史数据，无法对照。"
        )
        return result

    # 实时样本：同一批基线、同一套规则，判的是 MQTTX 真发出来的读数。
    # 文件读不出来（刚建的只有表头、被别处占着）就当作「还没有实时数据」，
    # 不能让这一块把整份报告拖垮
    live_rows = []
    try:
        live_rows = evaluate(models, load_new_samples(result["live_path"]), rules)
    except (OSError, ValueError, pd.errors.ParserError, pd.errors.EmptyDataError):
        live_rows = []

    result.update({
        "available": True,
        "profiles": profiles,
        "counts": counts,
        "missing_nodes": missing,
        "rows": rows,
        "n_mismatch": sum(1 for r in rows if r["mismatch"]),
        "n_rule_normal_ml_anomaly": sum(1 for r in rows if r["rule_normal"] and r["ml_anomaly"]),
        "n_rule_abnormal_ml_normal": sum(
            1 for r in rows if (not r["rule_normal"]) and (not r["ml_anomaly"])
        ),
        "live_rows": live_rows,
        "n_live_mismatch": sum(1 for r in live_rows if r["mismatch"]),
        "n_live_rule_normal_ml_anomaly": sum(
            1 for r in live_rows if r["rule_normal"] and r["ml_anomaly"]
        ),
    })

    # C4 实验与 C3 导出都在「已经判定成功」之后才做。实验要重训好几轮模型，
    # 导出要把判定写成文件 —— 两者都属于附加产物，任何一步出问题都不该
    # 把已经算好的 C1/C2 结果一起丢掉，所以各自兜住异常，只提示不中断
    try:
        result["c4"] = run_c4_experiments(history_dir, samples)
    except (ImportError, OSError, ValueError) as exc:
        result["c4"] = {"available": False, "skip_reason": f"C4 实验未能运行：{exc}",
                        "few": {}, "polluted": {}}

    # write_dashboard=False 的用途见 --no-verdicts：拿一份临时历史做实验时，
    # 不该把 Dashboard 上那一列覆盖成实验基线的判定结果
    result["verdicts_path"] = write_verdicts(result) if write_dashboard else None
    return result


# ---------------------------------------------------------------- 解释文案
def _z(value: float, mean: float, std: float) -> float:
    """离平时几个标准差。历史全是一个数（标准差 0）时无法定义，按 0 处理 ——
    那种情况下 _gap_text 会退回直接比大小"""
    return (value - mean) / std if std else 0.0


def _gap_text(value: float, mean: float, std: float, unit: str) -> str:
    """这一条离这间房的平时有多远。用标准差说话，别用「偏大」这种没量纲的词。"""
    if not std:  # 历史全是一个数时没法算倍数，退回直接比大小
        return f"与平时均值 {mean:.1f} {unit} 相差 {abs(value - mean):.1f} {unit}"
    z = _z(value, mean, std)
    if abs(z) < 1:
        return f"落在平时波动范围内（平时约 {mean:.1f} ± {std:.1f} {unit}）"
    direction = "高出" if z > 0 else "低于"
    return f"{direction}平时约 {abs(z):.1f} 个标准差（平时约 {mean:.1f} ± {std:.1f} {unit}）"


def severity(row: dict, profile: dict) -> float:
    """这一条离这间房平时有多远（两维平方和）。同一间房有一堆偏离样本时，
    用它挑出最极端的那条做代表，而不是把十几条几乎一样的句子都写一遍"""
    return (_z(row["temperature"], profile["temp_mean"], profile["temp_std"]) ** 2 +
            _z(row["humidity"], profile["hum_mean"], profile["hum_std"]) ** 2)


def explain_row(row: dict, profile: dict) -> str:
    """为什么固定规则放过了它、ML 却觉得不对 —— 用这条样本自己的数字说清楚。

    一维偏离和两维同时偏离的措辞不一样：两维都偏说「两维各自都没碰到阈值」，
    只有一维偏的时候这么说就变成了废话（另一维本来就在范围内，提它做什么）。
    """
    temp_text = _gap_text(row["temperature"], profile["temp_mean"], profile["temp_std"], "℃")
    hum_text = _gap_text(row["humidity"], profile["hum_mean"], profile["hum_std"], "%")
    deviating = [name for name, value, mean, std in
                 (("温度", row["temperature"], profile["temp_mean"], profile["temp_std"]),
                  ("湿度", row["humidity"], profile["hum_mean"], profile["hum_std"]))
                 if abs(_z(value, mean, std)) >= 1]
    # 先拼好再塞进 f-string：嵌套引号在 Python 3.12 之前是语法错误（PEP 701 才放开），
    # 报告不该依赖某个小版本的解释器才读得出来
    deviating_text = "、".join(deviating) or "这一条"
    return (
        f"{row['node']} 这一条是 {row['temperature']:.1f} ℃ / {row['humidity']:.1f} %："
        f"温度{temp_text}，湿度{hum_text}。"
        f"固定规则看的是 18 / 30 / 75 这几个共用阈值，这一条没碰到任何一个，所以判「正常」；"
        f"但{deviating_text}跟这间房自己的平时差得很远，ML 因此判「{ML_ANOMALY_TEXT}」。"
    )


def _conclusion_text(result) -> str:
    """结论段：对照结果里到底有没有 C2 要找的那种样本，有就逐条解释，没有就照实说。"""
    rows = result["rows"]
    normal_and_anomaly = [r for r in rows if r["rule_normal"] and r["ml_anomaly"]]
    abnormal_and_normal = [r for r in rows if (not r["rule_normal"]) and (not r["ml_anomaly"])]

    lines = []
    if normal_and_anomaly:
        lines.append(
            f"共 {len(rows)} 条待判样本，其中 {len(normal_and_anomaly)} 条是"
            f"「固定规则判正常、ML 判与平时明显不同」—— 这正是 C2 要找的情况，"
            f"逐条说明："
        )
        for r in normal_and_anomaly:
            lines.append("  · " + explain_row(r, result["profiles"][r["node"]]))
    else:
        # 不凑结果：没出现就写没出现，并说明实际看到了什么
        lines.append(
            f"本次测试未出现：{len(rows)} 条待判样本里，固定规则判为「正常」的每一条，"
            f"ML 也都判为接近平时，两者没有分歧。实际观察到的情况是"
            f"（{result['n_mismatch']} 条不一致，"
            f"{len(rows) - result['n_mismatch']} 条一致），"
            f"如需看到分歧，应调整待判样本本身（换更贴近阈值的取值），"
            f"而不是去调模型参数。"
        )

    if abnormal_and_normal:
        lines.append(
            f"另有 {len(abnormal_and_normal)} 条反向不一致（规则判异常、ML 判接近平时）："
            f"它们越过了固定阈值，但阈值附近的读数在这间房的历史里并不少见。"
        )
        for r in abnormal_and_normal:
            lines.append(
                f"  · {r['node']} {r['temperature']:.1f} ℃ / {r['humidity']:.1f} %："
                f"规则判「{r['rule_status']}」，ML 判「{r['ml_text']}」"
                f"（分数 {r['ml_score']:.3f}）。"
            )

    lines.append(
        "两边结论不同不是谁算错了：固定规则比的是所有人共用的阈值，"
        "ML 比的是这间房自己的历史。同一读数在一间房算异常、在另一间房算正常，"
        "正是「每间房各自设基线」的意义。"
    )
    return "\n".join(lines)


def group_live_hits(result) -> list:
    """把实时样本里「规则正常 · ML 异常」的按宿舍归并。

    报文是连着来的，同一类偏离一报就是十几条 —— 逐条写出来，读的人看到的
    是一屏几乎一样的句子，真正有用的信息（哪间房、偏了多少、出现多少次）
    反而被淹没。所以按宿舍归并，每间房只留最极端的一条做代表。
    """
    grouped = {}
    for r in result["live_rows"]:
        if r["rule_normal"] and r["ml_anomaly"]:
            grouped.setdefault(r["node"], []).append(r)

    summary = []
    for node, items in grouped.items():
        profile = result["profiles"][node]
        summary.append({
            "node": node,
            "count": len(items),
            "first": items[0],
            "last": items[-1],
            "worst": max(items, key=lambda r: severity(r, profile)),
            "explain": explain_row(max(items, key=lambda r: severity(r, profile)), profile),
        })
    return summary


def _render_live_block(result, esc, table_html) -> str:
    """实时样本对照：app.py 订阅 MQTT 后记下的实测读数，用同一批基线再判一次。

    只出表。数据从哪来、和上面那批模拟样本什么关系，写进表头一行带过 ——
    这一节的用途是并排看结果，不是讲这两份数据的分工。
    """
    live = result["live_rows"]
    if not live:
        return """
    <p class="muted" style="margin-top:18px"><b>固定规则 vs ML · 实时报文</b>：暂无实时样本。</p>"""

    shown = live[-LIVE_DISPLAY_ROWS:]
    return f"""
    <p class="muted" style="margin-top:18px"><b>固定规则 vs ML · 实时报文</b>
    （MQTT 报文逐条记入 <code>{esc(str(result['live_path']))}</code>，此处列最新
    {len(shown)} 条）</p>
    {table_html(shown)}"""


# ---------------------------------------------------------------- 报告板块
def render_ml_section(result) -> str:
    """C1/C2/C3 的报告板块：只放结果 —— 各宿舍的 ML 基准 + 两张对照表。

    模型怎么训、两边为什么不同、每条差几个标准差，全都不写在这一节里：
    它要回答的是「规则和 ML 各判了什么」，读的人自己比对。逐条解释留在
    format_console 的终端输出和 C4 板块，报告里不放。
    样式全部复用 render_report 里已有的类，不新增 CSS、不画图。
    """
    esc = html.escape

    if not result["available"]:
        return f"""
  <h2>机器学习对比</h2>
  <div class="panel"><p class="muted">本次未生成机器学习对照：{esc(result['skip_reason'])}</p></div>"""

    def table_html(items):
        """对照表。模拟样本和实时样本共用一份渲染逻辑 ——
        两块各写一遍的话，哪天要改列或改标红规则，总会漏掉一处"""
        body = []
        for r in items:
            # 不一致的行把两格标红：读的人一眼就能定位到「规则和 ML 打架」的那几条，
            # 不用逐行比对文字
            cls = ' class="warn"' if r["mismatch"] else ""
            body.append(
                "<tr>"
                f"<td>{esc(r['node'])}</td>"
                f"<td class=\"nowrap\">{esc(r['time_text'])}</td>"
                f"<td>{r['temperature']:.1f}</td>"
                f"<td>{r['humidity']:.1f}</td>"
                f"<td{cls}>{esc(r['rule_status'])}</td>"
                f"<td{cls}>{esc(r['ml_text'])}</td>"
                f"<td>{r['ml_score']:.3f}</td>"
                "</tr>"
            )
        return f"""
    <table>
      <thead><tr>
        <th>宿舍</th><th>时间</th><th>温度 ℃</th><th>湿度 %</th>
        <th>固定规则</th><th>ML 判定</th><th>ML 分数</th>
      </tr></thead>
      <tbody>{"".join(body)}</tbody>
    </table>"""

    # 各宿舍的基准单独成表：ML 判「接近平时」用的就是这一行数字，
    # 下面表里每一格的判定都是拿读数跟它比出来的
    baseline_body = "".join(
        "<tr>"
        f"<td>{esc(node)}</td>"
        f"<td>{p['temp_mean']:.1f} ± {p['temp_std']:.1f}</td>"
        f"<td>{p['hum_mean']:.1f} ± {p['hum_std']:.1f}</td>"
        f"<td>{result['counts'].get(node, '—')}</td>"
        "</tr>"
        for node, p in result["profiles"].items()
    )

    live_block = _render_live_block(result, esc, table_html)

    return f"""
  <h2>机器学习对比</h2>
  <div class="panel">
    <p class="muted"><b>各宿舍 ML 判定「{esc(ML_NORMAL_TEXT)}」的基准</b></p>
    <table>
      <thead><tr>
        <th>宿舍</th><th>温度基准 ℃</th><th>湿度基准 %</th><th>历史条数</th>
      </tr></thead>
      <tbody>{baseline_body}</tbody>
    </table>
    <p class="muted" style="margin-top:18px"><b>固定规则（{esc(RULE_TEXT)}） vs ML · 待判样本</b>
    （<code>{esc(str(result['samples_path']))}</code>）</p>
    {table_html(result['rows'])}
    {live_block}
  </div>"""


def render_c4_section(c4) -> str:
    """C4 的报告板块：两个「判断不太理想」的例子 + 各自一句原因。

    只摆实际跑出来的误判和它的来源。模型为什么不稳、点云为什么稀疏这类分析
    不写在这儿 —— 作业要的是「保留例子并说明可能原因」，一句够用。
    样式沿用 render_report 里已有的类，不新增 CSS。
    """
    esc = html.escape

    if not c4.get("available"):
        return f"""
  <h2>ML 判断不太理想的例子</h2>
  <div class="panel"><p class="muted">本次未运行 C4 实验：{esc(c4.get('skip_reason', '未知原因'))}</p></div>"""

    few_rows = c4["few"]["flips"]
    polluted_flips = c4["polluted"]["flips"]

    # 例一：历史太少。优先挑「探针就是该房历史均值」的那条 —— 连房间自己的
    # 平均值都被判成异常，就没有「这条样本本身就很怪」的余地了
    if few_rows:
        note_by_key = {(p["node"], p["temperature"], p["humidity"]): p["note"]
                       for p in c4["probes"]}
        best = max(few_rows, key=lambda f: (
            note_by_key.get((f["node"], f["temperature"], f["humidity"])) == "该宿舍历史上的均值",
            abs(f["full_score"] - f["few_score"]),
        ))
        # 同一条读数在 3/5/8/12/20/40 条历史下分别判成什么。只写「5 条会翻」
        # 看不出这是稳定现象还是刚好撞上 —— 这一行才是例子的分量所在
        stability = next((s for s in c4["few"]["stability"]
                          if (s["node"], s["temperature"], s["humidity"])
                          == (best["node"], best["temperature"], best["humidity"])), None)
        sweep = ""
        if stability:
            head = "".join(f"<th>{r['size']} 条</th>" for r in stability["rows"])
            # 标红的是「跟完整历史判得不一样」的那一格，也就是这行里唯一的异类。
            # 反过来的话（标红多数派）读起来像是多数派错了
            reference = stability["rows"][-1]["ml_anomaly"]
            body = "".join(
                f"<td{' class=\"warn\"' if r['ml_anomaly'] != reference else ''}>"
                f"{esc(ML_ANOMALY_TEXT if r['ml_anomaly'] else ML_NORMAL_TEXT)}</td>"
                for r in stability["rows"]
            )
            sweep = f"""
    <table>
      <thead><tr><th>历史条数</th>{head}</tr></thead>
      <tbody><tr><td>同一条读数判成</td>{body}</tr></tbody>
    </table>"""

        few_block = f"""
    <p class="muted"><b>① 历史太少</b>：{esc(best['node'])}
    {best['temperature']:.1f} ℃ / {best['humidity']:.1f} %
    （{esc(note_by_key.get((best['node'], best['temperature'], best['humidity']), '探针样本'))}）
    —— 历史 {best['full_rows']} 条时判「{esc(ML_NORMAL_TEXT)}」，分数
    {best['full_score']:+.4f}；砍到 {best['few_rows']} 条后判「{esc(ML_ANOMALY_TEXT)}」，
    分数 {best['few_score']:+.4f}。</p>{sweep}
    <p class="muted">原因：历史太少，模型没见全这间房「平时」的范围，正常的读数也会
    落在它没见过的区域 —— 判定随样本量跳变，不是模型坏了。</p>"""
    else:
        few_block = f"""
    <p class="muted"><b>① 历史太少</b>：把每间房的历史砍到前 {c4['few_rows']} 条重训再判
    （共 {c4['few']['checked']} 条探针）。<b>本次实验未出现</b>「历史多时判正常、
    历史少了就判异常」的翻转 —— 如实记录，不为凑例子去调模型参数或挑数据。</p>"""

    # 例二：历史本身有问题。挑变化最悬殊的那条
    if polluted_flips:
        best_p = max(polluted_flips, key=lambda f: abs(f["full_score"] - f["polluted_score"]))
        shift = next((s for s in c4["polluted"]["shifts"] if s["node"] == best_p["node"]), None)
        shift_text = ""
        if shift:
            shift_text = (
                f"；这间房的基准被这 {c4['pollute_count']} 条读数从 "
                f"{shift['before']['temp_mean']:.1f} ± {shift['before']['temp_std']:.1f} ℃、"
                f"{shift['before']['hum_mean']:.1f} ± {shift['before']['hum_std']:.1f} % 抬到 "
                f"{shift['after']['temp_mean']:.1f} ± {shift['after']['temp_std']:.1f} ℃、"
                f"{shift['after']['hum_mean']:.1f} ± {shift['after']['hum_std']:.1f} %"
            )
        polluted_block = f"""
    <p class="muted" style="margin-top:18px"><b>② 历史本身有问题</b>：{esc(best_p['node'])}
    {best_p['temperature']:.1f} ℃ / {best_p['humidity']:.1f} % —— 干净历史判「{esc(ML_ANOMALY_TEXT)}」，
    分数 {best_p['full_score']:+.4f}；历史里掺入 {c4['pollute_count']} 条偏高读数后改判
    「{esc(ML_NORMAL_TEXT)}」，分数 {best_p['polluted_score']:+.4f}{esc(shift_text)}。</p>
    <p class="muted">原因：模型对「平时」的定义完全来自喂进去的历史，它不会质疑数据对不对 ——
    错误数据混进去只会被当成常态学走，真正的异常反而放行。</p>"""
    else:
        polluted_block = f"""
    <p class="muted" style="margin-top:18px"><b>② 历史本身有问题</b>：往每间房历史尾部掺
    {c4['pollute_count']} 条偏高读数后重训再判。<b>本次实验未出现</b>
    「本来抓得出来的异常被放行」的变化 —— 如实记录。</p>"""

    return f"""
  <h2>ML 判断不太理想的例子</h2>
  <div class="panel">{few_block}{polluted_block}
  </div>"""


# ---------------------------------------------------------------- 控制台
def _display_width(text: str) -> int:
    """中文字符占两格，按它算宽度终端表格才不会歪。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    space = " " * max(0, width - _display_width(text))
    return space + text if align == "right" else text + space


def _console_table(items, headers, row_cells):
    """终端表格。表头、分隔线、每一行的列宽都由内容算出来 ——
    中文字符按两格宽算（见 _display_width），不然列会歪成斜的"""
    cells = [row_cells(r) for r in items]
    widths = [
        max(_display_width(h), *(_display_width(c[i]) for c in cells))
        for i, h in enumerate(headers)
    ]
    lines = ["  ".join(_pad(h, w) for h, w in zip(headers, widths)).rstrip(),
             "  ".join("-" * w for w in widths)]
    for c in cells:
        lines.append("  ".join(_pad(v, w) for v, w in zip(c, widths)).rstrip())
    return lines


def format_console(result) -> str:
    """终端对照表。跟报告同一份结果，改一处不会两边不一致。"""
    if not result["available"]:
        return f"【机器学习对比】未运行：{result['skip_reason']}"

    headers = ("宿舍", "时间", "温度℃", "湿度%", "固定规则", "ML 判定", "ML 分数", "")

    def cells(r):
        return (r["node"], r["time_text"][5:16], f"{r['temperature']:.1f}", f"{r['humidity']:.1f}",
                r["rule_status"], r["ml_text"], f"{r['ml_score']:.3f}",
                "不一致" if r["mismatch"] else "")

    lines = ["【机器学习对比】规则 vs ML", "",
             "— 模拟对照样本（" + str(result["samples_path"]) + "）—"]
    lines.extend(_console_table(result["rows"], headers, cells))

    live = result["live_rows"]
    lines.append("")
    if live:
        shown = live[-LIVE_DISPLAY_ROWS:]
        lines.append(f"— 实时样本（{LIVE_SOURCE_REAL}，{result['live_path']}，"
                     f"共 {len(live)} 条，列最新 {len(shown)} 条）—")
        lines.extend(_console_table(shown, headers, cells))
    else:
        lines.append(f"— 实时样本：暂无。启动 app.py 订阅 MQTT 后，MQTTX 发的报文会记入 "
                     f"{result['live_path']} —")

    lines.append("")
    lines.append(
        f"历史：{result['history_dir']}（"
        + "、".join(f"{n} {c} 条" for n, c in result["counts"].items())
        + "）；待判：%s（%d 条）" % (result["samples_path"], len(result["rows"]))
    )
    if result["missing_nodes"]:
        lines.append(
            "未参与：" + "；".join(f"{n}：{reason}" for n, reason in result["missing_nodes"])
        )
    lines.append(f"不一致 {result['n_mismatch']} 条，其中「规则正常 · ML 异常」"
                 f"{result['n_rule_normal_ml_anomaly']} 条")
    lines.append("")
    lines.append(_conclusion_text(result))

    if live:
        lines.append("")
        lines.append(f"实时样本不一致 {result['n_live_mismatch']} 条，其中「规则正常 · ML 异常」"
                     f"{result['n_live_rule_normal_ml_anomaly']} 条")
        hits = group_live_hits(result)
        if hits:
            # 与报告同一份归并结果：同一类偏离按宿舍写成一行，
            # 否则这里会刷出十几条几乎一样的句子，等于没有摘要
            for hit in hits:
                lines.append(f"  · {hit['node']}：这一类出现 {hit['count']} 次"
                             f"（{hit['first']['time_text'][5:16]} ~ {hit['last']['time_text'][5:16]}），"
                             f"最极端的一条——")
                lines.append("    " + hit["explain"])
        else:
            lines.append("实时样本里没有出现「规则正常 · ML 异常」，如实记录。")

    lines.append("")
    lines.append(format_c4_console(result.get("c4") or {}))
    if result.get("verdicts_path"):
        lines.append(f"Dashboard 判定文件：{result['verdicts_path']}")
    return "\n".join(lines)


def format_c4_console(c4) -> str:
    """C4 的终端摘要。跟报告同一份数字，不另算一遍。"""
    if not c4.get("available"):
        return f"【C4 判断不太理想的例子】未运行：{c4.get('skip_reason', '未知原因')}"

    note_by_key = {(p["node"], p["temperature"], p["humidity"]): p["note"]
                   for p in c4["probes"]}
    lines = ["【C4 判断不太理想的例子】ML 不是用了就一定更准", ""]

    few = c4["few"]
    if few["flips"]:
        best = max(few["flips"], key=lambda f: (
            note_by_key.get((f["node"], f["temperature"], f["humidity"])) == "该宿舍历史上的均值",
            abs(f["full_score"] - f["few_score"]),
        ))
        lines.append(f"— 实验一：历史砍到前 {c4['few_rows']} 条 —")
        lines.append(f"{few['checked']} 条探针里 {len(few['flips'])} 条由「{ML_NORMAL_TEXT}」"
                     f"翻成「{ML_ANOMALY_TEXT}」。最典型的一条：")
        lines.append(f"  {best['node']} {best['temperature']:.1f} ℃ / {best['humidity']:.1f} %"
                     f"（{note_by_key.get((best['node'], best['temperature'], best['humidity']), '探针')}）"
                     f"：40 条时 {best['full_score']:+.4f}，"
                     f"{best['few_rows']} 条时 {best['few_score']:+.4f}")
        stability = next((s for s in few["stability"]
                          if (s["node"], s["temperature"], s["humidity"])
                          == (best["node"], best["temperature"], best["humidity"])), None)
        if stability:
            trail = "、".join(
                f"{r['size']} 条 {'异常' if r['ml_anomaly'] else '正常'}" for r in stability["rows"]
            )
            lines.append(f"  同一条读数换个历史条数：{trail}")
    else:
        lines.append(f"— 实验一：历史砍到前 {c4['few_rows']} 条，{few['checked']} 条探针"
                     f"未出现「正常 → 异常」的翻转，如实记录 —")

    lines.append("")
    polluted = c4["polluted"]
    if polluted["flips"]:
        best = max(polluted["flips"], key=lambda f: abs(f["full_score"] - f["polluted_score"]))
        lines.append(f"— 实验二：历史掺入 {c4['pollute_count']} 条偏高读数 "
                     f"(均值 +{c4['pollute_temp_delta']:.1f} ℃ / +{c4['pollute_hum_delta']:.1f} %) —")
        lines.append(f"{polluted['checked']} 条探针里 {len(polluted['flips'])} 条由"
                     f"「{ML_ANOMALY_TEXT}」变成「{ML_NORMAL_TEXT}」。例如：")
        lines.append(f"  {best['node']} {best['temperature']:.1f} ℃ / {best['humidity']:.1f} %"
                     f"：干净历史 {best['full_score']:+.4f}，掺入后 {best['polluted_score']:+.4f}")
    else:
        lines.append(f"— 实验二：历史掺入 {c4['pollute_count']} 条偏高读数，"
                     f"{polluted['checked']} 条探针未出现判定变化，如实记录 —")

    lines.append("")
    lines.append("原因：判异常靠的是「这条读数在历史点云里孤不孤立」。历史少了，"
                 "点云稀疏、边界由少数几个点围出来，普通读数也容易显得孤立；"
                 "历史脏了，模型不会质疑数据，只会把错误当成常态学走。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程
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
        description="C1/C2：每节点 IsolationForest 基线与固定规则对照。"
    )
    parser.add_argument("--history-dir", default=str(C12_DIR),
                        help=f"历史数据目录，换一份历史数据就传这个参数（默认 {C12_DIR}）")
    parser.add_argument("--samples", help="待判样本 CSV，默认取 --history-dir 下的 "
                                          f"{SAMPLES_NAME}")
    parser.add_argument("--no-verdicts", action="store_true",
                        help="只打印结果，不写 Dashboard 读的 "
                             f"{VERDICTS_DIR / VERDICTS_NAME}")
    args = parser.parse_args(argv)

    result = run_pipeline(history_dir=args.history_dir, samples_path=args.samples,
                          write_dashboard=not args.no_verdicts)
    print(format_console(result))
    return 0 if result["available"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
