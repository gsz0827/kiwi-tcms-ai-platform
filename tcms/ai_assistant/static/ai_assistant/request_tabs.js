(() => {
  'use strict';
  const form = document.querySelector('.automation-edit-form');
  if (!form || !form.querySelector('[data-field="method"]')) return;
  const fields = ['query', 'headers', 'send_body', 'body'].map(name => form.querySelector('[data-field="' + name + '"]'));
  if (fields.some(field => !field)) return;
  const grid = fields[0].parentElement;
  grid.classList.add('automation-request-grid');
  const tablist = document.createElement('div');
  tablist.className = 'automation-request-tabs';
  tablist.setAttribute('role', 'tablist');
  tablist.setAttribute('aria-label', '请求配置');
  const workspace = document.createElement('div');
  workspace.className = 'automation-request-workspace';
  workspace.append(tablist);
  grid.append(workspace);
  const panels = [], buttons = [];
  function activate(index) {
    panels.forEach((panel, i) => { panel.hidden = i !== index; buttons[i].setAttribute('aria-selected', i === index ? 'true' : 'false'); buttons[i].tabIndex = i === index ? 0 : -1; });
  }
  [['查询参数', [fields[0]]], ['请求头', [fields[1]]], ['请求体', fields.slice(2)]].forEach(([label, content], index) => {
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = label; button.id = 'request-tab-' + index;
    button.setAttribute('role', 'tab'); button.setAttribute('aria-controls', 'request-panel-' + index);
    const panel = document.createElement('section');
    panel.id = 'request-panel-' + index; panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', button.id); panel.className = 'automation-request-panel';
    content.forEach(field => panel.append(field));
    button.addEventListener('click', () => activate(index));
    button.addEventListener('keydown', event => {
      const targets = {ArrowRight: (index + 1) % 3, ArrowLeft: (index + 2) % 3, Home: 0, End: 2};
      if (Object.hasOwn(targets, event.key)) { event.preventDefault(); activate(targets[event.key]); buttons[targets[event.key]].focus(); }
    });
    tablist.append(button); workspace.append(panel); buttons.push(button); panels.push(panel);
  });
  const errorIndex = panels.findIndex(panel => panel.querySelector('.has-error'));
  activate(errorIndex < 0 ? 0 : errorIndex);
  function reveal(event) {
    const index = panels.findIndex(panel => panel.contains(event.target));
    if (index >= 0) activate(index);
  }
  form.addEventListener('invalid', reveal, true);
  form.addEventListener('automation:reveal', reveal);
  const body = form.querySelector('textarea[name="body"]');
  if (body) {
    const actions = document.createElement('div'), format = document.createElement('button'), error = document.createElement('p');
    actions.className = 'automation-json-actions'; format.type = 'button'; format.className = 'btn btn-default'; format.textContent = '格式化 JSON';
    error.className = 'automation-json-error'; error.setAttribute('role', 'alert'); error.hidden = true;
    format.addEventListener('click', () => {
      try { body.value = JSON.stringify(JSON.parse(body.value || '{}'), null, 2); error.hidden = true; body.dispatchEvent(new Event('input', {bubbles: true})); }
      catch (exception) { error.textContent = 'JSON 格式不正确：' + exception.message; error.hidden = false; body.focus(); }
    });
    actions.append(format); body.after(actions, error);
  }
})();
