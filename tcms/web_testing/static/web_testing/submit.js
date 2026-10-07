(function () {
  'use strict';
  const form = document.getElementById('web-submit-form');
  if (!form) return;
  const mode = form.querySelector('[name="execution_mode"]');
  const plan = form.querySelector('[name="plan"]');
  const build = form.querySelector('[name="build"]');
  const env = form.querySelector('[name="environment"]');
  const plans = JSON.parse(document.getElementById('web-plan-versions').textContent);
  const builds = JSON.parse(document.getElementById('web-build-versions').textContent);
  let environments = JSON.parse(document.getElementById('web-environment-previews').textContent);
  function updateEnvironment() {
    try { environments = JSON.parse(document.getElementById('web-environment-previews').textContent); } catch (_) { return; }
    const effective = environments[env.value] || environments[''];
    document.getElementById('web-effective-url').textContent = effective.base_url;
    document.getElementById('web-effective-https').textContent = effective.ignore_https_errors ? '允许自签名证书' : '校验证书';
  }
  function updateMode() {
    const formal = mode.value === 'formal';
    ['plan', 'build'].forEach(name => {
      form.querySelector('[data-submit-field="' + name + '"]').hidden = !formal;
      form.querySelector('[name="' + name + '"]').required = formal;
      form.querySelector('[name="' + name + '"]').disabled = !formal;
    });
    env.required = formal;
    document.getElementById('web-formal-hint').hidden = !formal;
    document.getElementById('web-debug-hint').hidden = formal;
  }
  function updateBuilds() {
    const version = plans[plan.value];
    Array.from(build.options).forEach(option => {
      option.hidden = Boolean(option.value && version && builds[option.value] !== version);
      option.disabled = option.hidden;
    });
    if (build.selectedOptions[0]?.disabled) build.value = '';
  }
  mode.addEventListener('change', updateMode);
  plan.addEventListener('change', updateBuilds);
  env.addEventListener('change', updateEnvironment);
  updateMode(); updateBuilds(); updateEnvironment();
}());
