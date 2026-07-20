# Changelog

## 0.1.6 - 2026-07-20

### Added

- Added EPUB-only reliability SPEC for verifiable translation retries without introducing a unified multi-format document model.
- Added partial LLM response handling so valid translated items are preserved while missing IDs are retried separately.
- Added batch split and single-unit failure propagation with failed text block IDs.
- Added neighbor context and per-batch glossary term injection to improve translation consistency.
- Added optional glossary input in the New Translation flow.
- Added failed/warning text block counters in job summaries and chapter details.

### Changed

- Translation preview now uses the same context, glossary, partial retry, and failed-unit marker flow as full jobs.
- Jobs that keep original text after unit-level failures now finish as `finished_with_warnings` instead of normal `finished`.
- README and Chinese README now document verifiable retries, glossary input, and version `0.1.6`.

### Fixed

- Prevented successful translations in a partially valid LLM response from being discarded when other IDs are missing.
- Prevented stubborn unit-level translation failures from being silently omitted in generated EPUB output.

## 0.1.5 - 2026-07-09

### Changed

- In `append_block` mode, table translations now keep the original table intact and insert a translated table copy after it.
- Added bilingual table styling so copied translation tables are visually separated without changing the source table layout.

### Fixed

- Fixed table layout corruption caused by inserting translated `td`/`th` siblings into existing table rows.
- Avoided duplicate extraction for table cells that already contain translatable child elements.

## 0.1.4 - 2026-07-06

### Fixed

- Replaced the Gradio radio-based page selector with plain front-end navigation buttons so **Jobs** and **Settings** open immediately when clicked.
- Kept the three-page layout fully localizable while avoiding Gradio tab labels and radio state synchronization issues.

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
