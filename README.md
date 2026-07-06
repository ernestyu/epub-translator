# EPUB Translator Web

Local, Docker-friendly EPUB bilingual translation web app with checkpointed background jobs.

EPUB Translator Web lets you upload an EPUB, preview it in the browser, translate selected chapter ranges for a quick visual check, and then run a full-book translation using an OpenAI-compatible LLM API. It keeps the EPUB/XHTML structure under program control: the LLM only translates plain text and returns JSON.

## Features

- Browser-based Gradio UI with Chinese/English interface language support.
- Real EPUB rendering preview powered by EPUB.js.
- Sample translation preview by chapter and character range.
- Background translation jobs that keep running after page refresh or browser disconnect.
- Persistent job state in `data/jobs/<job_id>/state.json`.
- Chapter-level checkpoints in `data/jobs/<job_id>/translated/`.
- Batch-level JSON validation, retry, cache, and conservative JSON repair.
- Token-budgeted batching based on `LLM_CONTEXT_WINDOW`.
- OpenAI-compatible Chat Completions API support.
- Job list, job detail, resume, rerun failed chapters, cancel, delete job and output.
- Output EPUB files saved under `data/output/`.

## Quick Start

1. Copy the example environment file:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` for your LLM endpoint:

   ```env
   LLM_API_KEY=ollama
   LLM_BASE_URL=http://host.docker.internal:11434/v1
   LLM_MODEL=qwen3:32b
   LLM_CONTEXT_WINDOW=8192
   UI_LANGUAGE=en
   ```

3. Start with Docker Compose:

   ```bash
   docker compose up -d --build
   ```

4. Open:

   ```text
   http://localhost:7860
   ```

Generated books are written to:

```text
./data/output
```

## UI Workflow

The app has three tabs:

1. **New Translation**
   - Upload an EPUB.
   - Preview the real EPUB rendering.
   - Select a chapter and a character range for sample translation.
   - Run a full-book translation or only the selected preview range.

2. **Jobs**
   - Refresh and select jobs.
   - View job details and chapter status.
   - Resume jobs, rerun failed chapters, cancel jobs, or delete a job and its output.
   - Download finished EPUB files.

3. **Settings**
   - Configure translation defaults.
   - Configure LLM provider, base URL, API key, model, and context window.
   - Refresh models from the provider and test the selected model.
   - Choose UI language (`zh` or `en`).
   - Save settings to `.env`.

The UI language setting is persisted to `.env`. Refresh or restart the app for the whole interface to be rebuilt in the selected language.

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `LLM_API_KEY` | `ollama` | API key for your OpenAI-compatible endpoint. |
| `LLM_BASE_URL` | `http://host.docker.internal:11434/v1` | OpenAI-compatible base URL. |
| `LLM_MODEL` | `qwen3:32b` | Model name. |
| `LLM_CONTEXT_WINDOW` | `8192` | Context window used for automatic batch sizing. |
| `SOURCE_LANGUAGE` | `English` | Default source language. |
| `TARGET_LANGUAGE` | `Simplified Chinese` | Default target language. |
| `UI_LANGUAGE` | `zh` | UI language: `zh` or `en`. |
| `DEFAULT_OUTPUT_MODE` | `append_block` | `append_block` or `replace`. |
| `DEFAULT_CHAPTER_FAILURE_POLICY` | `keep_original_on_failed_chapter` | Failure policy for a failed chapter. |
| `DEFAULT_TRANSLATE_TITLES` | `true` | Translate heading elements. |
| `DEFAULT_TRANSLATE_FOOTNOTES` | `true` | Translate footnote-like elements. |
| `APP_HOST` | `0.0.0.0` | Gradio host. |
| `APP_PORT` | `7860` | Gradio port. |
| `DATA_DIR` | `/data` | Runtime data directory inside Docker. |

## How Translation Works

1. The uploaded EPUB is unpacked.
2. The OPF package and spine are parsed.
3. XHTML chapters are processed in spine order.
4. Translatable text blocks are extracted from tags such as `p`, `li`, `blockquote`, and headings.
5. Text blocks are batched with an automatic token budget derived from `LLM_CONTEXT_WINDOW`.
6. The LLM receives JSON input and must return JSON translations.
7. Validated translations are inserted into the XHTML DOM.
8. Each translated chapter is checkpointed to disk.
9. The EPUB is repackaged with the required `mimetype` zip ordering.

The LLM is never asked to generate XHTML/XML.

## Data Layout

```text
data/
  output/
  jobs/
    <job_id>/
      source.epub
      state.json
      logs.txt
      work/
      translated/
      result.epub
  cache/
  logs/
```

## Development

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run locally:

```bash
python app.py
```

Run tests:

```bash
python -m unittest discover -s tests
```

## Notes and Limitations

- The EPUB preview uses EPUB.js from a CDN. For fully offline deployments, vendor the JS assets and update `app/preview.py`.
- The first version runs one background job at a time.
- EPUB pagination is reader-dependent, so sample preview selection uses chapter + character range instead of fixed page numbers.
- The app supports OpenAI-compatible APIs. Provider-specific APIs outside that interface are not implemented.

## Version

Current version: `0.1.0`

## License

Apache-2.0. See [LICENSE](LICENSE).
