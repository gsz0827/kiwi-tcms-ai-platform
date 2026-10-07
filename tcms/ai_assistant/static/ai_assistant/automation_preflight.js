(() => {
  'use strict';
  const source = document.getElementById('automation-environment-previews');
  if (!source) return;
  const form = source.closest('form');
  if (!form) return;
  let environments;
  try { environments = JSON.parse(source.textContent); } catch (_) { return; }
  const environment = form.querySelector('select[name="environment"]');
  const datasets = form.querySelector('[name="datasets"]');
  const run = form.querySelector('select[name="test_run"]');
  const show = (id, value) => { const node = document.getElementById(id); if (node) node.textContent = value; };
  function refresh() {
    try { environments = JSON.parse(source.textContent); } catch (_) { return; }
    const selected = environments[environment?.value];
    show('automation-environment-preview', selected ? selected.name : '未选择');
    show('automation-environment-url', selected?.base_url || '—');
    show('automation-environment-timeout', selected ? `${selected.timeout} 秒` : '—');
    const cases = form.querySelectorAll('input[name="cases"]:checked').length;
    let groups = 1;
    let invalid = false;
    try {
      const values = JSON.parse(datasets?.value || '[]');
      if (values === null) groups = 1;
      else if (!Array.isArray(values) || values.length > 10 || values.some(value => !value || typeof value !== 'object' || Array.isArray(value))) invalid = true;
      else groups = Math.max(1, values.length);
    } catch (_) { invalid = true; }
    show('automation-execution-scope', invalid ? `${cases} 条脚本 · 数据组待校验` : `${cases} 条脚本 × ${groups} 组数据 · 预计 ${cases * groups} 条结果`);
    const stop = form.querySelector('[name="stop_on_failure"]')?.checked;
    const cookies = form.querySelector('[name="share_cookies"]')?.checked;
    show('automation-execution-strategy', `${stop ? '失败后停止' : '失败后继续独立配置'} · ${cookies ? '同组共享 Cookie' : '不共享 Cookie'}`);
    show('automation-execution-writeback', run?.value ? `回写到 ${run.selectedOptions[0].textContent.trim()}` : '不回写正式执行任务');
  }
  form.addEventListener('input', refresh);
  form.addEventListener('change', refresh);
  form.addEventListener('click', () => { queueMicrotask(refresh); });
  window.addEventListener('pageshow', refresh);
  refresh();
})();
