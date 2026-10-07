// Same-origin handlers work with the deployment's script-src policy.
(function () {
    document.querySelectorAll("[data-select-drafts]").forEach(function (toggle) {
        toggle.addEventListener("change", function () {
            toggle.form.querySelectorAll('input[name="draft_ids"]').forEach(function (box) {
                if (!box.disabled) { box.checked = toggle.checked; }
            });
        });
    });
    document.querySelectorAll("[data-print-report]").forEach(function (button) {
        button.addEventListener("click", function () { window.print(); });
    });
    document.querySelectorAll("[data-confirm]").forEach(function (confirmation) {
        // Folder forms have their own confirmation handler in platform_resource_browser.js.
        if (!confirmation.form) { return; }
        confirmation.form.addEventListener("submit", function (event) {
            var button = event.submitter || confirmation;
            if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) {
                event.preventDefault();
            }
        });
    });
    document.querySelectorAll(".model-test-form, #run-analysis-form, #review-form, #report-generation-form, #defect-generation-form").forEach(function (form) {
        form.addEventListener("submit", function (event) {
            if (event.defaultPrevented) { return; }
            if (form.dataset.submitting === "true") {
                event.preventDefault();
                return;
            }
            form.dataset.submitting = "true";
            var button = event.submitter || form.querySelector('button[type="submit"]');
            if (button && button.name) {
                var action = document.createElement("input");
                action.type = "hidden";
                action.name = button.name;
                action.value = button.value;
                form.appendChild(action);
            }
            form.querySelectorAll('button[type="submit"]').forEach(function (item) { item.disabled = true; });
            if (button) { button.textContent = "提交中…"; }
        });
    });
    window.addEventListener("pageshow", function (event) {
        if (event.persisted) { window.location.reload(); }
    });
}());
