@echo off
rem HighLow Bot 构建脚本: 测试 -> 打包 -> 冒烟 -> 泄密断言 -> 版本化产物
setlocal enabledelayedexpansion
cd /d "%~dp0"

for /f "delims=" %%v in ('python -c "from version import __version__; print(__version__)"') do set VER=%%v
echo === HighLow Bot v%VER% ===

echo [1/5] pytest...
python -m pytest -q || goto :fail

echo [2/5] pyinstaller...
python -m PyInstaller hlbot.spec --noconfirm --clean || goto :fail

echo [3/5] smoke: hlbot-cli --help...
dist\hlbot\hlbot-cli.exe --help >nul || goto :fail

echo [4/5] 自用构建检查...
rem 个人自用: 真实 config/.env/db 打进 _internal\seed\, 首启自动落地。
if exist "dist\hlbot\_internal\seed\config.yaml" (
    echo   [!] 产物含真实 API key 与交易数据 ^(seed^), 仅限自用, 严禁外发!
) else (
    echo   产物不含真实配置 ^(通用构建^), 首启生成模板。
)

echo [5/5] 版本化产物目录...
if exist "dist\hlbot-v%VER%" rmdir /s /q "dist\hlbot-v%VER%"
move "dist\hlbot" "dist\hlbot-v%VER%" >nul || goto :fail

echo.
echo BUILD OK: dist\hlbot-v%VER%\  (hlbot.exe=GUI, hlbot-cli.exe=终端/子命令)
echo.
echo 部署 = 把新 exe + _internal 覆盖进 dist\hlbot-live\, data/logs/config/.env 一律不动。
echo        绝不首启 dist\hlbot-v%VER%\ —— 它自带的 seed 库是打包时的快照, 首启会顶掉正史。
echo        打包前记得: copy dist\hlbot-live\data\trades.db data\trades.db  (保 seed 最新)
echo 记得: git tag v%VER%
exit /b 0

:fail
echo BUILD FAILED
exit /b 1
