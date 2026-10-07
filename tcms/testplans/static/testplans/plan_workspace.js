(function () {
    'use strict';
    function ready() {
        const form = document.getElementById('plan-filter-form');
        if (form) {
            const product = form.querySelector('[data-plan-products]');
            product.addEventListener('change', function () {
                form.querySelector('[name="product_version"]').value = '';
                const folder = form.querySelector('[name="folder"]');
                if (folder) folder.remove();
                form.requestSubmit();
            });
        }
        const tabs = Array.from(document.querySelectorAll('[data-plan-tab]'));
        if (!tabs.length) return;
        function select(name, focus) {
            const active = tabs.find(tab => tab.dataset.planTab === name) || tabs[0];
            tabs.forEach(tab => {
                const selected = tab === active;
                tab.setAttribute('aria-selected', String(selected));
                tab.tabIndex = selected ? 0 : -1;
            });
            document.querySelectorAll('[data-plan-panel]').forEach(panel => {
                panel.hidden = panel.dataset.planPanel !== active.dataset.planTab;
            });
            if (focus) active.focus();
        }
        tabs.forEach((tab, index) => {
            tab.addEventListener('click', function () {
                select(tab.dataset.planTab);
                history.replaceState(null, '', location.pathname + location.search + '#' + tab.dataset.planTab);
            });
            tab.addEventListener('keydown', function (event) {
                let target;
                if (event.key === 'ArrowRight') target = (index + 1) % tabs.length;
                if (event.key === 'ArrowLeft') target = (index + tabs.length - 1) % tabs.length;
                if (event.key === 'Home') target = 0;
                if (event.key === 'End') target = tabs.length - 1;
                if (target !== undefined) { event.preventDefault(); tabs[target].click(); tabs[target].focus(); }
            });
        });
        document.querySelectorAll('[data-plan-summary-link]').forEach(link => {
            link.addEventListener('click', () => select('hierarchy'));
        });
        window.addEventListener('hashchange', () => select(location.hash.slice(1)));
        select(location.hash.slice(1));
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready);
    else ready();
}());
