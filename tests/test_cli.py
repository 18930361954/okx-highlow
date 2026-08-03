"""CLI dispatch: 子命令表完整性 + argv 转发 + 默认走 main()。
同时兜住 hlbot.spec hiddenimports 漂移 (表里的模块必须真实可 import)。"""
import importlib
import sys
from unittest.mock import patch

import main as main_mod


def test_subcommand_modules_importable_with_main():
    for cmd, mod_name in main_mod._SUBCOMMANDS.items():
        mod = importlib.import_module(mod_name)
        assert callable(getattr(mod, "main", None)), f"{cmd} → {mod_name} 缺 main()"


def test_cli_no_args_runs_bot_main():
    with patch.object(main_mod, "main") as bot_main, \
         patch.object(sys, "argv", ["hlbot"]):
        main_mod.cli()
        bot_main.assert_called_once()


def test_cli_dispatches_and_rewrites_argv():
    captured = {}

    def fake_main():
        captured["argv"] = list(sys.argv)
        return 0

    fake_mod = type(sys)("fake_mod")
    fake_mod.main = fake_main
    with patch.object(sys, "argv", ["hlbot", "report", "--date", "2026-07-30"]), \
         patch.object(importlib, "import_module", return_value=fake_mod), \
         patch.object(main_mod, "ensure_user_files"):
        try:
            main_mod.cli()
        except SystemExit as e:
            assert e.code == 0
    assert captured["argv"] == ["hlbot report", "--date", "2026-07-30"]


def test_cli_unknown_subcommand_exits_2():
    with patch.object(sys, "argv", ["hlbot", "nonsense"]), \
         patch.object(main_mod, "ensure_user_files"):
        try:
            main_mod.cli()
            raise AssertionError("should exit")
        except SystemExit as e:
            assert e.code == 2


def test_cli_help_prints_usage(capsys):
    with patch.object(sys, "argv", ["hlbot", "--help"]), \
         patch.object(main_mod, "ensure_user_files"):
        main_mod.cli()
    out = capsys.readouterr().out
    assert "report" in out and "cleanup" in out
