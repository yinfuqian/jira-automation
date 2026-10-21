# jira-automation（故障分析工具）

故障分析工具用于综合分析日志、错误截图、业务页面截图、代码仓库和上下游模块信息。系统会先提取结构化错误证据，再使用 `gpt-5.6-sol` 进行高强度深度推理，输出问题明细、可能原因、代码位置、修复建议和相关模块影响。

## 主要能力

- 日志文件、压缩日志和多张图片统一上传分析。
- 图片识别统一由多模态模型逐张完成（本地 PaddleOCR 通道默认停用，可用 `LOCAL_OCR_ENABLED=true` 显式启用），`log_image` 与 `business_image` 都参与多模态分析。
- 多张图片逐张保留，不相互覆盖；最终结果合并重复问题，但保留不同错误明细。
- 结合主模块、相关模块与指定 Git Tag 代码进行故障定位；故障分析不依赖 `master`、`feature/*` 等分支。
- `gpt-5.6-sol` 默认启用 `OPENAI_REASONING_EFFORT=high` 深度推理。
- 用户由管理员维护 `users.csv`，应用热加载新增、禁用和立即下线状态。
- 除登录和账号申请外，服务接口必须携带有效登录令牌。
- 用户操作写入数据库审计表，便于按操作人和请求编号追踪。
- 内置 Codex Agent 技能执行接口：提交 Jira 链接与 `skill_id`，由服务端跑技能完成需求准入检查，写回 Jira 评论、按需流转状态，并把 OpenSpec 评审报告上传成需求单附件。
- Docker Compose 提供数据库迁移、API、Worker、前端和 OCR 运行环境，并可通过 `local-deps` Profile 启动本地 MySQL、Redis。

## 服务架构

| 服务 | 职责 | 默认端口 |
| --- | --- | --- |
| `frontend` | Nginx 静态页面和 `/api` 反向代理 | `8080` |
| `api` | Flask API、认证、上传、Git 和分析任务提交 | `5000` |
| `worker` | Celery 异步深度分析、图片识别与技能（Codex Agent）执行 | 无宿主机端口 |
| `migrate` | 创建数据库并执行 `flask db upgrade` | 一次性容器 |
| `mysql` | 业务数据、知识库和用户操作日志 | `3306` |
| `redis` | 登录会话、限流状态、Celery Broker 和结果存储 | `6379` |

## Docker 快速启动

环境要求：Docker Desktop 或 Docker Engine，且支持 Docker Compose v2。

当前 dev 与 master 分别使用 `docker-compose-dev.yaml` 和 `docker-compose-prod.yaml`，两份文件内容相同；MySQL、Redis 使用外部地址：

```powershell
Copy-Item .env.example .env
docker compose --env-file .env -f docker-compose-dev.yaml up -d
docker compose -f docker-compose-dev.yaml ps -a
```

开发环境重启 API 与 Celery Worker（不会重启数据库，也不会删除数据）：

```bash
bash restart-dev.sh
```

Master 生产环境使用 `docker-compose-prod.yaml` 与 `.env.production`：

```bash
bash restart-master.sh
```

两个脚本都支持传入 Compose 服务名，例如 `bash restart-dev.sh api` 或 `bash restart-master.sh worker`。如确需临时覆盖配置文件，可使用 `COMPOSE_FILE`、`ENV_FILE` 环境变量。

首次生产启动前至少修改 `.env` 中的以下内容：

```env
MYSQL_PASSWORD=请替换
MYSQL_ROOT_PASSWORD=请替换
REDIS_PASSWORD=请替换
OPENAI_KEY=请填写
OPENAI_URL=https://你的兼容接口地址
```

浏览器访问：`http://127.0.0.1:8080`。

停止服务但保留数据：

```powershell
docker compose -f docker-compose-dev.yaml down
```

仅在明确要删除数据库、Redis、OCR 模型和上传数据时才执行：

```powershell
docker compose -f docker-compose-dev.yaml down -v
```

## 用户登录与 users.csv

默认示例账号位于根目录 `users.csv`：

```csv
username,password,status
admin,admin123,1
```

生产环境必须修改默认密码。CSV 状态含义：

- `1`：启用。已登录会话持续有效，直到用户退出或会话过期。
- `0`：禁用。禁止新登录，已登录会话按当前会话生命周期处理。
- `2`：立即下线。禁止登录，并使现有会话在下一次接口调用时失效。

文件会按修改时间热加载，不需要重启 API。用户名和明文密码只保存在管理员维护的 CSV 中；数据库不会保存登录密码，操作日志也不会记录密码。Redis 仅保存会话和限流所需状态。

登录与账号申请接口允许匿名调用；其他业务接口必须携带登录后获得的 Bearer Token。匿名用户可以打开前端页面，但不能执行上传、分析、Git 同步等功能。

账号申请当前默认使用 Mock：

```env
ACCOUNT_REQUEST_PROVIDER=mock
```

后续接入第三方接口时配置：

```env
ACCOUNT_REQUEST_PROVIDER=http
ACCOUNT_REQUEST_API_URL=https://example.com/account-request
ACCOUNT_REQUEST_API_TOKEN=请填写
ACCOUNT_REQUEST_API_TIMEOUT=10
```

HTTP 提供方会转换为邮件接口需要的 JSON 字段：

```json
{
  "register_account": "申请的用户名",
  "register_user": "申请人姓名",
  "register_pwd": "申请密码"
}
```

接口返回 HTTP 成功且 JSON 中 `code=200` 时才视为发送成功；其他情况统一向客户端提示“账号申请失败，请联系管理员”。

申请内容包含用户名、密码、申请人姓名，以及服务端自动生成的申请编号和时间。调用失败时客户端直接提示联系管理员，不在本地保存申请记录。

## 模型与深度推理

默认配置：

```env
OPENAI_MODEL=gpt-5.6-sol
OPENAI_API_STYLE=chat
OPENAI_REASONING_EFFORT=high
```

兼容 Chat Completions 的接口使用 `reasoning_effort=high`；兼容 Responses 的接口使用 `reasoning.effort=high`。如第三方服务不支持对应参数，需要由接口提供方实现兼容，或在确认风险后调整推理强度配置。

## 图片识别与本地 OCR

图片识别默认由多模态模型逐张完成：日志截图、业务页面截图都直接交给模型，识别结果同时用于上下游链路判断和最终故障推理。这条路径不需要下载 OCR 模型，也不会因为本地 OCR 初始化失败而阻塞分析。

本地 PaddleOCR 通道**默认停用**（`LOCAL_OCR_ENABLED=false`）。仅在明确的离线或成本场景下才建议开启：设为 `true` 后，日志截图会先跑本地 OCR，置信度低于 `OCR_GPT_FALLBACK_CONFIDENCE`（默认 `0.6`）时再回退模型识别。

后端镜像仍然安装 Python 3.10、PaddlePaddle、PaddleOCR、OpenCV 和必要的 Linux 原生库，模型缓存写入 `ocr-models` 命名卷；需要保留这条离线预检能力时可以直接使用。

验证 OCR 运行环境：

```powershell
docker compose -f docker-compose-dev.yaml exec api python verify_runtime.py --ocr
```

关闭本地 OCR 后，若模型识别没有返回可用文字，结果中会明确说明“图片识别模型未返回可用结果”，并保留原图继续多模态分析。

## 数据库迁移

启用 `local-deps` Profile 时，Compose 会先等待本地 MySQL 和 Redis 健康；使用外部依赖时，迁移容器直接连接 `.env` 指定的地址并运行：

```powershell
python init_database.py
flask db upgrade
```

手工执行迁移：

```powershell
docker compose -f docker-compose-dev.yaml run --rm migrate
```

查看当前迁移日志：

```powershell
docker compose -f docker-compose-dev.yaml logs migrate
```

## 健康检查与运行验证

- `GET /health/live`：仅确认 API 进程存活。
- `GET /health/ready`：检查数据库、Redis、`users.csv` 和运行目录。

```powershell
Invoke-WebRequest http://127.0.0.1:5000/health/live
Invoke-WebRequest http://127.0.0.1:5000/health/ready
Invoke-WebRequest http://127.0.0.1:8080/health
docker compose -f docker-compose-dev.yaml exec api python verify_runtime.py --runtime
docker compose -f docker-compose-dev.yaml exec worker celery -A celery_worker.celery_app inspect ping
```

构建期完整自检：

```powershell
docker build --progress=plain -t jira-automation-backend:test backend
docker run --rm jira-automation-backend:test python verify_runtime.py --build
docker run --rm jira-automation-backend:test python verify_runtime.py --ocr
docker build --progress=plain -t jira-automation-frontend:test frontend
docker run --rm jira-automation-frontend:test nginx -t
```

## 本地开发与生产镜像发布

本项目明确区分本地构建与线上运行：

- `docker-compose-dev.yaml` 和 `docker-compose-prod.yaml` 内容完全相同，分别用于 dev 和 master 生产环境；dev 默认读取 `.env`，生产默认读取 `.env.production`。生产服务器不需要也不应该保存源码仓库或完整源码包。镜像仍会包含程序运行所需的应用文件，因此镜像仓库权限必须严格控制；Docker 镜像本身不是加密或防反编译方案。

这两份 Compose 的 `api`、`worker`、`migrate` 统一使用 host 网络，API 直接监听宿主机 `5000`。MySQL、Redis 必须配置为宿主机可访问的真实地址，不能使用 Compose 服务名。前端如单独部署，应把 `/api` 代理到宿主机 `127.0.0.1:5000`。

### 本地开发

使用外部 MySQL、Redis 时：

```powershell
Copy-Item .env.example .env
docker compose --env-file .env -f docker-compose-dev.yaml up -d
```

### 构建并推送发布镜像

在开发机或专用发布机执行，版本号应使用不可变版本，不要使用 `latest`：

```powershell
# 同时构建后端和前端。
powershell -ExecutionPolicy Bypass -File scripts\build_release_images.ps1 all `
  -Version 2026.07.18-1 `
  -Registry registry.example.com `
  -Push

# 只更新后端时只构建后端镜像。
powershell -ExecutionPolicy Bypass -File scripts\build_release_images.ps1 backend `
  -Version 2026.07.18-2 `
  -Registry registry.example.com `
  -Push
```

后端的 `migrate`、`api`、`worker` 共用一个后端镜像；前端使用独立镜像。因此后端代码变化只发布一个后端镜像，前端代码变化只发布一个前端镜像。

### 离线 apt 系统依赖

后端镜像构建阶段需要从 Debian 源安装 `git`、`curl`、`bsdtar`、`unzip` 和 7-Zip 系命令等系统依赖。仓库内
`backend/offline/apt/bookworm-amd64` 已预置这些软件包及其递归依赖的离线 deb 包，构建时自动优先使用本地包，
因此内网没有 apt 源也能构建后端镜像；未命中该目录时仍按 `${APT_MIRROR}` 在线安装。

系统依赖清单（`backend/Dockerfile` 的 `ARG APT_PACKAGES`）变化后需要重新导出：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\export_apt_offline_packages.ps1
```

脚本会下载 deb 包并生成 `MANIFEST.tsv`、`SHA256SUMS`，最后在 `--network none` 的容器内做一次断网安装自检。
ARM 服务器需要额外导出 `-Architecture arm64`。详细说明见 `backend/offline/apt/README.md`。

### 容器内 JDK 与 Maven 编译环境

技能需要在容器内编译 Java 代码，因此后端镜像内置两套 JDK 与一套 Maven，`migrate`、`api`、`worker`
三个容器都能直接用：

- JDK 8：`/opt/java/jdk8`（`JAVA8_HOME`，也是默认 `JAVA_HOME`）
- JDK 21：`/opt/java/jdk21`（`JAVA21_HOME`）
- Maven 3.9.9：`/opt/maven`（`MAVEN_HOME`，已加入 `PATH`）

两个 JDK 都来自 Adoptium(Temurin) 官方 tarball，版本由构建参数 `JDK8_VERSION`、`JDK21_VERSION`、
`MAVEN_VERSION` 固定；`amd64` 与 `arm64` 都支持。同一份代码只能用其中一个 JDK 编译，因此镜像内提供
`jdk` 助手做显式切换，避免编译时悄悄用错版本：

```bash
docker compose -f docker-compose-dev.yaml exec worker sh -c "jdk list"

# 技能执行 JDK 8 或 JDK 21 构建。
docker compose -f docker-compose-dev.yaml exec worker sh -c "cd /tmp/<项目> && jdk 8 mvn -DskipTests package"
docker compose -f docker-compose-dev.yaml exec worker sh -c "cd /tmp/<项目> && jdk 21 mvn -DskipTests package"

# 等价写法，直接指定 JAVA_HOME。
docker compose -f docker-compose-dev.yaml exec worker sh -c "cd /tmp/<项目> && JAVA_HOME=/opt/java/jdk8 mvn -DskipTests package"
```

`MAVEN_OPTS=-Dfile.encoding=UTF-8` 已内置：`javac` 默认跟随 JVM 平台编码，运行环境缺少可用 `LANG` 时会退化成
`US-ASCII`，中文源码报 `unmappable character` 而编译失败（实测清空 `LANG` 后有无这层保险结果相反）。
企业内网可以先用 `--build-arg ADOPTIUM_MIRRORS="..."`、`--build-arg MAVEN_URLS="..."`
把内部制品库排在最前面，下载地址按顺序尝试、命中即用。

选择哪个 JDK 看项目根 `pom.xml` 的 `<java.version>`（或 `maven.compiler.source`）：声明 `21` 的用 `jdk 21`，
声明 `1.8`／`1.7` 的用 `jdk 8`。默认固定在 JDK 8，是因为容器内要编译的业务仓库绝大多数是 Java 8，而且
老项目依赖的 lombok 1.16~1.18 在 JDK 21 下会直接报编译错误——用错版本会把依赖不兼容伪装成代码缺陷。

### Maven 本地仓库

镜像内 Maven 默认没有任何镜像配置，只会访问 Maven Central；业务仓库依赖公司内网私有制品
（如 `com.zhuiyi.prompt:llm-prompt-jar`、`com.cmos.dialogos:dialogos-api`），在容器里必然解析失败，
jira-code 的编译验证与热更新制品构建就只能整段跳过。所以 Compose 给 `worker` 额外挂了两个路径：

- `config/maven/settings.xml` → `/root/.m2/settings.xml`（只读）：把 `mirrorOf=*` 指向内网 Nexus
  聚合仓库 `http://pkg.in.wezhuiyi.com/repository/maven-public/`。该仓库匿名可读，文件里不含任何凭据，
  可以随仓库与部署包分发。
- `./data/m2/repository` → `/root/.m2/repository`：Maven 本地仓库缓存。放在宿主机 `data` 目录下，
  容器重建不会丢，也已被 `.gitignore` 忽略。

本地仓库首次需要预热：把开发机 `C:\Users\86183\.m2\repository` 的内容拷到部署机 `./data/m2/repository`，
业务仓库的私有依赖才能离线命中（整份缓存约 3.8 GB）。只拷私有依赖也能用，但首次构建仍需联网补齐其余依赖。

```powershell
# 开发机执行，目录内容拷到部署机相同相对位置
scp -r C:\Users\86183\.m2\repository user@server:/srv/jira-automation/data/m2/
```

```bash
# 部署机上确认挂载与缓存都已生效
docker compose -f docker-compose-dev.yaml exec worker sh -c "ls -l /root/.m2/settings.xml && ls /root/.m2/repository | head"
```

### 生成精简生产部署包

首次部署或生产 Compose、配置模板发生变化时生成部署包：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package_production_deploy.ps1 -Version 2026.07.18-1
```

生成的 ZIP 只包含：

- `docker-compose-prod.yaml`
- `restart-master.sh`
- `.env.production.example`
- `users.example.csv`
- `README.md`
- `config/maven/settings.xml`

`config/maven/settings.xml` 必须随包分发：生产 Compose 把它挂载为 `/root/.m2/settings.xml`，
缺少该文件时 Docker 会把挂载点建成空目录，Maven 反而读不到镜像配置。它不含任何凭据，可以安全分发。
Maven 本地仓库（`./data/m2/repository`）体积大且不随包发布，需要按「Maven 本地仓库」一节单独拷贝。

部署包不包含 `backend`、`frontend`、`windows-client` 或其他源码目录。

### 生产服务器更新

生产服务器首次部署时，把 `.env.production.example` 复制为 `.env.production`，把 `users.example.csv` 复制为 `config/users.csv`（该目录对应容器内 `/data/`，即 `AUTH_USERS_FILE=/data/users.csv`），填写真实配置后执行：

```bash
docker compose --env-file .env.production -f docker-compose-prod.yaml pull
docker compose --env-file .env.production -f docker-compose-prod.yaml up -d
```

以后正常更新不再复制源码包。只需修改 `.env.production` 中发生变化的镜像版本，然后拉取并重建对应容器：

```bash
# 只更新后端；迁移、API 和 Worker 使用同一镜像一起更新。
docker compose --env-file .env.production -f docker-compose-prod.yaml pull migrate api worker
docker compose --env-file .env.production -f docker-compose-prod.yaml up -d migrate api worker

# 前端镜像不由这份生产 Compose 管理；如单独部署前端，请在其部署目录执行对应更新。
```

离线生产环境可以逐个导出发生变化的镜像：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\export_release_image.ps1 `
  -Image registry.example.com/jira-automation/backend:2026.07.18-2
```

把生成的 `.tar` 和 `.sha256` 文件传到生产服务器，核验后执行 `docker load -i <镜像文件.tar>`，再运行对应的 `up -d` 命令即可。完整操作见 `deploy/README.md`。

## 技能执行接口（Codex Agent）

系统内置技能执行能力：外部系统提交一个 Jira 链接与 `skill_id`，服务端在独立工作目录中启动 Codex CLI，
加载 `skills/<skill_id>/SKILL.md`，由 Agent 按技能定义访问 Jira、执行检查并写回评论。当前内置技能为
`jira-gate-1`（Jira 需求准入检查 G1，产出难度分级与达标结论，把逐项打标记的检查项清单写入需求单评论；
不达标时流转到「评审中」并在评论里 @ 流转人；最后调用 `review-jira-req` 生成 OpenSpec 评审报告，
作为附件上传到需求单，文件名以「宋立志」结尾；交付完成后清理本次任务的工作区中间产物）、
`jira-gate-1-mock`（同一套判定流程的**只读预演版**：不写评论、不回写门禁字段、不上传附件、不流转状态，
也不 @ 流转人；依次调用 `review-jira-req` 与 `review-jira-design` 产出需求质量评审报告与
PRD—原型一致性审核报告，只把评论草稿与这两份报告打成
`<工作目录>/outputs/<KEY>-G1预检包-<时间戳>.zip` 供下载）、
`review-jira-req`（按 OpenSpec 好需求标准评审需求质量，同时读取 Jira 附件并抽取文本/图片证据，
产出 Markdown 报告，对 Jira 只读）、`review-jira-design`（对照定稿 PRD 审核产品原型初稿，
检查需求完整性、PRD—原型一致性、流程与状态覆盖，产出简洁可执行的自包含 HTML 报告；对 Jira 只读，
由 `jira-gate-1-mock` 在需求质量评审之后调用）与 `jira-code`（读取需求单的「开发方案」附件，从方案指定的基线分支拉出
`feature/<单号>` 分支，按方案实现代码并推送到远端；进度与异常都只更新在同一条
Jira 评论上；既不改单据状态，也不上传附件；推送完成后按方案里写的**流水线环境地址**做**热更新**，
热更新处理完再清理本地检出目录（顺序不能反：热更新要读检出目录里的部署文件与构建产物），
流程以 `devops-mcp-invoker` 技能为准）。另外内置 `devops-mcp-invoker`（把自然语言请求转换成对远端
MCP server `pipeline-integration-mcp` 的有序调用）与 `pipeline-adapter`（仓库/模块接入追一 CI/CD 流水线 v3
所需的 `ci.yaml`、`app.json`、Dockerfile、K8S 编排等），两者都是流程与模板文档型技能。

### 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/skill/list` | 列出技能目录中的可用技能 |
| `POST` | `/skill/run` | 提交技能任务，异步执行并立即返回 `task_id` |
| `GET` | `/skill/task/<task_id>` | 查询任务状态、阶段进度与最终结论 |
| `GET` | `/skill/task/<task_id>/artifacts/<文件名>` | 下载任务交付物（`outputs/`、`reports/`，文件名需 URL 编码；带 `?ticket=` 时可直接在浏览器打开） |
| `GET` | `/skill/tasks` | 最近任务列表，支持 `limit`、`skill_id`、`status` |
| `POST` | `/skill/task/<task_id>/cancel` | 取消排队或执行中的任务 |

提交任务（外部系统建议使用独立令牌 `X-API-Token`，避免共享登录会话）：

```bash
curl -X POST http://127.0.0.1:5000/skill/run \
  -H "X-API-Token: <SKILL_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
        "skill_id": "jira-gate-1",
        "jira_url": "https://jira.in.wezhuiyi.com/browse/CALL-1446",
        "inputs": {}
      }'

# 缺陷单门禁 + 自动故障分析：产品与模块既可用名称，也可用工具里的 ID
curl -X POST http://127.0.0.1:5000/skill/run \
  -H "X-API-Token: <SKILL_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
        "skill_id": "jira-gate-bug",
        "jira_url": "https://jira.in.wezhuiyi.com/browse/KEY-1234",
        "inputs": {"product": "客户服务", "module": "工单管理", "tagVersion": "v3.2.1"}
      }'
```

返回 `202`：

```json
{"task_id":"SKL-8F3C1D2E4A5B6071","record_id":12,"skill_id":"jira-gate-1","status":"queued","status_url":"/skill/task/SKL-8F3C1D2E4A5B6071"}
```

查询结果，`status` 取值 `queued`、`running`、`succeeded`、`failed`、`timeout`、`cancelled`：

```bash
curl -H "X-API-Token: <SKILL_API_TOKEN>" http://127.0.0.1:5000/skill/task/SKL-8F3C1D2E4A5B6071
```

登录用户也可以直接用 `Authorization: Bearer <登录令牌>` 调用同一组接口。任务与进度同时写入数据库表
`skill_run_records`，可通过 `GET /skill/tasks` 追踪。

查询结果里的 `artifacts` 数组列出该任务当前可下载的交付物（`name`、`size`、`modified_at`、`download_url`），
只包含约定的交付物目录（`outputs/` 与 `reports/`），任务尚未产出交付物或工作目录已清理时为空数组：

```json
{"artifacts":[{"name":"CALL-1941-G1预检包-20261012-153418.zip","size":15360,"modified_at":"2026-10-12T07:34:18Z","download_url":"/skill/task/SKL-8F3C1D2E4A5B6071/artifacts/CALL-1941-G1%E9%A2%84%E6%A3%80%E5%8C%85-20261012-153418.zip?ticket=eyJ0IjoiU0tMLThGM0MxRDJFIn0.aW1zaWdu.l9Kq2XbT7Rw"}]}
```

`download_url` 末尾的 `ticket` 是服务端用 HMAC 签名、与「任务 + 文件名」绑定的短时效票据，
浏览器地址栏、`<a download>` 或 `window.open` 这类**无法自定义请求头**的下载方式可以直接打开
`http://127.0.0.1:5000` + `download_url` 完成下载（响应带 `Content-Disposition: attachment`，
浏览器按原名保存；命令行加 `curl -o "CALL-1941-G1预检包.zip" "<上址>"` 即可）。

票据默认 30 分钟过期，可通过 `SKILL_ARTIFACT_TICKET_TTL` 调整；票据只在 URL 里出现、不落库，
泄露影响面仅限于「某个任务下的某个文件」，且很快失效。也能继续用请求头下载：不带 `ticket` 时按
`X-API-Token` 或登录令牌鉴权。下载接口只接受交付物目录下已存在的文件名：含路径分隔符、`..`、
以 `.` 开头或为空时返回 `400`（`reason=invalid-name`），文件不存在返回 `404`（`reason=not-found`），
票据或文件对不上则返回 `401`，任何情况都不会越出任务工作目录。

### 技能目录

技能放在项目根 `skills/` 目录，容器内**只读**挂载到 `/data/skills`，与 Codex 运行目录（`CODEX_HOME`）分开存放。新增技能只需建立 `skills/<skill_id>/SKILL.md`，
可选 `runtime.json` 覆盖提示词模板、超时时间与必填输入，详见 `skills/README.md`。

### 技能配置

配置来源统一为**仓库根目录的 `.env`**（容器内由 `docker compose -f docker-compose-dev.yaml --env-file .env` 注入）。应用启动时先读 `backend/.env` 再读根 `.env`，因此根 `.env` 里配置过的键一律以根 `.env` 为准；`backend/.env` 只保留本地开发路径（`SKILLS_DIR`、`SKILL_WORKSPACE_DIR`、`CODEX_HOME`）。真实进程环境变量优先级最高，任何 `.env` 都不会覆盖它。

除 `CODEX_API_KEY`、`JIRA_TOKEN`、`JIRA_BASE_URL` 三项必须自行填写外，其余技能配置均已内置已验证可用的默认值，部署时无需在 `.env` 中重复填写。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SKILL_API_TOKEN` | `local-dev-token` | 外部系统调用 `/skill` 接口的独立令牌；默认值仅供本地验证，生产环境必须替换为随机强令牌 |
| `SKILL_URL_ALLOWED_HOSTS` | `jira.in.wezhuiyi.com` | `jira_url` 主机白名单，逗号分隔；留空表示不限制 |
| `SKILL_ARTIFACT_TICKET_SECRET` | 留空 | 交付物下载票据的签名密钥；留空回退 `SKILL_API_TOKEN`，单独配置便于轮换票据而不影响接口调用 |
| `SKILL_ARTIFACT_TICKET_TTL` | `1800` | 交付物下载票据有效期（秒），让 `download_url` 能直接在浏览器打开且不会长期有效 |
| `SKILL_WORKSPACE_DIR` | `/data/skill-workspace` | 技能工作目录 |
| `CODEX_SKILL_TIMEOUT` | 留空 | 单次技能执行超时秒数；留空表示按技能自身 `runtime.json` 声明（`jira-code` 7200、`jira-defect-gate` 3600、其余 1800），填写后统一覆盖 |
| `CODEX_MODEL` | `codex/deepseek-flash` | Codex 使用的模型，后端以 `-c model=...` 覆盖，技能执行以该值为准 |
| `CODEX_MODEL_PROVIDER` | `skillrun` | 模型提供方标识，后端以 `-c model_provider=...` 传给 Codex |
| `CODEX_BASE_URL` | `https://newapi.in.wezhuiyi.com/v1` | 模型中转地址，后端以 `-c model_providers.<provider>.base_url=...` 覆盖；未配置时回退 `OPENAI_URL` |
| `CODEX_API_KEY` | 无（**必填**） | 模型中转令牌；未配置时回退 `OPENAI_KEY` |
| `CODEX_WIRE_API`、`CODEX_REASONING_EFFORT` | `responses`、`high` | Codex 请求协议与推理强度 |
| `CODEX_SANDBOX` | `danger-full-access` | Codex 子进程沙箱模式，隔离边界由容器提供 |
| `CODEX_NETWORK_RETRY_LIMIT` | `10` | 连续网络错误达到该次数即判定模型/Jira 不可达并提前终止，不再空转到超时 |
| `SKILL_RECOVER_ORPHANS` | `true` | worker 启动时把上一次运行遗留的 `running` 任务标记为失败 |
| `CODEX_HOME` | `/data/codex` | Codex 运行目录，挂载 `codex-home` 命名卷，只存放 Codex 运行期状态（`state_*.sqlite` 等） |
| `SKILLS_DIR` | `/data/skills` | 技能目录，容器内由 `./skills` 只读挂载而来，Codex 运行时不会改写它 |
| `JIRA_TOKEN` | 无（**必填**） | 注入给技能脚本的 Jira 个人访问令牌；运行时锁定为唯一来源，技能不会回落到 `skills/.jira-token` 等令牌文件 |
| `JIRA_BASE_URL` | 无（**必填**） | 注入给技能脚本的 Jira 站点地址；技能只允许访问该站点，传入其它站点的单号链接会被拒绝 |
| `GITLAB_PRIVATE_TOKEN` | 无 | `jira-code` 拉分支与推送代码用的 GitLab 令牌，需要仓库写权限；缺失时技能只会中断并如实上报 |
| `GIT_BASE_URL` | 无 | GitLab 站点地址；方案里只写 `group/repo` 时用它补全克隆地址 |
| `GIT_USER`、`GIT_PASSWORD` | 无 | 没有 `GITLAB_PRIVATE_TOKEN` 时的账号密码兜底 |
| `DEVOPS_MCP_TOKEN` | 无 | 流水线平台个人中心签发的 API Token；`jira-code` 的热更新阶段用它调 `pipeline-integration-mcp`，由后端写进 `CODEX_HOME/config.toml`（同名旧配置会被覆盖）；缺失时后端清掉该 server 配置，技能按「跳过并说明」处理 |
| `DEVOPS_MCP_URL` | `https://devops.ks1.wezhuiyi.com/mcp` | 流水线 MCP server 地址，与 `DEVOPS_MCP_TOKEN` 一起由后端写进 `CODEX_HOME/config.toml` |
| `ANALYSIS_API_BASE_URL` | `http://api:5000` | `jira-gate-bug` 回写时调用的本服务地址（宿主网络改为 `http://127.0.0.1:5000`） |
| `ANALYSIS_API_TOKEN` | `local-dev-analysis-token` | 技能脚本调用 `/analysis`、`/logfile` 等接口的内部令牌；生产环境必须替换 |

**技能运行参数来源锁定**：后端在启动技能前，用上面的配置值覆盖同名环境变量并注入 `SKILLRUN_ENV_LOCKED=1`；
技能脚本（`jira.mjs`、`jira-cli.mjs` 等）读到该标记后只认环境变量，不再回落到令牌文件、`--token` / `--base-url` 入参与内置默认站点。
必填项（`JIRA_BASE_URL`、`JIRA_TOKEN`、`DEVOPS_MCP_URL`、`CODEX_MODEL`、`CODEX_MODEL_PROVIDER`、`CODEX_BASE_URL`）缺失时，
`POST /skill/run` 直接返回 503 并列出缺哪一项，worker 也会把任务标记为失败，不会带着来源不明的参数继续跑；
`DEVOPS_MCP_TOKEN` 留空表示不启用热更新，此时后端会清掉 `CODEX_HOME/config.toml` 里的同名 MCP 配置。

技能运行参数由后端以 `codex exec -c ...` 传入（模型、中转地址、请求协议、推理强度等），容器内无需维护 `config.toml`；
需要附加 Codex 配置时用 `CODEX_EXTRA_ARGS` 追加，例如 `CODEX_EXTRA_ARGS=-c model_context_window=128000`。
Codex 自己生成的状态文件全部落在 `codex-home` 卷（本地运行对应 `backend/local-data/codex-home`），不会写进仓库目录。
令牌统一走环境变量，不要写进配置文件或提交到仓库。

### 部署要点

- 后端镜像已内置 Node.js 与 Codex CLI，构建参数为 `CODEX_CLI_VERSION` 与 `NPM_REGISTRY`。
- 后端镜像已内置附件解压工具（`bsdtar`／`unzip`，以及按发行版可用的 7-Zip 系命令），技能执行时**不需要联网安装**；
  构建阶段会校验这些命令存在，缺失会导致镜像构建失败。
- 后端镜像已内置 JDK 8、JDK 21 与 Maven 3.9.9，技能可直接在容器内编译 Java 代码；默认 `JAVA_HOME` 为
  JDK 8（业务仓库以 Java 8 为主），Java 21 项目用镜像内的 `jdk 21 <命令>` 显式切换。构建阶段会校验 JDK 8／JDK 21／Maven 三者可用，缺失会导致镜像构建失败。
- `docker compose --env-file .env -f docker-compose-dev.yaml up -d` 会挂载 `./skills:/data/skills:ro`，并使用 `./data/codex`、`./data/skill-workspace` 等宿主机目录。
- 首次部署后自检技能运行环境：

```bash
docker compose -f docker-compose-dev.yaml exec worker node /data/skills/jira-gate-1/scripts/jira-cli.mjs selftest
# jira-code 额外自检 GitLab 凭据（只回显来源，不打印令牌）
docker compose -f docker-compose-dev.yaml exec worker node /data/skills/jira-code/scripts/git-flow.mjs creds
# 配了 DEVOPS_MCP_TOKEN 时确认 MCP 已注册（看不出内容说明令牌没写进去）
docker compose -f docker-compose-dev.yaml exec worker sh -c 'grep -c pipeline-integration-mcp /data/codex/config.toml'
```

- API 只负责提交任务，Codex 实际执行发生在 `worker` 容器，请确保 `worker` 能访问 Jira 与模型中转。
- 要走 `jira-code` 的热更新，`worker` 还需要能访问 `devops.ks1.wezhuiyi.com`（流水线 MCP）与
  `pkg.in.wezhuiyi.com`（热更新工具下载与制品上传）；只做代码推送不需要这两个域名。
- 技能任务与日志分析共享 `worker` 并发槽位，单次技能可能运行数分钟；并发要求高时可单独部署一个监听同一队列的 Worker，或调整 `CELERY_WORKER_CONCURRENCY`。
- 技能工作目录保留在 `skill-workspace` 卷中便于排查，长期运行请按需清理。

### 本地验证（不使用 Docker）

本地直连 MySQL 与 Redis 即可跑通整条技能链路，表结构与生产使用同一套迁移。步骤：

1. 安装后端依赖：

```powershell
python -m venv backend\.venv-win
backend\.venv-win\Scripts\python.exe -m pip install -r backend\requirements-windows.txt
```

2. 填写**仓库根目录的 `.env`**（唯一权威来源；`backend/.env` 只用于放本地开发路径，见步骤 5）：

- 数据库与 Redis：`MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_DATABASE`、`MYSQL_USERNAME`、`MYSQL_PASSWORD` 指向可用的 MySQL 实例；`REDIS_HOST`、`REDIS_PORT` 指向可用的 Redis。应用默认按这组参数拼装 `SQLALCHEMY_DATABASE_URI`，无需手写连接串。
- 必填密钥：`JIRA_TOKEN`、`CODEX_API_KEY`。其余技能配置（含 `CODEX_MODEL`、`CODEX_BASE_URL`、`SKILL_API_TOKEN`、`JIRA_BASE_URL`）均已内置可用默认值。
- 要跑 `jira-code` 才需要额外配置 `GITLAB_PRIVATE_TOKEN`（需仓库写权限）与 `GIT_BASE_URL`；不填也能启动服务，只是该技能会中断并说明缺少凭据。
- `jira-code` 的热更新阶段需要 `DEVOPS_MCP_TOKEN`（流水线平台个人中心签发）；不填时热更新会跳过并在评论里说明，代码推送不受影响。`DEVOPS_MCP_URL` 有内置默认值，通常不用改。
- 本地路径：把 `LOCAL_STORAGE_DIR`、`LOG_DIR` 改成本机可写目录，不要沿用容器内的 `/data/...` 路径；`SKILLS_DIR`、`SKILL_WORKSPACE_DIR`、`CODEX_HOME` 放在 `backend/.env`（根 `.env` 中不配置这三项，否则会被 docker compose -f docker-compose-dev.yaml 带进容器）。

3. 建表（与生产同一套迁移）：

```powershell
cd backend
.\.venv-win\Scripts\python.exe init_database.py
.\.venv-win\Scripts\python.exe -m flask --app app.py db upgrade
```

4. 启动 Worker 与 API（两个终端分别执行，均在 `backend` 目录下）：

```powershell
.\.venv-win\Scripts\python.exe -m celery -A celery_worker.celery_app worker --loglevel=INFO --concurrency=1 --pool=solo
.\.venv-win\Scripts\python.exe -m flask --app app.py run --no-reload --port 5000
```

Windows 上 Celery 必须使用 `--pool=solo`；`--no-reload` 用于避免 Flask 重载子进程残留占用 `5000` 端口。

5. 本地专用的路径配置写在 `backend/.env`（根 `.env` 里已配置过的键一律以根 `.env` 为准，本文件只补根 `.env` 没有的项）：

```dotenv
# 技能目录：默认值是容器内的 /data/skills，Windows 本地必须指向仓库 skills 目录，
# 否则 /skill/list 会返回空列表。
SKILLS_DIR=E:\zhuiyi\jira-automation\skills
SKILL_WORKSPACE_DIR=E:\zhuiyi\jira-automation\backend\local-data\skill-workspace
CODEX_HOME=E:\zhuiyi\jira-automation\backend\local-data\codex-home
```

`ANALYSIS_API_BASE_URL`、`ANALYSIS_API_TOKEN` 等技能相关配置统一写在根 `.env`。

6. 自检与调用（`SKILL_API_TOKEN` 即根 `.env` 中配置的令牌）：

```powershell
node ..\skills\jira-gate-1\scripts\jira-cli.mjs selftest
node ..\skills\jira-code\scripts\jira-cli.mjs selftest
node ..\skills\jira-code\scripts\git-flow.mjs creds
node ..\skills\jira-gate-bug\scripts\jira-cli.mjs selftest
node ..\skills\jira-gate-bug\scripts\analysis-cli.mjs selftest
node ..\skills\jira-gate-bug\scripts\jira-cli.mjs scope <KEY|URL>
node ..\skills\jira-gate-bug\scripts\analysis-cli.mjs resolve --product yibot --module yibot-server
curl -H "X-API-Token: local-dev-token" http://127.0.0.1:5000/health/ready
curl -H "X-API-Token: local-dev-token" http://127.0.0.1:5000/skill/list
```

`jira-gate-bug` 的产品从 Jira「问题发现产品」读取；模块和版本优先取调用方输入，其次读取固定评论块
`[FAULT_ANALYSIS]`。版本也可取唯一的影响版本字段，或概要、描述、评论中的明确模块版本号；可辨认图片中显示的模块版本经 Agent 识别后可作为 `inputs.version`。修复版本不参与门禁和自动分析版本来源。仓库地址和版本到 Git Tag 的规则统一维护在
`skills/jira-gate-bug/config/scope-mapping.json`，不要把仓库地址重复写进每条评论。

模块与 Tag 的评论格式固定为（第一条是主模块，后续是关联模块）：

~~~text
[FAULT_ANALYSIS]
see-task:v1.2.3
see-management:v1.2.3
~~~

调用分析时，需要拉取的关联模块通过可重复的 `--related <模块:Tag>` 传入；脚本会分别解析仓库和 Tag，并在提交前校验精准
Tag 真实存在。评论固定块只写主模块和需要独立分析、拉取的非公共关联模块，每项都必须使用 `模块:精准Tag`；找不到精准 Tag 时按缺少分析参数打回，不会回落到 `master`、`feature/*` 或默认分支。

`scope-mapping.json` 中标记为 `"common": true` 的公共组件不单独写进评论固定块，也不要求发布或提供精准 Tag。主模块和普通关联模块的代码或日志确认引用公共组件后，分析服务会自动拉取该组件的默认分支参与排查；未命中的组件不拉取，拉取失败也不会阻断主分析。结论中需说明引用的公共组件功能和关键代码位置。

效果类缺陷如果不需要日志和复现步骤，必须在评论区明确写出 `明确缺陷不需要额外提供日志信息`。门禁确认缺陷属于效果类后，可跳过日志与复现检查；普通报错、异常、接口、性能、数据或资源问题不适用。

只想看 HTML 报告的排版、不想真的跑一次分析时，可以用既有结果 JSON 直接渲染，不访问网络：

```powershell
node ..\skills\jira-gate-bug\scripts\analysis-cli.mjs render --input <结果JSON> --out 报告.html
```

**验证注意事项**

- `jira-gate-1` 在达标与不达标两种情况下都会**真实写入 Jira 评论**（内容是逐项打标记的检查项清单）。本地验证请使用测试单或临时项目单，不要拿正式需求单试跑。
- `jira-code` 会真实创建分支、写代码并把提交**推送到远端仓库**（同时真实写一条评论）。本地验证请用测试仓库与测试单，不要指向业务主干仓库。
  热更新处理完之后技能会删掉工作区里的检出目录；推送失败或还有未推送提交时则会保留，便于继续排查。
  方案里写了流水线地址且配置了 `DEVOPS_MCP_TOKEN` 时，技能还会**真实热更新目标环境**（UAT 与 binary 环境会被拒绝，
  环境里只有唯一父组件候选时会把更新记录挂在父组件下并在评论里注明，多个候选才跳过）；本地验证请指向测试环境。
  热更新成功后记得按技能的评论提示关掉热加载：PaaS 用当前版本重新部署一次，K8S 点流水线界面的「重构」。
- `jira-gate-bug` 同样会真实写评论；没有自查结果时还会上传 HTML 报告附件并消耗一次完整的故障分析（拉代码 + 模型推理）。本地验证请使用测试缺陷单。
- 异步分析依赖 Redis：`/health/ready` 的 `checks.redis` 必须是 `ok`，否则 `/analysis/submit_async` 无法入队（技能第 3 步会失败）。上传与分析提交本身不依赖 Redis 之外的组件。
- 未配置 `LOCAL_OCR_ENABLED=true` 时图片识别走多模态模型；本地 `.env` 若仍写着 `true`，可用会话变量覆盖（`$env:LOCAL_OCR_ENABLED = "false"`），根 `.env` 的值不会覆盖已有环境变量。
- 想先验证链路而不碰 Jira，可临时新建一个只回复结论的测试技能目录，并把 `SKILLS_DIR` 指向该目录。
- 本地 `node` 版本需与容器内一致（默认 `0.142.5`）；用 `codex --version` 确认，必要时通过 `CODEX_BIN` 指定完整路径。
- 首次运行会在 `CODEX_HOME` 内生成 Codex CLI 自身的状态文件（`state_*.sqlite` 等，与业务数据库无关），并保留技能工作目录，均属预期行为。本地默认指向 `backend/local-data/codex-home`，容器内落在 `codex-home` 卷，都不会污染仓库目录。

## Git 与外部基础设施

GitLab 或 Git 服务配置保存在 `.env`。不要把真实令牌提交到仓库：

```env
GIT_BASE_URL=https://gitlab.example.com/
GIT_USER=
GIT_PASSWORD=
GITLAB_PRIVATE_TOKEN=
```

这两份 Compose 不启动本地 MySQL 和 Redis。使用外部服务时配置：

```env
MYSQL_HOST=数据库地址
MYSQL_PORT=3306
REDIS_HOST=Redis地址
REDIS_PORT=6379
```

外部依赖模式直接执行：

```powershell
docker compose --env-file .env -f docker-compose-dev.yaml up -d
```

## Windows 客户端

源码运行：

```powershell
python -m pip install -r windows-client\requirements.txt
python windows-client\log_analyzer_client.py
```

打包前通过环境变量或根目录 `.env` 配置后端地址：

```env
WINDOWS_CLIENT_BACKEND_URL=https://example.com/logapi
```

未配置时，打包客户端默认访问 `http://127.0.0.1:5000`。打包：

```powershell
windows-client\build-exe.bat
```

产物：`windows-client\dist\FaultAnalyzerClient.exe`。

macOS 必须在目标架构的 Mac 上运行 `windows-client/build-macos.sh`，产物为 `FaultAnalyzerClient.app` 和 `FaultAnalyzerClient-macos.zip`。详细说明见 `windows-client/README.md` 和 `windows-client/MACOS_BUILD.md`。

## 本地开发与测试

后端：

```powershell
backend\.venv-win\Scripts\python.exe -m unittest discover -s backend\tests -v
```

Windows 客户端：

```powershell
python -m unittest discover -s windows-client\tests -v
```

前端：

```powershell
Push-Location frontend\app\log-analyze
npm ci
npm run lint
npm run test:unit -- --runInBand
npm run build
Pop-Location
```

源码中文注释审计：

```powershell
python scripts\check_source_comments.py
```

## 安全清理

先预览，再删除可再生成内容：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\clean_generated.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts\clean_generated.ps1 -KeepReleaseArtifacts
```

清理脚本不会删除 `.env`、`users.csv`、源码、测试、文档和保留的最终发布包。

## 故障排查

### 登录密码正确但无法进入

检查 `users.csv` 的表头、编码和状态值，确认 API 能读取挂载文件：

```powershell
docker compose -f docker-compose-dev.yaml exec api python verify_runtime.py --runtime
docker compose -f docker-compose-dev.yaml logs api
```

### 迁移提示 MySQL 认证失败

当前依赖已使用 `PyMySQL[rsa]` 支持 MySQL 8 `caching_sha2_password`。确认使用最新后端镜像，并重新拉取、重启 API：`docker compose --env-file .env -f docker-compose-dev.yaml pull api && docker compose --env-file .env -f docker-compose-dev.yaml up -d api`。

### 图片长时间停在分析中

图片识别走多模态模型，长图或大图会明显更慢（默认本地 OCR 已停用）。先查看 Worker 日志确认当前阶段：

```powershell
docker compose -f docker-compose-dev.yaml logs -f worker
```

若确实需要本地 OCR 预检，再显式开启并检查运行环境：

```powershell
docker compose -f docker-compose-dev.yaml exec api python verify_runtime.py --ocr
```

### 多张图片只看到一个问题

确认客户端使用当前版本，并检查每张图片都获得独立上传引用。相同问题会合并展示，不同错误应分别出现在问题明细中。日志截图请选择 `log_image`，普通业务页面请选择 `business_image`。

### 前端可打开但功能不可用

这是未登录状态的预期行为。先登录，再检查浏览器请求是否携带 `Authorization: Bearer ...`，并查看 `/health/ready` 与 API 日志。

### 技能任务一直处于 running 或返回失败

技能执行发生在 `worker` 容器，按顺序排查：

```bash
# 1. 查看 Codex 执行过程与错误输出
docker compose -f docker-compose-dev.yaml logs -f worker
# 2. 技能运行环境自检（令牌、连通性、账号）
docker compose -f docker-compose-dev.yaml exec worker node /data/skills/jira-gate-1/scripts/jira-cli.mjs selftest
# 3. 确认镜像内 Codex CLI 可用
docker compose -f docker-compose-dev.yaml exec worker codex --version
```

常见原因：`worker` 无法访问 Jira 或模型中转地址；`JIRA_TOKEN` 未配置或已过期；
`CODEX_WIRE_API` 与中转服务的接口协议不匹配（`responses` 对应 `/v1/responses`，`chat` 对应 `/v1/chat/completions`）。
任务的阶段、最近事件与错误信息可通过 `GET /skill/task/<task_id>` 或 `skill_run_records` 表查看。

两类问题会自动识别，无需等满 `CODEX_SKILL_TIMEOUT`：

- **网络不可达**：Codex 反复输出 `Reconnecting... waiting for network` 时，连续达到 `CODEX_NETWORK_RETRY_LIMIT`
  （默认 10 次，按指数退避约 15 分钟）即提前终止，任务记为 `failed`，`stage=network_unreachable`，错误信息给出中转地址、
  出网与代理的排查方向。若该任务是从沙箱或离线环境启动的进程提交的，请改用正常网络环境重启 `worker`。
- **worker 重启遗留**：`worker` 启动时若 `SKILL_RECOVER_ORPHANS=true`（默认），会把上一次运行中残留的
  `running` 任务标记为 `failed`，`stage=worker_restarted`；仍被其它 worker 正常执行的任务不会被误改。
