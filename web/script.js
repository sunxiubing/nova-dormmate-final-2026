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

/* 后端地址候选，按顺序试，第一个连上的用。
   原来是写死的 http://192.168.131.200:5000 —— 那是台机器的局域网 IP，后来变了，
   写死就等于静默失效（请求发出去没人接，页面上什么也看不出来）。
   现在和 app.py 的启动方式对齐：本机跑用 localhost，手机访问时退回局域网地址。
   window.DORMATE_API_HOSTS 是给自动化测试覆盖用的。 */
var API_HOSTS = (typeof window !== 'undefined' && window.DORMATE_API_HOSTS)
  ? window.DORMATE_API_HOSTS
  : ['http://localhost:5000', 'http://192.168.171.200:5000'];

/* 媒体类型 → CSV 列名，和后端 MEDIA_COLUMN 一致 */
var MEDIA_FIELD = { photo: 'photo_name', video: 'video_name', audio: 'audio_name' };

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

  /* 历史条目里的媒体回看：照片给缩略图、视频给播放器、录音给音频条。
     地址指向后端的 /media/<文件名> —— CSV 里只有文件名，实体在 data/media。

     这里刻意不再要求 apiBase 已就绪：原来写的是「连不上后端就整块不渲染」，
     结果后端没起（或探测还没回来）时，历史里明明有文件名却什么控件都不出现，
     看着就是「视频和录音丢了」。现在一律按候选地址渲染，加载失败再把控件
     换成一行说明 —— 至少能看到是哪个文件、为什么没出来。
     后端探测成功后 renderHistory 会重跑，地址自动换成真正连上的那个。 */
  function buildMediaBox(record) {
    if (!record.photoName && !record.videoName && !record.audioName) return null;

    var base = apiBase || API_HOSTS[0];

    var box = document.createElement('div');
    box.className = 'history__media-box';

    /* 文件在不在，只有真发一次请求才知道。加载不出来时把控件替换成说明文字：
       留一个点不动的播放器，用户只会以为功能坏了 */
    function replaceOnFail(el, name) {
      el.addEventListener('error', function () {
        var hint = document.createElement('p');
        hint.className = 'history__media-missing';
        hint.textContent = name + ' 加载失败。请确认后端已启动（python app.py），' +
          '并且 data/media 里有这个文件。';
        if (el.parentNode) el.parentNode.replaceChild(hint, el);
      });
    }

    if (record.photoName) {
      var img = document.createElement('img');
      img.className = 'history__media--img';
      img.loading = 'lazy';
      img.alt = '抓拍照片 ' + record.timeText;
      img.title = '点击查看原图';
      img.src = mediaUrl(record.photoName, base);
      /* CSS 里给了 cursor: zoom-in，那就得真能放大 —— 开新标签页看原图。
         用 img.src 而不是重算一遍：地址里带着实际连上的 host，重算会丢 */
      img.addEventListener('click', function () {
        window.open(img.src, '_blank');
      });
      replaceOnFail(img, record.photoName);
      box.appendChild(img);
    }

    if (record.videoName) {
      var videoEl = document.createElement('video');
      videoEl.className = 'history__media--video';
      videoEl.controls = true;
      videoEl.preload = 'metadata';
      videoEl.src = mediaUrl(record.videoName, base);
      replaceOnFail(videoEl, record.videoName);
      box.appendChild(videoEl);
    }

    if (record.audioName) {
      var audioEl = document.createElement('audio');
      audioEl.className = 'history__media--audio';
      audioEl.controls = true;
      audioEl.preload = 'metadata';
      audioEl.src = mediaUrl(record.audioName, base);
      replaceOnFail(audioEl, record.audioName);
      box.appendChild(audioEl);
    }

    return box;
  }

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

      var mediaBox = buildMediaBox(record);
      if (mediaBox) item.appendChild(mediaBox);

      els.historyList.appendChild(item);
    });

    els.historyCount.textContent = records.length + ' 条';
    els.historyEmpty.hidden = records.length > 0;
    els.exportBtn.disabled = records.length === 0;

    /* 渲染是 records 变化的唯一出口，存档挂在这里就够，
       不用在每个改 records 的地方各写一遍（漏一个就丢一次数据） */
    persist();
  }

  /* ---------------- 导出 CSV ---------------- */

  /* 7 列，与 data/dormmate.csv 的表头、app.py 的 CSV_HEADER、小程序导出一致。
     后三列存的是文件名（不是二进制），实体都在后端 data/media 里 */
  var CSV_HEADERS = ['time', 'temperature', 'humidity', 'status',
                     'photo_name', 'video_name', 'audio_name'];

  function buildCsv() {
    var lines = [CSV_HEADERS.join(',')];

    /* records 为倒序（最新在前），导出时按时间正序，便于后续绘制趋势图 */
    records.slice().reverse().forEach(function (record) {
      lines.push([
        record.timeText,
        formatNumber(record.temperature),
        formatNumber(record.humidity),
        record.summary,
        record.photoName || '',
        record.videoName || '',
        record.audioName || ''
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

    /* 用户很自然会以为导出的 CSV 就是全部数据 —— 媒体实体不在 CSV 里，
       不提醒的话换台机器打开会发现照片视频全是坏的。
       页面上留一行文字，另加一次 alert：导出是用户主动触发的动作，这里弹一次不烦人 */
    var tips = 'CSV 已导出。完整保存图片、视频、音频，需要同步复制后端的 data/media 文件夹。';
    showNotice(tips);
    window.alert(tips);
  }

  /* ---------------- 后端同步 ---------------- */

  /* 当前连上的后端地址；没连上时为 ''，媒体控件据此决定渲不渲染 */
  var apiBase = '';

  var noticeEl = document.getElementById('export-notice');

  function showNotice(text) {
    if (!noticeEl) return;
    noticeEl.textContent = text;
    noticeEl.hidden = false;
  }

  function hideNotice() {
    if (noticeEl) noticeEl.hidden = true;
  }

  /* 不传 base 时用已探测到的后端地址；还没探测出来时退回第一个候选地址 ——
     历史上写过 mediaUrl() 直接返回 apiBase+'/media/...'，apiBase 为空串时
     会拼出一个相对路径 '/media/xxx'，指到静态服务器上，永远是 404 */
  function mediaUrl(filename, base) {
    return (base || apiBase || API_HOSTS[0]) + '/media/' + encodeURIComponent(filename);
  }

  /* 新纪录同步到后端 CSV。失败不会打断本地流程 —— 页面上这条记录已经出来了，
     只是刷新之后会丢，所以用 notice 提示而不是弹错误框打断用户 */
  function postRecordToBackend(record, hostIdx) {
    hostIdx = hostIdx || 0;
    if (hostIdx >= API_HOSTS.length) {
      showNotice('记录已显示在页面上，但保存到后端失败：请确认后端已启动（python app.py）。');
      return;
    }

    fetch(API_HOSTS[hostIdx] + '/api/addRecord', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        temperature: record.temperature,
        humidity: record.humidity
      })
    }).then(function (res) {
      if (res.ok) {
        apiBase = API_HOSTS[hostIdx];
        hideNotice();
        /* 把本地时间换成服务端返回的那个。页面这条记的是浏览器时间，CSV 里那行记的是
           服务端的 now，两边跨过整秒时就会差一秒 —— 刷新合并时同一次分析会被当成两条
           并排显示。统一以服务端为准，本地和后端就精确对得上了 */
        return res.json().then(function (json) {
          if (json && json.time && record.timeText !== json.time) {
            record.timeText = json.time;
            renderHistory();
          }
        }).catch(function () { /* 响应不是 JSON 也不影响本地已经显示出来的记录 */ });
      }
      /* 4xx 是数据本身的问题，换台机器一样会被拒；只有 5xx 才值得换 host 重试 */
      if (res.status >= 500) {
        postRecordToBackend(record, hostIdx + 1);
      } else {
        showNotice('后端拒绝了这条记录（HTTP ' + res.status + '），它只存在于本页面。');
      }
    }).catch(function () {
      postRecordToBackend(record, hostIdx + 1);
    });
  }

  /* 后端行 → 页面记录。状态不直接采信 CSV 里那一列，本地按同一套阈值重算：
     CSV 可能是手工改过的、也可能来自旧版本，页面上显示的始终是当下的判定规则 */
  function mapRowToRecord(row) {
    var temperature = Number(row.temperature);
    var humidity = Number(row.humidity);
    var tempState = judgeTemperature(temperature);
    var humidityState = judgeHumidity(humidity);

    return {
      id: 0,
      time: null,
      timeText: row.time || '',
      temperature: temperature,
      humidity: humidity,
      tempState: tempState,
      humidityState: humidityState,
      overall: overallState(tempState, humidityState),
      headline: buildHeadline(tempState, humidityState),
      suggestions: buildSuggestions(tempState, humidityState),
      summary: buildSummary(tempState, humidityState),
      photoName: row.photo_name || '',
      videoName: row.video_name || '',
      audioName: row.audio_name || ''
    };
  }

  /* 后端启动时刻（页面据此过滤）。空串表示没取到，这时不过滤 */
  var serverStartedAt = '';

  /* ---------------- 本地存档 ----------------
     页面上的记录只活在内存里，刷新、误关标签、Live Server 自动重载都会让它归零，
     表现就是「刚分析完，状态与建议和历史数据全没了」。所以每渲染一次就顺手存一份，
     下次打开先把它恢复出来 —— 不依赖后端是否在跑。
     只存文件名索引，媒体二进制始终只在 data/media 里，不进浏览器存储。 */
  var STORE_KEY = 'dormmate.web.v1';

  /* 「清空」的清零点：这个时刻之前的记录不再上屏，刷新也不会回来。
     后端没有删除接口、CSV 一个字都不动 —— 老数据仍然完整躺在文件里，
     只是页面这边把它划到了线外。空串 = 从没清空过，全都算数。 */
  var clearedBefore = '';

  function persist() {
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify({
        temperature: els.temperature.value,
        humidity: els.humidity.value,
        clearedBefore: clearedBefore,
        /* 存成后端行的形状（下划线列名），恢复时直接喂给 mapRowToRecord，
           判定状态重新算一遍，不会把旧版本的结论一起冻在存档里 */
        records: records.map(function (r) {
          return {
            time: r.timeText, temperature: r.temperature, humidity: r.humidity,
            photo_name: r.photoName || '', video_name: r.videoName || '', audio_name: r.audioName || ''
          };
        })
      }));
    } catch (err) {
      /* 无痕模式、存储配额满都会抛。存档是锦上添花，失败了也不该影响主流程 */
    }
  }

  function restore() {
    var raw;
    try { raw = window.localStorage.getItem(STORE_KEY); } catch (err) { return; }
    if (!raw) return;

    var data;
    try { data = JSON.parse(raw); } catch (err) { return; }
    if (!data || typeof data !== 'object') return;

    /* 输入框恢复成上次填的：用户要的就是「自动保留最后一次输入的温湿度」 */
    if (typeof data.temperature === 'string') els.temperature.value = data.temperature;
    if (typeof data.humidity === 'string') els.humidity.value = data.humidity;

    /* 清零点比 records 更重要：清空那一刻本地列表是空的，光靠 records 恢复，
       下次 loadBackendHistory 会把后端的老记录当成「本次运行的新记录」全捞回来 */
    if (typeof data.clearedBefore === 'string') clearedBefore = data.clearedBefore;

    if (Array.isArray(data.records) && data.records.length) {
      records = data.records.slice(0, MAX_RECORDS).map(mapRowToRecord);
      serial = records.length;
      /* 状态与建议面板也一并恢复：只恢复历史不恢复面板的话，
         刷新后会出现「列表里有数据、上面却说尚未分析」的错配 */
      renderResult(records[0]);
    }
  }

  /* 页面打开时把后端历史拉回来。这是「清空后刷新恢复」的机制：
     清空只动内存里的 records，CSV 一直在后端，刷新就重新读一遍。
     只展示本次启动之后新增的记录 —— CSV 里早先留下的行（项目早期数据、
     校验数据集）不再挤进实时视图。它们还在文件里，只是不上屏。 */
  function loadBackendHistory(hostIdx) {
    hostIdx = hostIdx || 0;
    if (hostIdx >= API_HOSTS.length) {
      showNotice('连不上后端服务，历史记录未能从服务器加载（本地记录仍会显示，分析也照常可用）。');
      return;
    }

    var base = API_HOSTS[hostIdx];

    fetch(base + '/api/serverInfo')
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (info) {
        var since = info && info.started_at ? String(info.started_at) : '';
        return fetch(base + '/api/getHistory')
          .then(function (res) {
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return res.json();
          })
          .then(function (rows) {
            if (!Array.isArray(rows)) throw new Error('返回的不是数组');
            apiBase = base;
            serverStartedAt = since;
            hideNotice();

            /* 时间戳格式两端一致（%Y-%m-%d %H:%M:%S），直接按字符串比大小就够 */
            var fresh = since
              ? rows.filter(function (row) { return String(row.time || '') >= since; })
              : rows;

            /* 再滤一道清零点：点过「清空」之后，那之前的行就永远不上屏了。
               这里读的是当前值而不是请求发起时的值 —— 请求在飞的时候用户点了清空，
               迟到的响应也得按新的线过滤，否则刚清完的记录会被它整批灌回来。
               严格大于：与清空同一秒内新建的那条靠下面的本地合并补回来，
               不会因为「和清零点同秒」被判出局 */
            if (clearedBefore) {
              fresh = fresh.filter(function (row) { return String(row.time || '') > clearedBefore; });
            }

            /* 后端是正序（旧在前），页面是倒序（新在前），翻转一下；
               只取最近 MAX_RECORDS 条，和本地新建记录的上限保持一致 */
            var merged = fresh.slice(-MAX_RECORDS).map(mapRowToRecord).reverse();

            /* 合并本地已有、后端还没有的记录。
               这个请求可能要跑几秒（第一个地址连不上要等超时），期间用户多半已经
               分析过几条了 —— 直接用后端的列表覆盖，会把这几条当场抹掉，
               表现就是「刚分析完就没了」。所以按时间戳对齐后补回去。 */
            var seen = {};
            merged.forEach(function (r) { seen[r.timeText] = true; });
            records.slice().reverse().forEach(function (r) {
              if (r.timeText && !seen[r.timeText]) { merged.unshift(r); seen[r.timeText] = true; }
            });

            records = merged.slice(0, MAX_RECORDS);
            serial = records.length;
            renderHistory();
          });
      })
      .catch(function () {
        loadBackendHistory(hostIdx + 1);
      });
  }

  /* 上传媒体：文件 + 类型 + 当时的温湿度 → 后端落盘 data/media 并记一行 CSV。
     成功后拿后端返回的时间戳/文件名在本地也插一条，用户拍完立刻能在历史里看到，不用刷新 */
  function uploadMedia(blob, fileName, mediaType, reading, onSuccess, onError) {
    var form = new FormData();
    form.append('file', blob, fileName);
    form.append('media_type', mediaType);
    form.append('temperature', String(reading.temperature));
    form.append('humidity', String(reading.humidity));

    /* 已经连上过就用那个地址，省一次失败探测；没连上就挨个试 */
    var hosts = apiBase ? [apiBase] : API_HOSTS.slice();

    function attempt(idx) {
      if (idx >= hosts.length) {
        onError('连不上后端，上传失败。请确认后端已启动（python app.py）。');
        return;
      }

      fetch(hosts[idx] + '/api/uploadMedia', { method: 'POST', body: form })
        .then(function (res) {
          return res.json().catch(function () { return {}; }).then(function (json) {
            if (res.ok) {
              apiBase = hosts[idx];
              hideNotice();

              var row = {
                time: json.time,
                temperature: reading.temperature,
                humidity: reading.humidity
              };
              row[MEDIA_FIELD[mediaType]] = json.filename;

              records.unshift(mapRowToRecord(row));
              if (records.length > MAX_RECORDS) {
                records.length = MAX_RECORDS;
              }
              renderHistory();
              onSuccess(json);
            } else if (res.status >= 500) {
              attempt(idx + 1);
            } else {
              onError(json.msg || ('上传被拒绝（HTTP ' + res.status + '）'));
            }
          });
        })
        .catch(function () {
          attempt(idx + 1);
        });
    }

    attempt(0);
  }

  /* 当前读数：取最近一次分析。没有分析记录就返回 null ——
     拍照/录像/录音都要往 CSV 里写温湿度，没有读数就没有环境上下文可记 */
  function getCurrentReading() {
    if (!records.length) return null;
    var r = records[0];
    return {
      temperature: r.temperature,
      humidity: r.humidity,
      tempState: r.tempState,
      humidityState: r.humidityState,
      summary: r.summary,
      timeText: r.timeText
    };
  }

  /* 最近 n 条，供「查看历史」语音播报用 */
  function getRecentRecords(n) {
    return records.slice(0, n).map(function (r) {
      return {
        temperature: r.temperature,
        humidity: r.humidity,
        summary: r.summary,
        timeText: r.timeText
      };
    });
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
      summary: buildSummary(tempState, humidityState),
      /* 媒体文件名索引，拍照/录像/录音后回填，导出时跟着一起写进 CSV */
      photoName: '',
      videoName: '',
      audioName: ''
    };

    records.unshift(record);
    if (records.length > MAX_RECORDS) {
      records.length = MAX_RECORDS;
    }

    renderResult(record);
    renderHistory();

    /* 校验通过才同步后端 —— 校验前就发请求的话，非法输入也会被记进 CSV */
    postRecordToBackend(record);
  }

  /* 清空 = 真清空：列表清零，下一条从 #1 重新数，之前的数据不再回来（刷新也不回来）。
     实现上是把清零点记下来（clearedBefore），之后后端来的行只要不晚于这个点就一律不认。
     注意这仍然是「页面上不显示」，不是「删数据」：
     后端没有删除接口，CSV 和 data/media 里的文件一个字都没动，
     需要旧数据时把后端 CSV 直接打开就能看到。
     输入框刻意保留：用户要的是「温湿度下面自动保留最后一次输入」，
     清完历史多半是要接着录下一条，把刚填好的数字抹掉只会逼他重敲一遍。 */
  function handleReset() {
    clearErrors();
    clearedBefore = formatTime(new Date());
    records = [];
    serial = 0;
    renderHistory();  /* 存档挂在里面，清零点跟着一起落盘 */
    showRejected('已清空，下一条记录从 #1 开始。请输入温度与湿度后点击「分析」。');
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

    /* 边敲边存。只靠 renderHistory 存档的话，敲完没点分析就刷新（Live Server 会自动刷新）
       这几个字就白敲了 —— 用户要的正是「输入框自动保留上一次的温湿度」 */
    persist();
  }

  els.form.addEventListener('submit', handleSubmit);
  els.resetBtn.addEventListener('click', handleReset);
  els.exportBtn.addEventListener('click', exportCsv);
  els.temperature.addEventListener('input', handleInput);
  els.humidity.addEventListener('input', handleInput);

  /* 摄像头和语音模块在各自的 IIFE 里，只能看见 DOM，够不到这里的 records/exportCsv。
     挂一个最小接口出去，比把三个闭包合成一个的大改动划算得多 */
  window.DormMate = {
    exportCsv: exportCsv,
    clearAll: handleReset,
    getCurrentReading: getCurrentReading,
    getRecentRecords: getRecentRecords,
    getApiBase: function () { return apiBase; },
    getServerStartedAt: function () { return serverStartedAt; },
    uploadMedia: uploadMedia,
    getCsvText: buildCsv,
    showNotice: showNotice
  };

  /* 先恢复本地存档，再拉后端。顺序不能反：initialize 之后 records 里已经有东西了，
     loadBackendHistory 回来时做的是合并不是覆盖（见那里的注释），不会把刚恢复的顶掉。
     反过来先拉后端的话，后端连不上就没得恢复，用户看到的就是一片空白 */
  restore();
  renderHistory();
  /* 页面打开就把后端历史拉回来：清空之后刷新能全部恢复，靠的就是这一步 */
  loadBackendHistory();
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
  var recordBtn = document.getElementById('camera-record-btn');
  var discardBtn = document.getElementById('camera-discard-btn');
  var keepBtn = document.getElementById('camera-keep-btn');
  var dropBtn = document.getElementById('camera-drop-btn');
  var statusEl = document.getElementById('camera-status');
  var metaEl = document.getElementById('camera-meta');

  if (!video || !canvas || !previewBtn || !snapBtn || !statusEl) {
    return;
  }

  var stream = null;
  /* 不选设备，交给浏览器挑默认的那个（桌面就一个摄像头，手机默认给后置）。
     拿到流之后再读轨道上报的朝向，只为了决定要不要做左右镜像 */
  var facingMode = 'environment';
  var recorder = null;
  var chunks = [];
  var recordTimer = null;
  /* 这段录像结束的原因：'' 正常结束（上传）/ 'discard' 用户放弃 / 'error' 出错。
     收尾统一在 onstop 里做，这里只记原因 —— 否则 onerror 和 onstop 会各扫一遍地，
     后跑的那个会把前一个的提示语覆盖掉 */
  var endReason = '';

  /* 录像时长上限。定 10 秒是产品和存储两头妥协的结果：
     再长单个文件就上几十 MB，而 data/media 是要整体复制的 */
  var MAX_RECORD_MS = 10000;

  /* 浏览器对录像容器的支持不统一，Chrome 认 webm，Safari 只认 mp4。
     按优先级挑第一个能用的，都挑不出来就别开录（硬着头皮录会得到 0 字节文件） */
  var VIDEO_MIMES = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm', 'video/mp4'];

  function pickVideoMime() {
    if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
    for (var i = 0; i < VIDEO_MIMES.length; i++) {
      if (MediaRecorder.isTypeSupported(VIDEO_MIMES[i])) return VIDEO_MIMES[i];
    }
    return '';
  }

  /* 取当前读数。没有分析记录就拦下来 —— CSV 那行必须有温湿度，
     否则拍下来的东西只是一堆没有环境上下文的文件 */
  function requireReading() {
    var reading = window.DormMate && window.DormMate.getCurrentReading
      ? window.DormMate.getCurrentReading()
      : null;

    if (!reading) {
      setStatus('请先在上方「数据录入」完成一次温湿度分析，再拍照或录像。', true);
      return null;
    }
    return reading;
  }

  /* 上传回调统一收口：三个入口（拍照/录像/录音）的提示语只有名词不同 */
  function uploadCallbacks(label) {
    return {
      onSuccess: function (json) {
        setStatus(label + '已上传并记入历史：' + json.filename);
      },
      onError: function (err) {
        setStatus(err, true);
      }
    };
  }

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

  /* 轨道上真实生效的 facingMode。读不到就返回空串，交给调用方保留原值 */
  function readFacingMode(ms) {
    try {
      var track = ms.getVideoTracks && ms.getVideoTracks()[0];
      var settings = track && track.getSettings ? track.getSettings() : null;
      return (settings && settings.facingMode) ? String(settings.facingMode) : '';
    } catch (err) {
      return '';
    }
  }

  /* 前置画面按视频通话的习惯做左右镜像：不镜像的话，人往左动画面往右走，看着别扭。
     后置不镜像 —— 镜像后置会让画面里的文字全反着显示。
     朝向只认轨道自己报的 facingMode：浏览器不报（桌面外接摄像头常见）就不镜像。
     不按设备顺序猜 —— 猜错的代价是把一个朝着房间的摄像头也镜像了，画面里的字全反 */
  function applyMirror() {
    video.style.transform = facingMode === 'user' ? 'scaleX(-1)' : '';
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

    /* 不指定设备，让浏览器给默认的那个。
       用 ideal 而不是 exact：exact 在设备不匹配时直接失败（OverconstrainedError），
       连预览都开不起来 */
    navigator.mediaDevices
      .getUserMedia({
        video: {
          width: { ideal: 1280 },
          height: { ideal: 720 },
          facingMode: { ideal: facingMode }
        },
        audio: false
      })
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

        /* 以轨道上报的实际朝向为准，只用来决定要不要镜像 */
        var actual = readFacingMode(ms);
        if (actual) facingMode = actual;
        applyMirror();

        snapBtn.disabled = false;
        if (recordBtn) recordBtn.disabled = false;

        setStatus('预览已开启，点击「抓拍快照」或「开始录像」。');
      })
      .catch(function (err) {
        setStatus(mapError(err), true);
      });
  }

  /* ---------------- 抓拍：先定格，确认后才入历史 ---------------- */

  /* 待确认的快照：{ blob, name, reading }。非空时画面处于定格状态，
     摄像头相关的按钮全部让位给「保存快照 / 放弃快照」 */
  var pendingSnap = null;

  function clearPendingSnap() {
    pendingSnap = null;
    /* 上下两个框一直都在，所以这里不是把画布藏起来，而是把里面的图擦掉 ——
       框还留着，只是回到「空的定格位」。上面的实时预览从头到尾没动过 */
    try {
      var ctx = canvas.getContext('2d');
      if (ctx && canvas.width) ctx.clearRect(0, 0, canvas.width, canvas.height);
    } catch (err) { /* 擦不掉也不影响流程，下次抓拍会整块重画 */ }

    if (keepBtn) keepBtn.hidden = true;
    if (dropBtn) dropBtn.hidden = true;
    if (metaEl) metaEl.hidden = true;

    /* 回到实时画面，按钮恢复可点（没流的时候保持禁用） */
    if (stream) {
      snapBtn.disabled = false;
      if (recordBtn) recordBtn.disabled = false;
    }
  }

  function onDropSnap() {
    if (!pendingSnap) return;
    clearPendingSnap();
    setStatus('已放弃这张快照，未上传、未写入历史。画面回到实时预览。');
  }

  function onKeepSnap() {
    if (!pendingSnap) return;

    if (!window.DormMate || !window.DormMate.uploadMedia) {
      setStatus('上传模块未就绪，图片未保存到后端。', true);
      return;
    }

    var snap = pendingSnap;
    if (keepBtn) keepBtn.disabled = true;
    if (dropBtn) dropBtn.disabled = true;
    setStatus('正在上传快照…');

    var cb = uploadCallbacks('快照');
    window.DormMate.uploadMedia(snap.blob, snap.name, 'photo', snap.reading,
      function (json) {
        clearPendingSnap();
        if (keepBtn) keepBtn.disabled = false;
        if (dropBtn) dropBtn.disabled = false;
        cb.onSuccess(json);
      },
      function (err) {
        /* 上传失败就把这张留着：定格还在，用户点「保存快照」能重试。
           直接丢弃的话，网络抖一下这张就白拍了 */
        if (keepBtn) keepBtn.disabled = false;
        if (dropBtn) dropBtn.disabled = false;
        setStatus(err + '（画面仍是定格的，可重试「保存快照」）', true);
      });
  }

  function onSnap() {
    if (!stream || video.readyState < 2 || !video.videoWidth) {
      setStatus('请先开启预览，等画面出现后再抓拍快照。', true);
      return;
    }
    if (pendingSnap) {
      setStatus('已经有一张待确认的快照，请先「保存快照」或「放弃快照」。', true);
      return;
    }

    /* 先要读数再画布：没有分析记录时直接拦下 ——
       CSV 那行要带温湿度，拍完再说「没有读数」等于白拍 */
    var reading = requireReading();
    if (!reading) return;

    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);

    /* 上下两个框都留着：上面 video 是实时预览，下面 canvas 是这张定格图。
       流不关 —— 放弃时把定格图擦掉就行，不用重新取一次流 */

    if (metaEl) {
      metaEl.hidden = false;
      metaEl.textContent = '抓拍时间：' + formatTime(new Date()) +
        '　分辨率：' + video.videoWidth + ' × ' + video.videoHeight;
    }

    /* 编码放在定格这一刻做，确认时直接上传，不用等 */
    var stamp = Date.now();
    canvas.toBlob(function (blob) {
      if (!blob) {
        /* 编码失败就退回实时画面，别把用户卡在一个永远存不下来的定格上 */
        clearPendingSnap();
        setStatus('抓拍图片编码失败，请重试。', true);
        return;
      }

      pendingSnap = { blob: blob, name: 'snap_' + stamp + '.jpg', reading: reading };

      if (keepBtn) { keepBtn.hidden = false; keepBtn.disabled = false; }
      if (dropBtn) { dropBtn.hidden = false; dropBtn.disabled = false; }
      /* 定格期间不许再抓、不许录 —— 画面已经不是实时的了 */
      snapBtn.disabled = true;
      if (recordBtn) recordBtn.disabled = true;

      setStatus('画面已定格。点「保存快照」记入历史并上传，或「放弃快照」回到实时画面。');
    }, 'image/jpeg', 0.92);
  }

  /* ---------------- 录像 ---------------- */

  /* 录像按钮态：录制中显示「停止录像」+ 红色，同时放开「放弃录像」。
     放弃按钮只在录制中可用 —— 没在录的时候它没有意义 */
  function setRecordingUI(on) {
    if (recordBtn) {
      recordBtn.textContent = on ? '停止录像' : '开始录像';
      if (on) {
        recordBtn.classList.add('is-recording');
      } else {
        recordBtn.classList.remove('is-recording');
      }
    }
    if (discardBtn) discardBtn.disabled = !on;
  }

  /* 录像收尾。reason 见 endReason 的说明，默认空串＝正常结束并上传 */
  function stopRecording(reason) {
    endReason = reason || '';
    if (recordTimer) {
      clearTimeout(recordTimer);
      recordTimer = null;
    }
    /* stop() 会触发 onstop，收尾和上传全在那边，这里只负责停下来 */
    if (recorder && recorder.state !== 'inactive') {
      recorder.stop();
    }
  }

  /* 放弃：录到一半反悔了。走的是和正常停止同一条路，只是 onstop 里不上传 */
  function onDiscard() {
    if (!recorder || recorder.state !== 'recording') {
      setStatus('当前没有正在进行的录像。', true);
      return;
    }
    stopRecording('discard');
  }

  function onRecordToggle() {
    if (recorder && recorder.state === 'recording') {
      stopRecording('');
      return;
    }

    if (pendingSnap) {
      setStatus('有还没确认的快照，请先「保存快照」或「放弃快照」再录像。', true);
      return;
    }
    if (!stream || video.readyState < 2) {
      setStatus('请先开启预览，等画面出现后再录像。', true);
      return;
    }

    var reading = requireReading();
    if (!reading) return;

    if (typeof MediaRecorder === 'undefined') {
      setStatus('当前浏览器不支持录像（MediaRecorder 不可用）。', true);
      return;
    }
    if (!window.DormMate || !window.DormMate.uploadMedia) {
      setStatus('上传模块未就绪，无法保存录像。', true);
      return;
    }

    var mime = pickVideoMime();
    if (!mime) {
      setStatus('当前浏览器没有可用的录像格式，无法录制。', true);
      return;
    }

    chunks = [];
    try {
      recorder = new MediaRecorder(stream, { mimeType: mime });
    } catch (err) {
      setStatus('录像启动失败：' + ((err && err.message) || '未知错误'), true);
      return;
    }

    recorder.ondataavailable = function (event) {
      if (event.data && event.data.size > 0) chunks.push(event.data);
    };

    recorder.onstop = function () {
      if (recordTimer) {
        clearTimeout(recordTimer);
        recordTimer = null;
      }
      setRecordingUI(false);

      var blob = new Blob(chunks, { type: mime });
      chunks = [];

      var reason = endReason;
      endReason = '';
      recorder = null;

      /* 退出原因优先于内容判断：先看用户是不是放弃了、是不是出错了，再看有没有内容。
         丢弃只发生在这里 —— 后端没有删除接口，一旦上传就撤不回来了 */
      if (reason === 'discard') {
        setStatus('已放弃这段录像，未上传、未写入 CSV。');
        return;
      }
      if (reason === 'error') {
        setStatus('录像过程中出错，这段没有上传。', true);
        return;
      }

      if (!blob.size) {
        setStatus('录像内容为空（可能刚开录就停了），未上传。', true);
        return;
      }

      /* 扩展名跟着实际 mime 走：后端白名单只认 mp4/webm，
         写成 .webm 却塞 mp4 内容的话浏览器回放时解不出来 */
      var ext = mime.indexOf('mp4') >= 0 ? 'mp4' : 'webm';
      var cb = uploadCallbacks('录像');
      setStatus('正在上传录像（' + Math.round(blob.size / 1024) + ' KB）…');
      window.DormMate.uploadMedia(blob, 'clip_' + Date.now() + '.' + ext, 'video', reading,
        cb.onSuccess, cb.onError);
    };

    recorder.onerror = function () {
      /* 只记原因，收尾统一交给 onstop —— 按规范出错后引擎也会再发一次 stop，
         两边都收尾的话后跑的那个会把前一个的提示语盖掉 */
      endReason = 'error';
      stopRecording('error');   /* 出错的那段别上传，留着也是坏文件 */
    };

    endReason = '';
    recorder.start();
    setRecordingUI(true);
    setStatus('录像中…最长 ' + (MAX_RECORD_MS / 1000) + ' 秒，到时会自动停止并上传；' +
      '不想要这段就点「放弃录像」。');

    recordTimer = setTimeout(function () {
      if (recorder && recorder.state === 'recording') {
        setStatus('已录满 ' + (MAX_RECORD_MS / 1000) + ' 秒，正在上传…');
        stopRecording(false);
      }
    }, MAX_RECORD_MS);
  }

  previewBtn.addEventListener('click', onPreview);
  snapBtn.addEventListener('click', onSnap);
  if (recordBtn) recordBtn.addEventListener('click', onRecordToggle);
  if (discardBtn) discardBtn.addEventListener('click', onDiscard);
  if (keepBtn) keepBtn.addEventListener('click', onKeepSnap);
  if (dropBtn) dropBtn.addEventListener('click', onDropSnap);
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
  var recordBtn = document.getElementById('voice-record-btn');
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
    clearSpeakWatchdog();
    /* 念完了 = 已经送到耳朵里，那句开场简报不用再补了。
       （不靠 onstart 一个信号：有的浏览器 onstart 会迟到甚至不来，onend 到了
       就说明这句真的走完了，比 onstart 更硬。）cancel 掉的那次上面已经拦住了，
       不会走到这儿把没念成的当成念成了 */
    if (!event || event.type !== 'error') {
      openingBrief = null;
    }
    /* onerror 是另一条「没出声」的路：onstart 来过了（浏览器没拦），
       但系统里没有能用的中文音色 / 语音服务挂了 —— 引擎会回过来说一声。
       不写出来的话，用户听到的仍然是「一片安静」 */
    if (event && event.type === 'error') {
      var code = event.error || '';
      setStatus('语音没能播出来（' + (code || '引擎报错') +
                '）：浏览器或系统缺少可用的中文语音。', true);
    }
  }

  function stopSpeaking() {
    /* speechSynthesis.cancel() 不触发 onend，speaking 必须手动清 */
    speaking = false;
    currentUtterance = null;
    // 主动取消不等于被浏览器拦下：不撤掉兜底计时器的话，
    // 1.5 秒后会凭一个已经作废的 utterance 报「语音没能播出来」
    clearSpeakWatchdog();
    try {
      if (win.speechSynthesis) {
        win.speechSynthesis.cancel();
      }
    } catch (err) { /* 忽略 */ }
  }

  /* 「调用过 speak()」和「真的出声了」是两件事。
     浏览器拦下自动播放（页面从没被点过）、系统没装中文音色、语音服务不可用，
     这几种情况下 speak() 不抛错、也不出声 —— 唯一的迹象是 onstart 一直不来。
     以前这里完全静默，用户只能靠「有没有听见」猜，把功能已生效和功能没生效
     混成同一个现象。现在给它一个兜底：等不到 onstart 就说清楚为什么，
     并且把这一条留着，等页面被点过之后自动补播 */
  var SPEECH_WATCHDOG_MS = 1500;
  var speakWatchdog = null;
  var speechStarted = false;

  function clearSpeakWatchdog() {
    if (speakWatchdog) {
      clearTimeout(speakWatchdog);
      speakWatchdog = null;
    }
  }

  function onSpeechBlocked() {
    speaking = false;          /* 没出声就不能一直占着「正在播」的位子，否则语音识别被永久抑制 */
    currentUtterance = null;
    /* 关键：把那一份从引擎的播放队列里撤掉。
       被拦下不等于被丢弃 —— Chrome 把它放在队列里攒着，等页面被点过（允许
       发声了）再一起念出来。重试几次就攒几份，用户点一下会听见同一句话念
       好几遍。这里撤掉，队列里永远只剩「当前这一次」 */
    try {
      if (win.speechSynthesis) {
        win.speechSynthesis.cancel();
      }
    } catch (err) { /* 忽略 */ }
    var ua = win.navigator && win.navigator.userActivation;
    if (ua && ua.hasBeenActive === false) {
      setStatus('语音没能播出来：浏览器要求页面先有过一次点击，才允许自动发声 —— ' +
                '在本页任意位置点一下，这条提醒会自动补播。', true);
    } else {
      setStatus('语音没能播出来：浏览器或系统没有可用的中文语音。点一下本页可以再试一次。', true);
    }
  }

  /* onStart 是可选的：自动提醒靠它判断「真的播出去了没有」，
     播出去了才把这条标记成已提醒，否则下一轮还会补 —— 见 pollAlert */
  function speak(text, onStart) {
    if (!win.speechSynthesis || !win.SpeechSynthesisUtterance) {
      setStatus('这台浏览器没有语音播报能力（speechSynthesis 缺失），只能看文字。', true);
      return;
    }
    if (speaking) {
      stopSpeaking();   /* 后说的覆盖先说的 */
    }
    var utterance = new win.SpeechSynthesisUtterance(text);
    /* 只设 lang，中文声音交给浏览器挑：页面刚加载时 getVoices() 通常还是空数组 */
    utterance.lang = 'zh-CN';
    utterance.onstart = function () {
      /* 上一次尝试被中止后，它的 onstart 仍可能迟到 —— 那声不是这一次的，
         算到这一次头上就会把「已提醒」标记错、状态栏也跟着说错话 */
      if (currentUtterance !== utterance) return;
      speechStarted = true;
      clearSpeakWatchdog();
      if (onStart) onStart();
    };
    utterance.onend = handleSpeechEnd;
    utterance.onerror = handleSpeechEnd;
    currentUtterance = utterance;
    speaking = true;
    speechStarted = false;
    clearSpeakWatchdog();
    speakWatchdog = setTimeout(function () {
      speakWatchdog = null;
      if (!speechStarted) onSpeechBlocked();
    }, SPEECH_WATCHDOG_MS);
    try {
      win.speechSynthesis.speak(utterance);
    } catch (err) {
      clearSpeakWatchdog();
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

  /* ---------------- 「朗读提醒」话术（B4） ---------------- */

  /* Dashboard 看板快照的键。和 dashboard/index.html 的 STORE_KEY、3d 的 DASH_KEY_3D
     是同一份：语音要念的是「系统里现在最要紧的那件事」，不是本页手动敲进去的那条
     读数（那是上面「朗读状态」的活）。
     它要求 web 和 dashboard 同源打开（localStorage 才共享），3d 早就依赖同一前提；
     从没打开过 Dashboard 就没有这份数据，只能照实说一句 */
  var DASH_STORE_KEY = 'dormmate.dashboard.v1';

  /* 结论的保留期，必须和 dashboard 的 VERDICT_HOLD_MS 一致：刚判完「已恢复」的
     那几分钟里，看板横幅和这里说的得是同一件事。改那边记得回来改这一行 ——
     这个常量属于看板的展示规则，不属于 A1 判定内核，所以没往 priority.js 里放 */
  var VERDICT_HOLD_MS = 5 * 60 * 1000;

  /* 从快照的 a1 字段重建 tracker。它的形状就是 a1Tracker（每节点
     {abnormalSince,count,kind}）—— 这三个字段正是 pick 读取的全部，
     所以可以直接摆出条目，不必走 feed（feed 会把起点改成「现在」）。
     localStorage 是谁都能改的地方，逐项验过再喂给 pick：脏数据会算出
     NaN 时长，播出来就是「已持续 NaN 分钟」 */
  function readDashTracker(snapshot) {
    var a1 = win.DormMateA1;
    var tracker = a1.createTracker();
    var saved = snapshot.a1;
    if (!saved || typeof saved !== 'object') return tracker;
    a1.NODE_ORDER.forEach(function (node) {
      var e = saved[node];
      if (!e || typeof e !== 'object') return;
      var since = (typeof e.abnormalSince === 'number' && isFinite(e.abnormalSince) && e.abnormalSince > 0)
        ? e.abnormalSince : null;
      var kind = (typeof e.kind === 'string' && a1.ALERT_TEXT[e.kind]) ? e.kind : null;
      if (since === null || kind === null) return;   // 正常段：不进候选集
      var count = (typeof e.count === 'number' && isFinite(e.count)) ? Math.floor(e.count) : 1;
      tracker[node] = { abnormalSince: since, count: Math.max(1, count), kind: kind };
    });
    return tracker;
  }

  /* 看板那边的状态词是「处理中｜风扇已开启 · …」，语音只要头三个字 */
  function alertStateWord(state) {
    if (state === 'handling') return '处理中';
    if (state === 'recovered') return '已恢复';
    if (state === 'attention') return '仍需关注';
    return '异常中';
  }

  /* 快照里找「刚处置完、结论还在保留期内」的宿舍。A1.pick 只认还在异常段里的
     节点，判完「已恢复」它就落选了 —— 而这几分钟恰恰是用户最想听一句结果的时候 */
  function recentVerdict(snapshot) {
    var fsm = snapshot.fsm;
    if (!fsm || typeof fsm !== 'object') return null;
    var found = null;
    win.DormMateA1.NODE_ORDER.forEach(function (node) {
      var f = fsm[node];
      if (!f || typeof f !== 'object') return;
      if (f.state !== 'recovered' && f.state !== 'attention') return;
      var at = Number(f.concludedAt) || 0;
      if (Date.now() - at >= VERDICT_HOLD_MS) return;   // 过期结论不算「当前提醒」
      if (!found || at > (Number(fsm[found].concludedAt) || 0)) found = node;
    });
    return found;
  }

  /* 完整话术。要说出口的句子一律避开「提醒」二字 —— TTS 的尾音被麦克风收回去
     会再触发一次指令（「清空历史」的回话避原词是同一个先例）；
     setStatus 是给人看的，可以出现原词 */
  function buildAlertSpeech() {
    var a1 = win ? win.DormMateA1 : null;
    if (!a1) return '判定内核未加载，无法朗读。';
    var raw = null;
    try { raw = win.localStorage.getItem(DASH_STORE_KEY); } catch (err) { return '读取看板数据失败。'; }
    if (!raw) return '看板数据不存在，请先打开 Dashboard 页面。';
    var snapshot = null;
    try { snapshot = JSON.parse(raw); } catch (err) { return '看板数据已损坏。'; }
    if (!snapshot || typeof snapshot !== 'object') return '看板数据已损坏。';

    var store = (snapshot.store && typeof snapshot.store === 'object') ? snapshot.store : {};
    var result = a1.pick(readDashTracker(snapshot), Date.now());
    var node = result.node || recentVerdict(snapshot);
    if (!node) return '当前没有需要关注的事项。';

    var fsm = snapshot.fsm && snapshot.fsm[node];
    var word = result.node
      ? alertStateWord(fsm && fsm.state)
      : ((fsm && fsm.state === 'recovered') ? '已恢复' : '仍需关注');

    /* 趋势看哪一维由异常类型决定。还在异常段里就用它当前的类型；
       已经判完的那一段，用上一帧读数反推当时是什么异常 ——
       A1.kindOf 用的是同一套阈值，不用在这里另抄一份 18/30/75 */
    var cur = store[node];
    var prev = (cur && typeof cur === 'object') ? cur.prev : null;
    var kind = result.node ? result.kind : (prev ? a1.kindOf(prev.temperature, prev.humidity) : null);
    var trend = a1.trendText(kind, prev, cur);
    return node + '，' + word + (trend ? '，' + trend : '') + '。';
  }

  function readAlert() {
    var text = buildAlertSpeech();
    setStatus('已朗读提醒：' + text);
    speak(text);
  }

  /* ---------------- B4：语音提醒（不等用户问，自己播） ----------------
     上面那个「朗读提醒」是用户主动问一句答一句，这一节是反过来：看板那边
     新出现一段异常、或者刚下了处置结论，这边主动念出来 —— 「语音提醒」这
     四个字要能兑现成真的会响，而不是等人来查。

     数据来源和「朗读提醒」完全一致：轮询看板写的 localStorage 快照。不新开
     MQTT 连接 —— 那会变成第三个客户端，还要在这页再养一份判定状态，
     迟早和看板说的不一致。代价是要求两页同源且看板开着（已写进 README 已知限制）。

     每 2.5 秒看一眼快照，只在**第一次能看到快照时**念一遍当前情况，攒成一句：
       当前有 3 处需要关注：dorm-a 高温、dorm-b 高温、dorm-c 低温偏湿。
     念完就不再主动出声 —— 之后再来多少条报文、再出多少结论都不念。
     想听就问一句「朗读提醒」，那是用户主动要的，随时答。

     早先的版本是「只播报变化」：页面打开时不念（怕一进页面就开口吓人），
     之后每出现一段新异常或结论才念。问题是这样一进页面根本没声音 —— 三个
     宿舍都在异常、状态栏也照实写着，页面却全程静音，用户只能得出「这功能
     没生效」；异常一直不结束的话，永远等不到「变化」，就永远不会开口。
     一遍定音的代价是「处置结论出来了也不吭声」，用户已知悉并选了这个。 */
  var ALERT_POLL_MS = 2500;
  var briefDone = false;   // 这次打开已经报过了（没东西可报也算报过），从此不再出声
  var openingBrief = null; // 还没念出去的那一句；念不出去就一直留着，等用户点页面补播
  var briefTried = false;  // 定时轮询已经试过一回了，别攒队列

  function currentAlerts(snapshot) {
    var a1 = win.DormMateA1;
    var out = [];
    var saved = snapshot.a1;
    if (saved && typeof saved === 'object') {
      a1.NODE_ORDER.forEach(function (node) {
        var e = saved[node];
        if (!e || typeof e !== 'object') return;
        var since = Number(e.abnormalSince);
        if (!isFinite(since) || since <= 0) return;
        var kind = (typeof e.kind === 'string' && a1.ALERT_TEXT[e.kind]) ? e.kind : null;
        var label = a1.typeText(kind) || '异常';
        out.push({
          short: node + ' ' + label,          // 攒成一句念的时候用
          text: node + '，' + label + '，需要关注。'
        });
      });
    }
    var fsm = snapshot.fsm;
    if (fsm && typeof fsm === 'object') {
      a1.NODE_ORDER.forEach(function (node) {
        var f = fsm[node];
        if (!f || typeof f !== 'object') return;
        if (f.state !== 'recovered' && f.state !== 'attention') return;
        var at = Number(f.concludedAt) || 0;
        if (!at) return;
        // 保留期和「朗读提醒」同一把尺子：过了期的结论不再是「当前提醒」，
        // 半路上打开页面不该被一条十分钟前的旧结论提醒一次
        if (Date.now() - at >= VERDICT_HOLD_MS) return;
        // 结论带上趋势，用户能听出数据在往哪边走（温度正在下降 / 湿度正在上升）
        var cur = snapshot.store && snapshot.store[node];
        var prev = (cur && typeof cur === 'object') ? cur.prev : null;
        var kind = prev ? a1.kindOf(prev.temperature, prev.humidity) : null;
        var trend = a1.trendText(kind, prev, cur);
        var word = f.state === 'recovered' ? '已恢复' : '仍需关注';
        out.push({
          short: node + ' ' + word,
          text: node + '，' + word + (trend ? '，' + trend : '') + '。'
        });
      });
    }
    return out;
  }

  /* fromGesture=true 表示这一次是用户点页面触发的（浏览器刚给了发声许可，
     见下面的 click 监听），只有这种时候才允许重试那一句没念出去的简报 */
  function pollAlert(fromGesture) {
    var a1 = win ? win.DormMateA1 : null;
    if (!a1 || !win.localStorage) return;
    var raw = null;
    try { raw = win.localStorage.getItem(DASH_STORE_KEY); } catch (err) { return; }
    if (!raw) return;
    var snapshot = null;
    try { snapshot = JSON.parse(raw); } catch (err) { return; }
    if (!snapshot || typeof snapshot !== 'object') return;

    /* 这一句攒好之后就一直留着，直到真的念出去为止。
       留着是必要的：被浏览器拦下（页面还没被点过）时它念不出去，
       要等用户点页面的那一下 —— 那正是浏览器开始允许发声的时刻 */
    if (!openingBrief) {
      if (briefDone) return;   // 这次打开已经报过了，之后不再出声
      briefDone = true;
      var list = currentAlerts(snapshot);
      if (list.length) {
        // 攒成一句：三条逐条念要念三遍，听的人等不起
        openingBrief = '当前有 ' + list.length + ' 处需要关注：' +
          list.map(function (a) { return a.short; }).join('、') + '。';
        setStatus('语音提醒：' + openingBrief);
      }
    }
    if (!openingBrief) return;

    /* 定时轮询只试一次，之后老老实实等用户点页面 —— 不这么办的话，没被点过的
       页面上每 2.5 秒就重试一次，而重试并不等于重放：引擎会把每次尝试都排进
       队列攒着，用户点一下听见的是同一句话念好几遍（真机上就是这样） */
    if (briefTried && !fromGesture) return;
    briefTried = true;

    /* 上一句「真的出声了」才让位：speak 是「后说的覆盖先说的」，硬插队会把
       正在念的一句掐断。但只出声一半不算数 —— 被浏览器拦下的那一次，speaking
       也是 true，要等 1.5 秒兜底计时器才松开。这期间用户点了页面（浏览器从此
       允许发声），若把「没出声的那一次」也当成占线，补播就会被挡在门外，
       点多少下都没反应 */
    if (speaking && speechStarted) return;
    /* 清掉 openingBrief 的时机是 onstart，不是这里 —— 提前清掉的话，被拦下
       就再也不会补播了；状态栏的同一个道理：先写成「语音提醒：…」，会盖掉
       上一条「没能播出来」的原因，用户看到的又是「说过了」，而什么都没听见 */
    var brief = openingBrief;
    speak(brief, function () {
      openingBrief = null;
      // 说出口的句子一律避开「提醒」二字：TTS 尾音被麦克风收回会再触发一次指令
      setStatus('语音提醒：' + brief);
    });
  }

  /* ---------------- 「分析环境」话术 ---------------- */

  /* 数字口语化：16 就说 16，16.5 说 16.5 */
  function numText(value) {
    var n = Number(value);
    return Number.isInteger(n) ? String(n) : n.toFixed(1);
  }

  /* 环境分析话术。偏冷、偏热、偏湿三种单异常的输出是用户逐字定下的，别改写：
       16/60 → 当前温度16℃，湿度60%，环境偏冷，建议增添衣物，关闭窗户减少冷风进入。
       31/60 → 当前温度31℃，湿度60%，环境偏热，建议开窗通风，适当使用风扇。
       25/80 → 当前温度25℃，湿度80%，环境偏湿，建议开启除湿设备。
     复合状态（偏冷+偏湿、偏热+偏湿）范例里没有，按同一句式拼出来。
     注意：这段文本直接交给 TTS，不走 speakable() —— 用户要的就是这个逐字输出。 */
  function buildEnvSpeech(t, h) {
    var head = '当前温度' + numText(t) + '℃，湿度' + numText(h) + '%，环境';

    if (t < 18 && h >= 75) {
      return head + '偏冷偏湿，建议增添衣物、关闭窗户减少冷风进入，同时开启除湿设备。';
    }
    if (t >= 30 && h >= 75) {
      return head + '偏热偏湿，建议开窗通风、适当使用风扇，同时开启除湿设备。';
    }
    if (t < 18) {
      return head + '偏冷，建议增添衣物，关闭窗户减少冷风进入。';
    }
    if (t >= 30) {
      return head + '偏热，建议开窗通风，适当使用风扇。';
    }
    if (h >= 75) {
      return head + '偏湿，建议开启除湿设备。';
    }
    return head + '舒适，温湿度均正常。';
  }

  function readEnvironment() {
    var bridge = win.DormMate;
    var reading = bridge && bridge.getCurrentReading ? bridge.getCurrentReading() : null;

    if (!reading) {
      setStatus('暂无分析记录，请先输入温湿度并点击「分析」。');
      speak('暂无分析记录，请先输入温湿度并点击分析。');
      return;
    }

    var text = buildEnvSpeech(reading.temperature, reading.humidity);
    setStatus(text);
    speak(text);
  }

  /* ---------------- 其余语音指令 ---------------- */

  /* 清空只清前端。回话里刻意不含「清空历史」四个字 ——
     TTS 的声音会被麦克风收回，含指令原词就会自己触发自己 */
  function clearHistory() {
    var bridge = win.DormMate;
    if (!bridge || !bridge.clearAll) {
      setStatus('清空失败：页面模块未就绪。', true);
      return;
    }

    bridge.clearAll();
    var text = '页面记录已清空，后端数据不受影响，刷新页面就能恢复。';
    setStatus(text);
    speak(text);
  }

  function readHistory() {
    var bridge = win.DormMate;
    var recent = bridge && bridge.getRecentRecords ? bridge.getRecentRecords(3) : [];

    if (!recent.length) {
      setStatus('暂无历史记录。');
      speak('暂无历史记录。');
      return;
    }

    var parts = recent.map(function (r, i) {
      return '第' + (i + 1) + '条，' + r.timeText +
        '，温度' + numText(r.temperature) + '摄氏度，湿度百分之' + numText(r.humidity) +
        '，状态' + r.summary;
    });
    var text = '最近' + recent.length + '条记录：' + parts.join('；') + '。';
    setStatus(text);
    speak(text);
  }

  function exportCsvByVoice() {
    var bridge = win.DormMate;
    if (!bridge || !bridge.exportCsv) {
      setStatus('导出失败：页面模块未就绪。', true);
      return;
    }

    var recent = bridge.getRecentRecords(1);
    if (!recent.length) {
      setStatus('暂无记录，无法导出。');
      speak('暂无记录，无法导出。');
      return;
    }

    bridge.exportCsv();
    /* 回话同样避开「导出csv」原词 */
    var text = '表格已导出。完整保存图片、视频和录音，还需同时复制媒体文件夹。';
    setStatus('已触发导出，浏览器会下载 dormmate.csv。' + text);
    speak(text);
  }

  /* ---------------- 录音（独立于语音识别） ---------------- */

  var audioRecorder = null;
  var audioChunks = [];
  var audioStream = null;
  var recording = false;

  var AUDIO_MIMES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/mpeg'];

  function pickAudioMime() {
    if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
    for (var i = 0; i < AUDIO_MIMES.length; i++) {
      if (MediaRecorder.isTypeSupported(AUDIO_MIMES[i])) return AUDIO_MIMES[i];
    }
    return '';
  }

  function setRecordButton(on) {
    if (!recordBtn) return;
    recordBtn.textContent = on ? '停止录音' : '开始录音';
    if (on) {
      recordBtn.classList.add('is-recording');
    } else {
      recordBtn.classList.remove('is-recording');
    }
  }

  function mapMicError(err) {
    var name = err && err.name;
    if (name === 'NotAllowedError' || name === 'SecurityError') {
      return '麦克风权限被拒绝，请在浏览器地址栏允许访问后重试。';
    }
    if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
      return '未找到麦克风，请确认设备已连接。';
    }
    if (name === 'NotReadableError' || name === 'TrackStartError') {
      return '麦克风被其他程序占用，请关闭占用程序后重试。';
    }
    return '录音失败：' + ((err && err.message) || '未知错误');
  }

  function releaseMic() {
    if (audioStream) {
      audioStream.getTracks().forEach(function (track) { track.stop(); });
      audioStream = null;
    }
  }

  function stopVoiceRecording() {
    if (!recording || !audioRecorder) {
      setStatus('当前没有正在进行的录音。');
      return;
    }
    /* 只负责停，上传全在 onstop 里 */
    if (audioRecorder.state !== 'inactive') {
      audioRecorder.stop();
    }
  }

  function startVoiceRecording() {
    if (recording) {
      setStatus('录音已在进行中。');
      return;
    }

    var bridge = win.DormMate;
    if (!win || !win.navigator || !win.navigator.mediaDevices || !win.navigator.mediaDevices.getUserMedia) {
      setStatus('当前环境不支持录音，请通过 localhost 或 HTTPS 打开本页。', true);
      return;
    }
    if (typeof MediaRecorder === 'undefined') {
      setStatus('当前浏览器不支持录音（MediaRecorder 不可用）。', true);
      return;
    }
    if (!bridge || !bridge.uploadMedia) {
      setStatus('上传模块未就绪，无法录音。', true);
      return;
    }

    var reading = bridge.getCurrentReading ? bridge.getCurrentReading() : null;
    if (!reading) {
      var hint = '请先完成一次温湿度分析，录音才能记入历史。';
      setStatus(hint, true);
      speak(hint);
      return;
    }

    var mime = pickAudioMime();
    if (!mime) {
      setStatus('当前浏览器没有可用的录音格式，无法录制。', true);
      return;
    }

    setStatus('正在请求麦克风权限…');

    win.navigator.mediaDevices.getUserMedia({ audio: true })
      .then(function (ms) {
        audioStream = ms;
        audioChunks = [];
        audioRecorder = new MediaRecorder(ms, { mimeType: mime });

        audioRecorder.ondataavailable = function (event) {
          if (event.data && event.data.size > 0) audioChunks.push(event.data);
        };

        audioRecorder.onstop = function () {
          recording = false;
          setRecordButton(false);
          releaseMic();

          var blob = new Blob(audioChunks, { type: mime });
          audioChunks = [];
          audioRecorder = null;

          if (!blob.size) {
            setStatus('录音内容为空，未上传。', true);
            return;
          }

          /* 扩展名跟实际编码走，后端白名单只认 mp3/aac/webm/m4a */
          var ext = mime.indexOf('mp4') >= 0 ? 'm4a' : (mime.indexOf('mpeg') >= 0 ? 'mp3' : 'webm');
          setStatus('正在上传录音（' + Math.round(blob.size / 1024) + ' KB）…');

          bridge.uploadMedia(blob, 'voice_' + Date.now() + '.' + ext, 'audio', reading,
            function (json) {
              setStatus('录音已上传并记入历史：' + json.filename);
              speak('录音已保存。');
            },
            function (err) { setStatus(err, true); });
        };

        audioRecorder.onerror = function () {
          recording = false;
          setRecordButton(false);
          releaseMic();
          setStatus('录音过程中出错，已中断。', true);
        };

        audioRecorder.start();
        recording = true;
        setRecordButton(true);
        setStatus('录音中…再次点击「停止录音」，或直接说「停止录音」来结束。');
      })
      .catch(function (err) {
        releaseMic();
        setStatus(mapMicError(err), true);
        speak(mapMicError(err));
      });
  }

  function onRecordToggle() {
    if (recording) {
      stopVoiceRecording();
    } else {
      startVoiceRecording();
    }
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
    /* 禁用按钮的 click() 是空操作，必须先查 disabled 才有提示。
       抓拍现在是两步，按钮在「有快照待确认」时也是禁用的，这两种情况要分开说 */
    if (snapBtn.disabled) {
      var blocked = (keepBtn && !keepBtn.hidden)
        ? '已经有一张待确认的快照，请先保存或放弃这一张。'
        : '请先开启摄像头预览，再下达抓拍指令。';
      setStatus(blocked, true);
      speak(blocked);
      return;
    }

    /* 没有分析记录时摄像头那边会拒绝抓拍，这里先拦一道 ——
       否则会播报「已完成抓拍」，而实际什么也没存下来 */
    var bridge = win.DormMate;
    if (!bridge || !bridge.getCurrentReading || !bridge.getCurrentReading()) {
      var hint = '请先完成一次温湿度分析，再下达抓拍指令。';
      setStatus(hint, true);
      speak(hint);
      return;
    }

    snapBtn.click();   /* 复用 M3 阶段1 的抓拍逻辑，不改动相机代码 */
    /* 抓拍只负责定格，入历史要用户在摄像头区域再确认一次。
       回话里不能说「已保存/已上传」——那是确认之后才会发生的事。
       同样避开「拍照」二字，防止被麦克风收回后自己触发自己 */
    var ok = '画面已定格，请在摄像头区域点「保存快照」写入历史，或点「放弃快照」取消。';
    setStatus(ok);
    speak(ok);
  }

  /* 指令表。顺序即匹配优先级：停止录音排在开始录音前面，
     免得「停止录音」这种带否定的说法先撞上别的关键词。
     key 用小写字，匹配前把识别文本也转小写并去掉空格（ASR 常把 csv 写成 CSV、
     还会在英文词周围插空格）。数组里所有 key 必须互不为子串，否则短的那个会抢匹配。 */
  var COMMANDS = [
    { key: '停止录音', run: stopVoiceRecording, label: '停止录音' },
    { key: '开始录音', run: startVoiceRecording, label: '开始录音' },
    /* 拍照排在朗读状态之前：原实现就是「拍照先播提示、朗读最后播」，
       speak() 是 cancel-and-replace，后说的胜出，所以朗读必须排在后面才会成为最终播报 */
    { key: '拍照', run: takeSnapshot, label: '拍照' },
    { key: '朗读状态', run: readStatus, label: '朗读状态' },
    /* B4：读的是看板快照里「当前最值得关注的那件事」，与「朗读状态」
       （读本页手动录入的那条）分工不同。两个命令不互为子串，匹配不会打架 */
    { key: '朗读提醒', run: readAlert, label: '朗读提醒' },
    { key: '分析环境', run: readEnvironment, label: '分析环境' },
    { key: '清空历史', run: clearHistory, label: '清空历史' },
    { key: '导出csv', run: exportCsvByVoice, label: '导出csv' },
    { key: '查看历史', run: readHistory, label: '查看历史' }
  ];

  var COMMAND_HINT = COMMANDS.map(function (c) { return '「' + c.label + '」'; }).join('');

  function handleFinal(text) {
    /* 冷却锁：上一条指令执行后 COMMAND_COOLDOWN_MS 内不再接受新指令。
       这里拦截而不是在 onResult 顶部拦，否则冷却期间连识别文字都不上屏了。 */
    if (isRunningCommand) {
      return;
    }

    var normalized = String(text).toLowerCase().replace(/\s+/g, '');
    var matched = [];
    for (var i = 0; i < COMMANDS.length; i++) {
      if (normalized.indexOf(COMMANDS[i].key) !== -1) {
        matched.push(COMMANDS[i]);
      }
    }

    if (!matched.length) {
      setStatus('未识别到指令，可说' + COMMAND_HINT + '。');
      return;
    }

    isRunningCommand = true;
    /* 用户就在跟前说话，还没念出去的开场简报就此作废：两条播报都在抢同一个
       喇叭，留着它，下一轮轮询会用简报盖掉用户刚要的那句话（cancel-and-replace
       后说的胜出）。用户的指令永远优先于系统自己攒的那句 */
    openingBrief = null;
    /* 一句话里说了多条就都执行。原来「拍照+朗读状态」就是这个行为
       （朗读最后播、cancel-and-replace 让它胜出），改成表格后不能丢 */
    matched.forEach(function (command) {
      command.run();
    });
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
    setStatus('正在监听，可说「朗读状态」「朗读提醒」「分析环境」「拍照」「查看历史」等指令。');
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

  /* 录音按钮在这里就绑上，且必须在下面的 SR 守卫之前：
     录音走的是 MediaRecorder，跟语音识别没关系，
     不该因为插件缺失或识别服务不通而一起废掉 */
  if (recordBtn) {
    recordBtn.addEventListener('click', onRecordToggle);
  }

  /* 语音提醒的轮询也在这里起，同样在 SR 守卫之前：
     播报走的是 TTS，跟语音识别没关系 —— 浏览器不给识别能力（或者识别服务连不上），
     「异常了主动喊一声」这件事照样该工作 */
  setInterval(pollAlert, ALERT_POLL_MS);

  /* 用户点一下页面 = 浏览器从此允许自动发声。立刻补播那一句 ——
     必须由手势来触发（fromGesture），引擎才认这份许可 */
  win.addEventListener('click', function () { pollAlert(true); });

  if (!SR) {
    setStatus('当前浏览器不支持语音识别，请使用 Chrome 或 Edge 浏览器（录音功能仍可用）。', true);
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


