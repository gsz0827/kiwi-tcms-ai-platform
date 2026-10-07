# Automation configuration directory inheritance

Web and API configuration lists now derive their directory membership from
their linked native business test case. Product roots and shared `case_group`
or legacy `case` folders come from the same source as `/ai/scenarios/`.
Configuration-specific `web_case`/`api_case` folder assignments no longer
participate in these two lists, but are retained for history/compatibility.
There is no database migration, automatic relinking, or deletion of old data.

The pane is a read-only view of the business taxonomy. It allows folder/case
selection, ID/name search, collapse, path copying, and divider resizing.
Folder creation/renaming/deletion and case/folder dragging remain in the
business case library. A link takes users to that library for directory
management. Case tree nodes include only visible business cases linked to
matching configurations owned by the current account, without exposing other
accounts' configuration names or payloads.

`q` searches configuration names; `case_q` searches business case names or
native `TC-ID`; `business_case` filters implementations of one visible,
same-product business case. Folder filters include descendants and operate
only on visible business-folder assignments. `association=unlinked` lists
only configurations whose business-case FK is null. Root-level linked cases
are not mislabeled as unlinked. By default, all owned configurations are kept
visible, including legacy unlinked records. Pagination preserves filters.

The configuration table displays the business directory path and links to the
primary business case detail. A configuration whose linked case is no longer
visible (or belongs to another product) remains owned and editable, but shows
an unavailable association instead of leaking its case name/path. Changing a
business case's folder, renaming/moving/deleting its folder, or linking another
implementation recomputes the directory view without rewriting configuration
payloads, timestamps, business bodies, or immutable execution snapshots.

The legacy configuration-folder CRUD/assignment endpoints remain available
for compatibility; their existing permissions, CSRF and product/type guards
are unchanged. Their controls are absent from these configuration pages.
Unrelated requirement/plan menus and specialised suite/execution flows are
unchanged.

Verification: directory inheritance, multi-implementation filtering, unlinked
vs root distinctions, folder updates, invalid/cross-product IDs, owner and
case-view permission boundaries, plus full business-workflow regression and
Chromium UI checks. UI checks use finally-cleaned disposable case/config/folder
fixtures; no real AI calls or target test executions.

Source backup: `/home/lenovo/D/kiwi-dev-platform/backups/pre-inherited-automation-dirs-20261003-vUZF1h/`.
