(function () {
    var statusUrl = document.getElementById("job-panel").dataset.statusUrl;
    var timer = window.setInterval(function () {
        window.fetch(statusUrl, {credentials: "same-origin"}).then(function (response) {
            if (!response.ok) { throw new Error("状态请求失败"); }
            return response.json();
        }).then(function (data) {
            var bar = document.getElementById("job-progress-bar");
            document.getElementById("job-status-label").textContent = data.status_label;
            document.getElementById("job-progress-text").textContent = data.progress + "%";
            document.getElementById("job-stage").textContent = "当前阶段：" + data.stage;
            bar.style.width = data.progress + "%";
            bar.setAttribute("aria-valuenow", data.progress);
            if (!data.is_terminal) { return; }
            window.clearInterval(timer);
            bar.classList.remove("active");
            document.getElementById("job-actions").style.display = "none";
            if (data.status === "completed") {
                document.getElementById("job-result").style.display = "block";
                var link = document.getElementById("result-link");
                if (data.result_url) { link.href = data.result_url; link.style.display = "inline-block"; }
                if (data.result && data.result.reply) {
                    document.getElementById("connection-result").textContent = "模型响应：" + data.result.reply + "，耗时 " + data.result.elapsed_ms + " 毫秒。";
                }
            } else {
                var error = document.getElementById("job-error");
                error.textContent = data.error_message || data.status_label;
                error.style.display = "block";
                window.setTimeout(function () { window.location.reload(); }, 800);
            }
        }).catch(function () {
            document.getElementById("job-stage").textContent = "当前阶段：暂时无法读取状态，正在重试……";
        });
    }, 1000);
}());
