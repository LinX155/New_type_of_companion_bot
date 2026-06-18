@echo off
chcp 65001 >nul
title Companion Bot - 停止进程

echo ========================================
echo   Companion Bot 全量进程清理
echo   （端口 + 命令行 + 启动器窗口 + 子进程树）
echo ========================================
echo.

echo [1/2] 后端：端口 8000 + 所有 app.main 进程
echo   - 端口 8000 监听进程
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo       杀 PID %%a ^(含子进程^)
    taskkill /T /F /PID %%a >nul 2>&1
)
echo   - 命令行含 app.main:app / app\main.py 的残留进程（含其启动器 cmd 窗口）
powershell -NoProfile -Command "$ErrorActionPreference='SilentlyContinue'; $all = Get-CimInstance Win32_Process; $roots = New-Object System.Collections.Generic.List[object]; $srv = $all | Where-Object { ($_.CommandLine -like '*app.main:app*' -or $_.CommandLine -like '*app\main.py*' -or $_.CommandLine -like '*app/main.py*') -and $_.ProcessId -ne $PID -and $_.CommandLine -notlike '*Get-CimInstance*' }; foreach ($t in $srv) { $roots.Add($t.ProcessId); $cur = $t; for ($i=0; $i -lt 6; $i++) { $par = $all | Where-Object { $_.ProcessId -eq $cur.ParentProcessId } | Select-Object -First 1; if (-not $par) { break }; if ($par.Name -eq 'cmd.exe' -or $par.Name -eq 'WindowsTerminal.exe') { if (-not ($roots -contains $par.ProcessId)) { $roots.Add($par.ProcessId) }; break }; $cur = $par } }; if ($roots.Count -eq 0) { Write-Host '       (无残留)' } else { $roots | Sort-Object -Unique | ForEach-Object { Write-Host ('       杀 PID ' + $_); & taskkill.exe /T /F /PID $_ 2>$null | Out-Null } }"

echo.
echo [2/2] 前端：端口 5173 + 所有 vite 进程
echo   - 端口 5173 监听进程
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173" ^| findstr "LISTENING"') do (
    echo       杀 PID %%a ^(含子进程^)
    taskkill /T /F /PID %%a >nul 2>&1
)
echo   - 命令行含 vite 的残留进程（含其启动器窗口）
powershell -NoProfile -Command "$ErrorActionPreference='SilentlyContinue'; $all = Get-CimInstance Win32_Process; $roots = New-Object System.Collections.Generic.List[object]; $srv = $all | Where-Object { $_.CommandLine -like '*vite*' -and $_.ProcessId -ne $PID -and $_.CommandLine -notlike '*Get-CimInstance*' }; foreach ($t in $srv) { $roots.Add($t.ProcessId); $cur = $t; for ($i=0; $i -lt 6; $i++) { $par = $all | Where-Object { $_.ProcessId -eq $cur.ParentProcessId } | Select-Object -First 1; if (-not $par) { break }; if ($par.Name -eq 'cmd.exe' -or $par.Name -eq 'WindowsTerminal.exe') { if (-not ($roots -contains $par.ProcessId)) { $roots.Add($par.ProcessId) }; break }; $cur = $par } }; if ($roots.Count -eq 0) { Write-Host '       (无残留)' } else { $roots | Sort-Object -Unique | ForEach-Object { Write-Host ('       杀 PID ' + $_); & taskkill.exe /T /F /PID $_ 2>$null | Out-Null } }"

echo.
echo ========================================
echo   [复查] 端口 8000 / 5173
netstat -ano | findstr ":8000 :5173" | findstr "LISTENING" >nul && (echo   警告：仍有端口监听残留) || (echo   8000 / 5173 已全部清空)
echo ========================================
echo   清理完成，可关闭此窗口
echo ========================================
pause
