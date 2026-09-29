/* A1「哪个宿舍现在最值得关注」的判定内核。
   dashboard 和 3d 两个页面都要用同一套判定，而且必须给出同样的结论 ——
   各自抄一份的下场参考 computeStatus 那次的漂移（一处阈值 18、一处 10，
   同一条数据两个页面显示不同结论）。所以抽成纯函数库，两页共用。

   规则（三条，按顺序比较，来自需求原文）：
     ① 连续异常时长：越长越优先
     ② 时长相同 → 异常总次数：越多越优先
     ③ 前两项都相同 → 固定顺序 dorm-a > dorm-b > dorm-c

   纯函数：不读时钟、不读 localStorage、不碰 DOM。
   now 由调用方注入 —— 生产环境传 Date.now()，单元测试传固定值，
   判定逻辑才能真正做到「同样输入必得同样输出」。
   浏览器里挂 window.DormMateA1，Node 里走 module.exports，同一份代码两边都能跑。 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.DormMateA1 = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  var NODE_ORDER = ['dorm-a', 'dorm-b', 'dorm-c'];

  /* 异常类型。阈值与 computeStatus、analysis.py 三处完全一致：
     温度 < 18 偏冷 / 温度 >= 30 偏热 / 湿度 >= 75 偏湿。
     返回 null 表示正常，其余五种对应告警要显示的五行文案。 */
  function kindOf(temp, hum) {
    var cold = temp < 18;
    var hot = temp >= 30;
    var wet = hum >= 75;
    if (hot && wet) return 'hotWet';
    if (cold && wet) return 'coldWet';
    if (hot) return 'hot';
    if (cold) return 'cold';
    if (wet) return 'wet';
    return null;
  }

  /* 告警文案，五种，逐字来自需求原文。页面和复盘报告都引用这一份，
     文案只有这一处，改的时候不会漏掉某个页面。 */
  var ALERT_TEXT = {
    hot: '持续高温时间较长',
    cold: '持续低温时间较长',
    wet: '持续高湿时间较长',
    hotWet: '持续高温偏湿时间较长',
    coldWet: '持续低温偏湿时间较长'
  };

  // 类型判断不出来（比如只拿到一个布尔量的老调用方）时的兜底。
  // 宁可说「异常持续中」，也不能瞎猜一个具体类型 —— 猜错会被当成事实读下去
  var ALERT_UNKNOWN = '异常持续中';

  function alertText(kind) {
    return ALERT_TEXT[kind] || ALERT_UNKNOWN;
  }

  /* 类型短名，总览表里用。ALERT_TEXT 是一整句话（进事件日志、复盘报告），
     塞进表格单元格里又长又不像「类型」。两套文案的键完全一致，
     不会出现某个类型有 ALERT_TEXT 却漏了 TYPE_TEXT */
  var TYPE_TEXT = {
    hot: '高温',
    cold: '低温',
    wet: '高湿',
    hotWet: '高温偏湿',
    coldWet: '低温偏湿'
  };

  function typeText(kind) {
    return TYPE_TEXT[kind] || '';
  }

  /* tracker 形状：{ "dorm-a": {abnormalSince: 毫秒|null, count: 数字, kind: 字符串|null}, ... }
     · abnormalSince —— 当前这一段连续异常的起点；正常报文把它置回 null。
       它同时被当作这一段的编号（eid）用：事件日志按 (node, eid) 分组，
       同一个异常段里的所有事件才会被复盘报告归到同一条时间线上。
     · count —— 本段内收到的异常报文条数。数据一恢复正常就清零（需求原文：
       「数据恢复正常，次数和计时全部清零」），所以它数的是「这一段持续期间
       收到了多少条超标数据」，不是全时累计。
     · kind —— 本段当前的异常类型（hot/cold/wet/hotWet/coldWet）。
       一段里类型可能变（先偏热、后偏热+偏湿），一直跟着最新那条报文走。 */
  function createTracker() {
    return {};
  }

  function entryOf(tracker, node) {
    var entry = tracker[node];
    if (!entry) {
      entry = tracker[node] = { abnormalSince: null, count: 0, kind: null };
    }
    // 老数据（页面升级前存下来的）里没有 kind 字段，补一个，
    // 免得后面读到 undefined 拼出「undefined 时间较长」这种文案
    if (entry.kind === undefined) entry.kind = null;
    return entry;
  }

  /* 喂一条报文。kind 为 null 表示正常，其余五种见 kindOf。
     返回该节点的最新状态，调用方据此判断段沿（0→1 / 1→0）。

     兼容：kind 传布尔量时按「异常与否」处理，类型记为 null（老调用方/测试用），
     不会因为类型未知就被当成正常报文。 */
  function feed(tracker, node, kind, now) {
    var entry = entryOf(tracker, node);
    var isAbnormal = (kind !== null && kind !== undefined && kind !== false);
    var type = (typeof kind === 'string' && ALERT_TEXT[kind]) ? kind : null;

    if (isAbnormal) {
      entry.count += 1;
      entry.kind = type;
      // 已经在异常段里就不动起点 —— 段的长度是从「第一次异常」算起的，
      // 每来一条就重置的话，时长永远停在一条报文的间隔上
      if (entry.abnormalSince === null) {
        entry.abnormalSince = now;
      }
    } else {
      // 恢复正常 = 这一轮结束：计时、次数、类型一起清零，下一次异常重新数
      entry.abnormalSince = null;
      entry.count = 0;
      entry.kind = null;
    }
    return entry;
  }

  /* 当前最值得关注的宿舍。
     候选集 = 正在异常段里的节点（abnormalSince 非 null）。
     都不异常就没有「最值得关注」这回事，返回 node:null，页面上横幅整块隐藏。

     返回 {node, reason, durationMs, count, kind}：
       reason 是「哪条规则决出的胜负」，写进事件日志、复盘报告用：
         only     —— 只有一个节点在异常，没得比
         duration —— ①时长更长
         count    —— 时长相同、②次数更多
         order    —— 前两项都相同、③固定顺序靠前
       kind 是赢家当前的异常类型，页面拿它取告警文案（alertText） */
  function pick(tracker, now, nodeOrder) {
    var order = nodeOrder || NODE_ORDER;
    var candidates = [];

    for (var i = 0; i < order.length; i++) {
      var node = order[i];
      var entry = tracker[node];
      if (!entry || entry.abnormalSince === null || entry.abnormalSince === undefined) {
        continue;
      }
      candidates.push({
        node: node,
        order: i,
        // 浏览器时钟被往回调过的话差值会是负的，负数时长显示成「-3 分钟」很荒唐，
        // 这里夹到 0。排序也跟着用这个值，保证显示和排序是同一个数
        durationMs: Math.max(0, now - entry.abnormalSince),
        count: entry.count || 0,
        kind: entry.kind || null
      });
    }

    if (!candidates.length) {
      return { node: null, reason: null, durationMs: 0, count: 0, kind: null };
    }

    candidates.sort(function (a, b) {
      if (a.durationMs !== b.durationMs) return b.durationMs - a.durationMs;  // ① 时长降序
      if (a.count !== b.count) return b.count - a.count;                      // ② 次数降序
      return a.order - b.order;                                               // ③ 固定顺序升序
    });

    var win = candidates[0];
    var reason = 'only';
    if (candidates.length > 1) {
      var runnerUp = candidates[1];
      if (win.durationMs > runnerUp.durationMs) {
        reason = 'duration';
      } else if (win.count > runnerUp.count) {
        reason = 'count';
      } else {
        // 时长、次数都和亚军一样，赢在固定顺序上
        reason = 'order';
      }
    }

    return {
      node: win.node, reason: reason, durationMs: win.durationMs,
      count: win.count, kind: win.kind
    };
  }

  /* 时长文案。秒级以下不显示小数（「45 秒」比「45.3 秒」干净），
     跨小时才带上小时，避免出现「725 分钟」这种要心算的数字 */
  function durationText(ms) {
    var totalSeconds = Math.floor(Math.max(0, ms) / 1000);
    if (totalSeconds < 60) {
      return totalSeconds + ' 秒';
    }
    var minutes = Math.floor(totalSeconds / 60);
    if (minutes < 60) {
      return minutes + ' 分钟';
    }
    var hours = Math.floor(minutes / 60);
    var rest = minutes % 60;
    return rest ? hours + ' 小时 ' + rest + ' 分钟' : hours + ' 小时';
  }

  /* 原因文案。横幅上不再显示它（横幅只显示异常类型），但它会写进事件日志的
     priority 记录里 —— 复盘报告要能回答「当时为什么挑了 dorm-a 而不是 dorm-b」 */
  var REASON_TEXT = {
    only: '唯一异常宿舍',
    duration: '持续异常时间更长',
    count: '异常次数更多',
    order: '按宿舍编号顺序（时长与次数相同）'
  };

  function reasonText(reason) {
    return REASON_TEXT[reason] || '当前最值得关注';
  }

  /* 页面横幅上的「原因」，把「哪种异常」和「赢在哪条规则」拼成一句话：
     持续高温时间更长 / 持续低温偏湿，异常次数更多 / …
     （需求原文给的例子就是「持续高温时间更长」这个句式。）

     REASON_TEXT 只讲规则（持续异常时间更长），不带类型 —— 读的人不知道是热还是冷；
     ALERT_TEXT 只讲类型（持续高温时间较长），不讲规则 —— 读的人不知道为什么是它。
     横幅上这两件事得同时说清楚，所以单独拼一句。
     这份文案只上页面；事件日志和复盘报告照旧用 reason / alert / count 这些结构化的
     字段，不把展示用的句子塞进数据里 —— 报告要能按字段筛，不该去解析中文 */
  function whyText(result) {
    if (!result || !result.node) return '';
    // 只有一个宿舍异常，没有比较这回事，硬说「时间更长」是编
    if (result.reason === 'only') return '唯一异常宿舍';
    var type = typeText(result.kind);
    // 类型不明（老调用方只传了个布尔量）时说不了是冷是热，别硬拼「持续异常时间更长」，
    // 退回只讲规则的那版文案
    if (!type) return reasonText(result.reason);
    var head = '持续' + type;
    if (result.reason === 'count') return head + '，异常次数更多';
    if (result.reason === 'order') return head + '，时长与次数相同，按宿舍编号顺序';
    // duration 是兜底分支：规则①先比，赢家绝大多数出在这里
    return head + '时间更长';
  }

  /* 给页面直接铺开的完整描述，两个页面都用它，格式不会再分叉：
     {node, alertText, whyText, durationText, count, reasonText, text}
     text 例：dorm-b｜持续高温时间更长 · 异常 3 次 · 已持续 12 分钟 */
  function describe(result) {
    if (!result || !result.node) {
      return { node: null, alertText: '', whyText: '', durationText: '', count: 0, reasonText: '', text: '' };
    }
    var dText = durationText(result.durationMs);
    var aText = alertText(result.kind);
    var wText = whyText(result);
    return {
      node: result.node,
      alertText: aText,
      whyText: wText,
      durationText: dText,
      count: result.count,
      reasonText: reasonText(result.reason),
      text: result.node + '｜' + wText + ' · 异常 ' + result.count + ' 次 · 已持续 ' + dText
    };
  }

  return {
    NODE_ORDER: NODE_ORDER,
    ALERT_TEXT: ALERT_TEXT,
    TYPE_TEXT: TYPE_TEXT,
    createTracker: createTracker,
    kindOf: kindOf,
    alertText: alertText,
    typeText: typeText,
    whyText: whyText,
    feed: feed,
    pick: pick,
    durationText: durationText,
    reasonText: reasonText,
    describe: describe
  };
});
