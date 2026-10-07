(function () {
  'use strict';
  document.querySelectorAll('[data-execution-results]').forEach(function (panel) {
    if (panel.dataset.bound) return;
    panel.dataset.bound = 'true';
    panel.addEventListener('click', function (event) {
      const button = event.target.closest('.execution-diagnostic-toggle');
      if (!button || !panel.contains(button)) return;
      const row = document.getElementById(button.getAttribute('aria-controls'));
      if (!row || !panel.contains(row)) return;
      const expanded = button.getAttribute('aria-expanded') !== 'true';
      row.hidden = !expanded;
      button.setAttribute('aria-expanded', String(expanded));
      button.textContent = expanded ? '收起诊断' : '原始诊断';
    });
  });
}());
