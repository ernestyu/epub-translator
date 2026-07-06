# Changelog

## 0.1.3 - 2026-07-06

### Changed

- Replaced Gradio tab labels with an updateable page selector so **New Translation**, **Jobs**, and **Settings** switch language immediately.
- Replaced Gradio accordion titles with updateable section headings for preview, translation defaults, and LLM settings.
- Replaced Gradio browser-locale-driven upload prompt text and disabled the default footer so framework chrome does not leak Chinese into the English UI.

### Fixed

- Fixed remaining Chinese labels in the English UI after live language switching, including page navigation, preview table headers, job detail table headers, and settings section headings.

## 0.1.2 - 2026-07-06

### Changed

- Completed the English UI pass by removing mixed Chinese/English labels from runtime UI code outside the translation dictionary.
- Localized EPUB reader controls, worker status messages, settings option labels, and job-loading errors.
- Added a Chinese README and language links between English and Chinese documentation.
- Expanded Docker build and deployment instructions.
- Added project acknowledgements, including a note that the current implementation does not depend on `oomol-lab/epub-translator` at runtime.

### Fixed

- Fixed mixed-language output mode and chapter failure policy labels in the English UI.
- Fixed mixed-language Previous/Next controls inside the EPUB preview reader.

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
