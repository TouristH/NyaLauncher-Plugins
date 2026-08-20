# 管理员审核记录

本目录只允许 `repository.json.trustedReviewers` 中的可信审核者修改。

```text
reviews/<plugin-id>/<strict-semver>.json
```

审核必须绑定插件 ID、版本和该固定 GitHub Release ZIP 的小写 SHA-256。启动器公开索引只会输出
`status: verified`、`reviewedBy`、`reviewedAt`、`sha256` 与可选说明；作者无法通过自己的
`_manifest.json` 写入绿色审核标志。

审核前至少应核对源仓库、依赖、能力声明、构建来源和最终 ZIP 哈希。撤销审核时记录会改为
`status: revoked`，作为防止旧命令乱序恢复绿标的持久顺序标记；公开索引不会输出该记录。
如版本存在风险，还应将对应历史版本标记为 `yanked`。
