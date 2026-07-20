# Segment 级翻译流水线开发 SPEC

## 1. 背景与目标

当前项目已经可以完成 EPUB 上传、章节预览、小范围翻译预览、批次翻译、LLM JSON 校验、批次缓存、章节检查点和 EPUB 重新打包。

但现在仍有一个核心风险：**某些段落在 LLM 返回格式错误、解析失败、批次局部失败时，可能没有得到目标语言译文，最终用户看到的是“这一段没翻译”，而系统却可能继续生成完成文件。**

本 SPEC 的目标是设计下一阶段翻译流水线，使翻译过程具备以下能力：

- 可检查：系统知道每一个源文本片段是否已经翻译成功。
- 可重试：失败片段可以被重新翻译，而不是整章重跑。
- 可恢复：服务中断或容器重启后，可以从已有状态继续。
- 不静默漏译：只要存在应翻译但未成功的片段，最终输出必须明确标记或阻止“成功完成”。
- 可扩展输入格式：未来 PDF、HTML、Markdown 等格式可以先转成统一文档模型，再输出 EPUB。
- 保持 EPUB 阅读体验：即使未来输入是 PDF，最终仍以 EPUB 为主要阅读输出格式。

## 2. 非目标

本 SPEC 不要求立刻实现 PDF 导入。

本 SPEC 不要求实现 LLM 并发调用。当前本地 LLM 服务只支持串行处理，所以并发不是主要优化方向。

本 SPEC 不要求复刻 PDF 的版式。PDF 输入的目标是生成可读 EPUB，而不是像素级还原原 PDF。

本 SPEC 不要求替换现有 EPUB XHTML 管线。EPUB 仍应尽量保留原书结构、章节、图片、脚注、表格和样式。

本 SPEC 不要求把所有内部参数暴露给用户。用户侧仍应保持简单，系统内部自动计算批次大小和重试策略。

## 3. 核心原则

### 3.1 源文本是唯一真相

最终输出必须从“源文本片段清单”生成，而不是从“已经拿到的译文列表”生成。

错误做法：

```python
for translation in translations:
    render(translation)
```

正确做法：

```python
for segment in source_segments:
    translation = translation_store.get(segment.id)
    render(segment, translation)
```

这样可以保证系统不会因为某个译文缺失，就把对应源段落从输出中跳过去。

### 3.2 LLM 只负责翻译文本，不负责决定结构

LLM 不应该决定哪些段落存在、表格怎么排列、章节如何组织、EPUB 如何写入。

LLM 的职责只应是：

- 接收一组带稳定 ID 的文本片段。
- 返回同一组 ID 的目标语言译文。

文档结构由程序维护。

### 3.3 每个片段必须有稳定 ID

每个可翻译单元都必须有稳定 ID。稳定 ID 应满足：

- 同一本书同一内容重复运行时尽量一致。
- 不依赖批次序号。
- 不依赖 LLM 返回顺序。
- 可以定位回源 EPUB 或未来 PDF 的来源位置。

EPUB 示例：

```text
epub:chapter=OEBPS/chapter01.xhtml:block=42
epub:chapter=OEBPS/chapter01.xhtml:table=3:row=2:cell=4
```

PDF 示例：

```text
pdf:page=12:block=8
pdf:page=12:table=1:row=3:cell=2
```

### 3.4 不能静默成功

如果一本书有 12000 个应翻译片段，最终只成功 11998 个，则任务不能被标记为完全成功。

允许的结果只有：

- `completed`：全部必译片段都有有效译文。
- `completed_with_warnings`：用户明确允许保留失败片段，且输出中有清晰标记。
- `failed`：失败数量超过策略允许范围，不能生成最终结果。
- `paused`：任务可恢复，但尚未完成。

### 3.5 优先优化串行 LLM

因为当前 LLM 后端串行，性能优化重点不是并发，而是：

- 让每次请求尽量接近上下文窗口的安全上限。
- 减少 JSON 格式错误。
- 失败后只重试失败片段。
- 利用缓存避免重复翻译。
- 持久化状态，避免服务重启后从头开始。

## 4. 总体架构

```text
输入文件
  |
  v
Importer
  |
  v
统一 Document Model
  |
  v
Segment Manifest
  |
  v
Planner / Batcher
  |
  v
Translator
  |
  v
Segment State Store
  |
  v
Coverage Gate
  |
  v
Renderer / EPUB Builder
```

### 4.1 模块职责

`Importer`：

- 读取输入文件。
- 抽取章节、段落、标题、脚注、表格、图片引用等结构。
- 生成统一文档模型。

`Segment Manifest`：

- 记录所有应翻译片段。
- 给每个片段分配稳定 ID。
- 记录片段类型、来源位置、字符数、是否必译。

`Planner / Batcher`：

- 根据 `LLM_CONTEXT_WINDOW`、prompt 预算、JSON 包装开销、目标语言膨胀系数，自动计算每批片段。
- 不再暴露手动批次大小给用户。

`Translator`：

- 调用 LLM。
- 校验 JSON。
- 做轻量修复。
- 校验返回 ID 覆盖。
- 写入片段级状态。

`Segment State Store`：

- 持久化每个片段的翻译状态。
- 支持失败重试、恢复、覆盖率统计。

`Coverage Gate`：

- 在生成最终 EPUB 前检查是否存在缺失译文。
- 阻止静默漏译。

`Renderer / EPUB Builder`：

- 从源结构和片段状态生成最终 EPUB。
- EPUB 输入继续保留原结构。
- PDF 输入未来生成新的 EPUB 结构。

## 5. 数据模型

### 5.1 Job 目录结构

建议每个任务目录如下：

```text
/data/jobs/<job_id>/
  input/
    original.epub
    original.pdf
  manifest.json
  segments.json
  batches.json
  translations.jsonl
  state.json
  logs/
    translator.log
  output/
    preview.epub
    bilingual.epub
    target.epub
```

其中：

- `manifest.json`：书籍级元数据和导入信息。
- `segments.json`：源片段清单。
- `batches.json`：批次规划结果。
- `translations.jsonl`：每条译文记录，一行一个片段，便于追加写入。
- `state.json`：任务汇总状态。

### 5.2 Segment 结构

```json
{
  "id": "epub:chapter=OEBPS/ch01.xhtml:block=42",
  "source_type": "epub",
  "document_id": "book-uuid-or-hash",
  "chapter_index": 1,
  "chapter_href": "OEBPS/ch01.xhtml",
  "page_index": null,
  "kind": "paragraph",
  "source_text": "Original text...",
  "source_hash": "sha256:...",
  "char_count": 128,
  "required": true,
  "status": "pending",
  "attempts": 0,
  "last_error": null
}
```

### 5.3 Segment 类型

第一阶段需要支持：

- `heading`
- `paragraph`
- `list_item`
- `blockquote`
- `figure_caption`
- `table_caption`
- `table_cell`
- `definition_term`
- `definition_description`
- `footnote`

未来 PDF 可增加：

- `pdf_text_block`
- `pdf_table_cell`
- `pdf_header`
- `pdf_footer`

### 5.4 Segment 状态

```text
pending
processing
completed
retryable_failed
permanent_failed
stale
skipped
```

含义：

- `pending`：尚未翻译。
- `processing`：当前批次处理中。
- `completed`：已有有效目标语言译文。
- `retryable_failed`：失败但仍可重试。
- `permanent_failed`：达到最大重试次数后仍失败。
- `stale`：源文本 hash 已变，旧译文不可直接使用。
- `skipped`：用户或策略明确跳过。

## 6. EPUB 导入策略

EPUB 仍是第一优先级。

导入器应：

- 解包 EPUB。
- 遍历 spine 中的 XHTML。
- 按章节建立结构。
- 提取可翻译节点。
- 保留图片、CSS、内部链接、脚注锚点。
- 对表格单元格单独建 segment。

### 6.1 段落类内容

段落、标题、列表项等可直接以块级元素为单位生成 segment。

### 6.2 表格内容

表格不应在原单元格中强行插入双语内容。

推荐策略：

- 原表格保持不变。
- 在原表格后生成一个目标语言表格。
- 目标语言表格保持相同行列结构。
- 单元格译文来自对应 `table_cell` segment。

这样既保留对照，又避免单元格内双语文本导致布局重叠。

### 6.3 脚注

脚注默认翻译。

脚注 segment 需要保留原始锚点关系，不能破坏正文跳转。

### 6.4 标题

标题默认翻译。

章节标题是否替换或双语展示由全局输出模式决定。

## 7. PDF 导入可行性设计

PDF 可以纳入这个流程，但应作为第二阶段或第三阶段功能。

推荐方向是使用 `run-llama/liteparse` 作为 PDF 解析候选。根据项目说明，LiteParse 是一个本地运行的 PDF 解析工具，支持输出 Markdown、JSON、Text，并提供文本块、空间位置信息和 OCR 相关能力。其 v2 版本为 Rust 重写，可通过 Cargo 安装，并提供多语言调用方式。

参考资料：

- [run-llama/liteparse GitHub](https://github.com/run-llama/liteparse)
- [LiteParse v2.0 Runs Everywhere](https://www.llamaindex.ai/blog/liteparse-v2-0-runs-everywhere)

### 7.1 PDF 初始范围

第一版 PDF 支持建议只做：

- 文本型 PDF。
- 简单阅读顺序。
- 普通段落。
- 简单标题推断。
- 简单表格尽力提取。

第一版不承诺：

- 扫描版 PDF 完整 OCR。
- 复杂多栏排版完美排序。
- 学术论文公式保持。
- 图文混排精确复刻。
- 页眉页脚自动完美识别。

### 7.2 PDF 到 EPUB 流程

```text
PDF
  |
  v
liteparse JSON / Markdown
  |
  v
PDF Importer
  |
  v
Document Model
  |
  v
Segment Manifest
  |
  v
Translation Pipeline
  |
  v
Generated EPUB
```

### 7.3 PDF Importer 职责

PDF Importer 应：

- 调用 liteparse CLI 或 Python 包。
- 获取页面、文本块、表格块、图片引用。
- 根据位置信息和 liteparse 输出推断阅读顺序。
- 过滤明显页眉页脚。
- 将页面文本转成章节或伪章节。
- 生成统一 Document Model。

### 7.4 PDF 输出 EPUB 策略

PDF 输入生成的 EPUB 应以“可读”为第一目标：

- 每若干页或每个推断章节生成一个 XHTML。
- 保留页码锚点，例如 `Page 12`。
- 图片可作为章节内图片保留。
- 表格尽量转成 HTML table。
- 无法可靠识别的复杂结构，可退化为普通段落。

## 8. 批次规划

用户只配置：

```text
LLM_CONTEXT_WINDOW
```

代码内部自动计算：

- prompt 固定开销。
- system message 开销。
- JSON schema 开销。
- 每个 segment 的 ID 和包装开销。
- 源语言字符到 token 的估算比例。
- 目标语言膨胀系数。
- 安全余量。

### 8.1 推荐计算方式

```text
usable_context = LLM_CONTEXT_WINDOW * 0.75
request_budget = usable_context * 0.55
response_budget = usable_context * 0.45
```

每批片段累加到接近 `request_budget` 即停止。

必须设置硬上限，避免超长段落拖垮单次请求：

```text
max_segment_chars = min(4000, request_budget_estimated_chars)
```

超长段落应进一步切分，但需要保留父 segment 关系。

### 8.2 为什么不暴露批次大小

手动批次大小对用户没有直观意义。

真正影响稳定性的是：

- 上下文窗口。
- prompt 长度。
- 源文本长度。
- 目标语言可能膨胀比例。
- JSON 返回稳定性。

因此批次应由代码自动规划。

## 9. LLM 请求与响应协议

### 9.1 请求格式

LLM 请求应尽量简单，减少 JSON 出错概率。

建议格式：

```json
{
  "target_language": "zh-CN",
  "items": [
    {
      "id": "epub:chapter=OEBPS/ch01.xhtml:block=42",
      "text": "Original text..."
    }
  ]
}
```

### 9.2 响应格式

```json
{
  "items": [
    {
      "id": "epub:chapter=OEBPS/ch01.xhtml:block=42",
      "translation": "译文..."
    }
  ]
}
```

### 9.3 校验规则

每次 LLM 返回后必须校验：

- JSON 可解析。
- 顶层存在 `items`。
- 每个 item 有 `id`。
- 每个 item 有 `translation`。
- 返回 ID 必须属于请求 ID。
- 必译 ID 不能缺失。
- `translation` 不能是空字符串。
- 不允许返回额外未知 ID。

### 9.4 轻量修复

可以继续保留轻量 JSON 修复，例如：

- 去除 Markdown code fence。
- 截取第一个完整 JSON 对象。
- 修复尾随逗号。
- 修复常见转义错误。

但修复失败后不能直接丢弃整批，应进入降级重试流程。

## 10. 重试策略

### 10.1 批次级重试

每批最多重试 3 次，内部常量即可，不暴露给用户。

触发条件：

- HTTP 请求失败。
- LLM 返回无法解析。
- JSON 校验失败。
- 返回 ID 缺失。
- 必译片段为空译文。

### 10.2 批次拆分重试

如果一个批次连续失败，应自动拆分：

```text
batch size N
  -> split into N/2 + N/2
  -> still failed
  -> split again
  -> eventually single segment retry
```

这样可以定位特定格式、特定文本导致的错误。

### 10.3 单片段重试

当降级到单 segment 后仍失败：

- 记录 `permanent_failed`。
- 记录错误原因。
- 不删除源文本。
- 不把任务标记为完全成功。

### 10.4 缺失 ID 补译

如果 LLM 返回了合法 JSON，但缺少部分 ID：

- 已返回且合格的译文立即保存。
- 缺失 ID 单独进入补译队列。
- 不应因为部分缺失而丢弃整个批次的成功结果。

### 10.5 格式触发错误的处理

某些文本格式可能诱发 LLM 输出坏 JSON，例如：

- 原文含大量引号。
- 原文含代码块。
- 原文含表格符号。
- 原文含 XML/HTML 片段。
- 原文含不完整括号或 JSON 类内容。

处理策略：

- 请求中对原文使用 JSON 原生字符串编码，不手写拼接 JSON。
- prompt 明确禁止复制外层 JSON 以外的解释文本。
- 必要时对问题 segment 使用更保守 prompt。
- 单 segment 仍失败时，记录失败，不静默跳过。

## 11. Coverage Gate

生成最终 EPUB 前必须运行 Coverage Gate。

检查内容：

- `segments.json` 中所有 `required=true` 的 segment。
- 每个 required segment 是否有 `completed` 译文。
- 源 hash 是否匹配。
- 是否存在 `retryable_failed` 或 `permanent_failed`。
- 是否存在空译文。

### 11.1 默认行为

默认不允许静默生成“看似完成”的 EPUB。

如果存在失败片段，应返回：

```text
任务未完全完成：共有 12000 个片段，11998 个成功，2 个失败。
```

并允许用户选择：

- 重试失败片段。
- 生成带警告 EPUB。
- 取消。

### 11.2 带警告 EPUB

如果用户选择生成带警告 EPUB：

- 失败片段保留原文。
- 在失败位置插入可见标记。
- 任务状态为 `completed_with_warnings`。
- 任务详情显示失败 segment ID 和原因。

示例标记：

```text
[Translation failed after 3 attempts]
```

中文界面显示：

```text
[翻译失败，已重试 3 次]
```

## 12. UI 需求

### 12.1 新建翻译任务

保留现有：

- 界面语言切换。
- EPUB 上传。
- EPUB 真实渲染预览。
- 按章节和字符数的小范围翻译预览。
- 全量翻译按钮。

新增或调整：

- 显示预估 segment 数量。
- 显示预估字符数。
- 小范围翻译预览也应走 segment pipeline。
- 预览结果使用同一个 EPUB 渲染窗口。

### 12.2 任务列表

任务列表应显示：

- 任务标题。
- 状态。
- 总 segment 数。
- 已完成 segment 数。
- 失败 segment 数。
- 最近错误。
- 创建时间。
- 更新时间。
- 删除按钮。

点击任务行后，下方显示任务详情。

### 12.3 任务详情

任务详情应显示：

- 章节进度。
- segment 进度。
- failed segment 列表。
- 最近错误日志。
- 输出文件。
- 重试失败片段按钮。
- 生成带警告 EPUB 按钮。

### 12.4 设置

设置页继续包含：

- 翻译默认设置。
- LLM 设置。
- Provider。
- Base URL。
- API Key。
- 模型刷新。
- 模型测试。
- 模型名称。
- `LLM_CONTEXT_WINDOW`。

不需要暴露：

- 每批最大重试次数。
- 批次大小。
- 每批最大字符数。

## 13. 输出模式

继续支持：

- `append_block`：原文段落后追加译文。
- `replace`：只保留目标语言。

表格特殊规则：

- `append_block`：原表格后追加目标语言表格。
- `replace`：替换表格单元格内容。

失败片段特殊规则：

- 默认不静默跳过。
- 用户允许后，失败片段保留原文并插入失败标记。

## 14. Docker 需求

### 14.1 当前阶段

当前阶段只实现 EPUB segment pipeline 时，不需要新增系统依赖。

### 14.2 未来 PDF 阶段

如果引入 LiteParse Rust CLI，Dockerfile 可采用多阶段构建。

示例：

```dockerfile
FROM rust:1-bookworm AS liteparse-builder
RUN cargo install liteparse

FROM python:3.12-slim
COPY --from=liteparse-builder /usr/local/cargo/bin/liteparse /usr/local/bin/liteparse
```

实际实现前必须确认：

- LiteParse 当前包名。
- CLI 二进制名称。
- CLI 参数格式。
- 是否需要 PDFium 或 OCR 额外依赖。
- 镜像体积影响。

### 14.3 可选构建参数

未来可以考虑：

```text
ENABLE_PDF_IMPORT=true
```

这样 EPUB-only 用户可以继续使用较小镜像。

## 15. 迁移计划

### 15.1 第一阶段：EPUB Segment Manifest

实现：

- 从 EPUB 抽取 segment。
- 写入 `segments.json`。
- 保留现有翻译输出行为。

验收：

- 上传 EPUB 后能看到 segment 统计。
- 同一本 EPUB 重复导入 segment ID 稳定。

### 15.2 第二阶段：Segment State Store

实现：

- 每个 segment 独立记录翻译状态。
- 翻译成功立即持久化。
- 服务重启后可恢复。

验收：

- 中断任务后重启，已完成 segment 不重译。

### 15.3 第三阶段：Coverage Gate

实现：

- 输出前检查漏译。
- 阻止静默完成。
- 允许用户生成带警告 EPUB。

验收：

- 人工制造缺失译文时，任务不能标为 `completed`。

### 15.4 第四阶段：失败拆分与补译

实现：

- 批次失败自动拆分。
- 缺失 ID 单独补译。
- 单 segment 最终失败后记录。

验收：

- 一个坏片段不会导致整章全部失败。
- 成功片段不会因为同批失败而被丢弃。

### 15.5 第五阶段：PDF Importer 原型

实现：

- Docker 中引入 liteparse。
- PDF 导入为 Document Model。
- 输出 EPUB。

验收：

- 简单文本型 PDF 可以转为 EPUB。
- EPUB 中可显示翻译结果。
- 复杂 PDF 失败时有明确提示。

## 16. 测试计划

### 16.1 单元测试

覆盖：

- segment ID 生成。
- EPUB XHTML segment 抽取。
- 表格 segment 抽取。
- 批次规划。
- LLM 返回校验。
- JSON 轻量修复。
- Coverage Gate。

### 16.2 集成测试

覆盖：

- 小 EPUB 完整翻译。
- 人工制造坏 JSON。
- 人工制造缺失 ID。
- 人工制造空译文。
- 中断后恢复。
- 表格双语输出。

### 16.3 回归测试

覆盖：

- 现有 EPUB 上传预览。
- 小范围翻译预览。
- 全量翻译。
- 任务列表。
- 删除任务。
- 设置保存。
- 中英文界面。

## 17. 风险与对策

### 17.1 状态文件变复杂

风险：

- 多个 JSON 文件可能出现不同步。

对策：

- 关键状态写入采用临时文件加原子 rename。
- `translations.jsonl` 只追加。
- 启动时可从 `segments.json` 和 `translations.jsonl` 重建状态。

### 17.2 Segment ID 不稳定

风险：

- EPUB 解析逻辑变动导致缓存失效。

对策：

- ID 中包含结构位置。
- 同时保存 `source_hash`。
- 缓存命中同时校验 ID 和 hash。

### 17.3 PDF 阅读顺序错误

风险：

- 多栏 PDF 或复杂排版顺序混乱。

对策：

- 第一版只承诺简单 PDF。
- 复杂 PDF 显示导入质量警告。
- 允许用户按页预览导入结果。

### 17.4 LLM 仍返回坏 JSON

风险：

- 特定文本始终诱发坏输出。

对策：

- 降级到单 segment。
- 使用更保守 prompt。
- 最终失败可见化，不静默丢弃。

## 18. 推荐开发顺序

建议不要先做 PDF。

推荐顺序：

1. EPUB segment manifest。
2. segment state persistence。
3. coverage gate。
4. failed / missing segment retry。
5. UI 增加 segment 进度和失败重试。
6. renderer 从 segment store 取译文。
7. LiteParse PDF importer 原型。

原因：

- 当前最痛的问题是 EPUB 漏译和失败恢复。
- PDF 只有在 segment pipeline 稳定后才容易接入。
- 统一 document model 是 PDF 支持的前提。

## 19. 验收标准

本 SPEC 第一阶段完成后，至少应满足：

- 任意 EPUB 翻译任务都有完整 segment 清单。
- 任意失败片段都能在任务详情中看到。
- LLM 返回坏 JSON 时不会静默漏译。
- 批次部分成功时，成功译文被保存，失败片段进入补译队列。
- 全量输出前必须通过 coverage gate。
- 失败 EPUB 只能以 `completed_with_warnings` 方式生成，且失败位置可见。
- 重启服务后可以继续未完成任务。

## 20. 总结

这个方案的核心不是“把书切块后调用 LLM”，而是建立一套以 source segment 为真相的翻译流水线。

它会让系统从：

```text
章节 -> 批次 -> 译文 -> EPUB
```

升级为：

```text
文档模型 -> 源片段清单 -> 片段状态 -> 覆盖率校验 -> EPUB
```

这样 EPUB 可以先受益于更可靠的重试与恢复机制。未来 PDF、HTML、Markdown 等格式也可以通过 Importer 接入同一套翻译和输出流程，最终统一生成适合阅读的 EPUB。
