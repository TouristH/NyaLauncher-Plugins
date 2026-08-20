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
  "id": "dev.example.toolbox",
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
        "url": "https://github.com/example/nya-toolbox/releases/download/v1.2.0/dev.example.toolbox-1.2.0.zip",
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

## 4. 使用 Issue 首次收录

创建 [Add Plugin Issue](../../issues/new?template=add-plugin.yml)，填写：

- 与 `_manifest.json` 完全一致的插件 ID；
- 插件公开 GitHub 仓库根地址。

Issue 创建后保持 `pending-validation`。可信维护者确认来源后输入 `/validate`，工作流才会读取默认
分支根目录的 `_manifest.json`、下载本轮有界 Release ZIP 并检查哈希与包内容。公网作者输入命令
不会触发重资产下载。
状态含义：

| 状态 | 含义 |
| --- | --- |
| `pending-validation` | 等待验证 |
| `validated` | 技术验证通过，等待维护者决定是否收录 |
| `validation-failed` | 验证失败；修复作者仓库后请维护者再次输入 `/validate` |
| `approved` | 已加入中心仓库 |
| `rejected` | 维护者拒绝收录 |

维护者使用 `/approve` 批准，或使用 `/reject 原因` 拒绝。技术验证通过只代表包符合规则，
并不自动获得绿色“管理员已审核”标志。

## 5. 发布后续版本

1. 增加版本号并重新构建插件。
2. 创建新的固定 GitHub Release 和 ZIP 资产。
3. 将新版本项按 SemVer 升序追加到 `_manifest.json` 的 `releases[]`；不得替换或删除旧项。
4. 等待每 6 小时的定时同步；仓库维护者也可手动运行 `Refresh publisher manifests` workflow_dispatch。

同步器只会新增 `plugins/<id>/releases/<version>.json`。已有版本的 URL、大小、SHA-256、
兼容性或能力声明不能被覆盖。常规定时同步不会反复下载既有 ZIP；若作者在同一 URL 重传资产，
启动器会因中心固定 SHA-256 不匹配而拒绝安装，维护者也可运行
`python tools/validate.py --check --verify-assets` 做全量历史审计，再撤回版本或审核标志。
单个作者仓库暂时不可访问或清单无效时，同步器会保留其原历史并发出告警，同时继续处理其他插件；
任何新候选仍必须先通过固定 ZIP、哈希和完整包校验，失败的候选不会写入历史。

## 6. 下架与安全问题

不要删除历史版本。创建 Remove/Yank Issue，维护者会将相关版本设为 `yanked: true` 并写明原因。
启动器不会提供已撤回版本的安装入口，但历史记录仍保留用于审计。

管理员审核是独立流程。只有 `repository.json.trustedReviewers` 中的维护者可以创建或撤销
`reviews/<id>/<version>.json`；审核精确绑定该版本 ZIP 的 SHA-256。

## Pull Request

普通插件提交请使用 Issue，避免多人同时修改中心索引。Pull Request 主要用于维护仓库工具、
文档和由可信审核者添加审核记录；CI 会阻止普通贡献者直接更改生成索引或历史版本。

仓库管理员应保护 `main`：所有人工变更必须走 PR，并将
`Enforce trusted registry policy / policy` 与 `Validate registry / validate` 设为必需检查。
前者通过 `pull_request_target` 运行主分支上的可信策略代码，不会检出或执行 PR 中的脚本；
不要用普通 PR 自己修改后的校验脚本替代它。必需检查必须启用“合并前分支必须为最新”
（strict/up-to-date），避免旧 PR 沿用旧版可信名单或策略结果。当前工作流未声明 `merge_group`，
因此在补齐对应触发器前不要启用 Merge Queue。

`approve-issue.yml` 与 `refresh.yml` 是唯一需要直接追加生成数据的写入流程。上线前必须创建受保护的
`registry-writer` Environment，并在其中配置 `NYA_REGISTRY_WRITER_TOKEN`：它应是专用机器人账号的
fine-grained token，只授予本仓库 Contents 写入权限。Issue 标签、评论和读取公开 Release 继续使用
权限受限的工作流 `GITHUB_TOKEN`。checkout 不持久化凭据；专用令牌只在最终 push 步骤中临时注入，
该步骤禁用 Git hooks，退出时会清除本地认证头。规则集只给这个机器人身份 bypass；
普通 `GITHUB_TOKEN` 无法把 bypass 精确限制到某两个工作流。Environment 只允许 `main` 上的受信工作流
使用，但不要设置会让定时同步永久等待的人工审批门。

不要给普通维护者、通用 Actions 身份或无关第三方 App 开放直接推送。若暂时无法配置专用机器人，
应把写入改为由机器人创建 PR，而不是关闭 `main` 保护。

## 运维提醒

GitHub 会在公开仓库连续 60 天没有仓库活动后自动停用定时工作流。管理员应监控
`Refresh publisher manifests` 最近一次成功时间，并在停用后重新启用；需要无人值守保证时，使用
仓库外的受信 watchdog 定期触发 `workflow_dispatch`。参见
[GitHub 官方说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)。
