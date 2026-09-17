# Throwaway library for checking the home page by hand. Writes to TEMP, never
# to the repo. Prints the port it started on.
$ErrorActionPreference = 'Continue'
$root = Join-Path $env:TEMP 'lj-hero-check'
if (Test-Path $root) { Remove-Item $root -Recurse -Force }
New-Item -ItemType Directory -Path $root | Out-Null

$ffmpeg = if (Test-Path './ffmpeg.exe') { './ffmpeg.exe' } else { 'ffmpeg' }
$names = @(
    'Harbour Lights S01E01.mp4',
    'Harbour Lights S01E02.mp4',
    'Night Train S01E01.mp4',
    'Night Train S01E02.mp4',
    'The Long Road (2019).mp4',
    'Quiet Harbour (2021).mp4'
)
foreach ($name in $names) {
    $out = Join-Path $root $name
    # Long enough that a resume position can clear the 15s floor.
    & $ffmpeg -hide_banner -loglevel error -y -f lavfi -i testsrc=size=160x90:rate=10 `
        -f lavfi -i sine=frequency=400 -t 90 -c:v libx264 -preset ultrafast -pix_fmt yuv420p `
        -c:a aac -shortest $out 2>&1 | Out-Null
}
Get-ChildItem $root | Select-Object Name, Length
Write-Output "library: $root"
