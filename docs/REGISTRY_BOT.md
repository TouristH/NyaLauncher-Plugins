# NyaLauncher 收录机器人配置

插件自动收录使用 GitHub App，而不是个人账号 PAT。工作流只在完成清单、固定 ZIP 和回归测试后
申请安装令牌；令牌最长有效一小时，任务结束后由官方 Action 撤销。

## 1. 创建 GitHub App

在仓库所有者账号下创建名为 `NyaLauncher Registry Bot` 的 GitHub App，建议 slug 为
`nyalauncher-registry-bot`。如果 GitHub 分配了不同 slug，请先把 `repository.json` 中的
`registryBotLogin` 改为实际的 `<slug>[bot]`。

配置：

- Homepage URL：本插件中心仓库地址。
- Webhook：关闭；机器人完全由 Actions 调度。
- Repository permissions：
  - Contents: Read and write
  - Pull requests: Read and write
  - Metadata: Read-only（GitHub 自动包含）
- Account permissions：无。

只把 App 安装到 `TouristH/NyaLauncher-Plugins`，不要授予其他仓库。

## 2. 保存凭据

在插件中心仓库设置：

- Actions variable `NYA_REGISTRY_APP_CLIENT_ID`：App 的 Client ID；
- Actions secret `NYA_REGISTRY_APP_PRIVATE_KEY`：完整 PEM 私钥，包括 BEGIN/END 行。

工作流使用 GitHub 官方 `actions/create-github-app-token` 生成短期 installation token。不要再创建
`NYA_REGISTRY_WRITER_TOKEN`，也不要把个人 PAT 粘贴到 Issue、日志或 workflow 文件中。

## 3. 保护 main

启用仓库的 Allow auto-merge，然后给 `main` 配置 ruleset：

- 所有变更必须通过 Pull Request；
- 必需检查：`Enforce trusted registry policy / policy` 与
  `Validate registry / validate`；
- 要求分支在合并前为最新；
- 不给 GitHub App、GitHub Actions 或普通维护者 bypass；
- 暂不启用 Merge Queue，除非两项 workflow 同时增加 `merge_group` 触发器。

机器人只向 `registry-bot/*` 分支推送并创建 PR。主分支上的可信策略会同时核对机器人登录名、
同仓来源、触发该 PR 更新的账号、分支用途和可修改路径；App 本身不能直接推送 `main`。

再为 `registry-bot/**` 建立分支 ruleset，限制只有这个 GitHub App 可以创建和更新这些分支。
这样即使某个普通协作者拥有 Contents 写权限，也不能接管机器人已经打开的 PR head。

## 4. 验证部署

1. 手动运行 `Refresh publisher manifests`。
2. 确认机器人创建同仓 PR，而不是直接修改 `main`。
3. 确认 `policy` 与 `validate` 均成功后 PR 自动合并并删除机器人分支。
4. 用一个无审核版本确认启动器显示风险警告。
5. 在 Review Issue 中执行机器人给出的 `/verify id@version sha256:...`，确认第二个机器人 PR
   合并后启动器显示绿色审核标志。

GitHub 官方的 App Actions 认证说明：
[Making authenticated API requests with a GitHub App in GitHub Actions](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/making-authenticated-api-requests-with-a-github-app-in-a-github-actions-workflow)。
