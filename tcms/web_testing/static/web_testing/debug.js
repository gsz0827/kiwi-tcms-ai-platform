(function () {
  'use strict';
  const form=document.getElementById('web-debug-form');
  if (!form) return;
  let environments=JSON.parse(document.getElementById('web-debug-environments').textContent);
  const selector=form.querySelector('[name="environment"]');
  function preview() {
    try { environments=JSON.parse(document.getElementById('web-debug-environments').textContent); } catch (_) { return; }
    const env=environments[selector.value];
    document.getElementById('web-debug-url').textContent=env?.base_url || '—';
    document.getElementById('web-debug-https').textContent=env ? (env.ignore_https_errors?'允许自签名证书':'校验证书') : '—';
  }
  selector.addEventListener('change',preview);preview();
}());
