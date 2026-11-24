#requires -Version 7.3
[CmdletBinding()]
param([string]$Lab = (Join-Path $env:USERPROFILE 'rsi-lab'))
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$Lab = [IO.Path]::GetFullPath($Lab)
$tools = Join-Path $Lab 'tools'
$back = Join-Path $Lab 'rsi-data-spring-boot'
$front = Join-Path $Lab 'rsi-data-platform'
$logs = Join-Path $Lab 'logs'
$null = New-Item -ItemType Directory -Force -Path $logs
foreach ($port in 3000,30845,3216) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "Port $port is occupied" }
}
if (-not $env:NVM_HOME) { $env:NVM_HOME = [Environment]::GetEnvironmentVariable('NVM_HOME', 'User') }
if (-not $env:NVM_SYMLINK) { $env:NVM_SYMLINK = [Environment]::GetEnvironmentVariable('NVM_SYMLINK', 'User') }
$env:Path = "$env:NVM_HOME;$env:NVM_SYMLINK;$env:Path"
nvm use 24.14.1
$env:PYTHONUTF8 = '1'
$env:NEXT_TELEMETRY_DISABLED = '1'
$node24 = Join-Path $env:NVM_SYMLINK 'node.exe'
$python = (Get-Command python.exe).Source
$workerArgs = @('-B', '-u', ('"' + "$PSScriptRoot\harness\worker.py" + '"'), '--db', ('"' + "$Lab\data\flight_2.sqlite" + '"'), '--runs', ('"' + "$logs\runs" + '"'))
$javaArgs = @(
    '-Dfile.encoding=UTF-8', ('"-Dloader.path=' + "$tools\h2-1.4.199.jar" + '"'),
    '-cp', 'core-starter/target/core-starter-1.0.0-executable.jar',
    'org.springframework.boot.loader.PropertiesLauncher', '--spring.profiles.active=dev',
    '--server.address=127.0.0.1', '--server.port=30845',
    '--eureka.client.enabled=false', '--spring.cloud.discovery.enabled=false',
    '--spring.datasource.driver-class-name=org.h2.Driver',
    '--spring.datasource.url=jdbc:h2:mem:rsi;MODE=MySQL;DB_CLOSE_DELAY=-1',
    '--spring.datasource.username=sa', ('--spring.datasource.password=' + [guid]::NewGuid().ToString('N')),
    '"--spring.datasource.druid.validation-query=SELECT 1"', '--spring.h2.console.enabled=false',
    '--server.tomcat.accesslog.enabled=true', '--server.tomcat.accesslog.buffered=false',
    '--server.tomcat.basedir=core-starter/target/tomcat-local'
)

$started = [Collections.Generic.List[Diagnostics.Process]]::new()
try {
    $started.Add((Start-Process $python -ArgumentList $workerArgs -WindowStyle Hidden -PassThru -RedirectStandardOutput "$logs\worker.log" -RedirectStandardError "$logs\worker.err.log"))
    $started.Add((Start-Process "$tools\jdk8\bin\java.exe" -ArgumentList $javaArgs -WorkingDirectory $back -WindowStyle Hidden -PassThru -RedirectStandardOutput "$logs\spring.log" -RedirectStandardError "$logs\spring.err.log"))
    $started.Add((Start-Process $node24 -ArgumentList @('node_modules/next/dist/bin/next','dev','--hostname','127.0.0.1','--port','3000') -WorkingDirectory $front -WindowStyle Hidden -PassThru -RedirectStandardOutput "$logs\next.log" -RedirectStandardError "$logs\next.err.log"))
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        if ($started.Where({$_.HasExited}).Count) { throw "A service exited. See $logs" }
        try {
            $api = Invoke-RestMethod 'http://127.0.0.1:30845/v2/api-docs' -TimeoutSec 2
            $workerReady = Get-NetTCPConnection -State Listen -LocalPort 3216 -ErrorAction SilentlyContinue
            if ($api.info.title -eq 'dataTesting-core' -and $workerReady.OwningProcess -eq $started[0].Id) {
                $page = Invoke-WebRequest 'http://127.0.0.1:3000/harness' -TimeoutSec 30
                $ready = $page.StatusCode -eq 200
            }
        } catch { $ready = $false }
        if ($ready) { break }
        Start-Sleep -Seconds 1
    }
    if (!$ready) { throw "Startup timed out. See $logs" }
    [pscustomobject]@{worker=$started[0].Id; java=$started[1].Id; next=$started[2].Id; page='http://127.0.0.1:3000/harness'}
} catch {
    foreach ($process in $started) { if (!$process.HasExited) { $process.Kill($true) } }
    throw
}
