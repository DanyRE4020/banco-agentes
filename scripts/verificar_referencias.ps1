$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$catalogPath = Join-Path $projectRoot 'catalogo/catalogo.json'
$catalog = Get-Content -Raw -Encoding UTF8 -LiteralPath $catalogPath | ConvertFrom-Json
$expectedHashes = @{
    '001_dimensiones_cliente_producto_txn (1).sql' = 'EEE849F1BFBDC138291A81682A0ED836AC5DBD37B9C499C2A00A273377E7696A'
    '002_generar_cuentas_y_saldos_enero_2026 (1).sql' = '6DFC90C1E55B2092584499C05A74B55309AEDA004D8CE135E0BC7E6F765ECD2B'
    '003_generar_transacciones_y_saldos_feb_jun_2026 (1).sql' = 'D28D22B90E9882FA3780C23D1107DF79CE72F9556BA8351D505EB108B0756FA3'
    'hub_and_spoke_adk.ipynb' = 'DF37909B4D8B31EA85972C1FF3553D5A1D04F3434C841D38FB1FA6A35A3E5007'
    'Ollama_Python_Client_Data_Steward (1).ipynb' = 'ADEFC420013F8B4234183513420E0033A481BE453701F397CED9BBA8358706E6'
}
foreach ($name in $expectedHashes.Keys) {
    $path = Join-Path (Join-Path $projectRoot 'referencias') $name
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $expectedHashes[$name]) {
        throw "Referencia original modificada: $name"
    }
}
foreach ($entry in $catalog.sources) {
    $path = Join-Path $projectRoot $entry.file
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $entry.sha256) {
        throw "Catalogo desactualizado: $($entry.file)"
    }
}
$expectedColumns = @{
    'dim.dim_cliente' = 12
    'dim.dim_producto' = 6
    'dim.dim_txn' = 5
    'cfg.fact_sdos_captacoloca_m' = 9
    'cfg.fact_txn_captacoloca_d' = 10
    'tmp_calculo_mes' = 13
}
if ($catalog.tables.Count -ne 6) { throw 'Numero inesperado de tablas.' }
$citations = @()
foreach ($table in $catalog.tables) {
    if (-not $expectedColumns.ContainsKey($table.name) -or $table.columns.Count -ne $expectedColumns[$table.name]) {
        throw "Tabla o columnas inesperadas: $($table.name)"
    }
    if ($table.foreign_keys.Count -ne 0) { throw 'Se inventaron claves foraneas.' }
    if ($table.name -eq 'dim.dim_txn' -and @($table.columns | Where-Object { $_.primary_key -or $_.unique }).Count) {
        throw 'dim_txn no declara PK ni UNIQUE.'
    }
    $citations += $table.source
    $citations += @($table.columns | ForEach-Object { $_.source })
    foreach ($column in $table.columns) {
        if (-not $table.source.excerpt.Contains($column.declaration)) {
            throw "Columna sin declaracion en DDL: $($column.id)"
        }
    }
}
foreach ($entry in $catalog.documentation) { $citations += $entry.source }
foreach ($entry in $catalog.interpretations) {
    if ($entry.kind -ne 'inferred_not_enforced') { throw 'Interpretacion mal etiquetada.' }
    $citations += $entry.sources
}
foreach ($citation in $citations) {
    $lines = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $projectRoot $citation.file)
    $excerpt = $lines[($citation.line - 1)..($citation.end_line - 1)] -join "`n"
    if (-not $excerpt.Contains($citation.excerpt)) { throw "Cita incorrecta: $($citation.file):$($citation.line)" }
}
$permanent = @($catalog.tables | Where-Object { -not $_.temporary })
$temporary = @($catalog.tables | Where-Object { $_.temporary })
if ($permanent.Count -ne 5 -or $temporary.Count -ne 1) { throw 'Clasificacion temporal incorrecta.' }
Write-Output "OK: 5 originales intactos; 3 hashes SQL coinciden con el catalogo."
Write-Output "OK: 5 tablas permanentes, 1 temporal, 55 columnas y ninguna FK declarada."
Write-Output "OK: $($citations.Count) citas verificadas contra archivos y lineas originales."
Write-Output 'Estas comprobaciones son estaticas; no ejecutan Python, ADK, Ollama ni SQL.'
