from flask import Flask, request, jsonify
import csv
import os
from datetime import datetime

app = Flask(__name__)
CSV_PATH = "./data/dormmate.csv"

# 没有csv就自动创建表头
def init_csv():
    if not os.path.exists(CSV_PATH):
        os.makedirs("data", exist_ok=True)
        with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "temp", "humi", "statusText", "time"])

# 接口1：读取全部历史记录（小程序、网页共用 GET）
@app.route("/api/getHistory", methods=["GET"])
def get_history():
    init_csv()
    res_list = []
    # utf-8-sig：导出的 CSV 常带 BOM，用 utf-8 读会把第一列名变成 "﻿time"，
    # 小程序那边取 item.time 就是 undefined。utf-8-sig 对无 BOM 文件行为一样，能同时兼容两种
    with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            res_list.append(row)
    return jsonify(res_list)

# 接口2：网页新增记录 POST
@app.route("/api/addRecord", methods=["POST"])
def add_record():
    init_csv()
    data = request.get_json()
    temp = data.get("temp")
    humi = data.get("humi")
    statusText = data.get("statusText")
    now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 计算id
    # utf-8-sig：导出的 CSV 常带 BOM，用 utf-8 读会把第一列名变成 "﻿time"，
    # 小程序那边取 item.time 就是 undefined。utf-8-sig 对无 BOM 文件行为一样，能同时兼容两种
    with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
        row_count = sum(1 for _ in f) - 1

    with open(CSV_PATH, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([row_count+1, temp, humi, statusText, now_time])
    return jsonify({"code":0,"msg":"保存成功"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)