(function () {
    "use strict";
    function initialize() {
        var forms = Array.from(document.querySelectorAll("[data-document-form]"));
        var states = forms.map(function (form) {
            function snapshot() {
                return JSON.stringify(Array.from(new FormData(form).entries()).filter(function (pair) {
                    return pair[0] !== "csrfmiddlewaretoken";
                }));
            }
            var state = { form:form, initial:snapshot(), snapshot:snapshot, submitting:false };
            form.addEventListener("submit", function (event) {
                if (event.defaultPrevented) { return; }
                if (state.submitting) { event.preventDefault(); return; }
                state.submitting = true;
                form.querySelectorAll("[data-document-save]").forEach(function (button) {
                    button.disabled = true; button.textContent = "保存中…";
                });
            });
            return state;
        });
        window.addEventListener("beforeunload", function (event) {
            if (states.some(function (state) { return !state.submitting && (state.form.dataset.documentUnsaved === 'true' || state.snapshot() !== state.initial); })) {
                event.preventDefault(); event.returnValue = "";
            }
        });
        document.querySelectorAll("[data-document-editor]").forEach(function (editor) {
            var input = editor.querySelector("textarea");
            var inputPane = editor.querySelector("[data-editor-input]");
            var preview = editor.querySelector("[data-editor-preview]");
            var writeButton = editor.querySelector("[data-editor-write]");
            var previewButton = editor.querySelector("[data-editor-preview-button]");
            var controller = null;
            var sequence = 0;
            function stop() {
                sequence += 1;
                if (controller) { controller.abort(); controller = null; }
                preview.removeAttribute("aria-busy");
            }
            function write(focus) {
                stop(); inputPane.hidden = false; preview.hidden = true;
                writeButton.setAttribute("aria-pressed", "true");
                previewButton.setAttribute("aria-pressed", "false");
                if (focus) { input.focus(); }
            }
            writeButton.addEventListener("click", function () { write(true); });
            // Reveal required text before native form validation tries to focus it.
            input.addEventListener("invalid", function () { write(false); });
            previewButton.addEventListener("click", async function () {
                stop();
                var version = sequence;
                var value = input.value;
                var previewValue = value;
                if (editor.hasAttribute('data-document-items') && !value.split(/\r?\n/).some(function(line) { return /^\s*(?:#{1,6}\s|[-+*]\s|\d+[.)]\s|[>|]|```|~~~)/.test(line); })) {
                    previewValue = value.split(/\r?\n/).filter(function(line) { return line.trim(); }).map(function(line,index) { return (index+1)+'. '+line.trim(); }).join('\n');
                }
                inputPane.hidden = true; preview.hidden = false;
                writeButton.setAttribute("aria-pressed", "false");
                previewButton.setAttribute("aria-pressed", "true");
                if (!value.trim()) { preview.textContent = "暂无内容"; return; }
                preview.textContent = "正在加载预览…";
                preview.setAttribute("aria-busy", "true");
                controller = new AbortController();
                var activeController = controller;
                var timeout = setTimeout(function () { activeController.abort(); }, 10000);
                try {
                    var token = input.form.querySelector('[name="csrfmiddlewaretoken"]').value;
                    var response = await fetch("/json-rpc/", {
                        method:"POST", credentials:"same-origin", signal:activeController.signal,
                        headers:{"Content-Type":"application/json", "X-CSRFToken":token},
                        body:JSON.stringify({jsonrpc:"2.0", method:"Markdown.render", params:[previewValue], id:"document-preview"})
                    });
                    if (!response.ok) { throw new Error("Preview unavailable"); }
                    var payload = await response.json();
                    if (payload.error || typeof payload.result !== "string") { throw new Error("Invalid preview"); }
                    if (version !== sequence || input.value !== value) { return; }
                    // RPC HTML is escaped once after server-side sanitization. Decode once only.
                    var decoder = document.createElement("textarea");
                    decoder.innerHTML = payload.result;
                    preview.innerHTML = decoder.value;
                } catch (error) {
                    if (version === sequence) { preview.textContent = "预览暂不可用，请返回编辑继续修改。"; }
                } finally {
                    clearTimeout(timeout);
                    if (version === sequence) { controller = null; preview.removeAttribute("aria-busy"); }
                }
            });
        });
    }
    if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", initialize); }
    else { initialize(); }
}());
