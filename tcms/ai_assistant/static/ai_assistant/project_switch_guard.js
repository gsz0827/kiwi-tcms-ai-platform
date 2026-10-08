/* Switching context is explicit and must not silently discard edited forms. */
document.addEventListener('DOMContentLoaded', () => {
    const switcher = document.getElementById('project-switch-form');
    if (!switcher) return;
    const values = form => JSON.stringify(Array.from(form.elements)
        .filter(el => el.name && !['hidden', 'submit', 'button', 'reset'].includes(el.type))
        .map(el => [el.name, el.type === 'checkbox' || el.type === 'radio' ? el.checked : el.value]));
    const tracked = Array.from(document.forms).filter(form => form !== switcher && form.method.toLowerCase() === 'post')
        .map(form => [form, values(form)]);
    switcher.addEventListener('submit', event => {
        if (tracked.some(([form, initial]) => form.isConnected && values(form) !== initial) &&
            !window.confirm('页面有未保存的修改，仍要切换项目并返回工作台吗？')) {
            event.preventDefault();
            event.stopImmediatePropagation();
        }
    }, true);
});
