"""utils/paths: frozen 感知与首运行引导。"""
import sys
from pathlib import Path

import pytest

import utils.paths as paths


def test_source_mode_app_root_is_project_root():
    assert not paths.is_frozen()
    assert paths.app_root() == Path(__file__).resolve().parent.parent
    assert paths.bundle_root() == paths.app_root()


def test_frozen_mode_roots(monkeypatch, tmp_path):
    exe = tmp_path / "dist" / "hlbot.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    meipass = tmp_path / "dist" / "_internal"
    meipass.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    assert paths.is_frozen()
    assert paths.app_root() == exe.parent
    assert paths.bundle_root() == meipass


def test_ensure_user_files_noop_in_source_mode():
    paths.ensure_user_files()  # 不应抛/不应退出


def test_ensure_user_files_copies_template_and_exits(monkeypatch, tmp_path):
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "config.example.yaml").write_text("system: {}\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "hlbot.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(paths, "APP_ROOT", exe_dir)
    with pytest.raises(SystemExit) as ei:
        paths.ensure_user_files()
    assert ei.value.code == 1
    assert (exe_dir / "config.yaml").read_text(encoding="utf-8") == "system: {}\n"


def test_ensure_user_files_existing_config_passes(monkeypatch, tmp_path):
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()
    (exe_dir / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "hlbot.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(exe_dir), raising=False)
    monkeypatch.setattr(paths, "APP_ROOT", exe_dir)
    paths.ensure_user_files()  # 有 config → 不退出


def test_bootstrap_seed_lands_real_config_env_db(monkeypatch, tmp_path):
    """个人自用构建: bundle 内 seed/ 的真实 config/.env/db 首启自动落地, 无提示。"""
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()
    bundle = tmp_path / "bundle"
    seed = bundle / "seed"
    (seed / "data").mkdir(parents=True)
    (seed / "config.yaml").write_text("real: 1\n", encoding="utf-8")
    (seed / ".env").write_text("OKX_API_KEY=real\n", encoding="utf-8")
    (seed / "data" / "trades.db").write_bytes(b"SQLITE_FAKE")
    (bundle / "config.example.yaml").write_text("tmpl: 1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "hlbot.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(paths, "APP_ROOT", exe_dir)

    assert paths.bootstrap_user_files() is None  # 无需用户处理
    assert (exe_dir / "config.yaml").read_text(encoding="utf-8") == "real: 1\n"
    assert (exe_dir / ".env").read_text(encoding="utf-8") == "OKX_API_KEY=real\n"
    assert (exe_dir / "data" / "trades.db").read_bytes() == b"SQLITE_FAKE"


def test_bootstrap_seed_never_overwrites_existing(monkeypatch, tmp_path):
    """exe 旁已有 config/db (老部署) → seed 不覆盖 (更新场景保用户数据)。"""
    exe_dir = tmp_path / "dist"
    (exe_dir / "data").mkdir(parents=True)
    (exe_dir / "config.yaml").write_text("mine: 1\n", encoding="utf-8")
    (exe_dir / "data" / "trades.db").write_bytes(b"MY_DB")
    bundle = tmp_path / "bundle"
    seed = bundle / "seed"
    (seed / "data").mkdir(parents=True)
    (seed / "config.yaml").write_text("seed: 1\n", encoding="utf-8")
    (seed / "data" / "trades.db").write_bytes(b"SEED_DB")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "hlbot.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(paths, "APP_ROOT", exe_dir)

    assert paths.bootstrap_user_files() is None
    assert (exe_dir / "config.yaml").read_text(encoding="utf-8") == "mine: 1\n"
    assert (exe_dir / "data" / "trades.db").read_bytes() == b"MY_DB"
