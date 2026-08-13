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

请从仓库根目录的 `templates/` 复制模板，不要把 ZIP、DLL 或 Git submodule
提交到这里。插件二进制文件应发布在插件作者自己的 GitHub Release 中。

插件作者只维护这里的插件与版本元数据。版本级管理员审核位于根目录 `reviews/`，
只能由 `repository.json` 中的可信审核者通过独立 PR 添加。
