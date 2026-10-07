(function () {
    function initialize() {
        var collapsed = new Set();
        document.querySelectorAll('[data-hub-toggle]').forEach(function (button) {
            button.addEventListener('click', function () {
                var id = button.dataset.hubToggle;
                if (collapsed.has(id)) collapsed.delete(id); else collapsed.add(id);
                button.setAttribute('aria-expanded', String(!collapsed.has(id)));
                button.firstElementChild.className = 'fa ' + (collapsed.has(id) ? 'fa-angle-right' : 'fa-angle-down');
                document.querySelectorAll('[data-hub-folder]').forEach(function (node) {
                    node.hidden = node.dataset.hubAncestors.split(',').some(function (parent) { return collapsed.has(parent); });
                });
            });
        });
        var product = document.getElementById('case-hub-product');
        if (product) product.addEventListener('change', function () {
            var folder = product.form.querySelector('input[name="folder"]');
            if (folder) folder.value = '';
        });
        var batch = document.getElementById('hub-batch');
        var selectPage = document.getElementById('hub-select-page');
        var checks = Array.from(document.querySelectorAll('.hub-case-check'));
        function updateSelection() {
            var selected = checks.filter(function (input) { return input.checked; });
            if (!batch) return;
            document.getElementById('hub-selected-count').textContent = '已选 ' + selected.length + ' 条';
            selectPage.checked = checks.length > 0 && selected.length === checks.length;
            selectPage.indeterminate = selected.length > 0 && selected.length < checks.length;
            batch.querySelectorAll('[data-hub-batch-action]').forEach(function (button) {
                button.disabled = !selected.length || (button.value !== 'export' && selected.some(function (input) {
                    return input.dataset.canMove !== 'true';
                }));
            });
        }
        if (selectPage) selectPage.addEventListener('change', function () {
            checks.forEach(function (input) { input.checked = selectPage.checked; });
            updateSelection();
        });
        checks.forEach(function (input) { input.addEventListener('change', updateSelection); });
        if (batch) batch.addEventListener('submit', function (event) {
            var action = event.submitter && event.submitter.value;
            if (action === 'move' && !document.getElementById('hub-batch-folder').value) {
                event.preventDefault(); window.alert('请先选择目标共享目录。'); return;
            }
            if (action === 'unfile' && !window.confirm('移出目录后，用例会变为未归档；不会删除用例。继续吗？')) {
                event.preventDefault();
            }
        });
        updateSelection();
        document.querySelectorAll('.hub-case-move-select').forEach(function (select) {
            select.addEventListener('change', function () { if (select.form) select.form.submit(); });
        });
        document.querySelectorAll('#hub-directory-manager .kiwi-folder-delete-form').forEach(function (form) {
            form.addEventListener('submit', function (event) {
                if (!window.confirm(form.dataset.confirm)) event.preventDefault();
            });
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
    else initialize();
}());
