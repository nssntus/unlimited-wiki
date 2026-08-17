# Unlimited Wiki

Unlimited Wiki 是一个多用户 Markdown 知识库。每个账号拥有独立的私有空间，可整理正本与 Raw 原料、生成和治理词条，并通过不可变投稿快照、AI 预审和管理员审核将内容发布到公开广场。

项目支持本机开发，也支持由同机 HTTPS 反向代理接入的公司内网单节点部署。Python 后端始终只绑定回环地址；不要将它直接暴露到局域网或公网。当前不支持多节点、高可用或共享网络文件系统。

## 核心能力

- 多租户私有空间：账号、个人组织、空间成员关系和工作区相互隔离；首个注册账号自动成为平台管理员。
- 空间权限：`owner` 可管理内容、模型和导出，`editor` 可编辑与治理内容，`viewer` 只读；平台管理员没有私有空间的隐式访问权。
- 团队协作：用户可创建团队空间、邀请已有账号、切换空间、调整 Editor/Viewer、移除成员、转移 Owner，并停用、恢复或软删除团队空间；Editor 和 Viewer 可主动退出，个人空间保持不可共享。
- Markdown 知识工作流：文章浏览、搜索、编辑、别名、分类、链接检查、合并与重定向。
- 动态分类：每个私有空间维护独立的一级分类和标签；新正本先进入 `_inbox`，AI 基于完整正文给出候选，用户预览确认后再原子移动。
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

登录后可从侧栏切换个人或团队空间。空间选择保存在当前会话中，同一账号在不同浏览器或设备上的会话可以停留在不同空间；切换后前端会清除旧空间的私有查询缓存。团队邀请只发送给已经注册的账号，有效期为 7 天，需要对方在应用内明确接受或拒绝。

团队 Owner 可先停用空间，再恢复或软删除。停用会立即阻止成员访问、撤销待处理邀请，并暂停尚未完成的空间任务；恢复后任务重新排队，但成员必须明确选择该空间才会重新进入。软删除是终态：空间不能访问或恢复，未完成任务以 `workspace_deleted` 结束，但磁盘正文、模型设置、投稿快照和已发布公开版本不会被物理删除。Editor 和 Viewer 可退出团队，活跃或停用空间的 Owner 必须先转移所有权；已经软删除的团队空间保留历史归属，不再阻止账号注销。

当当前空间被停用、删除或成员主动退出时，账号会话仍保持登录，页面进入“选择 Wiki 空间”状态。系统不会自动切换到个人空间或另一个团队空间，避免原本针对旧空间的请求写入错误空间。

## 公司内网部署

唯一受支持的内网拓扑是“浏览器 -> HTTPS Caddy/nginx -> 同机 `127.0.0.1:8765` 后端”。反向代理必须保留外部 `Host`，设置 `X-Forwarded-Proto: https` 和标准 `X-Forwarded-For`。应用只信任 `WIKI_TRUSTED_PROXY_CIDRS` 中的代理地址，并从代理链右侧剥离受信跳点；不要配置整个公司网段。

先完成依赖安装和前端构建，再创建专用系统用户以及仅该用户可写的 `.platform/`、`spaces/`、`.runtime/` 目录。生产环境变量至少包含：

```bash
WIKI_PUBLIC_ORIGIN=https://wiki.intra.example
WIKI_TRUSTED_PROXY_CIDRS=127.0.0.1/32
WIKI_REGISTRATION_MODE=bootstrap
WIKI_MAX_CONCURRENT_REQUESTS=64
WIKI_REQUEST_TIMEOUT_SECONDS=30
WIKI_MIN_FREE_BYTES=536870912
WIKI_PORT=8765
```

`WIKI_PUBLIC_ORIGIN` 必须是单一、规范的 HTTPS Origin，不能带路径、查询参数或凭据。LAN 模式会强制使用 `__Host-wiki_session`、`Secure`、`HttpOnly` 和 `SameSite=Strict` Cookie，并启用 HSTS；Host、Origin 或代理协议不匹配时请求会被拒绝。`WIKI_REGISTRATION_MODE=bootstrap` 只允许创建第一个管理员，成功后自动关闭注册。确认管理员可以登录后，将模式改为 `invite`；管理员在部署机生成绑定邮箱、限时且只能使用一次的邀请令牌：

```bash
python3 account_invites.py create --project-root . --email member@example.com --hours 72
```

通过受控渠道把令牌交给对应用户，用户在注册页输入相同邮箱和令牌。令牌只以 SHA-256 摘要保存，明文只在创建时输出一次。所有公司账号建立完成后可将模式设为 `closed`；当前没有企业 SSO 或邮件开户流程。账号创建后，团队 Owner 再发送 Workspace 邀请。

空数据库不能通过邀请注册创建首位管理员，即使部署机提前生成了邀请令牌也会拒绝；必须先在 `bootstrap` 模式完成管理员初始化。匿名安全限流记录保留 7 天，并在后续限流请求中自动清理。

仓库提供 `deploy/Caddyfile.example` 和 `deploy/systemd/` 模板。将域名替换为公司内网域名，确保所有客户端信任代理签发证书的内部 CA，然后安装并启动：

```bash
sudo useradd --system --home /opt/unlimited-wiki --shell /usr/sbin/nologin unlimited-wiki
sudo install -d -m 700 -o unlimited-wiki -g unlimited-wiki \
  /opt/unlimited-wiki/.platform /opt/unlimited-wiki/spaces /opt/unlimited-wiki/.runtime \
  /var/backups/unlimited-wiki
sudo cp deploy/systemd/unlimited-wiki.service /etc/systemd/system/
sudo cp deploy/systemd/unlimited-wiki-backup.service /etc/systemd/system/
sudo cp deploy/systemd/unlimited-wiki-backup.timer /etc/systemd/system/
sudo install -m 644 deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now unlimited-wiki.service
sudo systemctl enable --now unlimited-wiki-backup.timer
sudo systemctl reload caddy
```

systemd 模板为 SIGTERM 排空保留最多 10 分钟，高于 Caddy 的 60 秒请求窗口；正常停止会先停止接收新请求，并等待已进入后端的请求线程退出，再关闭 Workspace worker 和释放实例锁。离线备份只有在 `systemctl stop` 完成后才开始复制数据。

`GET /healthz` 是无认证存活探针；`GET /readyz` 只返回前端、存储、数据库、主密钥和磁盘余量的布尔检查，不会创建 Workspace service 或泄露路径。请求日志以 JSON Lines 输出到 stdout，包含 `request_id`、规范化路径、状态码、耗时、响应大小和经过受信代理解析的客户端 IP，可由 journald 收集。后端容量拒绝会单独记录 `capacity_rejected`，Caddy 模板也启用 JSON access log：

```bash
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/readyz
sudo journalctl -u unlimited-wiki.service -f
```

应用使用有界并发以及 socket/body 读取时限，Caddy 模板另设 header、body、write 和 idle timeout；超过并发上限时返回 `503` 和 `Retry-After`。这些限制不是 Python 业务处理的强制总 deadline，远端模型调用仍使用各自的超时和持久任务恢复。登录、注册、恢复码和举报还使用平台 SQLite 中的持久限流，应用重启不会清空计数。上线前应在部署机执行容量检查并按 CPU、内存和磁盘实测调整并发上限：

```bash
python3 capacity_check.py https://wiki.intra.example/healthz --requests 1000 --concurrency 50
```

健康探针只验证入口。正式容量验收还应为测试账号设置临时 `WIKI_CAPACITY_COOKIE`，分别检查文章列表、搜索和读取端点；测试完成后立即 `unset WIKI_CAPACITY_COOKIE`。携带 Cookie 时工具只接受初始 HTTPS URL，并且不会跟随任何重定向，避免会话被转发到其他 Origin 或降级地址。出现任何非 `200` 状态时工具返回非零退出码。

### 备份与恢复

完整实例数据包括 `.platform/` 和整个 `spaces/`。其中 `.platform/master.key` 用于解密 Workspace 模型凭据，丢失后无法恢复。由于平台 SQLite、各 Workspace SQLite 与 Markdown 文件之间没有全局快照事务，可靠备份必须停服执行，不能用运行中的裸文件复制代替。

```bash
sudo systemctl stop unlimited-wiki.service
sudo -u unlimited-wiki python3 backup_restore.py backup --project-root . --output /var/backups/unlimited-wiki/wiki-20260817T033000Z
sudo -u unlimited-wiki python3 backup_restore.py verify /var/backups/unlimited-wiki/wiki-20260817T033000Z
sudo systemctl start unlimited-wiki.service
```

备份会先取得实例锁、checkpoint 并检查所有 SQLite 数据库，再生成逐文件 SHA-256 manifest，并以原子目录改名发布。尚未初始化的 Workspace 可以没有 `.wiki-state/`，或保留一个空目录；一旦该目录中出现 WAL、锁文件或其他状态产物，`state.sqlite3` 就必须存在且通过完整性检查，否则备份和验证都会拒绝。备份目录应位于非 Web 根目录、权限为 `0700` 的加密磁盘；TLS 私钥和 `/etc/unlimited-wiki.env` 需通过公司的秘密备份流程另行保管。定时单元调用 `deploy/offline-backup.sh`：它只会重启脚本实际停止的服务，重启失败会让备份 unit 失败。备份保留清理由运维平台完成，不会自动删除唯一副本。

恢复必须在停服状态下进行，并且目标不能已有 `.platform/` 或 `spaces/`。先把旧数据目录移动到隔离位置，再执行：

```bash
python3 backup_restore.py verify /var/backups/unlimited-wiki/wiki-20260817T033000Z
sudo python3 backup_restore.py restore /var/backups/unlimited-wiki/wiki-20260817T033000Z \
  --project-root . --owner unlimited-wiki
sudo -u unlimited-wiki WIKI_DISABLE_REMOTE_WORKER=1 python3 serve.py
```

备份、恢复和服务进程共用 `.runtime/instance.lock`；备份取得锁后若发现 `.restore/` 会拒绝运行，不能从半恢复实例生成灾备副本。高权限恢复要求项目根由 root 管理且不能被服务账号、组或其他用户写入；恢复的 journal、staging 和待发布副本只放在恢复进程拥有的项目根 `.restore/`（`0700`）中，不放入服务账号可写的 `.runtime/`。恢复会校验 manifest、主密钥以及固定位置的平台/Workspace SQLite 数据库，撤销备份中的浏览器会话，并在未发布副本上通过 fd 锚定、不跟随符号链接、子项优先而根目录最后的方式应用 `--owner`；journal 同时绑定 owner 名称和解析后的 UID/GID，续做时任一项变化都会拒绝。再次校验后才原子发布 `.platform/` 与 `spaces/`。最终数据发布完成后才把稳定的 `.runtime/` 和 `instance.lock` 交给服务账号，再原子将 `.restore/` 标记为 `.restore-complete-*` 并清理。`.restore/` 表示恢复未完成并阻断服务和备份；`.restore-complete-*` 仅表示数据与属主均已发布、最后的清理被中断，不阻断服务或新备份，但目录仍含敏感副本，保持 root 私有且由运维在确认不存在 `.restore/` 后清除。两类目录均已从 Git 排除。复制、校验或属主交接失败会保留私有 journal，必须使用同一备份、owner 名称和 UID/GID 重试。完成只读冒烟检查后停止临时进程，再通过 systemd 正常启动。至少每季度在隔离目录进行一次恢复演练；没有经过恢复验证的备份不能视为可用。

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

模型不是浏览、编辑和本地检索的必要条件；AI 生成、Raw 预计分类、完整正文归类、AI 治理和投稿预审需要有效的模型配置。Raw 和正文归类均走本地持久任务队列，断网或模型失败不会阻塞原料入箱、正文阅读或人工归类。

## Raw 原料格式

原料箱会保留上传文件的原始字节，文字提取结果只缓存在当前用户空间的 `.wiki-state/extracted/` 中。文档解析和图片 OCR 均在本机完成。若当前空间已经配置模型，上传后的完整文字会通过持久后台任务发送到该模型，用于生成“预计分类方案”；方案只作预览，不创建目录。未配置模型时使用本地保守提示，用户仍可继续摄入和手动归类。用户点击生成词条后，相关原文摘录也会按模型设置进入生成流程。

## 动态分类工作流

每个工作区的分类注册表保存在 `wiki/.categories.json`，随私有 Wiki 一起导出；运行时建议、预览和对账状态保存在该空间的 `.wiki-state/state.sqlite3`。首次打开旧空间时会保留现有一级目录，为分类和词条补充稳定 ID；旧 `concepts` 目录中的词条只标记为待确认，不会自动移动。

1. Raw 入箱后，应用展示预计分类方案，但不创建正式目录。
2. 新建或采用种子正本时，文件先保存到 `wiki/_inbox/`；补充现有正本时保留原分类。
3. 后台模型基于完整正文返回最多三个现有分类候选、置信度、理由、标签和可选新分类。
4. 在“待归类”工作台选择推荐、改选或内联新建分类；高置信建议默认选中，内联分类可编辑并复用于多篇词条，也可批量指定现有分类和追加标签。选择本身不会移动文件，用户可明确暂缓或移除选择。
5. 提交前检查源/目标、正文 revision 和分类体系 revision；确认后原子创建目录、移动文件、更新元数据、相对链接、索引与日志，并返回可回滚的 operation ID。
6. “分类管理”支持创建、改名、排序、归档、恢复、批量迁移词条和删除空分类；非空分类必须先迁移或归档。
7. “文件对账”只报告应用外的新目录、移动、缺失或元数据冲突，必须由用户选择采纳、恢复或暂缓。

归类提交、分类管理和文件对账成功后都会保留最近一次 operation ID，并提供显式的整批回滚入口；若相关文件之后已被修改，系统会拒绝不安全的回滚。

分类只支持 `wiki/` 下一级目录；`_inbox` 是保留目录。分类名称采用 Unicode NFKC 规范化并忽略大小写检查重复，路径逃逸、保留名、符号链接和目标同名冲突会被拒绝，不会自动添加数字后缀。

| 类型 | 格式 | 处理方式 |
| --- | --- | --- |
| 文本与网页 | Markdown、TXT、CSV、TSV、JSON、YAML、XML、HTML | 本机直接读取 |
| Office Open XML | DOCX、XLSX/XLSM、PPTX/PPTM | 本机直接解析 |
| PDF 与电子书 | PDF、EPUB | 本机提取文字层 |
| 旧版 Office/OpenDocument | DOC/DOCM、XLS、PPT、ODT、ODS、ODP、RTF | 先由本机 LibreOffice 转换 |
| 图片 | PNG、JPEG、WebP、TIFF、BMP、GIF、HEIC | 本机 Tesseract OCR；HEIC 转换目前依赖 macOS `sips` |

单个原文件最大 10 MiB，图片最大 2500 万像素；超出像素限制的图片会在完整解码和 OCR 前直接拒绝。上传接口使用 Base64 JSON，因此请求封装会略大于原文件，不受其他普通写接口 64 KiB 上限约束。图片未识别到有效文字时会警告并退回，Raw 不会落盘；OCR 结果只有单个疑似噪声字符时也按无文字处理。

当前 PDF 只读取已有文字层，不对扫描页自动 OCR；多帧 GIF 和多页 TIFF 当前只读取第一帧。受密码保护、损坏、超出页数/行数/解压或提取文字上限的文件会在写入 Raw 前拒绝。

## 数据目录

运行数据默认位于仓库内，但均已被 Git 忽略：

```text
.platform/                          账号、组织/空间成员关系、加密主密钥和审计数据
spaces/<workspace-id>/wiki/        私有 Markdown 正本
spaces/<workspace-id>/wiki/.categories.json  可导出的动态分类注册表
spaces/<workspace-id>/wiki/_inbox/ 待归类正本
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
| `WIKI_PUBLIC_ORIGIN` | 内网部署唯一 HTTPS Origin；设置后进入严格 LAN 模式 | 未设置 |
| `WIKI_TRUSTED_PROXY_CIDRS` | 可提供真实客户端 IP 和 HTTPS 协议的同机代理 CIDR | 未设置 |
| `WIKI_REGISTRATION_MODE` | `open`、`bootstrap`、`invite` 或 `closed` | 本机 `open`；LAN `bootstrap` |
| `WIKI_MAX_CONCURRENT_REQUESTS` | 后端同时处理的请求上限 | `64` |
| `WIKI_REQUEST_TIMEOUT_SECONDS` | 单连接 socket 超时 | `30` |
| `WIKI_MIN_FREE_BYTES` | readiness 要求的数据盘最小剩余字节数 | `536870912` |
| `WIKI_DISABLE_REMOTE_WORKER` | 设为 `1` 时不启动远端任务工作器 | `0` |
| `WIKI_REMOTE_TASK_KINDS` | 限制远端工作器处理的任务类型，逗号分隔 | 全部支持类型 |
| `WIKI_SECURE_COOKIES` | 设为 `1` 时为会话 Cookie 添加 `Secure` | `0` |
| `WEB_ALLOW_FAKE_IP` | 兼容透明代理将域名解析到 `198.18.0.0/15` 的场景 | `0` |
| `WIKI_OCR_LANG` | Tesseract OCR 语言组合 | `chi_sim+eng` |

## 安全边界

- 后端只允许绑定 `127.0.0.1`、`localhost` 或 `::1`。LAN 模式精确校验外部 HTTPS Host、Origin 和受信代理协议，转发头仅在 socket peer 命中明确 CIDR 时使用。
- 回环 TCP 代理假设同一主机上的本地进程属于同一信任域：任何能连接后端端口的本地进程都可能伪造 `X-Forwarded-For`。不要在存在不可信本地账号或容器的主机上使用该模板；此类环境需要改用带文件权限的 Unix socket 或独立网络命名空间后才能上线。
- 多用户写请求使用会话、CSRF 和幂等保护；团队管理操作按账号、会话或当前空间隔离幂等作用域，并与平台数据变更在同一事务提交。密码使用 scrypt 派生。
- 私有 API 每次请求都从服务端会话重新验证当前空间成员关系；客户端提交的 Workspace、Owner 或角色字段不能形成授权。
- 团队空间只允许 Owner 管理成员。Owner 可邀请 Editor 或 Viewer、调整角色、移除成员并把 Owner 转移给现有活跃成员；最后一个 Owner 不能被直接移除或降级。个人空间不能邀请成员或转移 Owner。
- 当前空间按会话独立保存，不使用账号级全局切换。成员从当前团队空间被移除后，指向该空间的会话立即失效，不会自动改投其他空间执行原请求。
- 团队空间生命周期只允许当前活跃 Owner 操作：`active -> suspended -> active`，或 `suspended -> deleted`。个人空间不能停用、删除或退出；平台管理员没有私有空间旁路权限。停用、恢复、软删除、退出、会话解绑、邀请撤销、通知与审计在平台事务中提交。
- 当前空间不可用时，身份会话与 Workspace 上下文分离：认证仍有效，私有 Wiki API 返回 `409 workspace_selection_required`，只有空间列表、显式切换和账号级操作可继续。前端在确认服务端空间前会卸载旧 Wiki 并清除租户缓存。
- 停用空间的 `staged`、`queued` 和 `running` 任务转为 `paused`，恢复后按原阶段重新激活；软删除后所有未完成任务以 `workspace_deleted` 终结。迟到的远端结果仍受发起者权限、任务状态和 attempt fencing 约束，不会写入已停用或删除的空间。
- 平台记录的 Workspace 状态是生命周期真相来源。生命周期请求重放、服务启动和空间服务首次获取都会按当前状态幂等对账任务投影；旧的停用请求不会覆盖后来完成的恢复，平台提交后中断也会在重启或重试时修复。
- 生成、补证、治理和分类任务记录服务端解析出的发起者；worker 在领取和最终写入前重新检查其写权限。发起者被撤权后任务以 `auth_revoked` 结束，不提交远端结果。
- 私有投稿、发布状态和撤回操作同时绑定账号与当前空间；同一账号在不同空间中的同名文章不会共享投稿或公开版本状态。
- 个人组织和个人空间不可转移；个人空间仍有其他活跃成员时，必须先解除成员关系，账号注销会返回 `422` 且不产生任何变更。注销活跃或停用团队空间的 Owner 时，系统按现有 Owner、Editor、Viewer 的顺序稳定选择空间接任者，并按 Owner、Admin、Member 的顺序独立选择组织接任者；转移、审计和注销在同一事务内完成。任一仍可治理的组织或团队空间没有接任者时也返回 `422`，整次注销不产生变更。仅包含软删除空间的团队组织不再要求接任，终态空间的磁盘、模型、投稿和公开修订继续保留；仅未共享的个人空间会清理本地数据。
- Markdown 经净化后渲染，并设置 CSP、`nosniff` 和严格 Referrer Policy。
- 网页补证拒绝本地、私网、带凭据 URL 和 HTTPS 降级跳转。
- 本机开发默认开放注册；LAN 模式禁止开放注册，只允许首位管理员 bootstrap、邮箱绑定的单次邀请或完全关闭。bootstrap 的首位用户判定、邀请令牌消费与账号创建都在 SQLite 写事务中完成。
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
| `dynamic_categories.py` | 动态分类注册表、稳定 ID、标签和分类元数据 |
| `state_store.py` | 工作区任务、幂等、归类建议、预览、Raw 与对账状态 |
| `storage.py` | 原子文件及目录事务、恢复与回滚 |
| `platform_store.py` | 账号、工作区、投稿、公开版本、举报、通知和审计 |
| `platform_review.py` | 不可变投稿快照的后台 AI 预审 |
| `security.py`、`websearch.py` | 出站网络限制与远端补证 |
| `viewer/` | React 前端 |

## 贡献与文档维护

所有改动通过功能分支和 Pull Request 合并，不直接推送 `main`。任何改变功能、安装方式、配置、安全边界、数据布局或开发流程的提交，都必须在同一个 Pull Request 中同步维护本 README。

提交前请确认暂存区不包含 `.env`、API Key、数据库、Wiki/Raw 内容、运行状态或其他项目文档。

## License

[MIT](LICENSE)
