# nova-dormmate-final-2026
宿舍温湿度监测系统

# DormMate 宿舍温湿度监测系统

## 项目简介

宿舍温湿度数据读取、校验与数据分析系统。当前已完成M1数据读取校验、M2数据分析绘图模块、M3网页摄像头+语音交互模块。

## 业务判断规则

温湿度独立判定，可同时存在多个状态标签：

1. 温度 < 18 →偏冷

2. 温度 ≥30 →偏热

3. 湿度 ≥75 →偏湿

4. 不满足以上全部条件 →正常

## 回归测试数据集

- 25℃ / 60% →正常
- 16℃ / 60% →偏冷
- 31℃ / 60% →偏热
- 25℃ / 80% →偏湿

## 环境依赖
Python3
需要安装包：matplotlib
Web前端：VS Code + Live Server，浏览器推荐Edge/Chrome

## 运行步骤（当前可运行部分）
1. 克隆项目到本地
2. 安装Python依赖：`pip install -r requirements.txt`
3. 执行数据分析脚本（M1+M2）
4. 运行完成后，在report目录生成 trend.png、report.html
5. M3网页摄像头&语音交互模块：使用Live Server打开`web/index.html`，localhost环境运行，授予摄像头、麦克风权限。

## 项目目录结构
 - data：存放CSV历史测试数据集
 - analysis：Python数据分析脚本analysis.py（M1、M2模块已完成）
 - web：Web主应用（M1，M2、M3模块已完成）
 - dashboard：实时看板（待开发）
 - 3d：Three.js宿舍3D视图（待开发）
 - miniapp：移动端小程序（待开发）
 - report：脚本运行输出目录，保留trend.png、report.html

## 已实现功能
1. M1模块：读取CSV文件，对温湿度数据进行校验，按统一规则计算状态status
2. M2模块：对校验完成的数据做统计分析，生成趋势图trend.png与报告report.html
3. M3模块：网页摄像头预览、Canvas抓拍快照保存；ASR语音识别+TTS语音朗读
    - 支持语音指令：【拍照】、【朗读状态】
    - 识别文字实时展示在页面日志；TTS朗读宿舍当前温湿度状态
    - 增加异常捕获：麦克风拒绝、网络异常、环境错误时页面输出友好提示

## 已知限制
1. 当前仅完成后端M1、M2，网页M3模块；M4及其余模块暂未开发
2. 目前仅支持本地CSV文件离线分析
3. M3语音识别使用浏览器Web Speech API，依赖云端在线识别服务
    - 必须localhost环境（Live Server）运行，直接双击html文件会禁用语音API
    - 网络波动会导致识别间歇性失效；备选方案：可更换Whisper.js离线ASR