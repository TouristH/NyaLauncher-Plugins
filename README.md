# NyaLauncher Plugins

NyaLauncher 的独立第三方插件收录仓库。启动器读取本仓库生成的静态索引，
并从插件作者自己的 GitHub Release 下载插件包。

> 收录不等于安全审计。插件与 NyaLauncher 在同一进程中运行，能力授权不是
> 操作系统沙箱。请只安装你信任来源的插件。

## 插件作者如何提交

1. 在你自己的 GitHub 仓库开发插件，并按照
   [NyaLauncher 插件开发规范](https://github.com/redstore-noob/NyaLauncher/blob/main/NyaLauncher.Plugin.Abstractions/README.md)
   生成完整插件包。
2. ZIP 根目录必须直接包含 `plugin.json`、入口 DLL 和私有依赖；不要再套一层目录，
   也不要打包 `NyaLauncher.Plugin.Abstractions.dll`。
3. 在你自己的仓库创建一个固定版本 GitHub Release 并上传 ZIP。不要使用
   `latest`、分支文件或会被覆盖的下载地址。
4. Fork 本仓库，从 `templates/` 复制模板：

   ```text
   plugins/<你的插件 ID>/plugin.json
   plugins/<你的插件 ID>/releases/<版本>.json
   ```

5. 填写 Release ZIP 的精确字节数和小写 SHA-256：

   ```powershell
   (Get-Item .\plugin.zip).Length
   (Get-FileHash .\plugin.zip -Algorithm SHA256).Hash.ToLowerInvariant()
   ```

   ```bash
   stat -c %s plugin.zip
   sha256sum plugin.zip
   ```

6. 本地运行 `python tools/validate.py --write` 和
   `python tools/validate.py --check --verify-assets`（Windows 也可使用 `py`），提交条目和
   生成后的 `public/v1/index.json`，然后发起 Pull Request。

一次 PR 只提交一个插件或一个新版本。更新插件时新增版本 JSON；已经合并的版本
下载地址、大小和哈希视为不可变。需要安全下架时请提交 Issue，由仓库维护者标记
`yanked`，不要删除历史记录。

插件作者不应提交或修改 `reviews/`。收录合并后，`repository.json` 中列出的可信审核者
可以另行添加 `reviews/<插件 ID>/<版本>.json`。审核记录同时绑定插件 ID、严格 SemVer
版本和 Release ZIP 的 SHA-256；三者任一变化都会使索引生成失败。生成索引仅输出启动器
所需的 `reviewedBy` 等审核字段，不会把源文件的 `schemaVersion`、`pluginId` 或 `version`
泄漏到 `release.review`。

## 仓库工作方式

- `plugins/` 保存可审计的插件与版本元数据，不存 DLL、ZIP 或 Git submodule。
- `tools/validate.py` 严格验证字段、来源 URL、版本、能力、哈希和目录约束，并生成
  `public/v1/index.json`；CI 还会下载未撤回的 Release，核验实际大小、SHA-256、
  ZIP 路径、包内清单和入口程序集。
- `reviews/` 保存由可信审核者签入、与固定版本哈希精确绑定的管理员审核记录。
- Pull Request CI 确认索引是确定性生成的，并根据 PR 作者身份保护审核记录、校验器、
  schema、工作流和仓库配置。
- NyaLauncher 只读取 `public/v1/index.json`，下载后仍会二次核验大小、SHA-256、
  ZIP 路径和包内运行时清单。

> `verified` 表示可信审核者核对过这一份固定哈希的包及其声明，不是代码绝对安全的保证，
> 也不是操作系统级沙箱。

## 维护者设置

GitHub 上应保护 `main` 分支：要求 Pull Request、要求 CODEOWNERS 审核、要求
`Validate registry / validate` 状态检查通过，并禁止绕过和直接推送。CI 从目标分支的
`repository.json` 读取 `trustedReviewers`，因此 PR 不能通过把自己加入列表来获得审核权限。

## 索引地址

```text
https://raw.githubusercontent.com/TouristH/NyaLauncher-Plugins/main/public/v1/index.json
```

## 许可证

仓库工具和元数据结构使用 [MIT](LICENSE) 许可证。每个插件及其二进制包仍按插件
作者声明的许可证发布。
