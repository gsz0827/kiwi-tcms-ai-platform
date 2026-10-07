(() => {
  'use strict';
  const form = document.getElementById('automation-draft-form');
  if (!form) return;
  const all = document.getElementById('automation-draft-all');
  const save = document.getElementById('automation-draft-save');
  const count = document.getElementById('automation-draft-selection');
  if (!all || !save || !count) return;
  const eligible = [...form.querySelectorAll('input[name="draft_ids"]:not(:disabled)')];
  function update() {
    const chosen = eligible.filter(item => item.checked).length;
    count.textContent = `已选 ${chosen} 条`;
    save.disabled = chosen === 0;
    all.disabled = eligible.length === 0;
    all.checked = chosen > 0 && chosen === eligible.length;
    all.indeterminate = chosen > 0 && chosen < eligible.length;
  }
  all.addEventListener('change', () => { eligible.forEach(item => { item.checked = all.checked; }); update(); });
  eligible.forEach(item => item.addEventListener('change', update));
  form.addEventListener('submit', event => {
    if (!eligible.some(item => item.checked)) { event.preventDefault(); all.focus(); }
  });
  window.addEventListener('pageshow', update);
  update();
})();
