"""
Quantitative Momentum Scanner v3 — Alpha Architect QM Framework
================================================================
Fixes vs v2:
  1. ADV      — true 20-day and 63-day average dollar volume (post price-load)
  2. Momentum — skip-adjusted lookback: 6M=T-147→T-21, 9M=T-210→T-21
  3. Beta     — capped at trailing BETA_WINDOW_DAYS (252) not full history
  4. Safety   — empty-portfolio guard before any iloc[0] access
  5. Note     — "type=CS" already filters non-CS; EXCLUDED_TYPES is safety layer

Usage:
  pip install requests pandas numpy tqdm
  python qm_scanner_v3.py --api-key YOUR_MASSIVE_KEY
  python qm_scanner_v3.py --api-key YOUR_KEY --universe-size 300   # quick test
"""

import argparse, time, logging
from datetime import date, timedelta

import requests
import pandas as pd
import numpy as np
from tqdm import tqdm

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("QM-v3")

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL          = "https://api.massive.com"
SPY_TICKER        = "SPY"
RATE_LIMIT_SLEEP  = 0.13

UNIVERSE_SIZE     = 1500
LIQUIDITY_PCT_CUT = 0.15
HIGH_BETA_PCT_CUT = 0.10
LOW_MOM_PCT_CUT   = 0.05
MOMENTUM_TOP_N    = 100
QUALITY_TOP_N     = 50
BETA_WINDOW_DAYS  = 252          # FIX 3: used in compute_beta()
HISTORY_MONTHS    = 18

# Safety layer — Massive type=CS call already excludes most of these
EXCLUDED_TYPES = {"ADR","ETF","ETN","REIT","FUND","RIGHT","WARRANT","UNIT"}


# ── Massive API client ────────────────────────────────────────────────────────
class MassiveClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })

    def _get(self, path: str, params: dict = None, retries: int = 3) -> dict:
        url = f"{BASE_URL}{path}"
        for attempt in range(retries):
            try:
                r = self.session.get(url, params=params, timeout=20)
                if r.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    log.warning(f"Rate limited — sleeping {wait}s")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                if attempt == retries - 1:
                    log.error(f"Failed {url}: {e}")
                    return {}
                time.sleep(1.5)
        return {}

    def get_tickers(self) -> list[dict]:
        tickers, cursor = [], None
        params = {
            "market": "stocks", "type": "CS",
            "active": "true", "limit": 1000,
            "sort": "ticker", "order": "asc",
        }
        log.info("Fetching ticker reference data …")
        while True:
            if cursor:
                params["cursor"] = cursor
            data    = self._get("/v3/reference/tickers", params)
            results = data.get("results", [])
            if not results:
                break
            tickers.extend(results)
            next_url = data.get("next_url", "")
            cursor   = next_url.split("cursor=")[-1].split("&")[0] if "cursor=" in next_url else None
            if not cursor:
                break
            log.info(f"  … {len(tickers)} tickers")
            time.sleep(RATE_LIMIT_SLEEP)
        log.info(f"Total raw tickers: {len(tickers)}")
        return tickers

    def get_daily_bars(self, ticker: str, from_date: str, to_date: str) -> pd.DataFrame:
        params = {"adjusted": "true", "sort": "asc", "limit": 50000}
        path   = f"/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"
        data   = self._get(path, params)
        rows   = data.get("results", [])
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["t"], unit="ms").dt.date
        df = df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"})
        keep = [c for c in ["date","open","high","low","close","volume"] if c in df.columns]
        df   = df[keep].sort_values("date").reset_index(drop=True)
        time.sleep(RATE_LIMIT_SLEEP)
        return df

    def get_snapshot_all(self) -> list[dict]:
        return self._get("/v2/snapshot/locale/us/markets/stocks/tickers").get("tickers", [])


# ── Helpers ───────────────────────────────────────────────────────────────────
def trading_days_ago(n: int) -> str:
    return (date.today() - timedelta(days=int(n * 365 / 252) + 15)).isoformat()

def today_str() -> str:
    return date.today().isoformat()


# ── Step 1: Universe (initial pass using snapshot dollar volume) ──────────────
def build_initial_universe(client: MassiveClient, universe_size: int) -> tuple[list[str], dict]:
    """
    Pull active CS tickers, apply type/SIC exclusions.
    Use snapshot 1-2 day dollar volume only as a rough pre-filter to avoid
    fetching price history for obvious micro-caps/illiquid names.
    True 20/63-day ADV is computed in Step 1b after price history is loaded.
    """
    log.info("── STEP 1a: Initial universe filter ──")
    raw = client.get_tickers()
    if not raw:
        raise RuntimeError("No tickers returned — check API key")

    meta = {}
    for t in raw:
        tkr  = t.get("ticker", "")
        typ  = t.get("type", "").upper()
        sic  = t.get("sic_description", "").upper()
        if not tkr or "." in tkr:
            continue
        if typ in EXCLUDED_TYPES:
            continue
        if "REIT" in sic or "REAL ESTATE INV" in sic:
            continue
        meta[tkr] = {
            "name": t.get("name", ""),
            "type": typ,
            "exchange": t.get("primary_exchange", ""),
        }
    log.info(f"After type/SIC exclusion: {len(meta)} tickers")

    # Snapshot dollar volume as coarse pre-filter (keep ~3× target)
    snaps = client.get_snapshot_all()
    dv_snap = {}
    if snaps:
        for s in snaps:
            tkr = s.get("ticker", "")
            if tkr not in meta:
                continue
            day  = s.get("day", {})
            prev = s.get("prevDay", {})
            dv   = max(day.get("c", 0) * day.get("v", 0),
                       prev.get("c", 0) * prev.get("v", 0))
            dv_snap[tkr] = dv
    else:
        log.warning("Snapshot unavailable — keeping all tickers for price fetch")
        dv_snap = {t: 1.0 for t in meta}

    df = pd.DataFrame([{"ticker": t, "dv_snap": dv_snap.get(t, 0)} for t in meta])
    df = df[df["dv_snap"] > 0].sort_values("dv_snap", ascending=False)

    # Keep 3× target to allow for price-history failures & ADV re-ranking
    prefetch_n = min(len(df), universe_size * 3)
    tickers    = df.head(prefetch_n)["ticker"].tolist()
    log.info(f"Pre-filter universe: {len(tickers)} tickers (target: {universe_size})")
    return tickers, meta


def rank_by_true_adv(
    prices: dict[str, pd.DataFrame],
    universe_size: int,
) -> list[str]:
    """
    FIX 1: Compute true 20-day and 63-day average dollar volume from
    loaded price history. Re-rank and keep top `universe_size`.
    Drops bottom 15% by 20-day ADV.
    """
    log.info("── STEP 1b: Re-rank by true 20/63-day average dollar volume ──")
    rows = []
    for tkr, df in prices.items():
        df2 = df.copy()
        df2["dv"] = df2["close"] * df2["volume"]
        adv_20 = df2["dv"].tail(20).mean()
        adv_63 = df2["dv"].tail(63).mean()
        rows.append({"ticker": tkr, "adv_20": adv_20, "adv_63": adv_63})

    df_adv = pd.DataFrame(rows)

    # Drop bottom 15% by 20-day ADV (per QM liquidity rule)
    floor = df_adv["adv_20"].quantile(LIQUIDITY_PCT_CUT)
    df_adv = df_adv[df_adv["adv_20"] >= floor]

    # Primary rank: 20-day ADV (tighter, more current); 63-day as tiebreak
    df_adv = df_adv.sort_values(["adv_20","adv_63"], ascending=False).head(universe_size)
    tickers = df_adv["ticker"].tolist()

    log.info(
        f"After true ADV re-rank: {len(tickers)} tickers  "
        f"| 20d ADV range: "
        f"${df_adv['adv_20'].min()/1e6:.1f}M – ${df_adv['adv_20'].max()/1e6:.1f}M"
    )
    return tickers


# ── Price history loader ──────────────────────────────────────────────────────
def load_price_history(
    client: MassiveClient,
    tickers: list[str],
    from_date: str,
    to_date: str,
    min_bars: int = 300,
) -> dict[str, pd.DataFrame]:
    log.info(f"Fetching {HISTORY_MONTHS}m price history for {len(tickers)} tickers …")
    prices, skipped = {}, 0
    for tkr in tqdm(tickers, desc="Price fetch", unit="tkr"):
        df = client.get_daily_bars(tkr, from_date, to_date)
        if df.empty or len(df) < min_bars:
            skipped += 1
            continue
        prices[tkr] = df
    log.info(f"Loaded: {len(prices)} ok  |  skipped: {skipped}")
    return prices


# ── Step 2: Remove Outliers ───────────────────────────────────────────────────
def compute_beta(
    stock_rets: pd.Series,
    market_rets: pd.Series,
    window: int = BETA_WINDOW_DAYS,   # FIX 3: enforce trailing window
) -> float:
    aligned = pd.concat([stock_rets, market_rets], axis=1).dropna()
    aligned = aligned.tail(window)                                # FIX 3
    if len(aligned) < 60:
        return np.nan
    cov = np.cov(aligned.iloc[:, 0], aligned.iloc[:, 1])
    return cov[0, 1] / cov[1, 1] if cov[1, 1] != 0 else np.nan


def compute_momentum(closes: pd.Series, skip: int = 21) -> float:
    """12_2: from T-273 to T-21  (252 trading days of signal, skip last month)."""
    need = 252 + skip
    if len(closes) < need:
        return np.nan
    return (closes.iloc[-skip] / closes.iloc[-(252 + skip)]) - 1.0


def compute_momentum_n(closes: pd.Series, n_days: int, skip: int = 21) -> float:
    """
    FIX 2: Skip-adjusted lookback.
      6M → n_days=126: T-(126+21)=T-147 to T-21
      9M → n_days=189: T-(189+21)=T-210 to T-21
    Measures exactly n_days of return, excluding the most recent month.
    """
    need = n_days + skip
    if len(closes) < need:
        return np.nan
    return (closes.iloc[-skip] / closes.iloc[-need]) - 1.0


def remove_outliers(
    prices: dict[str, pd.DataFrame],
    market_df: pd.DataFrame,
) -> tuple[list[str], pd.DataFrame]:
    log.info("── STEP 2: Removing outliers ──")

    mkt_close = market_df.set_index("date")["close"]
    mkt_rets  = mkt_close.pct_change().dropna()

    rows = []
    for tkr, df in prices.items():
        c    = df.set_index("date")["close"]
        rets = c.pct_change().dropna()
        rows.append({
            "ticker":   tkr,
            "beta":     compute_beta(rets, mkt_rets),   # FIX 3 inside
            "mom_6m":   compute_momentum_n(c, 126),     # FIX 2
            "mom_9m":   compute_momentum_n(c, 189),     # FIX 2
            "mom_12_2": compute_momentum(c),
        })

    df_m = pd.DataFrame(rows).dropna(subset=["beta","mom_6m","mom_9m","mom_12_2"])
    n0   = len(df_m)

    beta_ceil  = df_m["beta"].quantile(1 - HIGH_BETA_PCT_CUT)
    mom6_floor = df_m["mom_6m"].quantile(LOW_MOM_PCT_CUT)
    mom9_floor = df_m["mom_9m"].quantile(LOW_MOM_PCT_CUT)

    df_m = df_m[
        (df_m["beta"]   <= beta_ceil)  &
        (df_m["mom_6m"] >= mom6_floor) &
        (df_m["mom_9m"] >= mom9_floor)
    ]

    log.info(
        f"After Step 2: {len(df_m)} remain (removed {n0-len(df_m)}) | "
        f"beta≤{beta_ceil:.2f}  6M≥{mom6_floor:.1%}  9M≥{mom9_floor:.1%}"
    )
    return df_m["ticker"].tolist(), df_m


# ── Step 3: Momentum Screen ───────────────────────────────────────────────────
def momentum_screen(df_metrics: pd.DataFrame, top_n: int) -> pd.DataFrame:
    log.info("── STEP 3: Momentum screen (12_2) ──")
    df = df_metrics.sort_values("mom_12_2", ascending=False).head(top_n)
    log.info(f"Top-{top_n} range: {df['mom_12_2'].min():.1%} – {df['mom_12_2'].max():.1%}")
    return df.reset_index(drop=True)


# ── Step 4: FIP Quality Screen ────────────────────────────────────────────────
def frog_in_pan_score(
    closes: pd.Series,
    lookback: int = 252,
) -> tuple[float, float, float]:
    """
    FIP = sign(12_2 mom) × (pct_neg - pct_pos)  [Da, Gurun & Warachka 2014]
    Returns: (fip_score, pct_positive_days, pct_negative_days)
    Lower fip = smoother uptrend = higher quality.
    """
    window = closes.iloc[-lookback:-21]
    if len(window) < 100:
        return np.nan, np.nan, np.nan
    dr      = window.pct_change().dropna()
    pct_pos = float((dr > 0).mean())
    pct_neg = float((dr < 0).mean())
    mom     = (closes.iloc[-21] / closes.iloc[-lookback]) - 1.0
    sign    = 1 if mom >= 0 else -1
    return sign * (pct_neg - pct_pos), pct_pos, pct_neg


def quality_screen(
    top_momentum: pd.DataFrame,
    prices: dict[str, pd.DataFrame],
    top_n: int,
) -> pd.DataFrame:
    log.info("── STEP 4: FIP quality screen ──")
    rows = []
    for _, row in top_momentum.iterrows():
        tkr = row["ticker"]
        if tkr not in prices:
            continue
        c = prices[tkr].set_index("date")["close"]
        fip, ppos, pneg = frog_in_pan_score(c)
        rows.append({"ticker": tkr, "fip_score": fip,
                     "pct_positive_days": ppos, "pct_negative_days": pneg})

    df_fip = pd.DataFrame(rows).dropna()
    df_out = top_momentum.merge(df_fip, on="ticker", how="inner")
    df_out = df_out.sort_values("fip_score", ascending=True).head(top_n)
    log.info(
        f"After Step 4: {len(df_out)} stocks | "
        f"FIP range: {df_out['fip_score'].min():.4f} – {df_out['fip_score'].max():.4f}"
    )
    return df_out.reset_index(drop=True)


# ── Step 5: Portfolio ─────────────────────────────────────────────────────────
def is_smart_rebalance_month() -> bool:
    """Rebalance near END of Feb/May/Aug/Nov (day ≥ 20) per Sias (2007)."""
    today = date.today()
    return today.month in {2, 5, 8, 11} and today.day >= 20


def build_portfolio(
    final_stocks: pd.DataFrame,
    prices: dict[str, pd.DataFrame],
    meta: dict,
    adv_map: dict,
) -> pd.DataFrame:
    # FIX 4: empty guard
    if final_stocks.empty:
        log.warning("No stocks passed QM filters — portfolio is empty")
        return pd.DataFrame()

    log.info("── STEP 5: Portfolio construction ──")
    rows = []
    for rank, (_, row) in enumerate(final_stocks.iterrows(), start=1):
        tkr  = row["ticker"]
        df   = prices.get(tkr, pd.DataFrame())
        last_close = float(df["close"].iloc[-1]) if not df.empty else np.nan
        rows.append({
            "rank":              rank,
            "ticker":            tkr,
            "name":              meta.get(tkr, {}).get("name", "")[:24],
            "exchange":          meta.get(tkr, {}).get("exchange", ""),
            "last_close":        last_close,
            "adv_20d_M":         round(adv_map.get(tkr, np.nan) / 1e6, 2),
            "mom_12_2":          row.get("mom_12_2", np.nan),
            "mom_6m":            row.get("mom_6m",   np.nan),
            "mom_9m":            row.get("mom_9m",   np.nan),
            "beta":              row.get("beta",      np.nan),
            "fip_score":         row.get("fip_score", np.nan),
            "pct_positive_days": row.get("pct_positive_days", np.nan),
            "pct_negative_days": row.get("pct_negative_days", np.nan),
        })

    df_port = pd.DataFrame(rows)
    n = len(df_port)
    df_port["equal_weight_pct"] = round(100.0 / n, 2)
    df_port["smart_rebalance"]  = is_smart_rebalance_month()
    return df_port


# ── Output ────────────────────────────────────────────────────────────────────
def print_portfolio(df: pd.DataFrame):
    # FIX 4: guard here too
    if df.empty:
        print("\n⚠  No stocks passed the QM filters. Portfolio is empty.")
        return

    smart = df["smart_rebalance"].iloc[0]
    flag  = "✓ SMART REBALANCE WINDOW" if smart else "⚠  WAIT — rebalance day≥20 in Feb/May/Aug/Nov"
    print("\n" + "═"*95)
    print(f"  QM PORTFOLIO  [{today_str()}]   {len(df)} stocks @ {df['equal_weight_pct'].iloc[0]:.1f}% each")
    print(f"  Seasonality:  {flag}")
    print("═"*95)

    disp = df.copy()
    disp["mom_12_2"]         = disp["mom_12_2"].map("{:.1%}".format)
    disp["mom_6m"]           = disp["mom_6m"].map("{:.1%}".format)
    disp["mom_9m"]           = disp["mom_9m"].map("{:.1%}".format)
    disp["beta"]             = disp["beta"].map("{:.2f}".format)
    disp["fip_score"]        = disp["fip_score"].map("{:.4f}".format)
    disp["pct_positive_days"]= disp["pct_positive_days"].map("{:.1%}".format)
    disp["pct_negative_days"]= disp["pct_negative_days"].map("{:.1%}".format)
    disp["last_close"]       = disp["last_close"].map("${:.2f}".format)
    disp["adv_20d_M"]        = disp["adv_20d_M"].map("${:.1f}M".format)
    disp["equal_weight_pct"] = disp["equal_weight_pct"].map("{:.1f}%".format)

    cols = ["rank","ticker","name","mom_12_2","mom_6m","mom_9m","beta",
            "fip_score","pct_positive_days","last_close","adv_20d_M","equal_weight_pct"]
    print(disp[cols].to_string(index=False))
    print("═"*95 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def run_scanner(
    api_key:        str,
    universe_size:  int = UNIVERSE_SIZE,
    momentum_top_n: int = MOMENTUM_TOP_N,
    quality_top_n:  int = QUALITY_TOP_N,
    output_csv:     str = "qm_portfolio_v3.csv",
):
    client    = MassiveClient(api_key)
    from_date = trading_days_ago(int(HISTORY_MONTHS * 21))
    to_date   = today_str()
    log.info(f"Window: {from_date} → {to_date}  ({HISTORY_MONTHS} months)")

    # Step 1a: coarse universe
    prefetch, meta = build_initial_universe(client, universe_size)

    # Market proxy
    log.info(f"Fetching {SPY_TICKER} …")
    market_df = client.get_daily_bars(SPY_TICKER, from_date, to_date)
    if market_df.empty:
        raise RuntimeError("Could not fetch SPY — check API key and plan")

    # Price history for prefetch list
    prices_all = load_price_history(client, prefetch, from_date, to_date)

    # Step 1b: re-rank by true 20/63-day ADV → final universe
    final_universe = rank_by_true_adv(prices_all, universe_size)
    prices = {t: prices_all[t] for t in final_universe if t in prices_all}

    # Build ADV lookup for portfolio output
    adv_map = {}
    for tkr, df in prices.items():
        df2 = df.copy(); df2["dv"] = df2["close"] * df2["volume"]
        adv_map[tkr] = df2["dv"].tail(20).mean()

    # Step 2
    survivors, df_metrics = remove_outliers(prices, market_df)
    prices = {t: prices[t] for t in survivors if t in prices}

    # Step 3
    top_mom = momentum_screen(df_metrics, momentum_top_n)

    # Step 4
    top_quality = quality_screen(top_mom, prices, quality_top_n)

    # Step 5
    portfolio = build_portfolio(top_quality, prices, meta, adv_map)

    print_portfolio(portfolio)

    if not portfolio.empty:
        portfolio.to_csv(output_csv, index=False)
        log.info(f"Saved → {output_csv}")

    return portfolio


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="QM Scanner v3 — Alpha Architect framework via Massive API"
    )
    p.add_argument("--api-key",        required=True)
    p.add_argument("--universe-size",  type=int, default=UNIVERSE_SIZE)
    p.add_argument("--momentum-top-n", type=int, default=MOMENTUM_TOP_N)
    p.add_argument("--quality-top-n",  type=int, default=QUALITY_TOP_N)
    p.add_argument("--output",         default="qm_portfolio_v3.csv")
    p.add_argument("--debug",          action="store_true")
    args = p.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    run_scanner(args.api_key, args.universe_size, args.momentum_top_n,
                args.quality_top_n, args.output)
