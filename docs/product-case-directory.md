# Product-root case directory

The primary business case library `/ai/scenarios/` uses native `Product` records
as virtual root nodes. New products immediately appear on the next page load;
renaming a product updates its root and computed paths. There are no duplicated
product-named `ProjectResourceFolder` rows, new tables, or data migrations.

Existing shared `case_group` folders and legacy `case` folders retain IDs,
parent relationships, assignments, and permissions. A native business case
without a valid, visible same-product assignment appears directly below its
product root. The library no longer offers an unfiled bucket. Legacy unfiled
links display the selected product's case list instead. Other legacy resource
browsers retain their compatibility filters.

The pane has a centered heading, ID/name search, expandable product/folder
nodes, and directly nested case nodes. It spans all visible cases, while the
right table follows selected product/folder filters and remains paginated.
At most 2,000 matching case nodes are rendered. A visible limit notice directs
users to server-side search when capped. Off-page case details load on demand
through a login-required, GET-only, permission-filtered, non-cacheable preview.
Private automation payloads are never fetched for this preview.

The path table column shows `Product / folder / ...`; right-click copying a
case includes `TC-ID · name` as the final segment. Product/folder paths can also
be copied. Paths are calculated from current relationships and are not stored
in business case bodies or immutable execution snapshots.

Product roots allow permitted shared-folder creation and path copying, not
product renaming/deletion. Visible read-only nodes permit copying without
write targets. Existing POST actions retain ownership/edit checks, CSRF,
same-product/type validation and folder-cycle protection. Dragging a case to
its root clears only its folder assignment. Moving a folder preserves its
subfolders and all case associations. Divider resizing remains unchanged.

Verification includes business-workflow/directory regression tests and a
Chromium acceptance run using unique disposable products, folders, cases and
sessions. Browser checks cover hierarchy, padded TC-ID search, collapse,
off-page previews, copying, root creation, case/folder drops, cross-product
rejection, divider resizing, and mobile layout. No real AI calls or target
executions are made by these checks.

Pre-change source backup:
`/home/lenovo/D/kiwi-dev-platform/backups/pre-product-case-tree-20261003-PlogXF/source.tar.gz`
