# 故障分析范围映射

`scope-mapping.json` 把 Jira 信息解析成故障分析接口需要的仓库和 Git Tag：

- Jira 提供产品：`问题发现产品`，例如 `see`、`pal`、`bot`。
- Jira 组件、调用方输入或评论固定块提供模块，例如 `see-task`。
- Jira 影响版本/修复版本/环境、调用方输入或评论固定块提供版本，例如 `3.2.1` 或 `v3.2.1`。
- 本文件提供 `产品 -> 模块 -> 仓库地址` 与 `版本 -> Git Tag` 的规则。故障分析只关注 Tag，不使用 `master`、`feature/*` 等分支。

评论固定块只在 Jira 字段缺失或存在多个候选时使用，格式固定为：

```text
[FAULT_ANALYSIS]
see-task:v1.2.3
see-management:v1.2.3
```

第一条是主模块，后续条目作为关联模块一起分析。需要分析或拉取的非公共模块必须写成 `模块:Tag`，模块名和精准 Tag 都不能省略；公共组件不单独写进固定块。`repo=<仓库地址>` 可作为临时覆盖项；正常情况下不要写仓库地址，仓库地址维护在本配置里。

主模块和非公共关联模块的 Tag 必须是 GitLab 中真实存在的精准 Tag。分析前会通过故障分析服务执行远端校验；Tag 不存在、不可访问或无法精确匹配时会按“缺少分析参数”打回，不会改用 `master`、`feature/*` 或默认分支。

公共组件模块可增加 `"common": true`。该标记表示模块性质并保留仓库映射，供代码分析按需联动使用；公共组件不写进报告人的评论固定块，不要求发布或提供精准 Tag。主模块和普通关联模块的代码或日志确认引用某个公共组件后，后端会自动拉取该组件的默认分支参与分析；未命中的公共组件不会拉取。拉取失败会记录为跳过证据，不阻断主分析。分析确认代码引用公共组件时，在结论中说明引用关系和关键代码位置即可。

## 版本映射

如果发布 Tag 和 Jira 版本不是同名，优先用精确映射维护：

```json
{
  "products": {
    "yicall": {
      "modules": {
        "yicall-task": {
          "repoUrl": "https://code.in.wezhuiyi.com/yicall/yicall-task.git",
          "versionMap": {
            "6.1.15": "v6.1.15-CallZhaoshangzhengquan-rc33"
          }
        }
      }
    }
  }
}
```

`versionMap` 可以放在模块、产品或文件顶层；查找顺序是模块 > 产品 > 全局。精确映射未命中时，才继续按 `versionRules` 做正则转换。

## 版本规则

规则按顺序匹配，第一条命中的规则生效。模板支持 `{version}`、`{1}` 和命名捕获组。

```json
{
  "versionRules": [
    {
      "name": "semver-to-release",
      "pattern": "^([0-9]+\\.[0-9]+\\.[0-9]+)$",
      "ref": "release-{1}"
    },
    {
      "name": "exact-ref",
      "pattern": "^.+$",
      "ref": "{version}"
    }
  ]
}
```

上例把 Jira 版本 `3.2.1` 转成 Tag `release-3.2.1`；其他版本保持原值，要求 Git 中存在同名 Tag。

## 人工覆盖

`analysis-cli.mjs` 支持显式覆盖：

```bash
node scripts/analysis-cli.mjs resolve \
  --product see \
  --module see-task \
  --version 3.2.1

node scripts/analysis-cli.mjs analyze \
  --dir /data/skill-workspace/work/defect-gate/KEY-1234/attachments \
  --product see \
  --module see-task \
  --version 3.2.1 \
  --branch https://code.in.wezhuiyi.com/see/see-task.git
```

`--branch` 只用于明确指定仓库地址；版本仍按 `versionRules` 转换。配置缺失或匹配到多个模块时，脚本会失败并列出候选，不会模糊选择。
