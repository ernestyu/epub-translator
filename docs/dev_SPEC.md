# EPUB 双语翻译 Web 服务开发 SPEC

## 1. 项目目标

本项目要实现一个本地部署的 EPUB 双语翻译 Web 服务。

系统应支持上传 EPUB 文件，使用 OpenAI-compatible LLM API 翻译书中内容，并生成新的双语 EPUB 文件。系统必须支持长时间任务、断线恢复、章节级 checkpoint、失败批次重试、历史任务查看和结果下载。

本项目不要求 LLM 返回 XHTML/XML。LLM 只负责将纯文本翻译成目标语言。EPUB 的 XHTML 结构由程序解析、修改和重新打包。这样可以避免 LLM 破坏 XML 结构导致整本书翻译失败。

最终用户体验应接近 PDFMathTranslate 这类工具：本地 Docker 部署，浏览器上传文件，等待后台翻译，完成后下载结果。即使浏览器断开、电脑休眠、页面刷新，也可以重新打开 WebUI 查看任务状态和下载结果。

## 2. 核心设计原则

### 2.1 不让 LLM 生成 XHTML/XML

禁止把完整 XHTML 片段交给 LLM 后要求它返回 XHTML。原因是模型可能输出多余标签、遗漏标签、破坏属性、增加 Markdown 代码块，导致 XML 校验失败。

正确做法是：

1. 程序解析 EPUB 中的 XHTML。
2. 程序提取可翻译的文本块，例如 `p`、`li`、`blockquote`、`h1` 到 `h6`。
3. LLM 只接收文本数组。
4. LLM 只返回 JSON 数组。
5. 程序校验 JSON 数组长度和内容。
6. 程序把译文插入到原文段落之后。
7. 程序重新保存 XHTML，再打包成 EPUB。

### 2.2 章节级 checkpoint

每个 EPUB spine item，也就是每个正文 XHTML 文件，应作为一个独立章节任务。

每章翻译完成后，立刻写入：

```text
/data/jobs/<job_id>/translated/<chapter_id>.xhtml
```

并更新：

```text
/data/jobs/<job_id>/state.json
```

如果任务中断，下次继续时应跳过已经完成的章节，只处理未完成或失败的章节。

### 2.3 批次级重试

每章内部应把文本块分成多个 batch。每个 batch 包含若干文本块，例如 5 到 20 个，或者按 token 数限制分组。

每个 batch 失败时，最多重试 3 次。失败原因包括：

1. LLM API 连接失败。
2. LLM API 返回非 200。
3. LLM 返回无法解析的 JSON。
4. JSON 数组长度与输入数组长度不一致。
5. 返回内容为空。
6. 返回了明显不是译文的内容，例如代码块、解释性文字、XML 包裹等。

### 2.4 可恢复任务

任务状态必须持久化到磁盘。不能只存在内存中。

浏览器断开后，后台任务仍应继续运行。任务完成后，结果 EPUB 应保存在 `/data/output`，并可在 WebUI 的“已完成文件”或“任务列表”页面中下载。

### 2.5 原子写入

所有关键文件写入都必须使用临时文件再 rename 的方式，避免写入一半导致 state 或 XHTML 损坏。

例如：

```text
state.json.tmp -> state.json
chapter_003.xhtml.tmp -> chapter_003.xhtml
result.epub.tmp -> result.epub
```

## 3. 功能需求

### 3.1 WebUI 功能

WebUI 应包含以下页面或 Tab。

#### 3.1.1 新建翻译任务

用户可以上传 EPUB，并设置：

1. 语言，要有两栏，一个是“源语言”，另一个是“目标语言”。两个都需要是一个固定列表，例如 `Simplified Chinese`、`English`、`German`。列表支持主要几种语言，比如中文，日语，英语，法语，德语，西班牙语等
2. 输出模式：

   * `append_block`：原文段落后追加译文段落。
   * `replace`：只保留译文。
3. 批次大小：

   * 默认 8 个文本块一批。
4. 每批最大字符数：

   * 默认 6000 字符。
5. 每批最大重试次数：

   * 默认 3。
6. 章节失败策略：

   * `stop_on_failed_chapter`：默认，失败则停止任务。
   * `keep_original_on_failed_chapter`：失败章节保留原文，继续打包。
7. 自定义翻译提示词，可选。
8. 是否翻译标题：

   * 默认开启。
9. 是否翻译脚注：

   * 默认开启。
10. 是否翻译目录：

* 默认可以先关闭，后续版本支持。

用户点击“开始翻译”后，系统创建 job，并立即返回 job id 和任务页面链接。

#### 3.1.2 任务列表

显示所有任务，按更新时间倒序排列。

每个任务显示：

1. job id。
2. 原文件名。
3. 目标语言。
4. 状态：

   * `queued`
   * `running`
   * `paused`
   * `finished`
   * `failed`
   * `cancelled`
5. 当前进度：

   * 已完成章节数 / 总章节数。
   * 已完成文本块数 / 总文本块数，如果可计算。
6. 创建时间。
7. 更新时间。
8. 输出文件下载按钮，如果已完成。
9. 操作按钮：

   * 查看详情。
   * 继续任务。
   * 只重跑失败章节。
   * 取消任务。
   * 删除任务。

#### 3.1.3 任务详情

显示某个 job 的详细状态。

内容包括：

1. 原 EPUB 文件名。
2. 输出 EPUB 文件路径。
3. 目标语言。
4. 模型名称。
5. LLM API 地址，隐藏 API key。
6. 任务状态。
7. 当前进度。
8. 章节列表。

章节列表应显示：

1. 章节 index。
2. 章节 href。
3. 章节标题，如果能读到。
4. 状态：

   * `pending`
   * `running`
   * `done`
   * `failed`
   * `skipped`
5. 文本块数量。
6. 已完成 batch 数。
7. 失败次数。
8. 最近错误。
9. 单章重跑按钮。

#### 3.1.4 已完成文件

显示 `/data/output` 下的 EPUB 文件，按修改时间倒序。

支持：

1. 刷新列表。
2. 选择文件。
3. 下载文件。
4. 删除文件。

### 3.2 后台任务功能

#### 3.2.1 创建任务

上传 EPUB 后，系统应：

1. 生成 `job_id`。
2. 创建目录：

```text
/data/jobs/<job_id>/
```

3. 保存原 EPUB：

```text
/data/jobs/<job_id>/source.epub
```

4. 解包 EPUB 到：

```text
/data/jobs/<job_id>/work/
```

5. 解析 EPUB 的 OPF 文件。
6. 读取 spine 顺序。
7. 建立章节列表。
8. 写入初始 state：

```text
/data/jobs/<job_id>/state.json
```

#### 3.2.2 执行任务

任务执行器应按 spine 顺序处理章节。

对每个章节：

1. 如果章节状态为 `done`，跳过。
2. 如果对应 translated XHTML 文件已存在，并且 state 中标记为 `done`，跳过。
3. 如果章节状态为 `failed`，只有在继续任务或重跑失败章节时才处理。
4. 解析章节 XHTML。
5. 提取可翻译文本块。
6. 分 batch 翻译。
7. 每个 batch 成功后，将译文写入内存中的 XHTML DOM。
8. 整章成功后写入 translated XHTML。
9. 更新 state。

#### 3.2.3 完成任务

所有章节完成后，系统应：

1. 将 translated 目录中的 XHTML 覆盖回 work 目录对应位置。
2. 保留未翻译的资源文件，例如图片、CSS、字体、音频。
3. 更新必要的 CSS。
4. 重新打包 EPUB 到临时文件：

```text
/data/jobs/<job_id>/result.epub.tmp
```

5. 打包成功后 rename 为：

```text
/data/jobs/<job_id>/result.epub
```

6. 再复制或硬链接到：

```text
/data/output/<safe_original_name>.<target_language>.<timestamp>.bilingual.epub
```

7. 更新 state 为 `finished`。

## 4. 目录结构

项目代码结构建议如下：

```text
epub-translator-web/
  Dockerfile
  docker-compose.yml
  requirements.txt
  .env.example
  .gitignore

  app/
    __init__.py
    web.py
    config.py
    models.py
    job_store.py
    epub_io.py
    extractor.py
    batcher.py
    llm_client.py
    translator.py
    worker.py
    packager.py
    utils.py
```

运行数据结构：

```text
data/
  input/
  output/
  jobs/
    <job_id>/
      source.epub
      state.json
      logs.txt
      work/
      translated/
        chapter_000.xhtml
        chapter_001.xhtml
      result.epub
  cache/
  logs/
```

## 5. 环境变量

`.env` 示例：

```env
LLM_API_KEY=ollama
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=qwen3:32b
TARGET_LANGUAGE=Simplified Chinese

APP_HOST=0.0.0.0
APP_PORT=7860

DATA_DIR=/data
LOG_LEVEL=INFO

DEFAULT_BATCH_SIZE=8
DEFAULT_MAX_BATCH_CHARS=6000
DEFAULT_BATCH_RETRIES=3
DEFAULT_CHAPTER_FAILURE_POLICY=stop_on_failed_chapter

LLM_TIMEOUT_SECONDS=300
LLM_TEMPERATURE=0.1
LLM_TOP_P=0.8
```

## 6. Docker 部署要求

### 6.1 Dockerfile

应使用 Python 3.12 slim。

需要安装：

```text
gradio
openai
beautifulsoup4
lxml
ebooklib
python-dotenv
pydantic
tenacity
```

如果 EPUB 打包使用标准 zip，也可以不依赖 ebooklib，但 ebooklib 对 OPF 读取会方便一些。

### 6.2 docker-compose.yml

示例：

```yaml
services:
  epub-translator:
    build: .
    container_name: epub-translator
    restart: unless-stopped
    ports:
      - "7860:7860"
    env_file:
      - .env
    volumes:
      - ./data:/data
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

## 7. 数据模型

### 7.1 JobState

`state.json` 应包含：

```json
{
  "job_id": "20260706-101201-a1b2c3d4",
  "status": "running",
  "source_filename": "book.epub",
  "source_path": "/data/jobs/20260706-101201-a1b2c3d4/source.epub",
  "work_dir": "/data/jobs/20260706-101201-a1b2c3d4/work",
  "translated_dir": "/data/jobs/20260706-101201-a1b2c3d4/translated",
  "result_path": "/data/jobs/20260706-101201-a1b2c3d4/result.epub",
  "output_path": "/data/output/book.Simplified Chinese.20260706-101201.bilingual.epub",
  "target_language": "Simplified Chinese",
  "model": "qwen3:32b",
  "base_url": "http://host.docker.internal:11434/v1",
  "mode": "append_block",
  "chapter_failure_policy": "stop_on_failed_chapter",
  "created_at": "2026-07-06 10:12:01",
  "updated_at": "2026-07-06 10:20:33",
  "started_at": "2026-07-06 10:12:02",
  "finished_at": null,
  "total_chapters": 20,
  "done_chapters": 8,
  "failed_chapters": 0,
  "total_text_blocks": 1500,
  "done_text_blocks": 612,
  "last_error": null,
  "chapters": []
}
```

### 7.2 ChapterState

`chapters` 数组中的每个元素：

```json
{
  "index": 0,
  "id": "chapter_000",
  "href": "Text/chapter1.xhtml",
  "abs_path": "/data/jobs/<job_id>/work/OEBPS/Text/chapter1.xhtml",
  "translated_path": "/data/jobs/<job_id>/translated/chapter_000.xhtml",
  "title": "Chapter 1",
  "status": "done",
  "text_blocks": 80,
  "done_text_blocks": 80,
  "batches": 10,
  "done_batches": 10,
  "failed_batches": 0,
  "attempts": 1,
  "last_error": null,
  "started_at": "2026-07-06 10:12:02",
  "finished_at": "2026-07-06 10:15:11"
}
```

### 7.3 BatchState

可以不单独写文件，但如果要更强恢复能力，建议每章有一个 batch state 文件：

```text
/data/jobs/<job_id>/translated/chapter_000.state.json
```

格式：

```json
{
  "chapter_id": "chapter_000",
  "status": "running",
  "batches": [
    {
      "batch_index": 0,
      "status": "done",
      "input_hash": "sha256...",
      "attempts": 1,
      "error": null
    }
  ]
}
```

## 8. EPUB 处理

### 8.1 解包 EPUB

EPUB 本质是 zip 文件，但必须注意 `mimetype` 文件打包顺序。

解包时：

1. 检查 zip 中是否有 `mimetype`。
2. 检查 `META-INF/container.xml`。
3. 从 `container.xml` 读取 rootfile，例如：

```xml
<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
```

4. 解析 OPF。
5. 读取 manifest 和 spine。
6. 根据 spine 顺序找到正文 XHTML 文件。

### 8.2 章节识别

只处理 media-type 为以下类型的 spine item：

```text
application/xhtml+xml
text/html
```

不翻译：

```text
image/*
text/css
application/font-woff
application/vnd.ms-opentype
application/x-font-ttf
```

### 8.3 重新打包 EPUB

打包时必须满足 EPUB zip 规范：

1. `mimetype` 必须是 zip 第一个文件。
2. `mimetype` 必须不压缩，即 `ZIP_STORED`。
3. 其他文件使用 `ZIP_DEFLATED`。
4. 保留原始目录结构。

伪代码：

```python
with ZipFile(output, "w") as zf:
    zf.write(work_dir / "mimetype", "mimetype", compress_type=ZIP_STORED)
    for file in all_files_except_mimetype:
        zf.write(file, relative_path, compress_type=ZIP_DEFLATED)
```

## 9. XHTML 文本提取规则

### 9.1 可翻译元素

默认翻译以下标签：

```text
p
li
blockquote
h1
h2
h3
h4
h5
h6
figcaption
td
th
dt
dd
```

### 9.2 跳过元素

跳过以下元素及其子元素：

```text
script
style
code
pre
samp
kbd
math
svg
audio
video
img
```

跳过空文本。

跳过只有数字、标点、页码的文本。

跳过长度低于某个阈值的内容可以做成配置，但默认不要跳过短标题。

### 9.3 文本块定义

每个可翻译元素是一个 TextBlock。

TextBlock 字段：

```json
{
  "block_id": "chapter_000_block_0012",
  "tag": "p",
  "text": "Original paragraph text.",
  "element_path": "internal reference only"
}
```

实现上不能把 BeautifulSoup element 直接序列化到 state。运行时可以用内存中的 element 引用，state 里只保存计数和 batch 状态。

### 9.4 内联标签处理

为了简单可靠，第一版可以对整个元素的 `get_text(" ", strip=True)` 进行翻译，然后把译文作为新的段落插入原段落后。这样会丢失译文里的斜体、链接等内联格式，但原文仍完整保留。

对于 `append_block` 模式，这是可以接受的，因为原文保留了完整格式。

后续可扩展到保留内联标签。

## 10. 输出模式

### 10.1 append_block

原文保留，在原元素后插入一个译文元素。

输入：

```html
<p class="para">This is a test.</p>
```

输出：

```html
<p class="para">This is a test.</p>
<p class="bilingual-translation" data-source-block-id="chapter_000_block_0001">这是一个测试。</p>
```

对于标题：

```html
<h2>Chapter One</h2>
<h2 class="bilingual-translation" data-source-block-id="chapter_000_block_0002">第一章</h2>
```

### 10.2 replace

用译文替换原元素文本。

输入：

```html
<p class="para">This is a test.</p>
```

输出：

```html
<p class="para">这是一个测试。</p>
```

第一版建议重点支持 `append_block`。`replace` 可以做，但要小心内联格式丢失。

### 10.3 CSS

生成 EPUB 时应注入一个 CSS 文件，或在现有 CSS 中追加：

```css
.bilingual-translation {
  margin-top: 0.2em;
  margin-bottom: 0.8em;
  opacity: 0.85;
}
```

不要指定太复杂的字体和颜色，避免不同阅读器兼容问题。

可以在 OPF manifest 中加入：

```xml
<item id="bilingual-style" href="Styles/bilingual.css" media-type="text/css"/>
```

并在每个 XHTML 的 head 中加入：

```html
<link rel="stylesheet" type="text/css" href="../Styles/bilingual.css"/>
```

路径要根据 XHTML 所在目录计算相对路径。

如果第一版不想改 OPF，也可以直接在每个 XHTML head 中插入 `<style>`，但不如 CSS 文件干净。

## 11. Batch 分组规则

### 11.1 输入

一章中的 TextBlock 列表。

### 11.2 分组约束

默认：

```text
DEFAULT_BATCH_SIZE=8
DEFAULT_MAX_BATCH_CHARS=6000
```

创建 batch 时同时满足：

1. 每批最多 8 个文本块。
2. 每批总字符数最多 6000。
3. 如果单个文本块超过 6000 字符，它单独成为一批。

### 11.3 Batch 输入格式

传给 LLM 的用户消息应包含 JSON：

```json
{
  "target_language": "Simplified Chinese",
  "items": [
    {
      "id": "chapter_000_block_0001",
      "text": "This is a test."
    },
    {
      "id": "chapter_000_block_0002",
      "text": "Another paragraph."
    }
  ]
}
```

## 12. LLM 调用

### 12.1 API

使用 OpenAI-compatible Chat Completions API：

```text
POST /v1/chat/completions
```

环境变量：

```text
LLM_BASE_URL
LLM_API_KEY
LLM_MODEL
```

### 12.2 System prompt

系统提示词：

```text
You are a professional book translator.

Translate each input item into the target language.

Rules:
1. Return only valid JSON.
2. Do not use Markdown.
3. Do not wrap the answer in code fences.
4. Do not add explanations.
5. Preserve item ids exactly.
6. Return the same number of items as the input.
7. Do not omit any item.
8. Do not merge items.
9. Do not split items.
10. Translate only the text value.
11. Keep names, URLs, code identifiers, file paths, and commands unchanged unless they clearly need translation.
```

### 12.3 User prompt

用户消息：

```text
Target language: Simplified Chinese

Return JSON in this exact format:
{
  "items": [
    {"id": "same id as input", "translation": "translated text"}
  ]
}

Input:
<json here>
```

### 12.4 期望返回格式

```json
{
  "items": [
    {
      "id": "chapter_000_block_0001",
      "translation": "这是一个测试。"
    }
  ]
}
```

### 12.5 LLM配置

应该有一个配置页面，允许自己配置LLM。支持常见的一些LLM provider 格式。以及openai 兼容的API

## 13. LLM 返回校验

每次 LLM 返回后必须校验。

### 13.1 JSON 修复

先尝试直接 `json.loads()`。

如果失败，尝试轻量修复：

1. 去掉 Markdown code fence：

````text
```json
...
````

````

2. 从返回中提取第一个 `{` 到最后一个 `}`。
3. 去掉前后解释性文字。
4. 再次 `json.loads()`。

禁止做过于激进的修复，避免错误数据悄悄通过。

### 13.2 格式校验

校验规则：

1. 顶层必须是 object。
2. 必须有 `items`。
3. `items` 必须是 list。
4. 长度必须等于输入 items 长度。
5. 每个 item 必须有 `id` 和 `translation`。
6. 返回 id 集合必须等于输入 id 集合。
7. `translation` 必须是字符串。
8. `translation` 不能为空，除非原文为空。
9. 不允许返回明显的 XML 文档。
10. 不允许返回整段解释。

### 13.3 自动重排

如果返回 items 顺序不同，但 id 完整，可以按输入顺序重排。

### 13.4 失败重试

如果校验失败，该 batch 重试，最多 3 次。

每次重试时应增强提示，例如：

```text
Your previous response was invalid because: <reason>.
Return only valid JSON with exactly the same ids.
````

## 14. 缓存设计

### 14.1 Batch 缓存

为了避免失败后重复消耗，应缓存每个 batch 的成功翻译。

缓存路径：

```text
/data/cache/batches/<sha256>.json
```

hash 输入应包括：

1. 模型名。
2. 目标语言。
3. mode。
4. user_prompt。
5. items 的 id 和 text。

示例 hash 输入：

```json
{
  "model": "qwen3:32b",
  "target_language": "Simplified Chinese",
  "user_prompt": "...",
  "items": [
    {"id": "...", "text": "..."}
  ]
}
```

如果缓存存在且校验通过，直接使用缓存，不请求 LLM。

### 14.2 缓存内容

```json
{
  "created_at": "2026-07-06 10:12:03",
  "model": "qwen3:32b",
  "target_language": "Simplified Chinese",
  "items": [
    {
      "id": "chapter_000_block_0001",
      "translation": "这是一个测试。"
    }
  ]
}
```

## 15. 任务恢复逻辑

### 15.1 启动时恢复

服务启动时扫描：

```text
/data/jobs/*
```

读取每个 `state.json`。

如果发现状态为：

```text
running
queued
retrying
```

但进程已不存在，则标记为：

```text
paused
```

用户可在 WebUI 点击“继续任务”。

不要自动继续所有旧任务，避免服务启动后立即占满 LLM。

### 15.2 继续任务

继续任务时：

1. 读取 state。
2. 确认 source.epub、work 目录存在。
3. 对每个章节：

   * `done` 且 translated 文件存在：跳过。
   * `done` 但 translated 文件不存在：改为 `pending`。
   * `failed`：根据用户操作决定是否重跑。
   * `pending`：处理。
4. 全部 done 后重新打包。

### 15.3 只重跑失败章节

用户点击“只重跑失败章节”时：

1. 将 failed 章节改为 pending。
2. 保留 done 章节不动。
3. 开始 worker。

## 16. Worker 设计

### 16.1 单进程后台线程

第一版可以使用 Python `threading.Thread` 做后台任务。

限制：

1. 同一时间默认只跑一个 job。
2. 可以设置队列，但第一版不需要复杂队列。
3. WebUI 不应阻塞等待整本书翻译完成。

### 16.2 Job Lock

每个 job 应有 lock 文件：

```text
/data/jobs/<job_id>/job.lock
```

运行 job 前创建 lock。完成、失败、取消后删除 lock。

如果服务重启后发现 lock 存在但没有对应进程，允许标记为 paused 并删除旧 lock。

### 16.3 取消任务

`state.json` 中加入：

```json
"cancel_requested": false
```

用户点击取消时改为 true。

worker 每完成一个 batch 后检查该字段。如果为 true，则停止任务并设置状态为 `cancelled`。

## 17. 日志要求

### 17.1 日志不要输出正文

日志禁止输出整段原文或译文，避免日志爆炸，也避免隐私泄露。

### 17.2 应输出的信息

启动时：

```text
LLM_BASE_URL
LLM_MODEL
DATA_DIR
```

任务开始：

```text
job_id
source filename
target language
mode
chapter count
```

每章开始：

```text
job_id chapter index href block count batch count
```

每个 batch：

```text
job_id chapter index batch index attempt status
```

LLM 请求：

```text
HTTP 200 OK
HTTP 429
HTTP 500
timeout
```

任务结束：

```text
job_id status elapsed output_path
```

### 17.3 日志文件

全局日志：

```text
/data/logs/app.log
```

每个 job 日志：

```text
/data/jobs/<job_id>/logs.txt
```

## 18. 错误处理

### 18.1 API 错误

如果 LLM API 返回：

```text
429
500
502
503
504
timeout
connection error
```

应按指数退避重试。

默认：

```text
第一次等待 3 秒
第二次等待 8 秒
第三次等待 20 秒
```

超过 batch 最大重试次数后，该 batch 失败。

### 18.2 JSON 错误

如果 JSON 校验失败，记录错误原因，重试 batch。

### 18.3 章节失败

如果某 batch 连续失败 3 次，该章节失败。

根据策略：

1. `stop_on_failed_chapter`：任务状态改为 failed，停止。
2. `keep_original_on_failed_chapter`：该章节状态改为 skipped，保留原 XHTML，继续后续章节。

### 18.4 打包失败

打包失败时，任务状态改为 failed，但保留所有 translated 章节。用户修复后可以重新打包。

## 19. 安全和文件名处理

### 19.1 文件名安全

上传文件名必须 sanitize。

只允许：

```text
字母
数字
空格
点
横线
下划线
中文字符
```

其他字符替换为 `_`。

### 19.2 路径安全

所有路径必须限制在 `/data/jobs/<job_id>` 或 `/data/output` 下。

禁止使用用户输入拼接任意路径。

### 19.3 API key

WebUI 和日志中不得显示完整 API key。

显示时只显示：

```text
oll***key
```

或者直接显示 `***`。

## 20. WebUI 与后台解耦

翻译任务不应由 Gradio 的 button callback 长时间阻塞执行。

正确流程：

1. 用户点击开始。
2. callback 创建 job。
3. callback 启动后台线程。
4. callback 立即返回 job id。
5. 用户进入任务详情页面查看进度。
6. 页面可以手动刷新状态。

第一版不需要 WebSocket 实时刷新。手动刷新即可。

## 21. 具体模块职责

### 21.1 config.py

负责读取环境变量。

输出一个 Config 对象：

```python
class Config:
    data_dir: Path
    jobs_dir: Path
    output_dir: Path
    cache_dir: Path
    logs_dir: Path

    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: int
    llm_temperature: float
    llm_top_p: float

    default_batch_size: int
    default_max_batch_chars: int
    default_batch_retries: int
```

### 21.2 models.py

定义数据结构：

```python
JobStatus = Literal["queued", "running", "paused", "finished", "failed", "cancelled"]
ChapterStatus = Literal["pending", "running", "done", "failed", "skipped"]

@dataclass
class ChapterState:
    ...

@dataclass
class JobState:
    ...
```

也可以使用 Pydantic。

### 21.3 job_store.py

负责：

1. 创建 job。
2. 读取 job state。
3. 写入 job state。
4. 列出 jobs。
5. 更新 job 状态。
6. 原子写入 JSON。

### 21.4 epub_io.py

负责：

1. 解包 EPUB。
2. 读取 container.xml。
3. 读取 OPF。
4. 找到 spine XHTML。
5. 返回章节列表。
6. 查找 OPF 所在目录。

### 21.5 extractor.py

负责：

1. 解析 XHTML。
2. 提取 TextBlock。
3. 跳过不可翻译元素。
4. 插入译文元素。
5. 保存 XHTML。

### 21.6 batcher.py

负责把 TextBlock 分 batch。

### 21.7 llm_client.py

负责：

1. 调用 OpenAI-compatible API。
2. 做 HTTP 重试。
3. 返回原始字符串。
4. 记录 HTTP 状态。

### 21.8 translator.py

负责：

1. 构建 prompt。
2. 调用 llm_client。
3. 解析 JSON。
4. 修复 JSON。
5. 校验结果。
6. batch 级重试。
7. batch 缓存。

### 21.9 worker.py

负责：

1. 执行 job。
2. 逐章处理。
3. 更新 state。
4. 处理取消。
5. 处理失败策略。
6. 调用 packager。

### 21.10 packager.py

负责：

1. 将 translated XHTML 覆盖回 work 目录。
2. 注入 CSS。
3. 打包 EPUB。
4. 输出到 `/data/output`。

### 21.11 web.py

负责 Gradio UI。

不要在 web.py 中写翻译核心逻辑。WebUI 只负责调用 job_store 和 worker。

## 22. 测试要求

### 22.1 单元测试

至少测试：

1. EPUB 解包和 OPF 解析。
2. spine 章节识别。
3. XHTML 文本提取。
4. batch 分组。
5. JSON 返回解析。
6. JSON code fence 修复。
7. id 顺序重排。
8. JSON 长度不一致时报错。
9. state 原子写入。
10. EPUB 重新打包后 zip 结构正确。

### 22.2 集成测试

准备一个小 EPUB，包含：

1. 两章 XHTML。
2. 段落。
3. 标题。
4. 列表。
5. 图片。
6. CSS。

用 mock LLM 返回固定译文。

测试：

1. 完整翻译成功。
2. 第一个 batch 返回坏 JSON，第二次重试成功。
3. 某章节失败后任务 failed。
4. `keep_original_on_failed_chapter` 策略下，失败章节保留原文并继续打包。
5. 中断后继续任务跳过已完成章节。

## 23. 第一版实现范围

第一版必须实现：

1. Docker 部署。
2. Gradio WebUI。
3. 上传 EPUB。
4. 创建 job。
5. 解包 EPUB。
6. 解析 spine。
7. 按章节 checkpoint。
8. 提取 `p`、`li`、`blockquote`、`h1-h6`。
9. LLM JSON 翻译。
10. batch 级缓存。
11. batch 级重试 3 次。
12. 章节级失败处理。
13. 重新打包 EPUB。
14. 任务列表。
15. 任务详情。
16. 已完成文件下载。
17. 继续任务。
18. 只重跑失败章节。

第一版可以暂时不实现：

1. 实时进度条。
2. 多任务并发。
3. 用户登录。
4. 保留译文内联格式。
5. 翻译 EPUB 目录 nav。
6. 翻译 metadata。
7. 删除缓存。
8. 复杂 CSS 配置。

## 24. 推荐开发顺序

### 阶段 1：基础 EPUB 处理

实现：

1. 解包 EPUB。
2. 读取 container.xml。
3. 读取 OPF。
4. 读取 spine。
5. 重新打包 EPUB。

验收标准：上传一个 EPUB，解包后原样打包，阅读器能打开。

### 阶段 2：XHTML 提取和插入

实现：

1. 提取文本块。
2. mock 翻译。
3. 插入译文段落。
4. 打包 EPUB。

验收标准：生成的 EPUB 中每段后面出现 mock 译文。

### 阶段 3：LLM 翻译

实现：

1. OpenAI-compatible API 调用。
2. JSON prompt。
3. JSON 校验。
4. batch 重试。

验收标准：小书能真实翻译成功。

### 阶段 4：checkpoint

实现：

1. state.json。
2. 每章 translated XHTML。
3. 跳过 done 章节。
4. 继续任务。

验收标准：手动中断后继续，只翻译剩余章节。

### 阶段 5：WebUI

实现：

1. 新建任务。
2. 任务列表。
3. 任务详情。
4. 下载结果。
5. 继续任务。
6. 重跑失败章节。

## 25. 验收标准

项目完成后，应满足：

1. 运行：

```bash
docker compose up -d --build
```

2. 浏览器打开：

```text
http://<server-ip>:7860
```

3. 上传 EPUB 后能创建任务。
4. 浏览器关闭后，后台任务继续运行。
5. 重新打开页面后，可以看到任务状态。
6. 翻译过程中某个 batch 返回坏 JSON，系统会自动重试。
7. 某个章节翻译完成后，生成章节 checkpoint。
8. 服务重启后，可以继续未完成任务。
9. 最终生成双语 EPUB。
10. 生成的 EPUB 能被常见阅读器打开。
11. `/data/output` 中能找到结果文件。
12. WebUI 能下载历史结果文件。
13. Docker logs 不输出整本书正文。
14. 任务失败时能看到失败章节和错误原因。

## 26. 关键实现建议

### 26.1 不要复用整本 translate API

不要继续使用：

```python
epub_translator.translate(source_path=..., target_path=...)
```

作为核心流程。它适合快速翻译整本书，但不适合做章节级 checkpoint。

### 26.2 可以参考 epub-translator，但不要绑定其内部 API

可以参考其 EPUB 翻译思路，但不要强依赖内部类，因为内部 API 可能变化。

### 26.3 第一版宁可简单，也要稳定

第一版允许译文不保留原文内联格式，因为原文仍然保留。最重要的是：

1. 不崩。
2. 可恢复。
3. 可继续。
4. 可下载。
5. EPUB 可打开。

### 26.4 先只支持 append_block

`append_block` 最安全。`replace` 可以后续增强。

### 26.5 界面支持国际化，可以在英语和中文之间切换。

## 27. 未来增强

后续可以增加：

1. 多任务队列。
2. 每任务并发章节翻译。
3. 每章节并发 batch 翻译。
4. 翻译目录 nav.xhtml。
5. 翻译 metadata。
6. 保留译文内联格式。
7. 术语表。
8. 人名表。
9. 双语样式设置。
10. 导出 HTML。
11. 导出 TXT。
12. 自动检测源语言。
13. 支持 AZW3/MOBI 转 EPUB 后翻译。
14. 支持 DeepL、Google Translate、Gemini、OpenAI、Ollama 多后端。

## 28. 结论

本项目应从“调用整本书翻译函数”改为“自主管理 EPUB 结构和翻译任务”。

核心策略是：

```text
EPUB 结构由程序控制
LLM 只返回纯译文 JSON
章节级 checkpoint
batch 级重试
任务状态持久化
最终重新打包 EPUB
```

这样才能解决长书翻译时最常见的问题：浏览器断开、模型输出格式错误、单批次失败导致整本书失败、无法从上次进度继续、翻译完成后找不到下载入口。
