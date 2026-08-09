# AI Top News · 每日 AI 新闻 + GitHub AI 项目雷达

一个**每天自动更新**的静态网站，两块内容合成一页：

- 📰 **AI 每日新闻** —— 汇总各大 AI 媒体（MIT Tech Review、VentureBeat、The Verge、
  TechCrunch、Ars Technica、Hugging Face、DeepMind、Google AI…）的最新头条。
- 🛰️ **GitHub AI 项目雷达** —— 每天从 GitHub 抓取近期新建、快速涨星的 AI 开源项目。

网站由 **GitHub Actions** 每天定时构建，并发布到 **GitHub Pages**，手机 / 电脑都能看，
深浅色自适应。

---

## 🚀 上线三步（只需做一次）

1. **把仓库改为 Public**（GitHub 免费版在私有仓库上开 Pages 需要付费；改公开即免费）
   `Settings → General → 底部 Danger Zone → Change repository visibility → Public`
2. **开启 Pages，来源选 GitHub Actions**
   `Settings → Pages → Build and deployment → Source: GitHub Actions`
3. **跑一次构建**
   `Actions 标签页 → 选 "Build & Deploy AI Top News" → Run workflow`
   跑完后，站点地址会显示在该次运行的 `deploy` 步骤里，形如
   `https://1skill.github.io/ai-top-news/`

之后每天 **06:00 UTC（约北京时间 14:00）** 会自动刷新，无需手动操作。

---

## 🧩 项目结构

```
ai-top-news/
├── config/sources.yaml          # 新闻源 + GitHub 雷达配置（改这里即可调整内容）
├── scripts/build.py             # 抓取数据并渲染 public/index.html + data.json
├── requirements.txt             # Python 依赖
├── public/                      # 构建产物（可直接用浏览器打开预览）
└── .github/workflows/daily.yml  # 每日定时构建 + 部署到 Pages
```

## 🔧 自定义

打开 `config/sources.yaml`：

- `news_feeds` —— 增删 RSS 新闻源（`name` + `url`，可选 `limit`）。
- `github.topics` —— 想追踪的 GitHub 话题标签（如 `llm`、`ai-agents`、`rag`）。
- `github.window_days` —— 只收录最近 N 天内新建的项目（“雷达”范围）。
- `github.min_stars` —— 过滤掉星标过少的项目。
- `site.max_news` / `site.max_repos` —— 页面展示数量上限。

## 🖥️ 本地预览

```bash
pip install -r requirements.txt
python scripts/build.py          # 生成 public/index.html
open public/index.html           # 或用浏览器打开
```

> 构建脚本对每个数据源都做了容错：单个源抓取失败只会被跳过并记录警告，
> 不会导致整个构建失败。
