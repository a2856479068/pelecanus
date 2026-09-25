const $ = id => document.getElementById(id);
let authenticated = false, busy = false, testingState = null, runStats, runHistory = [], codexFingerprint = '';
let historyPage = 1, historyPages = 1, historyTotal = 0, historyLoading = false, historySelecting = false, historySequence = 0, historySearchTimer;
let liveSignature = '', accountRefreshing = false;
const selectedHistory = new Set();
function requireLogin() {
  authenticated = false;
  historySequence++;
  selectedHistory.clear(); runHistory = []; codexFingerprint = '';
  $('records-content').hidden = true;
  $('admin-run-history').replaceChildren();
  location.replace('/admin?return=records');
}
async function api(path, body) {
  const response = await fetch(path, {cache:'no-store', credentials:'same-origin', method:body === undefined ? 'GET' : 'POST', headers:body === undefined ? {} : {'Content-Type':'application/json'}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  if (response.status === 401) { requireLogin(); throw new Error('请先登录后台'); }
  const data = await response.json().catch(() => ({error:`服务返回异常响应（HTTP ${response.status}）`}));
  if (!response.ok) throw new Error(data.error || '操作失败');
  if (body !== undefined) PelicanLive.invalidate();
  return data;
}
function updateDatabaseState() {
  $('database-reset').disabled = !PelicanLive.fresh() || busy || Boolean(testingState?.running || testingState?.stopping);
}
async function action(fn) {
  if (busy) return;
  busy = true; updateHistorySelection(); updateDatabaseState();
  try { await fn(); }
  catch (error) { historyMessage(error.message, true); }
  finally { busy = false; updateHistorySelection(); updateDatabaseState(); }
}
const labels = {success:'请求成功', error:'请求失败', cancelled:'手动取消', running:'生成中', queued:'排队中'};
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dateTime = value => value ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value*1000)) : '—';
function historyQuery(page = historyPage) {
  return new URLSearchParams({page, status:$('history-status').value, account:$('history-account').value,
    model:$('history-model').value, group:$('history-group').value, period:$('history-period').value, favorite:$('history-favorite').value, search:$('history-search').value.trim()});
}
function historyMessage(text, error = false) {
  $('history-message').textContent = text;
  $('history-message').classList.toggle('error', error);
}
function historyOptions(id, items) {
  const select = $(id), current = select.value;
  if (!items.some(item => item.value === current)) items.push({value:current, label:select.selectedOptions[0]?.textContent || current});
  const signature = JSON.stringify(items);
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;
  select.replaceChildren(...items.map(item => new Option(item.label, item.value)));
  select.value = current;
}
function recordDuration(run) {
  if (!run.started || (!run.finished && run.status !== 'running')) return '—';
  const end = run.finished || PelicanLive.now();
  if (end == null) return '待同步';
  const seconds = Math.max(0, end - run.started);
  return seconds < 60 ? `${seconds.toFixed(1)} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`;
}
function updateHistorySelection() {
  const available = runHistory.filter(run => !run.favorite && !['running','queued'].includes(run.status));
  const here = available.filter(run => selectedHistory.has(run.id)).length;
  const blocked = busy || historyLoading || historySelecting;
  $('history-select-page').checked = available.length > 0 && here === available.length;
  $('history-select-page').indeterminate = here > 0 && here < available.length;
  $('history-select-page').disabled = blocked || !available.length;
  $('history-select-all').disabled = blocked || !historyTotal;
  $('history-clear').disabled = blocked || !selectedHistory.size;
  $('history-delete').disabled = blocked || !selectedHistory.size;
  $('history-delete').textContent = selectedHistory.size ? `删除所选（${selectedHistory.size}）` : '删除所选';
  const elsewhere = selectedHistory.size - here;
  $('history-selection').textContent = `已选 ${selectedHistory.size} 项${elsewhere > 0 ? `（其他页 ${elsewhere} 项）` : ''}`;
  $('history-page').textContent = `第 ${historyPage} / ${historyPages} 页 · 共 ${historyTotal} 条 · 每页 20 条`;
  $('history-previous').disabled = blocked || historyPage <= 1;
  $('history-next').disabled = blocked || historyPage >= historyPages;
  $('history-refresh').disabled = blocked;
  document.querySelectorAll('[data-select-history]').forEach(input => {
    input.checked = selectedHistory.has(Number(input.dataset.selectHistory));
    input.disabled = blocked || !available.some(run => run.id === Number(input.dataset.selectHistory));
    input.closest('tr').classList.toggle('selected', input.checked);
  });
  document.querySelectorAll('#admin-run-history [data-delete-run], #admin-run-history [data-retry], #admin-run-history [data-favorite-run]').forEach(button => {
    button.disabled = blocked || (button.hasAttribute('data-delete-run') && Boolean(runHistory.find(run => run.id === Number(button.dataset.deleteRun))?.favorite)) || (button.hasAttribute('data-retry') && Boolean(testingState?.running || testingState?.stopping));
  });
}
function renderRunMonitor() {
  const active = testingState?.timeline.find(run => run.id === testingState.running);
  const summary = testingState?.stopping ? '正在停止测试…' : active ? `正在生成 #${active.id} · ${active.model} · ${active.effort}` : testingState?.settings.enabled ? '等待下一轮测试' : '测试已暂停';
  const queued = testingState?.queued_nodes?.length || 0;
  $('admin-run-progress').textContent = PelicanLive.fresh() ? summary + (active ? ` · 已耗时 ${recordDuration(active)}` : '') + (queued ? ` · 排队 ${queued} 项` : '') : '服务未连接，任务状态待确认';
  const statItems = runStats ? [['请求成功率',runStats.rate === null ? '—' : `${runStats.rate}%`],['请求总数（成功 + 失败）',runStats.total],['请求成功',runStats.success],['请求失败',runStats.failed],['手动取消（不计入）',runStats.cancelled],['生成中 / 排队（不计入）',runStats.running]] : [];
  $('admin-stats').innerHTML = statItems.map(([name,value]) => `<div><span>${name}</span><strong>${value}</strong></div>`).join('');
  const signature = JSON.stringify([runHistory, codexFingerprint]);
  if ($('admin-run-history').dataset.signature !== signature) {
    $('admin-run-history').dataset.signature = signature;
    $('admin-run-history').innerHTML = runHistory.length ? `<table class="records-table"><thead><tr><th scope="col">选择 / 记录</th><th scope="col">账号</th><th scope="col">模型 / 强度</th><th scope="col">来源 / 循环组</th><th scope="col">请求结果</th><th scope="col">作品 / 操作</th></tr></thead><tbody>${runHistory.map(run => {
      const account = run.protocol !== 'codex' ? 'API 节点' : run.account_label || '账号未记录';
      const current = run.account_fingerprint && run.account_fingerprint === codexFingerprint;
      const inProgress = ['running','queued'].includes(run.status);
      return `<tr><td data-label="记录"><label class="record-check"><input type="checkbox" data-select-history="${run.id}" aria-label="选择记录 #${run.id}" ${inProgress || run.favorite ? 'disabled' : ''}><b>#${run.id}</b></label><time>${dateTime(run.started)}</time></td><td data-label="账号"><span class="record-account">${escapeHTML(account)}</span>${current ? '<small class="current-account">当前账号</small>' : ''}${run.protocol === 'codex' && !run.account_fingerprint ? '<small>无法追溯账号</small>' : ''}</td><td data-label="模型"><b>${escapeHTML(run.model)}</b><small>${escapeHTML(run.effort)} · ${run.protocol === 'codex' ? '本机 Codex' : run.protocol === 'responses' ? 'Responses' : 'Chat Completions'}</small></td><td data-label="来源"><span>${escapeHTML(run.group_name || '单个模型')}</span><small>${({manual:'手动测试',scheduled:'定时测试',retry:'失败重试'})[run.source] || '历史记录'} · ${escapeHTML(run.node_name || '历史节点')}</small></td><td data-label="结果"><span class="badge ${escapeHTML(run.status)}">${escapeHTML(labels[run.status] || run.status)}</span><small data-live-duration="${run.id}">${inProgress ? '已耗时' : '耗时'} ${recordDuration(run)}</small>${run.error ? `<details class="record-error"><summary>失败原因</summary><p>${escapeHTML(run.error)}</p></details>` : ''}</td><td data-label="操作"><span class="record-assets">${[run.has_html ? 'HTML' : '', run.has_svg ? 'SVG' : ''].filter(Boolean).join(' · ') || (inProgress ? '等待输出' : '无可预览作品')}</span><div class="record-actions"><a class="button secondary" href="/?run=${run.id}" target="_blank" rel="noopener">查看输出 ↗</a>${run.status === 'error' ? `<button class="button secondary" type="button" data-retry="${run.id}">重试</button>` : ''}${run.has_svg ? `<a class="button secondary" href="/api/runs/${run.id}/svg?download=1" download="${escapeHTML(run.filename)}" title="脚本动画请用浏览器单独打开 SVG；图片预览可能不播放动画">下载 SVG ↓</a>` : ''}${!inProgress ? `<button class="button secondary favorite-button" data-favorite-run="${run.id}" aria-pressed="${Boolean(run.favorite)}">${run.favorite ? '★ 取消收藏' : '☆ 收藏'}</button><button class="button secondary delete-art-button" type="button" data-delete-run="${run.id}" ${run.favorite ? 'disabled' : ''}>${run.favorite ? '已收藏 · 不可删除' : '删除'}</button>` : ''}</div></td></tr>`;
    }).join('')}</tbody></table>` : '<div class="history-empty">没有符合筛选条件的生成记录。</div>';
  }
  updateHistorySelection();
}
async function refreshRuns(page = historyPage) {
  const sequence = ++historySequence;
  historyLoading = true; updateHistorySelection();
  try {
    const [fresh, currentState] = await Promise.all([api(`/api/admin/runs?${historyQuery(page)}`), api('/api/state')]);
    if (sequence !== historySequence) return;
    applyServerState(currentState);
    if (!authenticated) return;
    runHistory = fresh.items;
    historyPage = fresh.page; historyPages = fresh.pages; historyTotal = fresh.total;
    historyOptions('history-account', [{value:'all',label:'全部账号 / 接入方式'}, ...fresh.accounts.map(account => ({value:account.value,label:`${account.label}（${account.count} 条）`})), {value:'unknown',label:'账号未记录'}, {value:'api',label:'API 节点'}]);
    historyOptions('history-model', [{value:'all',label:'全部模型'}, ...fresh.models.map(model => ({value:model,label:model}))]);
    historyOptions('history-group', [{value:'all',label:'全部循环组'}, {value:'single',label:'单个模型 / 历史记录'}, ...fresh.groups.map(group => ({value:group,label:group}))]);
    runHistory.filter(run => run.favorite || ['running','queued'].includes(run.status)).forEach(run => selectedHistory.delete(run.id));
    runStats = {...testingState.stats, failed:testingState.stats.errors};
    renderRunMonitor(); updateDatabaseState();
  } finally {
    if (sequence === historySequence) { historyLoading = false; updateHistorySelection(); }
  }
}
async function deleteHistory(ids) {
  if (busy || !ids.length || !confirm(`确认删除 ${ids.length} 项作品及对应记录？此操作会同时移除画廊中的作品。`)) return;
  const account = $('history-account').value;
  await action(async () => {
    let deleted = 0;
    try {
      for (let offset = 0; offset < ids.length; offset += 500) {
        const batch = ids.slice(offset, offset + 500);
        const result = await api('/api/admin/runs/delete', {ids:batch, account});
        deleted += result.deleted;
        batch.forEach(id => selectedHistory.delete(id));
        historyMessage(`已删除 ${deleted} / ${ids.length} 项…`);
      }
      historyMessage(`已删除 ${deleted} 项作品及对应记录。`);
    } catch (error) {
      historyMessage(`已删除 ${deleted} 项；${error.message}。未删除的选择已保留。`, true);
    } finally { await refreshRuns(); }
  });
}
function applyHistoryFilters() {
  clearTimeout(historySearchTimer);
  selectedHistory.clear();
  historyPage = 1;
  historyMessage('');
  refreshRuns(1).catch(error => historyMessage(error.message, true));
}
['history-status','history-account','history-model','history-group','history-period','history-favorite'].forEach(id => $(id).addEventListener('change', applyHistoryFilters));
$('history-search').addEventListener('input', () => {
  clearTimeout(historySearchTimer);
  historySequence++;
  historyLoading = true; updateHistorySelection();
  historySearchTimer = setTimeout(applyHistoryFilters, 300);
});
$('history-refresh').addEventListener('click', () => refreshRuns().catch(error => historyMessage(error.message, true)));
$('history-previous').addEventListener('click', () => refreshRuns(historyPage - 1).catch(error => historyMessage(error.message, true)));
$('history-next').addEventListener('click', () => refreshRuns(historyPage + 1).catch(error => historyMessage(error.message, true)));
$('history-select-page').addEventListener('change', event => {
  runHistory.filter(run => !run.favorite && !['running','queued'].includes(run.status)).forEach(run => event.target.checked ? selectedHistory.add(run.id) : selectedHistory.delete(run.id));
  historyMessage('');
  updateHistorySelection();
});
$('admin-run-history').addEventListener('change', event => {
  const input = event.target.closest('[data-select-history]');
  if (!input || busy || historyLoading || historySelecting) return;
  input.checked ? selectedHistory.add(Number(input.dataset.selectHistory)) : selectedHistory.delete(Number(input.dataset.selectHistory));
  historyMessage('');
  updateHistorySelection();
});
$('history-clear').addEventListener('click', () => { selectedHistory.clear(); historyMessage('已清空选择。'); updateHistorySelection(); });
$('history-select-all').addEventListener('click', async () => {
  if (busy || historySelecting) return;
  historySelecting = true; updateHistorySelection();
  const query = historyQuery(1), signature = query.toString();
  query.set('selection', '1');
  try {
    const result = await api(`/api/admin/runs?${query}`);
    if (historyQuery(1).toString() !== signature) throw new Error('筛选已更改，请重新全选');
    selectedHistory.clear(); result.ids.forEach(id => selectedHistory.add(id));
    historyMessage(`已选择全部 ${result.total} 项筛选结果（跨页），正在生成的任务和已收藏作品已跳过。`);
  } catch (error) { historyMessage(error.message, true); }
  finally { historySelecting = false; updateHistorySelection(); }
});
$('history-delete').addEventListener('click', () => deleteHistory([...selectedHistory]));
$('database-reset').addEventListener('click', async () => {
  if (busy || testingState?.running || testingState?.stopping) return;
  if (!confirm('确认初始化当前数据库的生成数据？所有作品、原始输出、访客结果和请求统计将被清空，且无法恢复；定时测试将暂停。管理员密码和节点设置会保留。')) return;
  busy = true;
  historySequence++;
  updateDatabaseState(); updateHistorySelection();
  $('database-message').textContent = '正在初始化生成数据…';
  $('database-message').classList.remove('error');
  try {
    const result = await api('/api/admin/database/reset', {confirm:'RESET_GENERATION_DATA'});
    selectedHistory.clear(); historyPage = 1;
    $('history-search').value = '';
    for (const id of ['history-status','history-account','history-model','history-group','history-period','history-favorite']) $(id).value = 'all';
    await refreshRuns(1);
    historyMessage('生成记录与统计已初始化。');
    $('database-message').textContent = `已清空 ${result.deleted_runs} 条生成记录、${result.deleted_images} 张图片、${result.deleted_guest_results} 条访客结果及 ${result.deleted_outcomes} 条请求统计。${result.compacted ? '数据库空间已整理。' : '数据库内容已清空，空间整理未完成。'}`;
  } catch (error) {
    $('database-message').textContent = error.message;
    $('database-message').classList.add('error');
  } finally {
    busy = false;
    updateDatabaseState(); updateHistorySelection();
  }
});
$('admin-run-history').addEventListener('click', async event => {
  const favoriteButton = event.target.closest('[data-favorite-run]');
  if (favoriteButton) {
    if (busy || historyLoading || historySelecting) return;
    const id = Number(favoriteButton.dataset.favoriteRun);
    const favorite = favoriteButton.getAttribute('aria-pressed') !== 'true';
    return action(async () => {
      await api(`/api/admin/runs/${id}/favorite`, {favorite});
      if (favorite) selectedHistory.delete(id);
      await refreshRuns();
      historyMessage(favorite ? '已收藏，取消收藏前不能删除。' : '已取消收藏，现在可以删除。');
    });
  }

  const deleteButton = event.target.closest('[data-delete-run]');
  if (deleteButton) return deleteHistory([Number(deleteButton.dataset.deleteRun)]);
  const button = event.target.closest('[data-retry]');
  if (!button || busy) return;
  action(async () => {
    const result = await api(`/api/admin/runs/${button.dataset.retry}/retry`, {});
    await refreshRuns();
    historyMessage(`已创建失败请求重试 #${result.id}。`);
  });
});

function applyServerState(value) {
  if (!value.authenticated) { requireLogin(); return; }
  if (testingState && value.server_time < testingState.server_time) return;
  testingState = value;
  runStats = {...value.stats, failed:value.stats.errors};
  renderRunMonitor(); updateDatabaseState();
  for (const element of document.querySelectorAll('[data-live-duration]')) {
    const id = Number(element.dataset.liveDuration);
    const run = value.timeline.find(run => run.id === id) || runHistory.find(run => run.id === id);
    if (run) element.textContent = `${run.status === 'running' ? '已耗时' : '耗时'} ${recordDuration(run)}`;
  }
}
async function refreshAccount() {
  if (!authenticated || accountRefreshing) return;
  accountRefreshing = true;
  try {
    const status = await api('/api/admin/codex/account');
    codexFingerprint = status.account_fingerprint || '';
  } catch { codexFingerprint = ''; }
  finally { accountRefreshing = false; renderRunMonitor(); }
}
$('logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', {}); requireLogin(); }
  catch (error) { historyMessage(error.message, true); }
});
PelicanLive.start(value => {
  if (!value.authenticated) { requireLogin(); return; }
  const entering = !authenticated;
  authenticated = true;
  $('records-content').hidden = false; $('logout').hidden = false;
  $('records-sync').textContent = '已连接 · 每秒同步状态';
  applyServerState(value);
  if (entering) void refreshAccount();
  const signature = JSON.stringify([value.timeline.map(run => [run.id,run.status,run.finished,run.favorite]), value.stats]);
  if (signature !== liveSignature && !historyLoading && !busy && !historySelecting) {
    liveSignature = signature;
    refreshRuns().catch(error => historyMessage(error.message, true));
  }
}, () => {
  $('records-sync').textContent = '服务连接中断 · 状态待确认';
  document.querySelectorAll('[data-live-duration]').forEach(element => {
    if (testingState?.timeline.find(run => run.id === Number(element.dataset.liveDuration))?.status === 'running') element.textContent = '耗时待同步';
  });
  codexFingerprint = '';
  renderRunMonitor(); updateDatabaseState();
});
setInterval(() => {
  if (authenticated && !busy && !historyLoading && !historySelecting) refreshRuns().catch(error => historyMessage(error.message, true));
  void refreshAccount();
}, 5000);
