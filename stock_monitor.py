#!/usr/bin/env python3
"""
Yahoo Finance 中国股市实时终端监控
用法: python stock_monitor.py [--interval 秒数]
退出: Ctrl+C
"""

import argparse
import json
import os
import time
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.rule import Rule
from rich.text import Text
from rich import box

# ── 股票代码定义 ──────────────────────────────────────────────

INDICES = {
    "000001.SS": "上证综指",
    "399001.SZ": "深证成指",
    "000300.SS": "沪深300",
    "399006.SZ": "创业板指",
    "^HSI":      "恒生指数",
}

A_SHARES = {
    "600519.SS": "贵州茅台",
    "002594.SZ": "比亚迪",
    "600036.SS": "招商银行",
    "601398.SS": "工商银行",
    "000858.SZ": "五粮液",
}

HK_STOCKS = {
    "0700.HK": "腾讯控股",
    "9988.HK": "阿里巴巴",
    "3690.HK": "美团",
    "9618.HK": "京东集团",
    "2318.HK": "中国平安",
}

ETFS = {
    "510050.SS": "上证50ETF",
    "510300.SS": "沪深300ETF",
    "159915.SZ": "创业板ETF",
}

CATEGORIES = [
    ("大盘指数", INDICES),
    ("A 股",    A_SHARES),
    ("港  股",  HK_STOCKS),
    ("ETF",    ETFS),
]

console = Console()

NAMES_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ashare_names.json")
NAMES_CACHE_MAX_DAYS = 30

_NAMES: dict = {}           # { "600519.SS": "贵州茅台", ... }
_prev_pct: dict = {}        # { ticker/code: last_pct }  用于变化检测

HIGHLIGHT_STYLE = "on grey23"   # 高亮行背景色
HIGHLIGHT_THRESHOLD = 0.25      # 触发高亮的涨跌幅变化阈值（%）
HIGHLIGHT_DURATION = 0.5        # 高亮持续秒数


# ── 中文名称缓存（腾讯财经） ────────────────────────────────────

def _to_tencent_code(yf_code: str) -> str:
    num, exch = yf_code.split(".")
    return ("sh" if exch == "SS" else "sz") + num


def _fetch_names_tencent(yf_codes: list) -> dict:
    names = {}
    for i in range(0, len(yf_codes), 200):
        batch = yf_codes[i : i + 200]
        tn_batch = [_to_tencent_code(c) for c in batch]
        try:
            r = requests.get(
                f"https://qt.gtimg.cn/q={','.join(tn_batch)}",
                headers={"Referer": "https://finance.qq.com/"},
                timeout=15,
            )
            for line in r.text.strip().split("\n"):
                if "~" not in line or '"' not in line:
                    continue
                try:
                    varname  = line.split("=")[0]
                    prefix   = varname[2:4]
                    num_code = varname[4:]
                    cn_name  = line.split('"')[1].split("~")[1]
                    if cn_name:
                        suffix = "SS" if prefix == "sh" else "SZ"
                        names[f"{num_code}.{suffix}"] = cn_name
                except (IndexError, ValueError):
                    pass
        except Exception:
            pass
        time.sleep(0.05)
    return names


def load_name_cache() -> None:
    global _NAMES
    if os.path.exists(NAMES_CACHE_FILE):
        try:
            with open(NAMES_CACHE_FILE, "r", encoding="utf-8") as f:
                _NAMES = json.load(f)
        except Exception:
            pass


def fill_missing_names(codes: list) -> None:
    global _NAMES
    missing = [c for c in codes if c not in _NAMES]
    if not missing:
        return
    new_names = _fetch_names_tencent(missing)
    if not new_names:
        return
    _NAMES.update(new_names)
    try:
        with open(NAMES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_NAMES, f, ensure_ascii=False)
    except Exception:
        pass


# ── 变化检测 ──────────────────────────────────────────────────

def compute_highlighted(new_pcts: dict) -> set:
    """比较新旧涨跌幅，返回变化 >= HIGHLIGHT_THRESHOLD 的代码集合，并更新 _prev_pct。"""
    global _prev_pct
    highlighted = {
        code for code, pct in new_pcts.items()
        if pct is not None
        and code in _prev_pct
        and _prev_pct[code] is not None
        and abs(pct - _prev_pct[code]) >= HIGHLIGHT_THRESHOLD
    }
    _prev_pct.update({k: v for k, v in new_pcts.items() if v is not None})
    return highlighted


# ── 数据获取 ──────────────────────────────────────────────────

def fetch_quotes(ticker_map: dict) -> dict:
    results = {}
    tickers = list(ticker_map.keys())
    try:
        data = yf.Tickers(" ".join(tickers))
        for ticker, cn_name in ticker_map.items():
            try:
                fi     = data.tickers[ticker].fast_info
                price  = fi.last_price
                prev   = fi.previous_close
                if price is None or prev is None:
                    raise ValueError("no price")
                change = price - prev
                pct    = change / prev * 100 if prev else 0.0
                volume = fi.three_month_average_volume or 0
                results[ticker] = {"name": cn_name, "price": price,
                                   "change": change, "pct": pct, "volume": volume}
            except Exception:
                results[ticker] = {"name": cn_name, "price": None,
                                   "change": None, "pct": None, "volume": None}
    except Exception:
        for ticker, cn_name in ticker_map.items():
            results[ticker] = {"name": cn_name, "price": None,
                               "change": None, "pct": None, "volume": None}
    return results


# ── A 股全量数据获取（Yahoo Finance Screener）────────────────────

_CN_QUERY = yf.EquityQuery('or', [
    yf.EquityQuery('eq', ['exchange', 'SHH']),
    yf.EquityQuery('eq', ['exchange', 'SHZ']),
])


def fetch_ashare_ranked(sort_field: str, sort_asc: bool, size: int = 50) -> pd.DataFrame:
    try:
        result = yf.screen(_CN_QUERY, sortField=sort_field, sortAsc=sort_asc, size=size)
        rows = [
            {"code":   q.get("symbol", ""),
             "name":   q.get("shortName", ""),
             "price":  q.get("regularMarketPrice"),
             "pct":    q.get("regularMarketChangePercent"),
             "change": q.get("regularMarketChange"),
             "volume": q.get("regularMarketVolume")}
            for q in result.get("quotes", [])
        ]
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


def _leaderboard_size() -> int:
    """根据终端高度动态计算榜单可显示行数，确保内容不超出屏幕。"""
    # 固定开销（行数）：
    #   Panel 上下边框:             2
    #   自选股两行（各高 ~9 行）:   18
    #   两行之间空行:                1
    #   "A 股榜单" Rule:             1
    #   榜单表头区（标题+空+列+分隔）:4
    #   底部状态栏:                  1
    #   安全余量:                    3
    OVERHEAD = 30
    return max(5, console.size.height - OVERHEAD)


def fetch_all_data() -> tuple:
    """拉取全部数据，返回 (cats_data, df_gain, df_loss, df_vol, all_pcts)。"""
    # 自选股
    cats_data = [(cat, fetch_quotes(tmap)) for cat, tmap in CATEGORIES]

    # 榜单（行数自适应终端高度）
    size = _leaderboard_size()
    df_gain   = fetch_ashare_ranked("percentchange", sort_asc=False, size=size)
    df_loss   = fetch_ashare_ranked("percentchange", sort_asc=True,  size=size)
    df_vol    = fetch_ashare_ranked("dayvolume",     sort_asc=False, size=size)

    # 补全中文名
    lb_codes = (
        (list(df_gain["code"]) if not df_gain.empty else []) +
        (list(df_loss["code"]) if not df_loss.empty else []) +
        (list(df_vol["code"])  if not df_vol.empty  else [])
    )
    fill_missing_names(lb_codes)

    # 汇总本轮所有涨跌幅
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

    return cats_data, df_gain, df_loss, df_vol, all_pcts


# ── 表格渲染 ──────────────────────────────────────────────────

def _color(pct):
    if pct is None:  return "white"
    if pct > 0:      return "bright_red"
    if pct < 0:      return "bright_green"
    return "white"


def render_table(category_name: str, data: dict, highlighted: set = frozenset()) -> Table:
    table = Table(
        title=f"[bold]{category_name}[/bold]",
        box=box.SIMPLE_HEAD, show_header=True,
        header_style="bold cyan", pad_edge=True,
    )
    table.add_column("代码",     style="dim",    min_width=12, no_wrap=True)
    table.add_column("名称",                     min_width=8,  no_wrap=True)
    table.add_column("当前价",   justify="right", min_width=10, no_wrap=True)
    table.add_column("涨跌额",   justify="right", min_width=10, no_wrap=True)
    table.add_column("涨跌幅%",  justify="right", min_width=9,  no_wrap=True)
    table.add_column("均量(3M)", justify="right", min_width=10, no_wrap=True)

    for ticker, info in data.items():
        color = _color(info["pct"])
        price_str  = f"{info['price']:.2f}"  if info["price"]  is not None else "N/A"
        change_str = f"{info['change']:+.2f}" if info["change"] is not None else "N/A"
        pct_str    = f"{info['pct']:+.2f}%"   if info["pct"]    is not None else "N/A"

        vol = info["volume"]
        if vol is None:       vol_str = "N/A"
        elif vol >= 1e8:      vol_str = f"{vol/1e8:.1f}亿"
        elif vol >= 1e4:      vol_str = f"{vol/1e4:.0f}万"
        else:                 vol_str = str(int(vol))

        row_style = HIGHLIGHT_STYLE if ticker in highlighted else None
        table.add_row(
            ticker,
            f"[{color}]{info['name']}[/{color}]",
            f"[{color}]{price_str}[/{color}]",
            f"[{color}]{change_str}[/{color}]",
            f"[{color}]{pct_str}[/{color}]",
            vol_str,
            style=row_style,
        )
    return table


# ── 榜单渲染 ──────────────────────────────────────────────────

def render_leaderboard(title: str, df: pd.DataFrame, highlighted: set = frozenset()) -> Table:
    table = Table(
        title=f"[bold]{title}[/bold]",
        box=box.SIMPLE_HEAD, show_header=True,
        header_style="bold cyan", pad_edge=True,
    )
    table.add_column("#",       justify="right", style="dim", min_width=3,  no_wrap=True)
    table.add_column("代码",    style="dim",                  min_width=7,  no_wrap=True)
    table.add_column("名称",                                  min_width=8,  no_wrap=True)
    table.add_column("最新价",  justify="right",              min_width=7,  no_wrap=True)
    table.add_column("涨跌幅%", justify="right",              min_width=8,  no_wrap=True)
    table.add_column("成交量",  justify="right",              min_width=8,  no_wrap=True)

    for rank, (_, row) in enumerate(df.iterrows(), start=1):
        color = _color(row["pct"])
        pct   = row["pct"]
        price = row["price"]
        vol   = row["volume"]
        code  = row["code"]
        name  = _NAMES.get(code, row["name"])

        price_str = f"{price:.2f}" if pd.notna(price) else "N/A"
        pct_str   = f"{pct:+.2f}%" if pd.notna(pct)   else "N/A"

        if pd.isna(vol):     vol_str = "N/A"
        elif vol >= 1e8:     vol_str = f"{vol/1e8:.1f}亿"
        elif vol >= 1e4:     vol_str = f"{vol/1e4:.0f}万"
        else:                vol_str = str(int(vol))

        row_style = HIGHLIGHT_STYLE if code in highlighted else None
        table.add_row(
            str(rank), code,
            f"[{color}]{name}[/{color}]",
            f"[{color}]{price_str}[/{color}]",
            f"[{color}]{pct_str}[/{color}]",
            vol_str,
            style=row_style,
        )
    return table


# ── 整屏构建（纯渲染，不拉取数据）────────────────────────────────

def build_screen(interval: int, cats_data: list,
                 df_gain: pd.DataFrame, df_loss: pd.DataFrame, df_vol: pd.DataFrame,
                 highlighted: set = frozenset()) -> Group:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 自选股
    tables = [render_table(cat, data, highlighted) for cat, data in cats_data]
    row1 = Columns([tables[0], tables[1]], padding=(0, 3), equal=False)
    row2 = Columns([tables[2], tables[3]], padding=(0, 3), equal=False)
    watchlist = Group(row1, row2)

    # 榜单
    if df_gain.empty and df_loss.empty and df_vol.empty:
        leaderboard = Rule("[dim]榜单数据获取失败[/dim]")
    else:
        t1 = render_leaderboard("涨幅榜",   df_gain,  highlighted)
        t2 = render_leaderboard("跌幅榜",   df_loss,  highlighted)
        t3 = render_leaderboard("成交量榜", df_vol,   highlighted)
        leaderboard = Columns([t1, t2, t3], padding=(0, 3))

    content = Group(watchlist, Rule("  A 股榜单  "), leaderboard)
    panel = Panel(content, title="[bold yellow]中国股市实时监控[/bold yellow]",
                  border_style="blue")

    # 底部状态栏
    status = Table.grid(padding=(0, 2))
    status.add_column(justify="left")
    status.add_column(justify="center", ratio=1)
    status.add_column(justify="right")
    status.add_row(
        f"[dim]刷新间隔: [/dim][bold dim]{interval}s[/bold dim]",
        f"[dim]最后更新: [/dim][bold dim]{now}[/bold dim]"
        + (f"  [bold yellow]⚡ {len(highlighted)} 只股票变动[/bold yellow]" if highlighted else ""),
        "[dim]Ctrl+C 退出[/dim]",
    )

    return Group(panel, status)


# ── 主循环 ────────────────────────────────────────────────────

def _loading_panel() -> Panel:
    return Panel(
        "[bold yellow]正在加载数据…[/bold yellow]",
        title="[bold yellow]中国股市实时监控[/bold yellow]",
        border_style="blue",
    )


def main():
    parser = argparse.ArgumentParser(description="Yahoo Finance 中国股市终端监控")
    parser.add_argument("--interval", type=int, default=10,
                        help="刷新间隔秒数（默认 10）")
    args = parser.parse_args()
    interval = args.interval

    load_name_cache()

    try:
        with Live(_loading_panel(), console=console, screen=True, refresh_per_second=1) as live:
            # 首次拉取（初始化 _prev_pct，无高亮）
            cats_data, df_gain, df_loss, df_vol, all_pcts = fetch_all_data()
            compute_highlighted(all_pcts)   # 仅初始化 _prev_pct，不显示高亮
            live.update(build_screen(interval, cats_data, df_gain, df_loss, df_vol))

            while True:
                time.sleep(interval)

                # 拉取新数据
                cats_data, df_gain, df_loss, df_vol, all_pcts = fetch_all_data()
                highlighted = compute_highlighted(all_pcts)

                # 有变化：高亮显示 0.5 秒，再恢复正常
                if highlighted:
                    live.update(build_screen(interval, cats_data, df_gain, df_loss, df_vol, highlighted))
                    time.sleep(HIGHLIGHT_DURATION)

                live.update(build_screen(interval, cats_data, df_gain, df_loss, df_vol))

    except KeyboardInterrupt:
        console.print("\n[bold green]已退出监控。再见！[/bold green]")


if __name__ == "__main__":
    main()
