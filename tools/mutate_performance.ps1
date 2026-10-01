# Prove the measured performance fixes stay in place; any unproven mutation fails the command.
$ErrorActionPreference = 'Stop'
$mutations = @(
    @{ file = 'litejelly/subtitles.py'; name = 'stat before name check'; from = "            if entry.suffix.lower() not in SUBTITLE_EXTENSIONS:`n                continue`n            if require_stem and not entry.stem.lower().startswith(stem):`n                continue`n            if not is_within(video_path.parent, entry) or not entry.is_file():`n                continue"; to = "            if not is_within(video_path.parent, entry) or not entry.is_file():`n                continue`n            if entry.suffix.lower() not in SUBTITLE_EXTENSIONS:`n                continue`n            if require_stem and not entry.stem.lower().startswith(stem):`n                continue" }
    @{ file = 'litejelly/library.py'; name = 'listing rebuilt per request'; from = "        if cached is not None and cached[0] is videos:`n            return cached[1]"; to = "        if False:`n            return cached[1]" }
    @{ file = 'litejelly/web.py'; name = 'no compression'; from = '            if _accepts_gzip(self.headers.get("Accept-Encoding")):'; to = '            if False:' }
    @{ file = 'litejelly/web.py'; name = 'q=0 ignored'; from = '                    return float(value) > 0'; to = '                    return True' }
    @{ file = 'litejelly/ffmpeg.py'; name = 'duplicate cold probes'; from = "        if not owner:`n            return pending.result()"; to = "        if False:`n            return pending.result()" }
)
$mutations | ConvertTo-Json -Depth 5 -Compress | python "$PSScriptRoot/mutation_runner.py" --suite tests.test_performance,tests.test_subtitles,tests.test_lifecycle
exit $LASTEXITCODE
