"""DormMate 后端服务：历史记录读写 + 媒体文件上传与回放。

存储规则（三条，改动前先想清楚）：
  1. 照片/视频/录音的实体统一放 data/media，CSV 里只存文件名索引
  2. 网页和小程序的「清空」只清前端展示 —— 所以这里没有任何删除接口，
     文件一旦落盘就不会被 UI 抹掉
  3. CSV 保持 UTF-8 BOM + CRLF：Excel 双击打开、网页导出、analysis.py 都依赖这个约定，
     读用 utf-8-sig，写用 newline=''，别改成 utf-8 + 默认换行

接口：
  GET  /api/getHistory   读全部历史（网页、小程序共用）
  GET  /api/serverInfo   服务启动时刻，页面据此只展示本次运行期间的新记录
  POST /api/addRecord    新增一条温湿度记录，状态由服务端计算
  POST /api/uploadMedia  上传照片/视频/录音，落盘 data/media 并在 CSV 里记一行
  GET  /media/<filename> 回放/回看媒体文件
  POST /api/eventLog     A1~A4 事件闭环的一行事件（dashboard 实时上报）
  GET  /api/eventLog     读全部事件，给 analysis.py 的事件复盘和页面排查用
  POST /api/nodeStatus   B4 看板快照：三宿舍最新读数（dashboard 每条报文上报一次）
  GET  /api/nodeStatus   B4 看板快照：小程序「宿舍实时状态」卡读它

另有一条后台链路：app.py 自己订阅 MQTT（dormmate/+/env），见 start_mqtt()。
报文不再必须先经过浏览器里的 Dashboard 才能进到服务端 —— 原来只有那一条路，
页面一关或掉线，小程序的状态卡就永远停在「未上报」。事件日志（A1~A4 的
events.csv）仍然只由 Dashboard 产生，app.py 不碰，免得同一次异常被记两遍。
"""
import csv
import json
import os
import re
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 路径锚定到本文件所在目录，而不是当前工作目录。原来写的是 "./data/dormmate.csv"，
# 是相对 CWD 的：换个目录启动（比如 IDE 的默认工作目录）就会在错误的位置新建一份空 CSV，
# 表现成「历史记录突然全没了」。三个环境变量是给测试用的，正常启动不需要设置。
CSV_PATH = os.environ.get("DORMATE_CSV", os.path.join(BASE_DIR, "data", "dormmate.csv"))
MEDIA_DIR = os.environ.get("DORMATE_MEDIA", os.path.join(BASE_DIR, "data", "media"))
EVENT_CSV = os.environ.get("DORMATE_EVENTS", os.path.join(BASE_DIR, "data", "events.csv"))
PORT = int(os.environ.get("DORMATE_PORT", "5000"))
DEBUG = os.environ.get("DORMATE_DEBUG", "1") == "1"

CSV_HEADER = ["time", "temperature", "humidity", "status",
              "photo_name", "video_name", "audio_name"]
MEDIA_COLUMN = {"photo": "photo_name", "video": "video_name", "audio": "audio_name"}

# A4 事件日志：和温湿度 CSV 分开存。混进 dormmate.csv 会撑爆 history 接口的列结构，
# 而且两者的写入频率差一个量级（事件一次处置写十几行，读数一分钟才一行）。
# 六列：前四列是结构化的判定依据，后两列（alert / op_text）是给人直接看的
# 「异常类型」「用户操作」—— 复盘时打开 events.csv 就能读懂一行，
# 不用再拿 detail 里那串 JSON 去脑补。机器可读的那份仍是 detail（JSON 字符串），
# analysis.py 的复盘直接 json.loads 还原，不靠正则去猜文案
EVENT_HEADER = ["time", "node", "event_type", "alert", "op_text", "detail"]
# 这两个是给人看的短文本，长度封顶，免得一条脏数据把 CSV 撑成没法看的一行
EVENT_TEXT_MAX = 100
EVENT_TYPES = {"anomaly_start", "priority", "action", "reading", "verdict"}
EVENT_NODES = {"dorm-a", "dorm-b", "dorm-c"}

# B4 看板快照：dashboard 每收到一条报文，就把三个宿舍的最新读数报一份到这里，
# 小程序的「宿舍实时状态」卡 GET 它。纯内存、进程重启即清空 ——
# 这是「此刻三间房怎么样」的瞬时快照，不是历史；历史照旧走 CSV。
# Flask 开发服务器是多线程的，读改写要加锁，不然两个请求同时到会丢更新
NODE_STATUS = {}
NODE_STATUS_LOCK = threading.Lock()

# MQTT：app.py 自己订阅 broker，作为 Dashboard 之外的第二条数据入口。
# 默认端口 1884 是 mosquitto.conf 里的 tcp listener（另一个 9001 是 websockets，
# 给浏览器用的）。同一 broker 的各个 listener 互通，MQTTX 发到 1884 或 9001，
# 这里都收得到。换台机器部署就用环境变量覆盖。
MQTT_HOST = os.environ.get("DORMATE_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("DORMATE_MQTT_PORT", "1884"))
MQTT_TOPIC = "dormmate/+/env"
MQTT_RETRY_SECONDS = 5
MQTT_TOPIC_PATTERN = re.compile(r"^dormmate/([^/]+)/env$")

# C1/C2 的实时样本：MQTT 每来一条报文就追加一行，供 analysis/ml_anomaly.py
# 拿最新的真实读数跟「这间房平时什么样」的基线对照。
# 和 data/c12 下的模拟历史严格分开：那份是训练用的模拟数据，这份是实测。
LIVE_SAMPLES_CSV = os.environ.get("DORMATE_LIVE_SAMPLES",
                                  os.path.join(BASE_DIR, "data", "c12", "live_samples.csv"))
LIVE_HEADER = ["node", "time", "temperature", "humidity", "source"]
LIVE_SOURCE_REAL = "实时"
# 上限行数：演示时动辄连发上百条报文，文件不能无限长。超了就重写保留最后这些行。
# 报告只展示最新 12 条，120 行留了足够余量做「最近一段时间的走势」
LIVE_MAX_ROWS = 120
# 同一节点的相同读数在这个秒数内只记一次。挡住两种重复：
#   1. Dashboard 上报和 MQTT 订阅是两条路，同一条报文会各走一遍
#   2. MQTTX 手动连发同一个 payload
LIVE_DEDUP_SECONDS = 5
_last_live = {}
LIVE_LOCK = threading.Lock()

# 扩展名白名单。上传口是唯一的外部输入点，这里只做白名单、不做「排除 .exe」那种反向过滤 ——
# 反向过滤永远漏，白名单漏不了
ALLOWED_EXT = {
    "photo": {"jpg", "jpeg", "png", "webp"},
    "video": {"mp4", "webm"},
    "audio": {"mp3", "aac", "webm", "m4a"},
}

# 与前端校验、analysis.py 的有效量程一致：温度 -20~60 ℃，湿度 0~100 %
TEMP_RANGE = (-20.0, 60.0)
HUMI_RANGE = (0.0, 100.0)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# 本次进程的启动时刻。页面只展示「这次启动之后」新增的记录 ——
# CSV 里早先留下的行（项目早期数据、校验数据集）不再挤进实时视图。
# 它们仍然完整留在文件里，一个字都没动，只是不上屏。
# 时间格式和 CSV 的 time 列一致（%Y-%m-%d %H:%M:%S），所以前端可以直接按字符串比大小。
SERVER_STARTED_AT = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
CORS(app)  # 网页常从 Live Server 或 file:// 打开，跨源访问需要放行

# 中文不转义成 \uXXXX，方便直接在浏览器 Network 面板里读报文
if hasattr(app, "json"):          # Flask 2.2+
    app.json.ensure_ascii = False
else:                             # 更老的版本
    app.config["JSON_AS_ASCII"] = False


# ---------------- 判定与格式化 ----------------

def compute_status(temp, humi):
    """与网页 judgeTemperature/judgeHumidity、小程序 computeStatus、dashboard 同一套阈值：
    t<18 偏冷 / t>=30 偏热 / h>=75 偏湿。
    偏冷偏热和偏湿可以同时成立，用 + 拼接（偏冷+偏湿）—— 四处输出格式必须一致，
    否则同一条数据在 CSV 里和页面上会显示成两个样子。"""
    parts = []
    if temp < 18:
        parts.append("偏冷")
    elif temp >= 30:
        parts.append("偏热")
    if humi >= 75:
        parts.append("偏湿")
    return "+".join(parts) if parts else "正常"


def fmt_num(value):
    """25.0 写成 25，16.5 保持 16.5 —— 和现有 CSV 里的数字风格一致，
    免得整个文件变成清一色的 x.0"""
    return str(int(value)) if float(value).is_integer() else str(value)


def parse_reading(raw, label, low, high):
    """返回 (值, 错误信息)，二选一为 None。
    前端也校验一遍，但后端不能因此就信任输入 —— 这是个能被直接 curl 的公开接口。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, "%s不是数字：%r" % (label, raw)
    # NaN 与任何值比较都是 False，所以这里会走进错误分支，不会被当成合法读数放过去
    if not (low <= value <= high):
        return None, "%s超出有效范围（%s ~ %s）：%s" % (label, low, high, value)
    return value, None


# ---------------- CSV 读写 ----------------

def read_rows():
    """读全部记录。文件不存在返回空列表，不在这里新建 —— 读接口不该有副作用。"""
    if not os.path.exists(CSV_PATH):
        return []
    # utf-8-sig 同时兼容带 BOM（网页导出、Excel 另存都带）和不带的文件；
    # 用 utf-8 读带 BOM 的文件会把第一列名变成 "﻿time"，前端取 item.time 就是 undefined
    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def append_row(row):
    """追加一行（7 列，缺失的媒体列补空串）。
    新建文件时才写 BOM：utf-8-sig 的编码器每次 open 都往流开头塞 BOM，
    追加模式下那三个字节会被写进文件中间，整个文件就毁了 —— 所以追加必须用 utf-8。"""
    directory = os.path.dirname(CSV_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    is_new = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "w" if is_new else "a",
              encoding="utf-8-sig" if is_new else "utf-8", newline="") as f:
        writer = csv.writer(f)  # 默认 lineterminator='\r\n'，正好保持 CRLF 约定
        if is_new:
            writer.writerow(CSV_HEADER)
        writer.writerow([row.get(key, "") for key in CSV_HEADER])


def read_events():
    """读全部事件行。detail 还原成对象；解析不出来的坏行不丢、不报错，
    原样挂在 detail_raw 上 —— 复盘报告宁可显示一行看不懂的原始文本，
    也不能让整份报告因为一行脏数据打不开。"""
    if not os.path.exists(EVENT_CSV):
        return []
    with open(EVENT_CSV, "r", encoding="utf-8-sig", newline="") as f:
        rows = []
        for raw in csv.DictReader(f):
            row = {key: raw.get(key, "") for key in EVENT_HEADER}
            try:
                detail = json.loads(row["detail"])
            except (TypeError, ValueError):
                detail = None
            if isinstance(detail, dict):
                row["detail"] = detail
            else:
                # 合法 JSON 但不是对象（[1,2]、"abc"、null）也走这条路，前端一律按原始文本处理
                row["detail"] = None
                row["detail_raw"] = raw.get("detail", "")
            rows.append(row)
        return rows


def append_event_row(row):
    """追加一行事件（6 列）。BOM / CRLF 的处理和 append_row 同规矩：
    新建才写 BOM，追加用 utf-8。两处都写一遍而不是抽公共函数，
    是因为列数不同、改一处牵连两个文件的格式约定，收益不如各自独立清楚。"""
    directory = os.path.dirname(EVENT_CSV)
    if directory:
        os.makedirs(directory, exist_ok=True)

    is_new = not os.path.exists(EVENT_CSV)
    with open(EVENT_CSV, "w" if is_new else "a",
              encoding="utf-8-sig" if is_new else "utf-8", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(EVENT_HEADER)
        writer.writerow([row.get(key, "") for key in EVENT_HEADER])


# ---------------- 实时读数：快照 + 实时样本 ----------------

def record_live_sample(node, temp_text, humi_text):
    """把一条实测读数追加到 live_samples.csv。同一个节点的相同读数
    在 LIVE_DEDUP_SECONDS 秒内只记一次（见常量处的说明）。"""
    now = time.time()
    signature = (temp_text, humi_text)
    with LIVE_LOCK:
        last = _last_live.get(node)
        if last and last[0] == signature and now - last[1] < LIVE_DEDUP_SECONDS:
            return False
        _last_live[node] = (signature, now)

    directory = os.path.dirname(LIVE_SAMPLES_CSV)
    if directory:
        os.makedirs(directory, exist_ok=True)

    # 和 append_row 同一套 BOM/CRLF 规矩：新建才写 BOM，追加用 utf-8
    is_new = not os.path.exists(LIVE_SAMPLES_CSV)
    with open(LIVE_SAMPLES_CSV, "w" if is_new else "a",
              encoding="utf-8-sig" if is_new else "utf-8", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(LIVE_HEADER)
        writer.writerow([node, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         temp_text, humi_text, LIVE_SOURCE_REAL])

    trim_live_samples()
    return True


def trim_live_samples():
    """超过 LIVE_MAX_ROWS 就把文件重写成「表头 + 最后 N 行」。
    调用点已经加了锁（record_live_sample），这里不再重复加。"""
    try:
        with open(LIVE_SAMPLES_CSV, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
    except OSError:
        return
    if len(rows) - 1 <= LIVE_MAX_ROWS:
        return
    keep = rows[1:][-LIVE_MAX_ROWS:]
    with open(LIVE_SAMPLES_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(LIVE_HEADER)
        writer.writerows(keep)


def apply_node_reading(node, temp, humi, source):
    """一条读数进来之后要做的两件事：更新内存快照、记一份实时样本。
    POST 接口和 MQTT 订阅都走这里，两条路的落库方式不会各写各的。
    返回算出来的状态文本。"""
    temp_text, humi_text = fmt_num(temp), fmt_num(humi)
    status = compute_status(temp, humi)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with NODE_STATUS_LOCK:
        NODE_STATUS[node] = {"temperature": temp_text, "humidity": humi_text, "status": status}
        NODE_STATUS["_updated_at"] = now
    if record_live_sample(node, temp_text, humi_text):
        print("[报文] %s %s ℃ / %s %% → %s（来源：%s）"
              % (node, temp_text, humi_text, status, source), flush=True)
    return status


# ---------------- MQTT 订阅 ----------------

def start_mqtt():
    """后台线程订阅 broker。收不到 broker 就每 5 秒重试一次，
    永远不抛出去 —— MQTT 断了只是「实时那部分不更新」，
    HTTP 接口（历史记录、上传、小程序其它卡片）全都该照常工作。"""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("[MQTT] 未安装 paho-mqtt，已跳过订阅（实时状态卡将只认 Dashboard 上报）。"
              "安装：pip install paho-mqtt", flush=True)
        return

    # paho 2.x 要求显式声明回调版本；1.x 没有这个参数，退化成旧写法
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="dormmate-backend")
    except AttributeError:
        client = mqtt.Client(client_id="dormmate-backend")

    state = {"logged": False}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        # 回调版本不同，reason_code 的位置也不同：v2 给的是 ReasonCode 对象，
        # v1 直接给 int。取到 0 才算连上
        code = getattr(reason_code, "value", reason_code)
        if code:
            print("[MQTT] 连接被拒：%s" % reason_code, flush=True)
            return
        client.subscribe(MQTT_TOPIC, qos=0)
        print("[MQTT] 已连接 %s:%d，订阅 %s" % (MQTT_HOST, MQTT_PORT, MQTT_TOPIC), flush=True)

    def on_disconnect(client, userdata, *args):
        print("[MQTT] 与 broker 断开，paho 会自动重连", flush=True)

    def on_message(client, userdata, message):
        try:
            match = MQTT_TOPIC_PATTERN.match(message.topic)
            node = (match and match.group(1)) or None
            if node not in EVENT_NODES:
                return
            msg = json.loads(message.payload.decode("utf-8"))
            temp, err = parse_reading(msg.get("temperature"), "温度", *TEMP_RANGE)
            if err:
                return
            humi, err = parse_reading(msg.get("humidity"), "湿度", *HUMI_RANGE)
            if err:
                return
            apply_node_reading(node, temp, humi, "MQTT")
        except Exception as exc:                     # noqa: BLE001
            # 一条坏报文不该让订阅线程死掉 —— 线程一死，后面所有报文都收不到了
            print("[MQTT] 报文处理失败（已跳过）：%s" % exc, flush=True)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    def worker():
        while True:
            try:
                client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                client.loop_forever(retry_first_connection=False)
            except Exception as exc:                 # noqa: BLE001
                print("[MQTT] 连不上 %s:%d（%s），%d 秒后重试"
                      % (MQTT_HOST, MQTT_PORT, exc, MQTT_RETRY_SECONDS), flush=True)
            time.sleep(MQTT_RETRY_SECONDS)

    threading.Thread(target=worker, name="dormmate-mqtt", daemon=True).start()


# ---------------- 接口 ----------------

@app.route("/api/getHistory", methods=["GET"])
def get_history():
    """读全部历史。注意这里不按启动时间过滤 —— 过滤是展示层的策略，
    接口本身保持「如实返回 CSV 全部内容」，analysis.py 之类的直接读文件也一样。
    页面自己调 /api/serverInfo 拿启动时刻再筛。"""
    return jsonify(read_rows())


@app.route("/api/serverInfo", methods=["GET"])
def server_info():
    return jsonify({
        "started_at": SERVER_STARTED_AT,
        "csv_path": CSV_PATH,
        "media_dir": MEDIA_DIR,
    })


@app.route("/api/nodeStatus", methods=["POST"])
def set_node_status():
    """B4 看板快照写入。dashboard 每条报文上报一次，body 形如
    {"nodes": {"dorm-b": {"temperature": 26.8, "humidity": 56}}}。

    只收它真实收到过报文的宿舍 —— 没收到的不报，比报一个占位符诚实：
    小程序那边「未上报」和「26.8℃」是两件事。
    status 由服务端重算，不信任客户端传的，和 /api/addRecord 一个规矩。
    """
    data = request.get_json(silent=True) or {}
    nodes = data.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return jsonify({"code": 1, "msg": "nodes 必须是非空的宿舍快照对象"}), 400

    snapshot = {}
    for node, item in nodes.items():
        if node not in EVENT_NODES:
            return jsonify({"code": 1, "msg": "node 只能是 dorm-a / dorm-b / dorm-c：%s" % node}), 400
        if not isinstance(item, dict):
            return jsonify({"code": 1, "msg": "%s 的读数必须是对象" % node}), 400
        temp, err = parse_reading(item.get("temperature"), "温度", *TEMP_RANGE)
        if err:
            return jsonify({"code": 1, "msg": "%s：%s" % (node, err)}), 400
        humi, err = parse_reading(item.get("humidity"), "湿度", *HUMI_RANGE)
        if err:
            return jsonify({"code": 1, "msg": "%s：%s" % (node, err)}), 400
        snapshot[node] = (temp, humi)

    now = None
    for node, (temp, humi) in snapshot.items():
        # 和 MQTT 订阅走同一个出口：快照照旧更新，顺带记一份实时样本。
        # 两条路的相同读数会被 record_live_sample 去重，不会记成两行
        apply_node_reading(node, temp, humi, "Dashboard")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return jsonify({"code": 0, "msg": "已更新", "time": now})


@app.route("/api/nodeStatus", methods=["GET"])
def get_node_status():
    """B4 看板快照读取（小程序）。从没上报过时返回空 nodes —— 200 而不是 404：
    小程序把非 200 当「服务异常」弹提示，而「看板还没报过」是正常状态不是故障。"""
    with NODE_STATUS_LOCK:
        nodes = {k: v for k, v in NODE_STATUS.items() if k in EVENT_NODES}
        updated_at = NODE_STATUS.get("_updated_at")
    return jsonify({"code": 0, "nodes": nodes, "updated_at": updated_at})


@app.route("/api/addRecord", methods=["POST"])
def add_record():
    """新增一条温湿度记录。状态由服务端算，不接受客户端传 ——
    原来这个接口收 statusText 还写死过「正常」，偏冷的数据也被记成正常。"""
    data = request.get_json(silent=True) or {}

    temp, err = parse_reading(data.get("temperature"), "温度", *TEMP_RANGE)
    if err:
        return jsonify({"code": 1, "msg": err}), 400

    humi, err = parse_reading(data.get("humidity"), "湿度", *HUMI_RANGE)
    if err:
        return jsonify({"code": 1, "msg": err}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = compute_status(temp, humi)
    append_row({
        "time": now,
        "temperature": fmt_num(temp),
        "humidity": fmt_num(humi),
        "status": status,
    })
    return jsonify({"code": 0, "msg": "保存成功", "time": now, "status": status})


@app.route("/api/uploadMedia", methods=["POST"])
def upload_media():
    """接收照片/视频/录音：落盘 data/media，并在 CSV 里记一行（该媒体列填文件名）。
    温湿度取上传时页面当前的分析值，所以行里能看出「这个文件是在什么环境下拍的」。"""
    media_type = (request.form.get("media_type") or "").strip().lower()
    if media_type not in MEDIA_COLUMN:
        return jsonify({"code": 1, "msg": "media_type 必须是 photo / video / audio 之一"}), 400

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"code": 1, "msg": "没有收到文件（表单字段名要叫 file）"}), 400

    ext = os.path.splitext(upload.filename)[1].lower().lstrip(".")
    if ext not in ALLOWED_EXT[media_type]:
        allowed = "、".join(sorted(ALLOWED_EXT[media_type]))
        return jsonify({"code": 1, "msg": "%s 不支持 .%s 格式，允许：%s" % (media_type, ext, allowed)}), 400

    temp, err = parse_reading(request.form.get("temperature"), "温度", *TEMP_RANGE)
    if err:
        return jsonify({"code": 1, "msg": err}), 400

    humi, err = parse_reading(request.form.get("humidity"), "湿度", *HUMI_RANGE)
    if err:
        return jsonify({"code": 1, "msg": err}), 400

    # 时间戳 + 类型 + 4 字节随机：同一秒连拍也不会互相覆盖
    now = datetime.now()
    filename = "%s_%s_%s.%s" % (now.strftime("%Y%m%d_%H%M%S"), media_type,
                                os.urandom(4).hex(), ext)
    os.makedirs(MEDIA_DIR, exist_ok=True)
    upload.save(os.path.join(MEDIA_DIR, filename))

    time_text = now.strftime("%Y-%m-%d %H:%M:%S")
    status = compute_status(temp, humi)
    row = {
        "time": time_text,
        "temperature": fmt_num(temp),
        "humidity": fmt_num(humi),
        "status": status,
        MEDIA_COLUMN[media_type]: filename,
    }
    append_row(row)

    return jsonify({"code": 0, "msg": "上传成功", "time": time_text, "status": status,
                    "filename": filename, "media_type": media_type})


@app.route("/api/eventLog", methods=["POST"])
def add_event():
    """A1~A4 事件闭环的一行。时间由服务端写，不信任客户端传的时刻 ——
    events.csv 是复盘报告的时间轴，客户端时钟不准会让整个事件顺序乱掉。
    detail 强制要求是「能解析成对象的 JSON 字符串」：它是复盘报告唯一的数据来源，
    放行裸字符串的话 analysis.py 就得回去猜文案，机器可读的前提就没了。
    alert / op_text 是可选的两列人读文本（异常类型、用户操作），
    缺省按空串存；给了就必须是字符串 —— 数字或对象混进这两列，
    复盘报告渲染出来的就是 [object Object] 这种东西。"""
    data = request.get_json(silent=True) or {}

    node = data.get("node")
    if node not in EVENT_NODES:
        return jsonify({"code": 1, "msg": "node 必须是 dorm-a / dorm-b / dorm-c 之一"}), 400

    event_type = data.get("event_type")
    if event_type not in EVENT_TYPES:
        allowed = "、".join(sorted(EVENT_TYPES))
        return jsonify({"code": 1, "msg": "event_type 必须是 %s 之一" % allowed}), 400

    detail = data.get("detail")
    if not isinstance(detail, str):
        return jsonify({"code": 1, "msg": "detail 必须是 JSON 字符串"}), 400
    try:
        parsed = json.loads(detail)
    except ValueError:
        return jsonify({"code": 1, "msg": "detail 不是合法 JSON：%s" % detail[:80]}), 400
    if not isinstance(parsed, dict):
        return jsonify({"code": 1, "msg": "detail 解析出来必须是对象，收到的是 %s" % type(parsed).__name__}), 400

    texts = {}
    for key in ("alert", "op_text"):
        value = data.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            return jsonify({"code": 1, "msg": "%s 必须是字符串" % key}), 400
        texts[key] = value.strip()[:EVENT_TEXT_MAX]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    append_event_row({"time": now, "node": node, "event_type": event_type,
                      "alert": texts["alert"], "op_text": texts["op_text"], "detail": detail})
    return jsonify({"code": 0, "msg": "已记录", "time": now})


@app.route("/api/eventLog", methods=["GET"])
def get_events():
    """读全部事件。不按启动时间过滤，和 getHistory 保持同一套语义。"""
    return jsonify({"code": 0, "rows": read_events()})


@app.route("/media/<filename>", methods=["GET"])
def serve_media(filename):
    """回放/回看。用 <filename>（不含斜杠）而不是 <path:filename>：
    带路径分隔符的请求在路由层就 404 了，send_from_directory 里再做一层目录穿越防护。"""
    return send_from_directory(MEDIA_DIR, filename)


if __name__ == "__main__":
    # Flask 的 debug 重载器会把本模块跑两遍：父进程只负责监视文件、不执行 app.run，
    # 真正干活的是 WERKZEUG_RUN_MAIN=true 的子进程。不加这道判断的话，
    # 两个进程各起一条订阅线程，每条报文被处理两遍
    if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_mqtt()
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)
