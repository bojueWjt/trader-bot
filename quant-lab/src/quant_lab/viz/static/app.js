'use strict';
// All report and message data goes through textContent, never HTML parsing.
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const colors = ['#4689ef', '#ed9960', '#54b897', '#b887e0', '#db678d', '#b7ac53', '#64b7cb', '#af8193', '#919bd7'];
const charts = [];
let priceChart = null;
let currentDetail = null;
let detailSequence = 0;
const kinds = {submitted:'提交订单', accepted:'接受订单', rejected:'拒绝订单', working:'订单挂起', partial_fill:'部分成交', filled:'成交', cancelled:'撤单', expired:'到期', stop_triggered:'触发止损', tp_triggered:'触发止盈', funding:'资金费结算', closed:'结束', amended:'移动止损', management:'老师指令'};
const actions = {close_all:'全部平仓', reduce:'减仓', move_stop:'移动止损', cancel_pending:'取消挂单', add:'加仓', none:'无动作'};
const reasons = {channel_mismatch:'频道不符', graph_version_mismatch:'图版本不符', episode_ambiguity:'信号归属有歧义', uncertain:'指令不确定', episode_not_replayed:'无可执行信号', none:'无动作', at_or_before_t_dec:'早于或等于决策时间', at_or_after_horizon:'超出观察窗', execution_at_or_after_horizon:'执行时刻超出观察窗', invalid_contract:'指令契约无效', policy_disabled:'该口径不跟老师指令'};
const outcomes = {tp_hit:'止盈', stopped:'止损', time_exit:'到期出场', filled_closed:'已平仓', unfilled_expired:'未成交到期', right_censored:'观察窗删失', unevaluable:'不可评估', rejected:'拒绝'};
const fills = {filled:'已成交', partial:'部分成交', none:'未成交'};

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  if (className) {
    node.className = className;
  }
  return node;
}
function link(text, href) { const node = el('a', text); node.href = href; return node; }
function url(path, args) { return path + '?' + new URLSearchParams(args).toString(); }
function number(value, digits = 3) {
  if (value === null || value === undefined) {
    return '—';
  }
  return Number(value).toLocaleString('zh-CN', {maximumFractionDigits:digits});
}
function percent(value) { return value === null || value === undefined ? '—' : number(Number(value) * 100, 1) + '%'; }
function time(value) { return value ? String(value).replace('T', ' ').replace(/\+00:00$/, ' UTC') : '时间未知'; }
function seconds(value) { return Date.parse(value) / 1000; }
function signClass(value) {
  if (value > 0) {
    return 'positive';
  }
  if (value < 0) {
    return 'negative';
  }
  return '';
}
async function api(path, args = {}) {
  const response = await fetch(url(path, args), {cache:'no-store'});
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || '读取失败');
  }
  return data;
}
function error(value) { $('error').hidden = false; $('error').textContent = value.message || String(value); }
function table(headers) {
  const node = el('table'); const row = el('tr'); headers.forEach(text => row.append(el('th', text)));
  const head = el('thead'); head.append(row); const body = el('tbody'); node.append(head, body);
  return {node, body};
}
function options(select, items, chosen) {
  select.replaceChildren();
  items.forEach(([value, label]) => { const option = el('option', label); option.value = value; select.append(option); });
  if (chosen !== undefined) {
    select.value = chosen;
  }
}
function chart(container) {
  if (!window.LightweightCharts) {
    container.append(el('p', 'K线组件加载失败，请检查浏览器能否访问 unpkg.com。')); return null;
  }
  const style = getComputedStyle(document.documentElement);
  const created = LightweightCharts.createChart(container, {
    autoSize:true, layout:{background:{color:style.getPropertyValue('--panel').trim()}, textColor:style.getPropertyValue('--muted').trim()},
    grid:{vertLines:{color:style.getPropertyValue('--line').trim()}, horzLines:{color:style.getPropertyValue('--line').trim()}},
    timeScale:{timeVisible:true, secondsVisible:false}, localization:{locale:'zh-CN'}, rightPriceScale:{borderVisible:false}
  });
  charts.push(created); return created;
}
function crumbs(name, key, detail = false) {
  $('breadcrumbs').replaceChildren(link('全部频道', '/'));
  if (name) {
    $('breadcrumbs').append(el('span', ' / '), link(name, url('/channel', {channel:key})));
  }
  if (detail) {
    $('breadcrumbs').append(el('span', ' / 单笔信号'));
  }
}
async function home() {
  const data = await api('/api/overview'); $('home').hidden = false; crumbs();
  const variants = Object.entries(data.variants);
  const summary = table(['频道', ...variants.map(([,v]) => v.name)]);
  data.channels.forEach(channel => {
    const row = el('tr'); const name = el('td'); name.append(link(channel.name, url('/channel', {channel:channel.key}))); row.append(name);
    variants.forEach(([key]) => {
      const cell = el('td', undefined, 'stat'); const stats = channel.statistics[key];
      if (stats.status === '未出') {
        cell.append(el('span', '未出', 'dim'));
      }
      else {
        cell.append(el('strong', number(stats.mean_R) + ' R / 笔', signClass(stats.mean_R)));
        cell.append(el('small', `${stats.n} 笔 · 胜率 ${percent(stats.win_rate)} · 合计 ${number(stats.sum_R)} R`));
        cell.append(el('small', stats.ci95 ? `95% [${number(stats.ci95[0])}, ${number(stats.ci95[1])}]` : `95% 区间不足（${stats.n_days} 天）`));
        let conclusionClass = 'dim';
        if (stats.conclusion === '正期望') {
          conclusionClass = 'positive';
        }
        if (stats.conclusion === '负期望') {
          conclusionClass = 'negative';
        }
        cell.append(el('span', stats.conclusion, conclusionClass));
      }
      row.append(cell);
    });
    summary.body.append(row);
    const panel = el('div', undefined, 'panel'); panel.append(el('h2', channel.name + ' · 累计 R'));
    const canvas = el('div', undefined, 'curve'); panel.append(canvas); $('curves').append(panel);
    const curve = chart(canvas); const legend = el('div', undefined, 'legend'); panel.append(legend);
    variants.forEach(([key, variant], index) => {
      const stats = channel.statistics[key];
      if (stats.status !== '已出') {
        return;
      }
      const color = colors[index % colors.length]; const label = el('span', variant.name); const dot = el('i'); dot.style.backgroundColor = color; label.prepend(dot); legend.append(label);
      if (!curve) {
        return;
      }
      // Equal t_dec signals share one chart time; keep the last cumulative value.
      const values = new Map(); stats.cumulative_R.forEach(point => values.set(seconds(point.t_dec), point.cumulative_R));
      const series = curve.addLineSeries({color, lineWidth:2, title:variant.name, priceLineVisible:false, lastValueVisible:false});
      series.setData([...values].map(([time,value]) => ({time,value})).sort((a,b) => a.time-b.time));
    });
    if (curve) {
      curve.timeScale().fitContent();
    }
  });
  $('summary-table').append(summary.node);
}
function entryText(entries) {
  return (entries || []).map(entry => {
    if (entry.price_lo === null || entry.price_lo === undefined) {
      return '市价参考';
    }
    if (entry.price_hi !== entry.price_lo) {
      return number(entry.price_lo) + '–' + number(entry.price_hi);
    }
    return number(entry.price_lo);
  }).join(' / ') || '—';
}
async function channelPage() {
  const key = params.get('channel'); const data = await api('/api/channel', {channel:key});
  $('channel').hidden = false; $('channel-title').textContent = data.name + ' · 信号列表'; crumbs(data.name, key);
  const variants = Object.entries(data.variants);
  const available = variants.filter(([key]) => data.statuses[key] === '已出');
  const chosen = available.length ? available[0][0] : 'base';
  options($('list-variant'), variants.map(([key,v]) => [key, v.name + (data.statuses[key] === '未出' ? '（未出）' : '')]), chosen);
  options($('symbol-filter'), [['', '全部'], ...[...new Set(data.trades.map(row => row.instrument))].sort().map(v => [v,v])]);
  options($('month-filter'), [['', '全部'], ...[...new Set(data.trades.map(row => row.t_dec.slice(0,7)))].sort().reverse().map(v => [v,v])]);
  const teacherIds = new Set(data.teacher_episode_ids);
  function render() {
    const selected = $('list-variant').value;
    const rows = data.trades.filter(row => {
      const result = row.variants[selected];
      if ($('filled-only').checked && (!result || !['filled','partial'].includes(result.fill_status))) {
        return false;
      }
      if ($('teacher-only').checked && !teacherIds.has(row.episode_id)) {
        return false;
      }
      if ($('symbol-filter').value && row.instrument !== $('symbol-filter').value) {
        return false;
      }
      if ($('month-filter').value && !row.t_dec.startsWith($('month-filter').value)) {
        return false;
      }
      return true;
    });
    const order = $('sort').value;
    rows.sort((a,b) => {
      if (order === 'r-high' || order === 'r-low') {
        const ar = a.variants[selected]; const br = b.variants[selected];
        const av = ar && ar.net_R !== null ? Number(ar.net_R) : null;
        const bv = br && br.net_R !== null ? Number(br.net_R) : null;
        if (av === null && bv !== null) {
          return 1;
        } if (bv === null && av !== null) {
          return -1;
        }
        if (av !== bv) {
          return order === 'r-high' ? bv-av : av-bv;
        }
      }
      return order === 'old' ? a.t_dec.localeCompare(b.t_dec) : b.t_dec.localeCompare(a.t_dec);
    });
    $('trade-count').textContent = `${rows.length} / ${data.trades.length} 笔信号 · 成交状态、出场方式与执行数按所选口径显示`;
    const view = table(['决策时间（UTC）','币种 / 方向','入场','止损','止盈',...variants.map(([,v]) => v.name + ' R'),'成交状态','出场方式','老师指令执行数']);
    rows.forEach(row => {
      const result = row.variants[selected]; const fallback = Object.keys(row.variants)[0];
      const target = url('/trade', {channel:key, episode:row.episode_id, variant:result ? selected : fallback});
      const tr = el('tr', undefined, 'trade-row'); const date = el('td'); date.append(link(time(row.t_dec), target)); tr.append(date);
      tr.append(el('td', `${row.instrument} / ${row.side === 'long' ? '多' : '空'}`), el('td', entryText(row.entries)), el('td', number(row.stop)));
      tr.append(el('td', (row.targets || []).map(tp => number(tp.price) + (tp.fraction !== null && tp.fraction !== undefined ? ' (' + percent(tp.fraction) + ')' : '')).join(' / ') || '—', 'legs'));
      variants.forEach(([v]) => { const r = row.variants[v]; tr.append(el('td', r ? number(r.net_R) : '未出', r ? signClass(r.net_R) : 'dim')); });
      tr.append(el('td', result ? fills[result.fill_status] || result.fill_status : '未出'), el('td', result ? outcomes[result.outcome_kind] || result.outcome_kind : '未出'), el('td', result ? String(result.n_teacher_actions_executed || 0) : '未出'));
      tr.addEventListener('click', event => { if (!event.target.closest('a') && !window.getSelection().toString()) {
        location.href = target;
      } });
      view.body.append(tr);
    });
    $('trade-list').replaceChildren(view.node);
  }
  ['list-variant','filled-only','teacher-only','symbol-filter','month-filter','sort'].forEach(id => $(id).addEventListener('change', render)); render();
}
function eventLabel(event) {
  if (event.kind === 'management') {
    try { const reason = JSON.parse(event.reason); return `老师指令 · ${actions[reason.kind] || reason.kind} · ${reason.status}`; } catch (_) { return '老师指令'; }
  }
  const leg = {entry:'入场', tp:'止盈', sl:'止损', close:event.order_id.startsWith('teacher-close') ? '老师指令出场' : '到期／平仓', funding:'资金费'}[event.leg] || event.leg;
  return `${leg} · ${kinds[event.kind] || event.kind}`;
}
function focusAt(value) {
  if (!priceChart || !value) {
    return;
  }
  const at = seconds(value); const interval = Number(currentDetail.bars.interval.replace('m',''));
  const width = Number.isFinite(interval) ? Math.max(3600,interval * 60 * 15) : 86400;
  priceChart.timeScale().setVisibleRange({from:at-width, to:at+width});
}
function timeline(data) {
  $('timeline').replaceChildren();
  data.timeline.forEach(item => {
    const node = el('article', undefined, 'event ' + item.type); const when = el('button', time(item.time));
    when.type = 'button'; when.addEventListener('click', () => focusAt(item.time)); node.append(when);
    if (item.type === 'message') {
      node.append(el('h3', `${item.role} · #${item.message_id === undefined ? '—' : item.message_id} · 版本 ${item.version_no === undefined ? '—' : item.version_no}`));
      if (item.reply_to) {
        node.append(el('small', '回复 #' + item.reply_to, 'muted'));
      }
      node.append(el('pre', item.text === null ? '原文版本缺失' : item.text));
      (item.instructions || []).forEach(instruction => {
        const adoption = instruction.adoption + (instruction.discard_reason ? ' · ' + (reasons[instruction.discard_reason] || instruction.discard_reason) : '');
        const label = `${actions[instruction.action] || instruction.action} · 比例 ${percent(instruction.fraction)} · 目标 ${instruction.target_symbol || '—'} / #${instruction.target_message_id || '—'} · ${adoption}`;
        const block = el('div', label, 'instruction');
        if (instruction.stop_price !== null && instruction.stop_price !== undefined) {
          block.append(el('div', '止损目标 ' + number(instruction.stop_price)));
        }
        if (instruction.to_entry) {
          block.append(el('div', '目标止损：入场均价'));
        }
        if (instruction.episode_ambiguity) {
          block.append(el('div', instruction.episode_ambiguity));
        }
        if (instruction.execution_statuses.length) {
          block.append(el('div', '执行结果：' + instruction.execution_statuses.join(' / ')));
        }
        else if (instruction.adopted) {
          block.append(el('div', data.consistency.ok ? '已采用，未处理（可能已结束）' : '复算不一致，执行结果隐藏'));
        }
        node.append(block);
      });
      const parsed = el('details'); parsed.append(el('summary', '解析结果、消息时钟与图事件'), el('pre', JSON.stringify({parsed:item.parsed, graph_events:item.graph_events, available_at:item.available_at, message_date:item.message_date, event_time:item.event_time},null,2))); node.append(parsed);
    } else {
      node.append(el('h3', eventLabel(item)), el('div', `价格 ${number(item.price)} · 数量 ${number(item.qty)} · 已成交总量比例 ${percent(item.fraction_of_filled)}`, 'muted'));
      if (item.reason) {
        node.append(el('pre', item.reason));
      }
    }
    $('timeline').append(node);
  });
}
function clearPriceChart() {
  if (!priceChart) {
    return;
  }
  const index = charts.indexOf(priceChart);
  if (index >= 0) {
    charts.splice(index,1);
  }
  priceChart.remove();
  priceChart = null;
}
function renderChart(data) {
  clearPriceChart();
  $('chart').replaceChildren(); priceChart = chart($('chart')); if (!priceChart) {
    return;
  }
  const bars = data.bars.series.klines; const markBars = data.bars.series.markPriceKlines;
  const candle = priceChart.addCandlestickSeries({upColor:'#32a58c', downColor:'#d76678', borderVisible:false, wickUpColor:'#32a58c', wickDownColor:'#d76678'});
  candle.setData(bars);
  const marks = priceChart.addLineSeries({color:'#8996aa',lineWidth:1,priceLineVisible:false,lastValueVisible:false}); marks.setData(markBars.map(bar => ({time:bar.time,value:bar.close})));
  function priceSegment(price, from, to, color, title, dashed = true) {
    if (price === null || price === undefined || from >= to) {
      return;
    }
    const line = priceChart.addLineSeries({color,lineWidth:1,lineStyle:dashed ? 2 : 0,priceLineVisible:false,lastValueVisible:false,title});
    line.setData([{time:from,value:Number(price)}, {time:to,value:Number(price)}]);
  }
  const from = seconds(data.trade.t_dec); const to = seconds(data.horizon_end);
  data.plan.entries.forEach((entry,index) => {
    priceSegment(entry.price_lo,from,to,'#4689ef','入场 '+(index+1));
    if (entry.price_hi !== entry.price_lo) {
      priceSegment(entry.price_hi,from,to,'#4689ef','入场上沿');
    }
  });
  data.plan.tps.forEach((tp,index) => priceSegment(tp.price,from,to,'#32a58c','TP '+(index+1)));
  data.stop_segments.forEach(segment => priceSegment(segment.price,seconds(segment.start),seconds(segment.end),'#d76678','止损',false));
  const markers = []; const availableTimes = bars.map(bar => bar.time);
  function chartTime(value) {
    const at = seconds(value);
    if (!Number.isFinite(at) || !availableTimes.length || at < seconds(data.bars.start) || at > seconds(data.bars.end)) {
      return null;
    }
    let low = 0; let high = availableTimes.length-1;
    while (low < high) { const middle = Math.ceil((low+high)/2); if (availableTimes[middle] <= at) {
      low = middle;
    } else { high = middle-1; } }
    return availableTimes[low];
  }
  data.timeline.forEach(item => {
    const time = chartTime(item.time); if (time === null) {
      return;
    }
    if (item.type === 'message') {
      markers.push({time,position:'aboveBar',color:'#4689ef',shape:'circle',text:`${item.role} #${item.message_id || '—'}`}); return;
    }
    if (['filled','partial_fill'].includes(item.kind)) {
      markers.push({time,position:item.leg === 'entry' ? 'belowBar' : 'aboveBar',color:item.leg === 'entry' ? '#4689ef' : '#db678d',shape:item.leg === 'entry' ? 'arrowUp' : 'arrowDown',text:`${eventLabel(item)} ${number(item.price)} / ${percent(item.fraction_of_filled)}`});
    } else if (item.kind === 'management' || item.kind === 'amended' || item.kind === 'expired') {
      markers.push({time,position:'aboveBar',color:'#b887e0',shape:'square',text:eventLabel(item)});
    }
  });
  candle.setMarkers(markers.sort((a,b) => a.time-b.time));
  priceChart.timeScale().setVisibleRange({from:seconds(data.bars.start),to:seconds(data.bars.end)});
  priceChart.subscribeClick(event => {
    if (!event.time) {
      return;
    }
    const candidates = [...$('timeline').children]; let best = -1; let distance = Infinity;
    data.timeline.forEach((item,index) => { const difference = Math.abs(seconds(item.time)-Number(event.time)); if (difference < distance) {
      distance = difference; best = index;
    } });
    if (best >= 0) {
      candidates[best].scrollIntoView({behavior:'smooth',block:'nearest'});
    }
  });
  const partial = bars.filter(bar => bar.partial).length;
  $('marker-legend').textContent = `${data.bars.interval} 聚合 · ${bars.length} 根交易价K线 · ${markBars.length} 根标记价 · ${partial} 根不完整聚合（缺失分钟不补齐）；标记落在所属或最近可用K线上，精确时间见事件线。`;
}
async function detailPage() {
  const key = params.get('channel'); const episode = params.get('episode'); const list = await api('/api/channel', {channel:key});
  const row = list.trades.find(row => row.episode_id === episode);
  if (!row) {
    throw new Error('未知单笔');
  }
  $('detail').hidden = false; crumbs(list.name,key,true);
  options($('detail-variant'), Object.entries(list.variants).map(([key,v]) => [key,v.name + (row.variants[key] ? ' · ' + number(row.variants[key].net_R) + ' R' : '（未出）')]), params.get('variant'));
  for (const option of $('detail-variant').options) { option.disabled = !row.variants[option.value]; }
  async function render() {
    const sequence = ++detailSequence; $('loading').hidden = false; $('error').hidden = true;
    const variant = $('detail-variant').value; const args = {channel:key,episode,variant};
    if ($('interval').value !== 'auto') {
      args.interval = $('interval').value;
    }
    try {
      const data = await api('/api/detail',args); if (sequence !== detailSequence) {
        return;
      }
      currentDetail = data;
      history.replaceState(null,'',url('/trade',{channel:key,episode,variant}));
      $('detail-title').textContent = `${data.trade.instrument} · ${data.trade.side === 'long' ? '做多' : '做空'} · ${time(data.trade.t_dec)}`;
      $('range').textContent = `${time(data.bars.start)} → ${time(data.bars.end)} · 当前 ${data.bars.interval}`;
      $('consistency').className = data.consistency.ok ? '' : 'bad';
      $('consistency').textContent = data.consistency.ok ? `复算一致 · ${data.consistency.actual}` : `复算不一致 · 回测记录 ${data.consistency.expected} · 当前复算 ${data.consistency.actual} · 成交事件已隐藏。请核对图、行情与策略版本。`;
      $('cards').replaceChildren();
      const values = [['net_R',data.trade.net_R],['费用',data.trade.fees],['资金费',data.trade.funding],['滑点',data.trade.slippage],['MAE (R)',data.trade.mae_R],['MFE (R)',data.trade.mfe_R],['入场均价',data.trade.entry_avg_price],['出场均价',data.trade.exit_avg_price],['老师指令执行',data.trade.n_teacher_actions_executed]];
      values.forEach(([label,value]) => { const card = el('div',undefined,'card'); card.append(el('span',label),el('strong',number(value))); $('cards').append(card); });
      const legs = el('div',undefined,'card'); legs.append(el('span','出场腿 / 方式'),el('strong',(data.trade.exit_legs || []).join(' / ') + ' · ' + (outcomes[data.trade.outcome_kind] || data.trade.outcome_kind))); $('cards').append(legs);
      if (data.trade.censor_reason) {
        const card = el('div',undefined,'card'); card.append(el('span','删失原因'),el('strong',data.trade.censor_reason)); $('cards').append(card);
      }
      timeline(data); renderChart(data);
    } catch (failure) {
      if (sequence === detailSequence) {
        error(failure);
        clearPriceChart();
        $('chart').replaceChildren(); $('timeline').replaceChildren(); $('cards').replaceChildren();
        $('range').textContent = ''; $('marker-legend').textContent = '';
        $('consistency').className = 'bad'; $('consistency').textContent = '复算不可用：' + failure.message;
      }
    }
    finally { if (sequence === detailSequence) {
      $('loading').hidden = true;
    } }
  }
  $('detail-variant').addEventListener('change',render); $('interval').addEventListener('change',render); await render();
}
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  const style = getComputedStyle(document.documentElement);
  charts.forEach(chart => chart.applyOptions({layout:{background:{color:style.getPropertyValue('--panel').trim()},textColor:style.getPropertyValue('--muted').trim()},grid:{vertLines:{color:style.getPropertyValue('--line').trim()},horzLines:{color:style.getPropertyValue('--line').trim()}}}));
});
(async () => {
  try {
    if (location.pathname === '/channel') {
      await channelPage();
    }
    else if (location.pathname === '/trade') {
      await detailPage();
    }
    else { await home(); }
  } catch (failure) { error(failure); }
  finally { $('loading').hidden = true; }
})();
