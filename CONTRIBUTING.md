# 贡献指南

感谢为 NyaLauncher 社区提供插件。

## PR 检查清单

- [ ] 插件 ID 是稳定的小写反向域名，目录名与 ID 完全一致。
- [ ] `repositoryUrl` 指向我有权维护的 GitHub 仓库。
- [ ] 下载链接指向该仓库中的固定版本 GitHub Release 资产。
- [ ] ZIP 根目录直接包含有效的 NyaLauncher `plugin.json` 和入口 DLL。
- [ ] 版本 JSON、包内 `plugin.json` 的 ID/版本/兼容性/能力一致。
- [ ] `size` 是精确字节数，`sha256` 是下载资产的小写 SHA-256。
- [ ] 我没有提交 DLL、ZIP、Git submodule、密钥或私人信息。
- [ ] 我运行了 `python tools/validate.py --write` 和 `python tools/validate.py --check --verify-assets`。
- [ ] 一次 PR 只涉及一个插件或一个新增版本。
- [ ] 如果我不是 `trustedReviewers` 中的审核者，我没有提交或修改 `reviews/`。

维护者可能要求补充构建来源、许可证或安全说明。中心仓库不会执行插件代码，
收录也不代表 NyaLauncher 维护者为第三方代码提供安全保证。

## 版本审核（仅可信审核者）

审核应在插件条目已合并且 Release 资产固定后，通过独立 PR 添加：

```text
reviews/<插件 ID>/<严格 SemVer 版本>.json
```

从 `templates/review.json` 复制文件，逐项核对 `pluginId`、`version`、小写 SHA-256、
审核者 GitHub 登录名和 UTC 秒级时间。`reviewer` 必须位于目标分支
`repository.json` 的 `trustedReviewers` 中。校验器只会把与 Release 哈希完全一致的
审核合并进索引，并映射为启动器契约中的 `reviewedBy`。

可信审核者撤销审核时可以删除对应审核文件，同时应在同一 PR 将存在风险的版本设为
`yanked: true` 并填写 `yankReason`。历史版本的其他字段不可改写；修复内容必须发布新版本。
