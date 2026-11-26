$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$target = Join-Path $projectRoot '.env.postgres'
if (Test-Path -LiteralPath $target) {
    Write-Output '.env.postgres ya existe; no se modifica.'
    exit 0
}
function New-LocalPassword {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    [Convert]::ToBase64String($bytes)
}
$lines = @('POSTGRES_DB=banco', 'POSTGRES_USER=banco_admin',
    ('POSTGRES_PASSWORD=' + (New-LocalPassword)), 'POSTGRES_PORT=55432',
    'APP_DB_USER=banco_lectura', ('APP_DB_PASSWORD=' + (New-LocalPassword)))
[IO.File]::WriteAllText($target, ($lines -join "`n") + "`n", [Text.UTF8Encoding]::new($false))
Write-Output '.env.postgres creado con claves locales aleatorias; no se imprimen.'
