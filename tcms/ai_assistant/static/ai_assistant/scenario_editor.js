(function () {
    function initialize() {
        var form = document.getElementById('scenario-design-form');
        if (!form) return;
        var rows = document.getElementById('scenario-step-rows');
        var total = document.getElementById('id_steps-TOTAL_FORMS');
        var structured = form.querySelector('[data-structured-editor]');
        var raw = form.querySelector('[data-raw-editor]');
        var mode = form.dataset.designMode;
        var dirty = {structured: false, raw: false};
        structured.addEventListener('input', function () { dirty.structured = true; });
        raw.addEventListener('input', function () { dirty.raw = true; });
        function applyMode(value) {
            mode = value;
            [structured, raw].forEach(function (block, index) {
                var active = (index === 0) === (value === 'structured');
                block.hidden = !active;
                block.querySelectorAll('input,textarea,select,button').forEach(function (input) { input.disabled = !active; });
            });
        }
        form.querySelectorAll('[name="design_mode"]').forEach(function (radio) {
            radio.addEventListener('change', function () {
                if (radio.value !== mode && dirty[mode] && !window.confirm('切换编辑方式后，将保存另一种方式中的内容。是否继续切换？')) {
                    form.querySelector('[name="design_mode"][value="' + mode + '"]').checked = true;
                    return;
                }
                applyMode(radio.value);
            });
        });
        function renumber() {
            Array.from(rows.querySelectorAll('[data-step-row]')).forEach(function (row, index) {
                row.querySelector('[data-step-number]').textContent = index + 1;
                row.querySelectorAll('[name]').forEach(function (input) {
                    input.name = input.name.replace(/steps-(?:\d+|__prefix__)-/, 'steps-' + index + '-');
                    input.id = input.id.replace(/steps-(?:\d+|__prefix__)-/, 'steps-' + index + '-');
                });
                row.querySelector('[data-step-up]').disabled = index === 0;
                row.querySelector('[data-step-down]').disabled = index === rows.children.length - 1;
            });
            total.value = rows.children.length;
        }
        document.getElementById('scenario-step-add').addEventListener('click', function () {
            if (rows.children.length >= 100) { window.alert('最多可添加 100 个步骤。'); return; }
            rows.appendChild(document.getElementById('scenario-empty-step').content.cloneNode(true));
            renumber(); dirty.structured = true;
            rows.lastElementChild.querySelector('textarea').focus();
        });
        rows.addEventListener('click', function (event) {
            var button = event.target.closest('button'), row = button && button.closest('[data-step-row]');
            if (!row) return;
            if (button.hasAttribute('data-step-remove')) row.remove();
            else if (button.hasAttribute('data-step-up') && row.previousElementSibling) rows.insertBefore(row, row.previousElementSibling);
            else if (button.hasAttribute('data-step-down') && row.nextElementSibling) rows.insertBefore(row.nextElementSibling, row);
            renumber(); dirty.structured = true;
        });
        var saving = false, baseline;
        function snapshot() {
            return JSON.stringify(Array.from(new FormData(form).entries()).filter(function (entry) {
                return entry[0] !== 'csrfmiddlewaretoken' && entry[0] !== 'edit_token';
            }));
        }
        window.addEventListener('beforeunload', function (event) {
            if (!saving && (form.dataset.documentUnsaved === 'true' || snapshot() !== baseline)) { event.preventDefault(); event.returnValue = ''; }
        });
        form.addEventListener('submit', function (event) {
            if (saving) { event.preventDefault(); return; }
            if (mode === 'structured') renumber();
            saving = true;
            form.querySelectorAll('button[type="submit"]').forEach(function (button) { button.disabled = true; });
        });
        window.addEventListener('pageshow', function () {
            saving = false;
            form.querySelectorAll('button[type="submit"]').forEach(function (button) { button.disabled = false; });
        });
        form.querySelector('#id_test_type').setAttribute('list', 'scenario-test-types');
        applyMode(mode); renumber(); baseline = snapshot();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
    else initialize();
}());
