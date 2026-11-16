#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
覆盖上游 analysis 执行的入口：python .dsa/run_analysis.py <main.py 原生参数…>

为什么需要这一层：
- 上游 main.py 的 STOCK_LIST 只认股票，加密货币（BTC/ETH）压根拿不到行情 —— 全链路
  依赖 data_provider 里「A股 / 港股 / 美股 / 美股指数」四套口径，没有 crypto；
- devNotes 的「AI研判」页里，美股和加密货币是同一张自选表，用户不该为此切换两套配置。

做法：先用上游自带的 DataFetcherManager + StockTrendAnalyzer 把加密货币的数据准备好，
把「现价 / 日线 / 技术指标」写成一份紧凑的行情摘要；上游走完后再把这份摘要追加成一节
markdown，和其他股票的报告合并进同一份日报、同一份 .dsa/*.md。

加密货币没有财报、没有筹码分布，所以这一节只基于价格行为：
- 独立跑一遍 GeminiAnalyzer（prompt 里明确写「只按价格行为/技术面，别编造基本面」）
- 模型不可用或返回不可解析时退化成纯技术面摘要，至少不会让整份日报失败
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 和上游一致：引擎内部既 `from src.x` 也 `from data_provider`，两者都在仓库根下
CRYPTO_SUFFIX = "-USD"
CRYPTO_LABEL = {
    "BTC": "比特币",
    "ETH": "以太坊",
    "SOL": "Solana",
    "BNB": "BNB",
    "XRP": "XRP",
    "DOGE": "狗狗币",
    "ADA": "Cardano",
    "AVAX": "Avalanche",
    "LTC": "莱特币",
    "TON": "Toncoin",
    "LINK": "Chainlink",
}
BAR_DAYS = 120


def log(msg: str) -> None:
    print(f"[crypto] {msg}", flush=True)


def user_stock_list() -> str:
    """只读用户配置本身，不让上游帮忙拆分。

    上游 split_stock_list 是给股票 / 指数用的，会把 BTC、ETH 这类裸代码当成美股代码，
    所以这里必须先拿原始字符串自己分类，再决定哪些交给上游。
    """
    from src.config import Config

    return str(Config._resolve_env_value("STOCK_LIST", default="") or "")


def split_stock_list(raw: str) -> tuple[list[str], list[str]]:
    """把 STOCK_LIST 拆成（股票, 加密货币）；加密货币统一写成裸代码 BTC / ETH。"""
    from src.services.stock_list_parser import split_stock_list as upstream_split

    coins: list[str] = []
    for item in (raw or "").replace("，", ",").split(","):
        code = item.strip().upper()
        if not code:
            continue
        # 明确写成 BTC-USD 的一律当加密货币
        if code.endswith(CRYPTO_SUFFIX):
            coins.append(code[: -len(CRYPTO_SUFFIX)])
            continue
        # 裸代码：先看美股里有没有同名 ticker（BTC/ETH 这类常见币名不在美股列表里），
        # 命中已知币种且不是上游认识的股票写法时，按加密货币处理
        if code in CRYPTO_LABEL and not re.search(r"[.\d]", code):
            coins.append(code)
    stock_tokens = [t.strip() for t in upstream_split(raw or "") if t.strip()]
    known = set(coins)
    stocks = [
        t
        for t in stock_tokens
        # 上游把币种当美股代码收下了，这里剔掉；用户若真买了同名美股，请写成 XXX.US 之类避开
        if not (t.strip().upper().endswith(CRYPTO_SUFFIX) or t.strip().upper() in known)
    ]
    return stocks, coins


def collect_coin(coin: str) -> dict | None:
    """拉行情 + 算技术指标，返回一份可 JSON 化的快照；拿不到就返回 None。"""
    from data_provider import DataFetcherManager
    from src.stock_analyzer import StockTrendAnalyzer

    symbol = f"{coin}{CRYPTO_SUFFIX}"
    manager = DataFetcherManager()
    quote = manager.get_realtime_quote(symbol, log_final_failure=False)

    end = datetime.now().date()
    start = end - timedelta(days=BAR_DAYS * 2)
    df, error = manager.get_daily_data(symbol, start.isoformat(), end.isoformat(), days=BAR_DAYS)
    if df is None or not hasattr(df, "columns") or len(df) < 20:
        log(f"{coin}: 日线不足（{error or 'empty'}），跳过")
        return None
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.tail(BAR_DAYS)
    trend = StockTrendAnalyzer().analyze(df, symbol)

    last = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(getattr(quote, "price", 0) or last.get("close") or 0)
    prev_close = float(last.get("close") or 0)
    change_pct = (price / prev_close - 1) * 100 if prev_close else 0.0
    fields = {
        "现价": price,
        "今日涨跌幅%": round(change_pct, 2),
        "MA5": getattr(trend, "ma5", None),
        "MA10": getattr(trend, "ma10", None),
        "MA20": getattr(trend, "ma20", None),
        "MA60": getattr(trend, "ma60", None),
        "RSI6": getattr(trend, "rsi_6", None),
        "RSI12": getattr(trend, "rsi_12", None),
        "MACD DIF": getattr(trend, "macd_dif", None),
        "MACD DEA": getattr(trend, "macd_dea", None),
        "支撑位": getattr(trend, "support_level", None),
        "压力位": getattr(trend, "resistance_level", None),
    }
    recent = df.tail(20)
    return {
        "coin": coin,
        "symbol": symbol,
        "name": CRYPTO_LABEL.get(coin, coin),
        "price": price,
        "prevClose": float(prev.get("close") or 0),
        "changePct": round(change_pct, 2),
        "trend": str(getattr(trend, "trend_status", "") or ""),
        "metrics": {k: v for k, v in fields.items() if isinstance(v, (int, float)) and v == v},
        "high20": float(recent["high"].max()) if "high" in recent else None,
        "low20": float(recent["low"].min()) if "low" in recent else None,
        "avgVol20": float(recent["volume"].mean()) if "volume" in recent else None,
        "lastDate": str(last.get("date", ""))[:10],
    }


def coin_prompt(snapshot: dict) -> str:
    metrics = "\n".join(f"- {k}: {v:.4g}" if isinstance(v, float) else f"- {k}: {v}" for k, v in snapshot["metrics"].items())
    return f"""你是一名只做价格行为（price action）与资金结构的加密货币分析师。
下面只有 {snapshot['coin']}（{snapshot['name']}）的日线技术数据，没有链上数据、没有新闻、没有宏观信息，
禁止编造任何基本面、新闻或资金流数字；不确定的地方直接写「数据缺失」。

{snapshot['symbol']} 最新价 {snapshot['price']:.4f}（昨收 {snapshot['prevClose']:.4f}，今日 {snapshot['changePct']:+.2f}%）
趋势判定：{snapshot['trend']}
- 近 20 日最高: {snapshot['high20']}
- 近 20 日最低: {snapshot['low20']}
- 20 日均量: {snapshot['avgVol20']}
指标：
{metrics}

请严格输出以下 markdown 结构（不要加代码块围栏、不要输出其它内容）：
### 💭 一句话结论
<不超过 40 字，含方向与关键价位>

### 📌 核心结论
- 方向：看多 / 震荡 / 看空
- 评分：0-100 的整数
- 一句风险提示

### 📈 关键价位
- 支撑：<价格>（依据：哪条均线或前低）
- 压力：<价格>（依据：哪条均线或前高）
- 失效条件：<跌破/突破哪里即判断作废>

### ⚡ 策略
- 进场思路：<等什么信号，不要给具体仓位>
- 止损参考：<价位>
- 目标位：<价位>

> 仅为技术面参考，不构成投资建议。"""


def coin_section(snapshot: dict) -> str:
    """模型不可用时的纯技术面兜底：结构化表格 + 一句方向判断。"""
    rows = "\n".join(f"| {k} | {v:.4g} |" if isinstance(v, float) else f"| {k} | {v} |" for k, v in snapshot["metrics"].items())
    direction = "偏强" if "多头" in snapshot["trend"] else ("偏弱" if "空头" in snapshot["trend"] else "震荡")
    return f"""### 💭 一句话结论
技术面{direction}，模型研判暂不可用，仅给出指标参考。

### 📈 指标快照
| 指标 | 数值 |
|------|------|
{rows}

> 仅为技术面参考，不构成投资建议。"""


def run_coin_analysis(snapshot: dict) -> str:
    try:
        from src.analyzer import GeminiAnalyzer

        analyzer = GeminiAnalyzer(config=get_config())
        result = analyzer._call_litellm(
            prompt=coin_prompt(snapshot),
            generation_config={"temperature": 0.2, "max_output_tokens": 900},
            system_prompt="你是严谨的技术面分析师，只基于给定数据回答，不编造信息。",
        )
        text = result[0] if isinstance(result, tuple) else result
        if isinstance(text, str) and "###" in text:
            return text.strip()
        log(f"{snapshot['coin']}: 模型返回不可用，退化为技术面表格")
    except Exception as exc:  # noqa: BLE001 - 币种分析失败不能拖垮整份日报
        log(f"{snapshot['coin']}: 模型调用失败 {exc}，退化为技术面表格")
    return coin_section(snapshot)


def crypto_block(coins: list[str]) -> str:
    sections = []
    for coin in coins:
        log(f"分析 {coin}…")
        snapshot = collect_coin(coin)
        if not snapshot:
            continue
        body = run_coin_analysis(snapshot)
        sections.append(
            f"\n## ⚪ {snapshot['name']} ({coin})\n\n"
            f"> 加密货币 · 日线技术面 · {snapshot['lastDate']}\n\n{body}\n"
        )
    if not sections:
        return ""
    return "\n---\n\n## ₿ 加密货币研判\n" + "".join(sections)


def main() -> int:
    setup_env()
    raw = user_stock_list()
    stocks, coins = split_stock_list(raw)
    log(f"股票 {len(stocks)} 只，加密货币 {len(coins)} 个：{','.join(coins) or '无'}")

    env = dict(os.environ)
    # 只把股票交给上游：上游遇到 BTC-USD 会因为找不到数据源整份失败
    env["STOCK_LIST"] = ",".join(stocks) or "AAPL"
    native_args = sys.argv[1:]
    if stocks:
        code = subprocess.run([sys.executable, str(ROOT / "main.py"), *native_args], cwd=ROOT, env=env).returncode
        if code != 0:
            log(f"上游分析退出码 {code}，仍尝试补写加密货币部分")
    else:
        log("没有股票代码，跳过上游分析（只跑加密货币）")

    if not coins:
        return 0

    block = crypto_block(coins)
    if not block:
        log("加密货币部分没有可用数据")
        return 0

    reports_dir = ROOT / "reports"
    reports_dir.mkdir(exist_ok=True)
    path = reports_dir / f"report_{datetime.now().strftime('%Y%m%d')}.md"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        # 去掉上一轮追加的加密货币段，避免同一天多次运行时重复堆叠
        marker = "\n---\n\n## ₿ 加密货币研判"
        if marker in existing:
            existing = existing.split(marker)[0]
        path.write_text(existing.rstrip() + "\n" + block, encoding="utf-8")
    else:
        header = f"# 🎯 {datetime.now().strftime('%Y-%m-%d')} 决策仪表盘\n\n> 仅加密货币分析（未配置股票）\n"
        path.write_text(header + block, encoding="utf-8")
    log(f"已写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
