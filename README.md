# Stock Monitor

A股实时行情监控工具，提供终端版与 Web 版两种界面，支持涨跌榜、自选股、国际期货及英文新闻自动翻译。

---

## 功能

### 终端版 (`stock_monitor.py`)
- 实时拉取沪深两市 A 股行情（via yfinance Screener）
- 涨幅榜 / 跌幅榜 / 成交量榜，行数随终端窗口高度自适应
- 自选股板块，实时价格 + 涨跌幅
- 股票中文名懒加载缓存（腾讯财经 API，30 天有效期）
- 涨跌幅变化 ≥ 0.25% 时触发行高亮（0.5s）
- `rich.live.Live` 全屏原地刷新，无滚动追加

### Web 版 (`stock_monitor_ui.py`)
- 复用终端版数据层，额外提供 Flask Web 服务
- SSE 实时推送，支持多客户端同时订阅
- 国际期货面板：黄金、石油、白银、铜
- 英文新闻瀑布流（BBC、NYTimes、Guardian、CNN、NBC）
- Live News 面板：5 大来源最新头条，带翻页动画
- 自动翻译：DeepL Free API → Google Translate → 原文 fallback
- 翻译缓存（`trans_cache.json`，7 天 TTL）

---

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 运行终端版

```bash
python stock_monitor.py
```

按 `Ctrl+C` 退出，自动还原终端屏幕。

### 运行 Web 版

```bash
python stock_monitor_ui.py
# 可选参数
python stock_monitor_ui.py --interval 30 --port 5000
```

启动后访问 `http://localhost:5000`。

---

## 环境变量

| 变量 | 说明 | 是否必须 |
|------|------|----------|
| `DEEPL_API_KEY` | DeepL Free API Key（格式 `xxx:fx`） | 否，不设置自动降级至 Google |

```bash
export DEEPL_API_KEY="your-key:fx"
```

---

## 文件结构

```
Scripts/
├── stock_monitor.py        # 终端版
├── stock_monitor_ui.py     # Web 版 Flask 后端
├── templates/
│   └── index.html          # 前端页面
├── requirements.txt
├── ashare_names.json       # A股中文名缓存（自动生成）
└── trans_cache.json        # 翻译缓存（自动生成）
```

---

## 依赖

| 包 | 用途 |
|----|------|
| `yfinance` | A股 + 期货行情数据 |
| `rich` | 终端 TUI 渲染 |
| `pandas` | 数据处理 |
| `flask` | Web 服务 + SSE |
| `feedparser` | RSS/Atom 新闻抓取 |

---

## 注意事项

- 需要能访问 Yahoo Finance（`query2.finance.yahoo.com`）的网络环境
- akshare 在境外网络下无法使用，本项目已完全弃用
- Web 版翻页动画（Live News）在部分浏览器下存在兼容性问题，待修复
- 新闻自动刷新的长时间稳定性尚未完整验证
