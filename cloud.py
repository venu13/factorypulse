"""Optional Google Cloud integrations: Cloud Storage artifacts and BigQuery analytics.

Everything here is behind environment flags and is failure-tolerant: no function raises into the UI.
Failures are collected as warnings, and the app keeps running on an in-memory ``LocalStore``.

    FACTORYPULSE_GCS_BUCKET   bucket name -> save inspection images and exported work orders
    FACTORYPULSE_BQ_DATASET   dataset (or project.dataset) -> log telemetry/anomalies/work orders
    FACTORYPULSE_BQ_LOCATION  dataset location (default US)
"""
from __future__ import annotations

import functools
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from llm import resolve_project

logger = logging.getLogger("factorypulse.cloud")

SCHEMAS: Dict[str, List[Tuple[str, str]]] = {
    "telemetry_snapshots": [
        ("snapshot_id", "STRING"), ("logged_at", "TIMESTAMP"), ("session_id", "STRING"), ("machine_id", "STRING"),
        ("injected_mode", "STRING"), ("fault_severity", "FLOAT"), ("status", "STRING"), ("health_score", "FLOAT"),
        ("suspected_mode", "STRING"), ("mode_confidence", "FLOAT"), ("vibration_rms", "FLOAT"),
        ("bearing_temp_c", "FLOAT"), ("motor_current_a", "FLOAT"), ("rpm", "FLOAT"), ("acoustic_db", "FLOAT"),
        ("hours_to_temp_critical", "FLOAT"),
    ],
    "anomaly_flags": [
        ("snapshot_id", "STRING"), ("logged_at", "TIMESTAMP"), ("session_id", "STRING"), ("machine_id", "STRING"),
        ("channel", "STRING"), ("severity", "STRING"), ("kind", "STRING"), ("message", "STRING"),
        ("z_score", "FLOAT"), ("first_seen", "TIMESTAMP"), ("fault_onset", "TIMESTAMP"), ("detect_lag_s", "FLOAT"),
    ],
    "work_orders": [
        ("logged_at", "TIMESTAMP"), ("session_id", "STRING"), ("incident_id", "STRING"), ("machine_id", "STRING"),
        ("criticality", "STRING"), ("failure_mode", "STRING"), ("rul_hours", "FLOAT"), ("loto_required", "BOOLEAN"),
        ("required_parts", "STRING"), ("remediation_steps", "STRING"), ("root_cause_analysis", "STRING"),
        ("provenance_kind", "STRING"), ("provenance", "STRING"), ("safety_notes", "STRING"), ("gcs_pdf_uri", "STRING"),
        ("gcs_json_uri", "STRING"),
    ],
}


def gcs_bucket() -> Optional[str]:
    return (os.getenv("FACTORYPULSE_GCS_BUCKET") or "").strip() or None


def bq_dataset() -> Optional[str]:
    return (os.getenv("FACTORYPULSE_BQ_DATASET") or "").strip() or None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: Any) -> Optional[str]:
    return None if ts is None or pd.isna(ts) else pd.Timestamp(ts).isoformat()


# --------------------------------------------------------------------------- #
# Cloud Storage
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=1)
def _gcs_client():
    from google.cloud import storage

    return storage.Client(project=resolve_project())


def save_to_gcs(blob_name: str, data: bytes, content_type: str, client: Any = None) -> Tuple[Optional[str], Optional[str]]:
    """Upload bytes to the configured bucket. Returns (gs:// uri, warning); never raises."""
    bucket_name = gcs_bucket()
    if not bucket_name:
        return None, None
    try:
        client = client or _gcs_client()
        client.bucket(bucket_name).blob(blob_name).upload_from_string(data, content_type=content_type)
        return f"gs://{bucket_name}/{blob_name}", None
    except Exception as exc:  # noqa: BLE001
        logger.warning("GCS upload failed: %s", exc)
        return None, f"Cloud Storage upload failed ({type(exc).__name__}: {exc})"


# --------------------------------------------------------------------------- #
# Row builders (identical for local and BigQuery stores)
# --------------------------------------------------------------------------- #
def build_snapshot_rows(session_id: str, machine_id: str, report: Dict[str, Any], injected_mode: str,
                        fault_severity: float, fault_onset: Optional[pd.Timestamp]) -> Tuple[List[dict], List[dict]]:
    """Return (telemetry_snapshot_rows, anomaly_flag_rows) for one machine's analysis."""
    snap_id, logged = uuid.uuid4().hex, _now()
    s = report["snapshot"]
    snap_row = {
        "snapshot_id": snap_id, "logged_at": logged.isoformat(), "session_id": session_id, "machine_id": machine_id,
        "injected_mode": injected_mode, "fault_severity": float(fault_severity), "status": report["status"],
        "health_score": float(report["health_score"]), "suspected_mode": report["suspected_mode"],
        "mode_confidence": float(report["mode_confidence"]),
        "vibration_rms": s["vibration_rms"]["recent_mean"], "bearing_temp_c": s["bearing_temp_c"]["recent_mean"],
        "motor_current_a": s["motor_current_a"]["recent_mean"], "rpm": s["rpm"]["recent_mean"],
        "acoustic_db": s["acoustic_db"]["recent_mean"],
        "hours_to_temp_critical": s["derived"]["hours_to_temp_critical"],
    }
    flags = []
    for f in report["findings"]:
        first = pd.Timestamp(f["first_seen"])
        lag = (first - fault_onset).total_seconds() if fault_onset is not None else None
        flags.append({
            "snapshot_id": snap_id, "logged_at": logged.isoformat(), "session_id": session_id,
            "machine_id": machine_id, "channel": f["channel"], "severity": f["severity"], "kind": f["kind"],
            "message": f["message"], "z_score": float(f["z_score"]), "first_seen": first.isoformat(),
            "fault_onset": _iso(fault_onset), "detect_lag_s": lag,
        })
    return [snap_row], flags


def build_work_order_row(session_id: str, wo: Any, prov: Dict[str, Any], gcs_pdf: Optional[str] = None,
                         gcs_json: Optional[str] = None) -> dict:
    import json

    return {
        "logged_at": _now().isoformat(), "session_id": session_id, "incident_id": wo.incident_id,
        "machine_id": wo.machine_id, "criticality": wo.criticality, "failure_mode": wo.failure_mode,
        "rul_hours": float(wo.estimated_remaining_useful_life_hours), "loto_required": bool(wo.loto_procedure_required),
        "required_parts": json.dumps(wo.required_parts), "remediation_steps": json.dumps(wo.remediation_steps),
        "root_cause_analysis": wo.root_cause_analysis, "provenance_kind": prov["kind"], "provenance": prov["label"],
        "safety_notes": json.dumps(prov.get("notes", [])), "gcs_pdf_uri": gcs_pdf, "gcs_json_uri": gcs_json,
    }


def _empty(table: str) -> pd.DataFrame:
    return pd.DataFrame(columns=[c for c, _ in SCHEMAS[table]])


def analytics_from_frames(frames: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """pandas equivalent of the three BigQuery analytics queries (same output columns)."""
    af = frames.get("anomaly_flags", _empty("anomaly_flags")).copy()
    wf = frames.get("work_orders", _empty("work_orders")).copy()

    if len(af):
        af["day"] = pd.to_datetime(af["logged_at"], utc=True).dt.date
        per_day = af.groupby(["day", "machine_id"]).size().reset_index(name="anomalies")
        lag = af[af["detect_lag_s"].notna() & (af["detect_lag_s"] >= 0)]
        per_snap = lag.groupby(["machine_id", "snapshot_id"])["detect_lag_s"].min().reset_index()
        mttd = (per_snap.groupby("machine_id")["detect_lag_s"].agg(mean_detect_lag_s="mean", n="count").reset_index())
    else:
        per_day = pd.DataFrame(columns=["day", "machine_id", "anomalies"])
        mttd = pd.DataFrame(columns=["machine_id", "mean_detect_lag_s", "n"])
    if len(wf):
        by_crit = (wf.groupby(["criticality", "provenance_kind"])
                   .agg(work_orders=("incident_id", "count"), avg_rul_hours=("rul_hours", "mean")).reset_index())
    else:
        by_crit = pd.DataFrame(columns=["criticality", "provenance_kind", "work_orders", "avg_rul_hours"])
    return {"anomalies_per_day": per_day, "mean_time_to_detect": mttd, "work_orders_by_criticality": by_crit}


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #
class LocalStore:
    """In-memory fallback with the same interface and tables as BigQuery."""

    label = "local mode (in-memory, not persisted)"
    is_bigquery = False

    def __init__(self) -> None:
        self.tables: Dict[str, List[dict]] = {t: [] for t in SCHEMAS}
        self.warnings: List[str] = []

    def insert(self, table: str, rows: List[dict]) -> None:
        self.tables[table].extend(rows)

    def frames(self) -> Dict[str, pd.DataFrame]:
        return {t: (pd.DataFrame(r) if r else _empty(t)) for t, r in self.tables.items()}

    def analytics(self) -> Dict[str, pd.DataFrame]:
        return analytics_from_frames(self.frames())

    def pop_warnings(self) -> List[str]:
        w, self.warnings = self.warnings, []
        return w


class BigQueryStore:
    """Logs to BigQuery with load jobs (works on the free sandbox, which blocks streaming inserts/DML)."""

    is_bigquery = True

    def __init__(self, dataset: str, client: Any = None, location: Optional[str] = None) -> None:
        self.location = location or os.getenv("FACTORYPULSE_BQ_LOCATION", "US")
        self._client = client
        self.project = (dataset.split(".")[0] if "." in dataset else None) or resolve_project()
        self.dataset_id = dataset.split(".")[-1]
        self.warnings: List[str] = []
        self._ready = False

    @property
    def label(self) -> str:
        return f"BigQuery ({self.project}.{self.dataset_id})"

    @property
    def client(self):
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self.project, location=self.location)
        return self._client

    def _table(self, name: str) -> str:
        return f"{self.project}.{self.dataset_id}.{name}"

    def ensure_ready(self) -> None:
        """Create the dataset and tables on first use (idempotent)."""
        if self._ready:
            return
        from google.cloud import bigquery

        ds = bigquery.Dataset(f"{self.project}.{self.dataset_id}")
        ds.location = self.location
        self.client.create_dataset(ds, exists_ok=True)
        for name, cols in SCHEMAS.items():
            table = bigquery.Table(self._table(name), schema=[bigquery.SchemaField(c, t) for c, t in cols])
            self.client.create_table(table, exists_ok=True)
        self._ready = True

    def insert(self, table: str, rows: List[dict]) -> None:
        if not rows:
            return
        try:
            from google.cloud import bigquery

            self.ensure_ready()
            cfg = bigquery.LoadJobConfig(
                schema=[bigquery.SchemaField(c, t) for c, t in SCHEMAS[table]],
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            )
            self.client.load_table_from_json(rows, self._table(table), job_config=cfg).result(timeout=60)
        except Exception as exc:  # noqa: BLE001
            logger.warning("BigQuery insert into %s failed: %s", table, exc)
            self.warnings.append(f"BigQuery logging to '{table}' failed ({type(exc).__name__}: {exc})")

    def _query(self, sql: str) -> pd.DataFrame:
        rows = self.client.query(sql, location=self.location).result(timeout=60)
        return pd.DataFrame([dict(r.items()) for r in rows])

    def analytics(self) -> Dict[str, pd.DataFrame]:
        """Run the three analytics queries. Raises on failure (caller shows a warning + local fallback)."""
        self.ensure_ready()
        t = {n: f"`{self._table(n)}`" for n in SCHEMAS}
        return {
            "anomalies_per_day": self._query(
                f"SELECT DATE(logged_at) AS day, machine_id, COUNT(*) AS anomalies FROM {t['anomaly_flags']} "
                "GROUP BY day, machine_id ORDER BY day, machine_id"),
            "mean_time_to_detect": self._query(
                "SELECT machine_id, AVG(min_lag) AS mean_detect_lag_s, COUNT(*) AS n FROM ("
                f"SELECT machine_id, snapshot_id, MIN(detect_lag_s) AS min_lag FROM {t['anomaly_flags']} "
                "WHERE detect_lag_s IS NOT NULL AND detect_lag_s >= 0 GROUP BY machine_id, snapshot_id) "
                "GROUP BY machine_id ORDER BY machine_id"),
            "work_orders_by_criticality": self._query(
                "SELECT criticality, provenance_kind, COUNT(*) AS work_orders, AVG(rul_hours) AS avg_rul_hours "
                f"FROM {t['work_orders']} GROUP BY criticality, provenance_kind ORDER BY criticality"),
        }

    def pop_warnings(self) -> List[str]:
        w, self.warnings = self.warnings, []
        return w


def make_store() -> Any:
    """BigQueryStore when FACTORYPULSE_BQ_DATASET is set, otherwise a LocalStore."""
    ds = bq_dataset()
    return BigQueryStore(ds) if ds else LocalStore()
