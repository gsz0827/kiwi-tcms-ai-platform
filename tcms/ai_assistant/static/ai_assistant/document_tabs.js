(function () {
    "use strict";
    document.querySelectorAll("[data-document-tabs]").forEach(function (list) {
        var links = Array.from(list.querySelectorAll("[data-document-tab]"));
        var workspace = list.closest(".document-workspace");
        function select(link, focus, updateUrl) {
            links.forEach(function (item) {
                var active = item === link;
                item.setAttribute("aria-selected", String(active));
                item.tabIndex = active ? 0 : -1;
                var panel = workspace.querySelector(item.getAttribute("href"));
                if (panel) { panel.hidden = !active; }
            });
            if (focus) { link.focus(); }
            if (updateUrl) { history.replaceState(null, "", link.getAttribute("href")); }
        }
        function fromHash() {
            var link = links.find(function (item) { return item.getAttribute("href") === location.hash; });
            if (link) { select(link, false, false); }
        }
        links.forEach(function (link, index) {
            link.addEventListener("click", function (event) {
                event.preventDefault(); select(link, false, true);
            });
            link.addEventListener("keydown", function (event) {
                var next;
                if (event.key === "ArrowRight") { next = (index + 1) % links.length; }
                else if (event.key === "ArrowLeft") { next = (index + links.length - 1) % links.length; }
                else if (event.key === "Home") { next = 0; }
                else if (event.key === "End") { next = links.length - 1; }
                if (next !== undefined) { event.preventDefault(); select(links[next], true, true); }
            });
        });
        fromHash(); window.addEventListener("hashchange", fromHash);
    });
    // Existing AI jobs link to the former request panels. Keep these URLs usable.
    var legacy = /^#request-(\d+)$/.exec(location.hash);
    if (legacy) {
        var row = document.getElementById("request-" + legacy[1]);
        var listing = document.querySelector("[data-legacy-detail-url]");
        var detailUrl = row && row.dataset.detailUrl;
        if (!detailUrl && listing) { detailUrl = listing.dataset.legacyDetailUrl.replace("/0/", "/" + legacy[1] + "/"); }
        if (detailUrl) { location.replace(detailUrl + "#analysis"); }
    }
}());
