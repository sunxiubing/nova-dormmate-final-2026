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
"""
import csv
import json
import os
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
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)
