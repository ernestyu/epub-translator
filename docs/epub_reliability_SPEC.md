# EPUB 翻译可靠性与一致性增强 SPEC

## 1. 背景

本项目继续专注 EPUB 翻译，不引入多格式统一中间模型，也不改变现有 EPUB XHTML 处理主线。

此前讨论过将 EPUB、PDF、DOCX 等输入统一转成 Canonical Book Model，再统一输出 EPUB。该方向虽然有利于多格式扩展，但会损失 EPUB 原有结构、样式、脚注、内部链接、表格和阅读顺序保真度。对当前项目来说，这个代价不可接受。

因此下一阶段目标调整为：

```text
继续保留原 EPUB pipeline
只增强翻译过程的可靠性、一致性、重试和可恢复能力
```

EPUB 仍按原路径处理：

```text
EPUB
  -> 解包
  -> 读取 OPF / spine / XHTML / assets
  -> 在原 XHTML DOM 中抽取可翻译节点
  -> 调用 LLM 翻译
  -> 回写原 XHTML
  -> 重新打包 EPUB
```

本 SPEC 不再讨论 PDF、DOCX 或统一大格式。

## 2. 目标

本次开发要解决的核心问题是：

> 个别段落、表格单元格、标题或脚注在 LLM 返回错误、JSON 解析失败、返回缺项、空译文时，不应被静默跳过，也不应直接退化为原语言而仍标记任务成功。

具体目标：

- LLM 返回坏 JSON 时有重试和降级机制。
- LLM 返回合法 JSON 但缺少某些段落时，可以检测并补译。
- 批次中只有部分段落失败时，保存成功部分，只重试失败部分。
- 某个段落反复失败时，明确记录失败原因。
- 最终 EPUB 输出前做覆盖率检查。
- 允许用户按设置保留失败原文并继续，但任务状态必须带 warning。
- 加入邻居上下文，提升前后文一致性。
- 加入轻量术语表，提升全书术语一致性。
- 不破坏原 EPUB 格式。

## 3. 非目标

本次不做：

- PDF 导入。
- DOCX 导入。
- Markdown 中间格式。
- Canonical Book Model。
- Calibre HTMLZ / OEB 主流程替换。
- Pandoc AST 主流程替换。
- 多 LLM 并发。
- 复杂自动术语管理系统。
- 原 EPUB 样式重建。

## 4. 总体方案

在现有 EPUB pipeline 中新增一个“可验证翻译层”。

```text
EPUB XHTML nodes
  |
  v
Translatable Units
  |
  v
Batch Planner
  |
  +-- Neighbor Context
  +-- Matched Glossary Terms
  |
  v
LLM JSON Translation
  |
  v
Response Validation
  |
  +-- JSON parse
  +-- ID coverage
  +-- empty translation check
  +-- duplicate / extra id check
  |
  v
Retry / Split / Single Unit Fallback
  |
  v
Coverage Gate
  |
  v
Write Back To Original XHTML
```

重点不是改变 EPUB 结构，而是让每个可翻译节点都被跟踪、校验、重试和统计。

## 5. 可翻译单元

### 5.1 定义

可翻译单元是从原 XHTML DOM 中抽取出来、需要发送给 LLM 的最小文本对象。

第一阶段包括：

- 章节标题。
- 普通段落。
- 列表项。
- 引用块。
- 脚注。
- 图片说明。
- 表格标题。
- 表格单元格。

### 5.2 稳定 ID

每个可翻译单元必须有稳定 ID。

ID 不应依赖 LLM 返回顺序，也不应只依赖批次序号。

建议格式：

```text
epub:<chapter_href>:<node_kind>:<node_index>
epub:OEBPS/ch01.xhtml:p:00042
epub:OEBPS/ch03.xhtml:table:0002:row:0003:cell:0004
```

每个单元记录：

```json
{
  "id": "epub:OEBPS/ch01.xhtml:p:00042",
  "chapter_href": "OEBPS/ch01.xhtml",
  "kind": "paragraph",
  "source_text": "Original text...",
  "source_hash": "sha256:...",
  "char_count": 128,
  "dom_ref": {
    "node_index": 42
  }
}
```

### 5.3 DOM 回写

翻译完成后，仍然回写到原 XHTML DOM。

不能从翻译结果列表直接生成内容，必须从源 DOM 节点出发：

```python
for unit in source_units:
    translated = translations.get(unit.id)
    write_back(unit.dom_node, translated)
```

这样可以避免缺失译文被自然跳过。

## 6. LLM 请求与响应格式

### 6.1 请求格式

每批请求包含：

- 目标语言。
- 输出模式。
- 只读前文上下文。
- 只读后文上下文。
- 当前要翻译的 items。
- 当前 batch 命中的 glossary terms。

示例：

```json
{
  "target_language": "zh-CN",
  "previous_context": "Text before this batch, for reference only.",
  "next_context": "Text after this batch, for reference only.",
  "glossary": [
    {
      "source": "Wallfacer",
      "target": "面壁者"
    }
  ],
  "items": [
    {
      "id": "epub:OEBPS/ch01.xhtml:p:00042",
      "text": "Original text..."
    }
  ]
}
```

### 6.2 Prompt 要求

Prompt 必须明确：

- 只翻译 `items` 中的内容。
- `previous_context` 和 `next_context` 仅供理解，不要翻译，不要输出。
- 必须返回合法 JSON。
- 返回 items 必须与输入 items 的 ID 一一对应。
- 不要新增、删除、合并、拆分 ID。
- glossary 中的术语翻译必须优先采用。

### 6.3 响应格式

```json
{
  "items": [
    {
      "id": "epub:OEBPS/ch01.xhtml:p:00042",
      "translation": "译文..."
    }
  ]
}
```

## 7. 响应校验

每次 LLM 返回后必须校验。

### 7.1 JSON 校验

检查：

- 是否是合法 JSON。
- 顶层是否包含 `items`。
- `items` 是否是数组。
- 每个 item 是否包含 `id` 和 `translation`。

允许做轻量修复：

- 去除 Markdown code fence。
- 截取第一个 JSON object。
- 去除尾随逗号。
- 修复常见外层文本包裹。

轻量修复后仍失败，进入重试。

### 7.2 ID 覆盖校验

必须检查：

```text
input_ids == output_ids
```

异常包括：

- `missing_ids`：输入有，输出没有。
- `extra_ids`：输出有，输入没有。
- `duplicate_ids`：输出重复。

处理规则：

- 成功 ID 的译文可以保存。
- `missing_ids` 进入补译队列。
- `extra_ids` 丢弃并记录 warning。
- `duplicate_ids` 选择最后一个或全部视为无效，建议第一版视为无效并重试对应 ID。

### 7.3 内容校验

检查：

- 源文本非空，但译文为空。
- 译文只有空白字符。
- 译文明显是错误消息。
- 译文完全等于源文，且目标语言明显不同。

注意：

“译文等于源文”不能绝对判错，因为人名、代码、数字、品牌名可能本来就不需要翻译。因此只作为 warning 或弱校验。

## 8. 重试与降级

### 8.1 最大重试次数

最大重试次数内置为 3，不暴露给用户。

### 8.2 重试流程

推荐流程：

```text
原批次请求
  |
  +-- 成功且校验通过 -> 保存
  |
  +-- JSON 失败 / HTTP 失败 / 校验失败
        |
        v
      原批次重试，最多 3 次
        |
        v
      仍失败 -> 拆分批次
        |
        v
      小批次重试
        |
        v
      仍失败 -> 单 unit 重试
        |
        v
      单 unit 仍失败 -> 标记 failed
```

### 8.3 部分成功处理

如果一个批次返回了部分合法译文：

- 立即保存合法译文。
- 不要因为同批次中某些 ID 失败而丢弃全部结果。
- 对失败 ID 单独重试。

### 8.4 单 unit 失败

单 unit 达到最大重试次数后：

```json
{
  "id": "epub:OEBPS/ch01.xhtml:p:00042",
  "status": "failed",
  "attempts": 3,
  "last_error": "invalid JSON after retries"
}
```

如果用户设置为“失败内容保留原文并继续”，则：

- 输出原文。
- 在任务详情中记录 warning。
- 任务状态不能是普通 `completed`。
- 应标记为 `completed_with_warnings`。

如果用户设置为“失败则停止”，则：

- 当前任务进入 `failed` 或 `incomplete`。
- 不生成最终完成文件。

## 9. Coverage Gate

最终 EPUB 生成前必须运行覆盖率检查。

检查：

```text
total_units
translated_units
failed_units
missing_units
empty_units
warning_units
```

成功条件：

```text
missing_units = 0
failed_units = 0
empty_units = 0
```

如果用户允许失败保留原文：

```text
missing_units = 0
empty_units = 0
failed_units > 0
status = completed_with_warnings
```

注意：

失败保留原文也必须是显式状态，不能静默当作成功。

## 10. 邻居上下文

### 10.1 目的

提升翻译一致性，尤其是：

- 代词指代。
- 人名称呼。
- 上下文语气。
- 前后段承接。
- 章节内术语含义。

### 10.2 实现方式

每个 batch 附带：

```text
previous_context: 当前 batch 前约 300-500 字
next_context: 当前 batch 后约 300-500 字
```

上下文来自同一章节内相邻可翻译节点。

第一版不跨章节取上下文，避免复杂度和误引用。

### 10.3 校验约束

上下文不分配翻译 ID，不允许出现在输出中。

如果模型输出上下文内容：

- 因为输出 JSON 只接受 `items` ID，所以额外内容会被丢弃。
- 如果污染了某个译文，需要通过内容校验或用户反馈发现。

## 11. 轻量术语表

### 11.1 第一版范围

第一版只做用户可配置术语表，不做复杂自动术语发现。

术语格式：

```json
[
  {
    "source": "Wallfacer",
    "target": "面壁者",
    "note": "Three-Body terminology"
  }
]
```

### 11.2 注入策略

每个 batch 只注入当前源文本中实际命中的术语。

不要把整个 glossary 全量放入 prompt，避免浪费上下文。

匹配规则第一版可以简单：

- 大小写敏感或不敏感由语言判断。
- 精确字符串匹配。
- 支持 source 和 aliases。

### 11.3 状态记录

每个 batch 记录使用过的术语 hash。

未来如果术语表变化，可以判断是否需要重译受影响 batch。

第一版可以先只记录，不立即实现选择性重译。

### 11.4 自动术语候选

自动术语候选作为二期功能。

二期可以：

- 抽样开头、中间、结尾章节。
- 让 LLM 提取人名、地名、组织、专有名词。
- 写入 `suggested_glossary`。
- 用户确认后进入正式 glossary。

第一版不让 LLM 自动修改正式 glossary。

## 12. 状态记录

### 12.1 最小状态

为了支持重试和恢复，需要记录：

```json
{
  "unit_id": "epub:OEBPS/ch01.xhtml:p:00042",
  "source_hash": "sha256:...",
  "target_language": "zh-CN",
  "status": "completed",
  "translation": "译文...",
  "attempts": 1,
  "last_error": null,
  "updated_at": "2026-07-20T00:00:00Z"
}
```

### 12.2 状态类型

```text
pending
processing
completed
retryable_failed
failed
stale
skipped
```

### 12.3 恢复规则

重新运行时：

- `completed` 且 source hash 一致，可以跳过。
- `failed` 可以由用户手动重试。
- `retryable_failed` 自动进入重试队列。
- source hash 不一致，标记为 `stale` 并重新翻译。

## 13. UI 影响

### 13.1 新建翻译任务

新增或确认显示：

- 术语表输入或上传。
- 是否启用邻居上下文，默认开启。
- 翻译预览也应使用相同可靠性流程。

不新增：

- 手动 batch size。
- 手动 max chars。
- 手动 retry count。

### 13.2 任务列表与详情

任务详情增加：

- 总可翻译单元数。
- 已完成数。
- 失败数。
- warning 数。
- 最近失败原因。
- 重试失败单元按钮。

### 13.3 设置

设置中保留：

- 失败内容处理策略。
- 输出模式。
- 翻译标题。
- 翻译脚注。
- LLM_CONTEXT_WINDOW。

最大重试次数不暴露。

## 14. 与当前功能的关系

### 14.1 EPUB 预览

不改变。

上传后仍真实渲染 EPUB。

### 14.2 小范围翻译预览

小范围翻译预览也应经过：

- 稳定 ID。
- 邻居上下文。
- glossary 注入。
- JSON 校验。
- missing ID 补译。

预览结果继续回写到预览 EPUB 中显示。

### 14.3 表格

保留当前策略：

- `append_block`：原表格保持不变，后面追加目标语言表格。
- `replace`：替换单元格文本。

表格单元格也必须作为可翻译 unit 参与覆盖率检查。

## 15. 推荐开发顺序

建议按以下顺序实现：

1. 为当前 XHTML 可翻译节点建立稳定 unit ID。
2. LLM 响应增加严格 ID 覆盖校验。
3. 实现部分成功保存和 missing ID 补译。
4. 实现批次拆分与单 unit 重试。
5. 实现 Coverage Gate。
6. 增加邻居上下文。
7. 增加轻量用户术语表和 batch 命中注入。
8. UI 增加失败统计与重试失败单元。
9. 翻译预览接入同一可靠性流程。

其中 1-5 是可靠性核心，6-7 是一致性核心。

## 16. 验收标准

本阶段完成后，应满足：

- LLM 连续返回坏 JSON 时，不会静默漏译。
- LLM 返回合法 JSON 但缺少 ID 时，可以检测并补译。
- 批次中部分成功的译文不会被丢弃。
- 单个特殊段落反复失败时，可以定位到该段落。
- 最终 EPUB 生成前可以统计 total/completed/failed/warning。
- 失败保留原文时，任务状态为 `completed_with_warnings`。
- 表格单元格参与翻译覆盖率统计。
- 启用邻居上下文后，输出仍只接受当前 batch 的 ID。
- glossary 只注入当前 batch 命中的术语。
- EPUB 原有结构、资源、样式和渲染路径不被整体替换。

## 17. 总结

本阶段不追求多格式统一，也不重建 EPUB。

正确方向是：

```text
保留原 EPUB pipeline
+ 可翻译 unit ID
+ 可验证 LLM 返回
+ 缺失补译
+ 批次拆分
+ 单段重试
+ 覆盖率检查
+ 邻居上下文
+ 轻量术语表
```

这样可以吸收此前流程中最有价值的部分：manifest、run_state、完整性校验、重试、上下文和术语一致性，同时避免破坏 EPUB 原格式。
