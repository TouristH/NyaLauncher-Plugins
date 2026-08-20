# 向 NyaLauncher 插件中心发布插件

插件中心采用“作者仓库发布、中心仓库索引”的方式。作者不把 DLL 或 ZIP 提交到这里，
也不需要为每个版本修改中心仓库；中心仓库从作者仓库根目录的 `_manifest.json` 同步有界完整
发行历史，并把通过验证的缺失版本追加到中心目录。

## 1. 准备插件包

请先阅读
[NyaLauncher 第三方插件开发规范（API v1）](https://github.com/redstore-noob/NyaLauncher/blob/testplug/NyaLauncher.Plugin.Abstractions/README.md)。

发行 ZIP 必须满足：

- ZIP 根目录直接包含 `plugin.json`，不能再套一层目录。
- 包内包含 `entryAssembly` 指向的 DLL 和需要的私有依赖。
- 不得打包 `NyaLauncher.Plugin.Abstractions.dll`。
- `plugin.json` 必须明确填写稳定的小写反向域名 ID、严格 SemVer 版本、`apiVersion: "1.0"`
  和 `minimumLauncherVersion`。
- SemVer 的 major/minor/patch 与纯数字预发布标识不能超过 `2147483647`，以匹配启动器解析范围。
- 必要与可选能力必须真实、完整，并与 `_manifest.json` 一致。
- 包内路径不得穿越、包含符号链接、Windows 保留名或大小写冲突。
- 设置项的 `pattern` 会交给与启动器一致的 .NET Regex 编译器验证；本地运行完整验证需要
  PowerShell 7（`pwsh`）。

## 2. 创建固定 GitHub Release

在插件自己的公开 GitHub 仓库创建版本 Release 并上传 ZIP。必须使用固定标签的资产 URL：

```text
https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>.zip
```

不接受 `latest`、分支文件、Actions 临时产物或可以原地覆盖的下载地址。记录 ZIP 的精确大小和
小写 SHA-256：

```powershell
(Get-Item .\plugin.zip).Length
(Get-FileHash .\plugin.zip -Algorithm SHA256).Hash.ToLowerInvariant()
```

```bash
stat -c %s plugin.zip
sha256sum plugin.zip
```

## 3. 添加 `_manifest.json`

将 [`templates/_manifest.json`](templates/_manifest.json) 复制到插件仓库根目录。示例：

```json
{
  "$schema": "https://raw.githubusercontent.com/TouristH/NyaLauncher-Plugins/main/schemas/publisher-manifest-v1.schema.json",
  "manifest_version": 1,
  "id": "io.github.example.toolbox",
  "name": "Example Toolbox",
  "description": "示例工具插件。",
  "authors": ["Example Team"],
  "license": "MIT",
  "repository_url": "https://github.com/example/nya-toolbox",
  "maintainers": ["example"],
  "categories": ["utilities"],
  "releases": [
    {
      "version": "1.2.0",
      "channel": "stable",
      "published_at": "2026-08-20T12:00:00Z",
      "release_notes_url": "https://github.com/example/nya-toolbox/releases/tag/v1.2.0",
      "download": {
        "url": "https://github.com/example/nya-toolbox/releases/download/v1.2.0/io.github.example.toolbox-1.2.0.zip",
        "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "size": 123456
      },
      "api_version": "1.0",
      "minimum_launcher_version": "0.1.1",
      "required_capabilities": ["ui.components"],
      "optional_capabilities": []
    }
  ]
}
```

顶层名称、描述、作者、许可证、仓库、维护者和分类是首次收录后不可修改的稳定元数据。
`releases` 必须按严格 SemVer 升序保留作者发布过的完整版本历史；每个版本分别声明兼容性、能力和
固定 ZIP。不得删除已被中心看到的版本，也不得修改同版本 URL、大小、哈希、兼容性或能力。
每个版本的元数据必须与其 ZIP 根目录中的 `plugin.json` 一致。`maintainers` 使用 GitHub 登录名；
`categories` 可用值为：

```text
appearance automation gameplay integration launch management utilities
```

需要声明最大兼容版本时，在对应 `releases[]` 项添加 `maximum_launcher_version_exclusive`，其值为严格 SemVer。

单份清单最多包含 128 个版本，所有 `download.size` 合计最多 4 GiB。同步器每轮从尚未收录的
版本中优先选择最新版本，再按 SemVer 降序选取；单轮最多验证 16 个、声明大小合计最多 512 MiB。
批内所有 ZIP 全部通过后才会一起写入；其余较旧版本由后续轮次继续回填。

## 4. 自动首次收录

自动收录的插件 ID 必须使用仓库所有者可验证的 GitHub 名空间：

```text
io.github.<仓库 owner 小写>.<插件名>
```

例如 `https://github.com/example/nya-toolbox` 使用 `io.github.example.toolbox`。这条规则防止公开
Topic 仓库抢占别人的插件 ID；需要使用自有域名 ID 时，应先由管理员核验域名所有权。
首次收录还会把 GitHub 的 `repositoryId` 与 `ownerId` 固定到 `plugins.json`。仓库或账号改名、
转移以及旧路径被重新占用都不会自动继承发布权，必须由管理员核验并迁移身份记录。

完成 Release 与 `_manifest.json` 后，给作者仓库添加 `nyalauncher-plugin` Topic。定时机器人会：

1. 搜索公开、非 Fork、未归档仓库；
2. 下载并静态校验 `_manifest.json`；
3. 在全局资源预算内验证固定 ZIP、SHA-256 与运行时清单；
4. 创建同仓 `registry-bot/sync` PR；
5. 在可信策略与完整测试通过后自动合并。

成功收录的版本立即出现在索引中，但默认是“未审核”。不需要管理员输入 `/validate` 或
`/approve`。GitHub Topic 索引延迟时，可以创建
[Plugin Queue Issue](../../issues/new?template=add-plugin.yml)；Issue 候选在同一有界机器人任务中优先
处理，不会因为打开 Issue 就单独触发大文件下载。

状态含义：

| 状态 | 含义 |
| --- | --- |
| `queued-for-intake` | 已进入自动收录队列 |
| `validation-failed` | 清单或固定包不合规；修复后关闭并重新打开 Issue 以重新排队 |
| `pending-merge` | 机器人 PR 正在等待必需检查与自动合并 |
| `listed-unreviewed` | 已收录，但尚无管理员绿色审核标志 |

## 5. 发布后续版本

1. 增加版本号并重新构建插件。
2. 创建新的固定 GitHub Release 和 ZIP 资产。
3. 将新版本项按 SemVer 升序追加到 `_manifest.json` 的 `releases[]`；不得替换或删除旧项。
4. 等待定时同步；仓库维护者也可手动运行 `Refresh publisher manifests` workflow_dispatch。

同步器只会新增 `plugins/<id>/releases/<version>.json`。已有版本的 URL、大小、SHA-256、
兼容性或能力声明不能被覆盖。常规定时同步不会反复下载既有 ZIP；若作者在同一 URL 重传资产，
启动器会因中心固定 SHA-256 不匹配而拒绝安装，维护者也可运行
`python tools/validate.py --check --verify-assets` 做全量历史审计，再撤回版本或审核标志。
单个作者仓库暂时不可访问或清单无效时，同步器会保留其原历史并发出告警，同时继续处理其他插件；
任何新候选仍必须先通过固定 ZIP、哈希和完整包校验，失败的候选不会写入历史。

## 6. 下架与安全问题

不要删除历史版本。创建 Remove/Yank Issue，维护者会将相关版本设为 `yanked: true` 并写明原因。
启动器不会提供已撤回版本的安装入口，但历史记录与发布者 numeric identity 指针仍保留用于审计和
发现后续修复版本。

管理员审核是独立流程。作者可创建 Review Issue，机器人会显示中心记录的 canonical SHA-256。
管理员完成源码、依赖、能力和行为检查后执行完整命令：

```text
/verify io.github.example.toolbox@1.2.0 sha256:<64 位小写哈希> 可选审核说明
```

只有 `repository.json.trustedReviewers` 中的账号可执行。审核机器人不会信任可编辑的 Issue 正文，
而是再次用命令中的 ID、版本和 SHA-256 查询中心历史、重新下载固定 ZIP 并完整验证，然后通过
`registry-bot/review/*` PR 写入 `reviews/<id>/<version>.json`。撤销绿色标志使用：

```text
/revoke-review io.github.example.toolbox@1.2.0 sha256:<同一哈希> 撤销原因
```

## Pull Request

普通插件发布使用 Topic 或 Issue 兜底，避免多人同时修改中心索引。自动收录、版本同步、撤回与审核
均由同仓机器人 PR 写入；普通 Pull Request 主要用于维护工具和文档。CI 会阻止普通贡献者直接更改
生成索引、历史版本或审核记录。

仓库管理员应保护 `main`：所有人工变更必须走 PR，并将
`Enforce trusted registry policy / policy` 与 `Validate registry / validate` 设为必需检查。
前者通过 `pull_request_target` 运行主分支上的可信策略代码，不会检出或执行 PR 中的脚本；
不要用普通 PR 自己修改后的校验脚本替代它。必需检查必须启用“合并前分支必须为最新”
（strict/up-to-date），避免旧 PR 沿用旧版可信名单或策略结果。当前工作流未声明 `merge_group`，
因此在补齐对应触发器前不要启用 Merge Queue。

写入身份是只安装在本仓库的 GitHub App。工作流完成所有不可信输入处理和回归测试后，才用
`NYA_REGISTRY_APP_CLIENT_ID` 与 `NYA_REGISTRY_APP_PRIVATE_KEY` 申请一小时短期 installation token，
向 `registry-bot/*` 分支推送并创建 PR。App 不得获得 `main` bypass；合并必须经过 base-owned policy、
完整 validator 和 strict/up-to-date 规则。Issue 标签与评论继续使用权限受限的 `GITHUB_TOKEN`。
另需用 ruleset 将 `registry-bot/**` 分支的创建和更新限制为该 App；policy 还会核对每次 PR 事件的
sender，防止普通写权限协作者接管已有机器人 PR。

不要配置长期 `NYA_REGISTRY_WRITER_TOKEN`，也不要给普通维护者或通用 Actions 身份直接推送权限。
完整部署步骤见 [docs/REGISTRY_BOT.md](docs/REGISTRY_BOT.md)。

## 运维提醒

GitHub 会在公开仓库连续 60 天没有仓库活动后自动停用定时工作流。管理员应监控
`Refresh publisher manifests` 最近一次成功时间，并在停用后重新启用；需要无人值守保证时，使用
仓库外的受信 watchdog 定期触发 `workflow_dispatch`。参见
[GitHub 官方说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)。
