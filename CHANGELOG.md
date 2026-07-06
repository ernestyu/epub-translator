# Changelog

## 0.1.1 - 2026-07-06

### Changed

- Moved the UI language selector to the top of the **New Translation** tab so first-time users can switch languages immediately.
- UI language changes now apply to the current page immediately for the primary controls, tables, buttons, and messages.
- UI language changes are persisted to `.env` through `UI_LANGUAGE`.

### Fixed

- Fixed the previous behavior where saving `UI_LANGUAGE` in Settings did not visibly switch the interface.

### Notes

- Gradio tab titles are layout-level labels; if they do not update in-place in a running browser session, refresh the page after switching languages.

## 0.1.0 - 2026-07-06

### Added

- Initial GitHub-ready release.
- Docker/Gradio EPUB translation web app.
- Real EPUB preview with EPUB.js.
- Chapter/range translation preview.
- Background jobs with persisted state and chapter checkpoints.
- Batch retry, cache, JSON validation, and JSON repair.
- OpenAI-compatible LLM settings and model refresh/test controls.
- Chinese and English UI strings.
