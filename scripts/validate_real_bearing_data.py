#!/usr/bin/env python3
"""Validate FactoryPulse's anomaly detector on the public CWRU bearing dataset.

Dataset: Case Western Reserve University Bearing Data Center (real accelerometer recordings of a
test rig with seeded inner-race faults, 12 kHz drive-end sensor).
    https://engineering.case.edu/bearingdatacenter/download-data-file
    Normal baseline : https://engineering.case.edu/sites/default/files/{97..100}.mat
    Inner race 0.007": 105-108   0.014": 169-172   0.021": 209-212   (loads 0-3 HP)
    (file numbers verified against https://engineering.case.edu/bearingdatacenter/12k-drive-end-bearing-fault-data)

Usage:
    python scripts/validate_real_bearing_data.py                      # download to data/cwru, evaluate
    python scripts/validate_real_bearing_data.py --data-dir /path     # use local 97.mat, 105.mat, ... files
    python scripts/validate_real_bearing_data.py --no-download        # fail instead of downloading

Method (nothing here is tuned on this data; the DEFAULT detector settings are used):
  * Each recording is cut into 50 ms windows; the feature is the window RMS of the drive-end accelerometer.
  * Normal recordings sampled at 48 kHz are decimated to 12 kHz (CWRU documents 48 kHz for baseline data).
  * Window RMS is linearly rescaled so the healthy-baseline median equals FactoryPulse's nominal 2.0 mm/s.
    The data are accelerations in g, NOT velocities, so the absolute mm/s alarm limits become *relative*
    limits (2.25x / 3.55x baseline RMS). The other four channels are held at nominal values.
  * Stream per (fault, load): [75 baseline windows of Normal_L] + [held-out normal windows: rest of Normal_L and
    Normal of the other loads] + [all windows of the inner-race recording]. Labels: held-out normal = 0, fault = 1.
  * Window-level precision / recall / F1 / false-positive rate are computed on the detector's `anomaly` flag.
Results are written to reports/validation_cwru.json (read by the app's "Real Data Validation" panel).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from telemetry import NOMINAL, AnomalyDetector  # noqa: E402

BASE_URL = "https://engineering.case.edu/sites/default/files/{id}.mat"
PAGE_URL = "https://engineering.case.edu/bearingdatacenter/download-data-file"
NORMAL = {0: 97, 1: 98, 2: 99, 3: 100}
INNER_RACE = {  # diameter(in) -> {load(HP): file id}
    "0.007": {0: 105, 1: 106, 2: 107, 3: 108},
    "0.014": {0: 169, 1: 170, 2: 171, 3: 172},
    "0.021": {0: 209, 1: 210, 2: 211, 3: 212},
}
FS = 12_000
WINDOW_S = 0.05
N_BASELINE = 75
OUT_PATH = ROOT / "reports" / "validation_cwru.json"


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def download(file_id: int, data_dir: Path) -> Path:
    dest = data_dir / f"{file_id}.mat"
    if dest.exists() and dest.stat().st_size > 100_000:
        return dest
    data_dir.mkdir(parents=True, exist_ok=True)
    url = BASE_URL.format(id=file_id)
    print(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "factorypulse-validation/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        dest.write_bytes(r.read())
    return dest


def load_de_signal(path: Path) -> np.ndarray:
    from scipy.io import loadmat

    mat = loadmat(str(path))
    keys = [k for k in mat if k.endswith("_DE_time")]
    if not keys:
        raise ValueError(f"{path.name}: no *_DE_time channel (keys: {[k for k in mat if not k.startswith('__')]})")
    x = np.asarray(mat[keys[0]], dtype=float).ravel()
    return x


def to_12k(x: np.ndarray) -> tuple[np.ndarray, bool]:
    """Decimate 48 kHz recordings (normal baseline, ~240k samples) to 12 kHz. Returns (signal, decimated?)."""
    if len(x) > 180_000:
        from scipy.signal import decimate

        return decimate(x, 4, zero_phase=True), True
    return x, False


def window_rms(x: np.ndarray, fs: int = FS, window_s: float = WINDOW_S) -> np.ndarray:
    n = int(fs * window_s)
    k = len(x) // n
    return np.sqrt((x[: k * n].reshape(k, n) ** 2).mean(axis=1))


# --------------------------------------------------------------------------- #
# Evaluation (pure function; unit-tested with synthetic arrays)
# --------------------------------------------------------------------------- #
def evaluate_stream(baseline: np.ndarray, heldout_normal: np.ndarray, fault: np.ndarray,
                    window_s: float = WINDOW_S, n_heldout_same_load: Optional[int] = None) -> Dict:
    """Run the default AnomalyDetector over [baseline | held-out normal | fault] window-RMS features."""
    scale = NOMINAL["vibration_rms"] / float(np.median(baseline))
    feat = np.concatenate([baseline, heldout_normal, fault]) * scale
    n_base, n_norm, n_fault = len(baseline), len(heldout_normal), len(fault)
    ts = pd.date_range("2024-01-01", periods=len(feat), freq=pd.to_timedelta(window_s, unit="s"))
    df = pd.DataFrame({"timestamp": ts, "vibration_rms": feat})
    for ch, v in NOMINAL.items():
        if ch != "vibration_rms":
            df[ch] = v
    det = AnomalyDetector(baseline_n=n_base)
    scored = det.score(df)
    flag = scored["anomaly"].to_numpy()
    # The detector's baseline is the first max(10, min(baseline_n, 25% of stream)) samples: score only after it.
    n_used = max(10, min(det.baseline_n, int(len(df) * 0.25)))
    labels = np.concatenate([np.zeros(n_base + n_norm), np.ones(n_fault)])
    sl = slice(n_used, None)
    y, p = labels[sl].astype(bool), flag[sl]
    tp, fp, fn, tn = int((y & p).sum()), int((~y & p).sum()), int((y & ~p).sum()), int((~y & ~p).sum())
    onset = n_base + n_norm
    # False-alarm split: held-out windows from the baseline's own load vs. from other motor loads.
    n_same = n_norm if n_heldout_same_load is None else n_heldout_same_load
    held_flags = flag[n_base:n_base + n_norm]
    same_fp = int(held_flags[:n_same].sum())
    other_fp = int(held_flags[n_same:].sum())
    first_alarm = np.where(flag[onset:])[0]
    return {
        "baseline_windows_used": n_used, "heldout_normal_windows": n_norm, "fault_windows": n_fault,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "false_positive_rate": fp / (fp + tn) if fp + tn else None,
        "fp_same_load": same_fp, "heldout_same_load": n_same,
        "fp_other_load": other_fp, "heldout_other_load": n_norm - n_same,
        "detection_delay_windows": int(first_alarm[0]) if len(first_alarm) else None,
        "detection_delay_ms": int(first_alarm[0] * window_s * 1000) if len(first_alarm) else None,
    }


def aggregate(rows: List[Dict]) -> Dict:
    tp, fp, fn, tn = (sum(r[k] for r in rows) for k in ("tp", "fp", "fn", "tn"))
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec, "recall": rec, "f1": f1,
            "false_positive_rate": fp / (fp + tn) if fp + tn else None,
            "fp_same_load": sum(r["fp_same_load"] for r in rows),
            "heldout_same_load": sum(r["heldout_same_load"] for r in rows),
            "fp_other_load": sum(r["fp_other_load"] for r in rows),
            "heldout_other_load": sum(r["heldout_other_load"] for r in rows),
            "streams_with_detection": sum(r["detection_delay_windows"] is not None for r in rows),
            "streams": len(rows)}


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data" / "cwru")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args()

    needed = list(NORMAL.values()) + [i for d in INNER_RACE.values() for i in d.values()]
    missing = [i for i in needed if not (args.data_dir / f"{i}.mat").exists()]
    if missing and not args.no_download:
        print(f"Downloading {len(missing)} CWRU file(s) to {args.data_dir} ...")
        try:
            for i in missing:
                download(i, args.data_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"\nDownload failed ({type(exc).__name__}: {exc}).")
    missing = [i for i in needed if not (args.data_dir / f"{i}.mat").exists()]
    if missing:
        print("\nCould not obtain these files:", missing)
        print(f"Download them manually from {PAGE_URL}")
        print("  e.g. " + BASE_URL.format(id=missing[0]))
        print(f"and place them (named <id>.mat) in {args.data_dir}, then re-run with --data-dir.")
        return 2

    notes: List[str] = []
    normal_rms: Dict[int, np.ndarray] = {}
    for load, fid in NORMAL.items():
        x, dec = to_12k(load_de_signal(args.data_dir / f"{fid}.mat"))
        if dec:
            notes.append(f"{fid}.mat ({len(x) * 4} samples) treated as 48 kHz and decimated to 12 kHz")
        normal_rms[load] = window_rms(x)
    print(f"Normal windows per load: { {k: len(v) for k, v in normal_rms.items()} }")

    rows = []
    for dia, loads in INNER_RACE.items():
        for load, fid in loads.items():
            xf, _ = to_12k(load_de_signal(args.data_dir / f"{fid}.mat"))
            fault = window_rms(xf)
            base = normal_rms[load][:N_BASELINE]
            same = normal_rms[load][N_BASELINE:]
            held = np.concatenate([same] + [normal_rms[l] for l in NORMAL if l != load])
            res = evaluate_stream(base, held, fault, n_heldout_same_load=len(same))
            res.update({"fault": f"IR{dia.replace('0.', '')}", "diameter_in": dia, "load_hp": load, "file_id": fid,
                        "baseline_rms_g": float(np.median(base)), "fault_rms_g_median": float(np.median(fault))})
            rows.append(res)

    agg = aggregate(rows)
    result = {
        "dataset": "CWRU Bearing Data Center - 12 kHz drive-end, inner-race faults",
        "source_url": PAGE_URL,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": {
            "window_s": WINDOW_S, "baseline_windows": N_BASELINE, "feature": "window RMS of drive-end accelerometer (g)",
            "scaling": "baseline median rescaled to 2.0 mm/s nominal (accelerometer data are NOT velocity)",
            "detector": "AnomalyDetector defaults (not tuned on this data); other channels held at nominal",
        },
        "aggregate": agg, "streams": rows, "notes": notes,
        "limitations": [
            "Single sensor channel, one machine rig, one fault type (inner race): says nothing about thermal/cavitation modes.",
            "Window-level metrics: neighbouring windows are autocorrelated, so effective sample size is far below the window count.",
            "Fault windows are the entire fault recording; no labelled fault-onset transitions exist in this dataset.",
            "Absolute mm/s alarm limits are applied to RMS rescaled relative to baseline, not to calibrated velocity.",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))

    print(f"\n{'fault':8s} {'load':>4s} {'prec':>6s} {'recall':>7s} {'FPR':>6s} {'delay ms':>9s}")
    f = lambda v: "  n/a" if v is None else f"{v:6.3f}"  # noqa: E731
    for r in rows:
        print(f"{r['fault']:8s} {r['load_hp']:>4d} {f(r['precision'])} {f(r['recall'])}  {f(r['false_positive_rate'])} "
              f"{str(r['detection_delay_ms']):>9s}")
    print(f"\nAGGREGATE  precision={f(agg['precision'])} recall={f(agg['recall'])} F1={f(agg['f1'])} "
          f"FPR={f(agg['false_positive_rate'])}  (TP={agg['tp']} FP={agg['fp']} FN={agg['fn']} TN={agg['tn']})")
    print(f"False alarms on held-out NORMAL windows: same load as baseline {agg['fp_same_load']}/{agg['heldout_same_load']}"
          f", other motor loads {agg['fp_other_load']}/{agg['heldout_other_load']} (summed over streams)")
    print(f"Detected a fault in {agg['streams_with_detection']}/{agg['streams']} streams.  Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
