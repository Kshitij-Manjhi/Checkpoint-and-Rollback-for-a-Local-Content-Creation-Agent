const $ = (id) => document.getElementById(id);
let currentTaskId = null;
let checkpoints = [];
let reviewDecision = 'approve';

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}
function showNotice(message, isError = false) {
  const notice = $('notice'); notice.textContent = message; notice.classList.toggle('error', isError); notice.hidden = false;
}
function clearNotice() { $('notice').hidden = true; }
async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type':'application/json', ...(options.headers || {})}, ...options});
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = {detail: text}; }
  if (!response.ok) throw new Error(data?.detail || `${response.status} ${response.statusText}`);
  return data;
}
function setActiveTask(task) {
  currentTaskId = String(task.task_id);
  $('taskIdInput').value = currentTaskId;
  $('activeTask').classList.remove('empty');
  $('activeTask').innerHTML = `<strong>${escapeHtml(task.title || currentTaskId)}</strong><br><span>Task ID: ${escapeHtml(currentTaskId)}</span>`;
  $('rollbackButton').disabled = false;
}
function pill(status, fallback = 'unknown') {
  const text = String(status || fallback).toLowerCase();
  const css = ['safe','approved','verified','pending','rejected','blocked','unsafe','baseline'].includes(text) ? text : 'unsafe';
  return `<span class="pill ${css}">${escapeHtml(text.replaceAll('_',' '))}</span>`;
}
async function checkHealth() {
  try { await api('/api/health'); $('healthDot').className = 'dot online'; $('healthText').textContent = 'API connected'; }
  catch { $('healthDot').className = 'dot offline'; $('healthText').textContent = 'API unavailable'; }
}
async function loadModels() {
  const select = $('modelName');
  try {
    const result = await api('/api/models');
    select.innerHTML = '';
    if (!result.available || !result.models?.length) {
      select.add(new Option(result.message || 'No models found', ''));
      select.disabled = true;
      return;
    }
    result.models.forEach((name) => select.add(new Option(name, name)));
    select.disabled = $('mode').value !== 'ollama';
  } catch {
    select.innerHTML = '<option value="">Ollama unavailable</option>'; select.disabled = true;
  }
}
async function refreshCheckpoints() {
  if (!currentTaskId) return;
  const tbody = $('checkpointRows'); tbody.innerHTML = '<tr><td colspan="6" class="table-empty">Loading checkpoints…</td></tr>';
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(currentTaskId)}/checkpoints`);
    checkpoints = result.checkpoints || [];
    const detailed = await Promise.all(checkpoints.map(async (item) => {
      try { return {...item, detail: await api(`/api/checkpoints/${encodeURIComponent(item.checkpoint_id)}`)}; }
      catch (error) { return {...item, detail: null, detailError: error.message}; }
    }));
    if (!detailed.length) { tbody.innerHTML = '<tr><td colspan="6" class="table-empty">No checkpoints found for this task.</td></tr>'; return; }
    tbody.innerHTML = detailed.slice().reverse().map((item) => {
      const state = item.detail?.state || {};
      const status = state.approval_status || (Number(state.version || 0) === 0 ? 'baseline' : 'pending');
      const integrity = item.detail?.integrity_verified === true ? 'verified' : item.detail?.integrity_verified === false ? 'unsafe' : 'unknown';
      const canReview = status === 'pending' && state.decision !== 'reject' && Number(state.version || 0) > 0;
      const reviewButtons = canReview ? `<button class="button button-secondary" data-review="approve" data-id="${escapeHtml(item.checkpoint_id)}">Approve</button><button class="button button-danger" data-review="reject" data-id="${escapeHtml(item.checkpoint_id)}">Reject</button>` : '';
      const previewButton = `<button class="button button-quiet" data-preview="${escapeHtml(item.checkpoint_id)}">Inspect</button>`;
      return `<tr><td><div class="id-cell">${escapeHtml(item.checkpoint_id)}</div><div class="eyebrow">${escapeHtml(item.created_at || '')}</div></td><td>${escapeHtml(item.sequence)}</td><td>${pill(status)}</td><td>${pill(integrity)}</td><td>${item.is_safe ? pill('safe') : pill('unsafe')}</td><td><div class="actions">${previewButton}${reviewButtons}</div></td></tr>`;
    }).join('');
  } catch (error) { tbody.innerHTML = `<tr><td colspan="6" class="table-empty">${escapeHtml(error.message)}</td></tr>`; }
}
async function loadEvents() {
  if (!currentTaskId) { showNotice('Create or load a task first.', true); return; }
  const target = $('eventsList'); target.textContent = 'Loading audit events…';
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(currentTaskId)}/events`);
    const events = result.events || [];
    if (!events.length) { target.className = 'events-list empty'; target.textContent = 'No audit events recorded for this task yet.'; return; }
    target.className = 'events-list';
    target.innerHTML = events.slice().reverse().map((event) => `<div class="event"><time>${escapeHtml(event.created_at || '')}</time><div><strong>${escapeHtml(event.event_type || 'EVENT')}</strong><p>${escapeHtml(JSON.stringify(event.details || {}))}</p></div></div>`).join('');
  } catch (error) { target.className = 'events-list empty'; target.textContent = error.message; }
}
async function loadTask(taskId) {
  const result = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
  setActiveTask(result.task);
  await refreshCheckpoints();
  await loadEvents();
}
$('taskForm').addEventListener('submit', async (event) => {
  event.preventDefault(); clearNotice();
  try { const task = await api('/api/tasks', {method:'POST', body:JSON.stringify({title:$('taskTitle').value.trim() || 'Untitled task'})}); setActiveTask(task); await refreshCheckpoints(); await loadEvents(); showNotice(`Task created: ${task.task_id}`); }
  catch (error) { showNotice(error.message, true); }
});
$('selectTaskForm').addEventListener('submit', async (event) => {
  event.preventDefault(); clearNotice();
  try { await loadTask($('taskIdInput').value.trim()); showNotice(`Loaded task ${currentTaskId}`); }
  catch (error) { showNotice(error.message, true); }
});
$('generateForm').addEventListener('submit', async (event) => {
  event.preventDefault(); clearNotice();
  if (!currentTaskId) { showNotice('Create or load a task before generating a draft.', true); return; }
  const button = $('generateButton'); button.disabled = true; button.textContent = 'Generating…';
  try {
    const payload = {prompt:$('prompt').value.trim(), mode:$('mode').value};
    if (payload.mode === 'ollama' && $('modelName').value) payload.model_name = $('modelName').value;
    const result = await api(`/api/tasks/${encodeURIComponent(currentTaskId)}/generate`, {method:'POST', body:JSON.stringify(payload)});
    $('draftPreview').classList.remove('empty'); $('draftPreview').textContent = result.state?.draft || '(The generator returned an empty draft.)';
    await refreshCheckpoints(); await loadEvents();
    showNotice(`Draft generated. Status: ${result.approval_status || 'pending'}. Review it in Checkpoint history before rollback.`);
  } catch (error) { showNotice(error.message, true); }
  finally { button.disabled = false; button.innerHTML = 'Generate draft <span>↗</span>'; }
});
$('mode').addEventListener('change', () => { $('modelName').disabled = $('mode').value !== 'ollama' || !$('modelName').options.length || !$('modelName').value; });
$('refreshButton').addEventListener('click', async () => { clearNotice(); await refreshCheckpoints(); showNotice('Checkpoint history refreshed.'); });
$('eventsButton').addEventListener('click', loadEvents);
$('rollbackButton').addEventListener('click', async () => {
  if (!currentTaskId) return;
  if (!confirm('Roll back this task to its latest verified safe checkpoint? Pending and rejected drafts will not be selected.')) return;
  try { const result = await api(`/api/tasks/${encodeURIComponent(currentTaskId)}/rollback`, {method:'POST'}); $('draftPreview').classList.remove('empty'); $('draftPreview').textContent = result.state?.draft ?? '(Restored baseline has no draft content.)'; await refreshCheckpoints(); await loadEvents(); showNotice(`Rollback complete. Integrity verification: ${result.verification_passed ? 'passed' : 'failed'}. Checkpoint: ${result.checkpoint_id}`); }
  catch (error) { showNotice(error.message, true); }
});
$('checkpointRows').addEventListener('click', async (event) => {
  const button = event.target.closest('button'); if (!button) return;
  const id = button.dataset.id;
  if (button.dataset.review) { openReview(id, button.dataset.review); return; }
  if (button.dataset.preview) {
    try { const result = await api(`/api/checkpoints/${encodeURIComponent(button.dataset.preview)}`); $('draftPreview').classList.remove('empty'); $('draftPreview').textContent = result.state?.draft || JSON.stringify(result.state, null, 2) || 'No state available'; showNotice(`Checkpoint integrity ${result.integrity_verified ? 'verified' : 'could not be verified'}.` , !result.integrity_verified); }
    catch (error) { showNotice(error.message, true); }
  }
});
function openReview(checkpointId, decision) {
  reviewDecision = decision; $('reviewCheckpointId').value = checkpointId;
  $('reviewTitle').textContent = decision === 'approve' ? 'Approve draft' : 'Reject draft';
  $('reviewDescription').textContent = decision === 'approve' ? 'Approval makes this candidate eligible as a safe rollback checkpoint.' : 'Rejection keeps this candidate unsafe for rollback.';
  $('reviewReason').value = '';
  $('confirmReview').textContent = decision === 'approve' ? 'Approve draft' : 'Reject draft';
  $('confirmReview').className = decision === 'approve' ? 'button button-primary' : 'button button-danger';
  $('reviewDialog').showModal();
}
$('closeReview').addEventListener('click', () => $('reviewDialog').close());
$('cancelReview').addEventListener('click', () => $('reviewDialog').close());
$('reviewForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const id = $('reviewCheckpointId').value;
  const payload = {decision:reviewDecision, reviewer:$('reviewer').value.trim(), reason:$('reviewReason').value.trim()};
  try { const result = await api(`/api/checkpoints/${encodeURIComponent(id)}/review`, {method:'POST', body:JSON.stringify(payload)}); $('reviewDialog').close(); await refreshCheckpoints(); await loadEvents(); showNotice(`Review recorded: ${result.approval_status}. Rollback safety: ${result.is_safe ? 'enabled' : 'disabled'}.`); }
  catch (error) { showNotice(error.message, true); }
});
checkHealth(); loadModels();
