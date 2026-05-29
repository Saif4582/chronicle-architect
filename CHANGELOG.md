## v0.5.0.1 – Performance & Code Quality Release

### Performance
- Wiki sidebar extracted to independent render function (event delegation, optimistic DnD)
- TipTap polling guard added (saves ~5s on subsequent loads)
- All drag-and-drop zones use sidebar-only re-renders (no full page rebuild)
- Event delegation replaces ~200 inline handlers on sidebar tree
- Editor topbar, wiki topbar, landing page, wiki refs panel, collapsed chat container all wired behind partial updates

### Bug Fixes
- Fixed pendingWikiMutation guard (now properly prevents concurrent DnD)
- Removed dead variables (currentDragZone, formParentId)
- Hoisted renderEditorTopbar and renderCollapsedChatPanel to global scope (fixes editor crash)
- Added global escHtml() function (fixes wiki crash with usePartialUpdates=true)

### Backend (carried over from v0.5.0)
- Database indexes, N+1 query fix, combined /move endpoint, transaction wrapper

### Known Issues
- Wiki sidebar shows 0 entries when usePartialUpdates=true (pre-existing data race)
