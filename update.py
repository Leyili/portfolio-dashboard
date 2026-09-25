#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
持仓看板更新脚本

职责：
  1. 读取 holdings.json（持仓数量 / 成本 / 账户现金 / 融资 / 退休目标）
  2. 从东方财富抓取实时行情
  3. 自动补齐历史缺失交易日的净资产快照
  4. 计算账户、个股、集中度、目标完成度
  5. 渲染 dashboard.html（单文件自包含，可直接托管或双击打开）

用法：
  python update.py              # 抓行情并刷新看板（默认明文，本地直接双击看）
  python update.py --offline    # 不联网，用已有历史数据重算页面
  python update.py --no-encrypt # 强制明文，明文页面切勿上传公网
  python update.py --encrypt    # 加密生成，会询问密码并可选存入 secret.txt
  python update.py --password 密码            # 用指定密码加密
  python update.py --pack / --unpack          # 把 holdings.json 加/解密成 .enc
  python update.py --skip-if-closed           # 非交易日（周末/法定节假日）直接跳过，不写文件

密码来源（按顺序）：
  1. 环境变量 DASHBOARD_PASSWORD   ← GitHub Actions 用这个（存在仓库 Secrets 里）
  2. 同目录 secret.txt             ← 本地第一次运行会自动询问并保存（已被 .gitignore 排除）
  3. 命令行 --password
  4. 交互式输入
"""

import getpass
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, date, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
HOLDINGS_PATH = os.path.join(BASE, "holdings.json")
TEMPLATE_PATH = os.path.join(BASE, "template.html")
OUTPUT_PATH = os.path.join(BASE, "dashboard.html")
SECRET_PATH = os.path.join(BASE, "secret.txt")
PACKED_PATH = os.path.join(BASE, "holdings.json.enc")
CALENDAR_PATH = os.path.join(BASE, "trade_calendar.json")

# 服务器（GitHub Actions runner）是 UTC，A 股按北京时间走，必须显式换算
CN_TZ = timezone(timedelta(hours=8))

sys.path.insert(0, BASE)
try:
    import crypto_lite
except ImportError:
    crypto_lite = None

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TIMEOUT = 15


# ----------------------------------------------------------------------------
# 数据读写
# ----------------------------------------------------------------------------

def load_holdings():
    with open(HOLDINGS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_holdings(data):
    with open(HOLDINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------------
# 交易日历（周末 + 法定节假日）
# ----------------------------------------------------------------------------

_CALENDAR_CACHE = None


def load_calendar():
    """返回 {日期: True} 的交易日集合。文件缺失时返回空集合（退化为只判断周末）。"""
    global _CALENDAR_CACHE
    if _CALENDAR_CACHE is None:
        try:
            with open(CALENDAR_PATH, "r", encoding="utf-8") as f:
                obj = json.load(f)
            _CALENDAR_CACHE = {d: True for d in obj.get("trading_days", [])}
        except Exception:
            _CALENDAR_CACHE = {}
    return _CALENDAR_CACHE


def cn_today():
    """按北京时间取当天日期字符串。"""
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def _calendar_range(cal):
    ks = sorted(cal)
    return (ks[0], ks[-1]) if ks else (None, None)


def is_trading_day(day):
    """是否为 A 股交易日。

    1) 日期在 trade_calendar.json 覆盖范围内 → 直接查表（含法定节假日休市）；
    2) 超出覆盖范围（日历未更新到该年份）→ 退化为「周一至周五」判断，
       宁可按工作日更新，也不能因为日历过期而永久停更。
    """
    cal = load_calendar()
    if cal:
        if day in cal:
            return True
        lo, hi = _calendar_range(cal)
        if lo and lo <= day <= hi:
            return False  # 覆盖范围内但不在交易日列表里 = 休市
    try:
        return date.fromisoformat(day).weekday() < 5
    except Exception:
        return True


def _shift(day, step, limit=40):
    """按日历（覆盖范围内）或周一至周五（超出范围）找相邻交易日。"""
    cal = load_calendar()
    lo, hi = _calendar_range(cal)
    cur = date.fromisoformat(day)
    for _ in range(limit):
        cur += timedelta(days=step)
        s = cur.isoformat()
        if lo and lo <= s <= hi:
            if s in cal:
                return s
        elif cur.weekday() < 5:
            return s
    return None


def next_trading_day(day):
    """返回 > day 的下一个交易日。"""
    return _shift(day, 1)


def prev_trading_day(day):
    """返回 < day 的最近一个交易日。"""
    return _shift(day, -1)


# ----------------------------------------------------------------------------
# 行情抓取
# ----------------------------------------------------------------------------

def http_json(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(1.2 * (i + 1))
    raise last


def http_text(url, retries=3, referer=None):
    last = None
    for i in range(retries):
        try:
            heads = dict(UA)
            if referer:
                heads["Referer"] = referer
            req = urllib.request.Request(url, headers=heads)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("gbk", "ignore")
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(1.2 * (i + 1))
    raise last


def _quotes_em(holdings):
    """东方财富批量实时行情"""
    secids = []
    seen = set()
    for h in holdings:
        key = "%s.%s" % (h["market"], h["code"])
        if key not in seen:
            seen.add(key)
            secids.append(key)
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?"
           "secids=%s&fields=f12,f14,f2,f3,f4&fltt=2" % ",".join(secids))
    raw = http_json(url)
    out = {}
    for item in (raw.get("data") or {}).get("diff") or []:
        code = str(item.get("f12"))
        price = item.get("f2")
        if price in (None, "-"):
            continue
        out[code] = {
            "price": float(price),
            "pct": float(item.get("f3") or 0),
            "change": float(item.get("f4") or 0),
        }
    return out


def _quotes_sina(holdings):
    """新浪实时行情（字段：名称/今开/昨收/现价/最高/最低）"""
    syms = []
    seen = set()
    for h in holdings:
        sym = _sym_prefix(h["market"]) + h["code"]
        if sym not in seen:
            seen.add(sym)
            syms.append(sym)
    url = "https://hq.sinajs.cn/list=" + ",".join(syms)
    text = http_text(url, referer="https://finance.sina.com.cn")
    out = {}
    for line in text.split(";"):
        if "hq_str_" not in line or '"' not in line:
            continue
        try:
            sym = line.split("hq_str_")[1].split("=")[0].strip()
            body = line.split('"')[1]
        except IndexError:
            continue
        parts = body.split(",")
        if len(parts) < 4:
            continue
        try:
            prev_close = float(parts[2])
            price = float(parts[3])
        except ValueError:
            continue
        if price <= 0 or prev_close <= 0:
            continue
        out[sym[2:]] = {
            "price": price,
            "change": price - prev_close,
            "pct": (price - prev_close) / prev_close * 100,
        }
    return out


def fetch_quotes(holdings):
    """批量抓取实时行情，返回 {code: {price, pct, change}}。多数据源依次降级。"""
    last = None
    for fn in (_quotes_em, _quotes_sina):
        try:
            out = fn(holdings)
            if out:
                return out
        except Exception as e:
            last = e
    raise RuntimeError("实时行情获取失败（%s）" % last)


def _sym_prefix(market):
    return "sh" if int(market) == 1 else "sz"


def _kline_sina(market, code, limit):
    """新浪日线（不复权）"""
    sym = _sym_prefix(market) + code
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (sym, limit))
    raw = http_json(url)
    out = {}
    for it in raw or []:
        try:
            out[str(it["day"])[:10]] = float(it["close"])
        except Exception:
            continue
    return out


def _kline_tencent(market, code, limit):
    """腾讯日线（前复权，列序为 日期/开/收/高/低/量）"""
    sym = _sym_prefix(market) + code
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           "param=%s,day,,,%d,qfq" % (sym, limit))
    raw = http_json(url)
    node = (raw.get("data") or {}).get(sym) or {}
    rows = node.get("qfqday") or node.get("day") or []
    out = {}
    for r in rows:
        try:
            out[str(r[0])[:10]] = float(r[2])
        except Exception:
            continue
    return out


def _kline_em(market, code, limit):
    """东方财富日线（不复权）"""
    secid = "%s.%s" % (market, code)
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           "secid=%s&klt=101&fqt=0&lmt=%d&end=20500101&"
           "fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f53" % (secid, limit))
    raw = http_json(url, retries=2)
    out = {}
    for line in (raw.get("data") or {}).get("klines") or []:
        parts = line.split(",")
        if len(parts) >= 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                continue
    return out


def fetch_kline(market, code, limit=45):
    """日线收盘价，返回 {日期: 收盘价}。多数据源依次降级。"""
    last = None
    for fn in (_kline_sina, _kline_tencent, _kline_em):
        try:
            out = fn(market, code, limit)
            if out:
                return out
        except Exception as e:
            last = e
    raise RuntimeError("K 线数据获取失败（%s）" % last)


# ----------------------------------------------------------------------------
# 计算
# ----------------------------------------------------------------------------

def net_asset_on(date, holdings, accounts, klines):
    """按某日收盘价估算净资产，用于补齐历史缺口"""
    total_stock = 0.0
    for h in holdings:
        series = klines.get(h["code"]) or {}
        price = series.get(date)
        if price is None:
            earlier = [d for d in sorted(series) if d <= date]
            price = series[earlier[-1]] if earlier else None
        if price is None:
            continue
        total_stock += price * h["shares"]
    cash = sum(a.get("cash", 0.0) for a in accounts)
    debt = sum(a.get("margin_debt", 0.0) for a in accounts)
    return round(total_stock + cash - debt, 2)


def backfill_history(data, klines, today, calendar=None):
    """补齐 history 中缺失的交易日（用当日收盘价反推净资产）"""
    ref = calendar
    if not ref:
        for series in klines.values():
            if series and (ref is None or len(series) > len(ref)):
                ref = series
    if not ref:
        return 0

    trading_days = sorted(ref.keys())
    have = {h["date"] for h in data.get("history", [])}
    earliest = min(have) if have else None
    # 只补「已有记录起点之后」且「早于今天」的交易日：
    # 早于起点的日期持仓可能不同，反推不准；今天由实时行情负责写入
    missing = [d for d in trading_days
               if d < today and d not in have
               and (earliest is None or d >= earliest)]

    added = 0
    for d in missing:
        value = net_asset_on(d, data["holdings"], data["accounts"], klines)
        if value <= 0:
            continue
        data.setdefault("history", []).append({"date": d, "net_asset": value})
        added += 1

    data["history"].sort(key=lambda x: x["date"])
    return added


def compute(data, quotes, quote_date, trading_today):
    accounts = {a["id"]: a for a in data["accounts"]}
    meta = data["meta"]

    rows = []
    for h in data["holdings"]:
        q = quotes.get(h["code"])
        if not q:
            continue
        shares = h["shares"]
        price = q["price"]
        cost = h.get("cost")
        market_value = shares * price
        day_pnl = shares * q["change"]
        cost_amount = shares * cost if cost else None
        total_pnl = market_value - cost_amount if cost_amount else None
        rows.append({
            "code": h["code"],
            "name": h["name"],
            "account": h["account"],
            "account_name": accounts.get(h["account"], {}).get("name", h["account"]),
            "shares": shares,
            "cost": cost,
            "price": price,
            "prev_close": round(price - q["change"], 3),
            "change_pct": q["pct"],
            "market_value": market_value,
            "day_pnl": day_pnl,
            "cost_amount": cost_amount,
            "total_pnl": total_pnl,
            "pnl_pct": (total_pnl / cost_amount * 100) if cost_amount else None,
        })

    # 分账户汇总
    acc_rows = []
    for a in data["accounts"]:
        items = [r for r in rows if r["account"] == a["id"]]
        stock_value = sum(r["market_value"] for r in items)
        day_pnl = sum(r["day_pnl"] for r in items)
        cost = sum(r["cost_amount"] for r in items if r["cost_amount"])
        float_pnl = stock_value - cost if cost else 0.0
        net = stock_value + a.get("cash", 0.0) - a.get("margin_debt", 0.0)
        acc_rows.append({
            "id": a["id"],
            "name": a["name"],
            "cash": a.get("cash", 0.0),
            "margin_debt": a.get("margin_debt", 0.0),
            "stock_value": stock_value,
            "day_pnl": day_pnl,
            "float_pnl": float_pnl,
            "float_pnl_pct": (float_pnl / cost * 100) if cost else None,
            "net": net,
            "holdings": items,
        })

    total_market_value = sum(r["market_value"] for r in rows)
    total_cash = sum(a.get("cash", 0.0) for a in data["accounts"])
    total_margin = sum(a.get("margin_debt", 0.0) for a in data["accounts"])
    net_asset = total_market_value + total_cash - total_margin
    total_cost = sum(r["cost_amount"] for r in rows if r["cost_amount"])
    float_pnl = total_market_value - total_cost
    day_pnl = sum(r["day_pnl"] for r in rows)

    # 当日净资产涨跌：与上一交易日快照比较
    hist = sorted(data.get("history", []), key=lambda x: x["date"])
    prev_snapshot = None
    for h in reversed(hist):
        if h["date"] < quote_date:
            prev_snapshot = h
            break
    prev_net = prev_snapshot["net_asset"] if prev_snapshot else None
    day_pnl_net = (net_asset - prev_net) if prev_net else 0.0
    day_pnl_pct = (day_pnl_net / prev_net * 100) if prev_net else 0.0

    target = meta.get("target_net_asset", 6000000.0)
    years = meta.get("years_to_retire", 27)
    progress = net_asset / target * 100 if target else 0.0
    gap = target - net_asset
    required_cagr = ((target / net_asset) ** (1.0 / years) - 1) * 100 if net_asset > 0 and years else 0.0

    # 集中度（按净资产）
    conc = {}
    for r in rows:
        c = conc.setdefault(r["code"], {"code": r["code"], "name": r["name"],
                                        "shares": 0, "market_value": 0.0})
        c["shares"] += r["shares"]
        c["market_value"] += r["market_value"]
    concentration = sorted(conc.values(), key=lambda x: -x["market_value"])
    for c in concentration:
        c["pct"] = c["market_value"] / net_asset * 100 if net_asset else 0.0
    if total_cash:
        concentration.append({"code": "CASH", "name": "可用现金", "shares": 0,
                              "market_value": total_cash,
                              "pct": total_cash / net_asset * 100 if net_asset else 0.0})

    # 历史序列（含今日）
    if hist and hist[-1]["date"] == quote_date:
        hist[-1]["net_asset"] = round(net_asset, 2)
    else:
        hist.append({"date": quote_date, "net_asset": round(net_asset, 2)})
    hist.sort(key=lambda x: x["date"])

    series = []
    for i, h in enumerate(hist[-30:]):
        prev = hist[-30:][i - 1]["net_asset"] if i > 0 else None
        chg = (h["net_asset"] - prev) if prev is not None else 0.0
        series.append({
            "date": h["date"],
            "net_asset": h["net_asset"],
            "change": chg,
            "change_pct": (chg / prev * 100) if prev else 0.0,
            "progress": h["net_asset"] / target * 100 if target else 0.0,
            "gap": target - h["net_asset"],
        })

    summary = {
        "net_asset": net_asset,
        "prev_net_asset": prev_net,
        "day_pnl": day_pnl_net,
        "day_pnl_pct": day_pnl_pct,
        "stock_day_pnl": day_pnl,
        "total_market_value": total_market_value,
        "total_cash": total_cash,
        "total_margin": total_margin,
        "total_cost": total_cost,
        "float_pnl": float_pnl,
        "float_pnl_pct": (float_pnl / total_cost * 100) if total_cost else 0.0,
        "target": target,
        "progress": progress,
        "gap": gap,
        "years": years,
        "retire_age": meta.get("retire_age", 60),
        "required_cagr": required_cagr,
        "margin_ratio": (total_margin / net_asset * 100) if net_asset else 0.0,
        "as_of": quote_date,
        "generated_at": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "trading_today": trading_today,
        "owner": meta.get("owner", ""),
        # 休市期间不报「数据陈旧」：只要数据截止日已是最近一个交易日就算正常
        "last_trading_day": (cn_today() if trading_today
                             else (prev_trading_day(cn_today()) or quote_date)),
        "next_trading_day": next_trading_day(cn_today()) or "",
    }

    return {
        "summary": summary,
        "accounts": acc_rows,
        "holdings": sorted(rows, key=lambda x: -x["market_value"]),
        "concentration": concentration,
        "history": series,
    }


# ----------------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------------

def _ask(prompt):
    try:
        return getpass.getpass(prompt)
    except Exception:
        return input(prompt)


def resolve_password(argv):
    """决定本次是否加密，返回 None 表示生成明文页面。

    优先级：--no-encrypt > --password > 环境变量 > --encrypt（交互询问） > secret.txt > 明文
    本地双击默认是明文，方便直接看；GitHub Actions 靠环境变量注入密码，产出加密页。
    """
    if "--no-encrypt" in argv:
        return None

    for i, a in enumerate(argv):
        if a == "--password" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--password="):
            return a.split("=", 1)[1]

    env_pw = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if env_pw:
        return env_pw

    if "--encrypt" in argv:
        return _ask_new_password()

    if os.path.exists(SECRET_PATH):
        saved = open(SECRET_PATH, "r", encoding="utf-8").read().strip()
        if saved:
            return saved

    return None


def _ask_new_password():
    if crypto_lite is None:
        print("[错误] 缺少 crypto_lite.py，无法加密")
        sys.exit(1)
    print("")
    print("  数据将以 AES-256-GCM 加密后写入页面，打开时需输入密码。")
    while True:
        pw = _ask("  设置密码（输入不回显，至少 6 位）：")
        if len(pw) < 6:
            print("  太短，请重设")
            continue
        if pw != _ask("  再输一次确认："):
            print("  两次不一致，请重来")
            continue
        break
    ans = input("  保存到 secret.txt 以便以后自动使用？(Y/n) ").strip().lower()
    if ans in ("", "y", "yes"):
        with open(SECRET_PATH, "w", encoding="utf-8") as f:
            f.write(pw)
        print("  已保存（该文件已排除在 git 之外，切勿提交）")
    return pw


def pack_holdings(password):
    """把 holdings.json 加密成 holdings.json.enc —— 唯一能提交到公开仓库的持仓文件"""
    if crypto_lite is None:
        print("[错误] 缺少 crypto_lite.py，无法加密")
        sys.exit(1)
    blob = crypto_lite.encrypt(open(HOLDINGS_PATH, "rb").read(), password)
    with open(PACKED_PATH, "w", encoding="utf-8") as f:
        json.dump(blob, f)
    print("[打包] holdings.json → holdings.json.enc（%.1f KB 密文）"
          % (os.path.getsize(PACKED_PATH) / 1024))


def unpack_holdings(password):
    """把 holdings.json.enc 解密回 holdings.json"""
    if crypto_lite is None:
        print("[错误] 缺少 crypto_lite.py，无法解密")
        sys.exit(1)
    blob = json.load(open(PACKED_PATH, "r", encoding="utf-8"))
    with open(HOLDINGS_PATH, "wb") as f:
        f.write(crypto_lite.decrypt(blob, password))
    print("[解包] holdings.json.enc → holdings.json")


def render(calc, password=None):
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        tpl = f.read()
    payload = json.dumps(calc, ensure_ascii=False, separators=(",", ":"))

    if password:
        if crypto_lite is None:
            print("[错误] 缺少 crypto_lite.py，无法加密。请改用 --no-encrypt")
            sys.exit(1)
        blob = crypto_lite.encrypt(payload.encode("utf-8"), password)
        embedded = json.dumps(dict(enc=True, **blob),
                              ensure_ascii=False, separators=(",", ":"))
    else:
        embedded = '{"enc":false,"data":' + payload + '}'

    html = tpl.replace("__PAYLOAD__", embedded)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    return OUTPUT_PATH


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def main():
    offline = "--offline" in sys.argv

    # 给 CI 用的探针：只报今天是不是交易日，不联网、不写任何文件
    if "--check-trading-day" in sys.argv:
        day = cn_today()
        ok = is_trading_day(day)
        print("TODAY=%s" % day)
        print("TRADING_DAY=%s" % ("true" if ok else "false"))
        prv = prev_trading_day(day)
        nxt = next_trading_day(day)
        if prv:
            print("PREV=%s" % prv)
        if nxt:
            print("NEXT=%s" % nxt)
        return

    if "--pack" in sys.argv or "--unpack" in sys.argv:
        pw = resolve_password(sys.argv) or _ask("  密码（用于加/解密持仓文件）：")
        if not pw:
            print("[错误] 打包/解包需要密码")
            sys.exit(1)
        if "--pack" in sys.argv:
            pack_holdings(pw)
        else:
            unpack_holdings(pw)
        return

    # 统一按北京时间取「今天」——runner 在 UTC 时区，直接用 datetime.now() 会差 8 小时
    today = cn_today()
    data = load_holdings()

    if "--skip-if-closed" in sys.argv and not is_trading_day(today):
        nxt = next_trading_day(today)
        prv = prev_trading_day(today)
        print("[跳过] %s 不是 A 股交易日（周末或法定节假日），不更新数据。" % today)
        if prv:
            print("  最近交易日：%s" % prv)
        if nxt:
            print("  下一交易日：%s" % nxt)
        print("  页面与持仓文件保持原样，本次不产生提交。")
        return

    if not is_trading_day(today):
        print("[提示] %s 非交易日，仍按最近交易日数据渲染页面" % today)

    quotes = {}
    klines = {}
    trading_today = False
    quote_date = today

    if not offline:
        try:
            quotes = fetch_quotes(data["holdings"])
        except Exception as e:
            print("[警告] 实时行情抓取失败：%s" % e)

        # 基准 K 线：确定交易日历，以及行情数据归属哪一天。
        # 盘前/未开盘时接口仍返回上一交易日收盘价，此时不能记成今天。
        ref_h = data["holdings"][0]
        try:
            base_kline = fetch_kline(ref_h["market"], ref_h["code"])
        except Exception as e:
            print("[警告] 基准 K 线抓取失败：%s" % e)
            base_kline = {}

        last_day = max(base_kline) if base_kline else None
        trading_today = bool(last_day) and last_day == today
        quote_date = today if trading_today else (last_day or today)

        # 仅在确实存在缺口时才拉取全部个股 K 线，避免无谓请求被限流
        have = {h["date"] for h in data.get("history", [])}
        earliest = min(have) if have else None
        missing = [d for d in sorted(base_kline)
                   if d < today and d not in have
                   and (earliest is None or d >= earliest)]

        if missing:
            codes = {}
            for h in data["holdings"]:
                codes.setdefault(h["code"], h["market"])
            for code, market in codes.items():
                try:
                    klines[code] = fetch_kline(market, code)
                    time.sleep(0.35)
                except Exception as e:
                    print("[警告] %s 历史数据抓取失败：%s" % (code, e))
            added = backfill_history(data, klines, today, base_kline)
            if added:
                print("[补齐] 自动补回 %d 个缺失交易日的净资产快照" % added)
        elif quotes and not base_kline:
            print("[提示] 未取到交易日历，跳过历史补齐")

    if not quotes and not offline:
        print("[提示] 未取到实时行情，改用最近收盘价渲染")

    calc = compute(data, quotes or _fallback_quotes(data, klines), quote_date, trading_today)

    # 保护：一只行情都没取到时，算出来的净资产是错的，绝不落盘
    if not calc["holdings"]:
        print("[错误] 未取到任何行情数据，放弃本次更新（历史与页面保持原样）")
        sys.exit(1)

    if not offline:
        save_holdings(data)

    password = resolve_password(sys.argv)
    path = render(calc, password)
    s = calc["summary"]
    print("[完成] %s" % path)
    print("  净资产      {:,.2f} 元".format(s["net_asset"]))
    print("  目标完成度  {:.2f}%".format(s["progress"]))
    print("  当日盈亏    {:,.2f} 元".format(s["day_pnl"]))
    print("  数据截止    %s" % s["as_of"])
    print("  加密        %s" % ("是，打开需输入密码" if password else "否，明文页面请勿上传公网"))


def _fallback_quotes(data, klines):
    """行情接口失败时，用最近收盘价兜底"""
    out = {}
    for h in data["holdings"]:
        series = klines.get(h["code"]) or {}
        if not series:
            continue
        dates = sorted(series)
        price = series[dates[-1]]
        prev = series[dates[-2]] if len(dates) > 1 else price
        out[h["code"]] = {"price": price, "change": price - prev,
                          "pct": ((price - prev) / prev * 100) if prev else 0.0}
    return out


if __name__ == "__main__":
    main()
