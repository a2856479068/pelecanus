if (location.hash === '#run-records') location.replace('/admin/records');
const $ = id => document.getElementById(id);
let authenticated = false, busy = false, config, setup = false, nodes = [], editingNode = null, nodeSignature = '';
let nodeSearch = '', nodeStatus = 'all';
const selectedNodes = new Set();
let codexModels = [], codexCombinations = [], codexFingerprint = '', codexRefreshing = false;
let loopGroups = [], editingGroupId = null, groupBusy = false;
let testingState = null, stoppingTests = false, batchCancelled = false;
let formDirty = false, liveSignature = '', liveAuthLoading = false;
const effortOrder = ['none','minimal','low','medium','high','xhigh','max','ultra'];
const effortLabels = {none:'none · 无',minimal:'minimal · 极低',low:'low · 低',medium:'medium · 中',high:'high · 高',xhigh:'xhigh · 极高',max:'max · 最大',ultra:'ultra · 超高'};
function derivedCombinations(models) {
  return models.flatMap(model => {
    const efforts = Array.isArray(model.efforts) && model.efforts.length ? model.efforts : effortOrder;
    return efforts.filter(effort => effortOrder.includes(effort)).map(effort => ({model:model.model, effort}));
  });
}
function normalizeCombinations(combinations, models) {
  const knownModels = new Set(models.map(model => model.model));
  const explicit = Array.isArray(combinations);
  const pairs = explicit ? combinations.filter(pair => pair && knownModels.has(pair.model) && effortOrder.includes(pair.effort)) : [];
  const unique = [...new Map(pairs.map(pair => [`${pair.model}\u0000${pair.effort}`, pair])).values()];
  return explicit ? unique : derivedCombinations(models);
}
function modelCatalogKey(models, combinations = []) {
  return models.map(model => [model.model, model.name, ...(Array.isArray(model.efforts) ? [...model.efforts].sort() : []), model.default_effort || '']
    .join('\u0001')).sort().join('\u0002') + '|' + combinations.map(pair => `${pair.model}:${pair.effort}`).sort().join(',');
}
async function refreshCodex(forceRefresh = false) {
  if (codexRefreshing) return;
  codexRefreshing = true;
  $('refresh-codex').disabled = true;
  const loginHint = $('codex-login-hint');
  const previousModels = codexModels;
  const previousKey = modelCatalogKey(previousModels, codexCombinations);
  if (forceRefresh || !codexFingerprint) $('codex-status').textContent = forceRefresh ? '正在刷新当前账号与模型…' : '正在读取登录状态与模型列表…';
  try {
    const result = await api(`/api/admin/codex${forceRefresh ? `?refresh=1&_=${Date.now()}` : ''}`);
    const fetchedModels = Array.isArray(result.models) ? result.models.filter(model => model && typeof model.model === 'string' && model.model) : [];
    const fetchedCombinations = normalizeCombinations(result.combinations, fetchedModels);
    codexModels = fetchedModels;
    codexCombinations = fetchedCombinations;
    const catalogChanged = previousKey !== modelCatalogKey(codexModels, codexCombinations);
    const switched = codexFingerprint && result.account_fingerprint && codexFingerprint !== result.account_fingerprint;
    codexFingerprint = result.account_fingerprint || '';
    const identity = result.account_label ? ` · ${result.account_label}` : '';
    const change = forceRefresh && !result.model_error ? (catalogChanged ? ` · 组合定义已更新（${previousModels.length} → ${codexModels.length} 个模型，${codexCombinations.length} 个有效组合）` : ' · 模型与组合定义没有变化') : '';
    const checked = result.checked_at ? ` · ${new Date(result.checked_at * 1000).toLocaleTimeString('zh-CN', {hour12:false})} 已核对` : '';
    $('codex-status').textContent = (switched ? '检测到个人账号已切换' : result.message) + identity + (result.model_error ? ' · ' + result.model_error : ` · ${codexModels.length} 个可用模型，${codexCombinations.length} 个有效组合`) + change + checked;
    loginHint.textContent = result.logged_in ? '' : result.installed
      ? '未检测到 ChatGPT 账号登录。请在本机终端运行 codex login，选择 ChatGPT 账号；完成后点击“刷新账号与模型”。'
      : '未找到可用的 Codex CLI。请先安装 Codex CLI，在本机终端运行 codex login 并选择 ChatGPT 账号；完成后点击“刷新账号与模型”。';
    loginHint.hidden = result.logged_in;
    $('codex-model-select').replaceChildren(new Option('手动填写模型名称', ''), ...codexModels.map(m => new Option(m.name, m.model)));
    renderPrimaryModelSettings($('primary-model').value || config?.model);
    if (!groupBusy && (forceRefresh || catalogChanged || switched)) refreshGroupPairOptions();
    renderGroups();
    if ($('node-dialog').open) updateProvider();
  } catch (error) {
    codexFingerprint = ''; codexModels = []; codexCombinations = [];
    $('codex-status').textContent = error.message;
    loginHint.textContent = '读取本机 Codex 登录状态失败，请检查服务连接后点击“刷新账号与模型”。';
    loginHint.hidden = false;
    renderPrimaryModelSettings(); renderGroups();
  }
  finally { codexRefreshing = false; $('refresh-codex').disabled = false; }
}
function updateProvider() {
  const local = $('admin-protocol').value === 'codex';
  $('api-credentials').hidden = local;
  $('admin-url').disabled = local; $('admin-key').disabled = local;
  const previous = nodes.find(n => n.id === editingNode);
  $('admin-url').required = !local && (!previous || previous.protocol === 'codex');
  $('admin-key').required = !local && (!previous || previous.protocol === 'codex');
  $('codex-node-note').hidden = !local;
  $('codex-model-label').hidden = !local;
  $('codex-model-select').hidden = !local;
  $('codex-model-select').disabled = !local;
  $('codex-model-select').value = codexModels.some(m => m.model === $('admin-model').value) ? $('admin-model').value : '';
  const found = codexModels.find(m => m.model === $('admin-model').value);
  const efforts = local && found ? supportedComboEfforts(found) : effortOrder;
  const current = $('admin-effort').value;
  $('admin-effort').replaceChildren(...efforts.map(e => new Option(e, e)));
  $('admin-effort').value = efforts.includes(current) ? current : found?.default_effort || efforts[0];
}
function renderPrimaryModelSettings(selectedModel = config?.model) {
  const local = config?.protocol === 'codex';
  const selectedEffort = $('primary-effort').value || config?.effort;
  const select = $('primary-model');
  if (!select) return;
  const models = [...codexModels];
  if (local && !codexModels.length && selectedModel) {
    models.unshift({model:selectedModel, name:`${selectedModel}（当前配置）`, efforts:[], default_effort:config.effort});
  }
  select.replaceChildren(new Option(codexModels.length ? '选择模型' : '暂无可选模型', ''), ...models.map(model => new Option(model.name, model.model)));
  $('primary-model-settings').hidden = !local;
  $('active-model-hint').textContent = local
    ? (codexModels.length ? '单个模式每轮使用此模型与强度生成一次。' : '请刷新当前账号的模型列表。')
    : '当前使用 API 节点；请在下方高级设置中管理模型。';
  select.value = local && models.some(model => model.model === selectedModel) ? selectedModel : '';
  select.disabled = !local || !codexModels.length;
  select.required = local && codexModels.length > 0;
  $('primary-effort-wrap').hidden = !local;
  $('primary-effort').disabled = !local || (codexModels.length > 0 && !codexModels.some(model => model.model === select.value));
  renderPrimaryEfforts(select.value || config?.model, selectedEffort);
  updateModelDraftHint();
  renderScheduleMode();
}
function renderPrimaryEfforts(modelId, preferredEffort = $('primary-effort').value || config?.effort) {
  const model = codexModels.find(item => item.model === modelId);
  const efforts = model ? supportedComboEfforts(model) : effortOrder;
  const select = $('primary-effort');
  select.replaceChildren(...efforts.map(effort => new Option(effort, effort)));
  select.value = efforts.includes(preferredEffort) ? preferredEffort : model?.default_effort || efforts[0];
}
function supportedComboEfforts(model) {
  const defined = codexCombinations.filter(pair => pair.model === model.model).map(pair => pair.effort);
  if (codexCombinations.length) return [...new Set(defined)];
  // Older servers do not send explicit pairs. Keep the fallback compatible
  // while the page is waiting for the first catalog response.
  return Array.isArray(model.efforts) && model.efforts.length ? model.efforts : effortOrder;
}
function groupPairs() {
  return [...$('group-pair-list').querySelectorAll('.group-pair-row')].map(row => ({
    model:row.querySelector('[data-group-model]').value,
    effort:row.querySelector('[data-group-effort]').value
  }));
}
function fillPairEfforts(row, preferred) {
  const model = codexModels.find(item => item.model === row.querySelector('[data-group-model]').value);
  const allowed = model ? supportedComboEfforts(model) : [];
  const select = row.querySelector('[data-group-effort]');
  select.replaceChildren(...allowed.map(effort => new Option(effortLabels[effort], effort)));
  if (preferred && !allowed.includes(preferred)) {
    const unavailable = new Option(`${preferred}（当前不可用）`, preferred);
    unavailable.disabled = true;
    select.add(unavailable);
  }
  select.value = preferred || (allowed.includes(model?.default_effort) ? model.default_effort : allowed[0]) || '';
}
function addGroupPair(pair = {}) {
  const row = document.createElement('div');
  row.className = 'group-pair-row';
  row.innerHTML = '<label>模型<select data-group-model required></select></label><label>思考强度<select data-group-effort required></select></label><button class="button secondary" type="button" data-remove-pair>移除</button>';
  const select = row.querySelector('[data-group-model]');
  select.replaceChildren(new Option('选择模型', ''), ...codexModels.map(model => new Option(model.name || model.model, model.model)));
  if (pair.model && !codexModels.some(model => model.model === pair.model)) {
    const unavailable = new Option(`${pair.model}（当前不可用）`, pair.model);
    unavailable.disabled = true;
    select.add(unavailable);
  }
  select.value = pair.model || '';
  fillPairEfforts(row, pair.effort);
  select.addEventListener('change', () => { fillPairEfforts(row); updateGroupSummary(); });
  row.querySelector('[data-group-effort]').addEventListener('change', updateGroupSummary);
  row.querySelector('[data-remove-pair]').addEventListener('click', () => { row.remove(); updateGroupSummary(); });
  $('group-pair-list').append(row);
  updateGroupSummary();
}
function refreshGroupPairOptions() {
  const pairs = groupPairs();
  $('group-pair-list').replaceChildren();
  (pairs.length ? pairs : [{}]).forEach(addGroupPair);
}
function updateGroupSummary() {
  const pairs = groupPairs();
  const valid = pairs.length > 0 && pairs.every(pair => codexCombinations.some(allowed => allowed.model === pair.model && allowed.effort === pair.effort));
  const count = new Set(pairs.map(pair => `${pair.model}/${pair.effort}`)).size;
  $('group-selection-summary').textContent = !codexModels.length ? '请先刷新账号模型列表。' : valid
    ? `本组 ${count} 个组合，按行顺序执行；同组重复组合只保存一次。`
    : '请为每一行选择可用的模型与强度。';
  $('save-loop-group').disabled = groupBusy || !valid;
  $('add-group-pair').disabled = groupBusy || pairs.length >= 100;
}
function editGroup(group = null) {
  editingGroupId = group?.id ?? null;
  $('group-editor-title').textContent = group ? `编辑：${group.name}` : '新建循环组';
  $('loop-group-name').value = group?.name || '';
  $('loop-group-enabled').checked = group?.enabled ?? true;
  $('group-pair-list').replaceChildren();
  const members = group?.members || [];
  (members.length ? members : [{}]).forEach(addGroupPair);
  $('group-result').textContent = '';
}
function renderGroups() {
  $('loop-group-list').innerHTML = loopGroups.length ? loopGroups.map(group => `<article class="loop-group-card"><div><h3>${escapeHTML(group.name)} <span class="badge ${group.enabled ? 'success' : 'neutral'}">${group.enabled ? '启用' : '停用'}</span></h3><ol>${group.members.map(pair => `<li>${escapeHTML(pair.model)} · ${escapeHTML(pair.effort)}${codexModels.length && !codexCombinations.some(allowed => allowed.model === pair.model && allowed.effort === pair.effort) ? ' <span class="error">（当前账号不可用，请编辑）</span>' : ''}</li>`).join('')}</ol></div><div class="action-buttons"><button class="button secondary" type="button" data-edit-group="${group.id}">编辑</button><button class="button secondary" type="button" data-toggle-group="${group.id}">${group.enabled ? '停用' : '启用'}</button><button class="button secondary delete-art-button" type="button" data-delete-group="${group.id}">删除组</button></div></article>`).join('') : '<p class="field-note">尚无循环组，添加组合后保存即可。</p>';
  $('loop-group-list').querySelectorAll('button').forEach(button => { button.disabled = groupBusy; });
}
async function refreshGroups() {
  loopGroups = await api('/api/admin/groups');
  renderGroups();
}
async function groupAction(fn) {
  if (groupBusy) return;
  groupBusy = true;
  $('group-result').textContent = '正在保存…';
  $('group-result').classList.remove('error');
  renderGroups();
  updateGroupSummary();
  try { await fn(); }
  catch (error) { $('group-result').textContent = error.message; $('group-result').classList.add('error'); }
  finally { groupBusy = false; renderGroups(); updateGroupSummary(); }
}
$('refresh-codex').addEventListener('click', () => refreshCodex(true));
$('new-loop-group').addEventListener('click', () => { if (!groupBusy) editGroup(); });
$('add-group-pair').addEventListener('click', () => { if (!groupBusy) addGroupPair(); });
$('loop-group-form').addEventListener('submit', event => {
  event.preventDefault();
  if ($('save-loop-group').disabled || !$('loop-group-form').reportValidity()) return;
  groupAction(async () => {
    const saved = await api(editingGroupId === null ? '/api/admin/groups' : `/api/admin/groups/${editingGroupId}`, {
      name:$('loop-group-name').value.trim(), enabled:$('loop-group-enabled').checked, pairs:groupPairs()
    });
    await Promise.all([refreshGroups(), refreshNodes()]);
    editGroup(saved);
    $('group-result').textContent = `「${saved.name}」已保存，包含 ${saved.members.length} 个独立组合。`;
  });
});
$('loop-group-list').addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button || groupBusy) return;
  const id = Number(button.dataset.editGroup || button.dataset.toggleGroup || button.dataset.deleteGroup);
  const group = loopGroups.find(item => item.id === id);
  if (!group) return;
  if (button.dataset.editGroup) { editGroup(group); return; }
  if (button.dataset.deleteGroup && !confirm(`删除循环组「${group.name}」？已生成的作品和记录会保留。`)) return;
  groupAction(async () => {
    const operation = button.dataset.deleteGroup ? 'delete' : group.enabled ? 'disable' : 'enable';
    await api(`/api/admin/groups/${id}/${operation}`, {});
    await Promise.all([refreshGroups(), refreshNodes()]);
    if (editingGroupId === id) editGroup(loopGroups.find(item => item.id === id));
    $('group-result').textContent = operation === 'delete' ? '循环组已删除，历史作品已保留。' : operation === 'disable' ? '循环组已停用，等待中的组合已移出队列；正在生成的请求会完成。' : '循环组已启用，将在下一轮运行。';
  });
});
function renderScheduleMode() {
  const groups = $('schedule-mode').value === 'groups';
  $('primary-model-settings').hidden = groups || config?.protocol !== 'codex';
  $('active-model-hint').hidden = groups;
  $('loop-mode-hint').hidden = !groups;
  $('admin-run').hidden = false;
  $('primary-model').required = !groups && config?.protocol === 'codex' && codexModels.length > 0;
  document.querySelector('.combo-panel').hidden = !groups;
}
$('schedule-mode').addEventListener('change', renderScheduleMode);
$('primary-model').addEventListener('change', () => {
  const modelId = $('primary-model').value;
  $('primary-effort').disabled = codexModels.length > 0 && !codexModels.some(model => model.model === modelId);
  renderPrimaryEfforts(modelId, $('primary-effort').value);
  updateModelDraftHint();
});
$('primary-effort').addEventListener('change', updateModelDraftHint);
$('admin-protocol').addEventListener('change', updateProvider);
$('admin-model').addEventListener('change', updateProvider);
$('codex-model-select').addEventListener('change', () => {
  if ($('codex-model-select').value) { $('admin-model').value = $('codex-model-select').value; updateProvider(); }
});
const labels = {success:'请求成功', error:'请求失败', cancelled:'手动取消', running:'生成中', queued:'排队中'};
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
privacy.bind(document);
async function api(path, body) {
  const response = await fetch(path, {cache:'no-store', method:body === undefined ? 'GET' : 'POST', credentials:'same-origin', headers:body === undefined ? {} : {'Content-Type':'application/json'}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  if (response.status === 401 && !path.startsWith('/api/auth/')) {
    authenticated = false; $('logout').hidden = true;
    $('admin-content').hidden = true;
    if (!$('auth-dialog').open) $('auth-dialog').showModal();
    throw new Error('请输入正确的管理口令');
  }
  const data = await response.json().catch(() => ({error:`服务返回异常响应（HTTP ${response.status}），请刷新后重试`}));
  if (!response.ok) throw new Error(data.error || '操作失败');
  if (body !== undefined) PelicanLive.invalidate();
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
  formDirty = false;
  $('schedule-mode').value = config.schedule_mode || 'single';
  $('interval').value = config.interval_seconds;
  $('task-prompt-editor').value = config.task_prompt;
  $('timeout').value = config.timeout_seconds;
  $('retry-count').value = config.retry_count;
  $('max-tokens').value = config.max_output_tokens;
  $('admin-enabled').checked = config.enabled;
  $('guest-enabled').checked = config.guest_enabled;
  $('admin-content').hidden = false;
  $('logout').hidden = false;
  $('admin-status').className = `badge ${config.enabled ? 'success' : 'neutral'}`;
  $('admin-status').textContent = `${config.schedule_mode === 'groups' ? '循环组' : '单个模型'} · ${config.enabled ? '自动生成已启用' : '自动生成已暂停'}`;
  $('admin-next').textContent = config.enabled ? (config.next_run == null ? '生成结束后计时' : dateTime(config.next_run)) : '尚未启用';
  $('primary-effort').value = config.effort;
  $('admin-run').disabled = !config.has_key;
  renderPrimaryModelSettings(config.model);
  updateQuickRunButton();
}
function hasUnsavedModelSettings() {
  return config?.protocol === 'codex' && ((codexModels.length > 0 && !codexModels.some(model => model.model === $('primary-model').value))
    || $('primary-model').value !== config.model || $('primary-effort').value !== config.effort);
}
function updateModelDraftHint() {
  const hint = $('active-model-hint');
  if (config?.protocol === 'codex') {
    const selected = codexModels.find(model => model.model === $('primary-model').value);
    hint.textContent = codexModels.length && !selected
      ? '当前保存的模型不在此账号的可用列表中，请重新选择模型并保存。'
      : hasUnsavedModelSettings()
        ? '模型或思考强度已更改，点击“保存设置”后才会生效。'
        : (codexModels.length ? '单个模式每轮使用此模型与强度生成一次。' : '请刷新当前账号的模型列表。');
  }
  updateQuickRunButton();
}
function updateQuickRunButton() {
  const running = testingState?.running;
  const stopping = stoppingTests || testingState?.stopping;
  const offline = !PelicanLive.fresh();
  $('admin-run').disabled = offline || busy || stopping || (!config?.has_key && config?.schedule_mode !== 'groups') || Boolean(running) || config?.enabled;
  $('admin-run').title = config?.enabled ? '请先暂停循环，再手动生成一次' : '保存设置并只生成一张';
  $('admin-start').disabled = offline || busy || stopping || !config || Boolean(running) || config.enabled;
  $('admin-stop').disabled = offline || stoppingTests || !config || !(running || config.enabled || testingState?.queued_nodes?.length || stopping);
  $('admin-stop').textContent = stopping ? '正在暂停…' : '暂停生成';
}
function renderNodes() {
  selectedNodes.forEach(id => { if (!nodes.some(n => n.id === id && n.enabled && n.has_key)) selectedNodes.delete(id); });
  const filtered = filteredNodes();
  const signature = JSON.stringify([nodes,busy,nodeSearch,nodeStatus,[...selectedNodes].sort((a,b) => a-b)]);
  if (signature === nodeSignature) return;
  nodeSignature = signature;
  const running = nodes.some(n => n.last_run?.status === 'running');
  $('node-list').innerHTML = filtered.length ? filtered.map(n => `<article class="node-row ${n.active ? 'active-node' : ''}"><label class="node-select"><input type="checkbox" data-select="${n.id}" ${selectedNodes.has(n.id) ? 'checked' : ''} ${busy || !n.enabled || !n.has_key ? 'disabled' : ''}> 选择</label><div class="node-info"><h3>${escapeHTML(n.name)} ${n.active ? '<span class="badge success">当前节点</span>' : ''} ${!n.enabled ? '<span class="badge neutral">已停用</span>' : ''}</h3><p>${escapeHTML(n.base_url)} · ${escapeHTML(n.model)} · ${escapeHTML(n.effort)} · ${n.protocol === 'codex' ? 'Codex · ChatGPT 登录' : n.protocol === 'responses' ? 'Responses' : 'Chat Completions'}</p><p>凭据：${escapeHTML(n.api_key_masked)}</p></div><div class="node-actions"><button class="button secondary" type="button" data-edit="${n.id}" ${busy ? 'disabled' : ''}>编辑</button><button class="button secondary" type="button" data-copy="${n.id}" ${busy ? 'disabled' : ''}>复制</button><button class="button secondary" type="button" data-toggle="${n.id}" ${busy || (n.active && n.enabled) ? 'disabled' : ''}>${n.enabled ? '停用' : '启用'}</button><button class="button secondary" type="button" data-activate="${n.id}" ${busy || n.active || !n.enabled || !n.has_key ? 'disabled' : ''}>${n.active ? '使用中' : '设为当前'}</button><button class="button primary" type="button" data-test="${n.id}" ${busy || running || !n.enabled || !n.has_key ? 'disabled' : ''}>生成一次</button></div><div class="node-result">${n.last_run ? `<span class="badge ${escapeHTML(n.last_run.status)}">${labels[n.last_run.status] || '尚未检测'}</span><span>最近一轮 #${n.last_run.id} · ${new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(n.last_run.started*1000))}</span><a href="/?run=${n.last_run.id}" target="_blank" rel="noopener">查看结果 ↗</a>${n.last_run.error ? `<p>${escapeHTML(n.last_run.error)}</p>` : ''}` : '<span>尚未检测</span>'}</div></article>`).join('') : '<p class="filter-empty">没有符合当前筛选条件的节点。</p>';
  $('selected-node-count').textContent = `已选 ${selectedNodes.size} 个`;
  $('bulk-run').disabled = busy || !selectedNodes.size;
  updateQuickRunButton();
  $('add-node').disabled = busy;
  const selectable = filtered.filter(n => n.enabled && n.has_key);
  const selectedVisible = selectable.filter(n => selectedNodes.has(n.id)).length;
  $('select-all-nodes').checked = selectable.length > 0 && selectedVisible === selectable.length;
  $('select-all-nodes').indeterminate = selectedVisible > 0 && selectedVisible < selectable.length;
}
async function refreshNodes() { nodes = await api('/api/admin/nodes'); renderNodes(); }
const dateTime = value => value ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value*1000)) : '—';
function recordDuration(run) {
  if (!run.started || (!run.finished && run.status !== 'running')) return '—';
  const end = run.finished || PelicanLive.now();
  if (end == null) return '待同步';
  const seconds = Math.max(0, end - run.started);
  return seconds < 60 ? `${seconds.toFixed(1)} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`;
}
async function refreshState() { applyServerState(await api('/api/state')); }
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
  $('admin-model').value = node?.model || 'gpt-6-luna';
  $('admin-effort').value = node?.effort || 'low';
  $('admin-protocol').value = node?.protocol || 'codex';
  privacy.clear($('admin-url')); privacy.clear($('admin-key'));
  $('node-message').textContent = '';
  updateProvider();
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
      message(`已切换到「${config.node_name}」。${config.enabled ? '定时检测仍轮流执行所有已启用节点。' : '自动检测仍处于暂停状态。'}`);
    } else if (button.dataset.test) {
      const result = await api(`/api/admin/nodes/${button.dataset.test}/run`,{});
      message(`第 ${result.id} 轮生成已开始，结果将在对应节点下更新；当前节点保持不变。`);
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
async function enterAdmin() {
  authenticated = true;
  const destination = new URLSearchParams(location.search).get('return');
  if (destination === 'gallery' || destination === 'records') {
    location.replace(destination === 'records' ? '/admin/records' : '/');
    return;
  }
  showConfig(await api('/api/admin/settings'));
  void refreshCodex();
  await Promise.all([refreshNodes(), refreshState(), refreshGroups()]);
  editGroup();
  if ($('auth-dialog').open) $('auth-dialog').close();
}
async function load() {
  authenticated = false;
  $('admin-content').hidden = true;
  $('logout').hidden = true;
  try {
    const status = await api('/api/auth/status');
    setup = !status.configured;
    $('auth-title').textContent = setup ? '首次设置管理密码' : '登录后台管理';
    $('auth-description').textContent = setup ? (status.setup_allowed ? '设置 6–256 个字符的密码。登录一次即可管理后台和画廊。' : '请先在服务所在机器打开后台设置密码。') : '此浏览器保持登录 30 天，刷新、重新打开或重启服务后无需重复输入。可随时退出登录。';
    $('admin-token').minLength = setup ? 6 : 1;
    $('admin-token').autocomplete = setup ? 'new-password' : 'current-password';
    $('confirm-password-row').hidden = !setup;
    $('confirm-password').required = setup;
    $('auth-submit').textContent = setup ? '设置密码并登录' : '登录';
    $('auth-submit').disabled = setup && !status.setup_allowed;
    $('admin-status').textContent = setup ? '等待设置管理密码' : '等待登录';
    if (status.authenticated) { await enterAdmin(); return; }
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
function formSettings() {
  const values = {task_prompt:$('task-prompt-editor').value,schedule_mode:$('schedule-mode').value,retry_count:Number($('retry-count').value),interval_seconds:Number($('interval').value), timeout_seconds:Number($('timeout').value), max_output_tokens:Number($('max-tokens').value), guest_enabled:$('guest-enabled').checked};
  if (config?.protocol === 'codex' && values.schedule_mode === 'single') Object.assign(values, {model:$('primary-model').value || config.model, effort:$('primary-effort').value});
  return values;
}
$('admin-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!$('admin-form').reportValidity()) return;
  action(async () => {
    const values = formSettings();
    const result = await api('/api/admin/settings',values);
    showConfig(result);
    message('配置已保存并生效。后续生成使用新配置，无需重启。');
  });
});
$('admin-run').addEventListener('click', () => action(async () => {
  if (!$('admin-form').reportValidity()) return;
  showConfig(await api('/api/admin/settings', formSettings()));
  const result = await api('/api/admin/run',{});
  await Promise.all([refreshNodes(), refreshState()]);
  message(`生成任务 #${result.id} 已提交，可在公开页面查看结果。`);
}));
$('admin-start').addEventListener('click', () => {
  if (!$('admin-form').reportValidity()) return;
  action(async () => {
    const values = formSettings();
    showConfig(await api('/api/admin/settings', values));
    showConfig(await api('/api/admin/testing/start', {}));
    await refreshState();
    message('测试已开始，按当前模型或循环组立即运行，之后按设定间隔继续。');
  });
});
$('admin-stop').addEventListener('click', async () => {
  if (stoppingTests) return;
  stoppingTests = true; batchCancelled = true; updateQuickRunButton();
  try {
    await api('/api/admin/testing/stop', {});
    await Promise.all([refreshNodes(), refreshState()]);
    message('已停止测试：当前任务已取消，排队已清空，定时测试已暂停。');
  } catch (error) { message(error.message, true); }
  finally { stoppingTests = false; updateQuickRunButton(); }
});
$('auth-form').addEventListener('submit', async event => {
  event.preventDefault();
  const password = $('admin-token').value;
  if (setup && password !== $('confirm-password').value) { $('auth-error').textContent = '两次输入的密码不一致。'; return; }
  $('auth-submit').disabled = true;
  try {
    if (setup) { await api('/api/auth/setup',{password}); setup = false; }
    await api('/api/auth/login',{password});
    $('admin-token').value = ''; $('confirm-password').value = ''; $('auth-error').textContent = '';
    await enterAdmin();
  } catch (error) { authenticated = false; $('auth-error').textContent = error.message; }
  finally { $('auth-submit').disabled = false; }
});
$('logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout',{}); }
  finally {
    authenticated = false; config = undefined; nodes = []; loopGroups = []; editingGroupId = null; codexModels = []; codexCombinations = []; codexFingerprint = ''; selectedNodes.clear(); nodeSignature = ''; $('node-list').replaceChildren();
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
$('bulk-run').addEventListener('click', () => action(async () => {
  batchCancelled = false;
  const ids = [...selectedNodes];
  let started = 0;
  let failed = 0;
  const deadline = Date.now() + 30 * 60 * 1000;
  for (const id of ids) {
    if (batchCancelled) break;
    try { await api(`/api/admin/nodes/${id}/run`, {}); started++; await refreshState(); }
    catch (error) { failed++; if (started === 0) throw error; }
    while (!batchCancelled && (await api('/api/admin/nodes')).some(node => node.last_run?.status === 'running')) {
      if (Date.now() > deadline) throw new Error('批量检测等待超时，请刷新页面查看已完成结果');
      await new Promise(resolve => setTimeout(resolve, 2000));
      await refreshState();
    }
  }
  selectedNodes.clear(); $('select-all-nodes').checked = false; await refreshState(); await refreshNodes(); message(`已提交 ${started} 个节点的检测${failed ? `，${failed} 个节点提交失败` : ''}；服务会按单实例锁顺序执行。`, failed > 0);
}));
liveAuthLoading = true;
load().finally(() => { liveAuthLoading = false; });
setInterval(() => { if (authenticated && !busy && !$('node-dialog').open) Promise.all([refreshNodes(), ...(groupBusy ? [] : [refreshGroups()])]).catch(error => message(error.message,true)); }, 5000);
setInterval(() => { if (authenticated) refreshCodex(); }, 5000);

window.addEventListener('focus', async () => {
  if (busy || groupBusy) return;
  try {
    const status = await api('/api/auth/status');
    if (Boolean(status.authenticated) !== authenticated) await load();
    else if (authenticated) void refreshCodex();
  } catch { /* The next request reports a connection error. */ }
});

$('admin-form').addEventListener('input', () => { formDirty = true; });
$('admin-form').addEventListener('change', () => { formDirty = true; });
function applyServerState(value) {
  if (testingState && value.server_time < testingState.server_time) return;
  testingState = value;
  if (!config || !authenticated) return;
  const changed = Object.keys(value.settings).some(key => config[key] !== value.settings[key]) || config.task_prompt !== value.task_prompt;
  if (changed && !formDirty && !busy) showConfig({...config, ...value.settings, task_prompt:value.task_prompt});
  config.enabled = value.settings.enabled;
  config.next_run = value.settings.next_run;
  $('admin-enabled').checked = value.settings.enabled;
  $('admin-status').textContent = value.stopping ? '正在暂停…' : value.running ? `正在生成 #${value.running} · 已耗时 ${recordDuration(value.timeline.find(run => run.id === value.running) || {})}` : value.settings.enabled ? '循环已开启 · 等待下一轮' : '生成已暂停';
  $('admin-status').className = `badge ${value.running ? 'running' : value.settings.enabled ? 'success' : 'neutral'}`;
  const left = value.settings.next_run == null ? null : Math.max(0, Math.ceil(value.settings.next_run - value.server_time));
  $('admin-next').textContent = value.stopping ? '正在暂停' : value.running ? '生成结束后计时' : value.settings.enabled ? (left === null ? '等待服务端调度' : `${dateTime(value.settings.next_run)} · 剩余 ${left} 秒`) : '尚未启用';
  updateQuickRunButton();
}
PelicanLive.start(value => {
  $('admin-sync').textContent = '已连接 · 每秒从服务端同步状态';
  applyServerState(value);
  if (value.authenticated !== authenticated && !liveAuthLoading) {
    liveAuthLoading = true;
    load().finally(() => { liveAuthLoading = false; });
  }
  const signature = JSON.stringify([value.authenticated, value.running, value.stopping, value.settings, value.timeline.map(run => [run.id,run.status,run.finished,run.favorite])]);
  if (signature !== liveSignature) {
    if (authenticated && !busy) {
      liveSignature = signature;
      refreshNodes().catch(error => message(error.message,true));
    }
  }
}, () => {
  $('admin-sync').textContent = '服务连接中断 · 状态待确认';
  $('admin-status').textContent = '状态待同步';
  $('admin-next').textContent = '连接恢复后同步';
  updateQuickRunButton();
});
