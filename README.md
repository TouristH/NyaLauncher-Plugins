# NyaLauncher 插件中心

[![Validate registry](https://github.com/TouristH/NyaLauncher-Plugins/actions/workflows/validate.yml/badge.svg)](https://github.com/TouristH/NyaLauncher-Plugins/actions/workflows/validate.yml)
[![NyaLauncher](https://img.shields.io/badge/NyaLauncher-testplug-7c4dff)](https://github.com/redstore-noob/NyaLauncher/tree/testplug)

NyaLauncher 的独立社区插件索引。插件代码和安装包仍由作者自己的 GitHub 仓库托管，
本仓库只负责收录来源、保存不可变版本历史、验证发行包并生成启动器读取的静态索引。

插件开发规范以
[NyaLauncher Plugin Abstractions（testplug）](https://github.com/redstore-noob/NyaLauncher/blob/testplug/NyaLauncher.Plugin.Abstractions/README.md)
为准。

> 收录不等于代码安全审核。插件是与启动器同进程运行的 .NET 代码，能力授权不是操作系统沙箱。

## 工作方式

本仓库结合了 GitHub Topic 自动发现、Issue 兜底与固定 Release 包三种机制：

1. `plugins.json` 登记插件 ID、作者仓库地址及 GitHub 不可复用的仓库/所有者数字 ID。
2. 插件作者在自己仓库根目录维护 `_manifest.json`，以升序 `releases[]` 保留完整发行历史。
3. 每个发行版必须是不可变的 GitHub Release ZIP，并声明精确 URL、字节数和 SHA-256。
4. 收录机器人搜索 `nyalauncher-plugin` Topic，并自动下载 ZIP，检查包根 `plugin.json`、
   入口 DLL、兼容性、能力声明和安全路径。
5. 验证通过的新版本只会追加到该插件自己的历史目录，不会覆盖旧版本。
6. `public/v1/index.json` 由历史目录和管理员审核记录确定性生成，供启动器下载。

```text
plugins/<plugin-id>/
├─ plugin.json
└─ releases/
   ├─ 1.0.0.json
   ├─ 1.1.0.json
   └─ 2.0.0.json
```

因此，一个插件始终对应一个完整目录；启动器可以展示并选择仍兼容、未撤回的历史版本。

## 提交插件

正常发布不需要 Fork 中心仓库，也不需要等待管理员批准：

1. 在作者仓库创建固定 GitHub Release，上传 ZIP。
2. 在作者仓库根目录添加 `_manifest.json`（示例见
   [`templates/_manifest.json`](templates/_manifest.json)）。
3. 插件 ID 使用 `io.github.<仓库所有者小写>.<名称>`，并给仓库添加
   `nyalauncher-plugin` Topic。
4. 机器人自动发现、严格验证并通过机器人 PR 收录；收录时默认没有绿色审核标志。

GitHub Topic 索引可能短暂延迟。需要立即进入队列时可创建
[Plugin Queue Issue](../../issues/new?template=add-plugin.yml)；它仍由机器人自动处理，不需要
管理员输入 `/validate` 或 `/approve`。

首次收录后，作者发布新版本只需创建新的 Release，并把新项追加到 `_manifest.json` 的完整
`releases[]`。同步任务会以有界批次将缺失版本追加到 `plugins/<id>/releases/`，不会因两次采样间
连续发布而漏版。同一个版本号的 URL、大小、哈希或兼容信息一旦进入中心仓库便不可更改；需要修复时
必须发布新版本号。

完整要求见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 收录与审核是两件事

- **已收录（listed）**：版本通过格式、哈希、ZIP 与运行时清单验证，可以出现在仓库中；不代表管理员读过代码。
- **管理员已审核（verified）**：可信审核者审查了精确的插件 ID、版本和 ZIP SHA-256，启动器显示绿色标志。
- **未经审核**：启动器显示醒目警告，并在下载前要求用户再次确认风险。

审核记录位于 `reviews/<插件 ID>/<版本>.json`。管理员完成代码与行为检查后，在 Review Issue
中执行机器人给出的完整命令：

```text
/verify io.github.example.toolbox@1.2.0 sha256:<中心记录的 64 位哈希>
```

命令中的 ID、版本和 SHA-256 会再次与中心历史核对，固定 ZIP 也会重新下载验证。撤销审核使用
`/revoke-review ...`；它只移除绿色标志，不删除历史。若同版本资产被替换，审核不会迁移，
启动器与审核机器人都会拒绝不匹配的字节。

## 收录机器人

自动写入由仅安装在本仓库的 GitHub App 完成。工作流在所有不可信清单和 ZIP 验证结束后才申请
一小时短期安装令牌，创建同仓机器人 PR，并交由 `policy` 与 `validate` 两项检查后自动合并；
机器人没有绕过 `main` 规则的权限。部署步骤见 [机器人配置文档](docs/REGISTRY_BOT.md)。

启用机器人前，仓库管理员必须完成以下远端配置：

1. 创建 GitHub App，并且只安装到本插件中心仓库；
2. 配置 `NYA_REGISTRY_APP_CLIENT_ID` 变量和 `NYA_REGISTRY_APP_PRIVATE_KEY` Secret；
3. 开启仓库 Auto-merge；
4. 保护 `main`，将 `policy` 与 `validate` 设为必须且要求分支保持最新；
5. 限制 `registry-bot/**` 只能由该 GitHub App 创建和更新。

不要配置长期个人 PAT，也不要给予机器人直接推送 `main` 的 bypass。缺少任一项时，工作流会
安全失败，不会退化为跳过验证直接写入。

## 公开文件

- [`plugins.json`](plugins.json)：受监控作者仓库及其 GitHub numeric identity 列表。
- [`plugin_details.json`](plugin_details.json)：从完整历史目录生成的展示数据。
- [`public/v1/index.json`](public/v1/index.json)：NyaLauncher 使用的严格 v1 索引。

启动器固定索引地址：

```text
https://raw.githubusercontent.com/TouristH/NyaLauncher-Plugins/main/public/v1/index.json
```

## 许可证

仓库工具与元数据格式使用 [MIT](LICENSE)。插件及其二进制包仍按各自声明的许可证发布。
