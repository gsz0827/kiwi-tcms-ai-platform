(() => {
  'use strict';
  document.querySelectorAll('.automation-environment-control').forEach(control => {
    const select = control.querySelector('select[name="environment"]');
    if (!select) return;
    const summary = control.parentElement.querySelector('.automation-environment-summary');
    function updateSummary() {
      if (!summary) return;
      let values;
      try { values = JSON.parse(document.getElementById('automation-environment-previews')?.textContent || '{}'); } catch (_) { return; }
      const selected = values[select.value];
      summary.querySelector('[data-environment-site]').textContent = selected?.base_url || '请选择环境';
      const certificate = selected && Object.hasOwn(selected, 'ignore_https_errors');
      summary.querySelector('[data-environment-certificate]').hidden = !certificate;
      summary.querySelector('[data-environment-certificate-value]').hidden = !certificate;
      summary.querySelector('[data-environment-certificate-value]').textContent = certificate ? (selected.ignore_https_errors ? '允许自签名证书' : '校验证书') : '—';
      const timeout = selected && Object.hasOwn(selected, 'timeout');
      summary.querySelector('[data-environment-timeout]').hidden = !timeout;
      summary.querySelector('[data-environment-timeout-value]').hidden = !timeout;
      summary.querySelector('[data-environment-timeout-value]').textContent = timeout ? selected.timeout + ' 秒' : '—';
    }
    select.addEventListener('change', updateSummary);
    updateSummary();
    const link = control.querySelector('[data-environment-create]');
    if (!link || select.disabled || select.closest('fieldset[disabled]')) return;
    const form = select.form;
    const product = form?.querySelector('select[name="product"]');
    function updateProduct() {
      if (!product) return;
      const url = new URL(link.href, window.location.origin);
      if (product.value) url.searchParams.set('product', product.value);
      else url.searchParams.delete('product');
      link.href = url.pathname + url.search;
    }
    updateProduct();
    product?.addEventListener('change', updateProduct);
    let pending = false;
    let loading = false;
    link.addEventListener('click', () => { updateProduct(); pending = true; });
    window.addEventListener('focus', async () => {
      if (!pending || loading) return;
      loading = true;
      try {
        const response = await fetch(window.location.href, {credentials: 'same-origin', cache: 'no-store'});
        if (!response.ok || response.redirected) return;
        const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
        const fresh = doc.querySelector('.automation-edit-form select[name="environment"]');
        if (!fresh) return;
        const value = select.value;
        // Never clear a user's choice if the environment disappeared while editing.
        if (value && ![...fresh.options].some(option => option.value === value)) return;
        const previews = ['automation-environment-previews', 'web-environment-previews', 'web-debug-environments'];
        const updates = previews.map(id => [document.getElementById(id), doc.getElementById(id)]).filter(([current]) => current);
        if (updates.some(([, freshSource]) => !freshSource)) return;
        updates.forEach(([, freshSource]) => JSON.parse(freshSource.textContent));
        updates.forEach(([current, freshSource]) => { current.textContent = freshSource.textContent; });
        select.replaceChildren(...[...fresh.options].map(option => option.cloneNode(true)));
        select.value = value;
        select.dispatchEvent(new Event('change', {bubbles: true}));
        pending = false;
      } catch (_) {
        // Keep the current choices and retry when the user returns to this tab.
      } finally {
        loading = false;
      }
    });
  });
})();
