# 测试用例模板工具

该目录提供脱敏的 Excel/Xray CSV 模板生成与独立校验工具。

## 生成示例模板

```bash
python generate-test-case-templates.py --example
```

生成：

- `test-case-template.xlsx`
- `xray-test-case-import-template.csv`

示例统一使用 `DEMO-123` 和虚构订单业务数据。

## 使用边界

该生成器只维护仓库中的 `DEMO-123` 脱敏示例，不接受真实 Jira Key，也不能用于正式业务附件。

正式测试用例必须通过 `/qa-cases`，基于人工复核后的测试方案和人工审核后的 XMind 生成，再使用本目录的独立校验器检查。

## 独立校验

```bash
python validate-test-case-templates.py \
  test-case-template.xlsx \
  xray-test-case-import-template.csv \
  DEMO-123
```

校验内容：

- CSV UTF-8 BOM 和固定 10 列表头。
- Excel 必需列与 CSV 行数一致。
- ID 从 `TC001` 连续递增、非空且唯一。
- Excel 用例编号与 CSV `External ID` 集合及顺序一致。
- Excel“模块/分类”与 CSV `Test Repository Path` 逐行一致，按 `需求名称 / XMind业务层级 / 测试分类` 组织路径，并排除通用菜单包装层级和 `>`。
- Excel“测试数据”与 CSV `Data` 逐行一致，空值保持为空。
- Excel/CSV `Requirements` 全部等于目标 Jira Key。
- Xray 导入标准版本为 `3`：固定 10 列，CSV `Data` 映射 Xray `Data`，`Requirements` 映射 `Link "Tests"`，Repository Path 以需求名称为根并继承 XMind 业务层级。
