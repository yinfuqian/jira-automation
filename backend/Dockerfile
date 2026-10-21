# 故障分析工具后端镜像采用多阶段构建，最终运行镜像不携带测试、配置和默认用户密码文件。
ARG PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.10-slim-bookworm
# 技能执行需要容器内具备 Node.js 运行时与 Codex CLI，运行时镜像复用 Node 官方镜像中的产物。
ARG NODE_BASE_IMAGE=docker.m.daocloud.io/library/node:20-bookworm-slim
ARG CODEX_CLI_VERSION=0.142.5
ARG NPM_REGISTRY=https://registry.npmmirror.com

FROM ${NODE_BASE_IMAGE} AS node-runtime

FROM ${PYTHON_BASE_IMAGE} AS base

# 技能目录与 CODEX_HOME 分开：CODEX_HOME 只承载 Codex 自身状态，技能由宿主机目录只读挂载，
# 避免 Codex 运行时把内置技能和状态库（state/logs/goals 等 *.sqlite）写进业务技能目录。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FLASK_APP=app.py \
    AUTH_USERS_FILE=/data/users.csv \
    PADDLE_PDX_CACHE_HOME=/data/paddle-cache \
    REPO_CACHE_DIR=/tmp/jira-automation-repos \
    SKILLS_DIR=/data/skills \
    SKILL_WORKSPACE_DIR=/data/skill-workspace \
    CODEX_HOME=/data/codex

WORKDIR /app

# 系统镜像源和 Python 镜像源都允许在企业网络中通过构建参数替换。
#ARG APT_MIRROR=https://mirrors.aliyun.com/debian
ARG APT_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# 附件解压工具随镜像一起发布，避免技能执行时再联网安装：bsdtar（libarchive）负责 RAR/7z 只读解压，
# 7-Zip 系命令按发行版包名择优安装（bookworm 起部分版本改名为 7zip，因此两种包名都尝试）。
# 该清单同时用于离线安装校验，改动后必须用 scripts/export_apt_offline_packages.ps1 重新导出离线 deb 包。
ARG APT_PACKAGES="git ca-certificates curl libgomp1 libgl1 libglib2.0-0 unzip libarchive-tools p7zip-full 7zip"

# 仓库内 backend/offline/apt/<发行版代号>-<架构> 预置了这些系统依赖的离线 deb 包：
# 命中时完全本地安装，企业内网无 apt 源也能构建镜像；未命中时回退到在线镜像源安装。
RUN --mount=type=bind,source=offline/apt,target=/opt/apt-offline \
    set -eu; \
    deb_dir="/opt/apt-offline/$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release)-$(dpkg --print-architecture)"; \
    if [ -d "$deb_dir" ] && ls "$deb_dir"/*.deb >/dev/null 2>&1; then \
        echo "使用仓库内离线 deb 包安装系统依赖：$deb_dir"; \
        dpkg -i "$deb_dir"/*.deb; \
    else \
        echo "开始安装依赖" \
        && if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
            sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g; s|http://security.debian.org/debian-security|${APT_MIRROR}-security|g" /etc/apt/sources.list.d/debian.sources; \
        elif [ -f /etc/apt/sources.list ]; then \
            sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g; s|http://security.debian.org/debian-security|${APT_MIRROR}-security|g" /etc/apt/sources.list; \
        fi \
        && apt-get update \
        && apt-get install -y --no-install-recommends ${APT_PACKAGES} \
        && echo "安装成功"; \
    fi; \
    rm -rf /var/lib/apt/lists/*; \
    for package_name in ${APT_PACKAGES}; do \
        dpkg-query -W "$package_name" >/dev/null 2>&1 || { echo "系统依赖安装失败：$package_name" >&2; exit 1; }; \
    done

# 技能在容器内需要直接编译 Java 代码，因此镜像内置 JDK 8、JDK 21 与 Maven 3.9.9。
# 两个 JDK 都装 Adoptium(Temurin) 官方 tarball：bookworm 的 apt 源里没有 JDK 8，统一走 tarball
# 既能保证两个版本来源一致，也不会把离线 apt 依赖清单（backend/offline/apt）扩大到上百个 java 相关 deb。
# 下面两组地址按顺序尝试、命中即用；企业内网可以用 --build-arg 把内部制品库排在最前面。
ARG JDK8_VERSION=8u504b01
ARG JDK21_VERSION=21.0.12.1_1
ARG MAVEN_VERSION=3.9.9
ARG ADOPTIUM_MIRRORS="https://mirrors.tuna.tsinghua.edu.cn/Adoptium https://mirrors.huaweicloud.com/adoptium"
ARG MAVEN_URLS="https://mirrors.huaweicloud.com/apache/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/${MAVEN_VERSION}/apache-maven-${MAVEN_VERSION}-bin.tar.gz"
ARG TARGETARCH

# 默认使用 JDK 8：容器内要编译的业务仓库绝大多数是 Java 8，且老项目依赖的 lombok 1.16~1.18 在 JDK 21 下
# 会报编译错误。Java 21 项目用 jdk 助手显式切换（见 docker/jdk.sh），避免默认版本把依赖不兼容伪装成代码问题。
# MAVEN_OPTS 固定 UTF-8：javac 默认取 JVM 的平台编码，一旦运行环境没有可用 LANG（实测会退化成
# ANSI_X3.4-1968/US-ASCII），未声明 sourceEncoding 的项目编译中文源码就会报 unmappable character。
ENV JAVA8_HOME=/opt/java/jdk8 \
    JAVA21_HOME=/opt/java/jdk21 \
    JAVA_HOME=/opt/java/jdk8 \
    MAVEN_HOME=/opt/maven \
    MAVEN_OPTS="-Dfile.encoding=UTF-8" \
    PATH="/opt/java/jdk8/bin:/opt/maven/bin:${PATH}"

COPY docker/jdk.sh /usr/local/bin/jdk

RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
        amd64) jdk_arch=x64 ;; \
        arm64) jdk_arch=aarch64 ;; \
        *) echo "不支持的镜像架构：${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    download() { \
        archive="$1"; shift; \
        for url in $*; do \
            echo "下载 $archive：$url"; \
            if curl -fsSL --retry 3 --connect-timeout 20 -o "$archive" "$url"; then return 0; fi; \
            echo "该地址不可用，改试下一个：$url" >&2; \
            rm -f "$archive"; \
        done; \
        echo "所有地址都无法下载 $archive，请用 --build-arg 换成内部制品库地址" >&2; \
        return 1; \
    }; \
    jdk8_urls=""; \
    jdk21_urls=""; \
    for mirror in ${ADOPTIUM_MIRRORS}; do \
        jdk8_urls="$jdk8_urls $mirror/8/jdk/$jdk_arch/linux/OpenJDK8U-jdk_${jdk_arch}_linux_hotspot_${JDK8_VERSION}.tar.gz"; \
        jdk21_urls="$jdk21_urls $mirror/21/jdk/$jdk_arch/linux/OpenJDK21U-jdk_${jdk_arch}_linux_hotspot_${JDK21_VERSION}.tar.gz"; \
    done; \
    download /tmp/temurin8.tar.gz $jdk8_urls \
    && download /tmp/temurin21.tar.gz $jdk21_urls \
    && download /tmp/maven.tar.gz ${MAVEN_URLS} \
    && mkdir -p /opt/java/jdk8 /opt/java/jdk21 /opt/maven \
    && tar -xzf /tmp/temurin8.tar.gz -C /opt/java/jdk8 --strip-components=1 \
    && tar -xzf /tmp/temurin21.tar.gz -C /opt/java/jdk21 --strip-components=1 \
    && tar -xzf /tmp/maven.tar.gz -C /opt/maven --strip-components=1 \
    && rm -f /tmp/temurin8.tar.gz /tmp/temurin21.tar.gz /tmp/maven.tar.gz \
    && chmod +x /usr/local/bin/jdk \
    && /opt/java/jdk8/bin/javac -version \
    && /opt/java/jdk21/bin/javac -version \
    && jdk 21 mvn --version

FROM base AS build

# 构建阶段安装完整依赖并复制源码，用于编译检查、应用检查、Worker 检查和真实 OCR 预测。
COPY requirements.txt requirements-ocr.txt requirements-skill.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -i "${PIP_INDEX_URL}" -r requirements.txt \
    && python -m pip install -i "${PIP_INDEX_URL}" -r requirements-ocr.txt \
    && python -m pip install -i "${PIP_INDEX_URL}" -r requirements-skill.txt

COPY . .

# 使用示例用户表完成构建自检；该文件不会复制到最终运行镜像。
RUN find /app -type f -name '*.sh' -exec sed -i 's/\r$//' {} + \
    && chmod +x /app/docker-entrypoint.sh /app/migrate_db.sh /app/start.sh /app/start_worker.sh \
    && mkdir -p /data/upload /data/paddle-cache /app/app/logs /tmp/jira-automation-repos \
    && python -m pip check \
    && python -m compileall -q app.py celery_worker.py extensions.py app bootstrap_schema.py init_database.py manage_users.py verify_runtime.py \
    && AUTH_USERS_FILE=/app/users.example.csv python verify_runtime.py --build \
    && AUTH_USERS_FILE=/app/users.example.csv python verify_runtime.py --ocr \
    && rm -rf /app/app/logs/* \
    && find /app -type d -name '__pycache__' -prune -exec rm -rf '{}' +

FROM base AS runtime

ARG CODEX_CLI_VERSION
ARG NPM_REGISTRY

# 最终阶段使用白名单复制运行依赖和业务文件，避免把源码仓库中的测试、环境文件和临时产物带入生产。
COPY --from=build /usr/local /usr/local
# 集成 Node.js 与 Codex CLI，使接口能够在容器内以子进程方式执行技能。
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g --registry="${NPM_REGISTRY}" "@openai/codex@${CODEX_CLI_VERSION}" \
    && node --version \
    && codex --version

COPY --from=build /app/app /app/app
COPY --from=build /app/migrations /app/migrations
COPY --from=build /app/app.py /app/app.py
COPY --from=build /app/celery_worker.py /app/celery_worker.py
COPY --from=build /app/extensions.py /app/extensions.py
COPY --from=build /app/bootstrap_schema.py /app/bootstrap_schema.py
COPY --from=build /app/init_database.py /app/init_database.py
COPY --from=build /app/manage_users.py /app/manage_users.py
COPY --from=build /app/verify_runtime.py /app/verify_runtime.py
COPY --from=build /app/docker-entrypoint.sh /app/docker-entrypoint.sh
COPY --from=build /app/migrate_db.sh /app/migrate_db.sh
COPY --from=build /app/start.sh /app/start.sh
COPY --from=build /app/start_worker.sh /app/start_worker.sh
COPY --from=build /data/paddle-cache /data/paddle-cache

RUN chmod +x /app/docker-entrypoint.sh /app/migrate_db.sh /app/start.sh /app/start_worker.sh \
    && command -v bsdtar >/dev/null \
    && command -v unzip >/dev/null \
    && command -v java >/dev/null \
    && command -v javac >/dev/null \
    && command -v mvn >/dev/null \
    && jdk 8 javac -version >/dev/null \
    && (command -v 7z >/dev/null || command -v 7zz >/dev/null || command -v 7za >/dev/null \
        || echo "提示：镜像内没有 7-Zip 系命令，7z/rar 归档将由 bsdtar 处理") \
    && mkdir -p /data/upload /data/skills /data/codex /data/skill-workspace /app/app/logs /tmp/jira-automation-repos

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python verify_runtime.py --runtime || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["sh", "-c", "gunicorn -w ${WEB_CONCURRENCY:-4} -k ${GUNICORN_WORKER_CLASS:-gthread} --threads ${WEB_THREADS:-8} --timeout ${GUNICORN_TIMEOUT:-300} --bind 0.0.0.0:${API_PORT:-5000} 'app:create_app()'"]
