# Manxue AI

## 本机 Codex / ChatGPT 登录版

这个版本使用本机官方 Codex CLI，复用你已经完成的 ChatGPT 登录状态，不需要在网页后台填写 API Key。后台可读取账号模型、切换思考强度并按间隔生成；每个任务使用后台保存的提示词，默认生成一个包含 SVG 鹈鹕动画的 HTML，结果会保存到画廊。

Windows 自动优先使用 `%LOCALAPPDATA%\OpenAI\Codex\bin` 中桌面版安装的 CLI，未找到时再查找 PATH 中的 CLI / npm 包。可用 `PELICAN_CODEX_BIN` 显式指定程序。模型列表与生成共用此选择，避免从外部终端启动时误用另一套 CLI 而显示较少的模型；切换 CLI 后模型缓存自动失效。模型与思考强度以当前 CLI 返回的目录为准。

每次生成都使用 Codex CLI 的独立 `--ephemeral` 会话，并从临时工作目录开始；任务完成或失败后临时目录自动删除，不复用上一轮上下文。公开页面显示已保存的提示词，每次请求保存实际发送的提示词快照，调用层不会增加额外提示词前缀。修改提示词只影响后续请求；访客请求在提交时保存快照。后台每 5 秒通过新的 CLI 进程调用 `account/read`，显示当前个人账号的脱敏邮箱与核对时间；个人指纹由 CLI 返回的邮箱生成，不再把可能多人共享的 `tokens.account_id` 工作区标识当作个人账号。读取失败时不沿用旧身份或旧模型。请求开始、实际调用前及结束后核对身份；发现中途换号时拒绝将结果登记到原账号。CLI 登录仍由 Codex 自己管理，程序不复制或保存登录令牌。

隔离范围是新进程、新会话和独立临时工作目录。每次调用均忽略用户配置和项目规则、禁用项目说明加载，使用只读沙盒；本机 Codex 登录身份与 CLI 安装共享。它不是为每次请求创建独立虚拟机。`--ephemeral` 不持久化 CLI 会话文件，本站仍保存请求结果供画廊查看。

先确认 CLI 已登录：

```powershell
codex login
codex login status
```

首次安装依赖（Windows PowerShell）：

```powershell
git clone --branch codex/pelican-finish-interval-account-filter https://github.com/a2856479068/pelecanus.git
cd pelecanus
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r app\requirements.txt
```

之后双击 `start.bat` 即可启动；也可以在 PowerShell 中运行 `.\start.bat` 或 `.\start.ps1`。启动脚本优先使用项目的 `.venv`，否则使用系统 Python。打开 <http://127.0.0.1:8765/admin>，首次设置管理密码；公开画廊地址是 <http://127.0.0.1:8765/>。默认生成鹈鹕 SVG 动画 HTML，自动检测关闭，避免意外消耗额度。

Windows 可运行 `build-exe.ps1` 构建单文件 `dist/PelicanWatch.exe`，程序图标使用网站鹈鹕 SVG favicon 对应的多尺寸 ICO。需要先安装 PyInstaller（`python -m pip install pyinstaller`），并单独安装、登录 Codex CLI；EXE 不包含 Codex CLI、登录凭据、数据库或图片。把 EXE 放在项目的 `dist` 目录运行时，会沿用 `app/data/monitor.sqlite3` 中的管理员密码和节点配置。EXE 单独放到其他目录时，数据存于 `%LOCALAPPDATA%\PelicanWatch\data`；首次使用需设置管理密码。运行后会打开浏览器，控制台窗口关闭即停止服务。

下载文件统一使用 `模型简称-MMDD-HHMMSS.svg`，例如 `gpt6-sol-0924-164501.svg`；时间与页面一致，使用 UTC+8 的请求开始时间。HTML 使用相同名称，仅扩展名为 `.html`；访客结果也使用同一规则。图片没有单独的文件夹：本机保存在 `app/data/monitor.sqlite3`，Docker 保存在持久卷的 `/data/monitor.sqlite3`。SVG 位于 `image_library` 图片库（旧版 `runs.svg` 也可能保留副本），生成的 HTML 与原始输出位于 `runs` 表。删除作品会删除这些内容与对应记录；`run_outcomes` 仅保留请求编号、时间和结果用于计算最近 24 小时成功率，手动取消不计入成功率分母。迁移前已经删除且没有备份的请求结果无法追溯。可选 Docker 方式仍使用原项目的 `docker compose up -d --build`；容器无法直接复用宿主机的 ChatGPT 登录缓存，适合继续配置兼容 API 节点。

自托管的模型接口观察站，通过固定的鹈鹕 SVG 任务记录模型的请求结果与生成内容。支持 OpenAI 兼容的 Responses 和 Chat Completions 协议，提供公开画廊、历史时间线、后台节点管理和访客独立测试。

## 功能

- **单个模型 / 循环组**：单个模式每轮执行所选模型；循环组模式逐组执行启用组内的组合。每行明确绑定一个模型与思考强度，组 ID 独立关联成员和生成记录，相同组合可以属于不同组。整组保存使用数据库事务；停组会移除等待中的成员，正在生成的请求会完成。
- **单图生成**：每次从独立新会话开始，使用后台已保存的提示词生成一个作品；默认是 SVG 鹈鹕骑行 HTML 动画。仅请求成功或失败会影响状态，画面由用户在画廊人工判断。
- **手动开始 / 停止**：画廊和后台均可开始或停止测试。开始按已保存的单个模型或循环组立即运行；每张请求结束后（成功或失败）等待完整设定间隔，再执行下一张，循环组内的每个组合也遵守该间隔。生成中不预估下次时间，停止取消当前任务、清空队列并暂停定时测试。
- **秒数与单次生成**：后台间隔单位为秒，范围 1–86400，默认 1800 秒；旧版分钟设置自动换算为秒，实际时长不变。暂停循环后可在任一页面“生成一次”，完成后保持暂停；循环组模式仅执行第一个启用组合。
- **实时状态与耗时**：前后台共用服务端状态，每次读取完成后约一秒再次读取，操作后通知其他标签页立即重新读取。API 与页面使用 `no-store`，不从浏览器存储恢复运行状态。耗时、倒计时基于服务端时间与请求真实时间戳；连接失败显示待同步，不继续推算。登录会话仍可记住 30 天。时间线的 30 分钟表示统计分桶，不是生成间隔。
- **可编辑提示词**：后台保存 1–4000 字符的提示词，保留原有空格和换行。每条记录保留实际发送内容，历史记录不会随设置变化。
- **完整预览**：HTML 动画按完整画面等比缩放，缩略图与详情均保留全景；详情可另开原尺寸 HTML。预览适配不修改保存的原始输出或下载文件。
- **SVG 下载**：画廊卡片、详情及后台记录均可下载已有 SVG。导出保留可安全内嵌的样式和原始内联动画脚本，并按 XML 格式转义。脚本引用的页面按钮、输入框和容器保存在不可见的 XHTML 区域，避免因为缺失控件而中断动画；祖先容器的暂停等 class 状态会同步到画面。带 viewBox 的导出使用窗口尺寸显示，避免 HTML 的 `height:auto` 样式裁掉画面。预览用的 SVG 仍不含脚本，原始输出不改写。脚本动画需用 Chrome / Edge 等浏览器单独打开 SVG；作为普通图片或 `<img>` 使用时脚本不执行。外部脚本不打包，依赖完整页面布局、HTML document API 或外部库的作品仍可能需要下载完整 HTML。
- **收藏保护**：管理登录后可收藏或取消收藏，支持“仅收藏 / 未收藏”筛选。收藏持久保存在数据库中；单条删除、批量删除及初始化数据库都会检查保护状态，必须先取消收藏。全选跳过收藏和运行中的记录；若旧选择包含新收藏的记录，该批次整体拒绝删除，统计不受收藏操作影响。
- **后台账号标记**：每个新 Codex 请求保存个人账号指纹和脱敏邮箱，切换账号不会改写已有记录。旧版只有工作区指纹的记录标为“旧工作区（未区分个人账号）”，无法追溯具体用户，不自动归给当前账号；更早记录显示“账号未记录”，API 节点显示“API 节点”。账号标记仅向已登录管理员返回。画廊和后台均支持按账号筛选展示与跨页全选删除；切换筛选会清空旧选择，删除接口再次核对账号范围。后台还支持模型、状态、循环组、时间与关键词筛选，每页 20 条。
- **历史记录**：查看完整 HTML 动画、提取的 SVG、原始输出、本轮实际提示词和请求状态；支持按循环组查看。HTML 在独立沙盒中预览，保留内联样式与动画脚本，禁止网络访问。
- **访客生成**：使用自己的接口独立生成一张 SVG，支持排队与进度查询。
- **后台管理**：后台统一登录，画廊与后台共享 30 天登录，刷新、重新打开浏览器或重启服务后无需重复输入密码。浏览器只保留 HttpOnly 会话 Cookie，数据库保存会话令牌的哈希；退出登录立即使当前会话失效。
- **独立记录页**：后台设置页只保留生成配置与控制，不加载历史列表。右上角“生成记录”在新标签页打开 `/admin/records`，保留统计、筛选、收藏、多选删除和初始化数据库；共用后台登录，直接访问时会在登录后返回记录页。
- **批量删除**：公开画廊支持逐项勾选、全选本页、全选筛选结果（跨全部页面）、同一筛选下跨页保留选择和清空选择。全选跳过运行中的记录，批量删除自动分批提交，失败时保留未删除的选择。删除前统一确认，关联图片库记录同步删除；未登录会跳转统一登录并返回确认。
- **初始化生成数据**：后台“初始化数据库”会清空当前实例数据库中的作品、原始输出、访客结果和全部请求统计，并暂停定时测试；管理员密码、30 天登录会话、节点和循环组设置保留。存在收藏时须先取消全部收藏；正在生成或有访客请求时须先等待任务结束。操作只作用于当前实例的数据库，不会清理另存的备份文件。

## 托管平台一键部署

仓库根目录提供标准 `Dockerfile`，通常可被 Zeabur 以及其他支持 Dockerfile
的托管平台直接识别和构建。将 GitHub 仓库导入平台后，无需设置构建命令或启动命令。

部署服务时请使用以下配置：

| 配置项 | 值 |
| --- | --- |
| 构建入口 | 仓库根目录 `Dockerfile` |
| 容器 HTTP 端口 | `8765` |
| 持久化目录 | `/data` |
| 环境变量 | `ADMIN_TOKEN`（仅首次初始化时必填，6–256 字符） |
| 实例数 | `1` |

容器入口会先以 root 修复 `/data` 持久卷的属主，再以非特权用户运行服务；
请勿在平台设置中强制覆盖容器入口用户。

平台若自动注入 `PORT`，应用会优先使用该值并监听 `0.0.0.0`。必须为
`/data` 添加私有持久化卷；其中包含 SQLite 数据库和 API Key。不要启用多副本，
因为同一数据库目录只支持一个服务进程。

### Zeabur

仓库提供 [`zeabur/template.yaml`](zeabur/template.yaml)，已定义好服务来源、持久卷、端口、健康检查和环境变量，一条命令即可完成部署：

```sh
npx zeabur@latest auth login
npx zeabur@latest template deploy -f zeabur/template.yaml
```

命令会提示填写 `ADMIN_TOKEN`（管理密码，6–256 字符）和 `PUBLIC_DOMAIN`（访问域名）。部署完成后打开 `https://<你的域名>/admin` 登录，再按[配置节点](#配置节点)完成设置。Zeabur 在入口处终止 TLS，不需要另外架反向代理。

想让别人也能一键部署，可执行 `npx zeabur@latest template create -f zeabur/template.yaml` 发布模板，再到 [Zeabur Dashboard](https://dash.zeabur.com/account) 的 Template 页签复制部署按钮代码。按钮只能由模板作者生成。

也可以手动导入：

1. 在 Zeabur 新建项目，选择从 GitHub 导入本仓库。
2. 选择自动检测到的 Dockerfile 服务；HTTP 端口设为 `8765`（若界面自动识别则保持默认）。
3. 在 Variables 添加 `ADMIN_TOKEN`，填入首次后台初始化用的管理密码。
4. 添加 Persistent Volume，挂载路径填写 `/data`，并将服务副本数保持为 `1`。
5. 部署完成后，打开平台分配的域名并访问 `/admin` 配置 API 节点。

`ADMIN_TOKEN` 只会在数据卷尚未初始化时生效；修改它不会重置既有后台密码。Zeabur 上也无法通过页面重设密码（该通道仅对本机回环地址开放），忘记密码只能删除数据卷重来，历史记录会一并丢失。

挂载持久卷后 Zeabur 采用 Recreate 策略，重新部署时先停旧实例再起新实例，期间会有短暂停机。`compose.yaml` 里的 `mem_limit`、`cpus`、`pids_limit` 和日志轮转在模板格式中没有对应字段，需要在控制台的服务设置里单独调整；建议把内存放到 1 GB 以上。

### 其他 Docker 平台

Railway、Render、Fly.io、Coolify、Northflank 等 Dockerfile 平台可直接连接本仓库，
并按上表设置端口、环境变量和持久化卷。若平台只支持部署既有镜像，可先构建并推送：

```sh
docker build -t your-registry/manxue-ai:latest .
docker push your-registry/manxue-ai:latest
```

运行镜像时暴露服务端口并挂载 `/data`：

```sh
docker run -d \
  --name manxue-ai \
  --restart unless-stopped \
  -p 8765:8765 \
  -e ADMIN_TOKEN='替换为首次初始化管理密码' \
  -v manxue-ai-data:/data \
  your-registry/manxue-ai:latest
```

## Docker 部署

需要 Docker Engine / Docker Desktop 和 Docker Compose。以下 Compose 配置仅供本机使用，
其端口会绑定到 `127.0.0.1`；托管平台请使用上方的根目录 `Dockerfile`。Windows 用户请使用 Linux 容器。

```sh
git clone --branch codex/pelican-finish-interval-account-filter https://github.com/a2856479068/pelecanus.git
cd pelecanus
```

首次启动前必须通过 `ADMIN_TOKEN` 设置 6–256 字符的管理密码；未设置时服务不会完成初始化。

Linux / macOS（Bash）：

```bash
read -r -s -p "Initial admin password: " ADMIN_TOKEN
echo
export ADMIN_TOKEN
docker compose up -d --build
unset ADMIN_TOKEN
```

Windows（PowerShell 7）：

```powershell
$env:ADMIN_TOKEN = Read-Host 'Initial admin password' -MaskInput
docker compose up -d --build
Remove-Item Env:ADMIN_TOKEN
```

公开页面：<http://127.0.0.1:18765/>；后台：<http://127.0.0.1:18765/admin>。

### 配置节点

1. 登录后台，刷新当前账号的模型与强度组合定义。
2. 选择“单个模型”，设置模型和强度；或选择“循环组”，给每组命名、逐行添加组合并保存。
3. 设置间隔，保存生成方式和定时开关。单个模式还可立即生成一次。

结果状态只反映模型请求是否成功；生成内容的画面效果请在画廊人工判断。默认生成间隔为 1800 秒（30 分钟），自动检测初始关闭。

### 数据与运行维护

容器数据保存在 Compose 命名卷中，重建容器不会清空记录。`docker compose down -v` 会删除数据卷。

`ADMIN_TOKEN` 仅用于首次初始化，之后不会覆盖已保存的密码。首次配置完成并清除该环境变量后，可执行 `docker compose up -d --force-recreate`，从容器配置中移除初始密码。

对公网开放时，请在主机上配置 HTTPS 反向代理，转发至 `127.0.0.1:18765`。不要启用请求正文日志。

## 本地运行

支持 Windows、Linux 和 macOS，要求 Python 3.11+。Windows 可直接运行上面的 PowerShell 命令；Linux / macOS 使用下面的命令。

在仓库根目录执行：

```sh
cd app
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt  # 当前无第三方运行依赖
python server.py
```

访问 <http://127.0.0.1:8765/admin> 初始化管理密码，然后配置节点。本地数据默认写入 `app/data/`。

## 环境变量

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 监听地址；容器内为 `0.0.0.0` |
| `PORT` | `8765` | 服务端口 |
| `DATA_DIR` | `app/data`（容器内 `/data`） | 数据库与实例锁目录 |
| `ADMIN_TOKEN` | 空 | 仅首次初始化管理密码，不是 API Key 或会话令牌 |

## 默认提示词（后台可修改）

```text
创建一个HTML，内容是用SVG绘制一个鹈鹕骑自行车的2D动画，你不能进行任何测试，不能调用skills，不能网络检索，不能调用子智能体，直接生成
```

公开页展示当前原文，详情页展示该轮保存的实际提示词。历史记录保留当时提示词，不会被新设置改写。

## 检测说明

每次请求使用后台已保存的提示词，并通过 Codex CLI `--ephemeral` 启动独立会话，默认生成一个含 SVG 的 HTML 动画。状态只表示请求是否成功，不判断 SVG 格式、动画效果或画面质量；详情页保留安全预览、原始输出和实际提示词，画面请在画廊人工判断。请求成功不证明模型身份，也不代表模型的整体能力。

生成与失败重试会消耗对应 API 额度。访客生成使用访客提交的接口配置，不使用后台节点凭据。

服务需要持续运行才能执行定时任务；同一数据目录仅支持一个服务进程。

定时生成开启后，单个模式每轮执行所选模型一次；循环组模式按组建立顺序、组内行顺序执行所有启用组合。每个组合单独启动全新会话。修改中的组整体保存后生效，其他组的相同组合不会被合并。旧版非当前且启用的组合会迁入“默认循环组”。

## 隐私与安全

后台 API Key 保存在数据目录的 SQLite 数据库中，采用文件权限保护，**没有数据库加密**。管理密码保存为带随机盐的 PBKDF2-SHA256 哈希。不要发布数据库、数据卷、日志、备份或真实环境配置。

访客完整 API 地址和密钥仅在任务排队及执行期间保留在内存中；公开结果包含脱敏域名、模型、原始输出和安全 SVG 预览。访客结果会对其他访问者可见，不要向测试提交机密内容。完成结果保留一小时；重启后未完成任务标记中断。

## 友情链接

- [Linux DO](https://linux.do/)

## 许可证

[Apache License 2.0](LICENSE)
