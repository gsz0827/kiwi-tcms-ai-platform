(function () {
  'use strict';
  const field = document.getElementById('id_steps');
  const editor = document.getElementById('web-step-editor');
  if (!field || !editor) return;
  let steps;
  try { steps = JSON.parse(field.value); } catch (_) { return; }
  if (!Array.isArray(steps)) return;
  const actions = {goto:'打开页面',click:'点击',fill:'输入文字',select:'选择选项',check:'勾选',assert_visible:'断言元素可见',assert_text:'断言包含文本',assert_url:'断言网址包含',assert_title:'断言标题包含'};
  const rows = document.getElementById('web-step-rows');
  const sync = () => {field.value = JSON.stringify(steps);};
  function render() {
    rows.replaceChildren();
    steps.forEach((step, index) => {
      const row = document.createElement('div'); row.className = 'web-step-row';
      const number = document.createElement('span'); number.textContent = index + 1; row.append(number);
      const select = document.createElement('select'); select.className = 'form-control'; select.setAttribute('aria-label','操作类型');
      Object.entries(actions).forEach(([value, label]) => { const option = new Option(label, value); select.add(option); });
      select.value = step.action; select.addEventListener('change', () => {step.action = select.value; sync();}); row.append(select);
      [['selector','定位器，例如 #username'],['value','路径、输入值或预期文本']].forEach(([name, label]) => {
        const input = document.createElement('input'); input.className = 'form-control'; input.placeholder = label; input.setAttribute('aria-label',label); input.value = step[name] || ''; input.maxLength = 2000;
        input.addEventListener('input', () => {step[name] = input.value; sync();}); row.append(input);
      });
      [['上移',-1],['下移',1],['删除',0]].forEach(([label, delta]) => {
        const button = document.createElement('button'); button.type='button'; button.className='btn btn-default'; button.textContent=label;
        button.disabled = delta && (index+delta<0 || index+delta>=steps.length);
        button.addEventListener('click', () => {if (!delta) steps.splice(index,1); else [steps[index],steps[index+delta]]=[steps[index+delta],steps[index]]; render();}); row.append(button);
      }); rows.append(row);
    }); sync();
  }
  field.hidden = true;
  const label = document.querySelector('label[for="id_steps"]');
  if (label) label.hidden = true;
  editor.hidden = false; render();
  document.getElementById('web-add-step').addEventListener('click', () => {if(steps.length>=30)return; steps.push({action:'click',selector:'',value:''}); render();});
}());
