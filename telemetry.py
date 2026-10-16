"""FactoryPulse telemetry synthesis and anomaly detection.

Simulates a 55 kW centrifugal pump / motor train and provides a statistical
anomaly detector combining ISO-10816-style absolute limits, baseline z-scores,
and short-window volatility ratios.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

CHANNELS: List[str] = [
    "vibration_rms",
    "bearing_temp_c",
    "motor_current_a",
    "rpm",
    "acoustic_db",
]

CHANNEL_META: Dict[str, Dict[str, str]] = {
    "vibration_rms": {"label": "Vibration RMS", "unit": "mm/s"},
    "bearing_temp_c": {"label": "Bearing Temp", "unit": "°C"},
    "motor_current_a": {"label": "Motor Current", "unit": "A"},
    "rpm": {"label": "Shaft Speed", "unit": "RPM"},
    "acoustic_db": {"label": "Acoustic Level", "unit": "dB"},
}

NOMINAL: Dict[str, float] = {
    "vibration_rms": 2.0,
    "bearing_temp_c": 62.0,
    "motor_current_a": 42.0,
    "rpm": 2980.0,
    "acoustic_db": 68.0,
}

# Absolute alarm limits (vibration follows ISO 10816-3 zones C/D for Group 2).
LIMITS: Dict[str, Dict[str, float]] = {
    "vibration_rms": {"warn": 4.5, "crit": 7.1},
    "bearing_temp_c": {"warn": 80.0, "crit": 95.0},
    "motor_current_a": {"warn": 50.0, "crit": 58.0},
    "acoustic_db": {"warn": 78.0, "crit": 88.0},
}
RPM_DEVIATION_PCT = {"warn": 1.0, "crit": 3.0}

# Minimum credible sensor noise (1-sigma) per channel; stops a quiet commissioning
# window from producing an over-tight baseline and false alarms.
SENSOR_NOISE_FLOOR: Dict[str, float] = {
    "vibration_rms": 0.11,
    "bearing_temp_c": 0.6,
    "motor_current_a": 0.5,
    "rpm": 5.0,
    "acoustic_db": 0.7,
}


class FaultMode(str, Enum):
    NORMAL = "normal"
    BEARING_INNER_RACE = "bearing_inner_race"
    THERMAL_OVERLOAD = "thermal_overload"
    CAVITATION = "cavitation"

    @property
    def label(self) -> str:
        return {
            FaultMode.NORMAL: "Normal operation",
            FaultMode.BEARING_INNER_RACE: "Inner race bearing defect",
            FaultMode.THERMAL_OVERLOAD: "Motor thermal overload",
            FaultMode.CAVITATION: "Hydraulic cavitation",
        }[self]


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #
def _ar1(rng: np.random.Generator, n: int, sigma: float, phi: float = 0.6) -> np.ndarray:
    """Zero-mean AR(1) noise with stationary standard deviation ``sigma``."""
    eps = rng.normal(0.0, sigma * math.sqrt(1.0 - phi**2), n)
    out = np.empty(n)
    out[0] = rng.normal(0.0, sigma)
    for i in range(1, n):
        out[i] = phi * out[i - 1] + eps[i]
    return out


def generate_telemetry(
    mode: FaultMode | str = FaultMode.NORMAL,
    n_samples: int = 720,
    sample_interval_s: int = 5,
    fault_onset: float = 0.45,
    severity: float = 1.0,
    seed: int = 42,
    end_time: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """Generate a multichannel telemetry window.

    Args:
        mode: fault scenario to inject.
        n_samples: number of samples in the window.
        sample_interval_s: seconds between samples.
        fault_onset: fraction of the window at which the fault begins.
        severity: fault intensity multiplier (0.2 subtle .. 2.0 severe).
        seed: RNG seed for reproducibility.
        end_time: timestamp of the last sample (defaults to now, floored to seconds).
    """
    mode = FaultMode(mode)
    rng = np.random.default_rng(seed)
    n = int(n_samples)
    idx = np.arange(n)
    s = float(severity)

    onset_idx = int(n * fault_onset)
    if mode is FaultMode.NORMAL:
        ramp = np.zeros(n)
    else:
        ramp = np.clip((idx - onset_idx) / max(n - onset_idx - 1, 1), 0.0, 1.0)

    load = np.sin(2 * np.pi * idx / 240.0)

    vib = NOMINAL["vibration_rms"] + _ar1(rng, n, 0.12) + 0.08 * load
    temp = NOMINAL["bearing_temp_c"] + 1.5 * load + _ar1(rng, n, 0.5, 0.9)
    cur = NOMINAL["motor_current_a"] + 1.2 * load + _ar1(rng, n, 0.5)
    rpm = NOMINAL["rpm"] - 4.0 * load + rng.normal(0, 5.0, n)
    ac = NOMINAL["acoustic_db"] + 0.6 * load + _ar1(rng, n, 0.7)

    if mode is FaultMode.BEARING_INNER_RACE:
        spike_prob = np.where(ramp > 0, 0.04 + 0.30 * ramp, 0.0)
        spikes = (rng.random(n) < spike_prob) * rng.exponential(1.0, n) * (1.2 + 2.8 * ramp) * s
        vib += s * 3.8 * ramp**1.3 + spikes
        temp += s * 9.0 * ramp
        cur += s * 1.5 * ramp
        ac += s * 7.0 * ramp + 0.8 * spikes

    elif mode is FaultMode.THERMAL_OVERLOAD:
        k = 3.5
        expo = (np.exp(k * ramp) - 1.0) / (np.exp(k) - 1.0)
        temp += s * 58.0 * expo
        cur += s * 14.0 * expo
        rpm -= s * 45.0 * expo
        vib += s * 0.8 * expo
        ac += s * 2.0 * expo

    elif mode is FaultMode.CAVITATION:
        cur += (
            rng.normal(0, 1, n) * s * 3.5 * ramp
            + s * 3.2 * ramp * np.sin(2 * np.pi * idx / 7.3)
            - s * 3.0 * ramp
        )
        ac += (
            s * 9.0 * ramp
            + s * 4.0 * ramp * np.sin(2 * np.pi * idx / 5.1)
            + rng.normal(0, 1, n) * 1.5 * s * ramp
        )
        vib += s * 1.8 * ramp + np.abs(rng.normal(0, 1, n)) * 0.6 * s * ramp
        rpm += rng.normal(0, 1, n) * 25.0 * s * ramp

    end = end_time if end_time is not None else pd.Timestamp.now().floor("s")
    ts = pd.date_range(end=end, periods=n, freq=f"{int(sample_interval_s)}s")

    df = pd.DataFrame(
        {
            "timestamp": ts,
            "vibration_rms": np.clip(vib, 0.1, None),
            "bearing_temp_c": np.clip(temp, 20.0, None),
            "motor_current_a": np.clip(cur, 0.0, None),
            "rpm": np.clip(rpm, 0.0, None),
            "acoustic_db": np.clip(ac, 30.0, None),
        }
    )
    df["fault_label"] = (idx >= onset_idx) & (mode is not FaultMode.NORMAL)
    df["mode"] = mode.value
    return df


# --------------------------------------------------------------------------- #
# Anomaly detection
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    channel: str
    severity: str  # WARNING | CRITICAL
    kind: str  # threshold | drift | volatility
    message: str
    value: float
    z_score: float
    first_seen: str


@dataclass
class AnomalyReport:
    status: str  # HEALTHY | WARNING | CRITICAL
    health_score: float
    suspected_mode: str
    mode_confidence: float
    mode_scores: Dict[str, float]
    findings: List[Finding] = field(default_factory=list)
    snapshot: Dict[str, Any] = field(default_factory=dict)
    window_end: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clip01(x: float) -> float:
    return float(min(max(x, 0.0), 1.0))


class AnomalyDetector:
    """Threshold + z-score + volatility anomaly detector on a healthy baseline."""

    def __init__(
        self,
        baseline_n: int = 120,
        z_threshold: float = 3.5,
        persist_k: int = 3,
        persist_window: int = 5,
        vol_window: int = 12,
        vol_ratio_threshold: float = 3.0,
        recent_n: int = 24,
    ) -> None:
        self.baseline_n = baseline_n
        self.z_threshold = z_threshold
        self.persist_k = persist_k
        self.persist_window = persist_window
        self.vol_window = vol_window
        self.vol_ratio_threshold = vol_ratio_threshold
        self.recent_n = recent_n

    # -- per-sample scoring ------------------------------------------------- #
    def _baseline(self, df: pd.DataFrame) -> Dict[str, Tuple[float, float]]:
        n_base = max(10, min(self.baseline_n, int(len(df) * 0.25)))
        base = df.iloc[:n_base]
        out = {}
        for ch in CHANNELS:
            mu = float(base[ch].mean())
            sd = float(max(base[ch].std(ddof=1), SENSOR_NOISE_FLOOR[ch]))
            out[ch] = (mu, sd)
        return out

    @staticmethod
    def _level(value: pd.Series, warn: float, crit: float) -> pd.Series:
        return (value >= warn).astype(int) + (value >= crit).astype(int)

    def score(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return per-sample z-scores, threshold levels, volatility ratios and flags."""
        base = self._baseline(df)
        out = pd.DataFrame({"timestamp": df["timestamp"].values})
        flag_cols = []
        for ch in CHANNELS:
            mu, sd = base[ch]
            z = (df[ch] - mu) / sd
            out[f"{ch}_z"] = z.values

            if ch == "rpm":
                dev = (df[ch] - mu).abs() / mu * 100.0
                level = self._level(dev, RPM_DEVIATION_PCT["warn"], RPM_DEVIATION_PCT["crit"])
            else:
                level = self._level(df[ch], LIMITS[ch]["warn"], LIMITS[ch]["crit"])
            out[f"{ch}_level"] = level.values

            vol = df[ch].rolling(self.vol_window, min_periods=self.vol_window).std() / sd
            out[f"{ch}_vol"] = vol.fillna(0.0).values

            z_flag = (z.abs() > self.z_threshold).astype(int)
            z_persist = z_flag.rolling(self.persist_window, min_periods=1).sum() >= self.persist_k
            v_flag = (vol.fillna(0.0) > self.vol_ratio_threshold).astype(int)
            v_persist = v_flag.rolling(self.persist_window, min_periods=1).sum() >= self.persist_k
            flag = (level > 0) | z_persist | v_persist
            out[f"{ch}_flag"] = flag.values
            flag_cols.append(f"{ch}_flag")

        out["anomaly"] = out[flag_cols].any(axis=1)
        out["anomaly_score"] = out[[f"{ch}_z" for ch in CHANNELS]].abs().max(axis=1)
        return out

    # -- window-level analysis --------------------------------------------- #
    def analyze(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, AnomalyReport]:
        det = self.score(df)
        base = self._baseline(df)
        recent = slice(max(len(df) - self.recent_n, 0), len(df))
        findings: List[Finding] = []
        snapshot: Dict[str, Any] = {}

        for ch in CHANNELS:
            mu, sd = base[ch]
            rdf = df[ch].iloc[recent]
            z_recent = float((rdf.mean() - mu) / sd)
            level_max = int(det[f"{ch}_level"].iloc[recent].max())
            vol_max = float(det[f"{ch}_vol"].iloc[recent].max())
            snapshot[ch] = {
                "unit": CHANNEL_META[ch]["unit"],
                "latest": round(float(df[ch].iloc[-1]), 3),
                "recent_mean": round(float(rdf.mean()), 3),
                "recent_max": round(float(rdf.max()), 3),
                "baseline_mean": round(mu, 3),
                "baseline_std": round(sd, 4),
                "z_score": round(z_recent, 2),
                "delta_pct": round((float(rdf.mean()) - mu) / mu * 100.0, 2),
                "volatility_ratio": round(vol_max, 2),
            }

            flags = det[f"{ch}_flag"].values
            if not flags[recent].any():
                continue
            j = int(np.where(flags[recent])[0][-1]) + recent.start
            i = j
            while i > 0 and flags[i - 1]:
                i -= 1
            first_seen = pd.Timestamp(df["timestamp"].iloc[i]).isoformat()
            label = CHANNEL_META[ch]["label"]
            unit = CHANNEL_META[ch]["unit"]

            if level_max > 0:
                kind = "threshold"
                limit_name = "critical" if level_max == 2 else "warning"
                msg = (
                    f"{label} reached {rdf.max():.2f} {unit} "
                    f"(recent mean {rdf.mean():.2f}), breaching the {limit_name} limit."
                    if ch != "rpm"
                    else f"{label} deviates {abs(snapshot[ch]['delta_pct']):.2f}% from baseline "
                    f"({rdf.mean():.0f} vs {mu:.0f} {unit}), breaching the {limit_name} band."
                )
            elif abs(z_recent) > self.z_threshold:
                kind = "drift"
                msg = f"{label} drifting {z_recent:+.1f}σ from healthy baseline ({mu:.2f} {unit})."
            else:
                kind = "volatility"
                msg = (
                    f"{label} erratic: short-window variance is {vol_max:.1f}× baseline "
                    f"(baseline σ={sd:.2f} {unit})."
                )
            findings.append(
                Finding(
                    channel=ch,
                    severity="CRITICAL" if level_max == 2 else "WARNING",
                    kind=kind,
                    message=msg,
                    value=round(float(rdf.max()), 3),
                    z_score=round(z_recent, 2),
                    first_seen=first_seen,
                )
            )

        # Derived diagnostics -------------------------------------------------
        tail = df.iloc[-60:]
        hours = (tail["timestamp"] - tail["timestamp"].iloc[0]).dt.total_seconds().values / 3600.0
        slope = float(np.polyfit(hours, tail["bearing_temp_c"].values, 1)[0]) if len(tail) > 3 else 0.0
        t_last = float(df["bearing_temp_c"].iloc[-1])
        crit_t = LIMITS["bearing_temp_c"]["crit"]
        if t_last >= crit_t:
            hours_to_crit: Optional[float] = 0.0
        elif slope > 0.5:
            hours_to_crit = round((crit_t - t_last) / slope, 2)
        else:
            hours_to_crit = None
        vib_tail = df["vibration_rms"].iloc[-60:]
        peak_ratio = float(vib_tail.max() / max(vib_tail.median(), 1e-6))
        snapshot["derived"] = {
            "bearing_temp_slope_c_per_hr": round(slope, 2),
            "hours_to_temp_critical": hours_to_crit,
            "vibration_peak_to_median": round(peak_ratio, 2),
        }

        mode_scores = self._classify(snapshot, peak_ratio, slope)
        best_mode, best = max(mode_scores.items(), key=lambda kv: kv[1])
        has_findings = bool(findings)
        suspected = best_mode if (best >= 0.35 and has_findings) else FaultMode.NORMAL.value
        confidence = best if suspected != FaultMode.NORMAL.value else _clip01(1.0 - best)

        penalty = sum(25.0 if f.severity == "CRITICAL" else 10.0 for f in findings)
        health = float(max(0.0, 100.0 - penalty))
        if any(f.severity == "CRITICAL" for f in findings):
            status = "CRITICAL"
        elif findings:
            status = "WARNING"
        else:
            status = "HEALTHY"

        report = AnomalyReport(
            status=status,
            health_score=round(health, 1),
            suspected_mode=suspected,
            mode_confidence=round(confidence, 2),
            mode_scores={k: round(v, 2) for k, v in mode_scores.items()},
            findings=findings,
            snapshot=snapshot,
            window_end=pd.Timestamp(df["timestamp"].iloc[-1]).isoformat(),
        )
        return det, report

    @staticmethod
    def _classify(snap: Dict[str, Any], peak_ratio: float, temp_slope: float) -> Dict[str, float]:
        z = {ch: abs(snap[ch]["z_score"]) for ch in CHANNELS}
        vol = {ch: snap[ch]["volatility_ratio"] for ch in CHANNELS}
        cur_delta = snap["motor_current_a"]["delta_pct"]
        rpm_delta = snap["rpm"]["delta_pct"]

        bearing = 0.6 * _clip01(z["vibration_rms"] / 6.0) + 0.4 * _clip01((peak_ratio - 1.5) / 2.0)
        thermal = (
            0.5 * _clip01(z["bearing_temp_c"] / 10.0)
            + 0.3 * _clip01(cur_delta / 20.0)
            + 0.2 * _clip01(-rpm_delta / 1.2)
        )
        cavitation = (
            0.5 * _clip01((vol["motor_current_a"] - 1.5) / 3.5)
            + 0.3 * _clip01(z["acoustic_db"] / 8.0)
            + 0.2 * _clip01((vol["rpm"] - 1.5) / 3.0)
        )
        # Erratic current/acoustics are a hydraulic signature: damp the bearing hypothesis,
        # whose hallmark is impulsive vibration with a *stable* current draw.
        bearing *= 1.0 - 0.7 * _clip01((vol["motor_current_a"] - 1.5) / 3.5)
        # Temperature-driven faults dominate only when current rises.
        if cur_delta < 5.0:
            thermal *= 0.5
        return {
            FaultMode.BEARING_INNER_RACE.value: bearing,
            FaultMode.THERMAL_OVERLOAD.value: thermal,
            FaultMode.CAVITATION.value: cavitation,
        }


def analyze_window(df: pd.DataFrame) -> Tuple[pd.DataFrame, AnomalyReport]:
    """Convenience wrapper using default detector settings."""
    return AnomalyDetector().analyze(df)
