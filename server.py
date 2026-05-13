"""
BingX MA + Fibonacci 篩選器（永續合約版）
python server.py
"""
import threading, webbrowser, time, random, traceback, hmac, hashlib, json, os
from urllib.parse import urlencode
from flask import Flask, jsonify, request, render_template
import requests

app = Flask(__name__, template_folder='templates', static_folder='static')
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
        limit     = min(int(request.args.get("limit", 40)), 200)
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

        for sym in symbols:
            try:
                klines = get_klines(sym, iv, kline_limit)
                if len(klines) < mp + 5: continue

                closes     = [k[4] for k in klines]
                last_close = closes[-1]
                mas        = ma_series(closes, mp, mt)
                ma_val     = mas[-1]
                if ma_val is None: continue

                is_above = last_close > ma_val

                fib_groups = []
                if use_fib:
                    if not is_above: continue
                    fib_groups = find_all_fibs(klines, mas)
                    if not fib_groups: continue

                tk         = tickers.get(sym, {})
                change_pct = tk.get("changePct", 0)
                vol_usdt   = tk.get("quoteVol",  0)
                vol_change = vol_change_map.get(sym, None)

                # ── 相對成交量：當前K棒量 ÷ 前24根均量 ──────────────────
                vols = [k[5] for k in klines]
                if len(vols) >= 25 and sum(vols[-25:-1]) > 0:
                    rel_vol = vols[-1] / (sum(vols[-25:-1]) / 24)
                else:
                    rel_vol = None

                # ── 資金費率 & OI ────────────────────────────────────────
                funding   = funding_map.get(sym, None)
                oi_usdt   = oi_map.get(sym, None)

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
                            "levels": [list(lv) for lv in grp["levels"]],  # tuple→list
                            "barA":   barA_r,
                            "barB":   barB_r,
                        })

                results.append({
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
                    "volChangePct": vol_change,
                    "relVol":      round(rel_vol, 2) if rel_vol is not None else None,
                    "funding":     funding,
                    "oiUsdt":      oi_usdt,
                })
            except Exception as e:
                errors.append({"symbol": sym, "error": str(e)})

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
        fibs = find_all_fibs(klines, mas, strict=False)  # 全歷史搜尋
        # 過濾：只保留 fib0 在當前價格 ±50% 以內的組（剔除差太遠的歷史價位）
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
                "bar5":   grp["barA"] + 4,   # 第5根（用於標記）
                "bar8":   grp["barA"] + 7,   # 第8根（用於標記）
            })
        return jsonify({
            "symbol":   sym,
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
    下單（多方開倉，含止損止盈）
    body: {
      symbol, entry, sl, tp1, tp1_pct, tp2, tp2_pct, quantity
    }
    """
    try:
        d          = request.json or {}
        symbol     = d.get("symbol","")
        order_type = d.get("order_type","LIMIT")   # LIMIT 或 MARKET
        entry      = float(d.get("entry") or 0)
        sl         = float(d.get("sl", 0))
        tp1        = float(d.get("tp1", 0)) if d.get("tp1") else None
        tp2        = float(d.get("tp2", 0)) if d.get("tp2") else None
        qty        = float(d.get("quantity", 0))

        if not symbol or not sl or qty <= 0:
            return jsonify({"error": "缺少必要參數（symbol / sl / quantity）"}), 400
        if order_type == "LIMIT" and not entry:
            return jsonify({"error": "限價單需填入開倉點位"}), 400

        results = []

        # ── 計算各 TP 數量 ────────────────────────────────────────────
        tp1_pct = float(d.get("tp1_pct") or 0)
        tp2_pct = float(d.get("tp2_pct") or 0)
        tp1_qty = round(qty * tp1_pct / 100, 4) if tp1 and tp1_pct else 0
        tp2_qty = round(qty * tp2_pct / 100, 4) if tp2 and tp2_pct else 0

        # ── 主單（含 SL + TP1）───────────────────────────────────────
        order_body = {
            "symbol":       symbol,
            "side":         "BUY",
            "positionSide": "LONG",
            "type":         order_type,
            "quantity":     qty,
            "stopLoss":     json.dumps({"type":"STOP_MARKET","stopPrice":sl,"workingType":"MARK_PRICE"}),
        }
        if order_type == "LIMIT":
            order_body["price"] = entry
        # TP1 嵌入主單，帶部分數量
        if tp1 and tp1_qty > 0:
            order_body["takeProfit"] = json.dumps({
                "type":        "TAKE_PROFIT_MARKET",
                "stopPrice":   tp1,
                "workingType": "MARK_PRICE",
                "quantity":    tp1_qty
            })
        elif tp1:
            order_body["takeProfit"] = json.dumps({"type":"TAKE_PROFIT_MARKET","stopPrice":tp1,"workingType":"MARK_PRICE"})

        # TP2 嵌入主單，帶部分數量
        if tp2 and tp2_qty > 0:
            order_body["takeProfit2"] = json.dumps({
                "type":        "TAKE_PROFIT_MARKET",
                "stopPrice":   tp2,
                "workingType": "MARK_PRICE",
                "quantity":    tp2_qty
            })

        res = bpost_auth("/openApi/swap/v2/trade/order", order_body)
        results.append({"step": "主單(含SL+TP1)", "response": res})

        # 主單失敗就停
        if res.get("code", -1) != 0:
            return jsonify({"ok": False, "results": results})

        # ── 背景輪詢：等主單成交後自動補送 TP2 ──────────────────────
        if tp2 and tp2_qty > 0:
            order_id = str(res["data"]["order"].get("orderID") or res["data"]["order"].get("orderId",""))
            tp2_info = {
                "symbol":       symbol,
                "side":         "SELL",
                "positionSide": "LONG",
                "type":         "TAKE_PROFIT_MARKET",
                "stopPrice":    tp2,
                "quantity":     tp2_qty,
                "workingType":  "MARK_PRICE",
                "reduceOnly":   "true",
            }
            def poll_and_send_tp2(oid, tp2_body, sym):
                for _ in range(60):          # 最多輪詢 5 分鐘（60×5s）
                    time.sleep(5)
                    try:
                        q = bget_auth("/openApi/swap/v2/trade/order",
                                      {"symbol": sym, "orderId": oid})
                        status = q.get("data", {}).get("order", {}).get("status", "")
                        if status == "FILLED":
                            bpost_auth("/openApi/swap/v2/trade/order", tp2_body)
                            print(f"[TP2] {sym} 主單成交，TP2 已送出")
                            return
                        if status in ("CANCELLED", "FAILED", "EXPIRED"):
                            print(f"[TP2] {sym} 主單 {status}，放棄 TP2")
                            return
                    except Exception as ex:
                        print(f"[TP2] 輪詢錯誤：{ex}")
                print(f"[TP2] {sym} 輪詢超時，TP2 未送出")

            t = threading.Thread(target=poll_and_send_tp2,
                                 args=(order_id, tp2_info, symbol), daemon=True)
            t.start()
            results.append({"step": "TP2 背景監控", "response": {"msg": f"主單成交後自動送 TP2 @ {tp2}，qty={tp2_qty}"}})

        return jsonify({"ok": True, "results": results})

    except Exception as e:
        tb = traceback.format_exc()
        print("=== /api/place_order ERROR ===")
        print(tb)
        return jsonify({"error": str(e)}), 500


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
