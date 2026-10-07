(function () {
    "use strict";
    function initialize() {
        document.querySelectorAll("form[data-task-assignees-url]").forEach(function (form) {
            var requirement = form.querySelector('select[name="request"]');
            var assignee = form.querySelector('select[name="assignee"]');
            if (!requirement || !assignee) { return; }
            var status = document.createElement("p");
            status.className = "help-block document-assignee-status";
            status.setAttribute("role", "status");
            status.setAttribute("aria-live", "polite");
            status.id = "task-assignee-status";
            assignee.setAttribute("aria-describedby", status.id);
            assignee.parentNode.appendChild(status);
            var retry = document.createElement("button");
            retry.type = "button"; retry.className = "btn btn-default btn-sm document-assignee-retry";
            retry.textContent = "重试"; retry.hidden = true;
            assignee.parentNode.appendChild(retry);
            var sequence = 0;
            var controller;
            var blocked = false;
            var preferred = assignee.value;
            function clearOptions() {
                assignee.replaceChildren(new Option("未指派", ""));
            }
            async function update(resetPreferred) {
                if (resetPreferred) { preferred = assignee.disabled ? preferred : assignee.value; }
                sequence += 1;
                var version = sequence;
                if (controller) { controller.abort(); }
                retry.hidden = true;
                clearOptions();
                var requestId = requirement.value;
                if (!requestId) {
                    assignee.disabled = false; blocked = false;
                    status.textContent = ""; assignee.removeAttribute("aria-busy"); return;
                }
                blocked = true; assignee.disabled = true;
                assignee.setAttribute("aria-busy", "true");
                status.textContent = "正在更新负责人列表…";
                var params = new URLSearchParams({request:requestId});
                if (form.dataset.taskId) { params.set("task", form.dataset.taskId); }
                controller = new AbortController();
                var activeController = controller;
                var timeout = setTimeout(function () { activeController.abort(); }, 10000);
                try {
                    var response = await fetch(form.dataset.taskAssigneesUrl + "?" + params, {
                        credentials:"same-origin", headers:{Accept:"application/json"}, signal:activeController.signal
                    });
                    if (!response.ok) { throw new Error("Unavailable"); }
                    var payload = await response.json();
                    if (payload.request_id !== Number(requestId) || !Array.isArray(payload.assignees)) {
                        throw new Error("Invalid options");
                    }
                    if (version !== sequence || requirement.value !== requestId) { return; }
                    payload.assignees.forEach(function (person) {
                        assignee.add(new Option(person.name, String(person.id)));
                    });
                    var retained = Array.from(assignee.options).some(function (option) { return option.value === preferred; });
                    assignee.value = retained ? preferred : "";
                    status.textContent = preferred && !retained ? "原负责人不属于当前项目，已清空，请重新选择。" : "";
                    preferred = assignee.value;
                    assignee.disabled = false; blocked = false;
                } catch (error) {
                    if (version === sequence) {
                        status.textContent = "负责人列表加载失败，请重试后保存。";
                        retry.hidden = false;
                    }
                } finally {
                    clearTimeout(timeout);
                    if (version === sequence) { assignee.removeAttribute("aria-busy"); controller = null; }
                }
            }
            requirement.addEventListener("change", function () { update(true); });
            retry.addEventListener("click", function () { update(false); });
            // Capture runs before the editor's submit handler, keeping unsaved-change protection intact.
            form.addEventListener("submit", function (event) {
                if (blocked) {
                    event.preventDefault();
                    status.textContent = retry.hidden ? "负责人列表仍在更新，请稍后保存。" : "负责人列表加载失败，请重试后保存。";
                }
            }, true);
        });
    }
    if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", initialize); }
    else { initialize(); }
}());
