# Security Policy

## 报告问题

请不要在公开 Issue 中披露可直接利用的恶意插件、凭据泄漏或供应链漏洞。请通过仓库的
GitHub Security Advisory 私密报告，并附上插件 ID、版本、Release URL、SHA-256 和复现步骤。

一般的失效下载、兼容问题或作者主动下架可以使用 Remove/Yank Issue。

## 信任边界

- 插件与 NyaLauncher 在同一进程运行，能力授权不是系统沙箱。
- `listed` 只表示元数据、固定下载、哈希、ZIP 结构和包内清单通过自动验证。
- `verified` 表示可信审核者审查了精确的 `pluginId + version + SHA-256`，不是绝对安全保证。
- 未审核版本仍可被收录，但启动器会显示警告并在下载前要求用户确认。
- Topic 自动发现只接受 `io.github.<仓库所有者>.` 名空间；ID 或历史仓库冲突会停止自动收录，
  不会按“先提交者优先”覆盖现有身份。
- 中心同时固定 GitHub `repositoryId + ownerId`；owner/repo 路径改名、转移或被重新占用时会停止
  自动同步并要求人工迁移，不会只凭可复用的 URL 继承发布权。

作者仓库的 `_manifest.json` 可以更新到新版本，但每个版本必须指向固定 GitHub Release ZIP。
中心仓库会保留历史版本并拒绝修改已记录版本；同版本包被替换时，启动器下载校验或维护者运行
`python tools/validate.py --check --verify-assets` 会发现不匹配。中心记录不会被覆盖，已有审核也不会
迁移到新字节。定时同步只严格验证本轮新增候选，不会反复下载全部历史 ZIP。

中心写入由仅安装在本仓库的 GitHub App 使用短期 installation token 创建机器人 PR；App 不得绕过
`main` 的可信策略和完整验证检查。插件清单与 ZIP 的处理阶段不持有长期个人 PAT。

## 应急处理

可信维护者可以：

1. 将受影响版本标记为 `yanked`，保留原因和历史记录；
2. 删除对应审核记录，立即撤销启动器绿色标志；
3. 在确认修复后要求作者使用新的 SemVer 版本和新的固定 Release ZIP 发布。

不得通过改写旧版本 URL 或哈希来“修复”已经发布的记录。
