# Unlimited Wiki

Unlimited Wiki 是一个多用户 Markdown 知识库。每个账号拥有独立的私有空间，可整理正本与 Raw 原料、生成和治理词条，并通过不可变投稿快照、AI 预审和管理员审核将内容发布到公开广场。

项目支持本机开发，也支持由同机 HTTPS 反向代理接入的公司内网单节点部署。Python 后端始终只绑定回环地址；不要将它直接暴露到局域网或公网。当前不支持多节点、高可用或共享网络文件系统。

## 核心能力

- 多租户私有空间：账号、个人组织、空间成员关系和工作区相互隔离；首个注册账号自动成为平台管理员。
- 空间权限：`owner` 可管理内容、模型和导出，`editor` 可编辑与治理内容，`viewer` 只读；平台管理员没有私有空间的隐式访问权。
- 团队协作：用户可创建团队空间、邀请已有账号、切换空间、调整 Editor/Viewer、移除成员、转移 Owner，并停用、恢复或软删除团队空间；Editor 和 Viewer 可主动退出，个人空间保持不可共享。
- Markdown 知识工作流：可从空白手动创建词条，也可从 Raw 整理正本；支持文章浏览、搜索、带格式工具栏和安全预览的 Markdown 编辑、别名、分类、链接检查、合并与重定向。
- 即时分类：每个私有空间维护独立的一级分类和标签；保存时可搜索已有项或在当前选择器内创建，未选择则保留在 `_inbox`，不使用 AI 推荐或自动分类。
- Raw 原料箱：导入常用文档、表格、演示、网页、电子书和图片；在本机提取文字或 OCR 后预览并摄入私有正本。
- 可靠后台任务：生成、补证和治理任务持久化，支持重试、取消和崩溃恢复。
- 文件事务：跨文件写入使用锁、原子替换和 before-image，可按条件回滚。
- 工作区模型配置：支持 OpenAI、DeepSeek 及 OpenAI-compatible 公网模型服务；密钥加密保存且不会通过状态接口回显。
- Wiki 广场：匿名浏览与全文搜索、公共分类和标签、人工精选专题、不可变版本与相邻可见版本差异，以及来源、审核事实和相关词条。
- 公开协作：精确投稿快照、使用投稿 Workspace 个人模型的统一规则 AI 预审、管理员审核、发布负责人治理、复用许可、私人导入、订阅、结构化纠错、举报与审计。
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

## Wiki 广场

广场是公共知识发现入口，不是私人 Wiki 目录的镜像，也不是按互动热度排序的信息流。匿名访客可以浏览首页、搜索公开正文，并沿公共分类、公共标签、Admin 人工编排的精选专题、作者公开主页和相关词条继续阅读。投稿者可在投稿选择器中使用已有公共分类和标签，或提交新名称提案；提案在批准前不会进入公共导航、搜索或其他用户的选择器。Admin 在原投稿审核页接受、映射或确认创建提案；对于分类提案功能上线前遗留的“待公共分类”公开词条，Admin 可在“分类与策展”中搜索选择已有分类，或在同一选择器即时创建分类。“分类与策展”同时提供可搜索、按精选状态筛选的已发布词条工作台，管理员填写整数顺序和审计理由后可直接加入精选，也可取消已有精选，不需要打开完整正文预览；“内容”列表也会直接显示精选状态、顺序和快捷操作。分类创建或规范化同名复用、词条绑定、审计与公开索引补偿均在同一平台事务中处理；任意空分类创建接口仍不开放。搜索使用游标分页；列表、搜索、专题和关系查询只展示仍处于 `published` 状态、当前修订公开且公共索引已追上当前修订的词条，派生索引落后时宁可暂时隐藏旧投影。

公开修订一经发布不会原地改写。详情页展示当前版本、来源、内容 hash、审核规则与模型版本等事实；这些信息用于帮助读者判断，不代表平台保证内容绝对正确。版本历史只公开未隔离的修订，差异比较只允许相邻的可见版本。登录用户可以订阅词条更新、提交绑定到具体公开修订的结构化纠错，并查看自己的处理结果；匿名用户只能在严格限流下举报。举报和纠错记录目前长期保留，直至后续独立数据保留政策生效，不会公开展示提交者身份。

首个获批版本的投稿用户成为该公开词条唯一的发布负责人（entry steward）。治理权绑定用户，不随源 Workspace 的 Owner、Editor 或 Viewer 角色转移；只有发布负责人可以提交该词条的新版本、撤回整个词条、修改复用许可或关联自己的公开主页。团队中的其他成员可以创建独立投稿，但不能把内容强行接入既有版本谱系。取消待审投稿只终止该次投稿，不影响已经发布的词条；作者撤回是不可由 Admin 恢复的终态。Admin 下架只改变平台可见性，之后可按审核流程恢复；非当前历史修订可以单独隔离。账号注销会停用公开主页、撤销未来复用许可，并以作者撤回语义终止该用户负责的全部公开词条，公共正文、历史版本、昵称和主页链接不再可见。

### 复用许可与私人导入

公开词条默认仅允许阅读。发布负责人必须主动确认版本化许可，才能开启“允许复制到私人 Wiki”；许可作用于整个 entry，但每次导入只复制执行时的当前公开修订，并记录实际 `entry_id`、`revision_id` 和 `policy_version`。关闭许可只阻止之后的新导入，不删除已经合法创建的私人副本，也不授予副本再次公开发布或在平台外传播的权利。

导入确认会显示目标 Workspace、公开版本和署名。请求提交时，服务端仍以当前会话中的 Workspace 作为真实目标，并把界面确认的 Workspace ID 仅作为一致性前置条件；另一标签页切换空间后，旧确认会以 `409 workspace_changed` 拒绝，不会写入新空间。提交前还会重新验证词条仍已发布、目标仍是当前公开且未隔离的修订、复用许可和政策版本仍有效，以及当前用户仍有目标空间写权限。

成功导入会在目标 Workspace 的 `_inbox` 创建一篇独立 Markdown 正本，生成新的稳定 `Article-ID`，并保留来源 entry、公开 revision、署名和许可版本。副本之后由导入用户独立编辑，公开更新只产生订阅通知，不会自动覆盖私人内容。平台以 Workspace 和 revision 保证导入幂等，并用持久 intent、文件 operation attempt、Article-ID 和当前位置恢复崩溃或重放；重复请求返回已有副本，不会静默覆盖同名文件。原公开来源之后被撤回、下架、隔离或因作者注销而不可用时，私人副本继续保留，只把来源状态显示为不可用。

### 公共索引运维

公共搜索使用平台 SQLite 中的 FTS5 派生投影，只索引允许公开的标题、摘要、正文、公共分类、公共标签和署名，不直接索引投稿原始快照或私人路径。所有公共聚合查询仍会实时校验 entry 状态、当前 revision 和 revision visibility；因此索引刷新失败不会让已撤回、下架或隔离的旧内容重新公开。

`PublicIndexWorker` 是本地补偿工作器，不调用远端模型，并且在 `WIKI_DISABLE_REMOTE_WORKER=1` 时仍会启动。索引任务持久保存在 `public_index_jobs`，瞬时失败按退避策略进入 `retry`，达到上限后进入 `dead`；Admin 可在“公开索引补偿”页查看并重试失败任务。服务重启会回收过期的 `running` lease，单条损坏任务会被隔离，不能终止整个索引工作器。

远端生成/补证/治理 Worker 与广场 AI 预审 Worker 使用同一部署开关。仓库的正式 systemd 单元读取 `/etc/unlimited-wiki.env`，根目录 `.env.example` 是该文件以及外部 Compose 的受支持环境合同：正常服务不得设置 `WIKI_DISABLE_REMOTE_WORKER`（代码默认启用），并明确允许 `generate,supplement,governance`。当 Worker 被关闭或某个 task kind 被排除时，服务端会在业务写入和幂等记录之前拒绝新的远端任务或投稿，不再制造永久排队；既有队列保持原样，等待运维决策。

`WIKI_DISABLE_REMOTE_WORKER=1` 只用于恢复后的隔离冒烟或明确的维护窗口。重新启用会立即开始消费所有 Workspace 的 `queued` 生成/补证/治理任务，以及平台 `ai_queued` 投稿，并可能产生网页抓取、第三方模型调用、费用和数据外发。启用前必须先只读检查 `/readyz` 的 `capabilities` 和各 Workspace `/api/status`，盘点两类队列，再由有权限的运维人员决定保留、取消或恢复处理；代码发布不会自动重放、取消或改写历史任务。`/healthz` 仅表示进程存活；存在当前配置无法消费的 queued 工作时，`/readyz` 返回 `503 not_ready`。

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

`WIKI_PUBLIC_ORIGIN` 必须是单一、规范的 HTTPS Origin，不能带路径、查询参数或凭据。LAN 模式会强制使用 `__Host-wiki_session`、`Secure`、`HttpOnly` 和 `SameSite=Strict` Cookie，并启用 HSTS；Host、Origin 或代理协议不匹配时请求会被拒绝。`WIKI_REGISTRATION_MODE=bootstrap` 只允许无邀请创建第一个管理员；初始化完成后，Admin 可在“审核后台 -> 账号邀请”生成绑定邮箱、限时且只能使用一次的账号注册链接，无需修改环境变量或重启服务。链接只显示一次，服务端仅保存令牌哈希。

首位管理员的账号、个人空间、初始会话、恢复码哈希和注册审计在同一个平台事务中提交。全新部署没有 legacy `wiki/`、`raw/` 或 `.wiki-state/` 数据时，注册不会写项目根；只有检测到 legacy 数据时才使用 `.runtime/legacy-migration.lock` 串行迁移。迁移失败不会把已经成功的注册伪装成 HTTP 失败：注册仍返回会话和一次性恢复码，并将迁移状态标为 `retry_required`。文件发布前失败会回滚；文件已原子发布但数据库收尾失败时，保留 `prepared` manifest，下次调用校验目标与备份的完整文件集合和哈希后，将迁移记录与成功审计在同一事务中收敛为 `committed`。设置页可在再次验证当前密码后轮换自己的 24 小时恢复码；轮换会立即撤销旧码，明文新码只显示一次且不会写入数据库或幂等响应缓存。

部署机 CLI 仍可作为无浏览器运维备用：

```bash
python3 account_invites.py create --project-root . --email member@example.com --hours 72
```

通过受控渠道把链接或令牌交给对应用户。受邀者完成注册后只获得自己的个人空间；如需协作，团队 Owner 再在团队空间中邀请这个已有账号。`invite` 模式同样只接受有效账号邀请；`closed` 会连邀请注册一起关闭。当前没有企业 SSO 或邮件发送开户流程。

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

备份会先取得实例锁、checkpoint 并检查所有 SQLite 数据库，再生成 manifest v2：普通文件记录大小与 SHA-256，目录也逐项记录，因此空 Workspace、`wiki/`、`raw/` 和空分类目录都属于受校验的数据。SQLite 除 `integrity_check` 与外键检查外，还必须符合最早受支持的平台/Workspace 应用表、列、关键主键/全局唯一约束和身份字段类型签名；任意合法 SQLite 文件不能冒充应用数据库。平台库还会拒绝持久 trigger 和指向 `sessions` 的额外外键，并在内存副本中预演会话撤销，确保验证通过的备份可执行恢复。当前 v2 模型配置采用绑定 Workspace 和具体字段的 AES-GCM 密文；备份会使用随包 `master.key` 逐条验证 Base URL/API Key 密文，损坏、跨 Workspace 复制、字段互换或错误主密钥都会拒绝备份和恢复。历史 v1 密文的两个字段共用认证域，只兼容能够唯一判定的配置；若旧 API Key 本身也呈 HTTP(S) endpoint 形态，系统无法证明两列未互换，会保留 v1 密文并按“未配置”处理，同时拒绝该实例备份。Workspace 管理员必须在模型设置中显式重新输入完整 Base URL、API Key 和模型名，保存为字段绑定的 v2 密文后再生成备份；仅重启或升级不会自动洗白这类歧义配置。尚未初始化的 Workspace 可以没有 `.wiki-state/`，或保留一个空目录；一旦该目录中出现 WAL、锁文件或其他状态产物，`state.sqlite3` 就必须存在且通过检查。`spaces/` 的一级目录必须与平台数据库要求保留的 Workspace 完全一致：软删除团队空间仍需保留，已注销个人空间的残留根或无数据库归属的 orphan 根会让备份失败，运维人员必须核对后隔离或安全清理，工具不会自动删除。已发布备份不允许包含固定数据库的 WAL/SHM；restore 会先验证原包，再完整复制到私有 staging 并复验，不会通过过滤异常内容“修复”坏包。备份目录应位于非 Web 根目录、权限为 `0700` 的加密磁盘；TLS 私钥和 `/etc/unlimited-wiki.env` 需通过公司的秘密备份流程另行保管。定时单元调用 `deploy/offline-backup.sh`：它只会重启脚本实际停止的服务，重启失败会让备份 unit 失败。备份保留清理由运维平台完成，不会自动删除唯一副本。

Square V2 数据随平台 SQLite 一并备份。备份校验会认证公共 FTS5 虚拟表的模块、字段顺序和 tokenizer，验证 search generation、索引任务状态与规范 UTC 时间，并检查公共分类当前 slug 与历史 redirect 的统一命名空间以及公共分类/标签规范化名称唯一索引；普通表不能冒充 FTS5，损坏任务、重复规范名或会劫持旧分类深链的冲突都不能进入可恢复备份。投稿提案和 Admin 决议作为 submission 事实保留；历史公开 revision 冻结发布时的公共分类与标签显示，后续重命名或合并不会改写不可变 revision 快照和哈希。旧 revision 在本功能上线前没有冻结显示字段，只能以迁移时的当前公共名称作为基线回填。

恢复必须在停服状态下进行，并且目标不能已有 `.platform/` 或 `spaces/`。先把旧数据目录移动到隔离位置，再执行：

```bash
python3 backup_restore.py verify /var/backups/unlimited-wiki/wiki-20260817T033000Z
sudo python3 backup_restore.py restore /var/backups/unlimited-wiki/wiki-20260817T033000Z \
  --project-root . --owner unlimited-wiki
sudo -u unlimited-wiki WIKI_DISABLE_REMOTE_WORKER=1 python3 serve.py
```

备份、恢复和服务进程共用 `.runtime/instance.lock`；备份取得锁后若发现 `.restore/` 会拒绝运行，不能从半恢复实例生成灾备副本。高权限恢复要求项目根由 root 管理且不能被服务账号、组或其他用户写入；恢复的 journal、staging 和待发布副本只放在恢复进程拥有的项目根 `.restore/`（`0700`）中，不放入服务账号可写的 `.runtime/`。恢复会校验 manifest、主密钥以及固定位置的平台/Workspace SQLite 数据库，撤销备份中的浏览器会话，并在未发布副本上通过 fd 锚定、不跟随符号链接、子项优先而根目录最后的方式应用 `--owner`；journal 同时绑定 owner 名称和解析后的 UID/GID，续做时任一项变化都会拒绝。再次校验后才原子发布 `.platform/` 与 `spaces/`。最终数据发布完成后才把稳定的 `.runtime/` 和 `instance.lock` 交给服务账号，再原子将 `.restore/` 标记为 `.restore-complete-*` 并清理。`.restore/` 表示恢复未完成并阻断服务和备份；`.restore-complete-*` 仅表示数据与属主均已发布、最后的清理被中断，不阻断服务或新备份，但目录仍含敏感副本，保持 root 私有且由运维在确认不存在 `.restore/` 后清除。两类目录均已从 Git 排除。复制、校验或属主交接失败会保留私有 journal，必须使用同一备份、owner 名称和 UID/GID 重试。完成只读冒烟检查后停止临时进程；确认 `/readyz` 不再有不可消费队列，并完成模型调用、费用和数据外发授权后，移除临时 `WIKI_DISABLE_REMOTE_WORKER=1` 覆盖，再通过 systemd 正常启动。至少每季度在隔离目录进行一次恢复演练；没有经过恢复验证的备份不能视为可用。

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

模型不是浏览、编辑、本地检索或分类的必要条件；AI 生成、补证、治理和投稿内容预审使用当前 Workspace 的个人模型配置。投稿预审每次执行时读取该 Workspace 的最新配置，但只向模型发送脱敏后的不可变投稿内容和公共重复候选，不发送私人分类或公共分类提案，并使用平台统一、版本化的审核规则。provider 和 model 会进入审核记录；Base URL 与 API Key 不进入审核输入、持久化报告、日志或作者/Admin 响应。Admin 审核页仍会显示投稿者明确提交的公共分类和标签提案，这是人工审核所需的投稿事实。模型响应按不可信输入处理：标准结果使用 `decision`、`summary` 和 `issues`；兼容模型若改为输出唯一一个值为 `true` 的 `pass`、`needs_revision` 或 `reject` 状态键，平台会确定性归一化，缺少状态或多个状态同时为真仍会拒绝。未知字段会被丢弃，若回显实际 API Key 或 Base URL，整次预审会转为通用失败。断网、坏配置或单个模型失败不会终止全局审核工作器，也不会阻塞原料入箱、正文阅读或人工分类。

## Raw 原料格式

原料箱会保留上传文件的原始字节，文字提取结果只缓存在当前用户空间的 `.wiki-state/extracted/` 中。文档解析和图片 OCR 均在本机完成。上传和预览不会预测分类、创建分类任务或把分类体系发给模型。用户采用 Raw 为种子或新建正本时，可在当前处置页选择或创建分类与标签；暂不选择时写入 `_inbox`。需要生成或补证正文时，相关原文摘录仍会按当前模型设置进入生成流程。

## 即时分类工作流

每个工作区的分类注册表保存在 `wiki/.categories.json`，随私有 Wiki 一起导出；稳定分类 ID、一级目录和 Markdown `Tags` 是事实源。首次打开旧空间时会保留现有一级目录，并为分类和词条补充稳定 ID；未选择主分类的词条保留在 `_inbox`。

1. 编辑、生成、Raw 种子和词条页治理都使用同一个分类/标签选择器；输入与已有名称规范化完全匹配时复用已有对象，没有匹配时必须显式点击创建。
2. 每篇词条只有一个主分类，可有多个标签。分类对应一级目录；标签只写元数据，不创建目录。
3. 选择新分类时，分类记录、目录、词条写入或移动、元数据、相对链接、索引和日志进入同一个文件事务；失败会整体回滚，不留下空目录或孤立记录。
4. 暂不选择主分类时，词条保存在 `_inbox`，之后可从词条页原地分类。
5. 分类重命名、归档和恢复位于侧栏上下文菜单，先预览再提交。归档分类及内容继续可搜索和访问，但不进入默认导航和新词条选择器；非空分类不能直接删除。
6. 旧 `article-classification` 与 `raw-classification-plan` 任务在启动时终结为 `feature_removed`；旧 API、工作台和独立分类管理页面不再提供。
7. “文件对账”只报告应用外的新目录、移动、缺失或元数据冲突，必须由用户选择采纳、恢复或暂缓。

手动新建词条时，标题、Markdown 正文、主分类和标签由用户一次填写。服务端生成路径与稳定 `Article-ID`，并把新分类注册表、分类目录、草稿正文、索引和日志放入同一个排他文件事务；同标题或物理路径冲突不会覆盖既有正本，失败不会留下空目录。已提交事务可按稳定请求摘要重放并返回同一词条。编辑器保留原生 Markdown 作为事实源，格式工具栏只操作 Markdown 选区，桌面显示编辑与安全预览分栏，移动端使用编辑/预览切换。

分类治理和文件对账成功后都会保留 operation ID；路径变更的 StateStore 与公开导入路径投影可在崩溃恢复时按稳定 Article-ID 对账。

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
spaces/<workspace-id>/wiki/_inbox/ 未分类正本
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
| `WIKI_DISABLE_REMOTE_WORKER` | 正常运行时不设置；仅在隔离冒烟或维护窗口设为 `1`，此时不启动远端模型任务工作器，本地 `PublicIndexWorker` 仍运行 | 未设置（Worker 启用） |
| `WIKI_REMOTE_TASK_KINDS` | 限制远端工作器处理的任务类型，逗号分隔 | 全部支持类型 |
| `WIKI_SECURE_COOKIES` | 设为 `1` 时为会话 Cookie 添加 `Secure` | `0` |
| `WEB_ALLOW_FAKE_IP` | 兼容透明代理将域名解析到 `198.18.0.0/15` 的场景 | `0` |
| `WIKI_OCR_LANG` | Tesseract OCR 语言组合 | `chi_sim+eng` |

## 安全边界

- 后端只允许绑定 `127.0.0.1`、`localhost` 或 `::1`。LAN 模式精确校验外部 HTTPS Host、Origin 和受信代理协议，转发头仅在 socket peer 命中明确 CIDR 时使用。
- 回环 TCP 代理假设同一主机上的本地进程属于同一信任域：任何能连接后端端口的本地进程都可能伪造 `X-Forwarded-For`。不要在存在不可信本地账号或容器的主机上使用该模板；此类环境需要改用带文件权限的 Unix socket 或独立网络命名空间后才能上线。
- 多用户写请求使用会话、CSRF 和幂等保护；团队管理操作按账号、会话或当前空间隔离幂等作用域，并与平台数据变更在同一事务提交。密码使用 scrypt 派生。账号邀请创建不把含明文令牌的响应写入幂等缓存；撤销操作不含秘密，可以安全重放。
- 私有 API 每次请求都从服务端会话重新验证当前空间成员关系；客户端提交的 Workspace、Owner 或角色字段不能形成授权。
- 团队空间只允许 Owner 管理成员。Owner 只能邀请已经注册的账号成为 Editor 或 Viewer，并可调整角色、移除成员及把 Owner 转移给现有活跃成员；最后一个 Owner 不能被直接移除或降级。平台 Admin 创建账号邀请不会自动授予任何团队空间权限。个人空间不能邀请成员或转移 Owner。
- 当前空间按会话独立保存，不使用账号级全局切换。成员从当前团队空间被移除后，指向该空间的会话立即失效，不会自动改投其他空间执行原请求。
- 团队空间生命周期只允许当前活跃 Owner 操作：`active -> suspended -> active`，或 `suspended -> deleted`。个人空间不能停用、删除或退出；平台管理员没有私有空间旁路权限。停用、恢复、软删除、退出、会话解绑、邀请撤销、通知与审计在平台事务中提交。
- 当前空间不可用时，身份会话与 Workspace 上下文分离：认证仍有效，私有 Wiki API 返回 `409 workspace_selection_required`，只有空间列表、显式切换、Admin 账号邀请和其他账号级操作可继续。前端在确认服务端空间前会卸载旧 Wiki 并清除租户缓存。
- 停用空间的 `staged`、`queued` 和 `running` 私有任务转为 `paused`，恢复后按原阶段重新激活；软删除后所有未完成私有任务以 `workspace_deleted` 终结。广场 AI 预审在停用时转为可重试的通用失败，在删除时终结；排队投稿不再领取，运行 attempt 立即完成并通过 attempt fencing 丢弃迟到结果。审核调度与 Workspace 生命周期按空间串行；停用会等待已开始的外部请求及其结果收敛，返回后不会再发起新请求，失效 attempt 的结果不会进入投稿、审核记录或公开 revision。
- 平台记录的 Workspace 状态是生命周期真相来源。生命周期请求重放、服务启动和空间服务首次获取都会按当前状态幂等对账任务投影；旧的停用请求不会覆盖后来完成的恢复，平台提交后中断也会在重启或重试时修复。
- 生成、补证、治理和分类任务记录服务端解析出的发起者；worker 在领取和最终写入前重新检查其写权限。发起者被撤权后任务以 `auth_revoked` 结束，不提交远端结果。
- 私有投稿、发布状态和撤回操作同时绑定账号与当前空间；同一账号在不同空间中的同名文章不会共享投稿或公开版本状态。
- 个人组织和个人空间不可转移；个人空间仍有其他活跃成员时，必须先解除成员关系，账号注销会返回 `422` 且不产生任何变更。注销活跃或停用团队空间的 Owner 时，系统按现有 Owner、Editor、Viewer 的顺序稳定选择空间接任者，并按 Owner、Admin、Member 的顺序独立选择组织接任者；转移、审计和注销在同一事务内完成。任一仍可治理的组织或团队空间没有接任者时也返回 `422`，整次注销不产生变更。仅包含软删除空间的团队组织不再要求接任，终态空间的磁盘、模型、投稿和公开修订继续保留；仅未共享的个人空间会清理本地数据。
- Markdown 经净化后渲染，并设置 CSP、`nosniff` 和严格 Referrer Policy。
- 网页补证拒绝本地、私网、带凭据 URL 和 HTTPS 降级跳转。
- 广场公共 Markdown、结构化来源和纠错证据只允许无凭据的 `http`/`https` 公网目标；拒绝 localhost、本地或私网域名、非全局 IP、保留地址及浏览器可解释为这些地址的数字主机。平台不会抓取、预取或跟随来源 URL；不安全正文链接不可点击，HTTP 外链会明确标记为非加密连接。
- 公共 API 使用字段白名单，不返回私人 Workspace、文件路径、Raw 来源、内部用户 ID 或 AI 审核解释。公共分类、标签和专题只引用当前公开且未隔离的 revision；作者撤回、Admin 下架、账号注销和版本隔离后，搜索、版本、主页、专题、关系与缓存均不得继续提供被隐藏正文。
- 本机开发默认开放注册；LAN 模式禁止开放注册，只允许首位管理员 bootstrap、邮箱绑定的单次邀请或完全关闭。bootstrap 的首位用户判定、邀请令牌消费、账号关系、初始会话、恢复码哈希与注册审计都在同一 SQLite 写事务中完成；已关闭的 bootstrap 返回明确的 `registration_closed` 冲突。
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
| `square_v2.py` | 公共分类与策展、搜索投影、版本关系、复用导入、订阅、纠错和公开索引补偿 |
| `security.py`、`websearch.py` | 出站网络限制与远端补证 |
| `viewer/` | React 前端 |

## 贡献与文档维护

所有改动通过功能分支和 Pull Request 合并，不直接推送 `main`。任何改变功能、安装方式、配置、安全边界、数据布局或开发流程的提交，都必须在同一个 Pull Request 中同步维护本 README。

提交前请确认暂存区不包含 `.env`、API Key、数据库、Wiki/Raw 内容、运行状态或其他项目文档。

## License

[MIT](LICENSE)
