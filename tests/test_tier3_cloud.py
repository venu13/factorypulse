"""GCS / BigQuery integrations: fakes only, no network. Verifies logic, never live behaviour."""
import json

import pandas as pd
import pytest

import cloud
from copilot import generate_work_order
from llm import Provenance
from telemetry import FaultMode, analyze_window, generate_telemetry


def _report(mode=FaultMode.BEARING_INNER_RACE, seed=3):
    df = generate_telemetry(mode, seed=seed)
    onset = df.loc[df["fault_label"], "timestamp"].min() if df["fault_label"].any() else None
    return df, analyze_window(df)[1].to_dict(), onset


def test_flags_unset_means_local_store(monkeypatch):
    monkeypatch.delenv("FACTORYPULSE_BQ_DATASET", raising=False)
    monkeypatch.delenv("FACTORYPULSE_GCS_BUCKET", raising=False)
    assert isinstance(cloud.make_store(), cloud.LocalStore)
    assert cloud.save_to_gcs("a.txt", b"x", "text/plain") == (None, None)


def test_local_store_analytics_roundtrip():
    store = cloud.LocalStore()
    for mode in (FaultMode.BEARING_INNER_RACE, FaultMode.NORMAL):
        df, rep, onset = _report(mode)
        snaps, flags = cloud.build_snapshot_rows("s1", f"M-{mode.value}", rep, mode.value, 1.0, onset)
        store.insert("telemetry_snapshots", snaps)
        store.insert("anomaly_flags", flags)
    wo, prov = generate_work_order("M-bearing_inner_race", _report()[1])
    store.insert("work_orders", [cloud.build_work_order_row("s1", wo, prov.to_dict(), "gs://b/x.pdf", None)])
    a = store.analytics()
    assert set(a) == {"anomalies_per_day", "mean_time_to_detect", "work_orders_by_criticality"}
    assert a["anomalies_per_day"]["anomalies"].sum() == len(store.tables["anomaly_flags"]) > 0
    mttd = a["mean_time_to_detect"]
    assert list(mttd["machine_id"]) == ["M-bearing_inner_race"] and mttd["mean_detect_lag_s"].iloc[0] >= 0
    wbc = a["work_orders_by_criticality"]
    assert wbc["work_orders"].sum() == 1 and wbc["provenance_kind"].iloc[0] == "mock"
    assert store.tables["work_orders"][0]["gcs_pdf_uri"] == "gs://b/x.pdf"


def test_empty_local_analytics_do_not_crash():
    a = cloud.LocalStore().analytics()
    assert all(len(df) == 0 for df in a.values())


def test_row_columns_match_schema():
    _, rep, onset = _report()
    snaps, flags = cloud.build_snapshot_rows("s", "M", rep, "bearing_inner_race", 1.0, onset)
    assert set(snaps[0]) == {c for c, _ in cloud.SCHEMAS["telemetry_snapshots"]}
    assert set(flags[0]) == {c for c, _ in cloud.SCHEMAS["anomaly_flags"]}
    wo, prov = generate_work_order("M", rep)
    assert set(cloud.build_work_order_row("s", wo, prov.to_dict())) == {c for c, _ in cloud.SCHEMAS["work_orders"]}
    json.dumps(snaps + flags)  # must be JSON-serialisable for load_table_from_json


class FakeBlob:
    def __init__(self, sink, name): self.sink, self.name = sink, name
    def upload_from_string(self, data, content_type=None): self.sink[self.name] = (data, content_type)


class FakeBucket:
    def __init__(self, sink): self.sink = sink
    def blob(self, name): return FakeBlob(self.sink, name)


class FakeStorage:
    def __init__(self, fail=False): self.sink, self.fail = {}, fail
    def bucket(self, name):
        if self.fail:
            raise PermissionError("403 caller does not have storage.objects.create")
        return FakeBucket(self.sink)


def test_gcs_upload_returns_gs_uri(monkeypatch):
    monkeypatch.setenv("FACTORYPULSE_GCS_BUCKET", "my-bucket")
    fake = FakeStorage()
    uri, warn = cloud.save_to_gcs("work_orders/INC-1.json", b"{}", "application/json", client=fake)
    assert uri == "gs://my-bucket/work_orders/INC-1.json" and warn is None
    assert fake.sink["work_orders/INC-1.json"] == (b"{}", "application/json")


def test_gcs_failure_is_a_warning_not_an_exception(monkeypatch):
    monkeypatch.setenv("FACTORYPULSE_GCS_BUCKET", "my-bucket")
    uri, warn = cloud.save_to_gcs("x", b"1", "text/plain", client=FakeStorage(fail=True))
    assert uri is None and "PermissionError" in warn and "403" in warn


class FakeBQ:
    def __init__(self, fail_load=False):
        self.loads, self.created, self.queries, self.fail_load = [], [], [], fail_load

    def create_dataset(self, ds, exists_ok=False): self.created.append(("dataset", ds.dataset_id))
    def create_table(self, t, exists_ok=False): self.created.append(("table", t.table_id))

    def load_table_from_json(self, rows, table, job_config=None):
        if self.fail_load:
            raise RuntimeError("quota exceeded")
        self.loads.append((table, rows))
        class _Job:
            def result(self, timeout=None): return None
        return _Job()

    def query(self, sql, location=None):
        self.queries.append(sql)
        class _R:
            def result(self, timeout=None): return []
        return _R()


def test_bigquery_store_creates_dataset_tables_and_loads():
    client = FakeBQ()
    store = cloud.BigQueryStore("proj.factorypulse", client=client, location="US")
    _, rep, onset = _report()
    snaps, flags = cloud.build_snapshot_rows("s", "M", rep, "bearing_inner_race", 1.0, onset)
    store.insert("telemetry_snapshots", snaps)
    store.insert("anomaly_flags", flags)
    assert ("dataset", "factorypulse") in client.created
    assert {n for k, n in client.created if k == "table"} == set(cloud.SCHEMAS)
    assert [t for t, _ in client.loads] == ["proj.factorypulse.telemetry_snapshots", "proj.factorypulse.anomaly_flags"]
    assert store.warnings == []


def test_bigquery_failure_collected_as_warning():
    store = cloud.BigQueryStore("proj.ds", client=FakeBQ(fail_load=True))
    store.insert("work_orders", [{"x": 1}])
    w = store.pop_warnings()
    assert len(w) == 1 and "quota exceeded" in w[0] and store.warnings == []


def test_bigquery_analytics_issues_three_queries():
    client = FakeBQ()
    store = cloud.BigQueryStore("proj.ds", client=client)
    out = store.analytics()
    assert len(client.queries) == 3 and set(out) == {"anomalies_per_day", "mean_time_to_detect", "work_orders_by_criticality"}
    assert "`proj.ds.anomaly_flags`" in client.queries[0] and "GROUP BY" in client.queries[0]
    assert "`proj.ds.work_orders`" in client.queries[2]
