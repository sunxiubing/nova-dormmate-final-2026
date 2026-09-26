/* DormMate 温湿度监测 —— 交互逻辑
 *
 * 判定规则：
 *   温度 < 18 ℃          -> 偏冷
 *   温度 >= 30 ℃         -> 偏热
 *   湿度 >= 75 %         -> 偏湿
 *   其余                 -> 正常
 *
 * 校验规则（不通过则不进入分析、不生成记录）：
 *   1. 空值        -> 提示“不能为空”
 *   2. 非数字      -> 提示“必须是数字”
 *   3. 明显异常值  -> 超出有效范围（温度 -20 ~ 60 ℃，湿度 0 ~ 100 %）时提示
 */
let isRunningCommand = false; // 指令冷却锁
let mediaStream = null; // 保存摄像头流，用来判断摄像头是否开启

(function () {
  'use strict';

  /* 有效量程，超出即视为明显异常读数 */
  var SPECS = {
    temperature: { label: '温度', unit: '℃', min: -20, max: 60 },
    humidity: { label: '湿度', unit: '%', min: 0, max: 100 }
  };

  /* 历史记录保留上限，远高于“连续 6 条”的要求 */
  var MAX_RECORDS = 100;

  var NUMBER_PATTERN = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;

  var STATE_LABEL = {
    normal: '正常',
    cold: '偏冷',
    hot: '偏热',
    wet: '偏湿'
  };

  var els = {
    form: document.getElementById('analyze-form'),
    temperature: document.getElementById('temperature'),
    humidity: document.getElementById('humidity'),
    resetBtn: document.getElementById('reset-btn'),
    exportBtn: document.getElementById('export-btn'),
    errorBox: document.getElementById('error-box'),
    resultPanel: document.getElementById('result-panel'),
    historyList: document.getElementById('history-list'),
    historyEmpty: document.getElementById('history-empty'),
    historyCount: document.getElementById('history-count')
  };

  var records = [];
  var serial = 0;

  /* ---------------- 校验 ---------------- */

  function parseReading(rawValue, key) {
    var spec = SPECS[key];
    var text = String(rawValue == null ? '' : rawValue).trim();

    if (text === '') {
      return { error: spec.label + '不能为空，请输入读数。' };
    }

    if (!NUMBER_PATTERN.test(text)) {
      return { error: spec.label + '必须是数字，「' + text + '」不是有效读数。' };
    }

    var value = Number(text);

    if (!isFinite(value)) {
      return { error: spec.label + '数值无效，请重新输入。' };
    }

    if (value < spec.min || value > spec.max) {
      return {
        error: spec.label + '超出有效范围（' + spec.min + ' ~ ' + spec.max + ' ' + spec.unit +
          '），当前为 ' + formatNumber(value) + ' ' + spec.unit + '，明显异常，请检查输入。'
      };
    }

    return { value: value };
  }

  /* ---------------- 判定 ---------------- */

  function judgeTemperature(value) {
    if (value < 18) return 'cold';
    if (value >= 30) return 'hot';
    return 'normal';
  }

  function judgeHumidity(value) {
    return value >= 75 ? 'wet' : 'normal';
  }

  function buildSuggestions(tempState, humidityState) {
    var list = [];

    if (tempState === 'cold') {
      list.push('温度偏低：建议关好门窗、开启空调制热，夜间注意保暖。');
    } else if (tempState === 'hot') {
      list.push('温度偏高：建议开启空调制冷，并保持空气流通。');
    } else {
      list.push('温度处于舒适区间，维持现状即可。');
    }

    if (humidityState === 'wet') {
      list.push('湿度偏高：建议开启除湿机或空调除湿模式，注意防霉防潮。');
    } else {
      list.push('湿度处于舒适区间，维持现状即可。');
    }

    return list;
  }

  function buildHeadline(tempState, humidityState) {
    var abnormal = [];
    if (tempState !== 'normal') abnormal.push('温度' + STATE_LABEL[tempState]);
    if (humidityState !== 'normal') abnormal.push('湿度' + STATE_LABEL[humidityState]);

    if (abnormal.length === 0) return '环境舒适，温湿度均正常';
    return abnormal.join(' · ') + '，建议按下方指引调整';
  }

  function overallState(tempState, humidityState) {
    if (tempState !== 'normal') return tempState;
    if (humidityState !== 'normal') return humidityState;
    return 'normal';
  }

  /* 单一状态标签，用于历史徽标与 CSV 的 status 列 */
  function buildSummary(tempState, humidityState) {
    var abnormal = [];
    if (tempState !== 'normal') abnormal.push(STATE_LABEL[tempState]);
    if (humidityState !== 'normal') abnormal.push(STATE_LABEL[humidityState]);
    return abnormal.length ? abnormal.join('+') : '正常';
  }

  /* ---------------- 格式化 ---------------- */

  function formatNumber(value) {
    return Number.isInteger(value) ? String(value) : value.toFixed(1);
  }

  function pad2(n) {
    return String(n).padStart(2, '0');
  }

  function formatTime(date) {
    return date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate()) +
      ' ' + pad2(date.getHours()) + ':' + pad2(date.getMinutes()) + ':' + pad2(date.getSeconds());
  }

  /* ---------------- 渲染：错误提示 ---------------- */

  function showErrors(errors) {
    els.errorBox.innerHTML = '';

    var title = document.createElement('strong');
    title.className = 'alert__title';
    title.textContent = '输入未通过校验，已终止分析';
    els.errorBox.appendChild(title);

    var list = document.createElement('ul');
    list.className = 'alert__list';
    errors.forEach(function (message) {
      var item = document.createElement('li');
      item.textContent = message;
      list.appendChild(item);
    });
    els.errorBox.appendChild(list);

    els.errorBox.hidden = false;
  }

  function clearErrors() {
    els.errorBox.hidden = true;
    els.errorBox.innerHTML = '';
    els.temperature.classList.remove('is-invalid');
    els.humidity.classList.remove('is-invalid');
    els.temperature.removeAttribute('aria-invalid');
    els.humidity.removeAttribute('aria-invalid');
  }

  function markInvalid(input) {
    input.classList.add('is-invalid');
    input.setAttribute('aria-invalid', 'true');
  }

  function showRejected(message) {
    els.resultPanel.className = 'result result--empty';
    els.resultPanel.innerHTML = '';
    var p = document.createElement('p');
    p.className = 'result__placeholder';
    p.textContent = message;
    els.resultPanel.appendChild(p);
  }

  /* ---------------- 渲染：分析结果 ---------------- */

  function buildChip(label, valueText, unit, state) {
    var chip = document.createElement('div');
    chip.className = 'chip chip--' + state;

    var labelEl = document.createElement('span');
    labelEl.className = 'chip__label';
    labelEl.textContent = label;

    var valueEl = document.createElement('span');
    valueEl.className = 'chip__value';
    valueEl.textContent = valueText + ' ' + unit;

    var tagEl = document.createElement('span');
    tagEl.className = 'chip__tag';
    tagEl.textContent = STATE_LABEL[state];

    chip.appendChild(labelEl);
    chip.appendChild(valueEl);
    chip.appendChild(tagEl);
    return chip;
  }

  function renderResult(record) {
    var panel = els.resultPanel;
    panel.className = 'result';
    panel.innerHTML = '';

    var headline = document.createElement('p');
    headline.className = 'result__headline result__headline--' + record.overall;
    headline.textContent = record.headline;
    panel.appendChild(headline);

    var chips = document.createElement('div');
    chips.className = 'chips';
    chips.appendChild(buildChip('温度', formatNumber(record.temperature), '℃', record.tempState));
    chips.appendChild(buildChip('湿度', formatNumber(record.humidity), '%', record.humidityState));
    panel.appendChild(chips);

    var suggestionTitle = document.createElement('h3');
    suggestionTitle.className = 'suggestion__title';
    suggestionTitle.textContent = '改善建议';

    var suggestionList = document.createElement('ul');
    suggestionList.className = 'suggestion__list';
    record.suggestions.forEach(function (text) {
      var li = document.createElement('li');
      li.textContent = text;
      suggestionList.appendChild(li);
    });

    var suggestionBox = document.createElement('div');
    suggestionBox.className = 'suggestion';
    suggestionBox.appendChild(suggestionTitle);
    suggestionBox.appendChild(suggestionList);
    panel.appendChild(suggestionBox);

    var timeEl = document.createElement('p');
    timeEl.className = 'result__time';
    timeEl.textContent = '分析时间：' + record.timeText;
    panel.appendChild(timeEl);
  }

  /* ---------------- 渲染：历史记录 ---------------- */

  function renderHistory() {
    els.historyList.innerHTML = '';

    records.forEach(function (record, i) {
      var item = document.createElement('li');
      item.className = 'history__item history__item--' + record.overall;

      var index = document.createElement('span');
      index.className = 'history__index';
      index.textContent = '#' + (records.length - i);

      var body = document.createElement('div');
      body.className = 'history__body';

      var time = document.createElement('span');
      time.className = 'history__time';
      time.textContent = record.timeText;

      var values = document.createElement('span');
      values.className = 'history__values';
      values.textContent = '温度 ' + formatNumber(record.temperature) + ' ℃ · 湿度 ' +
        formatNumber(record.humidity) + ' %';

      body.appendChild(time);
      body.appendChild(values);

      var summary = document.createElement('span');
      summary.className = 'history__summary';
      summary.textContent = record.summary;

      item.appendChild(index);
      item.appendChild(body);
      item.appendChild(summary);
      els.historyList.appendChild(item);
    });

    els.historyCount.textContent = records.length + ' 条';
    els.historyEmpty.hidden = records.length > 0;
    els.exportBtn.disabled = records.length === 0;
  }

  /* ---------------- 导出 CSV ---------------- */

  var CSV_HEADERS = ['time', 'temperature', 'humidity', 'status'];

  function buildCsv() {
    var lines = [CSV_HEADERS.join(',')];

    /* records 为倒序（最新在前），导出时按时间正序，便于后续绘制趋势图 */
    records.slice().reverse().forEach(function (record) {
      lines.push([
        record.timeText,
        formatNumber(record.temperature),
        formatNumber(record.humidity),
        record.summary
      ].join(','));
    });

    return lines.join('\r\n') + '\r\n';
  }

  /* 前置 BOM（U+FEFF），保证 Excel 正确识别 UTF-8 中文状态列 */
  var UTF8_BOM = String.fromCharCode(0xFEFF);

  function exportCsv() {
    if (records.length === 0) return;

    var blob = new Blob([UTF8_BOM + buildCsv()], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');

    link.href = url;
    link.download = 'dormmate.csv';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  /* ---------------- 主流程 ---------------- */

  function handleSubmit(event) {
    event.preventDefault();
    clearErrors();

    var errors = [];
    var inputs = [
      { key: 'temperature', el: els.temperature },
      { key: 'humidity', el: els.humidity }
    ];
    var values = {};

    inputs.forEach(function (item) {
      var result = parseReading(item.el.value, item.key);
      if (result.error) {
        errors.push(result.error);
        markInvalid(item.el);
      } else {
        values[item.key] = result.value;
      }
    });

    /* 校验不通过：只提示，不分析、不写历史 */
    if (errors.length > 0) {
      showErrors(errors);
      showRejected('本次输入未通过校验，未生成分析结果。');
      return;
    }

    var tempState = judgeTemperature(values.temperature);
    var humidityState = judgeHumidity(values.humidity);
    var overall = overallState(tempState, humidityState);
    var now = new Date();

    var record = {
      id: ++serial,
      time: now,
      timeText: formatTime(now),
      temperature: values.temperature,
      humidity: values.humidity,
      tempState: tempState,
      humidityState: humidityState,
      overall: overall,
      headline: buildHeadline(tempState, humidityState),
      suggestions: buildSuggestions(tempState, humidityState),
      summary: buildSummary(tempState, humidityState)
    };

    records.unshift(record);
    if (records.length > MAX_RECORDS) {
      records.length = MAX_RECORDS;
    }

    renderResult(record);
    renderHistory();
  }

  function handleReset() {
    clearErrors();
    showRejected('尚未分析。请输入温度与湿度后点击「分析」。');
    els.temperature.focus();
  }

  function handleInput(event) {
    event.target.classList.remove('is-invalid');
    event.target.removeAttribute('aria-invalid');

    /* 所有字段都恢复干净时，收起错误提示 */
    if (!els.temperature.classList.contains('is-invalid') &&
        !els.humidity.classList.contains('is-invalid')) {
      els.errorBox.hidden = true;
    }
  }

  els.form.addEventListener('submit', handleSubmit);
  els.resetBtn.addEventListener('click', handleReset);
  els.exportBtn.addEventListener('click', exportCsv);
  els.temperature.addEventListener('input', handleInput);
  els.humidity.addEventListener('input', handleInput);

  renderHistory();
})();

/* ============================================================
 * M3 阶段1：摄像头手动抓拍
 * 独立 IIFE，不引用 M1 闭包内任何变量；仅手动抓拍，无自动拍照。
 * ============================================================ */
(function () {
  'use strict';

  var video = document.getElementById('camera-video');
  var canvas = document.getElementById('camera-canvas');
  var previewBtn = document.getElementById('camera-preview-btn');
  var snapBtn = document.getElementById('camera-snap-btn');
  var statusEl = document.getElementById('camera-status');
  var metaEl = document.getElementById('camera-meta');

  if (!video || !canvas || !previewBtn || !snapBtn || !statusEl) {
    return;
  }

  var stream = null;

  /* M1 的 formatTime 在另一个闭包里取不到，M3 内保留一份等价的 4 行实现 */
  function formatTime(date) {
    return date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate()) +
      ' ' + pad2(date.getHours()) + ':' + pad2(date.getMinutes()) + ':' + pad2(date.getSeconds());
  }

  function pad2(value) {
    return value < 10 ? '0' + value : String(value);
  }

  function setStatus(text, isError) {
    statusEl.textContent = text;
    if (isError) {
      statusEl.classList.add('is-error');
    } else {
      statusEl.classList.remove('is-error');
    }
  }

  function mapError(err) {
    var name = err && err.name;
    if (name === 'NotAllowedError' || name === 'SecurityError') {
      return '摄像头权限被拒绝，请在浏览器地址栏允许访问后重试。';
    }
    if (name === 'NotFoundError' || name === 'DevicesNotFoundError' || name === 'OverconstrainedError') {
      return '未检测到可用摄像头，请确认设备已连接。';
    }
    if (name === 'NotReadableError' || name === 'TrackStartError') {
      return '摄像头被其他应用占用，请关闭占用程序后重试。';
    }
    return '开启预览失败：' + ((err && err.message) || '未知错误');
  }

  function onPreview() {
    if (stream) {
      setStatus('预览已开启，无需重复操作。');
      return;
    }
    if (typeof navigator === 'undefined' || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus('当前环境不支持摄像头，请通过 localhost 或 HTTPS 打开本页（file:// 无法调用）。', true);
      return;
    }

    setStatus('正在请求摄像头权限…');

    navigator.mediaDevices
      .getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false })
      .then(function (ms) {
        /* 参数不能叫 mediaStream，否则会遮蔽全局变量，下面那行赋值就落不到全局上 */
        stream = ms;
        video.srcObject = ms;
        mediaStream = ms;   /* 同步到全局，语音模块据此判断摄像头是否已开启 */
        /* muted + autoplay 已满足自动播放策略，这里显式 play() 兜底；
           失败不影响抓拍判定，故单独吞掉而不进入下面的 catch */
        var played = video.play();
        if (played && typeof played.catch === 'function') {
          played.catch(function () {});
        }
        snapBtn.disabled = false;
        setStatus('预览已开启，点击「抓拍快照」生成图片。');
      })
      .catch(function (err) {
        setStatus(mapError(err), true);
      });
  }

  function onSnap() {
    if (!stream || video.readyState < 2 || !video.videoWidth) {
      setStatus('请先开启预览，等画面出现后再抓拍快照。', true);
      return;
    }

    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    canvas.hidden = false;

    if (metaEl) {
      metaEl.hidden = false;
      metaEl.textContent = '抓拍时间：' + formatTime(new Date()) +
        '　分辨率：' + video.videoWidth + ' × ' + video.videoHeight;
    }

    setStatus('已抓拍快照，图片已生成在下方画布。');
  }

  previewBtn.addEventListener('click', onPreview);
  snapBtn.addEventListener('click', onSnap);
})();

/* ============================================================
 * M3 阶段2：语音交互（ASR + TTS）
 * 独立 IIFE，不引用 M1/M3 闭包内任何变量。
 * ASR 用浏览器原生 webkitSpeechRecognition，TTS 用 window.speechSynthesis。
 * 无自动监听、无自动朗读、无自动拍照；唯一一个 setTimeout 是指令冷却锁（见 handleFinal）。
 * ============================================================ */
(function () {
  'use strict';

  var toggleBtn = document.getElementById('voice-toggle-btn');
  var statusEl = document.getElementById('voice-status');
  var transcriptEl = document.getElementById('voice-transcript');
  var logEl = document.getElementById('voice-log');

  /* 页面没有语音卡片时直接静默退出，不访问 window */
  if (!toggleBtn || !statusEl || !transcriptEl || !logEl) {
    return;
  }

  var win = typeof window !== 'undefined' ? window : null;
  var SR = win ? (win.SpeechRecognition || win.webkitSpeechRecognition) : null;

  var PLACEHOLDER = '点击「开始语音监听」后，请说出指令…';
  var LOG_MAX = 20;
  /* 指令冷却时长：TTS 播报的尾音可能被麦克风收回并再次识别成指令 */
  var COMMAND_COOLDOWN_MS = 2000;

  /* 致命错误：这些情况下不自动重启，否则会陷入每秒失败一次的死循环。
     network 也算致命——国内 Chrome 的 ASR 走 Google 服务器，network 是常态。 */
  var FATAL = {
    'not-allowed': true,
    'service-not-allowed': true,
    'audio-capture': true,
    'network': true
  };

  var recognition = null;      // 单例，避免重复重建
  var listening = false;
  var manualStop = false;      // 用户主动停止，onend 据此不重启
  var speaking = false;        // TTS 播放期间抑制 onresult，防止识别到自己的声音
  var currentUtterance = null;

  /* ---------------- UI ---------------- */

  function setStatus(text, isError) {
    statusEl.textContent = text;
    if (isError) {
      statusEl.classList.add('is-error');
    } else {
      statusEl.classList.remove('is-error');
    }
  }

  function setButton(on) {
    if (on) {
      toggleBtn.textContent = '停止语音监听';
      toggleBtn.classList.add('is-listening');
    } else {
      toggleBtn.textContent = '开始语音监听';
      toggleBtn.classList.remove('is-listening');
    }
  }

  function setTranscript(text) {
    transcriptEl.textContent = text || PLACEHOLDER;
  }

  function appendLog(text) {
    var item = document.createElement('li');
    item.className = 'voice__log-item';
    item.textContent = text;
    logEl.appendChild(item);
    while (logEl.children.length > LOG_MAX) {
      logEl.removeChild(logEl.children[0]);
    }
  }

  /* ---------------- TTS ---------------- */

  function handleSpeechEnd(event) {
    /* 旧 utterance 的延迟回调不应清掉新 utterance 的状态 */
    if (event && event.utterance && event.utterance !== currentUtterance) {
      return;
    }
    speaking = false;
    currentUtterance = null;
  }

  function stopSpeaking() {
    /* speechSynthesis.cancel() 不触发 onend，speaking 必须手动清 */
    speaking = false;
    currentUtterance = null;
    try {
      if (win.speechSynthesis) {
        win.speechSynthesis.cancel();
      }
    } catch (err) { /* 忽略 */ }
  }

  function speak(text) {
    if (!win.speechSynthesis || !win.SpeechSynthesisUtterance) {
      return;
    }
    if (speaking) {
      stopSpeaking();   /* 后说的覆盖先说的 */
    }
    var utterance = new win.SpeechSynthesisUtterance(text);
    /* 只设 lang，中文声音交给浏览器挑：页面刚加载时 getVoices() 通常还是空数组 */
    utterance.lang = 'zh-CN';
    utterance.onend = handleSpeechEnd;
    utterance.onerror = handleSpeechEnd;
    currentUtterance = utterance;
    speaking = true;
    try {
      win.speechSynthesis.speak(utterance);
    } catch (err) {
      speaking = false;
      currentUtterance = null;
    }
  }

  /* ---------------- 朗读文本组装 ---------------- */

  function textOf(root, selector) {
    if (!root || typeof root.querySelector !== 'function') {
      return '';
    }
    var node = root.querySelector(selector);
    return node && node.textContent ? String(node.textContent).trim() : '';
  }

  /* 让 TTS 读得自然：℃ → 摄氏度，58 % → 百分之58，· → 逗号停顿 */
  function speakable(text) {
    return String(text)
      .replace(/(\d+(?:\.\d+)?)\s*%/g, '百分之$1')
      .replace(/\s*℃/g, '摄氏度')
      .replace(/\s*·\s*/g, '，');
  }

  function buildStatusText() {
    /* 主源：最新一次分析的结果面板 */
    var panel = document.getElementById('result-panel');
    var chips = panel && typeof panel.querySelectorAll === 'function'
      ? panel.querySelectorAll('.chip') : [];
    var parts = [];
    for (var i = 0; i < chips.length; i++) {
      var label = textOf(chips[i], '.chip__label');
      var value = textOf(chips[i], '.chip__value');
      var tag = textOf(chips[i], '.chip__tag');
      if (!label || !value) {
        continue;
      }
      parts.push(label + ' ' + speakable(value) + (tag ? '，' + tag : ''));
    }
    if (parts.length) {
      return '当前' + parts.join('；') + '。';
    }

    /* 备用源：结果面板被「清空」后仍可读历史最新一条 */
    var list = document.getElementById('history-list');
    var item = list && typeof list.querySelector === 'function'
      ? list.querySelector('li') : null;
    var values = textOf(item, '.history__values');
    if (values) {
      var summary = textOf(item, '.history__summary');
      return '最新记录：' + speakable(values) + (summary ? '，状态 ' + summary : '') + '。';
    }

    return '';
  }

  function readStatus() {
    var text = buildStatusText();
    if (!text) {
      setStatus('暂无分析记录，无法朗读状态。');
      speak('暂无分析记录，请先输入温湿度并点击分析。');
      return;
    }
    setStatus('已朗读当前状态：' + text);
    speak(text);
  }

  /* ---------------- 语音指令 ---------------- */

  function takeSnapshot() {
    /* 摄像头未开启时直接拦截：不能走到 canvas，否则会生成一张黑屏图片。
       注意 TTS 回话里不能出现「拍照」二字，否则被麦克风收回后会再次触发本函数。 */
    if (!mediaStream) {
      setStatus('摄像头未开启，请先打开摄像头。', true);
      appendLog('错误：摄像头未开启，请先打开摄像头！');
      speak('摄像头未开启，请先打开摄像头');
      return;
    }

    var snapBtn = document.getElementById('camera-snap-btn');
    if (!snapBtn) {
      return;
    }
    /* 禁用按钮的 click() 是空操作，必须先查 disabled 才有提示 */
    if (snapBtn.disabled) {
      setStatus('请先开启摄像头预览，再下达抓拍指令。', true);
      speak('请先开启摄像头预览，再下达抓拍指令。');
      return;
    }
    snapBtn.click();   /* 复用 M3 阶段1 的抓拍逻辑，不改动相机代码 */
    setStatus('已通过语音指令触发抓拍。');
    speak('已完成抓拍。');
  }

  function handleFinal(text) {
    /* 冷却锁：上一条指令执行后 COMMAND_COOLDOWN_MS 内不再接受新指令。
       这里拦截而不是在 onResult 顶部拦，否则冷却期间连识别文字都不上屏了。 */
    if (isRunningCommand) {
      return;
    }

    var wantsSnap = text.indexOf('拍照') !== -1;
    var wantsRead = text.indexOf('朗读状态') !== -1;

    if (!wantsSnap && !wantsRead) {
      setStatus('未识别到指令，请说「朗读状态」或「拍照」。');
      return;
    }

    isRunningCommand = true;
    if (wantsSnap) {
      takeSnapshot();
    }
    /* 朗读放在最后：即使拍照先播了提示，cancel-and-replace 也保证朗读最终胜出 */
    if (wantsRead) {
      readStatus();
    }
    setTimeout(function () {
      isRunningCommand = false;
    }, COMMAND_COOLDOWN_MS);
  }

  /* ---------------- ASR 生命周期 ---------------- */

  function onResult(event) {
    if (speaking) {
      return;   /* TTS 正在播报，忽略麦克风收到的声音 */
    }
    var finalText = '';
    var interimText = '';
    var start = typeof event.resultIndex === 'number' ? event.resultIndex : 0;
    /* 逐个判断 isFinal：final 结果可能跨多个 onresult 事件，不能只取数组尾 */
    for (var i = start; i < event.results.length; i++) {
      var result = event.results[i];
      var transcript = result && result[0] && result[0].transcript
        ? String(result[0].transcript).trim() : '';
      if (!transcript) {
        continue;
      }
      if (result.isFinal) {
        finalText += transcript;
      } else {
        interimText += transcript;
      }
    }

    if (finalText) {
      setTranscript(finalText);
      appendLog(finalText);
      handleFinal(finalText);
    } else if (interimText) {
      setTranscript(interimText);
    }
  }

  function mapSpeechError(code) {
    if (code === 'no-speech') {
      return '未检测到语音，继续监听中…';
    }
    if (code === 'audio-capture') {
      return '未找到麦克风，请确认设备已连接。';
    }
    if (code === 'not-allowed' || code === 'service-not-allowed') {
      return '麦克风权限被拒绝，请在浏览器地址栏允许访问后重试。';
    }
    if (code === 'network') {
      return '语音识别服务网络错误：识别需要联网，Chrome 使用 Google 语音服务，国内可能不可达，可改用 Edge 浏览器。';
    }
    if (code === 'aborted') {
      return '语音识别已中止。';
    }
    return '语音识别出错：' + (code || '未知错误');
  }

  function onError(event) {
    var code = event && event.error ? event.error : '';
    setStatus(mapSpeechError(code), true);
    if (FATAL[code]) {
      listening = false;
      manualStop = true;   /* 置位后 onend 不会自动重启 */
      setButton(false);
    }
  }

  function onEnd() {
    if (!listening || manualStop) {
      return;
    }
    /* Chrome 静音约 10 秒会自动 end，这里重启以保持连续监听 */
    try {
      recognition.start();
    } catch (err) { /* 重启竞态，忽略 */ }
  }

  function makeRecognition() {
    if (recognition) {
      return recognition;
    }
    recognition = new SR();
    recognition.lang = 'zh-CN';
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.onresult = onResult;
    recognition.onerror = onError;
    recognition.onend = onEnd;
    return recognition;
  }

  function start() {
    if (listening) {
      return;
    }
    manualStop = false;
    listening = true;
    setButton(true);
    setStatus('正在监听，请说出「朗读状态」或「拍照」。');
    try {
      makeRecognition().start();
    } catch (err) {
      listening = false;
      setButton(false);
      setStatus('语音监听启动失败：' + ((err && err.message) || '未知错误'), true);
    }
  }

  function stop() {
    manualStop = true;    /* 必须先置位：onend 是异步回调，否则会被判定为需要重启 */
    listening = false;
    try {
      if (recognition) {
        recognition.stop();
      }
    } catch (err) { /* 忽略 */ }
    stopSpeaking();
    setButton(false);
    setTranscript('');
    setStatus('已停止语音监听。');
  }

  function onToggle() {
    if (listening) {
      stop();
    } else {
      start();
    }
  }

  /* ---------------- 初始守卫 ---------------- */

  if (!SR) {
    setStatus('当前浏览器不支持语音识别，请使用 Chrome 或 Edge 浏览器。', true);
    toggleBtn.disabled = true;
    return;
  }

  /* 注意：file:// 在 Secure Contexts 规范里算可信来源，isSecureContext 为 true，
     拦不住，必须显式判断协议。这里只警示不禁用——真失败会走 onerror 展示。 */
  if (win.location && win.location.protocol === 'file:') {
    setStatus('file:// 环境下语音识别不可靠，请通过 localhost 或 HTTPS 打开本页。', true);
  }

  toggleBtn.addEventListener('click', onToggle);
})();
