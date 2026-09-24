const $ = (id) => document.getElementById(id);
const labels = {success:'请求成功', error:'请求失败', cancelled:'手动取消', queued:'排队中', running:'生成中'};
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = (seconds, full = false) => seconds ? new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', ...(full ? {year:'numeric'} : {}), month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(seconds * 1000)) : '—';
let state, records = [], galleryPage = 1, galleryPages = 1, galleryTotal = 0, polling = false, guestBusy = false, deleteBusy = false, selectionBusy = false, testingBusy = false, adminAuthenticated = false, pendingDeleteIds = [], clockOffset = 0, detailSequence = 0, latestDisplayedRun = null;

function elapsedSeconds(run, now = Date.now() / 1000 + clockOffset) {
  const started = Number(run?.started);
  if (!Number.isFinite(started)) return null;
  const finished = Number(run?.finished);
  const end = Number.isFinite(finished) && finished > 0 ? finished : run?.status === 'running' ? now : null;
  return end === null ? null : Math.max(0, end - started);
}

function formatDuration(run, now = Date.now() / 1000 + clockOffset) {
  if (!run || run.status === 'queued') return '排队中';
  const seconds = elapsedSeconds(run, now);
  if (seconds === null) return '耗时 —';
  const prefix = run.status === 'running' ? '已耗时 ' : '耗时 ';
  if (seconds < 1) return `${prefix}小于 1 秒`;
  if (seconds < 60) return `${prefix}${seconds.toFixed(1)} 秒`;
  const total = Math.round(seconds);
  return `${prefix}${Math.floor(total / 60)} 分 ${String(total % 60).padStart(2, '0')} 秒`;
}

const selectedRuns = new Set();
const pendingDeleteKey = 'pelican-pending-delete';
let pendingDeleteAccount = 'all';
const favoriteBusy = new Set();

function favoriteButton(run) {
  if (!adminAuthenticated) return run.favorite ? '<span class="favorite-label">★ 已收藏 · 删除保护</span>' : '';
  return `<button class="button secondary favorite-button" data-favorite-run="${run.id}" aria-pressed="${Boolean(run.favorite)}" ${favoriteBusy.has(run.id) || ['running','queued'].includes(run.status) ? 'disabled' : ''}>${run.favorite ? '★ 取消收藏' : '☆ 收藏'}</button>`;
}

function svgDownload(run) {
  return run.has_svg ? `<a class="button secondary" href="/api/runs/${run.id}/svg?download=1" download="${escapeHTML(run.filename)}" title="脚本动画请用浏览器单独打开 SVG；图片预览可能不播放动画">下载 SVG ↓</a>` : '';
}

async function toggleFavorite(id, favorite) {
  if (favoriteBusy.has(id) || deleteBusy || selectionBusy) return;
  favoriteBusy.add(id);
  document.querySelectorAll(`[data-favorite-run="${id}"]`).forEach(button => button.disabled = true);
  try {
    const result = await deleteApi(`/api/admin/runs/${id}/favorite`, {favorite});
    if (result.favorite) selectedRuns.delete(id);
    while (polling) await new Promise(resolve => setTimeout(resolve, 50));
    await refresh();
    if ($('detail-dialog').open && $('detail-body').dataset.runId === String(id)) await showDetail(id);
    operationMessage(result.favorite ? '已收藏，取消收藏前不能删除。' : '已取消收藏，现在可以删除。');
  } catch (error) {
    operationMessage(error.message, true);
    if (error.status === 401) { adminAuthenticated = false; renderAuthState(); }
  } finally {
    favoriteBusy.delete(id);
    updateSelectionControls();
  }
}

async function api(path, body) {
  const response = await fetch(path, {method:body === undefined ? 'GET' : 'POST', headers:{...(body === undefined ? {} : {'Content-Type':'application/json'})}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  if (!response.ok) {
    const data = await response.json().catch(() => ({error:`服务返回异常响应（HTTP ${response.status}），请稍后重试`}));
    const error = new Error(data.error || `请求失败：${response.status}`);
    error.status = response.status;
    throw error;
  }
  return path.endsWith('/svg') ? response.blob() : response.json();
}

async function deleteApi(path, body) {
  const response = await fetch(path, {credentials:'same-origin', method:body === undefined ? 'GET' : 'POST', headers:body === undefined ? {} : {'Content-Type':'application/json'}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  const data = await response.json().catch(() => ({error:`服务返回异常响应（HTTP ${response.status}），请稍后重试`}));
  if (!response.ok) {
    const error = new Error(data.error || `请求失败：${response.status}`);
    error.status = response.status;
    throw error;
  }
  return data;
}
function cleanRunIds(ids) {
  return [...new Set((Array.isArray(ids) ? ids : [ids]).filter(id => Number.isSafeInteger(id) && id > 0))];
}
function renderAuthState() {
  $('gallery-logout').hidden = !adminAuthenticated;
  $('gallery-auth-state').textContent = adminAuthenticated ? '已登录管理 · 收藏作品不可删除' : '管理登录后可收藏和删除';
  $('account-filter').hidden = !adminAuthenticated;
  if (!adminAuthenticated) {
    if ($('account-filter').value !== 'all') selectedRuns.clear();
    $('account-filter').replaceChildren(new Option('全部账号 / 接入方式', 'all'));
    records.forEach(run => { delete run.account_label; delete run.account_fingerprint; });
  }
  renderGallery();
  renderTestingControls();
}
function renderAccountFilter(accounts = []) {
  if (!adminAuthenticated) return;
  const select = $('account-filter'), current = select.value, label = select.selectedOptions[0]?.textContent;
  const options = [new Option('全部账号 / 接入方式', 'all'),
    ...accounts.map(account => new Option(`${account.label}（${account.count} 条）`, account.value)),
    new Option('账号未记录', 'unknown'), new Option('API 节点', 'api')];
  if (!options.some(option => option.value === current)) options.push(new Option(label || '该账号暂无作品', current));
  select.replaceChildren(...options);
  select.value = current;
}
async function refreshAuth() {
  const status = await deleteApi('/api/auth/status');
  adminAuthenticated = Boolean(status.authenticated);
  renderAuthState();
  return status;
}
function renderDeleteDialog() {
  const count = pendingDeleteIds.length;
  $('delete-auth-title').textContent = adminAuthenticated ? `删除 ${count} 项作品` : '请先登录管理';
  $('delete-auth-description').textContent = adminAuthenticated
    ? `将永久删除所选 ${count} 项作品及对应记录（编号：${pendingDeleteIds.slice(0,12).map(id => '#' + id).join('、')}${count > 12 ? '…' : ''}）。`
    : `已保留所选 ${count} 项作品。前往统一登录后会返回此处，再确认删除；后台与画廊共用本次登录。`;
  $('delete-auth-submit').hidden = !adminAuthenticated;
  $('delete-auth-submit').disabled = deleteBusy;
  $('delete-login-link').hidden = adminAuthenticated;
}
async function askDelete(ids, account = 'all') {
  if (deleteBusy) return;
  const candidates = cleanRunIds(ids);
  if (!candidates.length) return;
  pendingDeleteIds = candidates;
  pendingDeleteAccount = account;
  deleteBusy = true;
  updateSelectionControls();
  $('delete-auth-error').textContent = '';
  try {
    await refreshAuth();
    deleteBusy = false;
    renderDeleteDialog();
    if (!$('delete-auth-dialog').open) $('delete-auth-dialog').showModal();
  } catch (error) { operationMessage(error.message, true); }
  finally { deleteBusy = false; updateSelectionControls(); }
}
async function deleteRuns(ids) {
  let deleted = 0;
  for (let offset = 0; offset < ids.length; offset += 500) {
    const batch = ids.slice(offset, offset + 500);
    const result = await deleteApi('/api/admin/runs/delete', {ids:batch, account:pendingDeleteAccount});
    if (result.deleted !== batch.length) throw new Error('部分记录未删除，请刷新后重试');
    deleted += result.deleted;
    const removed = new Set(batch);
    batch.forEach(id => selectedRuns.delete(id));
    pendingDeleteIds = pendingDeleteIds.filter(id => !removed.has(id));
    $('delete-auth-error').textContent = `已删除 ${deleted} / ${ids.length} 项…`;
  }
  const url = new URL(location.href);
  if (ids.includes(Number(url.searchParams.get('run')))) {
    url.searchParams.delete('run');
    history.replaceState(null, '', url.pathname + url.search + url.hash);
  }
  detailSequence++;
  if ($('detail-dialog').open) $('detail-dialog').close();
  while (polling) await new Promise(resolve => setTimeout(resolve, 50));
  await refresh(galleryPage);
  return {deleted};
}
async function confirmDelete(event) {
  event.preventDefault();
  if (deleteBusy || !pendingDeleteIds.length || !adminAuthenticated) return;
  const ids = [...pendingDeleteIds];
  deleteBusy = true;
  updateSelectionControls();
  $('delete-auth-submit').disabled = true;
  $('delete-auth-cancel').disabled = true;
  $('delete-auth-error').textContent = '正在删除…';
  try {
    await deleteRuns(ids);
    pendingDeleteIds = [];
    $('delete-auth-dialog').close();
    operationMessage(`已删除 ${ids.length} 项作品和对应记录。`);
  } catch (error) {
    if (error.status === 401) {
      adminAuthenticated = false;
      renderAuthState();
      renderDeleteDialog();
    }
    renderDeleteDialog();
    $('delete-auth-error').textContent = error.status === 401 ? '登录已过期，请重新登录。未删除的作品仍保留。' : `${error.message}（剩余 ${pendingDeleteIds.length} 项）`;
    await refresh(galleryPage);
  } finally {
    deleteBusy = false;
    $('delete-auth-submit').disabled = false;
    $('delete-auth-cancel').disabled = false;
    updateSelectionControls();
  }
}
function selectableRun(run) { return !run.favorite && run.status !== 'running' && run.status !== 'queued'; }
function updateSelectionControls() {
  document.querySelectorAll('.gallery-filters select').forEach(select => select.disabled = polling || deleteBusy || selectionBusy);
  const available = records.filter(selectableRun);
  const selectedHere = available.filter(run => selectedRuns.has(run.id)).length;
  const otherPages = Math.max(0, selectedRuns.size - selectedHere);
  $('select-page-runs').checked = available.length > 0 && selectedHere === available.length;
  $('select-page-runs').indeterminate = selectedHere > 0 && selectedHere < available.length;
  $('select-page-runs').disabled = deleteBusy || selectionBusy || !available.length;
  $('select-all-runs').disabled = deleteBusy || selectionBusy || !galleryTotal;
  $('select-all-runs').textContent = selectionBusy ? '正在选择…' : '全选筛选结果（跨页）';
  $('selected-run-count').textContent = `已选 ${selectedRuns.size} 项${otherPages ? `（其他页 / 筛选外 ${otherPages} 项）` : ''}`;
  $('clear-run-selection').disabled = deleteBusy || selectionBusy || !selectedRuns.size;
  $('delete-selected-runs').disabled = deleteBusy || selectionBusy || !selectedRuns.size;
  $('delete-selected-runs').textContent = selectedRuns.size ? `删除所选（${selectedRuns.size}）` : '删除所选';
  document.querySelectorAll('[data-select-run]').forEach(input => {
    input.checked = selectedRuns.has(Number(input.dataset.selectRun));
    input.disabled = deleteBusy || selectionBusy || !records.some(run => run.id === Number(input.dataset.selectRun) && selectableRun(run));
    input.closest('.run-card').classList.toggle('selected', input.checked);
  });
  document.querySelectorAll('[data-delete-run]').forEach(button => {
    const run = records.find(item => item.id === Number(button.dataset.deleteRun));
    button.disabled = deleteBusy || (run && !selectableRun(run));
  });
  document.querySelectorAll('[data-favorite-run]').forEach(button => {
    const run = records.find(item => item.id === Number(button.dataset.favoriteRun));
    button.disabled = !adminAuthenticated || deleteBusy || selectionBusy || favoriteBusy.has(Number(button.dataset.favoriteRun)) || Boolean(run && ['running','queued'].includes(run.status));
  });
}
async function resumeDeleteAfterLogin() {
  try {
    await refreshAuth();
    const saved = JSON.parse(sessionStorage.getItem(pendingDeleteKey) || 'null');
    sessionStorage.removeItem(pendingDeleteKey);
    if (!saved || Date.now() - saved.at > 10 * 60 * 1000 || !Array.isArray(saved.ids)) return;
    const ids = cleanRunIds(saved.ids);
    ids.forEach(id => selectedRuns.add(id));
    updateSelectionControls();
    if (ids.length) await askDelete(ids, saved.account || 'all');
  } catch (error) { operationMessage(error.message, true); }
}

function message(text, isError = false) {
  $('form-message').textContent = text;
  $('form-message').classList.toggle('error', isError);
}

function operationMessage(text, isError = false) {
  const notice = $('operation-message');
  notice.textContent = text;
  notice.classList.toggle('error', isError);
}

function badge(status) { return `<span class="badge ${escapeHTML(status)}">${escapeHTML(labels[status] || '尚未提交')}</span>`; }

function renderTestingControls() {
  const active = state?.settings.enabled || state?.running || state?.queued_nodes?.length;
  $('gallery-testing-controls').hidden = !adminAuthenticated;
  $('gallery-start').disabled = testingBusy || !state || Boolean(active) || state.stopping;
  $('gallery-stop').disabled = testingBusy || !state || (!active && !state.stopping);
  $('gallery-test-state').textContent = state?.stopping ? '正在停止…' : state?.running ? '测试进行中' : state?.settings.enabled ? '测试已开始，等待下一轮' : '测试已停止';
}

async function controlTesting(operation) {
  if (testingBusy) return;
  testingBusy = true; renderTestingControls();
  try {
    await deleteApi(`/api/admin/testing/${operation}`, {});
    while (polling) await new Promise(resolve => setTimeout(resolve, 50));
    await refresh();
    operationMessage(operation === 'start' ? '已按保存的模型 / 循环组设置开始测试。' : '已停止测试，当前任务已取消，排队和定时任务已暂停。');
  } catch (error) {
    if (error.status === 401) { adminAuthenticated = false; renderAuthState(); }
    operationMessage(error.message, true);
  } finally { testingBusy = false; renderTestingControls(); }
}

function galleryQuery(page = galleryPage) {
  return new URLSearchParams({page, status:$('filter').value, protocol:$('protocol-filter').value, source:$('source-filter').value, effort:$('effort-filter').value, has_svg:$('svg-filter').value, favorite:$('favorite-filter').value, group_by:$('group-filter').value, account:adminAuthenticated ? $('account-filter').value : 'all'});
}

function renderState() {
  const s = state.stats, config = state.settings;
  $('task-prompt').textContent = state.task_prompt || '';
  $('rate').innerHTML = `${s.rate ?? '—'}<small>%</small>`;
  $('rate-meter').style.width = `${s.completed ? s.success / s.completed * 100 : 0}%`;
  $('rate-note').textContent = s.completed ? `${s.success} / ${s.completed} 次已完成请求成功 · 含已删除作品` : '等待第一份请求结果';
  $('total').innerHTML = `${s.total}<small>次</small>`;
  $('errors').innerHTML = `${s.errors}<small>次</small>`;
  $('error-note').textContent = `请求失败 ${s.errors} · 手动取消 ${s.cancelled || 0}`;
  const grouped = config.schedule_mode === 'groups';
  $('model-title').textContent = grouped ? '循环组' : config.model;
  $('site-name').textContent = grouped ? '按组与组合顺序独立生成' : `${config.node_name || '单个模型'} · ${config.base_url}`;
  $('effort-label').textContent = grouped ? '按组合设置' : config.effort;
  const latest = grouped ? state.timeline.find(r => r.group_name) : state.timeline.find(r => r.node_id === config.active_node_id);
  latestDisplayedRun = latest || null;
  const currentRunning = latest?.status === 'running';
  $('model-status').className = `badge ${currentRunning ? 'running' : latest?.status || 'neutral'}`;
  $('model-status').textContent = currentRunning ? '生成中' : latest ? labels[latest.status] : '尚无记录';
  $('last-run').textContent = latest ? date(latest.started) : '尚无记录';
  $('last-duration').textContent = latest ? formatDuration(latest) : '尚无记录';
  $('next-run').textContent = config.enabled ? (config.next_run == null ? '生成结束后计时' : date(config.next_run)) : '尚未启用';
  $('schedule-note').textContent = config.enabled ? `每张生成结束后等待 ${config.interval_minutes} 分钟` : '站点自动生成已暂停';
  const queued = state.queued_nodes || [];
  const queueStatus = $('queue-status');
  queueStatus.hidden = !queued.length;
  queueStatus.textContent = queued.length ? `排队中的节点（${queued.length}）：${queued.map(name => escapeHTML(name)).join('、')} · 将依次生成一张图` : '';
  $('frequency').textContent = `结束后等 ${config.interval_minutes} 分钟 / ${grouped ? '逐组逐个生成' : '每次一张'}`;
  $('footer-frequency').textContent = `每 15 秒同步记录 · 每张结束后间隔 ${config.interval_minutes} 分钟`;
  $('guest-panel').hidden = !config.guest_enabled;
  $('run').disabled = guestBusy || !config.guest_enabled;
  if (!guestBusy) $('run').innerHTML = config.guest_enabled ? '<span aria-hidden="true">▷</span> 生成一个鹈鹕动画' : '访客生成暂未开放';
  renderTimeline();
  renderTestingControls();
  updateClock();
}

function updateClock() {
  if (latestDisplayedRun && $('last-duration')) $('last-duration').textContent = formatDuration(latestDisplayedRun);
  if (!state?.settings.enabled) { $('countdown').textContent = '— : —'; return; }
  if (state.running || state.stopping || state.settings.next_run == null) {
    $('countdown').textContent = state.stopping ? '停止中' : '生成中';
    return;
  }
  const left = Math.max(0, Math.ceil(state.settings.next_run - (Date.now() / 1000 + clockOffset)));
  $('countdown').textContent = left ? `${String(Math.floor(left / 60)).padStart(2,'0')} : ${String(left % 60).padStart(2,'0')}` : state.running ? '生成中' : '即将开始';
}

function renderTimeline() {
  const minutes = 30;
  const interval = minutes * 60;
  const count = Math.ceil(86400 / interval);
  const end = (Math.floor(state.server_time / interval) + 1) * interval;
  $('timeline-summary').textContent = `按请求开始时间汇总 · ${count} 个时段`;
  $('timeline-interval').textContent = `每个色块 = ${minutes} 分钟`;
  $('timeline').style.gridTemplateColumns = `repeat(${count}, minmax(8px, 1fr))`;
  const fragments = document.createDocumentFragment();
  for (let i = 0; i < count; i++) {
    const start = end - (count - i) * interval;
    const group = state.timeline.filter(r => r.started >= start && r.started < start + interval);
    const status = ['running', 'error', 'cancelled', 'success'].find(s => group.some(r => r.status === s)) || 'empty';
    const button = document.createElement('button');
    button.className = status;
    button.title = `${date(start)} · ${group.length} 次请求 · ${labels[status] || '无记录'}`;
    button.setAttribute('aria-label', button.title + '，查看详情');
    button.addEventListener('click', () => showSlot(start, group));
    fragments.append(button);
  }
  $('timeline').replaceChildren(fragments);
}

function showSlot(start, group) {
  if (group.length === 1) { showDetail(group[0].id); return; }
  detailSequence++;
  $('detail-title').textContent = `${date(start)} · ${group.length} 次生成`;
  $('detail-body').innerHTML = group.length ? group.map(r => `<button class="history-row" data-run="${r.id}"><span>${date(r.started)} · ${r.source === 'manual' ? '手动' : r.source === 'retry' ? '重试' : '定时'} · ${escapeHTML(r.model)} · ${formatDuration(r)}</span>${badge(r.status)}</button>`).join('') : '<p class="empty-slot">这个时段没有生成记录。</p>';
  $('detail-body').querySelectorAll('[data-run]').forEach(button => button.addEventListener('click', () => showDetail(Number(button.dataset.run))));
  if (!$('detail-dialog').open) $('detail-dialog').showModal();
}

function imageURL(id) { return `/api/runs/${id}/svg`; }

async function attachImage(img, id) {
  try { img.src = await imageURL(id); }
  catch { img.alt = '动画加载失败，请刷新重试'; }
}

function renderGallery() {
  const visible = records;
  records.filter(run => !selectableRun(run)).forEach(run => selectedRuns.delete(run.id));
  updateSelectionControls();
  const filtered = ['filter', 'protocol-filter', 'source-filter', 'effort-filter', 'svg-filter', 'account-filter', 'favorite-filter']
    .some(id => $(id).value !== 'all');
  $('empty-state').hidden = galleryTotal > 0 || filtered;
  $('filter-empty').hidden = galleryTotal > 0 || !filtered;
  $('gallery-page').textContent = `第 ${galleryPage} / ${galleryPages} 页 · 共 ${galleryTotal} 条`;
  $('previous-page').disabled = polling || galleryPage <= 1;
  $('next-page').disabled = polling || galleryPage >= galleryPages;
  const signature = JSON.stringify([adminAuthenticated, $('group-filter').value, visible.map(r => [r.id, r.status, r.has_svg, r.has_html, r.finished, r.favorite, r.group_key, r.account_label, r.account_fingerprint])]);
  if ($('gallery').dataset.signature === signature) return;
  $('gallery').dataset.signature = signature;
  let lastGroup = null;
  $('gallery').innerHTML = visible.map(r => {
    const heading = $('group-filter').value !== 'none' && r.group_key !== lastGroup
      ? `<h3 class="gallery-group-title">${escapeHTML(r.group_key)}<span>${visible.filter(item => item.group_key === r.group_key).length} 条</span></h3>` : '';
    lastGroup = r.group_key;
    const preview = r.has_html
      ? `<span class="html-preview"><iframe data-fit-preview src="/api/runs/${r.id}/html?preview=1" sandbox="allow-scripts" loading="lazy" tabindex="-1" title="模型生成的 HTML 动画"></iframe></span>`
      : r.has_svg
      ? `<img data-image="${r.id}" alt="模型生成的鹈鹕骑行 SVG" loading="lazy">`
      : `<span class="no-preview ${escapeHTML(r.status)}"><b aria-hidden="true">${r.status === 'running' ? '◌' : '↯'}</b>${r.status === 'running' ? '模型正在创作' : '暂无可预览的动画'}</span>`;
    return heading + `<article class="run-card"><label class="run-select"><input type="checkbox" data-select-run="${r.id}" aria-label="选择作品 #${r.id}" ${selectedRuns.has(r.id) ? 'checked' : ''} ${!selectableRun(r) ? 'disabled' : ''}> 选择 #${r.id}</label><button class="preview" data-run="${r.id}" aria-label="${escapeHTML(date(r.started) + '，查看生成结果')}">${preview}<span class="overlay">查看结果 ↗</span></button><div class="card-body"><div class="card-top"><time>${date(r.started)}</time>${badge(r.status)}</div><p>${r.group_name ? '循环组：' : '单个：'}${escapeHTML(r.group_name || r.node_name || '历史记录')}</p>${adminAuthenticated ? `<p class="record-account">${escapeHTML(r.account_label || (r.protocol === 'codex' ? '账号未记录' : 'API 节点'))}</p>` : ''}<p class="card-model">${escapeHTML(r.model)} · ${escapeHTML(r.effort)} · ${formatDuration(r)}</p><p>${r.source === 'manual' ? '手动生成' : r.source === 'retry' ? '失败重试' : '定时生成'} · 鹈鹕骑行动画</p><div class="card-actions">${svgDownload(r)}${favoriteButton(r)}<button class="button secondary delete-run" type="button" data-delete-run="${r.id}" ${!selectableRun(r) ? 'disabled' : ''}>${r.favorite ? '已收藏 · 不可删除' : '删除作品和记录'}</button></div></div></article>`;
  }).join('');
  $('gallery').querySelectorAll('[data-run]').forEach(button => button.addEventListener('click', () => showDetail(Number(button.dataset.run))));
  $('gallery').querySelectorAll('[data-delete-run]').forEach(button => button.addEventListener('click', event => { event.stopPropagation(); askDelete(Number(button.dataset.deleteRun)); }));
  $('gallery').querySelectorAll('[data-favorite-run]').forEach(button => button.addEventListener('click', () => toggleFavorite(Number(button.dataset.favoriteRun), button.getAttribute('aria-pressed') !== 'true')));
  $('gallery').querySelectorAll('[data-image]').forEach(img => attachImage(img, Number(img.dataset.image)));
  updateSelectionControls();
}

async function showDetail(id) {
  const sequence = ++detailSequence;
  $('detail-body').dataset.runId = String(id);
  $('detail-title').textContent = '读取生成结果…';
  $('detail-body').replaceChildren();
  if (!$('detail-dialog').open) $('detail-dialog').showModal();
  try {
    const r = await api(`/api/runs/${id}`);
    if (sequence !== detailSequence) return;
    $('detail-title').textContent = `鹈鹕动画 · ${date(r.started)}`;
    $('detail-body').innerHTML = `${r.has_html ? `<div class="html-preview detail-html"><iframe data-fit-preview src="/api/runs/${r.id}/html?preview=1" sandbox="allow-scripts" title="模型生成的 HTML 动画"></iframe></div>` : r.has_svg ? '<img class="detail-image" id="detail-image" alt="模型生成的鹈鹕骑行 SVG 动画">' : ''}<div class="detail-meta">${badge(r.status)}<span>${r.group_name ? '循环组：' : '单个：'}${escapeHTML(r.group_name || r.node_name || '历史记录')}</span><span>${escapeHTML(r.model)} / ${escapeHTML(r.effort)}</span><span>${r.protocol === 'codex' ? '本机 Codex' : r.protocol === 'responses' ? 'Responses' : 'Chat Completions'}</span>${adminAuthenticated ? `<span>${escapeHTML(r.account_label || (r.protocol === 'codex' ? '账号未记录' : 'API 节点'))}</span>` : ''}<span>${formatDuration(r)}</span><span>${r.source === 'manual' ? '手动生成' : r.source === 'retry' ? '失败重试' : '定时生成'} #${r.id}</span></div>${r.error ? `<p class="detail-error">${escapeHTML(r.error)}</p>` : ''}<details open><summary>原始输出</summary><pre>${escapeHTML(r.output || '请求尚未返回内容')}</pre></details><details><summary>本轮固定提示词</summary><pre>${escapeHTML(r.prompt || '')}</pre></details><details><summary>请求信息与用量</summary><pre>${escapeHTML(JSON.stringify({api:r.base_url,requested_model:r.model,returned_model:r.returned_model,effort:r.effort,protocol:r.protocol,usage:r.usage},null,2))}</pre></details><div class="detail-actions">${r.has_html ? `<a class="button secondary" href="/api/runs/${r.id}/html" target="_blank" rel="noopener">打开原尺寸 HTML ↗</a>` : ''}${r.has_html ? `<a class="button secondary" href="/api/runs/${r.id}/html" download="${escapeHTML((r.filename || 'pelican.svg').replace(/\.svg$/, '.html'))}">下载 HTML ↓</a>` : ''}${svgDownload(r)}${favoriteButton(r)}<button class="button delete-run" id="delete-detail" type="button" ${!selectableRun(r) ? 'disabled' : ''}>${r.favorite ? '已收藏 · 不可删除' : '删除作品和记录'}</button></div>${r.has_svg ? '<p class="field-note">下载的 SVG 如含脚本动画，请用浏览器单独打开；作为普通图片预览时可能不播放动画。</p>' : ''}`;
    $('delete-detail').addEventListener('click', () => askDelete(id));
    if ($('detail-image')) attachImage($('detail-image'), id);
    $('detail-body').querySelector('[data-favorite-run]')?.addEventListener('click', () => toggleFavorite(id, !r.favorite));
  } catch (error) {
    if (sequence === detailSequence) { $('detail-title').textContent = '无法读取记录'; $('detail-body').textContent = error.message; }
  }
}

async function refresh(page = galleryPage) {
  if (polling) return;
  polling = true;
  $('refresh').disabled = true; $('previous-page').disabled = true; $('next-page').disabled = true;
  document.querySelectorAll('.gallery-filters select').forEach(select => select.disabled = true);
  let retryPublic = false;
  try {
    await refreshAuth();
    const query = galleryQuery(page);
    const [newState, fresh, guests] = await Promise.all([api('/api/state'), api(`/api/runs?${query}`), api('/api/guest/results')]);
    renderGuestResults(guests);
    state = newState;
    clockOffset = state.server_time - Date.now()/1000;
    galleryPage = fresh.page; galleryPages = fresh.pages; galleryTotal = fresh.total;
    records = fresh.items;
    if (!Array.isArray(fresh.accounts)) { adminAuthenticated = false; renderAuthState(); }
    renderAccountFilter(fresh.accounts);
    renderState(); renderGallery();
    $('connection').textContent = state.running ? '模型生成进行中' : state.settings.enabled ? '自动生成已启用' : '自动生成已暂停';
    $('connection').className = 'connection online';
  } catch (error) {
    if (error.status === 401) {
      adminAuthenticated = false;
      renderAuthState();
      retryPublic = true;
    }
    $('connection').textContent = error.message === '请输入管理口令' ? '等待解锁' : '服务连接失败';
    $('connection').className = 'connection offline';
  } finally {
    polling = false; $('refresh').disabled = false;
    updateSelectionControls(); renderGallery();
  }
  if (retryPublic) return refresh(1);
}

privacy.bind(document);
let guestSignature = "";
$('guest-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (guestBusy || !$('guest-form').reportValidity()) return;
  const values = {base_url:privacy.read($('base-url')), api_key:privacy.read($('api-key')), model:$('model').value.trim(), effort:$('effort').value, protocol:$('protocol').value, retry_count:Number($('guest-retries').value)};
  if (!values.base_url || !values.api_key) { message('请填写你自己的 API 地址和 API Key。', true); return; }
  guestBusy = true; $('run').disabled = true; $('run').textContent = '正在提交…';
  message('正在提交独立生成任务…');
  try {
    const result = await api('/api/guest/run', values);
    guestSignature = '';
    await refresh();
    message(`生成任务 #${result.id} 已提交`);
  } catch (error) { message(error.message, true); }
  finally {
    values.base_url = ''; values.api_key = '';
    guestBusy = false;
    $('run').disabled = state?.settings.guest_enabled === false;
    $('run').textContent = state?.settings.guest_enabled === false ? '访客生成暂未开放' : '生成一个鹈鹕动画';
  }
});

function renderGuestResults(results) {
  const signature = JSON.stringify(results);
  if (signature === guestSignature) return;
  guestSignature = signature;
  $('guest-results').replaceChildren();
  results.forEach(addGuestResult);
  $('guest-count').textContent = results.length;
  $('guest-empty').hidden = results.length > 0;
}
function addGuestResult(result) {
  const card = document.createElement('article');
  card.className = 'guest-result';
  const timing = result.status === 'queued' ? `排队中 · 第 ${result.queue_position || 1} 位` : formatDuration(result);
  card.innerHTML = `<div class="guest-result-head"><strong>访客 #${escapeHTML(result.id)}</strong>${badge(result.status)}</div><p>${date(result.submitted || result.started)} · ${escapeHTML(result.model)} · ${escapeHTML(result.effort)}</p><p>API：${escapeHTML(result.api_masked || '未记录')} · ${escapeHTML(result.protocol || '')}</p><p>${timing}</p>${result.error ? `<p class="guest-error">${escapeHTML(result.error)}</p>` : ''}${result.has_html ? `<div class="html-preview detail-html guest-html"><iframe data-fit-preview src="/api/guest/results/${encodeURIComponent(result.id)}/html?preview=1" sandbox="allow-scripts" title="访客生成的 HTML 动画"></iframe></div><a href="/api/guest/results/${encodeURIComponent(result.id)}/html" download="${escapeHTML((result.filename || 'model.svg').replace(/\.svg$/, '.html'))}">下载 HTML ↓</a>` : ''}${result.has_svg ? '<button class="guest-preview" aria-label="查看本次生成的 SVG"><img alt="本次生成的鹈鹕骑行 SVG"></button><a class="guest-download" title="脚本动画请用浏览器单独打开 SVG">下载 SVG ↓</a>' : ''}<details><summary>原始输出</summary><pre>${escapeHTML(result.output || '请求尚未返回内容')}</pre></details>`;
  if (result.has_svg) {
    const url = `/api/guest/results/${encodeURIComponent(result.id)}/svg`;
    card.querySelector('img').src = url;
    card.querySelector('.guest-download').href = url + '?download=1';
    card.querySelector('.guest-download').download = result.filename || 'model.svg';
    card.querySelector('.guest-preview').addEventListener('click', () => {
      detailSequence++;
      $('detail-title').textContent = `访客 #${result.id} · 鹈鹕 SVG`;
      $('detail-body').innerHTML = `<img class="detail-image" src="${url}" alt="本次生成的鹈鹕骑行 SVG"><p class="field-note">API：${escapeHTML(result.api_masked || '未记录')}</p><details open><summary>原始输出</summary><pre>${escapeHTML(result.output || '')}</pre></details>`;
      if (!$('detail-dialog').open) $('detail-dialog').showModal();
    });
  }
  $('guest-results').append(card);
}

$('refresh').addEventListener('click', () => refresh());
['filter','protocol-filter','source-filter','effort-filter','svg-filter','group-filter','account-filter','favorite-filter'].forEach(id => {
  $(id).addEventListener('change', () => { selectedRuns.clear(); updateSelectionControls(); refresh(1); });
});
$('previous-page').addEventListener('click', () => refresh(galleryPage - 1));
$('next-page').addEventListener('click', () => refresh(galleryPage + 1));
$('close-detail').addEventListener('click', () => { detailSequence++; $('detail-dialog').close(); });
$('detail-dialog').addEventListener('cancel', () => detailSequence++);
$('delete-auth-form').addEventListener('submit', confirmDelete);
$('delete-auth-cancel').addEventListener('click', () => { pendingDeleteIds = []; $('delete-auth-dialog').close(); });
$('delete-auth-dialog').addEventListener('cancel', event => { if (deleteBusy) { event.preventDefault(); return; } pendingDeleteIds = []; $('delete-auth-error').textContent = ''; });
setInterval(updateClock, 1000);
setInterval(refresh, 15000);
refresh();
resumeDeleteAfterLogin();
const linkedRun = new URLSearchParams(location.search).get('run');
if (/^[1-9]\d{0,17}$/.test(linkedRun || '')) showDetail(Number(linkedRun));

$('gallery').addEventListener('change', event => {
  const input = event.target.closest('[data-select-run]');
  if (!input || deleteBusy || selectionBusy) return;
  const id = Number(input.dataset.selectRun);
  input.checked ? selectedRuns.add(id) : selectedRuns.delete(id);
  updateSelectionControls();
});
$('select-page-runs').addEventListener('change', event => {
  records.filter(selectableRun).forEach(run => { if (!event.target.checked) selectedRuns.delete(run.id); else selectedRuns.add(run.id); });
  updateSelectionControls();
});
$('clear-run-selection').addEventListener('click', () => { selectedRuns.clear(); updateSelectionControls(); });
$('delete-selected-runs').addEventListener('click', () => askDelete([...selectedRuns], $('account-filter').value));
$('select-all-runs').addEventListener('click', async () => {
  if (deleteBusy || selectionBusy) return;
  selectionBusy = true; updateSelectionControls();
  const query = galleryQuery(1), original = query.toString();
  query.set('selection', '1');
  try {
    const result = await api(`/api/runs?${query}`);
    if (galleryQuery(1).toString() !== original) throw new Error('筛选条件已更改，请重新全选');
    selectedRuns.clear();
    cleanRunIds(result.ids).forEach(id => selectedRuns.add(id));
    operationMessage(`已全选当前筛选结果中的 ${selectedRuns.size} 项，包含所有页面；进行中的任务和已收藏作品已跳过。`);
  } catch (error) { operationMessage(error.message, true); }
  finally { selectionBusy = false; updateSelectionControls(); }
});
$('gallery-start').addEventListener('click', () => controlTesting('start'));
$('gallery-stop').addEventListener('click', () => controlTesting('stop'));
$('delete-login-link').addEventListener('click', () => {
  try { sessionStorage.setItem(pendingDeleteKey, JSON.stringify({ids:pendingDeleteIds, account:pendingDeleteAccount, at:Date.now()})); }
  catch { operationMessage('浏览器未允许保留选择，请登录后重新选择。', true); }
});
$('gallery-logout').addEventListener('click', async () => {
  try {
    await deleteApi('/api/auth/logout', {});
    adminAuthenticated = false;
    renderAuthState();
    operationMessage('已退出管理登录。');
    await refresh(1);
  } catch (error) { operationMessage(error.message, true); }
});
window.addEventListener('focus', () => { refresh(); });
