param(
    [ValidateSet('up','verify','status','logs','down')][string]$Action = 'status',
    [string]$DockerExe = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $DockerExe) {
    $command = Get-Command docker -ErrorAction SilentlyContinue
    if ($command) { $DockerExe = $command.Source }
    else {
        foreach ($candidate in @(
            (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'),
            'C:\Program Files\Docker\Docker\resources\bin\docker.exe')) {
            if (Test-Path -LiteralPath $candidate -ErrorAction SilentlyContinue) { $DockerExe = $candidate; break }
        }
    }
}
if (-not $DockerExe) { throw 'No se puede acceder a docker.exe. Ejecute desde su PowerShell o use -DockerExe con su ruta.' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env.postgres'))) {
    throw 'Primero ejecute scripts/preparar_postgres.ps1.'
}
$composeArgs = @('compose','--project-directory',$projectRoot,'--env-file',
    (Join-Path $projectRoot '.env.postgres'),'-f',(Join-Path $projectRoot 'compose.yaml'))
function Invoke-Compose([string[]]$Arguments) {
    & $DockerExe @composeArgs @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose fallo con codigo $LASTEXITCODE." }
}
switch ($Action) {
    'up' {
        & (Join-Path $PSScriptRoot 'verificar_referencias.ps1')
        Invoke-Compose -Arguments @('config','--quiet')
        Invoke-Compose -Arguments @('up','-d','--wait','--wait-timeout','240')
        Invoke-Compose -Arguments @('ps')
    }
    'verify' {
        Invoke-Compose -Arguments @('exec','-T','postgres','bash','/project/postgres/verify.sh')
        $portLine = Get-Content -LiteralPath (Join-Path $projectRoot '.env.postgres') |
            Where-Object { $_ -match '^POSTGRES_PORT=' } | Select-Object -First 1
        $port = if ($portLine) { [int]($portLine -split '=',2)[1] } else { 55432 }
        $tcp = New-Object Net.Sockets.TcpClient
        try {
            $attempt = $tcp.ConnectAsync('127.0.0.1', $port)
            if (-not $attempt.Wait(5000)) { throw 'Timeout conectando al puerto publicado.' }
            $attempt.GetAwaiter().GetResult()
            Write-Output "OK: puerto publicado 127.0.0.1:$port acepta conexiones TCP."
        } finally { $tcp.Dispose() }
    }
    'status' { Invoke-Compose -Arguments @('ps') }
    'logs' { Invoke-Compose -Arguments @('logs','--no-color','postgres') }
    'down' { Invoke-Compose -Arguments @('down') }
}
