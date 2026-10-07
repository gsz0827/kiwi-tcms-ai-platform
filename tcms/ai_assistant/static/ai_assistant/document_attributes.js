(function () {
    "use strict";
    function initialize() {
        document.querySelectorAll('form[data-task-assignees-url],form[data-document-options-url]').forEach(function (form) {
            var task = Boolean(form.dataset.taskAssigneesUrl);
            var source = form.querySelector(task ? '#id_request' : '#id_category');
            var assignee = form.querySelector(task ? '#id_assignee' : '#id_assigned_to');
            var versionField = form.querySelector('#id_target_version');
            if (!source || !assignee || !versionField) { return; }
            var status = document.createElement('p');
            status.className = 'help-block document-assignee-status';
            status.id = 'task-assignee-status'; status.setAttribute('role','status');
            status.setAttribute('aria-live','polite');
            assignee.setAttribute('aria-describedby',status.id);
            versionField.setAttribute('aria-describedby',status.id);
            assignee.parentNode.appendChild(status);
            var retry = document.createElement('button');
            retry.type = 'button'; retry.className = 'btn btn-default btn-sm document-assignee-retry';
            retry.textContent = '重试'; retry.hidden = true; assignee.parentNode.appendChild(retry);
            var fields = [assignee,versionField], preferred = fields.map(function (field) { return field.value; });
            var sequence = 0, controller, blocked = false;
            function clearOptions() {
                assignee.replaceChildren(new Option('未指派',''));
                versionField.replaceChildren(new Option('未指定目标版本',''));
            }
            function enable(enabled) { fields.forEach(function(field) { field.disabled = !enabled; }); }
            async function update(resetPreferred) {
                if (resetPreferred) { fields.forEach(function(field,index) { if (!field.disabled) { preferred[index] = field.value; } }); }
                var current = ++sequence;
                if (controller) { controller.abort(); }
                retry.hidden = true; clearOptions();
                var sourceId = source.value;
                if (!sourceId) {
                    enable(true); blocked = false; status.textContent = ''; assignee.removeAttribute('aria-busy'); return;
                }
                blocked = true; enable(false); assignee.setAttribute('aria-busy','true');
                status.textContent = '正在更新版本与负责人列表…';
                var params = new URLSearchParams(); params.set(task ? 'request' : 'category',sourceId);
                if (task && form.dataset.taskId) { params.set('task',form.dataset.taskId); }
                controller = new AbortController(); var active = controller;
                var timeout = setTimeout(function () { active.abort(); },10000);
                try {
                    var response = await fetch((task ? form.dataset.taskAssigneesUrl : form.dataset.documentOptionsUrl)+'?'+params, {
                        credentials:'same-origin',headers:{Accept:'application/json'},signal:active.signal
                    });
                    if (!response.ok) { throw new Error('Unavailable'); }
                    var payload = await response.json();
                    if (payload[task ? 'request_id' : 'category_id'] !== Number(sourceId) || !Array.isArray(payload.assignees) || !Array.isArray(payload.versions)) {
                        throw new Error('Invalid options');
                    }
                    if (current !== sequence || source.value !== sourceId) { return; }
                    var removed = [];
                    [payload.assignees,payload.versions].forEach(function(options,index) {
                        options.forEach(function(option) { fields[index].add(new Option(option.name,String(option.id))); });
                        var retained = Array.from(fields[index].options).some(function(option) { return option.value === preferred[index]; });
                        if (preferred[index] && !retained) { removed.push(index === 0 ? '负责人' : '目标版本'); }
                        fields[index].value = retained ? preferred[index] : ''; preferred[index] = fields[index].value;
                    });
                    status.textContent = removed.length ? '原'+removed.join('、')+'不属于当前项目，已清空，请重新选择。' : '';
                    enable(true); blocked = false;
                } catch (error) {
                    if (current === sequence) { status.textContent = '版本与负责人列表加载失败，请重试后保存。'; retry.hidden = false; }
                } finally {
                    clearTimeout(timeout);
                    if (current === sequence) { assignee.removeAttribute('aria-busy'); controller = null; }
                }
            }
            source.addEventListener('change',function () { update(true); });
            retry.addEventListener('click',function () { update(false); });
            form.addEventListener('submit',function(event) {
                if (blocked) {
                    event.preventDefault(); status.textContent = retry.hidden ? '版本与负责人列表仍在更新，请稍后保存。' : '版本与负责人列表加载失败，请重试后保存。';
                }
            },true);
        });
    }
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded',initialize); }
    else { initialize(); }
}());
