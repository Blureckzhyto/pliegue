# Corre el Windows App Certification Kit (WACK) sobre la copia firmada de prueba del MSIX.
#     powershell -ExecutionPolicy Bypass -File tools\run_wack.ps1
# WACK necesita permisos de administrador: Windows pide confirmarlo (UAC). El reporte queda en
# build\msix\wack.xml y abajo se imprime el resumen (resultado de cada prueba y sus mensajes).
$root = Split-Path -Parent $PSScriptRoot
$version = [regex]::Match((Get-Content (Join-Path $root "ui\app.py") -Raw -Encoding UTF8), 'VERSION = "([^"]+)"').Groups[1].Value
$msix = Join-Path $root "build\msix\Pliegue_$version.0_x64_prueba_firmado.msix"
$report = Join-Path $root "build\msix\wack.xml"
$appcert = "C:\Program Files (x86)\Windows Kits\10\App Certification Kit\appcert.exe"
if (-not (Test-Path $msix)) { throw "No existe $msix (py -3.11 tools\build_msix.py --firmar-prueba)" }
if (-not (Test-Path $appcert)) { throw "No está WACK: winget install Microsoft.WindowsSDK.10.0.26100" }
Remove-Item $report -ErrorAction SilentlyContinue
$proc = Start-Process -FilePath $appcert -Verb RunAs -Wait -PassThru `
    -ArgumentList "test -appxpackagepath `"$msix`" -reportoutputpath `"$report`""
"appcert terminó (código $($proc.ExitCode))"
if (-not (Test-Path $report)) { throw "WACK no escribió el reporte" }
[xml]$xml = Get-Content $report -Encoding UTF8
"RESULTADO GENERAL: $($xml.REPORT.OVERALL_RESULT)"
foreach ($test in $xml.SelectNodes("//TEST")) {
    $result = $test.RESULT.'#cdata-section'
    if (-not $result) { $result = $test.RESULT.InnerText }
    $optional = if ($test.OPTIONAL -eq "TRUE") { " (opcional)" } else { "" }
    "  $($result.Trim().PadRight(8)) $($test.NAME)$optional"
    if ($result.Trim() -ne "PASS") {
        $test.SelectNodes(".//MESSAGE") | Select-Object -First 3 | ForEach-Object { "           - $($_.TEXT)" }
    }
}
