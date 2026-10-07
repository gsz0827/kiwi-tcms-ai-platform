(function () {
    function initialize() {
        var tree = document.querySelector('[data-product-tree]');
        if (!tree) return;
        var nodes = Array.from(tree.querySelectorAll('[data-product-tree-node]'));
        var input = tree.querySelector('input[type="search"]');
        var prefix = ['r', 'tp', 'tr'].includes(tree.dataset.treeNumberPrefix) ? tree.dataset.treeNumberPrefix : 'tc';
        var numberPattern = new RegExp('^(?:' + prefix + '-)?(\\d{1,18})$');
        var numberKey = tree.dataset.treeNumberKey || (prefix === 'r' ? 'r:' : 'c:');
        var collapsed = new Set(nodes.filter(function (node) {
            return node.dataset.nodeKind === 'product' && node.dataset.productTreeNode !== 'p:' + tree.dataset.selectedProduct;
        }).map(function (node) { return node.dataset.productTreeNode; }));
        var byKey = new Map(nodes.map(function (node) { return [node.dataset.productTreeNode, node]; }));
        function ancestors(node) { return node.dataset.nodeAncestors.split(',').filter(Boolean); }
        function render() {
            var term = input.value.trim().toLocaleLowerCase();
            var matches = new Set();
            if (term) {
                nodes.forEach(function (node) {
                    var number = numberPattern.exec(term);
                    if (node.dataset.nodeSearch.toLocaleLowerCase().includes(term) || (number && node.dataset.productTreeNode === numberKey + Number(number[1]))) {
                        matches.add(node.dataset.productTreeNode);
                        ancestors(node).forEach(function (key) { matches.add(key); });
                    }
                });
            }
            nodes.forEach(function (node) {
                var parents = ancestors(node);
                node.hidden = term ? !matches.has(node.dataset.productTreeNode) : parents.some(function (key) { return collapsed.has(key); });
                var toggle = node.querySelector('[data-product-tree-toggle]');
                if (toggle) {
                    var expanded = Boolean(term) || !collapsed.has(node.dataset.productTreeNode);
                    toggle.setAttribute('aria-expanded', String(expanded));
                    toggle.querySelector('span').className = expanded ? 'fa fa-angle-down' : 'fa fa-angle-right';
                }
            });
            tree.querySelector('#product-tree-no-match').hidden = !term || nodes.some(function (node) { return !node.hidden; });
        }
        input.addEventListener('input', render);
        var searchButton = tree.querySelector('[data-tree-search]');
        if (searchButton) searchButton.addEventListener('click', function () { render(); input.focus(); });
        tree.addEventListener('click', function (event) {
            var toggle = event.target.closest('[data-product-tree-toggle],[data-product-tree-folder]');
            if (toggle) {
                event.preventDefault();
                var key = toggle.dataset.productTreeToggle || toggle.dataset.productTreeFolder;
                if (collapsed.has(key)) collapsed.delete(key); else collapsed.add(key);
                // Clear the local search to allow explicitly collapsing matched branches.
                if (input.value) input.value = '';
                render();
            }
        });
        tree.addEventListener('keydown', function (event) {
            var node = event.target.closest('[data-product-tree-node]');
            if (!node || !event.target.matches('a')) return;
            var key = node.dataset.productTreeNode;
            if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
                event.preventDefault();
                if (event.key === 'ArrowLeft') {
                    if (node.querySelector('[data-product-tree-toggle]') && !collapsed.has(key)) collapsed.add(key);
                    else { var parent = byKey.get(ancestors(node).pop()); if (parent) parent.querySelector('a').focus(); }
                } else collapsed.delete(key);
                render();
            }
        });
        render();
        var modal = document.getElementById('scenario-tree-preview');
        var pending = null;
        tree.addEventListener('click', function (event) {
            var link = event.target.closest('[data-case-preview-url]');
            if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || !modal) return;
            event.preventDefault();
            if (pending) pending.abort();
            var controller = new AbortController();
            pending = controller;
            var body = modal.querySelector('.modal-body');
            body.textContent = '正在加载用例…';
            modal.querySelector('[data-preview-detail]').href = link.href;
            window.jQuery(modal).modal('show');
            fetch(link.dataset.casePreviewUrl, {credentials:'same-origin', signal:controller.signal, headers:{'X-Requested-With':'XMLHttpRequest'}})
                .then(function (response) { if (!response.ok || response.redirected) throw new Error('load'); return response.text(); })
                .then(function (html) { if (pending === controller) body.innerHTML = html; })
                .catch(function (error) { if (error.name !== 'AbortError' && pending === controller) body.textContent = '无法加载用例，请重试或打开详情。'; });
        });
        if (modal) window.jQuery(modal).on('hidden.bs.modal', function () { if (pending) pending.abort(); pending = null; });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize); else initialize();
}());
