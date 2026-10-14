#!/bin/sh
# 容器内 JDK 切换助手。
#
# 镜像同时安装了 JDK 8 与 JDK 21（/opt/java/jdk8、/opt/java/jdk21），默认 JAVA_HOME 指向 JDK 8。
# 同一份代码只能用其中一个 JDK 编译，这里提供显式选择，避免编译时悄悄用错版本。
set -eu

JAVA_ROOT="${JAVA_ROOT:-/opt/java}"

usage() {
    cat <<'TEXT'
用法：
  jdk list              列出容器内已安装的 JDK
  jdk home 8|21         打印对应 JDK 的 JAVA_HOME
  jdk 8|21 <命令...>    在指定 JDK 下执行命令，例如：jdk 21 mvn -DskipTests package

Java 8 项目（含用 lombok 1.16~1.18 的老项目）用 jdk 8，Java 21 项目用 jdk 21。

等价写法：JAVA_HOME=/opt/java/jdk8 mvn -DskipTests package
TEXT
}

resolve_home() {
    home="$JAVA_ROOT/jdk$1"
    if [ ! -x "$home/bin/java" ]; then
        echo "未安装 JDK $1：$home" >&2
        exit 1
    fi
    printf '%s' "$home"
}

case "${1:-list}" in
    list|ls)
        for dir in "$JAVA_ROOT"/jdk*; do
            [ -x "$dir/bin/java" ] || continue
            printf '%s\t%s\n' "$(basename "$dir")" "$("$dir/bin/java" -version 2>&1 | head -n 1)"
        done
        ;;
    home)
        [ "$#" -ge 2 ] || { usage >&2; exit 2; }
        resolve_home "$2"
        printf '\n'
        ;;
    8|21)
        home="$(resolve_home "$1")"
        shift
        [ "$#" -ge 1 ] || { usage >&2; exit 2; }
        JAVA_HOME="$home"
        export JAVA_HOME
        PATH="$home/bin:$PATH"
        export PATH
        exec "$@"
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
