# Unlimited Wiki

Unlimited Wiki 是一个仅绑定本机回环地址的多用户 Markdown 知识库。每个账号拥有独立的私有空间，可整理正本与 Raw 原料、生成和治理词条，并通过不可变投稿快照、AI 预审和管理员审核将内容发布到公开广场。

当前项目面向本机使用和开发验证，不是可直接暴露到公网的部署版本。

## 核心能力

- 多租户私有空间：账号、会话、角色和工作区相互隔离；首个注册账号自动成为管理员。
- Markdown 知识工作流：文章浏览、搜索、编辑、别名、分类、链接检查、合并与重定向。
- Raw 原料箱：导入常用文档、表格、演示、网页、电子书和图片；在本机提取文字或 OCR 后预览并摄入私有正本。
- 可靠后台任务：生成、补证和治理任务持久化，支持重试、取消和崩溃恢复。
- 文件事务：跨文件写入使用锁、原子替换和 before-image，可按条件回滚。
- 工作区模型配置：支持 OpenAI、DeepSeek 及 OpenAI-compatible 公网模型服务；密钥加密保存且不会通过状态接口回显。
- 公开协作：精确投稿快照、AI 预审、管理员审核、版本更新、通知、举报、下架与恢复。
- 安全渲染：前端使用本地打包的 Markdown 渲染和净化依赖，不依赖运行时 CDN。

## 技术栈

- 后端：Python 标准库 HTTP 服务、SQLite、OpenAI Python SDK、Cryptography、PyMuPDF、Pillow 与 Office 文档解析器
- 前端：React 19、TypeScript、Vite 8、Tailwind CSS 4、shadcn/Base UI、TanStack Query
- 存储：每个工作区的 Markdown 文件与 SQLite 状态库

## 环境要求

- macOS 或 Linux
- Python 3.10+
- Node.js `^20.19.0` 或 `>=22.12.0`
- npm
- 图片 OCR 需要 Tesseract，并至少安装与 `WIKI_OCR_LANG` 对应的语言数据（默认 `chi_sim+eng`）
- `.doc`、`.docm`、`.xls`、`.ppt`、OpenDocument 与 RTF 需要系统可执行的 LibreOffice `soffice`

## 快速开始

```bash
git clone https://github.com/nssntus/unlimited-wiki.git
cd unlimited-wiki

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt

npm --prefix viewer ci
npm --prefix viewer run build

python3 serve.py
```

也可以使用 `./start.sh` 启动后端。默认访问地址为 [http://127.0.0.1:8765](http://127.0.0.1:8765)。前端尚未构建时，后端会返回 `503`。

首次注册的账号会成为管理员。注册成功时系统只显示一次恢复码，恢复码有效期为 24 小时，请立即保存到安全位置。后续注册账号为普通用户。

## 前端开发

先启动允许 Vite 开发源的后端：

```bash
WIKI_DEV_ORIGINS=http://127.0.0.1:5173 python3 serve.py
```

再在另一个终端启动前端：

```bash
npm --prefix viewer run dev
```

## 模型配置

登录后可在“设置”中为当前私有空间配置模型提供方、Base URL、API Key 和模型名称。模型接口需兼容 OpenAI 的模型列表与 Chat Completions 协议。

默认多用户模式会拒绝 `localhost`、`.local`、私网 IP 和其他非全局地址，因此不能直接连接本机 Ollama 或内网模型服务。API Key 使用平台主密钥派生的范围密钥进行 AES-GCM 加密，状态接口只返回是否已配置，不返回密钥内容。

模型不是浏览、编辑和本地检索的必要条件；AI 生成、AI 治理和投稿预审需要有效的模型配置。

## Raw 原料格式

原料箱会保留上传文件的原始字节，文字提取结果只缓存在当前用户空间的 `.wiki-state/extracted/` 中。文档解析和图片 OCR 均在本机完成，不会因为上传或预览而把内容发送给模型；只有用户后续主动生成词条时，相关摘录才按模型设置进入生成流程。

| 类型 | 格式 | 处理方式 |
| --- | --- | --- |
| 文本与网页 | Markdown、TXT、CSV、TSV、JSON、YAML、XML、HTML | 本机直接读取 |
| Office Open XML | DOCX、XLSX/XLSM、PPTX/PPTM | 本机直接解析 |
| PDF 与电子书 | PDF、EPUB | 本机提取文字层 |
| 旧版 Office/OpenDocument | DOC/DOCM、XLS、PPT、ODT、ODS、ODP、RTF | 先由本机 LibreOffice 转换 |
| 图片 | PNG、JPEG、WebP、TIFF、BMP、GIF、HEIC | 本机 Tesseract OCR；HEIC 转换目前依赖 macOS `sips` |

单个原文件最大 10 MiB。上传接口使用 Base64 JSON，因此请求封装会略大于原文件，不受其他普通写接口 64 KiB 上限约束。图片未识别到有效文字时会警告并退回，Raw 不会落盘；OCR 结果只有单个疑似噪声字符时也按无文字处理。

当前 PDF 只读取已有文字层，不对扫描页自动 OCR；多帧 GIF 和多页 TIFF 当前只读取第一帧。受密码保护、损坏、超出页数/行数/解压或提取文字上限的文件会在写入 Raw 前拒绝。

## 数据目录

运行数据默认位于仓库内，但均已被 Git 忽略：

```text
.platform/                          平台数据库、加密主密钥和审计数据
spaces/<workspace-id>/wiki/        私有 Markdown 正本
spaces/<workspace-id>/raw/         私有 Raw 原料
spaces/<workspace-id>/.wiki-state/ 任务、幂等记录和事务历史
viewer/                             前端源码与构建配置
```

根目录下旧的 `wiki/`、`raw/` 和 `.wiki-state/` 仅用于首位管理员的旧单空间数据迁移，不是当前默认的数据边界。不要提交这些目录、`.env`、数据库、模型凭据或任何真实知识内容。

## 常用配置

| 环境变量 | 作用 | 默认值 |
| --- | --- | --- |
| `WIKI_PORT` | 修改本机服务端口，仍只绑定回环地址 | `8765` |
| `WIKI_DEV_ORIGINS` | 允许指定的 Vite 开发源访问后端 | 未设置 |
| `WIKI_DISABLE_REMOTE_WORKER` | 设为 `1` 时不启动远端任务工作器 | `0` |
| `WIKI_REMOTE_TASK_KINDS` | 限制远端工作器处理的任务类型，逗号分隔 | 全部支持类型 |
| `WIKI_SECURE_COOKIES` | 设为 `1` 时为会话 Cookie 添加 `Secure` | `0` |
| `WEB_ALLOW_FAKE_IP` | 兼容透明代理将域名解析到 `198.18.0.0/15` 的场景 | `0` |
| `WIKI_OCR_LANG` | Tesseract OCR 语言组合 | `chi_sim+eng` |

## 安全边界

- 服务只允许绑定 `127.0.0.1`、`localhost` 或 `::1`，并校验 Host 和 Origin。
- 多用户写请求使用会话、CSRF 和幂等保护；密码使用 scrypt 派生。
- Markdown 经净化后渲染，并设置 CSP、`nosniff` 和严格 Referrer Policy。
- 网页补证拒绝本地、私网、带凭据 URL 和 HTTPS 降级跳转。
- 注册入口当前始终开启，首个注册账号即管理员；启动新实例时应控制首次注册时机。
- 项目依赖 Unix 文件锁，当前不声明原生 Windows 支持。

## 验证

安装开发依赖并运行后端测试：

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q -m 'not performance'
python3 -m pytest -q -m performance
```

验证前端：

```bash
npm --prefix viewer ci
npm --prefix viewer run lint
npm --prefix viewer run typecheck
npm --prefix viewer run build
```

## 主要模块

| 路径 | 职责 |
| --- | --- |
| `serve.py` | HTTP API、静态前端、会话与启动编排 |
| `wiki_service.py` | 私有知识库工作流 |
| `document_ingest.py` | 文档解析、格式转换、图片 OCR 与提取缓存 |
| `state_store.py` | 工作区任务、幂等和 Raw 状态 |
| `storage.py` | 原子文件事务、恢复与回滚 |
| `platform_store.py` | 账号、工作区、投稿、公开版本、举报、通知和审计 |
| `platform_review.py` | 不可变投稿快照的后台 AI 预审 |
| `security.py`、`websearch.py` | 出站网络限制与远端补证 |
| `viewer/` | React 前端 |

## 贡献与文档维护

所有改动通过功能分支和 Pull Request 合并，不直接推送 `main`。任何改变功能、安装方式、配置、安全边界、数据布局或开发流程的提交，都必须在同一个 Pull Request 中同步维护本 README。

提交前请确认暂存区不包含 `.env`、API Key、数据库、Wiki/Raw 内容、运行状态或其他项目文档。

## License

[MIT](LICENSE)
