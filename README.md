# DormMate 宿舍温湿度监测系统
> 仓库：nova-dormmate-final-2026
> 已完成模块：M1~M5 + A1~A4事件处置闭环

## 项目简介
DormMate 是面向宿舍场景的多节点温湿度监测演示系统。系统支持离线读取CSV文件完成数据分析与绘图；基于MQTT协议接收 dorm-a、dorm-b、dorm-c 三个宿舍节点实时JSON温湿度报文，在网页看板和3D宿舍页面可视化展示环境状态。系统自动识别环境异常、计算异常优先级，支持下发设备控制指令；持续接收报文自动判定处置效果，全部异常与操作记录写入日志，自动生成带完整事件时间线的复盘网页报告。项目配套单元测试与端到端测试。

## 环境判定规则
温湿度独立判定，同一宿舍可同时存在多个状态标签：
- 温度＜18℃ → 偏冷
- 温度≥30℃ → 偏热
- 湿度≥75% → 偏湿
- 其余情况 → 正常

## 功能总览
1. **M1**：读取CSV温湿度数据，数据校验，判定环境状态
2. **M2**：数据统计分析，生成温湿度趋势图与网页报告
3. **M3**：网页摄像头抓拍、语音识别与语音播报，支持语音指令交互
4. **M4**：采集的温湿度数据持久化存储至CSV文件
5. **M5**：Mosquitto搭建MQTT实时通信服务，MQTTX模拟宿舍节点发送报文；完成错误Topic、非法JSON报文、Broker停机三类通信故障模拟测试
6. **A1~A4事件处置闭环**：自动计算异常宿舍优先级；远程控制风扇、灯光、除湿机及学习/睡眠/离寝场景模式；接收新报文自动判断处置结果；全流程事件写入日志，生成事件复盘报告

## 📁 项目目录        DormMate-Final/
├── 3d/                 # Three.js 3D宿舍可视化页面
├── a1/                 # A1优先级判定核心脚本
├── analysis/           # Python数据分析脚本，生成可视化与复盘报告
├── dashboard/          # 实时环境看板页面
├── data/               # 数据集、运行产生事件日志
├── miniapp/            # 微信小程序前端代码
├── report/             # 趋势图、输出网页报告
├── web/                # M3多媒体页面（摄像头、语音交互）
├── .gitignore
├── app.py              # Flask后端，事件日志落盘、接口服务
├── README.md
├── requirements.txt
└── server.py        ## 🛠️ 环境依赖
Python3
前端：VS Code + Live Server，推荐Chrome/Edge浏览器
MQTT服务：Mosquitto、MQTTX客户端

Python依赖包：`pandas matplotlib scikit-learn flask flask-cors`

##   快速启动
1. 安装Python依赖
```bash
pip install -r requirements.txt     
2. M1/M2 离线数据分析，生成报告python analysis/analysis.py
3. M3摄像头&语音模块：Live Server打开 web/index.html ，授予摄像头、麦克风权限
4. M5 MQTT实时通信 mosquitto -c mosquitto.conf
5. A1~A4 事件闭环演示，启动MQTT Broker后，启动后端服务 python app.py

##   已知限制
1. M1~M5、A1~A4模块已完成，其余预留模块暂未开发；
​2. 离线分析仅支持本地CSV文件；
​3. 语音识别依赖浏览器Web Speech API，仅localhost环境可用；
​4. 同一时间仅开启单个Dashboard页面，多页面同时操作会产生重复事件日志；
​5. 节点停止上报数据时，处置状态会保持「处理中」，不会自动超时判定。

