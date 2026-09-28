# DormMate 宿舍温湿度监测系统

## 项目介绍
DormMate是宿舍多节点温湿度监测系统。系统支持读取本地CSV温湿度数据进行校验、统计分析并生成可视化报告；网页端具备摄像头抓拍、语音识别交互能力；M5模块基于MQTT协议传输实时温湿度JSON数据，实现网页看板实时状态展示，并完成MQTT通信故障模拟验证。

## 业务判断规则
温湿度独立判定，可同时存在多个状态标签：
1. 温度 < 18 → 偏冷
2. 温度 ≥ 30 → 偏热
3. 湿度 ≥ 75 → 偏湿
4. 不满足以上全部条件 → 正常

## 回归测试数据集
- 25℃ / 60% → 正常
- 16℃ / 60% → 偏冷
- 31℃ / 60% → 偏热
- 25℃ / 80% → 偏湿

## 环境依赖
Python3
需要安装包：matplotlib
Web前端：VS Code + Live Server，浏览器推荐Edge/Chrome
M5模块额外依赖：Mosquitto MQTT Broker、MQTTX客户端

## 运行步骤 & 启动方法
1. 克隆项目到本地
2. 安装Python依赖：`pip install -r requirements.txt`
3. 执行数据分析脚本（M1+M2），运行完成后，在report目录生成 trend.png、report.html
4. M3网页摄像头&语音交互模块：使用Live Server打开`web/index.html`，localhost环境运行，授予摄像头、麦克风权限。
5. M5 MQTT实时通信模块：
    ① 使用管理员身份打开终端，启动Mosquitto服务
    ```bash
    mosquitto -c mosquitto.conf
    ```
    ② 打开MQTTX，新建发布、订阅两组连接（ClientID不能相同）
    ③ 订阅主题 `dormmate/dorm-c/env`
    ④ 发布JSON报文，网页Dashboard接收消息并实时更新宿舍温湿度状态。

## 项目目录结构
- data: 存放CSV历史测试数据集
- analysis: Python数据分析脚本analysis.py（M1、M2模块已完成）
- web: Web主应用（M1，M2、M3模块已完成）
- dashboard: 实时看板（M5已完成；M6待开发）
- 3d: Three.js宿舍3D视图（待开发）
- miniapp: 移动端小程序（已开发）
- report: 脚本运行输出目录，保留trend.png、report.html、实验截图
- .gitignore: Git忽略配置文件
- README.md: 项目说明文档

## 已实现功能
1. M1模块：读取CSV文件，对温湿度数据进行校验，按统一规则计算状态status
2. M2模块：对校验完成的数据做统计分析，生成趋势图trend.png与报告report.html
3. M3模块：网页摄像头预览、Canvas抓拍快照保存；ASR语音识别+TTS语音朗读
    - 支持语音指令：【拍照】、【朗读状态】
    - 识别文字实时展示在页面日志；TTS朗读宿舍当前温湿度状态
    - 增加异常捕获：麦克风拒绝、网络异常、环境错误时页面输出友好提示
4. M4模块：CSV文件持久化存储温湿度数据
5. M5模块：MQTT实时通信 + 三组故障模拟实验
    - M5故障模拟实验：
      ① 错误Topic测试：使用错误Topic发送报文，前端不更新对应宿舍面板；修正Topic后恢复
      ② 非法JSON报文测试：发送残缺JSON字符串，MQTT收到消息，但前端JSON解析报错、页面不刷新；补齐JSON括号修复
      ③ 停止Broker测试：关闭Mosquitto服务，MQTTX报ECONNREFUSED连接拒绝，通信中断；重启Broker恢复通信

## 已知限制
1. 当前已完成后端M1，M2，网页M3模块；M4、M5已完成；M6及其余模块暂未开发
2. 目前仅支持本地CSV文件离线分析
3. M3语音识别使用浏览器Web Speech API，依赖云端在线识别服务
    - 必须localhost环境（Live Server）运行，直接双击html文件会禁用语音API
    - 网络波动会导致识别间歇性失效；备选方案：可更换Whisper.js离线ASR