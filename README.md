# AI 每日情报站

由 AI Agent 每日自动生成的 AI/Agent 领域新闻摘要站。

## 架构(三阶段)

- **阶段1(当前)**:静态站。Agent 在本地生成摘要 → 写入 `posts/` → 推送到 GitHub → GitHub Pages 自动发布。前端自带全文检索(浏览器内完成,无后端)。
- **阶段2(预留)**:提问交互。方案 A:嵌入 Coze 机器人网页组件(零代码);方案 B:Vercel 云函数 + 大模型 API。页面右上角"提问"按钮即为预留位。
- **阶段3(预留)**:把"每日生成摘要"从本地电脑迁移到 GitHub Actions 云端定时运行,摆脱对本机开机的依赖。

## 目录结构

```
ai-daily-site/
├── index.html        # 全部前端(展示 + 检索 + 提问预留位)
├── posts/
│   ├── index.json    # 日期清单,如 ["2026-06-11"]
│   └── YYYY-MM-DD.md # 每日摘要
└── README.md
```

## 每日更新约定(Agent 执行)

1. 生成摘要写入 `posts/YYYY-MM-DD.md`
2. 把该日期追加进 `posts/index.json`
3. 推送到 GitHub(阶段1 手动 / 配置 Token 后自动)

## 部署(GitHub Pages)

1. GitHub 新建 Public 仓库(如 `ai-daily`)
2. 上传本文件夹内全部内容(保持 `posts/` 子目录结构)
3. 仓库 Settings → Pages → Branch 选 `main`、目录选 `/ (root)` → Save
4. 1-2 分钟后访问 `https://<你的用户名>.github.io/ai-daily/`

## 自动发布

本站由 Cowork 定时任务 `daily-ai-news-digest` 每日 09:06 自动生成摘要并 push 上线,无需手动操作。
