(function () {
  'use strict';
  const status = document.getElementById('web-run-progress');
  if (!status || !status.dataset.statusUrl) return;
  async function poll() {
    try {
      const response = await fetch(status.dataset.statusUrl, {headers:{Accept:'application/json'},cache:'no-store'});
      if (!response.ok || response.redirected) throw new Error('unavailable');
      const data = await response.json();
      status.textContent = data.status + ' · 完成 ' + data.completed + '/' + data.total;
      document.getElementById('web-progress').value = data.completed;
      if (data.terminal || data.completed !== Number(status.dataset.completed)) {window.location.reload(); return;}
    } catch (_) {status.textContent = '暂时无法获取进度，正在重试…';}
    window.setTimeout(poll, 3000);
  }
  window.setTimeout(poll, 3000);
}());
