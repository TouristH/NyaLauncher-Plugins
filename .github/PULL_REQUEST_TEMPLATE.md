## 收录内容

- 插件 ID：
- 新版本：
- 插件仓库：
- Release 页面：

## 作者确认

- [ ] 我已阅读 `CONTRIBUTING.md`。
- [ ] 我有权分发该插件和 Release 资产。
- [ ] 此 PR 只提交一个插件或一个新版本。
- [ ] Release URL 固定，ZIP 的 size 和 SHA-256 已复核。
- [ ] ZIP 根目录直接包含 `plugin.json` 和入口 DLL。
- [ ] 我已运行 `python tools/validate.py --write` 及 `--check --verify-assets`。
- [ ] 我没有冒用管理员身份提交审核；`reviews/` 只由 `trustedReviewers` 中的账号维护。
- [ ] 我理解收录不等于安全审计，插件会在启动器进程内运行。

## 可信审核者确认（仅审核 PR）

- [ ] 此审核对应已收录的插件 ID 和严格 SemVer 版本。
- [ ] 我从固定 Release 资产重新计算并核对了 SHA-256。
- [ ] `reviewer` 是我的 GitHub 登录名，`reviewedAt` 是 UTC 秒级时间。
- [ ] 我已重新生成索引，并确认 `release.review.reviewedBy` 与启动器契约一致。
