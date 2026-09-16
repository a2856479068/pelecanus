const $ = id => document.getElementById(id);
let token = '', busy = false, config, setup = false, nodes = [], editingNode = null, nodeSignature = '';
let nodeSearch = '', nodeStatus = 'all';
let runHistory = [], batchRunIds = new Set(), runStats;
const selectedNodes = new Set();
const labels = {passed:'双项通过', error:'请求失败', invalid:'校验未通过', running:'检测中', queued:'排队中', legacy:'历史单项'};
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
privacy.bind(document);
async function api(path, body) {
  const response = await fetch(path, {method:body === undefined ? 'GET' : 'POST', headers:{...(body === undefined ? {} : {'Content-Type':'application/json'}), ...(token ? {Authorization:`Bearer ${token}`} : {})}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  if (response.status === 401 && !path.startsWith('/api/auth/')) {
    token = ''; $('logout').hidden = true;
    $('admin-content').hidden = true;
    if (!$('auth-dialog').open) $('auth-dialog').showModal();
    throw new Error('请输入正确的管理口令');
  }
  const data = await response.json().catch(() => ({error:`服务返回异常响应（HTTP ${response.status}），请刷新后重试`}));
  if (!response.ok) throw new Error(data.error || '操作失败');
  return data;
}
function message(text, error = false) { $('admin-message').textContent = text; $('admin-message').classList.toggle('error',error); }
function filteredNodes() {
  return nodes.filter(n => {
    const haystack = `${n.name} ${n.model} ${n.base_url}`.toLowerCase();
    const matchesSearch = !nodeSearch || haystack.includes(nodeSearch.toLowerCase());
    const matchesStatus = nodeStatus === 'all' || (nodeStatus === 'enabled' && n.enabled) || (nodeStatus === 'disabled' && !n.enabled) || (nodeStatus === 'unconfigured' && !n.has_key);
    return matchesSearch && matchesStatus;
  });
}
function showConfig(data) {
  config = data;
  $('interval').value = config.interval_minutes;
  $('timeout').value = config.timeout_seconds;
  $('retry-count').value = config.retry_count;
  $('max-tokens').value = config.max_output_tokens;
  $('admin-enabled').checked = config.enabled;
  $('guest-enabled').checked = config.guest_enabled;
  $('admin-content').hidden = false;
  $('logout').hidden = false;
  $('admin-status').className = `badge ${config.enabled ? 'passed' : 'neutral'}`;
  $('admin-status').textContent = `${config.node_name} · ${config.enabled ? '自动检测已启用' : '自动检测已暂停'}`;
  $('admin-next').textContent = config.enabled ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(config.next_run*1000)) : '尚未启用';
  $('admin-run').disabled = !config.has_key;
}
function renderNodes() {
  selectedNodes.forEach(id => { if (!nodes.some(n => n.id === id && n.enabled && n.has_key)) selectedNodes.delete(id); });
  const filtered = filteredNodes();
  const signature = JSON.stringify([nodes,busy,nodeSearch,nodeStatus,[...selectedNodes].sort((a,b) => a-b)]);
  if (signature === nodeSignature) return;
  nodeSignature = signature;
  const running = nodes.some(n => n.last_run?.status === 'running');
  $('node-list').innerHTML = filtered.length ? filtered.map(n => `<article class="node-row ${n.active ? 'active-node' : ''}"><label class="node-select"><input type="checkbox" data-select="${n.id}" ${selectedNodes.has(n.id) ? 'checked' : ''} ${busy || !n.enabled || !n.has_key ? 'disabled' : ''}> 选择</label><div class="node-info"><h3>${escapeHTML(n.name)} ${n.active ? '<span class="badge passed">当前节点</span>' : ''} ${!n.enabled ? '<span class="badge neutral">已停用</span>' : ''}</h3><p>${escapeHTML(n.base_url)} · ${escapeHTML(n.model)} · ${escapeHTML(n.effort)} · ${n.protocol === 'responses' ? 'Responses' : 'Chat Completions'}</p><p>密钥 ${escapeHTML(n.api_key_masked)}</p></div><div class="node-actions"><button class="button secondary" type="button" data-edit="${n.id}" ${busy ? 'disabled' : ''}>编辑</button><button class="button secondary" type="button" data-copy="${n.id}" ${busy ? 'disabled' : ''}>复制</button><button class="button secondary" type="button" data-toggle="${n.id}" ${busy || (n.active && n.enabled) ? 'disabled' : ''}>${n.enabled ? '停用' : '启用'}</button><button class="button secondary" type="button" data-activate="${n.id}" ${busy || n.active || !n.enabled || !n.has_key ? 'disabled' : ''}>${n.active ? '使用中' : '设为当前'}</button><button class="button primary" type="button" data-test="${n.id}" ${busy || running || !n.enabled || !n.has_key ? 'disabled' : ''}>测试一次</button></div><div class="node-result">${n.last_run ? `<span class="badge ${escapeHTML(n.last_run.status)}">${labels[n.last_run.status] || '尚未检测'}</span><span>最近一轮 #${n.last_run.id} · ${new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(n.last_run.started*1000))}</span><a href="/?run=${n.last_run.id}" target="_blank" rel="noopener">查看结果 ↗</a>${n.last_run.error ? `<p>${escapeHTML(n.last_run.error)}</p>` : ''}` : '<span>尚未检测</span>'}</div></article>`).join('') : '<p class="filter-empty">没有符合当前筛选条件的节点。</p>';
  $('selected-node-count').textContent = `已选 ${selectedNodes.size} 个`;
  $('bulk-run').disabled = busy || !selectedNodes.size;
  $('admin-run').disabled = busy || running || !config?.has_key;
  $('add-node').disabled = busy;
  const selectable = filtered.filter(n => n.enabled && n.has_key);
  const selectedVisible = selectable.filter(n => selectedNodes.has(n.id)).length;
  $('select-all-nodes').checked = selectable.length > 0 && selectedVisible === selectable.length;
  $('select-all-nodes').indeterminate = selectedVisible > 0 && selectedVisible < selectable.length;
}
async function refreshNodes() { nodes = await api('/api/admin/nodes'); renderNodes(); }
const dateTime = value => value ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value*1000)) : '—';
function renderRunMonitor() {
  const tracked = runHistory.filter(run => batchRunIds.has(run.id));
  const finished = tracked.filter(run => run.status !== 'running');
  const running = runHistory.filter(run => run.status === 'running');
  const summary = tracked.length ? `${finished.length}/${tracked.length} 已完成` : (running.length ? `${running.length} 个节点检测中` : '暂无检测任务');
  $('admin-run-progress').innerHTML = `<strong>${escapeHTML(summary)}</strong>${tracked.length ? `<progress max="${tracked.length}" value="${finished.length}" aria-label="批量检测进度"></progress>` : ''}${running.length ? `<span class="run-current">当前：${running.map(run => escapeHTML(run.node_name || `第 ${run.id} 轮`)).join('、')}</span>` : ''}`;
  const statItems = runStats ? [['总计',runStats.total,'neutral'],['通过',runStats.passed,'passed'],['校验失败',runStats.invalid,'invalid'],['请求失败',runStats.failed,'error'],['启用节点',runStats.enabled_nodes,'running']] : [];
  $('admin-stats').innerHTML = statItems.map(([name,value,tone]) => `<div><span>${name}</span><strong class="${tone}">${value}</strong></div>`).join('');
  $('admin-run-history').innerHTML = runHistory.length ? `<h3>最近检测记录</h3>${runHistory.slice(0,12).map(run => `<div class="admin-history-row"><span class="badge ${escapeHTML(run.status)}">${escapeHTML(labels[run.status] || run.status)}</span><span>#${run.id} · ${escapeHTML(run.node_name || '历史节点')} · ${dateTime(run.started)}</span><a href="/?run=${run.id}" target="_blank" rel="noopener">查看 ↗</a>${['error','invalid'].includes(run.status) ? `<button class="button secondary" type="button" data-retry="${run.id}">重试</button>` : ''}</div>`).join('')}` : '<p class="field-note">暂无检测历史。</p>';
}
async function refreshRuns() { [runHistory, runStats] = await Promise.all([api('/api/admin/runs'), api('/api/admin/stats')]); renderRunMonitor(); }
function editNode(id = null) {
  const node = nodes.find(n => n.id === id);
  editingNode = id;
  $('node-title').textContent = node ? '编辑节点' : '新增节点';
  $('node-name').value = node?.name || '';
  $('saved-url').textContent = node?.base_url || '尚未配置';
  $('saved-key').textContent = node?.api_key_masked || '尚未配置';
  $('admin-url').required = !node; $('admin-key').required = !node;
  $('admin-url').placeholder = node ? '留空保留当前地址' : 'example.com 或 https://example.com/v1';
  $('admin-key').placeholder = node ? '留空保留当前密钥' : '填写该节点的 API Key';
  $('admin-model').value = node?.model || 'gpt-6-astra';
  $('admin-effort').value = node?.effort || 'medium';
  $('admin-protocol').value = node?.protocol || 'responses';
  privacy.clear($('admin-url')); privacy.clear($('admin-key'));
  $('node-message').textContent = '';
  $('node-dialog').showModal();
}
$('add-node').addEventListener('click', () => editNode());
$('node-cancel').addEventListener('click', () => $('node-dialog').close());
$('node-dialog').addEventListener('close', () => { privacy.clear($('admin-url')); privacy.clear($('admin-key')); });
$('node-list').addEventListener('click', event => {
  const checkbox = event.target.closest('input[data-select]');
  if (checkbox) { checkbox.checked ? selectedNodes.add(Number(checkbox.dataset.select)) : selectedNodes.delete(Number(checkbox.dataset.select)); renderNodes(); return; }
  const button = event.target.closest('button');
  if (!button || busy) return;
  if (button.dataset.edit) return editNode(Number(button.dataset.edit));
  action(async () => {
    if (button.dataset.copy) {
      await api(`/api/admin/nodes/${button.dataset.copy}/copy`, {}); await refreshNodes(); message('节点副本已创建。');
    } else if (button.dataset.toggle) {
      const node = nodes.find(n => n.id === Number(button.dataset.toggle));
      await api(`/api/admin/nodes/${button.dataset.toggle}/${node.enabled ? 'disable' : 'enable'}`, {}); await refreshNodes(); message(node.enabled ? '节点已停用。' : '节点已启用。');
    } else if (button.dataset.activate) {
      showConfig(await api(`/api/admin/nodes/${button.dataset.activate}/activate`,{}));
      message(`已切换到「${config.node_name}」。${config.enabled ? '后续定时检测使用该节点。' : '自动检测仍处于暂停状态。'}`);
    } else if (button.dataset.test) {
      const result = await api(`/api/admin/nodes/${button.dataset.test}/run`,{});
      message(`第 ${result.id} 轮检测已开始，结果将在对应节点下更新；当前节点保持不变。`);
    }
    await refreshNodes();
  });
});
$('node-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !$('node-form').reportValidity()) return;
  busy = true; $('node-save').disabled = true; $('node-cancel').disabled = true; renderNodes();
  const values = {name:$('node-name').value.trim(),base_url:privacy.read($('admin-url')),api_key:privacy.read($('admin-key')),model:$('admin-model').value.trim(),effort:$('admin-effort').value,protocol:$('admin-protocol').value};
  try {
    await api(editingNode === null ? '/api/admin/nodes' : `/api/admin/nodes/${editingNode}`,values);
    $('node-dialog').close();
    showConfig(await api('/api/admin/settings')); await refreshNodes();
    message('节点已保存。可设为当前节点，或单独测试一次。');
  } catch (error) { $('node-message').textContent = error.message; }
  finally { values.base_url = ''; values.api_key = ''; busy = false; $('node-save').disabled = false; $('node-cancel').disabled = false; renderNodes(); }
});
async function load() {
  $('admin-content').hidden = true;
  $('logout').hidden = true;
  try {
    const status = await api('/api/auth/status');
    setup = !status.configured;
    $('auth-title').textContent = setup ? '首次设置管理密码' : '登录后台管理';
    $('auth-description').textContent = setup ? (status.setup_allowed ? '设置 12 位以上的密码。以后每次进入后台都需要登录。' : '请先在服务所在机器打开后台设置密码。') : '请输入管理密码。登录状态最长保留 2 小时。';
    $('admin-token').minLength = setup ? 12 : 1;
    $('admin-token').autocomplete = setup ? 'new-password' : 'current-password';
    $('confirm-password-row').hidden = !setup;
    $('confirm-password').required = setup;
    $('auth-submit').textContent = setup ? '设置密码并登录' : '登录';
    $('auth-submit').disabled = setup && !status.setup_allowed;
    $('admin-status').textContent = setup ? '等待设置管理密码' : '等待登录';
    if (!$('auth-dialog').open) $('auth-dialog').showModal();
  } catch (error) { $('admin-status').textContent = error.message; }
}
async function action(fn) {
  if (busy) return;
  busy = true; $('admin-save').disabled = true; renderNodes();
  try { await fn(); }
  catch (error) { message(error.message,true); }
  finally { busy = false; $('admin-save').disabled = false; renderNodes(); }
}
$('admin-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!$('admin-form').reportValidity()) return;
  action(async () => {
    const values = {retry_count:Number($('retry-count').value),interval_minutes:Number($('interval').value), timeout_seconds:Number($('timeout').value), max_output_tokens:Number($('max-tokens').value), enabled:$('admin-enabled').checked, guest_enabled:$('guest-enabled').checked};
    const result = await api('/api/admin/settings',values);
    showConfig(result);
    message('配置已保存并生效。后续检测使用新配置，无需重启。');
  });
});
$('admin-run').addEventListener('click', () => action(async () => {
  const result = await api('/api/admin/run',{});
  await refreshNodes();
  message(`后台第 ${result.id} 轮检测已开始，可在公开页面查看结果。`);
}));
$('auth-form').addEventListener('submit', async event => {
  event.preventDefault();
  const password = $('admin-token').value;
  if (setup && password !== $('confirm-password').value) { $('auth-error').textContent = '两次输入的密码不一致。'; return; }
  $('auth-submit').disabled = true;
  try {
    if (setup) { await api('/api/auth/setup',{password}); setup = false; }
    const session = await api('/api/auth/login',{password});
    token = session.token;
    showConfig(await api('/api/admin/settings'));
    await refreshNodes();
    await refreshRuns();
    $('auth-dialog').close(); $('admin-token').value = ''; $('confirm-password').value = ''; $('auth-error').textContent = '';
  } catch (error) { token = ''; $('auth-error').textContent = error.message; }
  finally { $('auth-submit').disabled = false; }
});
$('logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout',{}); }
  finally {
    token = ''; config = undefined; nodes = []; selectedNodes.clear(); nodeSignature = ''; $('node-list').replaceChildren();
    privacy.clear($('admin-url')); privacy.clear($('admin-key'));
    $('admin-content').hidden = true; $('logout').hidden = true;
    await load();
  }
});
$('select-all-nodes').addEventListener('change', event => {
  filteredNodes().filter(n => n.enabled && n.has_key).forEach(n => event.target.checked ? selectedNodes.add(n.id) : selectedNodes.delete(n.id));
  renderNodes();
});
$('node-search').addEventListener('input', event => { nodeSearch = event.target.value.trim(); renderNodes(); });
$('node-status-filter').addEventListener('change', event => { nodeStatus = event.target.value; renderNodes(); });
$('admin-run-history').addEventListener('click', event => {
  const button = event.target.closest('[data-retry]');
  if (!button || busy) return;
  action(async () => {
    const result = await api(`/api/admin/runs/${button.dataset.retry}/retry`, {});
    batchRunIds.add(result.id);
    await Promise.all([refreshRuns(), refreshNodes()]);
    message(`已创建重试检测 #${result.id}。`);
  });
});
$('bulk-run').addEventListener('click', () => action(async () => {
  const ids = [...selectedNodes];
  let started = 0;
  let failed = 0;
  const deadline = Date.now() + 30 * 60 * 1000;
  for (const id of ids) {
    try { const result = await api(`/api/admin/nodes/${id}/run`, {}); batchRunIds.add(result.id); started++; await refreshRuns(); }
    catch (error) { failed++; if (started === 0) throw error; }
    while ((await api('/api/admin/nodes')).some(node => node.last_run?.status === 'running')) {
      if (Date.now() > deadline) throw new Error('批量检测等待超时，请刷新页面查看已完成结果');
      await new Promise(resolve => setTimeout(resolve, 2000));
      await refreshRuns();
    }
  }
  selectedNodes.clear(); $('select-all-nodes').checked = false; await refreshRuns(); await refreshNodes(); message(`已提交 ${started} 个节点的检测${failed ? `，${failed} 个节点提交失败` : ''}；服务会按单实例锁顺序执行。`, failed > 0);
}));
load();
setInterval(() => { if (token && !busy && !$('node-dialog').open) Promise.all([refreshNodes(), refreshRuns()]).catch(error => message(error.message,true)); }, 5000);
