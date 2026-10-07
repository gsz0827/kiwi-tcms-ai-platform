(function () {
  'use strict';
  document.querySelectorAll('.allure-report-card[data-poll="true"]').forEach(function (card) {
    if (card.dataset.polling) return;
    card.dataset.polling = 'true';
    let attempts = 0;
    async function poll() {
      if (++attempts > 120) {
        card.querySelector('.allure-state').textContent = '报告仍在生成，请稍后刷新。';
        return;
      }
      try {
        const response = await fetch(card.dataset.statusUrl, {cache:'no-store', headers:{Accept:'application/json'}});
        if (response.redirected || !response.ok) return;
        const data = await response.json();
        if (data.status === 'ready' || data.status === 'error') {window.location.reload(); return;}
        card.querySelector('.allure-state').textContent = data.label;
      } catch (_) { /* Transient network failure: keep the task result readable. */ }
      window.setTimeout(poll, 3000);
    }
    window.setTimeout(poll, 3000);
  });
}());
