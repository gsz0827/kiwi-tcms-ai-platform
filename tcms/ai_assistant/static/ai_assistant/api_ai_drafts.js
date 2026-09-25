// 接口用例 AI 生成：任务仍在执行时轮询真实状态，避免用户手动刷新页面。
// 状态来自 ai_assistant:job_status（数据库中的真实阶段与进度），
// 与后台任务详情页使用同一个端点，因此两处显示始终一致。
(function () {
    var panel = document.getElementById("api-ai-job");
    if (!panel) { return; }

    var statusUrl = panel.dataset.statusUrl;
    var bar = document.getElementById("api-ai-job-bar");
    var statusLabel = document.getElementById("api-ai-job-status");
    var progressText = document.getElementById("api-ai-job-progress");
    var stage = document.getElementById("api-ai-job-stage");
    var error = document.getElementById("api-ai-job-error");

    function setStage(text) {
        if (stage) { stage.textContent = text; }
    }

    var timer = window.setInterval(function () {
        window.fetch(statusUrl, {credentials: "same-origin"}).then(function (response) {
            if (!response.ok) { throw new Error("状态请求失败"); }
            return response.json();
        }).then(function (data) {
            statusLabel.textContent = data.status_label;
            progressText.textContent = data.progress + "%";
            setStage(data.stage || "");
            if (bar) {
                bar.style.width = data.progress + "%";
                bar.setAttribute("aria-valuenow", data.progress);
            }
            if (!data.is_terminal) { return; }
            window.clearInterval(timer);
            if (bar) { bar.classList.remove("active"); }
            if (data.status === "completed") {
                // 草稿已经在本次请求中落库，重新加载即可看到完整列表。
                window.location.reload();
                return;
            }
            // 失败或取消：就地显示原因，页面上的重试与重新生成入口仍然可用。
            error.textContent = data.error_message || data.status_label;
            error.style.display = "block";
            setStage("任务已结束，可重新生成或查看后台任务详情。");
        }).catch(function () {
            setStage("暂时无法读取状态，正在重试……");
        });
    }, 2000);
}());
