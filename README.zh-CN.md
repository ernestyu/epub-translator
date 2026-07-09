# EPUB Translator Web

中文 | [English](README.md)

EPUB Translator Web 是一个可本地部署、适合 Docker 运行的 EPUB 双语翻译 Web 应用。它支持上传 EPUB、浏览器真实预览、按章节和字符范围进行小段翻译预览，并通过 OpenAI-compatible LLM API 执行整本书翻译。

本项目的核心原则是：EPUB/XHTML 结构由程序控制，LLM 只接收纯文本并返回 JSON 译文，避免让模型直接生成 XHTML/XML。

## 功能特性

- Gradio 浏览器界面，支持中文/英文 UI 切换。
- 使用 EPUB.js 进行真实 EPUB 渲染预览。
- 支持按章节 + 字符范围进行翻译预览。
- 后台翻译任务，浏览器刷新或断开后任务仍可继续运行。
- 任务状态持久化到 `data/jobs/<job_id>/state.json`。
- 章节级 checkpoint 写入 `data/jobs/<job_id>/translated/`。
- batch 级 JSON 校验、重试、缓存和保守 JSON 修复。
- 根据 `LLM_CONTEXT_WINDOW` 自动计算 token 批次预算。
- 支持 OpenAI-compatible Chat Completions API。
- 任务列表、任务详情、继续任务、重跑失败章节、取消任务、删除任务和输出文件。
- 生成的 EPUB 保存到 `data/output/`。

## 快速开始

1. 复制环境变量示例：

   ```bash
   cp .env.example .env
   ```

2. 编辑 `.env`，配置你的 LLM 服务：

   ```env
   LLM_API_KEY=ollama
   LLM_BASE_URL=http://host.docker.internal:11434/v1
   LLM_MODEL=qwen3:32b
   LLM_CONTEXT_WINDOW=8192
   UI_LANGUAGE=zh
   ```

3. 使用 Docker Compose 启动：

   ```bash
   docker compose up -d --build
   ```

4. 打开浏览器：

   ```text
   http://localhost:7860
   ```

生成的 EPUB 文件在：

```text
./data/output
```

## Docker 编译和部署

### 推荐方式：Docker Compose

构建并启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f epub-translator
```

停止服务：

```bash
docker compose down
```

代码更新后重新构建：

```bash
docker compose build --no-cache
docker compose up -d
```

项目的 `docker-compose.yml` 会挂载本地运行数据：

```yaml
volumes:
  - ./data:/data
```

因此任务、日志、缓存和输出 EPUB 文件会保存在宿主机，即使容器重建也不会丢失。

### 手动 Docker 构建

构建镜像：

```bash
docker build -t epub-translator-web:0.1.5 .
```

运行容器：

```bash
docker run --rm -p 7860:7860 --env-file .env -v ./data:/data epub-translator-web:0.1.5
```

如果你在 Linux 主机上使用宿主机本地 LLM 服务，需要确保容器能访问 `host.docker.internal`。本项目的 Compose 文件已经包含：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

## 界面流程

应用包含三个标签页：

1. **新建翻译任务**
   - 顶部可立即切换界面语言。
   - 上传 EPUB。
   - 查看真实 EPUB 渲染预览。
   - 选择章节和字符范围进行翻译预览。
   - 执行整本书翻译，或只翻译当前预览范围。

2. **任务列表**
   - 刷新并选择任务。
   - 查看任务详情和章节状态。
   - 继续任务、重跑失败章节、取消任务。
   - 删除任务及其输出文件。
   - 下载完成后的 EPUB。

3. **设置**
   - 配置翻译默认行为。
   - 配置 LLM Provider、Base URL、API Key、模型和上下文大小。
   - 刷新模型列表并测试选中模型。
   - 保存设置到 `.env`。

界面语言切换位于“新建翻译任务”页顶部。设置会保存到 `.env`。大多数可见控件会立即切换语言；如果浏览器保留旧的标签页标题，刷新页面即可。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | `ollama` | OpenAI-compatible API key。 |
| `LLM_BASE_URL` | `http://host.docker.internal:11434/v1` | OpenAI-compatible Base URL。 |
| `LLM_MODEL` | `qwen3:32b` | 模型名称。 |
| `LLM_CONTEXT_WINDOW` | `8192` | 用于自动 batch 预算的上下文大小。 |
| `SOURCE_LANGUAGE` | `English` | 默认源语言。 |
| `TARGET_LANGUAGE` | `Simplified Chinese` | 默认目标语言。 |
| `UI_LANGUAGE` | `zh` | 界面语言：`zh` 或 `en`。 |
| `DEFAULT_OUTPUT_MODE` | `append_block` | `append_block` 或 `replace`。 |
| `DEFAULT_CHAPTER_FAILURE_POLICY` | `keep_original_on_failed_chapter` | 章节失败处理策略。 |
| `DEFAULT_TRANSLATE_TITLES` | `true` | 是否翻译标题。 |
| `DEFAULT_TRANSLATE_FOOTNOTES` | `true` | 是否翻译脚注类元素。 |
| `APP_HOST` | `0.0.0.0` | Gradio 监听地址。 |
| `APP_PORT` | `7860` | Gradio 端口。 |
| `DATA_DIR` | `/data` | Docker 容器内运行数据目录。 |

## 翻译流程

1. 解包上传的 EPUB。
2. 解析 OPF package 和 spine。
3. 按 spine 顺序处理 XHTML 章节。
4. 从 `p`、`li`、`blockquote`、标题等标签中提取可翻译文本块。
5. 根据 `LLM_CONTEXT_WINDOW` 自动计算 token 预算并分 batch。
6. LLM 接收 JSON 输入并返回 JSON 译文。
7. 程序校验译文 JSON 并插入 XHTML DOM。
8. 每章完成后写入 checkpoint。
9. 按 EPUB zip 规范重新打包。

LLM 不会被要求生成 XHTML/XML。

## 数据目录

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

## 本地开发

安装依赖：

```bash
python -m pip install -r requirements.txt
```

本地运行：

```bash
python app.py
```

运行测试：

```bash
python -m unittest discover -s tests
```

## 说明和限制

- EPUB 预览使用 CDN 上的 EPUB.js。如果需要完全离线部署，请将 JS 文件本地化并修改 `app/preview.py`。
- 当前版本同一时间只运行一个后台任务。
- EPUB 页码依赖阅读器动态排版，因此翻译预览使用“章节 + 字符范围”，不使用固定页码。
- 当前支持 OpenAI-compatible API，不实现非兼容接口的 Provider 专有 API。

## 致谢

- 感谢 [oomol-lab/epub-translator](https://github.com/oomol-lab/epub-translator)。本项目最早的简单原型曾使用该包，也参考了其 EPUB 翻译思路。
- 当前实现运行时**不依赖** `oomol-lab/epub-translator`。EPUB 解析、checkpoint、batch、重打包等逻辑均在本仓库中实现。
- EPUB 渲染预览由 [EPUB.js](https://github.com/futurepress/epub.js) 提供支持。

## 版本

当前版本：`0.1.5`

## 许可证

Apache-2.0。详见 [LICENSE](LICENSE)。
