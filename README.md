# Skills

可安装的智能体技能集合（Claude Code / Codex / DSH 等），由 [skills](https://www.npmjs.com/package/skills)（Agent Skills 生态）规范管理。

| 技能 | 说明 |
|---|---|
| [inventory-ledger-skill](inventory-ledger-skill/) | 通用进销存台账 V1：采购/销售/调拨/库存调整/附加费用/月末一次加权平均成本，Excel 单文件持久化 |

## 安装

以 inventory-ledger-skill 为例（各技能目录内 README 有更详细说明与自检步骤）：

```bash
# Claude Code（全局）
npx skills add boboty/Skills --skill inventory-ledger-skill -g -a claude-code
# Codex（全局）
npx skills add boboty/Skills --skill inventory-ledger-skill -g -a codex
# DSH：复制 inventory-ledger-skill/ 到 ~/.agents/skills/（或项目 .agents/skills/）
```

## 许可证

本仓库当前**未授权任何开源许可证**（待定，按需求方确认后再添加 LICENSE 文件）。