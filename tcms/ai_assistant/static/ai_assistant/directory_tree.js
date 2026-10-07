(function () {
    function initialize() {
        var pane = document.querySelector('.kiwi-resource-pane');
        var workspace = pane && pane.closest('.kiwi-resource-workspace');
        var tree = pane && pane.querySelector('[data-resource-browser]');
        var dialog = document.getElementById('directory-quick-dialog');
        var menu = document.getElementById('directory-context-menu');
        if (!tree || !dialog || !menu) return;
        if (tree.dataset.treeToolsReady) return;
        tree.dataset.treeToolsReady = 'true';
        document.body.appendChild(menu);
        if (dialog.parentNode !== document.body) document.body.appendChild(dialog);
        var targetNode = null;
        var menuScrollTop = pane.scrollTop;
        var returnFocus = null;
        var form = document.getElementById('directory-quick-form');
        var name = document.getElementById('directory-quick-name');
        var product = document.getElementById('directory-quick-product');
        function closeMenu(focus) {
            menu.hidden = true;
            if (focus && returnFocus) returnFocus.focus();
        }
        function showMenu(node, x, y) {
            targetNode = node;
            menuScrollTop = pane.scrollTop;
            returnFocus = node.matches('a,button') ? node : node.querySelector('a,button');
            menu.querySelectorAll('[data-tree-command]').forEach(function (button) {
                var command = button.dataset.treeCommand;
                var isCase = Boolean(node.dataset.caseNode) || node.dataset.treeType === 'case';
                if (command === 'new-case') button.hidden = !node.dataset.treeNewCaseUrl;
                else if (command === 'child-plan') button.hidden = !node.dataset.treeChildPlanUrl;
                else if (command === 'copy-path') button.hidden = !node.dataset.treePath;
                else if (command === 'create') button.hidden = isCase || !node.dataset.treeCreateUrl;
                else if (command === 'case-rename') button.hidden = !isCase || !node.dataset.treeRenameUrl;
                else if (command === 'rename') button.hidden = isCase || !node.dataset.treeRenameUrl;
                else if (command === 'delete') button.hidden = isCase || !node.dataset.treeDeleteUrl;
            });


            menu.hidden = false;
            menu.style.left = Math.max(0, Math.min(x, window.innerWidth - menu.offsetWidth - 8)) + 'px';
            menu.style.top = Math.max(0, Math.min(y, window.innerHeight - menu.offsetHeight - 8)) + 'px';
            menu.querySelector('button:not([hidden])').focus();
        }
        tree.addEventListener('contextmenu', function (event) {
            if (event.target.closest('input,textarea,select,button,[contenteditable]')) return;
            var node = event.target.closest('[data-directory-node],[data-case-node],[data-tree-path]');
            if (!node || !tree.contains(node)) return;
            event.preventDefault();
            showMenu(node, event.clientX, event.clientY);
        });
        tree.addEventListener('keydown', function (event) {
            var node = event.target.closest('[data-directory-node],[data-case-node],[data-tree-path]');
            if (!node) return;
            if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
                event.preventDefault();
                var box = event.target.getBoundingClientRect();
                showMenu(node, box.left, box.bottom);
            }
            if (event.key === 'F2' && node.dataset.treeRenameUrl) {
                event.preventDefault();
                openDialog(node.dataset.caseNode ? 'case-rename' : 'rename', node);
            }
        });
        function openDialog(command, node) {
            closeMenu(false);
            var create = command === 'create', remove = command === 'delete', caseRename = command === 'case-rename';
            var data = node.dataset;
            var original = data.treeName || '';
            form.action = create ? data.treeCreateUrl : remove ? data.treeDeleteUrl : data.treeRenameUrl;
            form.elements.resource_type.value = data.treeKind || '';
            form.elements.parent.value = create ? data.directoryNode || '' : '';
            form.elements.expected_name.value = original;
            form.elements.next.value = window.location.pathname + window.location.search;
            if (remove) {
                var next = new URL(window.location.href);
                ['folder','page','case_page'].forEach(function (key) { next.searchParams.delete(key); });
                form.elements.next.value = next.pathname + next.search;
            }
            document.getElementById('directory-quick-title').textContent = create ? (data.directoryNode ? '新建子文件夹' : '新建文件夹') : remove ? '删除文件夹' : caseRename ? '重命名用例' : '重命名文件夹';
            document.getElementById('directory-quick-target').textContent = original ? '当前：' + original : '';
            document.getElementById('directory-quick-product-field').hidden = !create;
            product.disabled = !create;
            product.required = create;
            product.value = data.treeProduct || '';
            document.getElementById('directory-quick-name-field').hidden = remove;
            name.disabled = remove;
            name.required = !remove;
            name.maxLength = caseRename ? Number(data.treeNameMax) : 120;
            name.value = create ? '' : original;
            document.getElementById('directory-quick-delete-note').hidden = !remove;
            document.getElementById('directory-quick-rename-note').hidden = !caseRename;
            var submit = document.getElementById('directory-quick-submit');
            submit.textContent = remove ? '确认删除' : create ? '创建' : '保存';
            submit.className = remove ? 'btn btn-danger' : 'btn btn-primary';
            window.jQuery(dialog).modal('show');
            window.jQuery(dialog).one('shown.bs.modal', function () { if (!remove) { name.focus(); name.select(); } else { submit.focus(); } });
        }
        document.querySelectorAll('[data-directory-create]').forEach(function (button) {
            button.addEventListener('click', function () { openDialog('create', button); });
        });
        var copyNotice = document.createElement('div');
        copyNotice.className = 'directory-copy-status';
        copyNotice.setAttribute('role', 'status');
        copyNotice.setAttribute('aria-live', 'polite');
        copyNotice.hidden = true;
        document.body.appendChild(copyNotice);
        var copyTimer;
        function copyPath(node) {
            closeMenu(true);
            var value = node && node.dataset.treePath;
            if (!value) return;
            function feedback() {
                copyNotice.textContent = '路径已复制';
                copyNotice.hidden = false;
                clearTimeout(copyTimer);
                copyTimer = setTimeout(function () { copyNotice.hidden = true; }, 2200);
            }
            function fallback() {
                var field = document.createElement('textarea');
                field.value = value;
                field.style.cssText = 'position:fixed;left:-9999px;top:0';
                document.body.appendChild(field);
                field.select();
                var copied = false;
                try { copied = document.execCommand('copy'); } catch (error) {}
                field.remove();
                if (returnFocus) returnFocus.focus();
                if (copied) feedback(); else window.prompt('请复制路径', value);
            }
            if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(value).then(feedback, fallback);
            else fallback();
        }


        menu.addEventListener('click', function (event) {
            var button = event.target.closest('[data-tree-command]');
            if (button && !button.hidden) {
                if (button.dataset.treeCommand === 'new-case') {
                    closeMenu(false);
                    if (targetNode.dataset.treeNewCaseUrl) window.location.assign(targetNode.dataset.treeNewCaseUrl);
                }
                else if (button.dataset.treeCommand === 'child-plan') {
                    closeMenu(false);
                    if (targetNode.dataset.treeChildPlanUrl) window.location.assign(targetNode.dataset.treeChildPlanUrl);
                }
                else if (button.dataset.treeCommand === 'copy-path') copyPath(targetNode);
                else openDialog(button.dataset.treeCommand, targetNode);
            }
        });
        menu.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') { closeMenu(true); return; }
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                var buttons = Array.from(menu.querySelectorAll('button:not([hidden])'));
                var index = buttons.indexOf(document.activeElement);
                buttons[(index + (event.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length].focus();
            }
        });
        document.addEventListener('pointerdown', function (event) { if (!menu.contains(event.target)) closeMenu(false); });
        document.addEventListener('keydown', function (event) { if (event.key === 'Escape') closeMenu(false); });
        window.addEventListener('resize', function () { closeMenu(false); });
        pane.addEventListener('scroll', function () { if (pane.scrollTop !== menuScrollTop) closeMenu(false); });


        if (tree.querySelector('[data-tree-drop]')) {
        // Only drags originating in this tree may invoke its existing POST actions.
        var moving = null, submitted = false;
        var moveForm = document.getElementById('directory-move-form');
        var status = document.createElement('p');
        status.className = 'directory-drag-status';
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        var rootTools = tree.querySelector('.directory-root-tools');
        if (rootTools) rootTools.insertAdjacentElement('afterend', status);
        else tree.prepend(status);
        var dropNodes = Array.from(tree.querySelectorAll('[data-tree-drop]'));
        function clearDropStyles() {
            dropNodes.forEach(function (node) { node.classList.remove('directory-drop-valid', 'directory-drop-active', 'directory-drop-invalid'); });
        }
        function finishMove() {
            if (moving) moving.node.classList.remove('directory-drag-source');
            moving = null;
            clearDropStyles();
            if (!submitted) status.textContent = '';
        }
        function dropProblem(target) {
            if (!moving || !target) return '请选择目标目录。';
            var data = target.dataset;
            if (data.dropProduct && data.dropProduct !== moving.product) return '不能跨项目移动。';
            if (data.treeDrop === 'root') return moving.folder || data.dropAcceptCase === 'true' ? '' : '用例请拖到文件夹或“未归档”。';
            if (data.treeDrop === 'unfiled') return moving.folder ? '文件夹请拖到“项目根目录”。' : '';
            if (data.dropKind !== moving.kind && data.dropKind !== 'case_group') return '目录类型不兼容，请选择同类目录或共享业务目录。';
            if (moving.folder) {
                var node = target, visited = new Set();
                while (node) {
                    var id = node.dataset.dropId;
                    if (id === moving.id) return '不能移到自身或子文件夹中。';
                    if (visited.has(id)) return '目录层级异常，请刷新后重试。';
                    visited.add(id);
                    var parent = node.dataset.dropParent;
                    node = dropNodes.find(function (candidate) { return candidate.dataset.dropId === parent; });
                }
            }
            return '';
        }
        tree.addEventListener('dragstart', function (event) {
            var node = event.target.closest('[data-tree-drag]');
            if (!node || !tree.contains(node) || submitted || !moveForm) { event.preventDefault(); return; }
            closeMenu(false);
            moving = {node:node, folder:node.dataset.treeDrag === 'folder', id:node.dataset.dragId,
                kind:node.dataset.dragKind, product:node.dataset.dragProduct};
            if (!moving.id || !moving.product || !moving.kind) { finishMove(); event.preventDefault(); return; }
            // No names, request data or credentials are placed on the drag clipboard.
            event.dataTransfer.setData('application/x-kiwi-directory', moving.id);
            event.dataTransfer.effectAllowed = 'move';
            node.classList.add('directory-drag-source');
            dropNodes.forEach(function (target) { if (!dropProblem(target)) target.classList.add('directory-drop-valid'); });
            status.textContent = moving.folder ? '拖到目标文件夹或项目根目录，子目录与用例会一同保留。' : tree.dataset.productRooted ? '拖到目标文件夹或所属项目根目录。' : '拖到目标文件夹，或拖到“未归档”。';
        });
        tree.addEventListener('dragover', function (event) {
            if (!moving) return;
            event.preventDefault();
            var target = event.target.closest('[data-tree-drop]');
            dropNodes.forEach(function (node) { node.classList.remove('directory-drop-active', 'directory-drop-invalid'); });
            var problem = dropProblem(target);
            event.dataTransfer.dropEffect = problem ? 'none' : 'move';
            if (target) target.classList.add(problem ? 'directory-drop-invalid' : 'directory-drop-active');
            status.textContent = problem || '松开鼠标，保存新的目录位置。';
        });
        tree.addEventListener('dragleave', function (event) {
            var target = event.target.closest('[data-tree-drop]');
            if (target && !target.contains(event.relatedTarget)) target.classList.remove('directory-drop-active', 'directory-drop-invalid');
        });
        tree.addEventListener('drop', function (event) {
            // External files/text never become a resource move.
            if (!moving) return;
            event.preventDefault(); event.stopPropagation();
            var target = event.target.closest('[data-tree-drop]');
            var problem = dropProblem(target);
            if (problem) { finishMove(); status.textContent = problem; return; }
            var source = moving, folderId = target.dataset.dropId || '';
            if (source.folder && folderId === (source.node.dataset.dragParent || '')) { finishMove(); status.textContent = '文件夹已在该位置。'; return; }
            moveForm.replaceChildren();
            function field(key, value) { var input = document.createElement('input'); input.type = 'hidden'; input.name = key; input.value = value; moveForm.appendChild(input); }
            field('csrfmiddlewaretoken', form.elements.csrfmiddlewaretoken.value);
            field('next', window.location.pathname + window.location.search);
            if (source.folder) {
                moveForm.action = source.node.dataset.treeMoveUrl;
                field('parent', folderId);
            } else {
                moveForm.action = moveForm.dataset.caseUrl;
                field('resource_type', source.kind); field('object_id', source.id); field('folder', folderId);
            }
            // Existing endpoints re-check edit/owner rights, product, type and cycles.
            submitted = true;
            finishMove();
            status.textContent = '正在保存目录位置…';
            moveForm.submit();
        });
        tree.addEventListener('dragend', finishMove);
        document.addEventListener('keydown', function (event) { if (event.key === 'Escape' && moving) finishMove(); });
        }

        var handle = document.createElement('button');
        handle.type = 'button'; handle.className = 'directory-resizer';
        handle.setAttribute('role', 'separator'); handle.setAttribute('aria-orientation', 'vertical');
        handle.setAttribute('aria-label', '调整目录宽度'); handle.setAttribute('aria-valuemin', '220');
        handle.setAttribute('title', '拖动调整目录宽度；方向键微调；双击恢复默认');
        pane.insertAdjacentElement('afterend', handle);
        var storageKey = 'kiwi-directory-width:v1:' + (pane.dataset.account || '') + ':' + tree.dataset.resourceBrowser;
        var width = 310, dragging = null;
        function maximum() { return Math.max(220, Math.min(560, workspace.clientWidth - 480)); }
        function apply(value, persist) {
            width = Math.round(Math.max(220, Math.min(maximum(), value)));
            workspace.style.setProperty('--directory-width', width + 'px');
            handle.setAttribute('aria-valuemax', String(maximum())); handle.setAttribute('aria-valuenow', String(width));
            if (persist) { try { localStorage.setItem(storageKey, String(width)); } catch (error) {} }
        }
        try { var saved = Number(localStorage.getItem(storageKey)); if (Number.isFinite(saved) && saved >= 220 && saved <= 560) width = saved; } catch (error) {}
        apply(width, false);
        handle.addEventListener('pointerdown', function (event) {
            if (event.button !== 0) return;
            event.preventDefault(); dragging = {x:event.clientX,width:width};
            handle.setPointerCapture(event.pointerId); document.body.classList.add('directory-resizing');
        });
        handle.addEventListener('pointermove', function (event) { if (dragging) apply(dragging.width + event.clientX - dragging.x, false); });
        function stopDrag() { if (dragging) apply(width, true); dragging = null; document.body.classList.remove('directory-resizing'); }
        handle.addEventListener('pointerup', stopDrag); handle.addEventListener('pointercancel', stopDrag); handle.addEventListener('lostpointercapture', stopDrag);
        handle.addEventListener('keydown', function (event) {
            if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); apply(width + (event.key === 'ArrowRight' ? 10 : -10), true); }
            if (event.key === 'Home') { event.preventDefault(); apply(310, true); }
        });
        handle.addEventListener('dblclick', function () { apply(310, true); });
        document.querySelectorAll('.directory-width-reset').forEach(function (button) { button.addEventListener('click', function () { apply(310, true); }); });
        window.addEventListener('resize', function () { apply(width, false); });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
    else initialize();
}());
