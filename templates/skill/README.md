# 这里没有模板

`scripts/create_skill.py` 会检查这个目录在不在，但它不读里面的文件 ——
生成的四个文件（`__init__.py` / `manifest.yaml` / `handler.py` / `SKILL.md`）
全部由脚本现拼，写到 `skills/<名字>/` 下。

这里以前放着四份示例，后来它们和脚本漂了：照着抄出来的包在装载时才报错。
示例只留一份，就是脚本自己，见 `SKILL_SPEC.md` 的「脚手架」一节。

```bash
python scripts/create_skill.py <skill_name> --desc "描述"
```
