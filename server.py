from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app) # 允许跨域

# 模拟数据库，用来临时存 Web 发来的数据
db_storage = {"data": []}

# 1. 接收 Web 端数据
@app.route('/api/sync', methods=['POST'])
def sync_from_web():
    try:
        req_data = request.get_json()
        if not req_data or 'data' not in req_data:
            return jsonify({"code": 400, "message": "数据格式错误"}), 400
        
        db_storage['data'] = req_data['data']
        print(f"【同步成功】收到来自 Web 端的 {len(db_storage['data'])} 条真实数据")
        return jsonify({"code": 200, "message": "同步成功", "received_count": len(db_storage['data'])}), 200
    except Exception as e:
        return jsonify({"code": 500, "message": str(e)}), 500

# 2. 给小程序提供数据
@app.route('/api/get_data', methods=['GET'])
def get_data_for_miniprogram():
    return jsonify({"code": 200, "message": "success", "data": db_storage['data']}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)