(() => {
    const panel = document.getElementById('api-run-status');
    if (!panel || panel.dataset.terminal === 'true') return;
    let failures = 0;
    async function poll() {
        if (document.hidden) { window.setTimeout(poll, 4000); return; }
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 8000);
        try {
            const response = await fetch(panel.dataset.url, {
                credentials: 'same-origin', cache: 'no-store', signal: controller.signal
            });
            if (!response.ok) throw new Error('status unavailable');
            const data = await response.json();
            if (data.status !== panel.dataset.status || String(data.finished) !== panel.dataset.finished) {
                window.location.reload();
                return;
            }
            failures = 0;
            document.getElementById('api-poll-error').hidden = true;
            if (!data.terminal) window.setTimeout(poll, 4000);
        } catch (_) {
            document.getElementById('api-poll-error').hidden = false;
            if (++failures < 5) window.setTimeout(poll, 6000);
        } finally {
            window.clearTimeout(timeout);
        }
    }
    window.setTimeout(poll, 3000);
})();
