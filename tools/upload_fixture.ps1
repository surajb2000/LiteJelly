# A video with NO subtitle stream, plus a sidecar file to upload to it, so the
# "no tracks at all" path is the one under test.
$ErrorActionPreference = 'Continue'
$root = Join-Path $env:TEMP 'lj-upload-check'
if (Test-Path $root) { Remove-Item $root -Recurse -Force }
New-Item -ItemType Directory -Path $root | Out-Null

$ffmpeg = if (Test-Path './ffmpeg.exe') { './ffmpeg.exe' } else { 'ffmpeg' }
$out = Join-Path $root 'Harbour Lights (2021).mkv'
& $ffmpeg -hide_banner -loglevel error -y `
    -f lavfi -i testsrc=size=320x180:rate=15 -f lavfi -i sine=frequency=400 `
    -t 90 -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac -shortest `
    $out 2>&1 | Out-Null

# Deliberately not beside the video: it is the file a viewer would pick from
# their own device, and it must not be discovered until it is uploaded.
$drop = Join-Path $env:TEMP 'lj-upload-drop'
if (Test-Path $drop) { Remove-Item $drop -Recurse -Force }
New-Item -ItemType Directory -Path $drop | Out-Null

$srt = @"
1
00:00:01,000 --> 00:00:05,000
Uploaded line one.

2
00:00:06,000 --> 00:00:10,000
Caf$([char]0xE9) ouvert tard.

3
00:00:11,000 --> 00:00:20,000
Uploaded line three.
"@
# Written as cp1252 on purpose: an accented file is where browser-side text
# decoding would have gone wrong.
[IO.File]::WriteAllText((Join-Path $drop 'Harbour Lights.fr.srt'), $srt,
                        [Text.Encoding]::GetEncoding(1252))
[IO.File]::WriteAllText((Join-Path $drop 'not-a-subtitle.srt'),
                        "#!/bin/sh`nrm -rf /`n", [Text.Encoding]::UTF8)

Get-ChildItem $root, $drop | Select-Object Name, Length
Write-Output "library: $root"
Write-Output "drop:    $drop"
