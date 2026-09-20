# Builds a file with EMBEDDED subtitles, the shape that misbehaves in the wild:
# MKV, which no browser plays directly, so every seek restarts the pipe.
$ErrorActionPreference = 'Continue'
$root = Join-Path $env:TEMP 'lj-subs-check'
if (Test-Path $root) { Remove-Item $root -Recurse -Force }
New-Item -ItemType Directory -Path $root | Out-Null

$ffmpeg = if (Test-Path './ffmpeg.exe') { './ffmpeg.exe' } else { 'ffmpeg' }

# A cue every two seconds for six minutes, so there is always one on screen
# and a gap is unmistakable.
$srt = Join-Path $root 'cues.srt'
$lines = New-Object System.Text.StringBuilder
for ($i = 0; $i -lt 180; $i++) {
    $start = $i * 2
    $end = $start + 2
    $ts = { param($s) '{0:00}:{1:00}:{2:00},000' -f [Math]::Floor($s / 3600), [Math]::Floor(($s % 3600) / 60), ($s % 60) }
    [void]$lines.AppendLine([string]($i + 1))
    [void]$lines.AppendLine((& $ts $start) + ' --> ' + (& $ts $end))
    [void]$lines.AppendLine("cue $($i + 1) at $start s")
    [void]$lines.AppendLine('')
}
[IO.File]::WriteAllText($srt, $lines.ToString(), (New-Object Text.UTF8Encoding $false))

$out = Join-Path $root 'Subtitle Probe (2024).mkv'
& $ffmpeg -hide_banner -loglevel error -y `
    -f lavfi -i testsrc=size=640x360:rate=25 -f lavfi -i sine=frequency=400 `
    -i $srt -t 360 `
    -map 0:v -map 1:a -map 2:s `
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p -g 250 `
    -c:a aac -c:s srt -metadata:s:s:0 language=eng -disposition:s:0 default `
    $out 2>&1 | Out-Null

Remove-Item $srt
& "$(Split-Path $ffmpeg)\ffprobe.exe" -hide_banner -loglevel error -show_entries stream=index,codec_type,codec_name -of csv=p=0 $out 2>&1
Get-ChildItem $root | Select-Object Name, Length
Write-Output "library: $root"
