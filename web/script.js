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
