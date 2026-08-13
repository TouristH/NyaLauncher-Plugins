# 管理员版本审核

管理员审核按插件版本和 Release 资产的精确 SHA-256 绑定：

```text
reviews/
└─ dev.example.toolbox/
   └─ 1.0.0.json
```

只有 `repository.json` 中 `trustedReviewers` 列出的维护者可以提交或修改审核文件。
生成器只会在 `pluginId`、`version` 和 `sha256` 与收录版本完全一致时，把审核合并为
公开索引中的 `release.review.status = "verified"`。源字段 `reviewer` 会映射为
`reviewedBy`；源文件的 `schemaVersion`、`pluginId` 和 `version` 不会进入公开审核对象。

审核表示维护者核对了该固定二进制资产及其元数据，并不表示插件代码绝对安全，也
不替代用户对插件来源和能力授权的判断。请从 `templates/review.json` 复制模板。
