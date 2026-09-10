# 18x Manga Sommelier 🎴

**基于个人口味的成人漫画推荐系统。**

它读取你自己拥有的漫画清单，从 E-Hentai 元数据（作者、标签、评分、页数）中构建**口味画像**，然后跨多个站点寻找你还没有收藏的作品——附封面、直达链接，以及一段 AI 撰写的、评论家风格的推荐理由，告诉你**这部作品本身**是什么样的。

一切都在本地运行。它只查询**元数据与链接**——不下载内容，不绕过 DRM 或付费墙，不上传任何东西。

![界面截图](docs/images/ui-home.png)

> 语言 / Language: [English](README.md) | 简体中文

---

## ⚠️ 免责声明

- 本项目**仅限成年人（18+）**，所有目标站点均含有成人内容；
- 这是一个**个人推荐工具**，不是爬虫或下载器；
- 不下载、存储或再分发任何内容，只使用公开元数据（标题 / 作者 / 标签 / 评分 / 页数）与画廊链接；
- 内置限速遵守各站点 API 政策，请勿调低；
- 请支持你喜欢的作者。

---

## 工作原理

```
folder_list.txt（GBK 编码，你自己的文件夹名）
        │  1. parse      结构化条目（社团 / 作者 / 标题 / 语言 / 卷号）
        ▼
E-Hentai 元数据匹配      规则匹配 + 可选 DeepSeek 消歧
        │  2. lookup     → 每部作品的 gid / 标签 / 评分 / 页数
        ▼
偏好画像                3. profile   → TOP 作者与标签、评分/篇幅/分类统计
        ▼
推荐器（三通道）          4. recommend → 从多站点取候选，对照画像打分，
        │                   硬过滤 + 降权
        ▼
网页 UI / 原生窗口        5. serve|app → 浏览、收藏/无感/忽略、合并、搜索、重跑
        │
反馈闭环                6. feedback  → 喜欢/无感回写进画像权重
```

### 三个推荐通道

| 通道 | 配额 | 逻辑 |
|---|---|---|
| 🎯 精准推荐 | 每批 10 | 用你的 TOP 作者和 TOP 标签组合在各站点搜索 |
| 🔥 热作排行 | 每批 10 | 热门作品，但**必须**命中你的 TOP 作者或 TOP 标签（硬过滤） |
| 📚 连载追踪 | 不限 | 找出你已收藏系列的新卷 |

每次更新生成**全新一批**（旧批次自动退役），上次更新之后发布的作品获得时间加权。你已收藏 / 无感 / 忽略的作品跨站排除，已推荐过的作品大幅降权。

### 内置口味规则（全部可配置）

- 只推中文版（`require_chinese`）
- 硬排除品类 / 标签 / 标题模式（如 3D、AI 生成、Cosplay、真人写真）
- 软降权（如各类 NTR）

### AI（可选）

配置 key 时使用 [DeepSeek](https://platform.deepseek.com/) `deepseek-chat`，用于：搜索词扩展、标题消歧、匹配核验、鉴赏式理由撰写。**没有 key 时全部功能自动降级**为规则/模板模式。

---

## 快速开始（源码运行，Windows）

> 环境要求：Windows 10/11、Python 3.10–3.13；窗口模式需要 Microsoft Edge / WebView2 运行时。

```bat
git clone https://github.com/myainygem/18x-manga-sommelier.git
cd 18x-manga-sommelier
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.json config.json
```

编辑 `config.json`：

```jsonc
{
  "proxy": "",                       // 如使用 Clash： "http://127.0.0.1:7897"
  "ai": {
    "enabled": true,
    "api_key": "sk-...",             // DeepSeek key（可选）
    "model": "deepseek-chat"
  }
}
```

准备 `folder_list.txt`——每行一个条目，**GBK 编码**，与你的漫画文件夹名一致（虚构示例）：

```
[社团 (作者)] 作品名 CH.1 [Chinese] [Digital]
(作者) 另一部作品 [Chinese]
```

然后依次运行：

```bat
python cli.py check                    # 环境自检（站点连通性、AI key）
python cli.py parse                    # 解析 folder_list.txt
python cli.py lookup                   # 逐条匹配 E-Hentai 元数据（长任务）
python cli.py auto-resolve             # （可选）AI 攻坚未命中条目
python cli.py profile                  # 生成口味画像
python cli.py recommend                # 生成推荐（reports/ 目录）
python cli.py serve                    # 浏览器 UI：http://127.0.0.1:8765
python cli.py app                      # 原生窗口（WebView2，无需浏览器）
```

完整流程（含 18comic cookie 说明与便携/安装包打包）见 [docs/INSTALL.md](docs/INSTALL.md)。

---

## Web UI

单页应用（浅色玻璃拟态主题，中文界面）：

- **首页**——封面斜向跑马灯 + 画像摘要
- **推荐通道**——精准 / 热作 / 连载卡片：封面、中文标签名、分开展示的 简介 与 推荐理由
- **反馈**——收藏 / 不感兴趣 / 忽略（5 秒撤销）、跨站手动合并（可撤回）、收藏开关
- **收藏列表**——按合并后的作者别名分组，含手点收藏；可移出/恢复条目
- **搜索**——跨站搜索，显示各站结果总数，一键收藏，防盗链图床走本地代理
- **更新推荐**——选择站点后后台批量生成，实时看日志

## CLI 命令参考

| 命令 | 用途 |
|---|---|
| `python cli.py check [--ai]` | 环境自检 |
| `python cli.py parse` | 解析 `folder_list.txt`（GBK） |
| `python cli.py lookup [--limit N] [--reset-manual] [--reset-all]` | 匹配 E-Hentai 元数据（可断点续跑） |
| `python cli.py auto-resolve [--max N]` | AI 多语言检索攻坚未命中条目 |
| `python cli.py manual-pick 行号 [下标]` | 人工确认候选 |
| `python cli.py verify-ai [--max N]` | AI 复核匹配，错配转人工 |
| `python cli.py profile` | （重新）生成画像 |
| `python cli.py recommend [--sites ehentai,nhentai] [--no-ai] [--force]` | 生成一批推荐 |
| `python cli.py series-check` | 仅跑连载通道 |
| `python cli.py feedback WORK_ID like\|meh\|hide` | 命令行记录反馈 |
| `python cli.py update-profile` | 折算待应用反馈权重（UI 在更新推荐时自动做） |
| `python cli.py enrich` / `regen-reasons` | 回填收藏数/简介 / 重写理由 |
| `python cli.py backfill-covers` / `purge-dupes` | 补封面 / 清理与收藏库重复的推荐 |
| `python cli.py jm-login` | 交互式配置 18comic cookie（读取本机 Chrome cookie） |
| `python cli.py serve` / `app` | 浏览器 UI / 原生窗口 |
| `python cli.py status` | 库/画像/推荐统计 |

## 配置参考

`config.json`（默认值见 `config.example.json`）：

| 键 | 含义 |
|---|---|
| `proxy` | HTTP(S) 代理，如 `http://127.0.0.1:7897` |
| `rate_limits.*` | 各域名最小请求间隔（秒） |
| `sites.*` | 站点开关：ehentai / nhentai / wnacg / 18comic / hitomi |
| `ai.enabled` / `api_key` / `model` | DeepSeek 配置（环境变量 `DEEPSEEK_API_KEY` 优先） |
| `ai.search_expand` / `disambiguate` / `reasons` | 各项 AI 功能开关 |
| `recommend.quota_precision` / `quota_trending` / `quota_series` | 每批数量（`0` = 不限） |
| `recommend.min_score` | 入选分数线 |
| `recommend.prefer_recent_since_last` | 上次更新后发布的作品加分 |
| `filters.require_chinese` | 只推中文版 |
| `filters.hard_exclude_categories` / `hard_exclude_tags` / `title_regex_exclude` | 硬过滤 |
| `filters.penalty_tags` | 分数降权 |
| `ui.search_site` | 标签点击"站内搜索"的默认目标站 |

## 支持的站点

| 站点 | 角色 | 说明 |
|---|---|---|
| E-Hentai | 主元数据源 | 搜索 + gdata API，限速 7 秒 |
| nhentai | 推荐 / 搜索 | 官方 v2 API，经 curl_cffi 伪装 Chrome 指纹（普通 requests 会 403） |
| wnacg 绅士漫画 | 中文版 | HTML 解析 |
| 18comic 禁漫 | 中文版 | jmcomic APP API 客户端 |
| hitomi.la | 搜索 / 可选推荐 | 无头 Chrome 渲染搜索页；较慢 |

## 数据与隐私

- 所有数据都在项目 `data/` 目录（SQLite 数据库、画像 JSON、缓存）。除站点元数据/API 查询与（配置了 key 时的）DeepSeek 调用外，不发任何数据；
- `config.json`、`folder_list.txt` 与 `data/` 下除公开的 `tag_zh.json` 翻译资源外的所有内容都被 `.gitignore` 排除。**切勿提交你的收藏数据或 API key**；
- 便携模式：在打包后的 `MangaRecV1.exe` 旁放一个 `portable.flag` 文件，数据与配置就全部留在同一文件夹，不碰 `%APPDATA%`。

## 打包（Windows）

```bat
pip install -r requirements-build.txt
pyinstaller manga_rec_v1.spec --noconfirm                          # → dist\MangaRecV1\
"<Inno Setup 6>\ISCC.exe" installer\manga_rec_v1_release.iss       # → release\
```

spec 内置的是**干净种子**（空配置、无收藏数据）——Inno 脚本里的 `onlyifdoesntexist` 规则保证重复运行安装程序不会覆盖用户配置/数据。

## Releases

见 [Releases](https://github.com/myainygem/18x-manga-sommelier/releases)：

- **`MangaRecV1-Setup-1.0.0.exe`** —— V1 窗口版应用（WebView2），内置空收藏库。构建个人收藏库需按上文从源码走 CLI 流程；安装程序主要用于便捷运行 UI。

## 已知限制

- hitomi 搜索由浏览器渲染、速度慢；在推荐生成中启用它会明显拉长时间；
- 纯汉字无假名的标题不做罗马音转换（已接受的取舍）；
- 流水线以 Windows 为先（作者在 Windows 上开发）。

## 路线图

- [ ] 应用内导入收藏清单（在 UI 里解析文件夹列表）
- [ ] 更多站点与更好的跨站同作品识别
- [ ] 英文界面
- [ ] Linux 支持

## 开源致谢

本项目基于以下开源项目构建：

| 项目 | 用途 | 许可证 |
|---|---|---|
| [requests](https://github.com/psf/requests) | HTTP 客户端 | Apache-2.0 |
| [curl_cffi](https://github.com/lexiforest/curl_cffi) | TLS 指纹伪装（nhentai 访问） | MIT |
| [pykakasi](https://github.com/miurahr/pykakasi) | 日文假名/汉字罗马音转换 | GPL-3.0-or-later |
| [OpenCC](https://github.com/BYVoid/OpenCC) | 简繁中文转换 | Apache-2.0 |
| [jmcomic（JMComic-Crawler-Python）](https://github.com/tonquer/jmcomic) | 18comic APP API 客户端 | MIT |
| [playwright](https://github.com/microsoft/playwright-python) | 无头浏览器（hitomi 搜索、cookie 登录） | Apache-2.0 |
| [pywebview](https://github.com/r0x0r/pywebview) | 原生窗口（WebView2） | BSD-3-Clause |
| [pythonnet](https://github.com/pythonnet/pythonnet) | .NET 互操作（WebView2 所需） | MIT |
| [clr-loader](https://github.com/pythonnet/clr-loader) | .NET 运行时加载 | MIT |
| [pywin32](https://github.com/mhammond/pywin32) | Windows API（Chrome cookie 解密） | PSF |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | 程序打包 | GPL-2.0-or-later（含例外条款） |
| [Inno Setup](https://jrsoftware.org/isinfo.php) | 安装程序制作 | 免费软件 |

另感谢 [DeepSeek](https://platform.deepseek.com/) 提供的可选 AI 服务，以及 E-Hentai、nhentai、wnacg、18comic、hitomi 等站点公开的元数据。

## 许可证

[MIT](LICENSE) © 2026 myainygem

本项目仅供成人（18+）个人学习研究使用。
