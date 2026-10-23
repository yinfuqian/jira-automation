---
name: pipeline-adapter
description: >
  将项目接入追一科技 CI/CD 流水线（编译与部署分离 v3，支持单独接入 K8S、单独接入 docker-compose，或同时接入两者）。
  当用户请求：
  1) 将模块/仓库/项目"接入流水线"、"适配 K8s"、"适配 docker-compose"、"生成 ci.yaml";
  2) 需要"编译与部署分离"、"v3 流水线"、"docker-compose-and-kubernetes"、"kubernetes"、"docker-compose";
  3) 需要生成 Dockerfile/app.json/K8S 编排文件/docker-compose.yml/sonar 配置 时触发。
---

# Pipeline Adapter

## 为什么需要接入 CI/CD 流水线

### 核心目的
将项目从"手动/脚本化构建部署"升级为"平台化、标准化、自动化"的交付模式。接入后，代码提交即可触发自动编译、镜像构建、质量扫描和多环境部署，无需人工干预构建细节。

另一层核心价值是**解耦部署运行方式与仓库源码**：
- **以前**：若同时支持 docker-compose 和 K8S，仓库需维护两套独立的编排文件，任何配置变更（端口、环境变量、资源限制）都要同步修改多处。
- **现在**：仓库内只保留一份抽象的 `app.json` 规格文件，平台根据这份规格自动生成 docker-compose、K8S 乃至未来新部署方式的编排。仓库无需关心最终部署到什么环境。
- **未来**：若业务需要支持新的部署场景（如边缘节点、Serverless），只需在平台侧新增转换器，**仓库代码和配置零改动**即可适配。

### 编译与部署分离 v3 解决了什么问题

| 问题 | v3 之前的做法 | v3 的解决方式 |
|------|--------------|---------------|
| 编译镜像臃肿 | 构建和运行共用同一镜像，包含编译工具、源码、缓存 | 编译阶段使用 builder 镜像，最终产出 `scratch` 空镜像，仅包含二进制/制品和 `app.json` |
| 配置散落在代码和脚本中 | 环境变量、端口、资源限制写在 Dockerfile / docker-compose / K8S YAML 多处 | 所有运行时配置收敛到 `app.json`，由平台在部署阶段统一注入 |
| 多环境维护成本高 | 每个环境（开发/测试/生产）需要维护独立的 docker-compose 或 K8S 文件 | 一份 `app.json` + 模板化编排文件，平台按环境渲染变量 |
| 架构适配困难 | 需要为 amd64 / arm64 分别准备环境和镜像 | `ci.yaml` 声明多架构，`Dockerfile` 通过 `TARGETARCH` 交叉编译，一份配置产出多架构包 |
| 运行时基础镜像僵化 | 编译产物与特定基础镜像强绑定，更换运行时版本或加固镜像需改造 Dockerfile | 制品按规范目录（如 `/app/`）存放于 `scratch` 空镜像，平台在部署阶段可叠加**任意运行时基础镜像 + 服务制品**，灵活适配安全加固、国产化替换等场景 |
| 运行时安全问题 | 容器以 root 启动 | 通过 `apprt` + `gosu` 实现非 root 用户运行 |

### 接入后项目的变化

1. **新增 10+ 个配置文件**，但**原有业务代码零改动**。
2. **平台接管部署细节**：镜像构建完成后，平台读取 `app.json` 自动组装运行时镜像（注入 JDK/nginx/Python 等解释器）、生成环境变量 ConfigMap、挂载卷、配置探针。
3. **统一的质量门禁**：SonarQube 代码扫描、单元测试成为流水线必过环节。
4. **一份代码，多处部署**：同一套配置同时支持 docker-compose（开发/测试环境）和 K8S（生产环境）。

---

## 接入后会生成哪些文件

以下文件根据 `orchestration.type`（部署方式）按需生成。**所有项目都会生成通用文件**，仅 docker-compose 或仅 K8S 的项目则只生成对应编排文件。

### 通用文件（所有项目）

| 文件 | 作用 | 使用者 |
|------|------|--------|
| `ci.yaml` | 向流水线提供**构建任务的必要参数**：技术栈、构建方式、单元测试命令、多架构声明、编排类型 | **流水线引擎**读取后决定编译策略和部署方式 |
| `Dockerfile` | **服务制品构建的入口**。多阶段构建确保在任意环境（本地、CI 节点、不同架构机器）都能一致地产出制品；最终产出 `scratch` 空镜像，仅包含编译产物和 `app.json`。制品必须按规范目录（如 `/app/`）存放，确保后续平台可采用「**任意运行时基础镜像 + 规范目录制品**」的模式组装最终运行时镜像 | **任何具备 Docker 的环境**均可执行构建 |
| `app.json` | **服务运行时配置的抽象规格文件**。将健康检查、资源限制、端口、环境变量、挂载卷、运行时解释器等配置从具体编排格式（docker-compose / K8S）中抽离出来，平台据此统一生成二进制启动参数、docker-compose 编排和 K8S 编排 | **部署平台**读取后自动生成各环境的运行时镜像和编排文件 |
| `build_files/build.sh` | 编译入口脚本。将复杂编译逻辑收敛于此，保持 Dockerfile 简洁且可复用 | **构建阶段**由 Dockerfile 调用 |
| `deployments/binary/apprt` | **服务启动入口**。容器启动时作为 entrypoint 执行，负责创建非 root 用户、切换用户身份、启动应用主进程 | **容器启动时**执行 |
| `sonar-project.properties` | SonarQube 代码扫描配置 | **质量门禁** |

### docker-compose 专属（`orchestration.type` 含 `docker-compose`）

| 文件 | 作用 |
|------|------|
| `docker-compose.yml` | docker-compose 服务编排 |
| `deployments/binary/dependence_install.sh` | 运行时依赖安装 |
| `hook.sh` | docker-compose 环境初始化钩子 |

### K8S 专属（`orchestration.type` 含 `kubernetes`）

| 文件 | 作用 |
|------|------|
| `deployments/kubernetes/*_deployment.yaml` | K8S Deployment 编排 |
| `deployments/kubernetes/*_service.yaml` | K8S Service |
| `deployments/kubernetes/*_ingress.yaml` | K8S Ingress（可选） |
| `deployments/kubernetes/*_statefulset.yaml` | K8S StatefulSet（按需） |
| `deployments/kubernetes/*_job.yaml` | K8S Job（按需） |

### 关键设计

1. **运行时镜像的组装模式**：编译阶段产出 `scratch` 空镜像（仅含规范目录下的制品 + `app.json`），部署阶段由平台叠加「**任意运行时基础镜像 + 规范目录制品**」组装成最终运行时镜像。这实现了运行时非功能性要求的灵活适配：安全加固、国产化基础镜像替换、运行时版本升级等场景，均**无需修改仓库**。
2. **对制品的约束**：由于最终制品运行在平台选择的基础镜像中，制品本身应尽量**消除对操作系统的依赖**。例如 Go 项目需静态编译，Python 项目需使用 portable 方式打包依赖。
3. **配置收敛**：v3 不再单独维护 K8S ConfigMap 文件。所有配置收敛到 `app.json` 的 `environments` 和 `secrets` 中，平台在部署时自动提取生成。

---

## 执行前强制检查清单

生成文件前必须确认：

- [ ] 已运行 `python3 scripts/detect_project_info.py {项目根目录}` 获取技术栈与旧版状态
- [ ] 已确认 `ci.yaml` 的 `orchestration.type`，确定生成文件集合
- [ ] 旧版升级项目 → 读取 `references/upgrade-workflow.md` 执行迁移（**禁止直接覆盖模板**）
- [ ] 新项目 → 交互确认：模块名、REPO_GROUP、技术栈、端口、部署方式、Ingress 需求
- [ ] `app.json` 必须通过 `python3 scripts/generate_app_json.py ...` 生成，禁止手写
- [ ] 已运行 `python3 scripts/scan_health_endpoints.py` 检查探针端点
- [ ] Dockerfile 最后一个 `FROM` 为 `scratch`，且包含 `COPY ... app.json /app/app.json`
- [ ] docker-compose.yml 已运行 `python3 scripts/validate_docker_compose.py` 校验通过
- [ ] 所有文件生成后已汇总 TODO 项并输出给用户

---

## Quick Start

1. **运行检测脚本**获取项目信息：
   ```bash
   python3 scripts/detect_project_info.py {项目根目录}
   ```
2. 根据 `legacy.is_legacy_project` 判断是**旧版升级**还是**新项目**：
   - `true` → 读取 `references/upgrade-workflow.md` 执行迁移适配
   - `false` → 继续按下方新项目流程执行
3. 交互确认新项目信息（仅新项目需要）。
4. 按 `orchestration.type` 生成对应文件（默认 `docker-compose-and-kubernetes`）。
5. 运行 `scripts/scan_health_endpoints.py` 检查探针端点。
6. **输出前再次执行上方检查清单。**

---

## 分步指引索引

详细操作步骤请进入对应章节阅读：

| 步骤 | 内容 | 详细文档 |
|------|------|---------|
| **Step 1** | 识别项目信息（自动探测、REPO_GROUP 确认、交互提问、多架构规则） | [`references/step-by-step-guide.md#step-1`](references/step-by-step-guide.md#step-1) |
| **Step 2** | 旧版升级检测与适配（判断依据、升级核心要点、清理旧版文件） | [`references/upgrade-workflow.md`](references/upgrade-workflow.md) |
| **Step 3** | 复制新版模板（模板路径映射、占位符替换规则、多模块规则） | [`references/step-by-step-guide.md#step-3`](references/step-by-step-guide.md#step-3) |
| **Step 4** | 根据源码调整编译与启动命令（Dockerfile 约束、技术栈专属模板） | [`references/step-by-step-guide.md#step-4`](references/step-by-step-guide.md#step-4) |
| **Step 5** | `app.json` 生成规范（脚本命令、必填字段、层级规范、volumes 类型） | [`references/step-by-step-guide.md#step-5`](references/step-by-step-guide.md#step-5) |
| **Step 6** | 规范校验与一致性检查（端口/路径/名称/Label Selector/docker-compose/探针/Secret/引号） | [`references/step-by-step-guide.md#step-6`](references/step-by-step-guide.md#step-6) |
| **Step 7** | 列出 TODO 并提醒用户 | [`references/step-by-step-guide.md#step-7`](references/step-by-step-guide.md#step-7) |
| **Step 8** | 本地验证建议 | [`references/step-by-step-guide.md#step-8`](references/step-by-step-guide.md#step-8) |

### 按需读取的 references

| 检查项 | 参考文件 |
|--------|---------|
| `ci.yaml` 规范 | [`references/ci-v3-spec.md`](references/ci-v3-spec.md) |
| `app.json` 完整字段说明 | [`references/app-json-spec.md`](references/app-json-spec.md) |
| `app.json` 生成详细指南 | [`references/app-json-generation-guide.md`](references/app-json-generation-guide.md) |
| K8S YAML 规范 | [`references/k8s-yaml-spec.md`](references/k8s-yaml-spec.md) |
| docker-compose 规范 | [`references/paas-spec.md`](references/paas-spec.md) |
| sonar 配置规范 | [`references/sonar-spec.md`](references/sonar-spec.md) |
| 各技术栈模式 | [`references/tech-stack-patterns.md`](references/tech-stack-patterns.md) |

---

## 特别注意

1. **新版不再生成 `k8s-config-map.yaml`**。配置统一收敛到 `app.json`。
2. **K8S YAML 文件后缀必须是 `.yaml`**，不能是 `.yml`。
3. 若项目已存在旧版文件，**执行升级适配，不要直接覆盖模板**。
4. 已接入 `apprt` 配置中心（存在 `deployments/apprt/apprt.json`）的模块，**不要**自定义 `deployments/binary/apprt`，也**不要**在 Dockerfile 中额外拷贝 apprt 脚本。
5. **未接入 `apprt` 的模块，请不要在 `deployments/apprt/` 下放置 `apprt.json`**，否则流水线会误判。
6. 若模块不需要 Ingress，可以不创建 `*_ingress.yaml`。

## 机器可读结果（仅顶层调用时）

- 当本技能是平台 `/skill/run` 的顶层技能（任务提示里的 skill_id 就是 `pipeline-adapter`）时，必须在接入文件生成与校验完成之后、清理工作区之前，把最终结果写到**任务根目录**（当前工作目录）的 `.skill-result.json`：

```json
{
  "schemaVersion": 1,
  "skillId": "pipeline-adapter",
  "conclusion": "已完成",
  "summary": "已生成 ci.yaml、app.json、Dockerfile 与 K8S 编排并通过校验",
  "blockingCount": 0
}
```

- `conclusion` 按实际结果写 `已完成` / `部分完成` / `失败` / `不适用`；没有 Jira 单号时省略 `issueKey`。
- 该文件只用于调用统计，不是交付物，不写入 Jira，平台读取入库后会自动删除；无法形成业务结论时可以不写。

---

## FAQ / 常见问题

### Q1: 更新模块时报 `field is immutable`
**解决**：在 K8S 主控机删除旧 Deployment 后重新更新。

### Q2: sonar 扫描线上有数据但单测覆盖率始终为 0
**原因**：流水线执行单测时不是 root 用户，可能因权限不足导致失败。
**解决**：确保单测命令和日志目录对普通用户可写。

### Q3: 编译出的包在 scratch 镜像中无法运行
**原因**：制品存在操作系统依赖（如动态链接库、路径硬编码等），而 `scratch` 空镜像不含任何操作系统组件。平台后续叠加的运行时基础镜像也可能与制品的预期环境不一致。
**解决**：
- **Go**：确保 `CGO_ENABLED=0` 和 `-ldflags '-extldflags "-static"'` 实现静态编译。
- **Java**：`apprt` 中通过 `WORKDIR` 推导 JDK 路径：`JAVA_HOME="${WORKDIR}/../share/jdk"`，禁止硬编码 `JAVA_HOME=/app/share/jdk`。
- **Python**：使用 portable 方式打包依赖（如 `pip install --target` 或虚拟环境打包），确保解释器和库路径相对化。
- **通用**：`apprt` 脚本中禁止硬编码任何绝对路径（包括 `/app/`），所有路径必须通过 `WORKDIR="$( cd "$( dirname "$0"  )" && pwd  )"` 自推导；制品本身避免依赖特定发行版的系统库。
