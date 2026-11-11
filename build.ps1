$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& '.\.venv\Scripts\python.exe' -m PyInstaller --noconfirm --clean --onefile --windowed --collect-all customtkinter --icon 'assets\app-icon.ico' --add-data 'assets\app-icon.ico;assets' --add-data 'assets\app-icon.png;assets' --name '网络配置切换-可选择版' --distpath 'build\release' 'network_switcher.py'
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
Copy-Item -LiteralPath 'build\release\网络配置切换-可选择版.exe' -Destination 'dist\网络配置切换-可选择版.exe' -Force
