// 接口自动化的表单提交后会写库或投递后台任务，重复提交没有意义。
// 提交瞬间禁用按钮并给出反馈；服务器端的幂等校验仍然保留，作为兜底。
(function () {
    var forms = document.querySelectorAll(".api-workspace form.api-edit-form");
    Array.prototype.forEach.call(forms, function (form) {
        form.addEventListener("submit", function (event) {
            if (form.dataset.submitting === "true") {
                event.preventDefault();
                return;
            }
            form.dataset.submitting = "true";
            var button = form.querySelector("button[type=submit], button:not([type])");
            if (!button) { return; }
            button.disabled = true;
            button.textContent = "正在提交……";
        });
    });
    window.addEventListener("pageshow", function (event) {
        if (event.persisted) { window.location.reload(); }
    });
}());
