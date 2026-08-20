## 变更类型

- [ ] 仓库工具、Schema、工作流或文档
- [ ] 可信审核者添加或撤销 `reviews/` 记录
- [ ] 维护者执行安全撤回

普通插件首次收录请使用 **Add Plugin Issue**；后续版本由作者仓库 `_manifest.json` 自动同步，
不要手工编辑 `plugins/<id>/releases/` 或生成索引。

## 检查

- [ ] 改动范围与标题一致，没有提交 ZIP、DLL、构建目录或密钥。
- [ ] 我已运行 `python tools/validate.py --check`。
- [ ] 我已运行 `python -m unittest discover -s tests -v`。
- [ ] 若修改审核记录，`reviewer` 是我的 GitHub 登录名，且 ID、版本、SHA-256 完全匹配。
- [ ] 若撤回版本，只修改 `yanked` 与非空 `yankReason`，没有改写历史下载或哈希。
