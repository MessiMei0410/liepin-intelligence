from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from _local import env_path, fixture_base_db, require_local

import pytest
from fastapi.testclient import TestClient

import asa_core.app as asa_app
from asa_core.app import create_app


SOURCE_DB = env_path("ASA_SOURCE_DB", Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"))


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # 与 test_asa_core_v1 同一约定：正式库只读复制到临时副本，create_app 的 migrate 只动副本。
    target = tmp_path_factory.mktemp("asa-app-version") / "asa-app-version.db"
    require_local(SOURCE_DB, "正式库 talent_system_v3")
    source = sqlite3.connect(fixture_base_db())
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _make_dist(tmp_path: Path, build_id: str | None = "test-build-123") -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>ASA</title>", encoding="utf-8")
    if build_id is not None:
        (dist / "build.json").write_text(json.dumps({"build_id": build_id}), encoding="utf-8")
    return dist


def test_app_version_returns_dist_build_id(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asa_app, "ASA_WEB_DIST", _make_dist(tmp_path))
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = client.get("/api/v1/app-version")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "build_id": "test-build-123"}


def test_app_version_without_build_file_reports_null(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 旧 dist（本次改动前构建的）没有 build.json：返回 null，前端视为无信息不提示。
    monkeypatch.setattr(asa_app, "ASA_WEB_DIST", _make_dist(tmp_path, build_id=None))
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = client.get("/api/v1/app-version")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "build_id": None}


def test_app_version_missing_dist_is_503(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asa_app, "ASA_WEB_DIST", tmp_path / "no-such-dist")
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = client.get("/api/v1/app-version")
        assert response.status_code == 503
        body = response.json()
        assert body["ok"] is False
        assert body["build_id"] is None


def test_app_version_corrupt_build_file_reports_null(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = _make_dist(tmp_path)
    (dist / "build.json").write_text("not-json{", encoding="utf-8")
    monkeypatch.setattr(asa_app, "ASA_WEB_DIST", dist)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = client.get("/api/v1/app-version")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "build_id": None}
