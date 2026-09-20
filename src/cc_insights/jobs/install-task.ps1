<#
.SYNOPSIS
    Install (or remove) the CC-Insights background ingest job on Windows.

.DESCRIPTION
    The Windows counterpart of scripts/install-launchd.sh, and it keeps the
    same contract: run `cci init && cci ingest && cci derive && cci sync auto`
    every 15 minutes, once at
    logon, logging to the config directory. That cadence is the whole point of
    the project -- agent log directories are pruned on a rolling basis, so
    history that is not captured is lost permanently.

    It registers one scheduled task under the current user, writes nothing
    outside the CC-Insights config directory, and reads your agent logs
    read-only. No elevation is required: the task runs as you, which is also
    the only account that can see your logs.

    There is no watch task. `cci install --watch` is refused on Windows and
    says so: Task Scheduler starts processes but does not supervise them, so
    a long-running `cci watch` registered here would stay dead after its
    first exit with nothing to report it. See `_watch_is_macos_only` in
    scheduler.py. The 15-minute job below captures the same history.

.PARAMETER Uninstall
    Stop and unregister the task instead of installing it.

.PARAMETER IntervalMinutes
    How often to run. Defaults to 15, matching the launchd job.

.PARAMETER ConfigDir
    The CC-Insights config directory the task should read and write. `cci
    install` passes the directory it was itself run against, so the task
    fills the database the CLI reads. Defaults to the same place
    config.default_config_dir() would pick.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [int]$IntervalMinutes = 15,
    [string]$ConfigDir = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Exactly `scheduler.LABEL`, and that is a contract, not a coincidence:
# `_status_task` asks Get-ScheduledTask for this name, so a prettier one here
# meant every install was followed by `cci doctor` reporting "not installed"
# and `scheduler.active()` returning None for good. Nothing compared the two
# spellings until tests/test_scripts.py did.
$TaskName = 'com.cc-insights'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed $TaskName"
    } else {
        Write-Host "$TaskName was not installed"
    }
    exit 0
}

# %APPDATA%\cc-insights is where config.default_config_dir() puts things on
# Windows; CC_INSIGHTS_HOME overrides it there and must override it here too,
# or the task would log somewhere the CLI never looks. -ConfigDir wins over
# both: `cci install --config-dir X` has already resolved this question, and
# the answer has to be the same one.
if (-not $ConfigDir) {
    $ConfigDir = if ($env:CC_INSIGHTS_HOME) { $env:CC_INSIGHTS_HOME }
                 else { Join-Path $env:APPDATA 'cc-insights' }
}
$LogDir = Join-Path $ConfigDir 'logs'

# Two steps, not `(Get-Command ...).Source`: -ErrorAction covers the lookup but
# not the property access, so under Set-StrictMode a missing `cci` throws here
# instead of falling through to the .venv below.
$CciCommand = Get-Command cci -ErrorAction SilentlyContinue
$Cci = if ($CciCommand) { $CciCommand.Source } else { $null }
if (-not $Cci) {
    $VenvCci = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\cci.exe'
    if (Test-Path $VenvCci) { $Cci = $VenvCci }
}
if (-not $Cci) {
    Write-Error "cannot find the 'cci' executable. Install the package first:  pip install -e ."
    exit 1
}

& $Cci --config-dir $ConfigDir init | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# cmd.exe rather than PowerShell for the payload: `&&` short-circuits the same
# way the launchd job's /bin/sh does, so a failed ingest does not derive over
# half-written rows. Redirecting both streams keeps the two log files the
# macOS job produces.
$OutLog = Join-Path $LogDir 'ingest.log'
$ErrLog = Join-Path $LogDir 'ingest.err'
# `init` leads, matching the launchd job. It applies pending migrations, and
# without it an upgrade that adds one stops this task dead: every other
# command refuses a database older than the code, the refusal lands in
# ingest.err, and nobody reads ingest.err. Capture then stops silently while
# the agent logs it would have read are pruned. init is idempotent.
#
# `sync auto` trails, matching the launchd job, and the order is the safety
# argument: capture has already happened, so an unreachable server cannot
# cost an event. It exits 0 even when it could not connect, so a network
# failure is never mistaken for a capture failure. With nobody signed in it
# does nothing.
#
# The leading `set` is what keeps the task writing the database the CLI
# reads. A scheduled task inherits the environment of the account, not of
# the shell that registered it, so CC_INSIGHTS_HOME exported in this
# session -- or a config dir passed as -ConfigDir -- would otherwise be
# invisible at run time and every step would fall back to
# %APPDATA%\cc-insights. The user would then have two databases: one the
# task fills, one `cci serve` reads, and no error in either. Setting it
# once in the cmd session covers every step, including steps added later
# by someone who does not know to repeat a flag. This is the same fix as
# the plists' EnvironmentVariables key.
$Command = "set `"CC_INSIGHTS_HOME=$ConfigDir`"&& `"$Cci`" init >> `"$OutLog`" 2>> `"$ErrLog`" && `"$Cci`" ingest >> `"$OutLog`" 2>> `"$ErrLog`" && `"$Cci`" derive >> `"$OutLog`" 2>> `"$ErrLog`" && `"$Cci`" sync auto >> `"$OutLog`" 2>> `"$ErrLog`""

# `/s /c "<everything>"` -- cmd strips exactly one outer quote pair and takes
# the rest verbatim. Without the outer pair it splits on the first quoted path
# and the job silently never runs. $Cci and the log paths can all contain
# spaces (`C:\Program Files\...`), so every one of them is quoted inside.
$Action = New-ScheduledTaskAction -Execute "$env:ComSpec" -Argument "/s /c `"$Command`""

# Omitting -RepetitionDuration is how Task Scheduler spells "repeat
# indefinitely". [TimeSpan]::MaxValue looks like it should mean the same thing
# and is rejected as out of range on some Windows builds -- which registers the
# task with no repetition at all, so ingest runs once at logon and then never
# again. That failure is invisible until the history has already been pruned.
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Trigger.Repetition = (New-ScheduledTaskTrigger `
    -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)).Repetition

# Stay out of the way of interactive work, and never block a shutdown.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -Priority 7

$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host "installed $TaskName"
Write-Host "  runs    : $Cci init && $Cci ingest && $Cci derive && $Cci sync auto"
Write-Host "  every   : $IntervalMinutes minutes (and at logon, and once now)"
Write-Host "  config  : $ConfigDir"
Write-Host "  logs    : $OutLog"
Write-Host "  remove  : powershell -ExecutionPolicy Bypass -File $PSCommandPath -Uninstall"
