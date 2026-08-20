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

本仓库结合了 Issue 收录与固定 Release 包两种机制：

1. `plugins.json` 只登记插件 ID 和作者仓库地址。
2. 插件作者在自己仓库根目录维护 `_manifest.json`，以升序 `releases[]` 保留完整发行历史。
3. 每个发行版必须是不可变的 GitHub Release ZIP，并声明精确 URL、字节数和 SHA-256。
4. 自动同步会下载 ZIP，检查包根 `plugin.json`、入口 DLL、兼容性、能力声明和安全路径。
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

推荐使用 [Add Plugin Issue](../../issues/new?template=add-plugin.yml)，无需 Fork 中心仓库：

1. 在作者仓库创建固定 GitHub Release，上传 ZIP。
2. 在作者仓库根目录添加 `_manifest.json`（示例见
   [`templates/_manifest.json`](templates/_manifest.json)）。
3. 创建 Add Plugin Issue，只填写插件 ID 和公开仓库地址。
4. 维护者输入 `/validate` 触发固定 ZIP 技术验证，通过后使用 `/approve` 收录。

首次收录后，作者发布新版本只需创建新的 Release，并把新项追加到 `_manifest.json` 的完整
`releases[]`。同步任务会以有界批次将缺失版本追加到 `plugins/<id>/releases/`，不会因两次采样间
连续发布而漏版。同一个版本号的 URL、大小、哈希或兼容信息一旦进入中心仓库便不可更改；需要修复时
必须发布新版本号。

完整要求见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 收录与审核是两件事

- **已收录（listed）**：版本通过格式、哈希、ZIP 与运行时清单验证，可以出现在仓库中；不代表管理员读过代码。
- **管理员已审核（verified）**：可信审核者审查了精确的插件 ID、版本和 ZIP SHA-256，启动器显示绿色标志。
- **未经审核**：启动器显示醒目警告，并在下载前要求用户再次确认风险。

审核记录位于 `reviews/<插件 ID>/<版本>.json`。如果同版本包的 SHA-256 发生变化，审核不会迁移，
而是使验证失败。管理员可以删除审核记录撤销绿色标志，并将风险版本标记为 `yanked`。

## 公开文件

- [`plugins.json`](plugins.json)：已收录作者仓库列表。
- [`plugin_details.json`](plugin_details.json)：从完整历史目录生成的展示数据。
- [`public/v1/index.json`](public/v1/index.json)：NyaLauncher 使用的严格 v1 索引。

启动器固定索引地址：

```text
https://raw.githubusercontent.com/TouristH/NyaLauncher-Plugins/main/public/v1/index.json
```

## 许可证

仓库工具与元数据格式使用 [MIT](LICENSE)。插件及其二进制包仍按各自声明的许可证发布。
