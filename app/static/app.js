(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const toast = (message, error = false) => {
    const node = $('#toast');
    if (!node) return;
    node.textContent = message;
    node.className = `toast visible${error ? ' error' : ''}`;
    window.setTimeout(() => { node.className = 'toast'; }, 5000);
  };
  const api = async (url, options = {}) => {
    const headers = {'X-Governance-Request': 'v4', ...(options.headers || {})};
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const response = await fetch(url, {...options, headers});
    const payload = response.headers.get('content-type')?.includes('json') ? await response.json() : {};
    if (!response.ok) {
      const detail = Array.isArray(payload.detail) ? payload.detail.map(x => x.msg || x).join('; ') : payload.detail;
      throw new Error([payload.error || 'Request failed', detail].filter(Boolean).join(' — '));
    }
    return payload;
  };
  const formObject = (form) => {
    const value = Object.fromEntries(new FormData(form).entries());
    for (const key of Object.keys(value)) value[key] = String(value[key]).trim();
    if (!value.go_live_date) value.go_live_date = null;
    return value;
  };
  const withBusy = async (button, action) => {
    const label = button?.textContent;
    if (button) { button.disabled = true; button.textContent = 'Working…'; }
    try { return await action(); }
    finally { if (button) { button.disabled = false; button.textContent = label; } }
  };
  const locator = (asset) => asset.resource_path || asset.resource_id || [asset.catalog_name, asset.schema_name, asset.resource_name].filter(Boolean).join('.');
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));

  const projectForm = $('#project-form');
  if (projectForm) projectForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const projectId = projectForm.dataset.projectId;
    const button = $('button[type=submit]', projectForm);
    try {
      const result = await withBusy(button, () => api(projectId ? `/api/projects/${projectId}` : '/api/projects', {
        method: projectId ? 'PATCH' : 'POST',
        body: JSON.stringify(formObject(projectForm))
      }));
      window.location.assign(`/projects/${result.project_id}`);
    } catch (error) { toast(error.message, true); }
  });

  const renderScan = (target, scan, selectable = false) => {
    if (!scan.assets?.length) {
      target.innerHTML = '<div class="empty compact">No tagged assets were discovered. Add an asset manually when creating a production request, or verify scanner permissions.</div>';
      return;
    }
    target.innerHTML = scan.assets.map((asset, index) => {
      const compliant = asset.compliance_status === 'COMPLIANT';
      const checked = selectable && compliant ? 'checked' : '';
      const disabled = selectable && !compliant ? 'disabled' : '';
      const data = escapeHtml(JSON.stringify({
        resource_type: asset.resource_type, resource_id: asset.resource_id || '', resource_name: asset.resource_name,
        resource_path: asset.resource_path || '', catalog_name: asset.catalog_name || '', schema_name: asset.schema_name || ''
      }));
      if (selectable) return `<label class="asset-choice ${compliant ? '' : 'disabled'}">
        <input type="checkbox" class="asset-checkbox" data-asset="${data}" ${checked} ${disabled}>
        <span><strong>${escapeHtml(asset.resource_name)}</strong><small>${escapeHtml(locator(asset))}</small></span>
        <span class="asset-type">${escapeHtml(asset.resource_type)}</span>
        <small>${escapeHtml(Object.entries(asset.tags || {}).map(([k,v]) => `${k}=${v}`).join(' · '))}</small>
        <span class="badge result-${compliant ? 'pass' : 'fail'}">${escapeHtml(asset.compliance_status)}</span>
      </label>`;
      const repairable = asset.compliance_status === 'FIXABLE' && !['job', 'pipeline'].includes(asset.resource_type);
      return `<div class="asset-table-row"><span><strong>${escapeHtml(asset.resource_name)}</strong><small class="block">${escapeHtml(locator(asset))}</small></span><span class="asset-type">${escapeHtml(asset.resource_type)}</span><small>${escapeHtml(Object.entries(asset.tags || {}).map(([k,v]) => `${k}=${v}`).join(' · '))}</small><span class="asset-actions"><span class="badge result-${compliant ? 'pass' : 'fail'}">${escapeHtml(asset.compliance_status)}</span>${repairable ? `<button type="button" class="button secondary compact fix-tag-button" data-asset="${data}">Fix tags</button>` : ''}</span></div>`;
    }).join('');
  };

  const scanButton = $('#scan-assets');
  if (scanButton) scanButton.addEventListener('click', () => withBusy(scanButton, async () => {
    try {
      const scan = await api(`/api/projects/${scanButton.dataset.projectId}/scan`, {method:'POST', body: JSON.stringify({environment:'dev'})});
      renderScan($('#scan-results'), scan, false);
      toast(`Scanned ${scan.assets.length} assets.`);
    } catch (error) { toast(error.message, true); }
  }));
  const scanResults = $('#scan-results');
  if (scanResults && scanButton) scanResults.addEventListener('click', async (event) => {
    const button = event.target.closest('.fix-tag-button');
    if (!button) return;
    await withBusy(button, async () => {
      try {
        const asset = JSON.parse(button.dataset.asset);
        const result = await api(`/api/projects/${scanButton.dataset.projectId}/fix-tags`, {method:'POST', body:JSON.stringify({asset})});
        toast(result.detail || 'Missing tags were applied.');
        const scan = await api(`/api/projects/${scanButton.dataset.projectId}/scan`, {method:'POST', body:JSON.stringify({environment:'dev'})});
        renderScan(scanResults, scan, false);
      } catch (error) { toast(error.message, true); }
    });
  });

  const requestForm = $('#production-request-form');
  const picker = $('#asset-picker');
  const requestScan = $('#request-scan-assets');
  const runRequestScan = async (assets = null) => {
    const body = {environment:'dev'};
    if (assets) body.assets = assets;
    const scan = await api(`/api/projects/${requestForm.dataset.projectId}/scan`, {method:'POST', body: JSON.stringify(body)});
    renderScan(picker, scan, true);
    return scan;
  };
  if (requestForm) {
    runRequestScan().catch(error => { picker.innerHTML = `<div class="empty compact">${escapeHtml(error.message)}</div>`; });
    requestScan.addEventListener('click', () => withBusy(requestScan, async () => {
      try { await runRequestScan(); toast('Development assets refreshed.'); } catch (error) { toast(error.message, true); }
    }));
    $('#manual-add').addEventListener('click', async (event) => withBusy(event.currentTarget, async () => {
      const asset = {
        resource_type: $('#manual-type').value,
        resource_name: $('#manual-name').value.trim(),
        resource_id: $('#manual-id').value.trim(),
        resource_path: $('#manual-path').value.trim(),
        catalog_name: $('#manual-catalog').value.trim(),
        schema_name: $('#manual-schema').value.trim()
      };
      try {
        const scan = await api(`/api/projects/${requestForm.dataset.projectId}/scan`, {method:'POST', body: JSON.stringify({environment:'dev', assets:[asset]})});
        const existing = $$('.asset-checkbox').map(node => JSON.parse(node.dataset.asset));
        const combined = [...existing.filter(item => locator(item) !== locator(asset)), asset];
        const all = await api(`/api/projects/${requestForm.dataset.projectId}/scan`, {method:'POST', body: JSON.stringify({environment:'dev', assets:combined})});
        renderScan(picker, all, true);
        toast(scan.assets[0]?.detail || 'Asset validated.');
      } catch (error) { toast(error.message, true); }
    }));
    requestForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const assets = $$('.asset-checkbox:checked').map(node => JSON.parse(node.dataset.asset));
      if (!assets.length) { toast('Select at least one compliant asset.', true); return; }
      const payload = formObject(requestForm); payload.assets = assets; delete payload.go_live_date;
      const button = $('button[type=submit]', requestForm);
      try {
        const result = await withBusy(button, () => api(`/api/projects/${requestForm.dataset.projectId}/production-requests`, {method:'POST', body:JSON.stringify(payload)}));
        window.location.assign(`/production-requests/${result.request_id}`);
      } catch (error) { toast(error.message, true); }
    });
  }

  $$('.decision-button').forEach(button => button.addEventListener('click', () => withBusy(button, async () => {
    try {
      const result = await api(`/api/production-requests/${button.dataset.requestId}/decision`, {method:'POST', body:JSON.stringify({approve:button.dataset.approve === 'true', comment:$('#decision-comment')?.value || ''})});
      toast(`Request is now ${result.request_status.replaceAll('_',' ')}.`);
      window.setTimeout(() => window.location.reload(), 400);
    } catch (error) { toast(error.message, true); }
  })));
  const revalidate = $('#revalidate-request');
  if (revalidate) revalidate.addEventListener('click', () => withBusy(revalidate, async () => {
    try { await api(`/api/production-requests/${revalidate.dataset.requestId}/revalidate`, {method:'POST'}); window.location.reload(); }
    catch (error) { toast(error.message, true); }
  }));
  const retry = $('#retry-request');
  if (retry) retry.addEventListener('click', () => withBusy(retry, async () => {
    try { await api(`/api/production-requests/${retry.dataset.requestId}/retry`, {method:'POST'}); window.location.reload(); }
    catch (error) { toast(error.message, true); }
  }));
  const recover = $('#recover-request');
  if (recover) recover.addEventListener('click', () => withBusy(recover, async () => {
    const comment = $('#recovery-comment')?.value?.trim() || '';
    if (comment.length < 5) { toast('Enter a recovery reason of at least five characters.', true); return; }
    if (!window.confirm('Confirm that the protected worker has stopped before recovering this request.')) return;
    try {
      await api(`/api/production-requests/${recover.dataset.requestId}/recover`, {method:'POST', body:JSON.stringify({comment})});
      window.location.reload();
    } catch (error) { toast(error.message, true); }
  }));

  /* ---------------------------------------------------------------------
     Responsive nav toggle (mobile menu) — additive only, does not touch
     any existing behavior above.
  --------------------------------------------------------------------- */
  const navToggle = $('.nav-toggle');
  const primaryNav = $('nav[aria-label="Primary navigation"]');
  if (navToggle && primaryNav) {
    navToggle.addEventListener('click', () => {
      const isOpen = primaryNav.classList.toggle('open');
      navToggle.setAttribute('aria-expanded', String(isOpen));
    });
    primaryNav.addEventListener('click', (event) => {
      if (event.target.tagName === 'A') {
        primaryNav.classList.remove('open');
        navToggle.setAttribute('aria-expanded', 'false');
      }
    });
    window.addEventListener('resize', () => {
      if (window.innerWidth > 900) {
        primaryNav.classList.remove('open');
        navToggle.setAttribute('aria-expanded', 'false');
      }
    });
  }
})();

