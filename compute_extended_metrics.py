"""
Extended Metrics Computation
=============================
Computes accumulation urgency metrics and sector/industry health aggregations.
Called by the weekly snapshot collector after holdings are selected.

Functions:
  compute_accumulation_urgency()  — per-ticker weekly_health extension
  compute_pullback_recovery()     — pullback_recovery_days (precise definition)
  compute_sector_health()         — sector_health table rows
  compute_industry_health()       — industry_health table rows
"""

import numpy as np
import pandas as pd
from typing import Optional


# ── Accumulation Urgency ──────────────────────────────────────────────────────
def compute_accumulation_urgency(
    closes: pd.Series,
    highs:  pd.Series,
    lows:   pd.Series,
    volumes: pd.Series,
) -> dict:
    """
    Computes all 8 accumulation urgency metrics for one ticker.
    All inputs are date-indexed Series, most-recent last.
    Returns dict of metric values — NaN if insufficient data.
    """
    result = {
        "relative_volume_20d":                None,
        "relative_volume_63d":                None,
        "up_down_volume_ratio":               None,
        "close_location_value_20d":           None,
        "days_since_new_high":                None,
        "pullback_recovery_days":             None,
        "volume_acceleration_4w":             None,
        "momentum_acceleration_rank_change_4w": None,  # filled by caller
    }

    n = len(closes)
    if n < 20:
        return result

    # ── relative_volume_20d ──────────────────────────────────────────────────
    current_vol = float(volumes.iloc[-1])
    avg_vol_20  = float(volumes.tail(20).mean())
    avg_vol_63  = float(volumes.tail(63).mean()) if n >= 63 else None
    result["relative_volume_20d"] = current_vol / avg_vol_20 if avg_vol_20 > 0 else None
    result["relative_volume_63d"] = current_vol / avg_vol_63 if avg_vol_63 and avg_vol_63 > 0 else None

    # ── up_down_volume_ratio (trailing 20d) ───────────────────────────────────
    # Compute daily returns from close series directly (positional alignment)
    c_20   = closes.tail(21).values    # 21 prices → 20 returns
    v_20   = volumes.tail(20).values   # 20 volume values
    dr_20  = np.diff(c_20)             # 20 price changes (not pct, sign is same)
    if len(dr_20) == len(v_20):
        up_mask   = dr_20 > 0
        down_mask = dr_20 < 0
        up_v      = v_20[up_mask]
        down_v    = v_20[down_mask]
        if len(up_v) > 0 and len(down_v) > 0:
            up_vol   = float(up_v.mean())
            down_vol = float(down_v.mean())
            result["up_down_volume_ratio"] = up_vol / down_vol if down_vol > 0 else None
        elif len(up_v) > 0 and len(down_v) == 0:
            # Pure uptrend in window — ratio is undefined (no down days to compare)
            result["up_down_volume_ratio"] = None
        else:
            result["up_down_volume_ratio"] = None

    # ── close_location_value_20d ─────────────────────────────────────────────
    # CLV = avg((close-low)/(high-low)) — floor: high==low → 0.5
    h20 = highs.tail(20)
    l20 = lows.tail(20)
    c20 = closes.tail(20)
    ranges = h20.values - l20.values
    with np.errstate(invalid='ignore', divide='ignore'):
        clvs = np.where(
            ranges > 0,
            (c20.values - l20.values) / ranges,
            0.5    # floor: high==low → neutral 0.5
        )
    result["close_location_value_20d"] = float(np.mean(clvs))

    # ── days_since_new_high ───────────────────────────────────────────────────
    # 52-week (252-bar) trailing window
    window_252 = closes.tail(252)
    peak_52w   = window_252.max()
    # Find the last date the close equalled or exceeded the 52-week high
    at_high    = window_252[window_252 >= peak_52w * 0.9999]  # float tolerance
    if not at_high.empty:
        last_high_pos     = window_252.index.get_loc(at_high.index[-1])
        result["days_since_new_high"] = int(len(window_252) - 1 - last_high_pos)
    else:
        result["days_since_new_high"] = len(window_252) - 1

    # ── pullback_recovery_days ────────────────────────────────────────────────
    result["pullback_recovery_days"] = compute_pullback_recovery(closes)

    # ── volume_acceleration_4w ────────────────────────────────────────────────
    # Change in relative_volume_20d vs 4 weeks (20 trading days) ago
    if n >= 40:
        vol_4w_ago  = float(volumes.tail(40).head(20).mean())   # avg_vol at T-20
        rel_vol_now = result["relative_volume_20d"]
        if vol_4w_ago > 0 and rel_vol_now is not None:
            # relative_volume_20d 4 weeks ago
            past_current_vol = float(volumes.iloc[-21])
            rel_vol_4w_ago   = past_current_vol / vol_4w_ago if vol_4w_ago > 0 else None
            if rel_vol_4w_ago:
                result["volume_acceleration_4w"] = rel_vol_now - rel_vol_4w_ago

    return result


def compute_pullback_recovery(closes: pd.Series) -> Optional[int]:
    """
    Locked definition:
      Pullback: most recent >5% decline from a local closing high within trailing 252d.
      Pullback low: lowest close during that drawdown.
      Recovery: first later close that EXCEEDS the pre-pullback local high close.
      Returns:
        int  = trading days from pullback low to recovery close
        None = currently in pullback (not yet recovered)
        0    = no >5% pullback in trailing 252d
    """
    c = closes.tail(252).reset_index(drop=True)
    n = len(c)

    # Scan forward to find the most recent >5% pullback
    # A pullback starts when close drops >5% below a local high
    # Search from most recent backward to find last event
    THRESHOLD = 0.05

    pullback_start_idx  = None
    pullback_high_val   = None
    pullback_low_idx    = None
    pullback_low_val    = None
    recovery_idx        = None

    # Walk forward through the series
    running_high     = float(c.iloc[0])
    running_high_idx = 0
    in_pullback      = False
    pb_high_val      = None
    pb_high_idx      = None
    pb_low_val       = None
    pb_low_idx       = None

    for i in range(1, n):
        price = float(c.iloc[i])

        if not in_pullback:
            if price > running_high:
                running_high     = price
                running_high_idx = i
            elif (running_high - price) / running_high > THRESHOLD:
                # Pullback starts
                in_pullback  = True
                pb_high_val  = running_high
                pb_high_idx  = running_high_idx
                pb_low_val   = price
                pb_low_idx   = i
        else:
            # In pullback — track the low
            if price < pb_low_val:
                pb_low_val = price
                pb_low_idx = i

            # Check for recovery: close EXCEEDS pre-pullback high
            if price > pb_high_val:
                # Recovered — store this event, reset, continue looking for later ones
                pullback_start_idx = pb_high_idx
                pullback_high_val  = pb_high_val
                pullback_low_idx   = pb_low_idx
                pullback_low_val   = pb_low_val
                recovery_idx       = i

                # Reset for next potential pullback
                in_pullback      = False
                running_high     = price
                running_high_idx = i

    # After full scan:
    if in_pullback:
        # Currently in a pullback that has not recovered
        # This IS the most recent pullback event
        return None   # NULL — not yet recovered

    if pullback_low_idx is None:
        # No >5% pullback found in trailing 252d
        return 0

    # Most recent completed pullback: days from low to recovery
    return int(recovery_idx - pullback_low_idx)


# ── Sector & Industry Health ──────────────────────────────────────────────────
def compute_sector_health(
    snapshot_date: str,
    top100_df: pd.DataFrame,   # all 100 momentum candidates with sector/metrics
    top50_df:  pd.DataFrame,   # final 50 FIP-selected portfolio
) -> list[dict]:
    """
    Aggregate sector-level stats from top-100 and top-50 candidate sets.
    Returns list of dicts, one per sector.
    """
    rows = []
    all_sectors = set(top100_df["sector"].dropna().unique())

    n_top100 = len(top100_df)
    n_top50  = len(top50_df)

    for sector in sorted(all_sectors):
        s100 = top100_df[top100_df["sector"] == sector]
        s50  = top50_df[top50_df["sector"]   == sector]

        cnt100 = len(s100)
        cnt50  = len(s50)

        retention = cnt50 / cnt100 if cnt100 > 0 else None

        def safe_mean(df, col):
            v = df[col].dropna()
            return float(v.mean()) if len(v) > 0 else None

        rows.append({
            "snapshot_date":               snapshot_date,
            "sector":                      sector,
            "stock_count_top100":          cnt100,
            "stock_count_top50":           cnt50,
            "avg_mom_3m_top100":           safe_mean(s100, "mom_3m"),
            "avg_mom_6m_top100":           safe_mean(s100, "mom_6m"),
            "avg_mom_12_2_top100":         safe_mean(s100, "mom_12_2"),
            "avg_fip_top100":              safe_mean(s100, "fip_score"),
            "avg_acceleration_rank_top100":safe_mean(s100, "momentum_acceleration_rank"),
            "avg_mom_3m_top50":            safe_mean(s50,  "mom_3m"),
            "avg_mom_6m_top50":            safe_mean(s50,  "mom_6m"),
            "avg_mom_12_2_top50":          safe_mean(s50,  "mom_12_2"),
            "avg_fip_top50":               safe_mean(s50,  "fip_score"),
            "avg_acceleration_rank_top50": safe_mean(s50,  "momentum_acceleration_rank"),
            "sector_weight_top100":        round(cnt100 / n_top100, 4) if n_top100 > 0 else None,
            "sector_weight_top50":         round(cnt50  / n_top50,  4) if n_top50  > 0 else None,
            "fip_filter_retention_rate":   round(retention, 4) if retention is not None else None,
        })

    return rows


def compute_industry_health(
    snapshot_date: str,
    top100_df: pd.DataFrame,
    top50_df:  pd.DataFrame,
) -> list[dict]:
    """
    Aggregate industry-level stats from top-100 and top-50 candidate sets.
    Returns list of dicts, one per industry.
    """
    rows = []
    all_industries = set(top100_df["industry_clean"].dropna().unique())

    n_top100 = len(top100_df)
    n_top50  = len(top50_df)

    for industry in sorted(all_industries):
        i100 = top100_df[top100_df["industry_clean"] == industry]
        i50  = top50_df[top50_df["industry_clean"]   == industry]

        cnt100 = len(i100)
        cnt50  = len(i50)
        retention = cnt50 / cnt100 if cnt100 > 0 else None

        # Parent sector: use most common sector among top-100 members
        sector = (i100["sector"].mode().iloc[0]
                  if not i100["sector"].dropna().empty else None)

        def safe_mean(df, col):
            v = df[col].dropna() if col in df.columns else pd.Series()
            return float(v.mean()) if len(v) > 0 else None

        rows.append({
            "snapshot_date":                 snapshot_date,
            "industry":                      industry,
            "sector":                        sector,
            "stock_count_top100":            cnt100,
            "stock_count_top50":             cnt50,
            "avg_mom_3m_top100":             safe_mean(i100, "mom_3m"),
            "avg_mom_6m_top100":             safe_mean(i100, "mom_6m"),
            "avg_mom_12_2_top100":           safe_mean(i100, "mom_12_2"),
            "avg_fip_top100":                safe_mean(i100, "fip_score"),
            "avg_acceleration_rank_top100":  safe_mean(i100, "momentum_acceleration_rank"),
            "avg_mom_3m_top50":              safe_mean(i50,  "mom_3m"),
            "avg_mom_6m_top50":              safe_mean(i50,  "mom_6m"),
            "avg_mom_12_2_top50":            safe_mean(i50,  "mom_12_2"),
            "avg_fip_top50":                 safe_mean(i50,  "fip_score"),
            "avg_acceleration_rank_top50":   safe_mean(i50,  "momentum_acceleration_rank"),
            "industry_weight_top100":        round(cnt100/n_top100,4) if n_top100>0 else None,
            "industry_weight_top50":         round(cnt50 /n_top50, 4) if n_top50 >0 else None,
            "fip_filter_retention_rate":     round(retention,4) if retention is not None else None,
        })

    return rows
