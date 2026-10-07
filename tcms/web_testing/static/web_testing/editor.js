(function () {
  'use strict';
  const field = document.getElementById('id_steps');
  const editor = document.getElementById('web-step-editor');
  const schemaNode = document.getElementById('web-step-schema');
  if (!field || !editor || !schemaNode) return;
  const schema = JSON.parse(schemaNode.textContent);
  const metadata = Object.fromEntries(schema.map(item => [item.action, item]));
  const rows = document.getElementById('web-step-rows');
  const table = document.getElementById('web-step-table');
  const json = document.getElementById('web-step-json');
  const error = document.getElementById('web-editor-error');
  const add = document.getElementById('web-add-step');
  const form = field.closest('form');
  let steps = [], mode = 'json';
  const sync = () => { field.value = JSON.stringify(steps, null, 2); };
  function parse() {
    let parsed;
    try { parsed = JSON.parse(field.value); }
    catch (_) { throw new Error('JSON 格式不正确，请修正后再切换表格；原内容已保留。'); }
    if (!Array.isArray(parsed) || parsed.length > 30 || parsed.some(step => !step || Array.isArray(step) || typeof step !== 'object' || typeof step.action !== 'string' || !Object.hasOwn(metadata,step.action) || Object.keys(step).some(key => !['action','selector','value'].includes(key)) || ['selector','value'].some(key => key in step && typeof step[key] !== 'string'))) {
      throw new Error('步骤结构不支持表格编辑，请检查 action、selector、value；原内容已保留。');
    }
    return parsed;
  }
  function showError(message) { error.textContent = message; error.hidden = !message; }
  function showMode(next) {
    mode = next;
    table.hidden = next !== 'table'; json.hidden = next !== 'json'; field.hidden = next !== 'json';
    editor.querySelectorAll('[data-step-mode]').forEach(button => {
      const selected = button.dataset.stepMode === next;
      button.classList.toggle('is-selected',selected); button.setAttribute('aria-pressed',String(selected));
    });
  }
  function validate() {
    const issues = [];
    if (steps.length < 1) issues.push({message:'请至少添加一个步骤。'});
    if (!steps.some(step => metadata[step.action].assertion)) issues.push({message:'请至少添加一个断言。'});
    steps.forEach((step,index) => {
      const info = metadata[step.action];
      if (info.locator && !String(step.selector || '').trim()) issues.push({index,field:'selector',message:`第 ${index+1} 步：请填写元素定位器。`});
      if (info.required && !step.value) issues.push({index,field:'value',message:`第 ${index+1} 步：请填写输入或预期值。`});
      if (step.action === 'assert_count' && !/^\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}$/.test(step.value || '') && !(/^[0-9]{1,5}$/.test(step.value || '') && Number(step.value) <= 10000)) issues.push({index,field:'value',message:`第 ${index+1} 步：数量应为 0–10000 的整数或变量占位符。`});
      ['selector','value'].forEach(key => {if ((step[key] || '').length > 2000) issues.push({index,field:key,message:`第 ${index+1} 步：内容不能超过 2000 字符。`});});
    });
    rows.querySelectorAll('tr').forEach(row => row.classList.remove('web-step-bad'));
    rows.querySelectorAll('[aria-invalid]').forEach(input => input.removeAttribute('aria-invalid'));
    for (const issue of issues) {
      if (issue.index === undefined) continue;
      const row = rows.children[issue.index]; row.classList.add('web-step-bad');
      row.querySelector(`[data-step-field="${issue.field}"]`)?.setAttribute('aria-invalid','true');
    }
    showError(issues[0]?.message || '');
    if (issues.length) {
      const first = issues.find(issue => issue.field);
      if (first) rows.children[first.index].querySelector(`[data-step-field="${first.field}"]`)?.focus();
    }
    return !issues.length;
  }
  function render() {
    rows.replaceChildren();
    steps.forEach((step,index) => {
      const row = document.createElement('tr'); row.className='web-step-row';
      const cell = () => {const td=document.createElement('td'); row.append(td); return td;};
      cell().textContent=String(index+1);
      const select=document.createElement('select'); select.className='form-control'; select.setAttribute('aria-label',`第 ${index+1} 步操作类型`);
      for (const [label,assertion] of [['操作',false],['断言',true]]) {
        const group=document.createElement('optgroup');group.label=label;
        schema.filter(item => item.assertion===assertion).forEach(item => group.append(new Option(item.label,item.action)));
        select.append(group);
      }
      select.value=step.action; cell().append(select);
      const inputs={};
      for (const key of ['selector','value']) {
        const input=document.createElement('textarea');input.className='form-control';input.rows=2;input.maxLength=2000;
        input.dataset.stepField=key;input.value=step[key] || '';input.setAttribute('aria-label',`第 ${index+1} 步${key==='selector'?'定位器':'输入或预期值'}`);
        input.addEventListener('input',() => {step[key]=input.value;sync();input.removeAttribute('aria-invalid');});
        cell().append(input);inputs[key]=input;
      }
      function updateFields() {
        const info=metadata[step.action];
        inputs.selector.disabled=!info.locator;inputs.selector.placeholder=info.locator?'如 #username 或 text=登录':'无需定位器';
        inputs.value.disabled=!info.value;inputs.value.placeholder=info.placeholder || '无需填写';
      }
      select.addEventListener('change',() => {step.action=select.value;updateFields();sync();});
      updateFields();
      const controls=document.createElement('div');controls.className='web-step-controls';cell().append(controls);
      for (const [label,symbol,kind] of [['上移','↑','up'],['下移','↓','down'],['复制步骤','⧉','copy'],['删除','×','delete']]) {
        const button=document.createElement('button');button.type='button';button.title=label;button.setAttribute('aria-label',label);button.textContent=symbol;
        button.disabled=(kind==='up' && index===0)||(kind==='down' && index===steps.length-1)||(kind==='copy' && steps.length>=30);
        button.addEventListener('click',() => {
          if (kind==='delete') steps.splice(index,1);
          else if (kind==='copy') steps.splice(index+1,0,{...step});
          else {const target=index+(kind==='up'?-1:1);[steps[index],steps[target]]=[steps[target],steps[index]];}
          showError('');render();
        });controls.append(button);
      }
      rows.append(row);
    });
    add.disabled=steps.length>=30;document.getElementById('web-editor-limit').textContent=`${steps.length} / 30 步`;
    sync();
  }
  // Move, never copy, the original field so JSON submission has one authoritative value.
  const wrapper = document.getElementById('web-steps-field');
  json.append(field);
  if (wrapper) {
    wrapper.querySelectorAll('.errorlist').forEach(node => editor.insertBefore(node,table));
    wrapper.hidden=true;
    const section=wrapper.closest('.automation-form-section');
    if (section && section.querySelectorAll('.form-group').length===1) {
      section.querySelector('h2').hidden=true;
      section.append(editor);
    }
  }
  editor.hidden=false;
  try {steps=parse();render();showMode('table');}
  catch (exc) {showMode('json');showError(exc.message);}
  editor.querySelectorAll('[data-step-mode]').forEach(button => button.addEventListener('click',() => {
    if (button.dataset.stepMode==='json') {if(mode==='table')sync();showMode('json');showError('');return;}
    try {steps=parse();render();showMode('table');showError('');}
    catch (exc) {showMode('json');showError(exc.message);field.focus();}
  }));
  add.addEventListener('click',() => {if(steps.length<30){steps.push({action:'click',selector:'',value:''});render();rows.lastElementChild.querySelector('[data-step-field="selector"]').focus();}});
  form.addEventListener('submit',event => {
    if (mode==='table') {sync();if(!validate())event.preventDefault();}
    // JSON mode is validated by the same server validator and is never overwritten here.
  });
}());
