---
layout: default
title: 使用与部署说明
---

# ASR-arxiv-daily 使用说明

自动检索 arXiv 上的自动语音识别、语音转文字论文，累积 JSON 记录并生成仓库首页与 GitHub Pages 页面。默认每 12 小时更新，每周补查代码链接。

## Usage

使用 Python 3.10 或更新版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python daily_arxiv.py
```

常用命令如下。

```bash
# 只抓取论文，跳过 GitHub API 搜索；仍保留摘要中的 GitHub 链接
python daily_arxiv.py --skip-code-search

# 给历史记录补查代码链接，不重新抓取论文
python daily_arxiv.py --update_paper_links

# 从缓存重新生成页面，无需网络，不改变最近抓取时间
python daily_arxiv.py --render-only

# 使用其他配置；输出路径相对于该配置文件所在目录
python daily_arxiv.py --config_path config.yaml

# 运行离线测试
python -m unittest discover -s tests -v
```

程序首次运行会创建缺失的输出目录和缓存文件。首次提交日期用于排序，更新日期单独显示；同一 arXiv ID 的不同版本合并为一条记录。新一轮抓取未命中的历史论文仍会保留。所有主题抓取成功后才开始写入，arXiv 请求失败会退出并保留已有文件。

## GitHub Actions 与 Pages

1. 将项目上传到自己的 GitHub 仓库，并启用 Actions。
2. 打开 **Actions → Update ASR papers → Run workflow**，执行第一次更新。
3. 工作流使用内置 `GITHUB_TOKEN` 提交 README 和 `docs/` 中的生成结果，默认不需要额外 Secret。仓库或组织策略需要允许工作流写入仓库；受保护分支也需要允许自动提交。
4. 工作流直接构建并部署 Pages。在 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
5. 首次部署后访问 Actions 的部署结果中的页面地址。

每日任务在 UTC 00:00、12:00（北京时间 08:00、20:00）运行；代码链接补查在每周一 UTC 08:00（北京时间 16:00）运行。定时任务在默认分支运行，GitHub 可能延迟调度。手动运行时勾选 `update_paper_links` 可只补查代码链接。

工作流通过官方 Pages Actions 显式部署，保证同一次自动更新可以更新网站。初次启用 Pages 前，论文抓取与提交步骤仍可使用，后续部署步骤会提示需要配置 Pages。

## 检索配置

编辑根目录的 `config.yaml`。默认只搜索标题和摘要，并限制在 `cs.CL`、`cs.SD`、`eess.AS` 分类中。完整短语包括 `Speech Recognition`、`Speech-to-Text` 等；缩写 `ASR` 还必须命中 `speech`、`spoken`、`transcription` 或 `acoustic`，降低同名缩写的噪声。关键词检索可能收录以 ASR 为辅助环节的论文，也可能遗漏未使用这些词的论文。

`max_results` 是每个主题每次抓取的最新论文数量，默认 50，并非严格的每日时间窗口。中断更新很久后，可临时调大该值进行补抓。新增主题示例：

```yaml
keywords:
  ASR:
    filters: [Speech Recognition, Speech-to-Text]
    abbreviations: [ASR]
    context: [speech, spoken, transcription, acoustic]
    categories: [cs.CL, cs.SD, eess.AS]
  Streaming ASR:
    filters: [Streaming Speech Recognition, Online Speech Recognition]
    categories: [cs.CL, cs.SD, eess.AS]
```

| 配置 | 作用 |
| --- | --- |
| `show_authors` / `show_links` | 显示作者列 / 代码链接列 |
| `publish_readme` / `publish_gitpage` | 生成 README / Pages 首页 |
| `publish_wechat` | 额外生成 `docs/wechat.md` 列表，仅生成本地文件 |
| `search_code` | 是否查询 GitHub API，关闭后仍保留摘要、备注中的直接链接 |
| `github_max_requests` | 每次运行最多发起的 GitHub 搜索请求数，默认 10 |
| `request_timeout` | GitHub API 单次请求超时秒数 |
| `repository` / `show_badge` | 可选仓库徽章，格式为 `owner/repository`；Actions 中自动识别仓库 |

GitHub 搜索使用 arXiv ID 匹配仓库 README。结果是候选仓库，可能是资料集合，也可能并非作者实现。摘要与备注中的 GitHub 链接标为“论文链接”，搜索结果标为“候选仓库”。API 限流或请求失败时停止本轮代码搜索，论文更新仍会继续。每周任务优先补查从未查询或最久未查询的缺失链接，受请求预算限制。

本地可通过环境变量 `GITHUB_TOKEN` 提供令牌；不要把令牌写进配置或提交到仓库。未提供令牌时也能运行，但搜索额度更低。

## 文件布局

```text
config.yaml                        检索、输出与代码链接配置
daily_arxiv.py                     抓取、缓存合并与页面生成
requirements.txt                  Python 依赖
README.md                         自动生成的论文列表
docs/asr-arxiv-daily.json          所有页面共用的结构化论文缓存
docs/index.md                     自动生成的 Pages 首页
docs/_config.yml                  Pages 主题与站点信息
docs/README.md                    本使用说明
.github/workflows/                定时更新、部署与离线测试
tests/                            查询、缓存、输出与失败行为测试
```

请修改 `docs/README.md` 维护使用说明，首页会在下一次运行时重新生成。相比参考仓库，三个发布渠道共用一份结构化 JSON，避免重复抓取和代码链接不一致；无需维护额外的 web/wechat JSON 副本。

## 来源与许可

参考 [liutaocode/TTS-arxiv-daily](https://github.com/liutaocode/TTS-arxiv-daily) 和 [Vincentqyw/cv-arxiv-daily](https://github.com/Vincentqyw/cv-arxiv-daily)。ASR 版本调整了检索、缓存格式、页面生成与 Actions 流程。保留 Apache-2.0 许可，见根目录 `LICENSE`。
