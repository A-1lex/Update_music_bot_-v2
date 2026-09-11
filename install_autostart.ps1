$ErrorActionPreference = 'Stop'
$botRoot = $PSScriptRoot
$pythonWindowless = Join-Path $botRoot '.venv\Scripts\pythonw.exe'
$pythonConsole = Join-Path $botRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonWindowless)) { throw 'pythonw.exe not found in .venv' }
& $pythonConsole -c 'import psutil, aiogram'
if ($LASTEXITCODE -ne 0) { throw 'Fix the project Python environment before enabling autostart.' }
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument ('"' + (Join-Path $botRoot 'supervisor.py') + '"') -WorkingDirectory $botRoot
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# No -Force: preserve any existing task with this name.
Register-ScheduledTask -TaskName 'UpdateMusikBotSupervisor' -Action $action -Trigger $trigger -Principal $principal -Settings $settings
Write-Output 'Autostart installed for the next Windows sign-in. The bot was not started.'
