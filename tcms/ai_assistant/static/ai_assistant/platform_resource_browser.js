(function () {
    function initializeBrowser() {
        var browsers = document.querySelectorAll("[data-resource-browser]");
        browsers.forEach(function (browser) {
            var kind = browser.getAttribute("data-resource-browser");
            var filter = browser.querySelector(".kiwi-resource-filter");
            var noMatch = browser.querySelector(".kiwi-resource-no-match");

            document.querySelectorAll(".kiwi-resource-modal").forEach(function (modal) {
                if (modal.parentNode !== document.body) {
                    document.body.appendChild(modal);
                }
            });

            if (filter && noMatch) {
                filter.addEventListener("input", function () {
                    var term = filter.value.trim().toLocaleLowerCase();
                    var visibleCount = 0;
                    browser.querySelectorAll(".kiwi-resource-item").forEach(function (item) {
                        var visible = !term || item.textContent.toLocaleLowerCase().indexOf(term) !== -1;
                        item.hidden = !visible;
                        if (visible) visibleCount += 1;
                    });
                    noMatch.hidden = visibleCount !== 0;
                });
            }

            var productSelect = document.getElementById("folder-product-" + kind);
            var parentSelect = document.getElementById("folder-parent-" + kind);
            if (productSelect && parentSelect) {
                var filterParents = function () {
                    var productId = productSelect.value;
                    Array.prototype.forEach.call(parentSelect.options, function (option) {
                        option.hidden = Boolean(option.value) && option.dataset.product !== productId;
                    });
                    if (parentSelect.selectedOptions.length && parentSelect.selectedOptions[0].hidden) {
                        parentSelect.value = "";
                    }
                };
                productSelect.addEventListener("change", filterParents);
                filterParents();
            }

            browser.querySelectorAll(".kiwi-resource-move-select").forEach(function (select) {
                select.addEventListener("change", function () {
                    if (select.form) select.form.submit();
                });
            });

            browser.querySelectorAll(".kiwi-folder-delete-form").forEach(function (form) {
                form.addEventListener("submit", function (event) {
                    if (!window.confirm(form.getAttribute("data-confirm"))) {
                        event.preventDefault();
                    }
                });
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initializeBrowser);
    } else {
        initializeBrowser();
    }
}());
