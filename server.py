"""
BingX MA + Fibonacci 篩選器（永續合約版）
python server.py
"""
import threading, webbrowser, time, random, traceback, hmac, hashlib, json, os, signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode
from flask import Flask, jsonify, request, render_template
import requests

app = Flask(__name__, template_folder='templates', static_folder='static')
tp2_watchers = {}   # watcher_id → {symbol, orderId, tp2, qty, status, msg}
BINGX      = "https://open-api.bingx.com"
BINGX_DEMO = "https://open-api-vst.bingx.com"   # VST 模擬盤

# ── 設定檔 ────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

def load_config():
    # 雲端優先從環境變數讀取，本機則讀 config.json
    env_key    = os.environ.get("BINGX_API_KEY", "")
    env_secret = os.environ.get("BINGX_API_SECRET", "")
    env_demo   = os.environ.get("BINGX_DEMO_MODE", "").lower()
    if env_key and env_secret:
        return {
            "api_key":    env_key,
            "api_secret": env_secret,
            "demo_mode":  env_demo != "false",
        }
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {"api_key": "", "api_secret": "", "demo_mode": True}

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

# ── HTTP（公開）───────────────────────────────────────────────────────────────
def bget(path, params=None):
    try:
        r = requests.get(BINGX + path, params=params or {}, timeout=12)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        raise Exception("連線逾時，請確認網路")
    except requests.exceptions.ConnectionError:
        raise Exception("無法連線 BingX，請確認網路")
    except Exception as e:
        raise Exception(f"API錯誤: {e}")

# ── 簽名 & 認證請求 ───────────────────────────────────────────────────────────
def _sign(qs: str, secret: str) -> str:
    """對 query string 做 HMAC-SHA256，回傳 hex digest"""
    return hmac.new(secret.encode('utf-8'), qs.encode('utf-8'), hashlib.sha256).hexdigest()

def _build_qs(params: dict) -> str:
    """
    BingX 官方簽名規則（與官方 demo parseParam 一致）：
      1. 其他參數按 key 字母排序
      2. timestamp 固定附加在最後
      3. 值不做 URL encoding，使用 raw 字串
    """
    ts = int(time.time() * 1000)
    sorted_pairs = sorted(params.items(), key=lambda x: x[0])
    qs = "&".join(f"{k}={v}" for k, v in sorted_pairs)
    qs += f"&timestamp={ts}"
    return qs

def bget_auth(path, params=None):
    """需要簽名的 GET 請求"""
    cfg = load_config()
    key, secret = cfg.get("api_key",""), cfg.get("api_secret","")
    if not key or not secret:
        raise Exception("尚未設定 API Key / Secret")
    base = BINGX_DEMO if cfg.get("demo_mode", True) else BINGX
    qs  = _build_qs(dict(params or {}))
    sig = _sign(qs, secret)
    url = f"{base}{path}?{qs}&signature={sig}"
    r = requests.get(url, headers={"X-BX-APIKEY": key}, timeout=12)
    r.raise_for_status()
    return r.json()

def bpost_auth(path, body=None):
    """需要簽名的 POST 請求（params 全放 URL query string，body 為空）"""
    cfg = load_config()
    key, secret = cfg.get("api_key",""), cfg.get("api_secret","")
    if not key or not secret:
        raise Exception("尚未設定 API Key / Secret")
    base = BINGX_DEMO if cfg.get("demo_mode", True) else BINGX
    qs  = _build_qs(dict(body or {}))
    sig = _sign(qs, secret)
    url = f"{base}{path}?{qs}&signature={sig}"
    r = requests.post(url, headers={"X-BX-APIKEY": key}, timeout=12)
    r.raise_for_status()
    return r.json()

# ── Tickers ───────────────────────────────────────────────────────────────────
def get_all_tickers():
    data = bget("/openApi/swap/v2/quote/ticker")
    result = {}
    for t in data.get("data", []):
        sym = t.get("symbol", "")
        if sym.endswith("-USDT"):
            result[sym] = {
                "changePct": float(t.get("priceChangePercent", 0)),
                "quoteVol":  float(t.get("quoteVolume", t.get("volume", 0))),
                "lastPrice": float(t.get("lastPrice", 0)),
            }
    return result

# ── K線 ───────────────────────────────────────────────────────────────────────
def get_klines(symbol, interval, limit=200):
    data = bget("/openApi/swap/v3/quote/klines",
                {"symbol": symbol, "interval": interval, "limit": limit})
    result = []
    for k in data.get("data", []):
        if isinstance(k, dict):
            result.append([
                int(k.get("time", k.get("openTime", 0))),
                float(k.get("open",   0)),
                float(k.get("high",   0)),
                float(k.get("low",    0)),
                float(k.get("close",  0)),
                float(k.get("volume", 0)),
            ])
        elif isinstance(k, (list, tuple)) and len(k) >= 6:
            result.append([int(k[0]), float(k[1]), float(k[2]),
                           float(k[3]), float(k[4]), float(k[5])])
    result.sort(key=lambda x: x[0])
    return result

# ── 時間週期 ──────────────────────────────────────────────────────────────────
def parse_iv(raw):
    raw = raw.strip().lower()
    if raw.isdigit(): return raw + "m"
    return {"1min":"1m","3min":"3m","5min":"5m","15min":"15m","30min":"30m",
            "60m":"1h","1hour":"1h","4hour":"4h","day":"1d","week":"1w"}.get(raw, raw)

# ── MA 計算 ───────────────────────────────────────────────────────────────────
def sma(closes, p):
    if len(closes) < p: return None
    return sum(closes[-p:]) / p

def ema(closes, p):
    if len(closes) < p: return None
    k = 2 / (p + 1)
    v = sum(closes[:p]) / p
    for c in closes[p:]: v = c * k + v * (1 - k)
    return v

def ma_series(closes, p, tp):
    fn = ema if tp == "EMA" else sma
    out = []
    for i in range(len(closes)):
        out.append(fn(closes[:i+1], p))
    return out

# ── Fibonacci 層級 ────────────────────────────────────────────────────────────
# BingX 公式：price = fib0 + (fib1 - fib0) * ratio
# 標籤數值即 ratio 本身（1.46→1.46, 1.73→1.73, 6.92→6.92 ...）
RAW_LEVELS = [
    (0.0,    "#555555"),   # 0軸 = fib0
    (1.0,    "#555555"),   # 1軸 = fib1
    (1.46,   "#ef5350"),   # 1.46 紅
    (1.73,   "#f59e0b"),   # 1.73 黃
    (6.92,   "#26a69a"),   # 6.92
    (12.11,  "#26a69a"),   # 12.11
    (17.3,   "#26a69a"),   # 17.3
    (22.49,  "#26a69a"),   # 22.49
    (27.68,  "#26a69a"),   # 27.68
    (32.87,  "#26a69a"),   # 32.87
    (38.06,  "#26a69a"),   # 38.06
    (43.25,  "#26a69a"),   # 43.25
    (48.44,  "#26a69a"),   # 48.44
    (53.63,  "#26a69a"),   # 53.63
    (58.82,  "#26a69a"),   # 58.82
    (64.01,  "#26a69a"),   # 64.01
    (69.2,   "#26a69a"),   # 69.2
    (74.39,  "#26a69a"),   # 74.39
    (79.58,  "#26a69a"),   # 79.58
    (84.77,  "#26a69a"),   # 84.77
    (89.96,  "#26a69a"),   # 89.96
    (95.15,  "#26a69a"),   # 95.15
    (100.34, "#26a69a"),   # 100.34
    (105.53, "#26a69a"),   # 105.53
]
FIB_LEVELS = sorted(RAW_LEVELS, key=lambda x: x[0])

def make_fib_group(klines, mas, bar_a, bar_b):
    """
    給定兩根 K 棒索引，計算 Fib 資料。
    fib0 = min(實體低點A, 實體低點B)  → min(open,close)
    fib1 = max(實體高點A, 實體高點B)  → max(open,close)
    """
    kA, kB = klines[bar_a], klines[bar_b]
    body_lo_A = min(kA[1], kA[4])
    body_hi_A = max(kA[1], kA[4])
    body_lo_B = min(kB[1], kB[4])
    body_hi_B = max(kB[1], kB[4])

    fib0 = min(body_lo_A, body_lo_B)
    fib1 = max(body_hi_A, body_hi_B)
    rang = fib1 - fib0
    if rang <= 0:
        return None

    levels = [(ratio, fib0 + rang * ratio, color)
              for ratio, color in FIB_LEVELS]

    return {
        "fib0":   fib0,
        "fib1":   fib1,
        "levels": levels,   # [(ratio, price, color), ...]
        "barA":   bar_a,
        "barB":   bar_b,
    }


def find_all_fibs(klines, mas, strict=True):
    """
    底底高（多方）Fib 組搜尋
    ─────────────────────────────────────────────────────────────
    Pattern 定義：

      barA = 第1根：收盤 > MA
      barB = 第2根（緊接在 barA 後）：收盤 > MA，且 barB.low > barA.low

    gap ≤ 3 的設計理由：
      bar5 = barA+4，bar8 = barA+7 是從 barA 計數。
      若 gap > 3，barB 落在 bar5 之後，bar5 在兩底之間失去意義。

    Fib：
      fib0 = min(barA, barB 實體低點)   → 兩底最低的實體下緣
      fib1 = max(barA, barB 實體高點)   → 兩底最高的實體上緣
      延伸位 price = fib0 + (fib1 - fib0) × ratio

    過濾條件：
      ① barA+4（第5根）K 棒範圍跨過 Fib 1.73（lo ≤ 1.73 ≤ hi）
      ② barA+7（第8根）K 棒範圍跨過 Fib 1.73
      ③ barB 之後無任何 K 棒收盤低於 barA 的低點（保護結構完整性）

    strict=True（篩選器）：遇到連續2根 close ≤ MA 就停掃描
    strict=False（彈窗）：掃描全部歷史
    ─────────────────────────────────────────────────────────────
    """
    n = len(klines)
    if n < 10:
        return []

    # ── 決定掃描起點 ──────────────────────────────────────────────
    if strict:
        scan_start = 0
        consecutive_below = 0
        for i in range(n - 1, -1, -1):
            if mas[i] is None:
                scan_start = i + 1
                break
            if klines[i][4] <= mas[i]:
                consecutive_below += 1
                if consecutive_below >= 2:
                    scan_start = i + 2
                    break
            else:
                consecutive_below = 0
    else:
        scan_start = next((i for i, m in enumerate(mas) if m is not None), 0)

    groups = []

    for i in range(scan_start + 1, n):
        barA = i - 1   # 第1根
        barB = i       # 第2根（緊接在 barA 後面）

        # 兩根都必須收盤在 MA 上方
        if mas[barA] is None or mas[barB] is None:
            continue
        if klines[barA][4] <= mas[barA] or klines[barB][4] <= mas[barB]:
            continue

        # barB 低點 > barA 低點（更高的底）
        if klines[barB][3] <= klines[barA][3]:
            continue

        grp = make_fib_group(klines, mas, barA, barB)
        if not grp:
            continue

        # ── Fib 1.73 條件 ─────────────────────────────────────────
        fib173 = grp["fib0"] + (grp["fib1"] - grp["fib0"]) * 1.73
        b5 = grp["barA"] + 4   # 第5根
        b8 = grp["barA"] + 7   # 第8根

        if b5 >= n or b8 >= n:
            continue

        # 碰到 = K 棒範圍跨過 1.73（lo ≤ 1.73 ≤ hi）
        if not (klines[b5][3] <= fib173 <= klines[b5][2]):
            continue
        if not (klines[b8][3] <= fib173 <= klines[b8][2]):
            continue

        # ── 無效化：barB 後有 K 棒收盤低於 barA 低點 ──────────────
        bar_a_low = klines[grp["barA"]][3]
        if any(klines[j][4] < bar_a_low for j in range(grp["barB"] + 1, n)):
            continue

        groups.append(grp)

    return sorted(groups, key=lambda g: g["barA"])   # 舊→新


def make_fib_group_short(klines, mas, bar_a, bar_b):
    """
    空單 Fib 組：
    fib0 = max(實體高點A, 實體高點B) → 兩頂最高的實體上緣（壓力）
    fib1 = min(實體低點A, 實體低點B) → 兩頂最低的實體下緣
    延伸位 price = fib0 + (fib1 - fib0) × ratio  → fib0 > fib1，ratio > 1 時往下延伸
    """
    kA, kB = klines[bar_a], klines[bar_b]
    body_lo_A = min(kA[1], kA[4])
    body_hi_A = max(kA[1], kA[4])
    body_lo_B = min(kB[1], kB[4])
    body_hi_B = max(kB[1], kB[4])

    fib0 = max(body_hi_A, body_hi_B)   # 頂部（較高）
    fib1 = min(body_lo_A, body_lo_B)   # 底部（較低）
    rang = fib0 - fib1
    if rang <= 0:
        return None

    # 空單延伸往下：price = fib0 + (fib1 - fib0) * ratio = fib0 - rang * ratio
    levels = [(ratio, fib0 + (fib1 - fib0) * ratio, color)
              for ratio, color in FIB_LEVELS]

    return {
        "fib0":   fib0,
        "fib1":   fib1,
        "levels": levels,
        "barA":   bar_a,
        "barB":   bar_b,
        "side":   "short",
    }


def find_all_fibs_short(klines, mas, strict=True):
    """
    頂頂低（空方）Fib 組搜尋 — 多方邏輯完全相反
    ─────────────────────────────────────────────
    barA = 第1根：收盤 < MA
    barB = 第2根（緊接）：收盤 < MA，且 barB.high < barA.high（更低的頂）

    Fib 1.73 條件：
      bar5 = barA+4、bar8 = barA+7 範圍跨過空方 Fib 1.73

    無效化：barB 後有任何 K 棒收盤高於 barA 的高點
    ─────────────────────────────────────────────
    """
    n = len(klines)
    if n < 10:
        return []

    if strict:
        scan_start = 0
        consecutive_above = 0
        for i in range(n - 1, -1, -1):
            if mas[i] is None:
                scan_start = i + 1
                break
            if klines[i][4] >= mas[i]:
                consecutive_above += 1
                if consecutive_above >= 2:
                    scan_start = i + 2
                    break
            else:
                consecutive_above = 0
    else:
        scan_start = next((i for i, m in enumerate(mas) if m is not None), 0)

    groups = []

    for i in range(scan_start + 1, n):
        barA = i - 1
        barB = i

        if mas[barA] is None or mas[barB] is None:
            continue
        # 兩根都必須收盤在 MA 下方
        if klines[barA][4] >= mas[barA] or klines[barB][4] >= mas[barB]:
            continue
        # barB 高點 < barA 高點（更低的頂）
        if klines[barB][2] >= klines[barA][2]:
            continue

        grp = make_fib_group_short(klines, mas, barA, barB)
        if not grp:
            continue

        # 空方 Fib 1.73（往下延伸）
        fib173 = grp["fib0"] + (grp["fib1"] - grp["fib0"]) * 1.73
        b5 = grp["barA"] + 4
        b8 = grp["barA"] + 7

        if b5 >= n or b8 >= n:
            continue

        if not (klines[b5][3] <= fib173 <= klines[b5][2]):
            continue
        if not (klines[b8][3] <= fib173 <= klines[b8][2]):
            continue

        # 無效化：barB 後有 K 棒收盤高於 barA 的高點
        bar_a_high = klines[grp["barA"]][2]
        if any(klines[j][4] > bar_a_high for j in range(grp["barB"] + 1, n)):
            continue

        groups.append(grp)

    return sorted(groups, key=lambda g: g["barA"])


def get_funding_rates():
    """
    批次取得所有永續合約的資金費率（%）
    回傳 dict: { symbol: funding_rate_pct }
    """
    try:
        data = bget("/openApi/swap/v2/quote/premiumIndex")
        result = {}
        for item in data.get("data", []):
            sym = item.get("symbol", "")
            if sym.endswith("-USDT"):
                rate = item.get("lastFundingRate", item.get("fundingRate", 0))
                result[sym] = round(float(rate) * 100, 4)   # 轉為 %
        return result
    except:
        return {}


def get_open_interests():
    """
    批次取得所有永續合約的未平倉量（USDT 計）
    回傳 dict: { symbol: oi_usdt }
    """
    try:
        data = bget("/openApi/swap/v2/quote/openInterests")
        result = {}
        for item in data.get("data", []):
            sym = item.get("symbol", "")
            if sym.endswith("-USDT"):
                result[sym] = float(item.get("openInterest", 0))
        return result
    except:
        return {}


def get_daily_volume_change(symbols):
    """
    批次取得所有標的今日 vs 昨日成交量變化%
    用日線 K 棒，取最近2根：倒數第2根=昨日，最後1根=今日
    回傳 dict: { symbol: vol_change_pct }
    """
    result = {}
    for sym in symbols:
        try:
            klines = get_klines(sym, "1d", 3)   # 抓3根確保有2根完整日線
            if len(klines) < 2:
                result[sym] = 0.0
                continue
            # 最後一根可能是當日未完成，倒數第2根才是昨日完整
            # 取倒數第2和倒數第3（如有）或直接用倒數2根
            vol_today     = klines[-1][5]
            vol_yesterday = klines[-2][5]
            if vol_yesterday > 0:
                result[sym] = (vol_today - vol_yesterday) / vol_yesterday * 100
            else:
                result[sym] = 0.0
        except:
            result[sym] = 0.0
    return result




@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scan")
def scan():
    try:
        iv        = parse_iv(request.args.get("interval", "1h"))
        mp        = int(request.args.get("maPeriod", 20))
        mt        = request.args.get("maType", "SMA")
        limit     = min(int(request.args.get("limit", 40)), 500)
        ftype     = request.args.get("filterType", "volume")
        fval      = float(request.args.get("filterVal", 0))
        use_fib   = request.args.get("useFib", "false").lower() == "true"

        tickers = get_all_tickers()
        symbols = list(tickers.keys())

        # ── 成交量變化% 篩選（需要先取日線）────────────────────────────────────────
        vol_change_map = {}
        if ftype == "volchange":
            candidates = sorted(symbols, key=lambda s: tickers[s]["quoteVol"], reverse=True)
            candidates = candidates[:min(limit * 3, 200)]
            vol_change_map = get_daily_volume_change(candidates)
            if fval >= 0:
                filtered = [s for s in candidates if vol_change_map.get(s, 0) >= fval]
            else:
                filtered = [s for s in candidates if vol_change_map.get(s, 0) <= fval]
            filtered.sort(key=lambda s: abs(vol_change_map.get(s, 0)), reverse=True)
            symbols = filtered[:limit]
        elif ftype == "volspike":
            filtered = [s for s in symbols if abs(tickers[s]["changePct"]) >= fval]
            filtered.sort(key=lambda s: tickers[s]["quoteVol"], reverse=True)
            symbols = filtered[:limit]
        elif ftype == "chgrank":
            # 漲跌幅排行：取漲幅前半 + 跌幅前半
            # 多頭候選來自漲榜，空頭候選來自跌榜
            half = max(limit // 2, 1)
            top_up = sorted(symbols, key=lambda s: tickers[s]["changePct"], reverse=True)[:half]
            top_dn = sorted(symbols, key=lambda s: tickers[s]["changePct"])[:half]
            seen = set()
            merged = []
            for s in top_up + top_dn:
                if s not in seen:
                    seen.add(s)
                    merged.append(s)
            symbols = merged[:limit]
        elif ftype == "random":
            random.shuffle(symbols)
            symbols = symbols[:limit]
        else:
            symbols.sort(key=lambda s: tickers[s]["quoteVol"], reverse=True)
            symbols = symbols[:limit]

        # ── 批次抓取動能指標（一次 API call 取全市場）────────────────────
        funding_map = get_funding_rates()
        oi_map      = get_open_interests()

        kline_limit = min(max(mp * 4, 150), 300)
        results, errors = [], []

        def process_symbol(sym):
            try:
                klines = get_klines(sym, iv, kline_limit)
                if len(klines) < mp + 5: return None

                closes     = [k[4] for k in klines]
                last_close = closes[-1]
                mas        = ma_series(closes, mp, mt)
                ma_val     = mas[-1]
                if ma_val is None: return None

                is_above = last_close > ma_val

                fib_groups_long  = []
                fib_groups_short = []
                if use_fib:
                    # 多空都搜，不限方向
                    fib_groups_long  = find_all_fibs(klines, mas)
                    fib_groups_short = find_all_fibs_short(klines, mas)
                    if not fib_groups_long and not fib_groups_short: return None
                    # 標記方向、合併、按時間排序、取最新5組
                    for g in fib_groups_long:  g["side"] = "long"
                    for g in fib_groups_short: g["side"] = "short"
                fib_groups = sorted(
                    fib_groups_long + fib_groups_short,
                    key=lambda g: g["barA"]
                )[-5:]

                tk         = tickers.get(sym, {})
                change_pct = tk.get("changePct", 0)
                vol_usdt   = tk.get("quoteVol",  0)
                vol_change = vol_change_map.get(sym, None)

                vols = [k[5] for k in klines]
                if len(vols) >= 25 and sum(vols[-25:-1]) > 0:
                    rel_vol = vols[-1] / (sum(vols[-25:-1]) / 24)
                else:
                    rel_vol = None

                funding = funding_map.get(sym, None)
                oi_usdt = oi_map.get(sym, None)

                n      = min(60, len(klines))
                recent = klines[-n:]
                offset = len(klines) - n
                buf    = closes[-(n + mp):]
                ma50   = ma_series(buf, mp, mt)[-n:]

                fib_out = []
                barA_time = None
                if fib_groups:
                    # 統計用：最新那組的 timestamp
                    barA_time = klines[fib_groups[-1]["barA"]][0]
                    for grp in fib_groups:
                        barA_r = grp["barA"] - offset
                        barB_r = grp["barB"] - offset
                        if barB_r < 0:
                            continue
                        fib_out.append({
                            "fib0":   grp["fib0"],
                            "fib1":   grp["fib1"],
                            "levels": [list(lv) for lv in grp["levels"]],
                            "barA":   max(barA_r, -offset),
                            "barB":   barB_r,
                            "side":   grp.get("side", "long"),
                        })

                return {
                    "symbol":      sym,
                    "displayName": sym.replace("-USDT", "") + "/USDT.P",
                    "lastClose":   last_close,
                    "maVal":       ma_val,
                    "isAbove":     is_above,
                    "changePct":   change_pct,
                    "volumeUsdt":  vol_usdt,
                    "opens":   [k[1] for k in recent],
                    "highs":   [k[2] for k in recent],
                    "lows":    [k[3] for k in recent],
                    "closes":  [k[4] for k in recent],
                    "times":   [k[0] for k in recent],
                    "maSeries": ma50,
                    "fibs":        fib_out,
                    "fibCount":    len(fib_out),
                    "fibSide":     fib_groups[-1].get("side", "long") if fib_groups else "long",
                    "barATime":    barA_time,
                    "volChangePct": vol_change,
                    "relVol":      round(rel_vol, 2) if rel_vol is not None else None,
                    "funding":     funding,
                    "oiUsdt":      oi_usdt,
                }
            except Exception as e:
                return {"__error__": True, "symbol": sym, "error": str(e)}

        # 並行抓取，最多 12 個執行緒同時跑
        with ThreadPoolExecutor(max_workers=12) as ex:
            futures = {ex.submit(process_symbol, sym): sym for sym in symbols}
            for future in as_completed(futures):
                r = future.result()
                if r is None: continue
                if r.get("__error__"):
                    errors.append({"symbol": r["symbol"], "error": r["error"]})
                else:
                    results.append(r)

        results.sort(key=lambda x: x["volumeUsdt"], reverse=True)

        return jsonify({
            "results":  results, "errors": errors,
            "total":    len(results),
            "above":    sum(1 for r in results if r["isAbove"]),
            "below":    sum(1 for r in results if not r["isAbove"]),
            "interval": iv,
        })

    except Exception as e:
        tb = traceback.format_exc()
        print("=== /api/scan ERROR ===")
        print(tb)
        return jsonify({"error": str(e), "traceback": tb}), 500


@app.route("/api/kline_detail")
def kline_detail():
    sym      = request.args.get("symbol", "")
    iv       = parse_iv(request.args.get("interval", "1h"))
    mp       = int(request.args.get("maPeriod", 20))
    mt       = request.args.get("maType", "SMA")
    try:
        klines = get_klines(sym, iv, 300)
        if not klines:
            return jsonify({"error": "無法取得K線"}), 404
        closes = [k[4] for k in klines]
        mas    = ma_series(closes, mp, mt)

        # 多空都搜（不限方向），合併後取最新 5 組
        fibs_long  = find_all_fibs(klines, mas, strict=False)
        fibs_short = find_all_fibs_short(klines, mas, strict=False)
        for g in fibs_long:  g["side"] = "long"
        for g in fibs_short: g["side"] = "short"

        # 合併多空、依 barA 排序，取最新 5 組（不過濾價格距離）
        all_fibs = sorted(fibs_long + fibs_short, key=lambda g: g["barA"])
        fibs = all_fibs[-5:]   # 最新 5 組

        latest_side = fibs[-1]["side"] if fibs else "long"

        fibs_out = []
        for grp in fibs:
            fibs_out.append({
                "fib0":   grp["fib0"],
                "fib1":   grp["fib1"],
                "levels": [list(lv) for lv in grp["levels"]],
                "barA":   grp["barA"],
                "barB":   grp["barB"],
                "bar5":   grp["barA"] + 4,
                "bar8":   grp["barA"] + 7,
                "side":   grp["side"],
            })
        return jsonify({
            "symbol":   sym,
            "fibSide":  latest_side,
            "opens":    [k[1] for k in klines],
            "highs":    [k[2] for k in klines],
            "lows":     [k[3] for k in klines],
            "closes":   closes,
            "times":    [k[0] for k in klines],
            "volumes":  [k[5] for k in klines],
            "maSeries": mas,
            "fibs":     fibs_out,
        })
    except Exception as e:
        tb = traceback.format_exc()
        print("=== /api/kline_detail ERROR ===")
        print(tb)
        return jsonify({"error": str(e), "traceback": tb}), 500


@app.route("/api/debug_fib")
def debug_fib():
    """
    除錯用：逐步說明某個標的為何通過或未通過底底高篩選。
    用法：/api/debug_fib?symbol=SAHARA-USDT&interval=1h&maPeriod=20&maType=SMA
    """
    sym = request.args.get("symbol", "")
    iv  = parse_iv(request.args.get("interval", "1h"))
    mp  = int(request.args.get("maPeriod", 20))
    mt  = request.args.get("maType", "SMA")

    try:
        klines = get_klines(sym, iv, 300)
        if not klines:
            return jsonify({"error": "無法取得K線"}), 404

        closes = [k[4] for k in klines]
        mas    = ma_series(closes, mp, mt)
        n      = len(klines)

        # ── strict scan_start ──
        scan_start = 0
        consecutive_below = 0
        for i in range(n - 1, -1, -1):
            if mas[i] is None:
                scan_start = i + 1; break
            if klines[i][4] <= mas[i]:
                consecutive_below += 1
                if consecutive_below >= 2:
                    scan_start = i + 2; break
            else:
                consecutive_below = 0

        log = []
        log.append(f"K線數={n}  scan_start={scan_start}  最新close={closes[-1]:.6f}  MA={mas[-1]:.6f if mas[-1] else 'N/A'}")

        # 掃描最後50根內所有候選配對
        candidates = []
        for i in range(max(scan_start + 1, n - 50), n):
            barA, barB = i - 1, i
            reasons = []

            if mas[barA] is None or mas[barB] is None:
                reasons.append("MA為None")
            else:
                if klines[barA][4] <= mas[barA]:
                    reasons.append(f"barA收盤{klines[barA][4]:.4f}≤MA{mas[barA]:.4f}")
                if klines[barB][4] <= mas[barB]:
                    reasons.append(f"barB收盤{klines[barB][4]:.4f}≤MA{mas[barB]:.4f}")
                if klines[barB][3] <= klines[barA][3]:
                    reasons.append(f"barB低點{klines[barB][3]:.4f}≤barA低點{klines[barA][3]:.4f}")

            if reasons:
                candidates.append({"barA": barA, "barB": barB, "pass": False, "fail": reasons})
                continue

            grp = make_fib_group(klines, mas, barA, barB)
            if not grp:
                candidates.append({"barA": barA, "barB": barB, "pass": False, "fail": ["make_fib_group返回None"]})
                continue

            fib173 = grp["fib0"] + (grp["fib1"] - grp["fib0"]) * 1.73
            b5, b8 = grp["barA"] + 4, grp["barA"] + 7
            fib_reasons = []

            if b5 >= n:
                fib_reasons.append(f"b5={b5}超出範圍")
            else:
                t5 = klines[b5][3] <= fib173 <= klines[b5][2]
                if not t5:
                    fib_reasons.append(f"b5[{b5}]未碰1.73(lo={klines[b5][3]:.4f} 1.73={fib173:.4f} hi={klines[b5][2]:.4f})")

            if b8 >= n:
                fib_reasons.append(f"b8={b8}超出範圍")
            else:
                t8 = klines[b8][3] <= fib173 <= klines[b8][2]
                if not t8:
                    fib_reasons.append(f"b8[{b8}]未碰1.73(lo={klines[b8][3]:.4f} 1.73={fib173:.4f} hi={klines[b8][2]:.4f})")

            bar_a_low = klines[barA][3]
            breached = [j for j in range(barB + 1, n) if klines[j][4] < bar_a_low]
            if breached:
                fib_reasons.append(f"無效化：{len(breached)}根K棒收盤低於barA低點{bar_a_low:.4f} (first={breached[0]})")

            entry = {
                "barA": barA, "barB": barB,
                "barA_close": klines[barA][4], "barB_close": klines[barB][4],
                "barA_low": klines[barA][3],   "barB_low": klines[barB][3],
                "fib0": grp["fib0"], "fib1": grp["fib1"], "fib173": fib173,
                "b5": b5, "b8": b8,
            }
            if fib_reasons:
                entry["pass"] = False
                entry["fail"] = fib_reasons
            else:
                entry["pass"] = True

            candidates.append(entry)

        passed = [c for c in candidates if c.get("pass")]
        failed = [c for c in candidates if not c.get("pass")]

        return jsonify({
            "symbol": sym, "interval": iv, "maPeriod": mp, "maType": mt,
            "summary": log[0],
            "passed_count": len(passed),
            "passed": passed,
            "failed_last10": failed[-10:],
        })

    except Exception as e:
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500


@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = load_config()
    return jsonify({
        "api_key":   cfg.get("api_key",""),
        "has_secret": bool(cfg.get("api_secret","")),
        "demo_mode": cfg.get("demo_mode", True),
    })

@app.route("/api/config", methods=["POST"])
def set_config():
    data = request.json or {}
    cfg  = load_config()
    if "api_key"    in data: cfg["api_key"]    = data["api_key"]
    if "api_secret" in data: cfg["api_secret"] = data["api_secret"]
    if "demo_mode"  in data: cfg["demo_mode"]  = bool(data["demo_mode"])
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/place_order", methods=["POST"])
def place_order():
    """
    下單（多方/空方開倉，含止損止盈）
    body: {
      symbol, side('long'|'short'), order_type, entry, sl,
      tp1, tp1_pct, tp2, tp2_pct, quantity
    }
    """
    try:
        d           = request.json or {}
        symbol      = d.get("symbol","")
        order_type  = d.get("order_type","LIMIT")    # LIMIT 或 MARKET
        side_str    = d.get("side", "long")           # 'long' 或 'short'
        margin_mode = d.get("margin_mode", "isolated") # 'isolated' 或 'cross'
        entry       = float(d.get("entry") or 0)
        sl          = float(d.get("sl", 0))
        tp1         = float(d.get("tp1", 0)) if d.get("tp1") else None
        tp2         = float(d.get("tp2", 0)) if d.get("tp2") else None
        qty         = float(d.get("quantity", 0))

        is_short      = (side_str == "short")
        order_side    = "SELL" if is_short else "BUY"
        position_side = "SHORT" if is_short else "LONG"
        close_side    = "BUY"  if is_short else "SELL"

        if not symbol or not sl or qty <= 0:
            return jsonify({"error": "缺少必要參數（symbol / sl / quantity）"}), 400
        if order_type == "LIMIT" and not entry:
            return jsonify({"error": "限價單需填入開倉點位"}), 400

        results = []

        # ── 設定保證金模式（逐倉/全倉）────────────────────────────────
        margin_api = "ISOLATED" if margin_mode == "isolated" else "CROSSED"
        try:
            mr = bpost_auth("/openApi/swap/v2/trade/marginType",
                            {"symbol": symbol, "marginType": margin_api})
            results.append({"step": f"保證金模式({margin_api})", "response": mr})
        except Exception as me:
            results.append({"step": f"保證金模式({margin_api})", "response": {"warning": str(me)}})

        # ── 計算各 TP 數量 ────────────────────────────────────────────
        tp1_pct = float(d.get("tp1_pct") or 0)
        tp2_pct = float(d.get("tp2_pct") or 0)
        tp1_qty = round(qty * tp1_pct / 100, 4) if tp1 and tp1_pct else 0
        tp2_qty = round(qty * tp2_pct / 100, 4) if tp2 and tp2_pct else 0

        # ── 主單（只含 SL，TP 全部改為輪詢送出）─────────────────────
        order_body = {
            "symbol":       symbol,
            "side":         order_side,
            "positionSide": position_side,
            "type":         order_type,
            "quantity":     qty,
            "stopLoss":     json.dumps({"type":"STOP_MARKET","stopPrice":sl,"workingType":"MARK_PRICE"}),
        }
        if order_type == "LIMIT":
            order_body["price"] = entry

        res = bpost_auth("/openApi/swap/v2/trade/order", order_body)
        results.append({"step": "主單(含SL)", "response": res})

        if res.get("code", -1) != 0:
            return jsonify({"ok": False, "results": results})

        # ── 背景輪詢：主單成交後同時送出 TP1 + TP2 ───────────────────
        tp_orders = []
        if tp1 and tp1_qty > 0:
            tp_orders.append({"label": f"TP1@{tp1}", "body": {
                "symbol": symbol, "side": close_side, "positionSide": position_side,
                "type": "TAKE_PROFIT_MARKET", "stopPrice": tp1,
                "quantity": tp1_qty, "workingType": "MARK_PRICE",
            }})
        if tp2 and tp2_qty > 0:
            tp_orders.append({"label": f"TP2@{tp2}", "body": {
                "symbol": symbol, "side": close_side, "positionSide": position_side,
                "type": "TAKE_PROFIT_MARKET", "stopPrice": tp2,
                "quantity": tp2_qty, "workingType": "MARK_PRICE",
            }})

        if tp_orders:
            order_id   = str(res["data"]["order"].get("orderID") or res["data"]["order"].get("orderId",""))
            watcher_id = f"{symbol}_{order_id}"
            tp2_watchers[watcher_id] = {
                "symbol": symbol, "orderId": order_id,
                "tp1": tp1, "tp2": tp2, "qty": qty,
                "status": "watching", "msg": ""
            }

            def poll_and_send_tps(oid, tp_list, sym, wid):
                intervals = [5]*120 + [30]*2760
                for interval in intervals:
                    if tp2_watchers.get(wid, {}).get("status") == "cancelled":
                        print(f"[TP] {sym} 已取消監控")
                        return
                    time.sleep(interval)
                    try:
                        q = bget_auth("/openApi/swap/v2/trade/order",
                                      {"symbol": sym, "orderId": oid})
                        status = q.get("data", {}).get("order", {}).get("status", "")
                        if status == "FILLED":
                            # 平行同時送出所有 TP 單
                            def send_tp(tp):
                                r2 = bpost_auth("/openApi/swap/v2/trade/order", tp["body"])
                                ok = r2.get("code", -1) == 0
                                print(f"[TP] {sym} {tp['label']} {'已送出' if ok else '失敗:'+r2.get('msg','')}")
                                return f"{tp['label']} {'✓' if ok else '✗'+r2.get('msg','')}"
                            with ThreadPoolExecutor(max_workers=len(tp_list)) as tex:
                                sent = list(tex.map(send_tp, tp_list))
                            all_ok = all("✓" in s for s in sent)
                            tp2_watchers[wid]["status"] = "sent_ok" if all_ok else "sent_fail"
                            tp2_watchers[wid]["msg"]    = " | ".join(sent)
                            return
                        if status in ("CANCELLED", "FAILED", "EXPIRED"):
                            tp2_watchers[wid]["status"] = "aborted"
                            tp2_watchers[wid]["msg"]    = f"主單 {status}"
                            return
                    except Exception as ex:
                        print(f"[TP] 輪詢錯誤：{ex}")
                tp2_watchers[wid]["status"] = "timeout"
                tp2_watchers[wid]["msg"]    = "監控超時（24h）"

            t = threading.Thread(target=poll_and_send_tps,
                                 args=(order_id, tp_orders, symbol, watcher_id), daemon=True)
            t.start()
            tp_desc = " + ".join(f"{tp['label']}({tp['body']['quantity']}張)" for tp in tp_orders)
            results.append({"step": "TP 背景監控", "response": {
                "msg": f"主單成交後自動送 {tp_desc}",
                "watcher_id": watcher_id
            }})

        return jsonify({"ok": True, "results": results})

    except Exception as e:
        tb = traceback.format_exc()
        print("=== /api/place_order ERROR ===")
        print(tb)
        return jsonify({"error": str(e)}), 500


@app.route("/api/backtest_fib", methods=["GET","POST"])
def backtest_fib():
    """
    回測：bar8（barA+7）碰到 Fib 1.73 進場，止損放 Fib 0
    POST body: { symbols:[...], interval, maPeriod, maType, lookForward }
    symbols 由前端傳入（目前 list 上的幣種）
    """
    if request.method == "POST":
        d            = request.json or {}
        iv           = parse_iv(d.get("interval", "1h"))
        mp           = int(d.get("maPeriod", 55))
        mt           = d.get("maType", "SMA")
        look_fwd     = int(d.get("lookForward", 100))
        symbols_data = d.get("symbolsData", [])   # [{symbol, fibSide}, ...]
        # 相容舊格式
        if not symbols_data and d.get("symbols"):
            symbols_data = [{"symbol": s, "fibSide": "long"} for s in d["symbols"]]
        # 決定顯示用的方向（多方向時標示 mixed）
        sides = list({s.get("fibSide","long") for s in symbols_data})
        fib_side = sides[0] if len(sides)==1 else "mixed"
    else:
        iv       = parse_iv(request.args.get("interval", "1h"))
        mp       = int(request.args.get("maPeriod", 55))
        mt       = request.args.get("maType", "SMA")
        look_fwd = int(request.args.get("lookForward", 100))
        limit    = min(int(request.args.get("limit", 50)), 100)
        tickers  = get_all_tickers()
        syms     = sorted(tickers.keys(), key=lambda s: tickers[s]["quoteVol"], reverse=True)[:limit]
        fib_side = request.args.get("fibSide", "long")
        symbols_data = [{"symbol": s, "fibSide": fib_side} for s in syms]

    try:
        if not symbols_data:
            return jsonify({"error": "沒有傳入幣種"}), 400
        funding_map = get_funding_rates()

        def process_sym(sym_info):
            """
            與彈窗 modal 邏輯完全一致：
              進場 = 第9根（barA+8）開盤市價
              多頭 SL = min(lows[barA, barB, bar8])
              空頭 SL = max(highs[barA, barB, bar8])
              統計：hitTP1(6.92) / hitTP2(12.11) / hitBeyond(收盤超 12.11) / hitSL

            注意：與 kline_detail 使用同樣的 300 根 K 線，
            避免條件③（barA 後無收盤低於 barA.low）在大量歷史資料下
            把所有組都排除。
            """
            sym = sym_info["symbol"]
            try:
                klines = get_klines(sym, iv, 300)   # 與 kline_detail 一致
                if len(klines) < mp + 20:
                    return []
                closes  = [k[4] for k in klines]
                opens   = [k[1] for k in klines]
                highs   = [k[2] for k in klines]
                lows    = [k[3] for k in klines]
                mas     = ma_series(closes, mp, mt)
                n       = len(klines)

                # 多空都搜，合併取最新 5 組（與 kline_detail 一致）
                fibs_long  = find_all_fibs(klines, mas, strict=False)
                fibs_short = find_all_fibs_short(klines, mas, strict=False)
                for g in fibs_long:  g["side"] = "long"
                for g in fibs_short: g["side"] = "short"
                all_grps = sorted(fibs_long + fibs_short, key=lambda g: g["barA"])[-5:]

                if not all_grps:
                    return []

                recs = []
                for grp in all_grps:
                    b1   = grp["barA"]
                    b2   = grp["barB"]
                    b8   = grp["barA"] + 7
                    b9   = grp["barA"] + 8
                    side = grp["side"]
                    if b9 >= n:
                        continue

                    fib692  = next((p for r,p,_ in grp["levels"] if abs(r-6.92)  < 0.01), None)
                    fib1211 = next((p for r,p,_ in grp["levels"] if abs(r-12.11) < 0.01), None)
                    fib1730 = next((p for r,p,_ in grp["levels"] if abs(r-17.3)  < 0.01), None)
                    if fib692 is None:
                        continue

                    entry = opens[b9]

                    if side == "short":
                        sl_p = max(
                            highs[b1] if 0<=b1<n else -1e9,
                            highs[b2] if 0<=b2<n else -1e9,
                            highs[b8] if 0<=b8<n else -1e9,
                        )
                    else:
                        sl_p = min(
                            lows[b1] if 0<=b1<n else 1e9,
                            lows[b2] if 0<=b2<n else 1e9,
                            lows[b8] if 0<=b8<n else 1e9,
                        )

                    hit_sl = hit_tp1 = hit_tp2 = hit_tp3 = False
                    for i in range(b9, n):   # 含進場棒（b9）本身
                        if hit_sl: break   # 已停損，不再掃
                        if side == "short":
                            if not hit_tp1:
                                if lows[i] <= fib692:   # 先到 6.92 → TP1（同根優先）
                                    hit_tp1 = True
                                elif highs[i] >= sl_p:  # 未到 6.92 先碰 SL
                                    hit_sl = True
                            if hit_tp1:
                                if not hit_tp2 and fib1211 and lows[i] <= fib1211: hit_tp2 = True
                                if not hit_tp3 and fib1730 and lows[i] <= fib1730: hit_tp3 = True
                        else:
                            if not hit_tp1:
                                if highs[i] >= fib692:  # 先到 6.92 → TP1（同根優先）
                                    hit_tp1 = True
                                elif lows[i] <= sl_p:   # 未到 6.92 先碰 SL
                                    hit_sl = True
                            if hit_tp1:
                                if not hit_tp2 and fib1211 and highs[i] >= fib1211: hit_tp2 = True
                                if not hit_tp3 and fib1730 and highs[i] >= fib1730: hit_tp3 = True

                    risk_amt = abs(entry - sl_p)
                    rr692  = round(abs(fib692  - entry) / risk_amt, 2) if risk_amt > 0 else None
                    rr1211 = round(abs(fib1211 - entry) / risk_amt, 2) if (risk_amt > 0 and fib1211) else None
                    rr1730 = round(abs(fib1730 - entry) / risk_amt, 2) if (risk_amt > 0 and fib1730) else None
                    recs.append({
                        "symbol":     sym,
                        "side":       side,
                        "entry":      round(entry, 6),
                        "slPrice":    round(sl_p, 6),
                        "rr692":      rr692,
                        "rr1211":     rr1211,
                        "rr1730":     rr1730,
                        "hitTP1":     hit_tp1,
                        "hitTP2":     hit_tp2,
                        "hitTP3":     hit_tp3,
                        "hitSL":      hit_sl,
                        "ongoing":    b9 >= n - 2,
                    })
                return recs
            except Exception as _e:
                print(f"[backtest] {sym} error: {_e}")
                return []

        records = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = [ex.submit(process_sym, s) for s in symbols_data]
            for f in as_completed(futures):
                records.extend(f.result())

        if not records:
            return jsonify({"error": "沒有找到符合的 Fib 組", "totalGroups": 0})

        total      = len(records)
        ongoing    = sum(1 for r in records if r.get("ongoing"))
        valid      = total - ongoing
        cnt_tp1    = sum(1 for r in records if r["hitTP1"])
        cnt_tp2    = sum(1 for r in records if r["hitTP2"])
        cnt_tp3    = sum(1 for r in records if r["hitTP3"])
        cnt_sl     = sum(1 for r in records if r["hitSL"])
        rr692_list  = [r["rr692"]  for r in records if r.get("rr692")  is not None]
        rr1211_list = [r["rr1211"] for r in records if r.get("rr1211") is not None]
        rr1730_list = [r["rr1730"] for r in records if r.get("rr1730") is not None]
        avg_rr692   = round(sum(rr692_list)  / len(rr692_list),  2) if rr692_list  else None
        avg_rr1211  = round(sum(rr1211_list) / len(rr1211_list), 2) if rr1211_list else None
        avg_rr1730  = round(sum(rr1730_list) / len(rr1730_list), 2) if rr1730_list else None

        # 明細：依幣種分組，每幣種顯示所有組
        sym_groups = {}
        for r in records:
            sym_groups.setdefault(r["symbol"], []).append(r)
        detail_list = [
            {"symbol": sym, "groups": grps}
            for sym, grps in sorted(sym_groups.items())
        ]

        return jsonify({
            "totalGroups":  total,
            "validGroups":  valid,
            "ongoing":      ongoing,
            "cntTP1":       cnt_tp1,
            "cntTP2":       cnt_tp2,
            "cntTP3":       cnt_tp3,
            "cntSL":        cnt_sl,
            "avgRR692":     avg_rr692,
            "avgRR1211":    avg_rr1211,
            "avgRR1730":    avg_rr1730,
            "symbolCount":  len(symbols_data),
            "detail":       detail_list,
        })

    except Exception as e:
        tb = traceback.format_exc()
        print("=== /api/backtest_fib ERROR ===")
        print(tb)
        return jsonify({"error": str(e), "traceback": tb}), 500


@app.route("/api/tp2_watchers", methods=["GET"])
def get_tp2_watchers():
    """查詢所有 TP2 監控狀態"""
    return jsonify(list(tp2_watchers.values()))

@app.route("/api/tp2_watchers/<wid>/cancel", methods=["POST"])
def cancel_tp2_watcher(wid):
    """取消指定的 TP2 監控"""
    if wid in tp2_watchers:
        tp2_watchers[wid]["status"] = "cancelled"
        return jsonify({"ok": True})
    return jsonify({"error": "找不到此監控"}), 404

_last_ping = time.time()

@app.route("/api/ping", methods=["POST"])
def ping():
    global _last_ping
    _last_ping = time.time()
    return jsonify({"ok": True})

def _watchdog(timeout=90):
    """本機模式：超過 timeout 秒沒收到 ping 就關閉 server"""
    time.sleep(timeout + 5)   # 等前端先連上再開始監控
    while True:
        time.sleep(2)
        if time.time() - _last_ping > timeout:
            print("\n[watchdog] 瀏覽器已關閉，server 自動停止。")
            os._exit(0)

@app.route("/api/account_balance", methods=["GET"])
def account_balance():
    """查詢帳戶 USDT 餘額"""
    try:
        data = bget_auth("/openApi/swap/v2/user/balance", {"currency": "USDT"})
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 啟動 ──────────────────────────────────────────────────────────────────────
def open_browser():
    time.sleep(1.3)
    webbrowser.open("http://127.0.0.1:5678")

if __name__ == "__main__":
    is_dev = os.environ.get("ENV", "dev") != "production"
    host   = "127.0.0.1" if is_dev else "0.0.0.0"
    port   = int(os.environ.get("PORT", 5678))
    print("=" * 50)
    print("  BingX MA + Fib 篩選器")
    print(f"  http://{host}:{port}")
    print("  Ctrl+C 停止")
    print("=" * 50)
    if is_dev:
        threading.Thread(target=open_browser, daemon=True).start()
        threading.Thread(target=_watchdog, daemon=True).start()
    app.run(host=host, port=port, debug=False)
