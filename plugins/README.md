# 插件条目目录

每个插件使用一个与插件 ID 完全相同的直接子目录：

```text
plugins/
└─ dev.example.toolbox/
   ├─ plugin.json
   └─ releases/
      ├─ 1.0.0.json
      └─ 1.1.0.json
```

这个目录由中心仓库同步器维护。插件作者在自己的仓库根目录以升序 `releases[]` 维护
`_manifest.json` 的完整历史。同步器分批验证缺失的固定 GitHub Release ZIP 后，只新增对应的
版本 JSON；不要直接提交 ZIP、DLL、Git submodule，也不要手工覆盖已经收录的版本。

`plugin.json` 保存当前展示元数据，`releases/` 保存完整且不可变的版本历史。
版本级管理员审核位于根目录 `reviews/`，只能由 `repository.json` 中的可信审核者维护。
