(() => {
  'use strict';
  const form = document.getElementById('api-suite-submit-form');
  if (!form) return;
  const mode = form.querySelector('[name="execution_mode"]');
  const plan = form.querySelector('[name="plan"]');
  const build = form.querySelector('[name="build"]');
  const environment = form.querySelector('[name="environment"]');
  const plans = JSON.parse(document.getElementById('api-suite-plan-versions').textContent);
  const builds = JSON.parse(document.getElementById('api-suite-build-versions').textContent);
  function updateMode() {
    const formal = mode.value === 'formal';
    ['plan', 'build'].forEach(name => {
      form.querySelector('[data-submit-field="' + name + '"]').hidden = !formal;
      const field = form.querySelector('[name="' + name + '"]');
      field.required = formal;
      field.disabled = !formal;
    });
    environment.required = formal;
    document.getElementById('api-suite-formal-hint').hidden = !formal;
    document.getElementById('api-suite-debug-hint').hidden = formal;
  }
  function updateBuilds() {
    const version = plans[plan.value];
    [...build.options].forEach(option => {
      option.hidden = Boolean(option.value && version && builds[option.value] !== version);
      option.disabled = option.hidden;
    });
    if (build.selectedOptions[0]?.disabled) build.value = '';
  }
  function updateEnvironment() {
    const previews = JSON.parse(document.getElementById('api-suite-environment-previews').textContent);
    const effective = previews[environment.value] || previews[''];
    document.getElementById('api-suite-effective-url').textContent = effective.base_url;
    document.getElementById('api-suite-effective-timeout').textContent = effective.timeout + ' 秒';
  }
  mode.addEventListener('change', updateMode);
  plan.addEventListener('change', updateBuilds);
  environment.addEventListener('change', updateEnvironment);
  updateMode(); updateBuilds(); updateEnvironment();
  form.addEventListener('submit', event => {
    if (form.dataset.submitting === 'true') { event.preventDefault(); return; }
    form.dataset.submitting = 'true';
    const button = form.querySelector('button[type="submit"]');
    if (button) { button.disabled = true; button.textContent = '正在提交……'; }
  });
})();
