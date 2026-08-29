# ============================================================
#  文件后缀转换器 - Windows 桌面快捷方式创建脚本
#  用法：双击同目录下的「创建桌面快捷方式.bat」，
#        或右键本文件 -> 使用 PowerShell 运行
# ============================================================

$ErrorActionPreference = 'Stop'

# 1. 定位主程序（本脚本需与 file_extension_changer.py 放在同一目录）
$scriptPath = Join-Path $PSScriptRoot 'file_extension_changer.py'
if (-not (Test-Path $scriptPath)) {
    Write-Host '[错误] 未找到 file_extension_changer.py，请将本脚本与主程序放在同一目录后再运行。' -ForegroundColor Red
    Read-Host '按回车键退出'
    exit 1
}

# 2. 定位 Python：优先 pythonw.exe（启动时不显示黑色控制台窗口）
$pythonExe = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) {
    Write-Host '[错误] 未检测到 Python，请先安装 Python 3.8+：https://www.python.org/downloads/' -ForegroundColor Red
    Read-Host '按回车键退出'
    exit 1
}
$pythonw = Join-Path (Split-Path $pythonExe) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $pythonExe }

# 3. 在桌面创建快捷方式（覆盖旧的也无需担心，语义一致）
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop '文件后缀转换器.lnk'
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = '"' + $scriptPath + '"'
$lnk.WorkingDirectory = $PSScriptRoot
$lnk.Description = '文件后缀转换器（批量修改文件扩展名，双击即用）'
$lnk.Save()

Write-Host ('[完成] 已在桌面创建快捷方式：' + $lnkPath) -ForegroundColor Green
Write-Host '以后双击桌面上的「文件后缀转换器」图标即可打开工具。'
Read-Host '按回车键退出'
