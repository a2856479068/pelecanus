const $ = (id) => document.getElementById(id);
const labels = {passed: '双项通过', invalid: '校验未通过', error: '请求失败', queued: '排队中', running: '检测中', legacy: '历史单项'};
const candyLabels = {passed:'糖果通过',invalid:'糖果未通过',error:'糖果请求失败',running:'糖果检测中',not_run:'未检测糖果'};
const candyBadge = status => `<span class="badge ${escapeHTML(status)}">${candyLabels[status] || '未检测糖果'}</span>`;
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = (seconds, full = false) => seconds ? new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', ...(full ? {year:'numeric'} : {}), month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(seconds * 1000)) : '—';
let state, records = [], galleryPage = 1, galleryPages = 1, galleryTotal = 0, polling = false, guestBusy = false, clockOffset = 0, detailSequence = 0;

async function api(path, body) {
  const response = await fetch(path, {method:body === undefined ? 'GET' : 'POST', headers:{...(body === undefined ? {} : {'Content-Type':'application/json'})}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  if (!response.ok) {
    const data = await response.json().catch(() => ({error:`服务返回异常响应（HTTP ${response.status}），请稍后重试`}));
    throw new Error(data.error || `请求失败：${response.status}`);
  }
  return path.endsWith('/svg') ? response.blob() : response.json();
}

function message(text, isError = false) {
  $('form-message').textContent = text;
  $('form-message').classList.toggle('error', isError);
}

function badge(status) { return `<span class="badge ${escapeHTML(status)}">${escapeHTML(labels[status] || '尚未检测')}</span>`; }

function qualityTag(tests = {}, corner = true) {
  const reasons = [];
  if (tests.pelican?.review?.status === 'invalid') reasons.push('鹈鹕视觉审核未通过');
  if (tests.candy?.status === 'invalid') reasons.push('糖果答案未通过');
  if (!reasons.length) return '';
  return `<span class="${corner ? 'quality-corner' : 'quality-flag'}" title="${reasons.join('；')}" aria-label="降智 · 自动删除：${reasons.join('；')}"><span>降智 · 自动删除</span></span>`;
}

function reviewDetail(tests = {}) {
  const review = tests.pelican?.review;
  if (!review) return tests.pelican ? '<p class="field-note">本记录尚无视觉审核结果。</p>' : '';
  const names = {pelican:'鹈鹕形态',bicycle:'自行车结构',riding:'脚与踏板接触',motion:'骑行动作'};
  const checks = Object.entries(review.checks || {}).map(([key,value]) => `${names[key] || key}：${value === true ? '通过' : value === false ? '未通过' : '无法判断'}`);
  return `<div class="review-detail"><strong>视觉审核</strong><p>${escapeHTML(review.reason || '鹈鹕形态、自行车结构及骑行动作通过审核')}</p><p>${escapeHTML(checks.join(' · '))}</p></div>`;
}

function testSummary(tests = {}, internal = true) {
  const names = {pelican:'鹈鹕',candy:'糖果'};
  const statuses = {passed:'通过',invalid:'未通过',error:'请求失败',queued:'排队中',running:'检测中',not_run:'未检测'};
  const review = tests.pelican?.review;
  const reviewLabels = {passed:'通过',invalid:'未通过',error:'异常',uncertain:'无法判断'};
  return '<div class="test-results">' + Object.entries(tests).map(([name, result]) => `<span class="test-result ${escapeHTML(result.status)}">${names[name] || name} · ${statuses[result.status] || '未检测'}${result.attempts > 1 ? ` · 尝试 ${result.attempts} 次` : ''}</span>`).join('') + (internal && tests.pelican ? `<span class="test-result ${escapeHTML(review?.status || '')}">视觉审核 · ${reviewLabels[review?.status] || (tests.pelican.status === 'running' ? '待完成' : '未审核')}</span>` : '') + '</div>';
}
function candyDetail(result, prompt) {
  if (!result) return '';
  return `<details class="candy-detail"><summary>糖果题回答与判分</summary><p class="field-note">判分规则：回答中出现独立数字 21 即通过。</p><pre>${escapeHTML(result.output || result.error || (result.status === 'not_run' ? '旧记录未进行糖果测试' : '等待回答'))}</pre>${prompt ? `<details><summary>本轮糖果题原文</summary><pre>${escapeHTML(prompt)}</pre></details>` : ''}</details>`;
}

function renderState() {
  const s = state.stats, config = state.settings;
  $('rate').innerHTML = `${s.completed ? (s.passed / s.completed * 100).toFixed(1) : '—'}<small>%</small>`;
  $('rate-meter').style.width = `${s.completed ? s.passed / s.completed * 100 : 0}%`;
  $('rate-note').textContent = s.completed ? `${s.passed} / ${s.completed} 次糖果题通过` : s.legacy ? `${s.legacy} 轮历史单项不计入通过率` : '等待第一份检测结果';
  $('total').innerHTML = `${s.total}<small>次</small>`;
  $('errors').innerHTML = `${s.errors + s.invalid}<small>次</small>`;
  $('error-note').textContent = `校验未通过 ${s.invalid} · 请求失败 ${s.errors}`;
  $('model-title').textContent = config.model;
  $('site-name').textContent = `${config.node_name || '当前节点'} · ${config.base_url}`;
  $('effort-label').textContent = config.effort;
  const latest = state.timeline.find(r => r.node_id === config.active_node_id && r.candy_status !== 'not_run');
  const currentRunning = latest?.candy_status === 'running';
  $('model-status').className = `badge ${currentRunning ? 'running' : latest?.candy_status || 'neutral'}`;
  $('model-status').textContent = currentRunning ? '糖果检测中' : latest ? candyLabels[latest.candy_status] : '尚未检测';
  $('last-run').textContent = latest ? date(latest.started) : '尚无记录';
  $('next-run').textContent = config.enabled ? date(config.next_run) : '尚未启用';
  $('schedule-note').textContent = config.enabled ? `每 ${config.interval_minutes} 分钟 · 后台自动运行` : '站点自动检测已暂停';
  const queued = state.queued_nodes || [];
  const queueStatus = $('queue-status');
  queueStatus.hidden = !queued.length;
  queueStatus.textContent = queued.length ? `排队中的节点（${queued.length}）：${queued.map(name => escapeHTML(name)).join('、')} · 将按顺序检测` : '';
  $('frequency').textContent = `${config.interval_minutes} 分钟 / 糖果题计入时间线`;
  $('footer-frequency').textContent = `每 15 秒同步记录 · 后台每 ${config.interval_minutes} 分钟检测`;
  $('run').disabled = guestBusy || !config.guest_enabled;
  if (!guestBusy) $('run').innerHTML = config.guest_enabled ? '<span aria-hidden="true">▷</span> 开始检测' : '访客检测暂未开放';
  renderTimeline();
  updateClock();
}

function updateClock() {
  if (!state?.settings.enabled) { $('countdown').textContent = '— : —'; return; }
  const left = Math.max(0, Math.ceil(state.settings.next_run - (Date.now() / 1000 + clockOffset)));
  $('countdown').textContent = left ? `${String(Math.floor(left / 60)).padStart(2,'0')} : ${String(left % 60).padStart(2,'0')}` : state.candy_running ? '糖果检测中' : '即将开始';
}

function renderTimeline() {
  const minutes = state.settings.interval_minutes;
  const interval = minutes * 60;
  const count = Math.ceil(86400 / interval);
  const end = (Math.floor(state.server_time / interval) + 1) * interval;
  $('timeline-summary').textContent = `仅糖果题 · ${count} 个时段`;
  $('timeline-interval').textContent = `每个色块 = ${minutes} 分钟`;
  $('timeline').style.gridTemplateColumns = `repeat(${count}, minmax(8px, 1fr))`;
  const fragments = document.createDocumentFragment();
  for (let i = 0; i < count; i++) {
    const start = end - (count - i) * interval;
    const group = state.timeline.filter(r => r.started >= start && r.started < start + interval);
    const status = ['running', 'error', 'invalid', 'passed', 'legacy'].find(s => group.some(r => r.candy_status === s)) || 'empty';
    const button = document.createElement('button');
    button.className = status;
    button.title = `${date(start)} · ${group.length} 次检测 · ${candyLabels[status] || '无糖果检测'}`;
    button.setAttribute('aria-label', button.title + '，查看详情');
    button.addEventListener('click', () => showSlot(start, group));
    fragments.append(button);
  }
  $('timeline').replaceChildren(fragments);
}

function showSlot(start, group) {
  if (group.length === 1) { showDetail(group[0].id); return; }
  detailSequence++;
  $('detail-title').textContent = `${date(start)} · ${group.length} 次检测`;
    $('detail-body').innerHTML = group.length ? group.map(r => `<button class="history-row" data-run="${r.id}"><span>${date(r.started)} · ${r.source === 'manual' ? '手动' : r.source === 'retry' ? '重试' : '定时'} · ${escapeHTML(r.scene)}</span>${candyBadge(r.candy_status)}</button>`).join('') : '<p class="empty-slot">这个时段没有检测记录。</p>';
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
  const filtered = ['filter', 'protocol-filter', 'source-filter', 'effort-filter', 'svg-filter']
    .some(id => $(id).value !== 'all');
  $('empty-state').hidden = galleryTotal > 0 || filtered;
  $('filter-empty').hidden = galleryTotal > 0 || !filtered;
  $('gallery-page').textContent = `第 ${galleryPage} / ${galleryPages} 页 · 共 ${galleryTotal} 条`;
  $('previous-page').disabled = polling || galleryPage <= 1;
  $('next-page').disabled = polling || galleryPage >= galleryPages;
  const signature = JSON.stringify([$('group-filter').value, visible.map(r => [r.id, r.status, r.has_svg, r.finished, r.tests, r.group_key])]);
  if ($('gallery').dataset.signature === signature) return;
  $('gallery').dataset.signature = signature;
  let lastGroup = null;
  $('gallery').innerHTML = visible.map(r => {
    const heading = $('group-filter').value !== 'none' && r.group_key !== lastGroup
      ? `<h3 class="gallery-group-title">${escapeHTML(r.group_key)}<span>${visible.filter(item => item.group_key === r.group_key).length} 条</span></h3>` : '';
    lastGroup = r.group_key;
    return heading + `<article class="run-card"><button class="preview" data-run="${r.id}" aria-label="${escapeHTML(date(r.started) + ' ' + r.scene)}，查看检测详情">${r.has_svg ? `<img data-image="${r.id}" alt="${escapeHTML(r.scene)} · 模型生成的鹈鹕骑行" loading="lazy">` : `<span class="no-preview ${escapeHTML(r.status)}"><b aria-hidden="true">${r.status === 'running' ? '◌' : '↯'}</b>${r.status === 'running' ? '模型正在创作' : '本轮未生成可展示动画'}</span>`}<span class="overlay">查看详情 ↗</span></button><div class="card-body"><div class="card-top"><time>${date(r.started)}</time>${badge(r.status)}</div>${testSummary(r.tests)}<p>节点：${escapeHTML(r.node_name || '历史节点')}</p><p>场景：${escapeHTML(r.scene)}</p><p class="card-model">${escapeHTML(r.model)} · ${escapeHTML(r.effort)} · ${r.finished ? `${Math.max(0, r.finished-r.started).toFixed(1)}s` : '进行中'}</p><p>${r.source === 'manual' ? '手动检测' : r.source === 'retry' ? '重试检测' : '定时检测'} · 校验码 ${r.checks.nonce ? '一致' : r.status === 'running' ? '待核对' : '未通过'}</p></div></article>`;
  }).join('');
  $('gallery').querySelectorAll('[data-run]').forEach(button => button.addEventListener('click', () => showDetail(Number(button.dataset.run))));
  $('gallery').querySelectorAll('.preview').forEach((button, i) => button.insertAdjacentHTML('beforeend', qualityTag(visible[i].tests)));
  $('gallery').querySelectorAll('[data-image]').forEach(img => attachImage(img, Number(img.dataset.image)));
}

async function showDetail(id) {
  const sequence = ++detailSequence;
  $('detail-title').textContent = '读取检测记录…';
  $('detail-body').replaceChildren();
  if (!$('detail-dialog').open) $('detail-dialog').showModal();
  try {
    const r = await api(`/api/runs/${id}`);
    if (sequence !== detailSequence) return;
    $('detail-title').textContent = `${r.scene} · ${date(r.started)}`;
    $('detail-body').innerHTML = `${r.has_svg ? '<img class="detail-image" id="detail-image" alt="模型生成的鹈鹕骑行 SVG 动画">' : ''}<div class="detail-meta">${badge(r.status)}<span>节点：${escapeHTML(r.node_name || '历史节点')}</span><span>${escapeHTML(r.model)} / ${escapeHTML(r.effort)}</span><span>${r.protocol === 'responses' ? 'Responses' : 'Chat Completions'}</span><span>${r.finished ? `${Math.max(0, r.finished-r.started).toFixed(1)} 秒` : '进行中'}</span><span>${r.source === 'manual' ? '手动检测' : '定时检测'} #${r.id}</span></div><div class="check-grid">${[['svg','SVG 格式'],['animation','动画声明'],['nonce','校验码']].map(([key,label]) => `<span class="${r.checks[key] ? '' : 'fail'}">${r.checks[key] ? '✓' : '—'} ${label}</span>`).join('')}<span>本轮校验码：${escapeHTML(r.nonce)}</span></div>${r.error ? `<p class="detail-error">${escapeHTML(r.error)}</p>` : ''}${testSummary(r.tests)}${candyDetail(r.tests?.candy, r.candy_prompt)}<p class="field-note">视觉审核是模型判断，可能存在误判；质量标签不代表模型身份。</p><details><summary>请求信息与用量</summary><pre>${escapeHTML(JSON.stringify({api:r.base_url,requested_model:r.model,returned_model:r.returned_model,effort:r.effort,protocol:r.protocol,usage:r.usage},null,2))}</pre></details><details><summary>本轮完整提示词</summary><pre>${escapeHTML(r.prompt)}</pre></details><details><summary>原始输出</summary><pre>${escapeHTML(r.output || '尚无输出')}</pre></details>${r.has_svg ? '<button class="button secondary" id="download-svg">下载 SVG 动画 ↓</button>' : ''}`;
    if (r.has_svg) {
      const frame = document.createElement('div');
      frame.className = 'review-image';
      $('detail-image').replaceWith(frame);
      frame.innerHTML = '<img class="detail-image" id="detail-image" alt="模型生成的鹈鹕骑行 SVG 动画">' + qualityTag(r.tests);
      attachImage($('detail-image'), id);
      $('download-svg').addEventListener('click', async () => {
        try { const link = document.createElement('a'); link.href = await imageURL(id); link.download = `pelican-${id}-${r.nonce}.svg`; link.click(); }
        catch (error) { message(error.message, true); }
      });
    }
    $('detail-body').insertAdjacentHTML('beforeend', (r.has_svg ? '' : qualityTag(r.tests, false)) + reviewDetail(r.tests));
  } catch (error) {
    if (sequence === detailSequence) { $('detail-title').textContent = '无法读取记录'; $('detail-body').textContent = error.message; }
  }
}

async function refresh(page = galleryPage) {
  if (polling) return;
  polling = true;
  $('refresh').disabled = true; $('previous-page').disabled = true; $('next-page').disabled = true;
  document.querySelectorAll('.gallery-filters select').forEach(select => select.disabled = true);
  try {
    const query = new URLSearchParams({page, status:$('filter').value, protocol:$('protocol-filter').value, source:$('source-filter').value, effort:$('effort-filter').value, has_svg:$('svg-filter').value, group_by:$('group-filter').value});
    const [newState, fresh, guests] = await Promise.all([api('/api/state'), api(`/api/runs?${query}`), api('/api/guest/results')]);
    renderGuestResults(guests);
    state = newState;
    clockOffset = state.server_time - Date.now()/1000;
    galleryPage = fresh.page; galleryPages = fresh.pages; galleryTotal = fresh.total;
    records = fresh.items;
    renderState(); renderGallery();
    $('connection').textContent = state.candy_running ? '糖果检测进行中' : state.settings.enabled ? '自动检测已启用' : '自动检测已暂停';
    $('connection').className = 'connection online';
  } catch (error) {
    $('connection').textContent = error.message === '请输入管理口令' ? '等待解锁' : '服务连接失败';
    $('connection').className = 'connection offline';
  } finally { polling = false; $('refresh').disabled = false; document.querySelectorAll('.gallery-filters select').forEach(select => select.disabled = false); renderGallery(); }
}

privacy.bind(document);
let guestSignature = "";
$('guest-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (guestBusy || !$('guest-form').reportValidity()) return;
  const values = {base_url:privacy.read($('base-url')), api_key:privacy.read($('api-key')), model:$('model').value.trim(), effort:$('effort').value, protocol:$('protocol').value, test_type:$('test-type').value,retry_count:Number($('guest-retries').value)};
  if (!values.base_url || !values.api_key) { message('请填写你自己的 API 地址和 API Key。', true); return; }
  guestBusy = true; $('run').disabled = true; $('run').textContent = '正在提交…';
  message('正在提交访客任务…');
  try {
    const result = await api('/api/guest/run', values);
    guestSignature = '';
    await refresh();
    message(`任务 #${result.id} 已提交`);
  } catch (error) { message(error.message, true); }
  finally {
    values.base_url = ''; values.api_key = '';
    guestBusy = false;
    $('run').disabled = state?.settings.guest_enabled === false;
    $('run').textContent = state?.settings.guest_enabled === false ? '访客检测暂未开放' : '开始检测';
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
  card.innerHTML = `<div class="guest-result-head"><strong>访客 #${escapeHTML(result.id)}</strong>${result.status === 'passed' && result.test_type !== 'both' ? '<span class="badge passed">单项通过</span>' : badge(result.status)}</div>${testSummary(result.tests, false)}<p>${date(result.submitted || result.started)} · ${escapeHTML(result.model)} · ${escapeHTML(result.effort)}</p><p>API：${escapeHTML(result.api_masked || "未记录")}</p><p>${result.tests.pelican ? `${escapeHTML(result.scene)} · 校验码${result.checks.nonce ? '一致' : ['queued','running'].includes(result.status) ? '待检测' : '未通过'} · ` : '糖果题 · '}${result.status === "queued" ? `排队中 · 第 ${result.queue_position || 1} 位` : result.finished ? `${result.started ? Math.max(0,result.finished-result.started).toFixed(1) : 0}s` : "正在检测"}</p>${candyDetail(result.tests?.candy)}${result.error ? `<p class="guest-error">${escapeHTML(result.error)}</p>` : ''}${result.has_svg ? '<button class="guest-preview" aria-label="放大本次访客动画"><img alt="本次访客测试生成的动画"></button><a class="guest-download" download="guest-pelican.svg">下载 SVG ↓</a>' : ''}`;
  let url;
  if (result.has_svg) {
    card.querySelector('.guest-preview').insertAdjacentHTML('beforeend', qualityTag(result.tests));
    url = `/api/guest/results/${result.id}/svg`;
    card.querySelector('img').src = url;
    card.querySelector('a').href = url;
    card.querySelector('button').addEventListener('click', () => {
      detailSequence++;
      $('detail-title').textContent = `访客 #${result.id} · ${result.scene}`;
      $('detail-body').innerHTML = '<div class="review-image"><img class="detail-image" alt="本次访客测试生成的动画">' + qualityTag(result.tests) + '</div><p class="field-note">API：' + escapeHTML(result.api_masked || '未记录') + '</p>' + candyDetail(result.tests?.candy);
      $('detail-body').querySelector('img').src = url;
      if (!$('detail-dialog').open) $('detail-dialog').showModal();
    });
  }
  if (!result.has_svg) card.insertAdjacentHTML('beforeend', qualityTag(result.tests, false));
  $('guest-results').append(card);
}

$('refresh').addEventListener('click', () => refresh());
$('filter').addEventListener('change', () => refresh(1));
$('protocol-filter').addEventListener('change', () => refresh(1));
$('source-filter').addEventListener('change', () => refresh(1));
$('effort-filter').addEventListener('change', () => refresh(1));
$('svg-filter').addEventListener('change', () => refresh(1));
$('group-filter').addEventListener('change', () => refresh(1));
$('previous-page').addEventListener('click', () => refresh(galleryPage - 1));
$('next-page').addEventListener('click', () => refresh(galleryPage + 1));
$('close-detail').addEventListener('click', () => { detailSequence++; $('detail-dialog').close(); });
$('detail-dialog').addEventListener('cancel', () => detailSequence++);
setInterval(updateClock, 1000);
setInterval(refresh, 15000);
refresh();
const linkedRun = new URLSearchParams(location.search).get('run');
if (/^[1-9]\d{0,17}$/.test(linkedRun || '')) showDetail(Number(linkedRun));
