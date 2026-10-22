# 故障分析工具生产部署说明

生产服务器只需要本目录中的部署文件和已发布镜像，不需要项目源码，也不要执行 `docker compose up --build`。

生产 Compose 中的 `api`、`worker`、`migrate` 统一使用 Docker host 网络：API 直接监听宿主机 `${API_PORT:-5000}`。启动前请确认该端口未被其他进程占用。

MySQL 和 Redis 必须在 `.env.production` 中填写宿主机可访问的真实 IP 或域名。不要填写 `mysql`、`redis`、`api` 等 Compose 服务名，因为 host 网络不提供 Compose DNS 名称解析。前端如单独部署，应把 `/api` 请求代理到宿主机的 `API_PORT`。

## 首次部署

1. 将 `docker-compose-prod.yaml`、`.env.production.example`、`users.example.csv`、`restart-master.sh` 和本说明放在同一目录；
   解压部署包时 `config/maven/settings.xml` 要保持 `config/maven/` 相对路径不变（生产 Compose 按该路径挂载）。
2. 将 `.env.production.example` 复制为 `.env.production`，填写镜像地址、MySQL、Redis、模型和第三方接口配置。
3. 将 `users.example.csv` 复制为 `data/users.csv`（Compose 将它挂载到容器 `/users.csv`），修改默认密码并由管理员持续维护。
4. 如需使用技能执行接口（`/skill`），把 `skills/` 目录上传到与 Compose 文件同级的位置（容器只读挂载为 `/data/skills`），技能所需的模型参数在 `.env.production` 中配置。
5. 登录镜像仓库后拉取并启动：

```bash
docker compose --env-file .env.production -f docker-compose-prod.yaml pull
docker compose --env-file .env.production -f docker-compose-prod.yaml up -d
docker compose --env-file .env.production -f docker-compose-prod.yaml ps -a
```

`migrate` 是一次性迁移容器，成功完成后显示为 `Exited (0)` 属于正常状态。`api` 和 `worker` 应保持运行。

API 存活检查为 `http://服务器地址:${API_PORT:-5000}/health/live`。前端如单独部署，再访问对应的前端地址。

### Jira 自动触发

手动调用 `/skill/run` 成功但新建缺陷没有自动分析时，需要确认 Jira 侧是否配置了 webhook 或 Automation 规则。服务端入口为：

```text
POST http://<服务器地址>:${API_PORT:-5000}/skill/jira/events
```

Jira Automation 使用请求头 `X-API-Token: <SKILL_API_TOKEN>`；Jira 原生 webhook 使用 `.env.production` 中的 `JIRA_WEBHOOK_SECRET` 做 HMAC-SHA256 签名。接口默认只接收 `jira:issue_created`、只接收 `缺陷`/`Bug`，同一缺陷已有排队或执行中任务时自动去重。若 Jira 规则仍指向 `/skill/run`，必须把请求体改成 `skill_id`、`jira_url`、`inputs` 三个字段，或者改为调用上述专用 webhook 入口。

## 只更新后端

发布端构建并推送新的后端镜像后，只修改 `.env.production` 的 `BACKEND_IMAGE`，然后执行：

```bash
docker compose --env-file .env.production -f docker-compose-prod.yaml pull migrate api worker
docker compose --env-file .env.production -f docker-compose-prod.yaml up -d migrate api worker
```

迁移、API 和 Worker 共用同一个后端镜像，更新后端时必须一起更新。

## 完整更新

```bash
docker compose --env-file .env.production -f docker-compose-prod.yaml pull
docker compose --env-file .env.production -f docker-compose-prod.yaml up -d
```

## 重启服务

仅更新挂载的 `skills/`、`.env.production` 或容器运行状态后，可执行：

```bash
bash restart-master.sh
```

该脚本默认用 `docker compose up -d --force-recreate` 重建 `api` 和 `worker`，会重新读取 `.env.production`，但不会重启或删除数据库。需要指定服务时，可传入 Compose 服务名，例如 `bash restart-master.sh api`。

## 离线导入镜像

在可访问镜像的机器上使用仓库中的 `scripts/export_release_image.ps1` 分别导出发生变化的镜像，把 `.tar` 和 `.sha256` 文件传到生产服务器。核对摘要后导入：

```bash
sha256sum -c jira-automation-backend-2026.07.18-1.tar.sha256
docker load -i jira-automation-backend-2026.07.18-1.tar
```

导入后无需 `--build`，直接执行对应组件的 `up -d` 命令。

## 回滚

将 `.env.production` 中的镜像标签恢复为上一个已验证版本，再执行对应组件的 `up -d`。不要使用 `latest`，否则无法可靠确认当前版本和回滚目标。

## 技能执行（/skill 接口）

技能功能需要两项额外条件，未配置时不影响其余业务功能：

1. 后端镜像需为包含 Codex CLI 的版本（`docker compose ... pull` 拉取最新镜像即可）。
2. 将本包的 `skills/` 目录上传到 Compose 文件同级目录，容器会把它**只读**挂载到 `/data/skills`；目录缺失时 `/skill/list` 返回空列表。

镜像内已预装附件解压工具（`bsdtar`、`unzip` 及可用的 7-Zip 系命令）与 Python/Node 依赖，
技能执行过程中不需要联网安装任何软件；如需确认，可在容器内执行 `docker compose ... exec worker sh -c "command -v bsdtar unzip 7z"`。

镜像同时内置 JDK 8（`/opt/java/jdk8`，默认 `JAVA_HOME`）、JDK 21（`/opt/java/jdk21`）与 Maven 3.9.9
（`/opt/maven`），技能可以直接在容器内编译 Java 代码，无需额外拉取构建环境。Java 8 项目直接用默认 JDK，
Java 21 项目用镜像内的 `jdk` 助手显式切换：

```bash
docker compose --env-file .env.production -f docker-compose-prod.yaml exec worker sh -c "jdk list"
docker compose --env-file .env.production -f docker-compose-prod.yaml exec worker sh -c "cd /tmp/<项目> && jdk 21 mvn -DskipTests package"
```

生产 Compose 还给 `worker` 挂了两处 Maven 路径，两者都要在宿主机准备好：

- `config/maven/settings.xml` → `/root/.m2/settings.xml`（只读）：把 Maven 镜像指向内网 Nexus 聚合仓库。
  业务仓库依赖的私有制品（`com.zhuiyi.prompt:llm-prompt-jar`、`com.cmos.dialogos:dialogos-api` 等）只有这个库里有，
  镜像自带的 Maven 直连 Maven Central 必然解析失败，jira-code 的编译验证与热更新制品构建会整段跳过。
  该文件不含任何凭据，随部署包分发的 `config/maven/` 就是它。若缺失，Docker 会把挂载点建成空目录，Maven 同样读不到配置。
- `./data/m2/repository` → `/root/.m2/repository`：Maven 本地仓库缓存。需要先从开发机
  `C:\Users\<用户>\.m2\repository`（约 3.8 GB）拷贝预热，之后容器重建不会丢。只拷私有依赖也能用，
  但首次构建仍需联网补齐其余依赖。

```bash
# 首次部署：在部署机建目录，并从开发机拷入预热好的本地仓库
mkdir -p data/m2/repository
scp -r <开发机>:/c/Users/<用户>/.m2/repository/* data/m2/repository/

# 自检：settings.xml 必须是文件（不是空目录），且本地仓库里已有依赖
docker compose --env-file .env.production -f docker-compose-prod.yaml exec worker \
  sh -c "ls -l /root/.m2/settings.xml && ls /root/.m2/repository | head"
```

技能运行参数（模型、中转地址、请求协议、推理强度）由后端以 `codex exec -c ...` 传入，容器内不需要维护 `config.toml`；
Codex 自身的状态库（`state_*.sqlite` 等）写在 `codex-home` 命名卷中，不会落到宿主机目录。需要附加 Codex 配置时用
`CODEX_EXTRA_ARGS` 追加，例如 `CODEX_EXTRA_ARGS=-c model_context_window=128000`。

`.env.production` 是技能运行参数的唯一权威来源，除下面三项外其余技能配置均已内置默认值，可不再改动：

```env
# 【必填】技能使用的 Jira 个人访问令牌
JIRA_TOKEN=请填写
# 【必填】技能访问的 Jira 站点地址，必须与令牌所属站点一致：技能只允许访问这个地址，
# 传入其它站点的需求单链接会被直接拒绝，避免把评论写到别的环境。
JIRA_BASE_URL=https://jira.in.wezhuiyi.com
# 【必填】Codex 使用的模型中转令牌
CODEX_API_KEY=请填写
```

`docker compose --env-file .env.production -f docker-compose-prod.yaml` 注入的是真实进程环境变量，优先级最高，不会被宿主目录里任何
`.env` 文件覆盖；容器内也没有 `.env` 文件，因此容器里的取值完全由这份文件决定。

需要按环境调整或加固时，再确认这几项（默认值已可直接使用）：

```env
# 外部系统调用 /skill 接口的独立令牌；默认值为本地验证令牌，生产环境必须替换为随机强令牌
SKILL_API_TOKEN=local-dev-token
# jira_url 允许的主机白名单
SKILL_URL_ALLOWED_HOSTS=jira.in.wezhuiyi.com
# Codex 使用的模型与中转地址
CODEX_MODEL=codex/deepseek-flash
CODEX_BASE_URL=https://newapi.in.wezhuiyi.com/v1
# jira-code 拉分支、推送代码用的 GitLab 凭据（令牌需要仓库写权限）与站点地址
GITLAB_PRIVATE_TOKEN=请填写
GIT_BASE_URL=https://code.in.wezhuiyi.com/
# jira-code 热更新用的流水线平台令牌（个人中心签发）；不填时热更新会跳过并在 Jira 评论里说明
DEVOPS_MCP_TOKEN=请填写
# 流水线 MCP server 地址，有默认值，通常不用改
DEVOPS_MCP_URL=https://devops.ks1.wezhuiyi.com/mcp
```

部署后自检：

```bash
docker compose --env-file .env.production -f docker-compose-prod.yaml exec worker \
  node /data/skills/jira-gate-1/scripts/jira-cli.mjs selftest
# G1 / G2 的类型门禁自检：返回 supported=true（issuetype 为「故事」）才会继续处理
docker compose --env-file .env.production -f docker-compose-prod.yaml exec worker \
  node /data/skills/jira-gate-1/scripts/jira-cli.mjs precheck CALL-1
docker compose --env-file .env.production -f docker-compose-prod.yaml exec worker \
  node /data/skills/jira-gate-2/scripts/jira-cli.mjs precheck CALL-1
# jira-code 额外自检 GitLab 凭据（只回显来源，不打印令牌）
docker compose --env-file .env.production -f docker-compose-prod.yaml exec worker \
  node /data/skills/jira-code/scripts/git-flow.mjs creds
# 配了 DEVOPS_MCP_TOKEN 时确认流水线 MCP 已写进任务独立的 Codex 配置
docker compose --env-file .env.production -f docker-compose-prod.yaml exec worker \
  sh -c 'for f in /data/codex/runs/*/config.toml; do grep -q pipeline-integration-mcp "$f" && echo "$f"; done'
curl -H "X-API-Token: <SKILL_API_TOKEN>" http://127.0.0.1:5000/skill/list
```

## 安全边界

- 生产服务器不保存源码仓库或完整源码包，只保存部署配置、运行数据和镜像。
- 镜像仍包含程序运行所需的应用文件；Docker 镜像不是加密或防反编译手段，镜像仓库权限仍必须严格控制。
- `.env.production`、`users.csv`、上传文件、日志和 OCR 命名卷不进入镜像。
- `users.csv` 支持热加载；修改用户状态不需要重启服务。
- 不执行 `docker compose --env-file .env.production -f docker-compose-prod.yaml down -v`，除非明确需要删除上传文件、日志和 OCR 模型数据。
