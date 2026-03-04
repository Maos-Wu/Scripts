#!/usr/bin/env python3
"""
Yahoo Finance 中国股市 Web 监控
用法: python stock_monitor_ui.py [--interval 秒数] [--port 端口]
浏览器打开: http://localhost:5000
"""

import argparse
import json
import math
import os
import queue
import sys
import threading
import time as time_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from time import mktime

import pandas as pd
import requests as _requests
from flask import Flask, Response, jsonify, render_template

# ── PyInstaller 兼容路径 ──────────────────────────────────────
# _BUNDLE_DIR: 只读资源（templates 等），打包时在 sys._MEIPASS，源码时在脚本目录
# _DATA_DIR:   可写数据（缓存文件），打包时在 exe 同级目录，源码时在脚本目录
_IS_BUNDLE  = getattr(sys, 'frozen', False)
_BUNDLE_DIR = sys._MEIPASS if _IS_BUNDLE else os.path.dirname(os.path.abspath(__file__))
_DATA_DIR   = os.path.dirname(sys.executable) if _IS_BUNDLE else os.path.dirname(os.path.abspath(__file__))

# 打包环境下，显式告知 curl_cffi SSL 证书路径
if _IS_BUNDLE:
    import certifi as _certifi
    _ca = _certifi.where()
    os.environ['CURL_CA_BUNDLE']     = _ca
    os.environ['SSL_CERT_FILE']      = _ca
    os.environ['REQUESTS_CA_BUNDLE'] = _ca

import stock_monitor as sm

try:
    import feedparser as _feedparser
    _HAS_FEEDPARSER = True
except ImportError:
    _HAS_FEEDPARSER = False

app = Flask(__name__, template_folder=os.path.join(_BUNDLE_DIR, 'templates'))

_cache: dict = {}
_news_cache: list = []
_live_news_cache: list = []
_subscribers: list = []
_subscribers_lock = threading.Lock()

LEADERBOARD_SIZE = 30

# 每条来源可独立设置：
#   name    显示名称
#   url     RSS/Atom feed 地址
#   max     最多抓取条数（覆盖默认值）
#   headers 额外 HTTP 请求头（如 Authorization、Cookie 等）
#
# 新增来源只需在列表末尾追加一个 dict，不需要改动任何其他代码。
# ── 新闻来源配置 ─────────────────────────────────────────────
# 每条支持字段：name / url / max（覆盖全局默认）/ headers（额外请求头）
# 新增来源只需追加一个 dict。

NEWS_MAX_PER_FEED = 10   # 全局默认，可被每条来源的 max 字段覆盖

NEWS_FEEDS: list[dict] = [
    # 英美综合
    {"name": "BBC",      "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "NYTimes",  "url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"},
    {"name": "Guardian", "url": "https://www.theguardian.com/world/rss"},
    {"name": "FT",       "url": "https://www.ft.com/rss/home"},
    # 财经/商业
    {"name": "YahooFin", "url": "https://finance.yahoo.com/news/rssindex"},
    # 政治
    {"name": "Politico", "url": "https://rss.politico.com/politics-news.xml"},
    # 亚洲
    {"name": "SCMP",     "url": "https://www.scmp.com/rss/91/feed"},
    {"name": "Nikkei",   "url": "https://asia.nikkei.com/rss/feed/nar"},
    # Reuters/AP 国内 DNS 无法解析，暂时注释
    # {"name": "Reuters", "url": "https://feeds.reuters.com/reuters/topNews"},
    # {"name": "AP",      "url": "https://feeds.apnews.com/rss/apf-topnews"},
]

# Live / Breaking 来源（轮询频率更高）
LIVE_NEWS_FEEDS: list[dict] = [
    {"name": "CNBC",       "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"},
    {"name": "SkyNews",    "url": "https://feeds.skynews.com/feeds/rss/home.xml"},
    {"name": "AlJazeera",  "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "MarketWatch","url": "https://feeds.marketwatch.com/marketwatch/topstories"},
    {"name": "TheOnion",   "url": "https://www.theonion.com/rss"},
]

FUTURES = {
    # 大宗商品
    "GC=F":     "黄金",
    "CL=F":     "油(WTI)",
    "BZ=F":     "油(布伦特)",
    "SI=F":     "白银",
    "HG=F":     "铜",
    # 美股指数
    "^GSPC":    "标普500",
    "^IXIC":    "纳指",
    "^DJI":     "道指",
    # 亚太指数
    "^N225":    "日经225",
    "^KS11":    "KOSPI",
    # 外汇
    "CNY=X":    "美元/人民币",
    "JPY=X":    "美元/日元",
    # 风险指标
    "^VIX":     "VIX恐慌指数",
    "^TNX":     "美债10Y收益率",
}


def _fmt_vol(vol) -> str:
    if vol is None:
        return "N/A"
    try:
        f = float(vol)
        if math.isnan(f):
            return "N/A"
    except (TypeError, ValueError):
        return "N/A"
    if f >= 1e8:
        return f"{f/1e8:.1f}亿"
    if f >= 1e4:
        return f"{f/1e4:.0f}万"
    return str(int(f))


def _serialize(cats_data, df_gain, df_loss, df_vol, highlighted, futures_data=None) -> dict:
    watchlist = {}
    for cat_name, data in cats_data:
        rows = []
        for ticker, info in data.items():
            rows.append({
                "ticker": ticker,
                "name":   info["name"],
                "price":  round(info["price"],  2) if info["price"]  is not None else None,
                "change": round(info["change"], 2) if info["change"] is not None else None,
                "pct":    round(info["pct"],    2) if info["pct"]    is not None else None,
                "volume": _fmt_vol(info["volume"]),
            })
        watchlist[cat_name] = rows

    def df_to_rows(df):
        rows = []
        for rank, (_, row) in enumerate(df.iterrows(), start=1):
            code = row["code"]
            price = row["price"] if "price" in row.index else None
            pct   = row["pct"]   if "pct"   in row.index else None
            vol   = row["volume"] if "volume" in row.index else None
            rows.append({
                "rank":   rank,
                "code":   code,
                "name":   sm._NAMES.get(code, row.get("name", "")),
                "price":  round(float(price), 2) if price is not None and pd.notna(price) else None,
                "pct":    round(float(pct),   2) if pct   is not None and pd.notna(pct)   else None,
                "volume": _fmt_vol(vol),
            })
        return rows

    futures = [
        {
            "ticker": ticker,
            "name":   info["name"],
            "price":  round(info["price"],  2) if info["price"]  is not None else None,
            "change": round(info["change"], 2) if info["change"] is not None else None,
            "pct":    round(info["pct"],    2) if info["pct"]    is not None else None,
        }
        for ticker, info in (futures_data or {}).items()
    ]

    return {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "watchlist": watchlist,
        "leaderboard": {
            "gain":   df_to_rows(df_gain)  if not df_gain.empty  else [],
            "loss":   df_to_rows(df_loss)  if not df_loss.empty  else [],
            "volume": df_to_rows(df_vol)   if not df_vol.empty   else [],
        },
        "futures": futures,
        "highlighted": list(highlighted),
    }


def _notify_subscribers(msg: str = "refresh"):
    with _subscribers_lock:
        for q in list(_subscribers):
            q.put(msg)


# ── 翻译配置 ──────────────────────────────────────────────────
# 将 DeepL Free API Key 设置为环境变量：
#   export DEEPL_API_KEY="your-key-here:fx"
# 免费层每月 500,000 字符。不设置则自动 fallback 到 Google 免费接口。
DEEPL_API_KEY  = os.environ.get("DEEPL_API_KEY", "").strip()
TRANS_TTL_DAYS = 7   # JSON 缓存有效期（天），超期自动清除

# ── 翻译缓存 ──────────────────────────────────────────────────
# 内存结构：{ "English title": {"cn": "中文", "ts": 1706000000} }
_TRANS_CACHE_FILE = os.path.join(_DATA_DIR, "trans_cache.json")
_trans_cache: dict = {}
_trans_lock   = threading.Lock()


def _trans_cutoff() -> float:
    return time_module.time() - TRANS_TTL_DAYS * 86400


def _load_trans_cache() -> None:
    global _trans_cache
    if not os.path.exists(_TRANS_CACHE_FILE):
        return
    try:
        with open(_TRANS_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        cutoff  = _trans_cutoff()
        loaded  = expired = migrated = 0
        for k, v in raw.items():
            if isinstance(v, str):
                # 旧格式（无时间戳）：迁移为新格式，时间戳设为当前时间保留一轮
                _trans_cache[k] = {"cn": v, "ts": time_module.time()}
                migrated += 1
            elif isinstance(v, dict) and v.get("ts", 0) >= cutoff:
                _trans_cache[k] = v
                loaded += 1
            else:
                expired += 1
        print(f"[trans] loaded {loaded}, migrated {migrated}, expired {expired} (>{TRANS_TTL_DAYS}d)", flush=True)
    except Exception as e:
        print(f"[trans] load error: {e}", flush=True)


def _save_trans_cache() -> None:
    cutoff = _trans_cutoff()
    with _trans_lock:
        to_save = {k: v for k, v in _trans_cache.items()
                   if isinstance(v, dict) and v.get("ts", 0) >= cutoff}
    try:
        with open(_TRANS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False)
    except Exception as e:
        print(f"[trans] save error: {e}", flush=True)


def _is_cjk(text: str) -> bool:
    """文本中 CJK 字符超过 30% 则视为已是中文，跳过翻译。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return cjk > len(text) * 0.3


def _translate_deepl(text: str) -> str:
    """调用 DeepL Free API，失败时返回空字符串。"""
    # 免费账号 key 以 :fx 结尾，对应专属域名
    url = ("https://api-free.deepl.com/v2/translate"
           if DEEPL_API_KEY.endswith(":fx")
           else "https://api.deepl.com/v2/translate")
    try:
        resp = _requests.post(
            url,
            headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
            data={"text": text, "target_lang": "ZH"},
            timeout=10,
        )
        return resp.json()["translations"][0]["text"].strip()
    except Exception as e:
        print(f"[trans] DeepL error: {e}", flush=True)
        return ""


def _translate_google(text: str) -> str:
    """调用 Google Translate 免费公开接口，失败时返回空字符串。"""
    try:
        resp = _requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text},
            timeout=8,
        )
        data = resp.json()
        return "".join(seg[0] for seg in data[0] if seg[0]).strip()
    except Exception as e:
        print(f"[trans] Google error: {e}", flush=True)
        return ""


def _translate_one(text: str) -> str:
    """优先 DeepL（若配置了 Key），失败自动 fallback 到 Google，最终 fallback 原文。"""
    if DEEPL_API_KEY:
        result = _translate_deepl(text)
        if result:
            return result
    return _translate_google(text) or text


def _apply_translations(items: list) -> None:
    """为每条新闻写入 title_cn。命中有效缓存直接返回，否则翻译、更新时间戳并缓存。"""
    new_entries = 0
    cutoff = _trans_cutoff()
    for item in items:
        title = item["title"]
        if _is_cjk(title):
            item["title_cn"] = title
            continue
        with _trans_lock:
            entry = _trans_cache.get(title)
        # 命中且未过期
        if entry and isinstance(entry, dict) and entry.get("ts", 0) >= cutoff:
            item["title_cn"] = entry["cn"]
        else:
            cn = _translate_one(title)
            ts = time_module.time()
            with _trans_lock:
                _trans_cache[title] = {"cn": cn, "ts": ts}
            item["title_cn"] = cn
            new_entries += 1
            time_module.sleep(0.05)   # 50ms 节流，避免触发频率限制
    if new_entries:
        _save_trans_cache()


_DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; newsbot/1.0)"}


def _fetch_one_feed(feed_cfg: dict) -> list:
    """抓取单个 RSS/Atom feed，返回该来源的条目列表。"""
    source  = feed_cfg["name"]
    url     = feed_cfg["url"]
    limit   = feed_cfg.get("max", NEWS_MAX_PER_FEED)
    headers = {**_DEFAULT_HEADERS, **feed_cfg.get("headers", {})}
    result  = []
    try:
        resp = _requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
        feed = _feedparser.parse(resp.content)
        for entry in feed.entries[:limit]:
            title = (entry.get("title") or "").strip()
            link  = (entry.get("link")  or "").strip()
            if not title or not link:
                continue
            ts = 0
            parsed_time = (entry.get("published_parsed") or
                           entry.get("updated_parsed") or
                           entry.get("created_parsed"))
            if not parsed_time:
                raw = (entry.get("published") or entry.get("updated") or
                       entry.get("created") or "")
                if raw:
                    import email.utils
                    tpl = email.utils.parsedate(raw)
                    if tpl:
                        parsed_time = tpl
            if parsed_time:
                ts = mktime(parsed_time)
                time_str = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
            else:
                time_str = "--"
            result.append({"source": source, "title": title,
                           "link": link, "time": time_str, "_ts": ts})
    except Exception as e:
        print(f"[news] {source}: {e}", flush=True)
    return result


def _fetch_feeds(feeds: list) -> list:
    """通用 RSS/Atom 抓取器：并发抓取所有 feeds，返回按时间降序排列的条目。"""
    if not _HAS_FEEDPARSER:
        return []
    items = []
    with ThreadPoolExecutor(max_workers=len(feeds)) as executor:
        futures = [executor.submit(_fetch_one_feed, cfg) for cfg in feeds]
        for future in as_completed(futures):
            items.extend(future.result())
    items.sort(key=lambda x: x.get("_ts", 0), reverse=True)
    _apply_translations(items)
    return items


def fetch_news() -> list:
    return _fetch_feeds(NEWS_FEEDS)


def fetch_live_news() -> list:
    return _fetch_feeds(LIVE_NEWS_FEEDS)


def _bg_news_loop(fetch_fn, cache_key: str, sse_msg: str, interval: int):
    """通用后台新闻线程主体，fetch_fn 决定抓取哪组 feeds。"""
    if not _HAS_FEEDPARSER:
        print(f"[{sse_msg}] feedparser not installed — disabled.", flush=True)
        return
    g = globals()
    while True:
        try:
            g[cache_key] = fetch_fn()
            _notify_subscribers(sse_msg)
        except Exception as e:
            print(f"[{sse_msg} thread] {e}", flush=True)
        time_module.sleep(interval)


def background_news(interval: int = 120):
    _bg_news_loop(fetch_news, "_news_cache", "news", interval)


def background_live_news(interval: int = 30):
    _bg_news_loop(fetch_live_news, "_live_news_cache", "live-news", interval)


def background_fetch(interval: int):
    sm.load_name_cache()
    first_run = True
    while True:
        try:
            cats_data    = [(cat, sm.fetch_quotes(tmap)) for cat, tmap in sm.CATEGORIES]
            futures_data = sm.fetch_quotes(FUTURES)
            df_gain = sm.fetch_ashare_ranked("percentchange", sort_asc=False, size=LEADERBOARD_SIZE)
            df_loss = sm.fetch_ashare_ranked("percentchange", sort_asc=True,  size=LEADERBOARD_SIZE)
            df_vol  = sm.fetch_ashare_ranked("dayvolume",     sort_asc=False, size=LEADERBOARD_SIZE)

            lb_codes = (
                (list(df_gain["code"]) if not df_gain.empty else []) +
                (list(df_loss["code"]) if not df_loss.empty else []) +
                (list(df_vol["code"])  if not df_vol.empty  else [])
            )
            sm.fill_missing_names(lb_codes)

            all_pcts: dict = {}
            for _, data in cats_data:
                for ticker, info in data.items():
                    if info["pct"] is not None:
                        all_pcts[ticker] = info["pct"]
            for df in (df_gain, df_loss, df_vol):
                if not df.empty:
                    for _, row in df.iterrows():
                        if pd.notna(row["pct"]):
                            all_pcts[row["code"]] = row["pct"]

            highlighted = sm.compute_highlighted(all_pcts)
            if first_run:               # 首次运行仅初始化 _prev_pct，不高亮
                highlighted = set()
                first_run = False

            _cache.update(_serialize(cats_data, df_gain, df_loss, df_vol, highlighted, futures_data))
            _notify_subscribers("refresh")
        except Exception as e:
            print(f"[fetch error] {e}", flush=True)

        time_module.sleep(interval)


# ── 路由 ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    return jsonify(_cache)


@app.route("/api/news")
def api_news():
    return jsonify(_news_cache)


@app.route("/api/live-news")
def api_live_news():
    return jsonify(_live_news_cache)


@app.route("/stream")
def stream():
    q: queue.Queue = queue.Queue()
    with _subscribers_lock:
        _subscribers.append(q)

    def event_stream():
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _subscribers_lock:
                try:
                    _subscribers.remove(q)
                except ValueError:
                    pass

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 入口 ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Yahoo Finance 中国股市 Web 监控")
    parser.add_argument("--interval", type=int, default=60, help="刷新间隔秒数（默认 60）")
    parser.add_argument("--port",     type=int, default=5000, help="HTTP 端口（默认 5000）")
    args = parser.parse_args()

    _load_trans_cache()

    t = threading.Thread(target=background_fetch, args=(args.interval,), daemon=True)
    t.start()
    tn = threading.Thread(target=background_news, args=(600,), daemon=True)
    tn.start()
    tl = threading.Thread(target=background_live_news, args=(120,), daemon=True)
    tl.start()

    print(f"启动 Web 监控 → http://localhost:{args.port}", flush=True)
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
