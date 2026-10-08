(() => {
  'use strict';
  const make = (tag, text, cls) => { const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(cls)node.className=cls; return node; };
  const button = (text, action) => { const node=make('button',text,'btn btn-default');node.type='button';node.addEventListener('click',action);return node; };
  const typeOf = value => value===null?'null':Array.isArray(value)?'array':typeof value;
  const encode = value => typeof value==='string'?value:JSON.stringify(value);
  function decoded(value,type){
    if(type==='string')return value;
    const parsed=JSON.parse(value);
    if(typeOf(parsed)!==type || (type==='number'&&!Number.isFinite(parsed)))throw new Error('值与数据类型不符');
    return parsed;
  }
  function jsonEditor(textarea,kind){
    let data;
    try{data=JSON.parse(textarea.value|| (kind==='assertions'?'[]':'{}'));}catch(_){return;}
    const array=kind==='assertions', extract=kind==='extracts';
    if(extract && data && Object.values(data).some(value=>typeof value!=='string'))return;
    if(array ? !Array.isArray(data)||data.some(v=>!v||typeof v!=='object'||Array.isArray(v)) : !data||typeof data!=='object'||Array.isArray(data))return;
    const host=make('div',undefined,'automation-json-editor');
    const actions=make('div',undefined,'automation-json-actions');
    const table=make('table',undefined,'table');const header=make('thead');const head=make('tr');
    for(const title of array?['字段路径','比较方式','预期值','数据类型','操作']:extract?['变量名','响应字段路径','操作']:['名称','值','数据类型','操作'])head.append(make('th',title));
    header.append(head);table.append(header);const tbody=make('tbody');table.append(tbody);
    const error=make('p','','automation-json-error');error.setAttribute('role','alert');error.hidden=true;
    let raw=false,dirty=false;
    function input(value,label){const node=make('input',undefined,'form-control');node.value=value??'';node.setAttribute('aria-label',label);return node;}
    function options(values,value,label){const node=make('select',undefined,'form-control');node.setAttribute('aria-label',label);for(const item of values){const labels={equals:'等于',exists:'存在',string:'文本',number:'数字',boolean:'布尔值',null:'空值',object:'对象',array:'数组'};const option=make('option',labels[item]||item);option.value=item;node.append(option);}node.value=value;return node;}
    function changed(){dirty=true;serialize();}
    function row(key,value,meta){
      const tr=make('tr');tr.meta=meta||{};
      const keyInput=input(key,array?'字段路径':extract?'变量名':'名称');tr.keyInput=keyInput;
      const op=array?options(['equals','exists'],meta?.operator||'equals','比较方式'):null;tr.op=op;
      const valueInput=input(encode(value),extract?'响应字段路径':'值');tr.valueInput=valueInput;
      const type=options(['string','number','boolean','null','object','array'],typeOf(value),'数据类型');tr.typeInput=type;
      for(const node of [keyInput,...(op?[op]:[]),valueInput,...(extract?[]:[type])]){const td=make('td');td.append(node);tr.append(td);node.addEventListener('input',changed);node.addEventListener('change',changed);}
      const td=make('td');td.append(button('删除',()=>{tr.remove();changed();}));tr.append(td);tbody.append(tr);
      function toggleExpected(){valueInput.disabled=!!op&&op.value==='exists';type.disabled=valueInput.disabled;}
      if(op)op.addEventListener('change',toggleExpected);toggleExpected();
    }
    function draw(){tbody.replaceChildren();if(array){for(const entry of data)row(entry.path,Object.hasOwn(entry,'expected')?entry.expected:'',entry);}else{for(const [key,value] of Object.entries(data))row(key,value);} }
    function serialize(){
      if(raw||!dirty)return true;
      try{
        const result=array?[]:Object.create(null);const keys=new Set();
        for(const tr of tbody.children){
          const key=tr.keyInput.value.trim();if(!key)throw new Error('名称或字段路径不能为空');
          if(!array&&keys.has(key))throw new Error('存在重复名称，请先修改');keys.add(key);
          const value=array&&tr.op.value==='exists'?undefined:decoded(tr.valueInput.value,tr.typeInput.value);
          if(array){const entry={...tr.meta,path:key,operator:tr.op.value};if(value!==undefined)entry.expected=value;else delete entry.expected;result.push(entry);}else{result[key]=value;}
        }
        textarea.value=JSON.stringify(result,null,2);error.hidden=true;textarea.dispatchEvent(new Event('input',{bubbles:true}));return true;
      }catch(e){error.textContent=e.message+'；请修正后再保存或切换编辑方式。';error.hidden=false;return false;}
    }
    const mode=button('JSON 编辑',()=>{
      if(!raw&&!serialize())return;
      if(raw){try{const value=JSON.parse(textarea.value|| (array?'[]':'{}'));if(array?!Array.isArray(value)||value.some(v=>!v||typeof v!=='object'||Array.isArray(v)):!value||typeof value!=='object'||Array.isArray(value))throw new Error('结构不符');data=value;draw();dirty=false;}catch(_){error.textContent='JSON 格式或结构不正确';error.hidden=false;return;}}
      raw=!raw;table.hidden=raw;add.hidden=raw;textarea.hidden=!raw;mode.textContent=raw?'表格编辑':'JSON 编辑';
    });
    const add=button('添加一行',()=>{row('',array?'':'');changed();});actions.append(add,mode);
    host.append(table,actions,error);textarea.after(host);textarea.hidden=true;draw();
    textarea.form?.addEventListener('submit',event=>{if(!serialize()){event.preventDefault();event.stopImmediatePropagation();textarea.dispatchEvent(new CustomEvent('automation:reveal',{bubbles:true}));error.scrollIntoView({block:'center'});}},true);
  }
  function orderedPicker(field, orderInput) {
    const select=field.querySelector('select[multiple]');
    const checks=[...field.querySelectorAll('input[type="checkbox"]')];
    if(!select&&!checks.length)return;
    const options=select||field.querySelector('div[id]')||checks[0].closest('ul');
    if(!options)return;
    const candidates=()=>select?[...select.options]:checks;
    const value=item=>String(item.value);
    const chosen=item=>select?item.selected:item.checked;
    const label=item=>(select?item.textContent:item.closest('label')?.textContent||item.parentElement.textContent).trim();
    let order;
    try{order=JSON.parse(orderInput.value||'[]').map(String);}catch(_){order=[];}
    let dragging=null;
    const search=make('input',undefined,'form-control automation-picker-search');
    search.type='search';search.placeholder='按脚本编号或名称筛选';search.setAttribute('aria-label','筛选执行脚本');
    const heading=make('h3','执行顺序','automation-order-heading');
    const selected=make('ol',undefined,'automation-selected-list automation-ordered-list');
    selected.setAttribute('aria-label','执行顺序');
    const status=make('span','','sr-only');status.setAttribute('role','status');
    options.classList.add('automation-picker-options');
    options.before(search);options.after(heading,selected,status);
    search.addEventListener('keydown',event=>{if(event.key==='Enter')event.preventDefault();});
    function move(id,target) {
      if(!order.includes(id)||!order.includes(target)||id===target)return;
      const index=order.indexOf(target);
      order.splice(order.indexOf(id),1);order.splice(index,0,id);
      refresh();
      status.textContent='已调整执行顺序';
    }
    function refresh() {
      const active=candidates().filter(chosen),ids=active.map(value);
      order=order.filter(id=>ids.includes(id));
      for(const id of ids)if(!order.includes(id))order.push(id);
      orderInput.value=JSON.stringify(order.map(Number));
      selected.replaceChildren();
      const byId=new Map(active.map(item=>[value(item),item]));
      if(!order.length){selected.append(make('li','尚未添加脚本','text-muted'));return;}
      order.forEach((id,index)=>{
        const item=byId.get(id),li=make('li');
        li.dataset.caseId=id;
        const handle=make('span','⠿','automation-order-handle');
        handle.draggable=true;handle.title='拖动调整顺序';
        handle.addEventListener('dragstart',event=>{dragging=id;event.dataTransfer.effectAllowed='move';event.dataTransfer.setData('text/plain',id);li.classList.add('is-dragging');});
        handle.addEventListener('dragend',()=>{dragging=null;li.classList.remove('is-dragging');});
        li.addEventListener('dragover',event=>{if(dragging&&dragging!==id){event.preventDefault();event.dataTransfer.dropEffect='move';}});
        li.addEventListener('drop',event=>{if(dragging){event.preventDefault();const source=dragging;dragging=null;move(source,id);}});
        const content=make('span',undefined,'automation-order-name');
        content.append(make('span',String(index+1),'automation-order-position'),make('span',label(item)));
        const controls=make('span',undefined,'automation-order-actions');
        for(const [text,delta,name] of [['↑',-1,'上移'],['↓',1,'下移']]){
          const control=button(text,()=>{const next=order[index+delta];if(next){move(id,next);selected.querySelector('[data-case-id="'+id+'"] button[data-action="'+name+'"]')?.focus();}});
          control.dataset.action=name;control.setAttribute('aria-label',name+' '+label(item));
          control.disabled=index+delta<0||index+delta>=order.length;controls.append(control);
        }
        const remove=button('移除',()=>{if(select)item.selected=false;else item.checked=false;refresh();status.textContent='已移除脚本';});
        remove.setAttribute('aria-label','移除 '+label(item));controls.append(remove);
        li.append(handle,content,controls);selected.append(li);
      });
    }
    search.addEventListener('input',()=>{
      const term=search.value.trim().toLowerCase();
      for(const item of candidates()){const node=select?item:item.closest('div')||item.closest('li')||item.parentElement;node.hidden=!label(item).toLowerCase().includes(term);}
    });
    options.addEventListener('change',()=>refresh());refresh();
    field.closest('form').addEventListener('submit',()=>refresh(),true);
  }

  document.querySelectorAll('.automation-edit-form textarea').forEach(textarea=>{
    if(['query','headers','variables','extracts','assertions'].includes(textarea.name))jsonEditor(textarea,textarea.name);
  });
  document.querySelectorAll('.automation-edit-form [data-field="cases"]').forEach(field=>{
    const orderInput=field.closest('form').querySelector('[name="ordered_case_ids"]');
    if(orderInput){orderedPicker(field,orderInput);return;}
    const select=field.querySelector('select[multiple]');const checks=[...field.querySelectorAll('input[type="checkbox"]')];
    if(!select&&!checks.length)return;
    const search=make('input',undefined,'form-control automation-picker-search');search.type='search';search.placeholder='按脚本编号或名称筛选';search.setAttribute('aria-label','筛选执行脚本');
    const selected=make('ol',undefined,'automation-selected-list');selected.setAttribute('aria-label','已选执行脚本');
    const options=select||field.querySelector('div[id]')||checks[0].closest('ul');if(!options)return;
    options.classList.add('automation-picker-options');options.before(search);options.after(selected);
    search.addEventListener('keydown',event=>{if(event.key==='Enter')event.preventDefault();});
    function refresh(){
      selected.replaceChildren();const candidates=select?[...select.options]:checks;
      for(const item of candidates){if(!(select?item.selected:item.checked))continue;const label=select?item.textContent:item.closest('label')?.textContent||item.parentElement.textContent;
        const li=make('li');li.append(make('span',label.trim()),button('移除',()=>{if(select)item.selected=false;else item.checked=false;refresh();}));selected.append(li);}
    }
    search.addEventListener('input',()=>{const term=search.value.trim().toLowerCase();if(select){for(const option of select.options)option.hidden=!option.textContent.toLowerCase().includes(term);}else for(const check of checks){const node=check.closest('div')||check.closest('li')||check.parentElement;node.hidden=!node.textContent.toLowerCase().includes(term);}});
    options.addEventListener('change',refresh);refresh();
  });
  const environment=document.querySelector('.automation-edit-form select[name="environment"]');
  if(environment&&!document.getElementById('automation-environment-previews')){const preview=document.getElementById('automation-environment-preview');function refresh(){if(preview)preview.textContent=environment.selectedOptions[0]?.textContent||'未选择';}environment.addEventListener('change',refresh);refresh();}
})();
