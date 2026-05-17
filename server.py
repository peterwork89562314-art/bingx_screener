"""
BingX MA + Fibonacci 篩選器（永續合約版）
python server.py
"""
import threading, webbrowser, time, random, traceback, hmac, hashlib, json, os
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
                    fib_groups_long  = find_all_fibs(klines, mas)       if is_above else []
                    fib_groups_short = find_all_fibs_short(klines, mas) if not is_above else []
                    if not fib_groups_long and not fib_groups_short: return None
                fib_groups = fib_groups_long or fib_groups_short

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
                if fib_groups:
                    grp    = fib_groups[-1]
                    barA_r = grp["barA"] - offset
                    barB_r = grp["barB"] - offset
                    if barB_r >= 0:
                        fib_out.append({
                            "fib0":   grp["fib0"],
                            "fib1":   grp["fib1"],
                            "levels": [list(lv) for lv in grp["levels"]],
                            "barA":   barA_r,
                            "barB":   barB_r,
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
                    "fibSide":     "short" if fib_groups_short and not fib_groups_long else "long",
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
    fib_side = request.args.get("fibSide", "long")   # 'long' 或 'short'
    try:
        klines = get_klines(sym, iv, 300)
        if not klines:
            return jsonify({"error": "無法取得K線"}), 404
        closes = [k[4] for k in klines]
        mas    = ma_series(closes, mp, mt)

        # 依方向選擇 fib 搜尋函式（全歷史）
        if fib_side == "short":
            fibs = find_all_fibs_short(klines, mas, strict=False)
        else:
            fibs = find_all_fibs(klines, mas, strict=False)

        # 過濾：只保留 fib0 在當前價格 ±50% 以內的組
        cur_price = closes[-1]
        fibs = [g for g in fibs
                if cur_price > 0 and abs(g["fib0"] - cur_price) / cur_price <= 0.5]

        # 回傳最新 1 組
        fibs_out = []
        for grp in fibs[-1:]:
            fibs_out.append({
                "fib0":   grp["fib0"],
                "fib1":   grp["fib1"],
                "levels": [list(lv) for lv in grp["levels"]],
                "barA":   grp["barA"],
                "barB":   grp["barB"],
                "bar5":   grp["barA"] + 4,
                "bar8":   grp["barA"] + 7,
            })
        return jsonify({
            "symbol":   sym,
            "fibSide":  fib_side,
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
            sym      = sym_info["symbol"]
            fib_side = sym_info.get("fibSide", "long")
            try:
                klines = get_klines(sym, iv, 500)
                if len(klines) < mp + 20:
                    return []
                closes = [k[4] for k in klines]
                mas    = ma_series(closes, mp, mt)

                if fib_side == "short":
                    groups = find_all_fibs_short(klines, mas, strict=False)
                else:
                    groups = find_all_fibs(klines, mas, strict=False)

                recs = []
                for grp in groups:
                    bar8 = grp["barA"] + 7   # 確認棒（碰 1.73）
                    bar9 = grp["barA"] + 8   # 從這根開始找回 Fib1 的進場機會
                    if bar9 >= len(klines):
                        continue

                    # Fib 價位
                    fib0   = grp["fib0"]
                    fib1   = grp["fib1"]
                    fib173 = next((p for r, p, _ in grp["levels"] if abs(r - 1.73) < 0.01), None)
                    fib692 = next((p for r, p, _ in grp["levels"] if abs(r - 6.92) < 0.01), None)
                    if fib692 is None or fib173 is None:
                        continue

                    # bar8 必須真的碰到 1.73（確認信號）
                    k8 = klines[bar8]
                    if fib_side == "short":
                        if not (k8[3] <= fib173 <= k8[2]):
                            continue
                    else:
                        if not (k8[3] <= fib173 <= k8[2]):
                            continue

                    # 從 bar9 開始，找第一根回到 Fib1 的進場棒（限價單）
                    entry_bar = None
                    search_end = min(bar9 + look_fwd, len(klines))
                    for j in range(bar9, search_end):
                        k = klines[j]
                        if fib_side == "short":
                            # 空單：回升到 Fib1（高點 >= fib1）才進場
                            if k[2] >= fib1:
                                entry_bar = j
                                break
                        else:
                            # 多單：回落到 Fib1（低點 <= fib1）才進場
                            if k[3] <= fib1:
                                entry_bar = j
                                break

                    if entry_bar is None:
                        continue   # 沒有回到 Fib1，跳過

                    # 需要足夠的未來K棒
                    if entry_bar + look_fwd >= len(klines) - 1:
                        continue

                    fib1211 = next((p for r, p, _ in grp["levels"] if abs(r - 12.11) < 0.01), None)

                    future = klines[entry_bar + 1: entry_bar + 1 + look_fwd]

                    sl_bar   = None
                    tp692_bar  = None
                    tp1211_bar = None
                    for i, k in enumerate(future):
                        if fib_side == "short":
                            hit_sl   = k[2] >= fib0
                            hit_692  = k[3] <= fib692
                            hit_1211 = fib1211 is not None and k[3] <= fib1211
                        else:
                            hit_sl   = k[3] <= fib0
                            hit_692  = k[2] >= fib692
                            hit_1211 = fib1211 is not None and k[2] >= fib1211

                        if hit_sl   and sl_bar    is None: sl_bar    = i
                        if hit_692  and tp692_bar  is None: tp692_bar  = i
                        if hit_1211 and tp1211_bar is None: tp1211_bar = i

                    # 6.92 結果
                    if tp692_bar is not None and (sl_bar is None or tp692_bar < sl_bar):
                        result692 = "direct_win"
                    elif sl_bar is not None and tp692_bar is not None:
                        result692 = "sl_win"
                    elif sl_bar is not None:
                        result692 = "sl_lose"
                    else:
                        result692 = "neither"

                    # 12.11 結果（在 6.92 直達基礎上繼續往前）
                    if tp1211_bar is not None and (sl_bar is None or tp1211_bar < sl_bar):
                        result1211 = "direct_win"
                    elif sl_bar is not None and tp1211_bar is not None:
                        result1211 = "sl_win"
                    elif sl_bar is not None:
                        result1211 = "sl_lose"
                    else:
                        result1211 = "neither"

                    # relVol at entry_bar
                    rel_vol = None
                    if entry_bar >= 25:
                        avg = sum(klines[i][5] for i in range(entry_bar - 24, entry_bar)) / 24
                        rel_vol = round(klines[entry_bar][5] / avg, 2) if avg > 0 else None

                    # MA 距離%（bar8，確認棒）
                    ma_dist = None
                    if mas[bar8]:
                        ma_dist = round((klines[bar8][4] - mas[bar8]) / mas[bar8] * 100, 2)

                    recs.append({
                        "symbol":      sym,
                        "fibSide":     fib_side,
                        "result":      result692,
                        "result1211":  result1211,
                        "slBar":       sl_bar,
                        "tpBar":       tp692_bar,
                        "tp1211Bar":   tp1211_bar,
                        "relVol":      rel_vol,
                        "maDistPct":   ma_dist,
                        "funding":     funding_map.get(sym),
                    })
                return recs
            except:
                return []

        records = []
        with ThreadPoolExecutor(max_workers=12) as ex:
            futures = [ex.submit(process_sym, s) for s in symbols_data]
            for f in as_completed(futures):
                records.extend(f.result())

        if not records:
            return jsonify({"error": "沒有找到符合的 Fib 組", "total": 0})

        total      = len(records)
        direct_win = sum(1 for r in records if r["result"] == "direct_win")
        sl_win     = sum(1 for r in records if r["result"] == "sl_win")
        sl_lose    = sum(1 for r in records if r["result"] == "sl_lose")
        neither    = sum(1 for r in records if r["result"] == "neither")

        # 12.11 統計
        dw1211 = sum(1 for r in records if r["result1211"] == "direct_win")
        sw1211 = sum(1 for r in records if r["result1211"] == "sl_win")

        # 平均到達根數
        direct_bars  = [r["tpBar"]     for r in records if r["result"]    == "direct_win" and r["tpBar"]     is not None]
        sl_bars      = [r["tpBar"]     for r in records if r["result"]    == "sl_win"     and r["tpBar"]     is not None]
        direct1211_b = [r["tp1211Bar"] for r in records if r["result1211"]== "direct_win" and r["tp1211Bar"] is not None]
        avg_direct   = round(sum(direct_bars)  / len(direct_bars),  1) if direct_bars  else None
        avg_sl       = round(sum(sl_bars)      / len(sl_bars),      1) if sl_bars      else None
        avg_1211     = round(sum(direct1211_b) / len(direct1211_b), 1) if direct1211_b else None

        # ── 盈虧比計算 ────────────────────────────────────────────────
        # entry = Fib 1, SL = Fib 0
        # TP1 = Fib 6.92 → Reward = 5.92R
        # TP2 = Fib 12.11 → Reward = 11.11R
        RR_692  = 5.92
        RR_1211 = 11.11

        effective = total - neither
        if effective > 0:
            win_rate       = direct_win / effective
            loss_rate      = (sl_win + sl_lose) / effective
            ev_per_trade   = round(win_rate * RR_692  - loss_rate * 1.0, 3)
            ev_1211        = round(dw1211   / effective * RR_1211 - loss_rate * 1.0, 3)
            break_even     = round(1 / (1 + RR_692)  * 100, 1)
            break_even1211 = round(1 / (1 + RR_1211) * 100, 1)
            actual_win_pct = round(win_rate * 100, 1)
        else:
            ev_per_trade = ev_1211 = break_even = break_even1211 = actual_win_pct = None

        def bucket(key, bkts):
            out = []
            for lbl, fn in bkts:
                g = [r for r in records if r.get(key) is not None and fn(r[key])]
                if not g: continue
                dw   = sum(1 for r in g if r["result"]    == "direct_win")
                sw   = sum(1 for r in g if r["result"]    == "sl_win")
                sl   = sum(1 for r in g if r["result"]    in ("sl_win","sl_lose"))
                dw12 = sum(1 for r in g if r["result1211"]== "direct_win")
                eff  = len(g) - sum(1 for r in g if r["result"] == "neither")
                ev   = round((dw/eff)*RR_692  - (sl/eff)*1.0, 2) if eff > 0 else None
                ev12 = round((dw12/eff)*RR_1211 - (sl/eff)*1.0, 2) if eff > 0 else None
                out.append({
                    "label":          lbl,
                    "total":          len(g),
                    "directWin":      dw,
                    "slWin":          sw,
                    "directRate":     round(dw   / len(g) * 100, 1),
                    "slWinRate":      round(sw   / len(g) * 100, 1),
                    "eventualRate":   round((dw + sw) / len(g) * 100, 1),
                    "ev":             ev,
                    "direct1211":     dw12,
                    "direct1211Rate": round(dw12 / len(g) * 100, 1),
                    "ev1211":         ev12,
                })
            return out

        vol_buckets     = bucket("relVol", [
            ("<1x",  lambda v: v < 1),
            ("1-2x", lambda v: 1 <= v < 2),
            ("2-3x", lambda v: 2 <= v < 3),
            (">3x",  lambda v: v >= 3),
        ])
        funding_buckets = bucket("funding", [
            ("<-0.01%",  lambda v: v < -0.01),
            ("-0.01~0%", lambda v: -0.01 <= v < 0),
            ("0~0.01%",  lambda v: 0 <= v < 0.01),
            (">0.01%",   lambda v: v >= 0.01),
        ])
        ma_buckets = bucket("maDistPct", [
            ("<1%",  lambda v: abs(v) < 1),
            ("1-3%", lambda v: 1 <= abs(v) < 3),
            ("3-5%", lambda v: 3 <= abs(v) < 5),
            (">5%",  lambda v: abs(v) >= 5),
        ])

        # 每個 symbol 取最佳結果（同一幣種可能有多組 fib）
        # 優先順序：direct_win > sl_win > sl_lose > neither
        priority = {"direct_win":0,"sl_win":1,"sl_lose":2,"neither":3}
        sym_best = {}
        for r in records:
            sym = r["symbol"]
            if sym not in sym_best or priority[r["result"]] < priority[sym_best[sym]["result"]]:
                sym_best[sym] = r

        detail_list = sorted(sym_best.values(), key=lambda r: priority[r["result"]])

        return jsonify({
            "total":       total,
            "directWin":   direct_win,
            "slWin":       sl_win,
            "slLose":      sl_lose,
            "neither":     neither,
            "directRate":  round(direct_win / total * 100, 1),
            "slWinRate":   round(sl_win     / total * 100, 1),
            "eventualRate":round((direct_win + sl_win) / total * 100, 1),
            "avgDirectBars": avg_direct,
            "avgSlBars":     avg_sl,
            "rrReward":      round(RR_692, 2),
            "evPerTrade":    ev_per_trade,
            "breakEvenWinRate": break_even,
            "actualWinPct":  actual_win_pct,
            "dw1211":        dw1211,
            "sw1211":        sw1211,
            "direct1211Rate":round(dw1211 / total * 100, 1) if total else 0,
            "eventual1211Rate":round((dw1211 + sw1211) / total * 100, 1) if total else 0,
            "avg1211Bars":   round(sum(direct1211_b)/len(direct1211_b),1) if direct1211_b else None,
            "ev1211":        ev_1211,
            "breakEven1211": break_even1211,
            "lookForward":   look_fwd,
            "symbolCount":   len(symbols_data),
            "fibSide":       fib_side,
            "volBuckets":    vol_buckets,
            "fundingBuckets":funding_buckets,
            "maBuckets":     ma_buckets,
            "detail":        detail_list,
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
    app.run(host=host, port=port, debug=False)
