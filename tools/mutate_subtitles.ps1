# Run subtitle mutations in an isolated copy; any unproven mutation fails the command.
$ErrorActionPreference = 'Stop'

$mutations = @(
    @{ name = 'offset back in the URL';     from = "'&track=' + encodeURIComponent(track.id) +";            to = "'&track=' + encodeURIComponent(track.id) + '&offset=0.00' +" }
    @{ name = 'lookup by currentTime';      from = 'cuesAt(cues, displayTime()';                            to = 'cuesAt(cues, el.video.currentTime' }
    @{ name = 'rebuild on every restart';   from = 'if (state.subtitleSignature === subtitleSignature(plan) && $$(';  to = 'if (false && $$(' }
    @{ name = 'clear cues on every restart'; from = 'if (state.subtitleSignature && state.subtitleSignature !== subtitleSignature(plan)) {'; to = 'if (true) {' }
    @{ name = 'deselect on restart';        from = "    state.appliedAudioOffset = state.audioOffset;`n    state.lastSavedPosition = -1;"; to = "    state.activeSubtitle = 'off';`n    state.appliedAudioOffset = state.audioOffset;`n    state.lastSavedPosition = -1;" }
    @{ name = 'no retry on failure';        from = 'addSubtitleTrack(plan, track, attempt + 1);';           to = 'void 0;' }
    @{ name = 'silent give-up';             from = "showToast('Could not load ' + track.label + ' subtitles', 4000);"; to = 'void 0;' }
    @{ name = 'reuse the dead element';     from = '      element.remove();';                               to = '      void 0;' }
    @{ name = 'retry duplicates the track'; from = 'node.dataset.trackId === track.id';                     to = 'false' }
    @{ file = $true; name = 'cache writer translates'; platform = 'win32'; from = 'cache_path.write_text(vtt, encoding="utf-8", newline="")'; to = 'cache_path.write_text(vtt, encoding="utf-8")' }
    @{ file = $true; name = 'vtt endings left alone';   from = "    text = text.replace(`"\r\n`", `"\n`").replace(`"\r`", `"\n`").lstrip(`"\ufeff`")`n    if not text.lstrip()"; to = "    text = text.lstrip(`"\ufeff`")`n    if not text.lstrip()" }
    @{ file = $true; name = 'old conversions reused';   from = 'key = f"v{CACHE_VERSION}|{video_path}|{stat.st_mtime_ns}|{track_id}"'; to = 'key = f"{video_path}|{stat.st_mtime_ns}|{track_id}"' }
    @{ name = 'cue pinned to the screen';   from = "el.subtitleLayer.style.setProperty('--subtitle-rest', Math.round(rest) + 'px');"; to = 'void 0;' }
    @{ name = 'fill treated as letterbox';  from = "if (state.aspect === 'contain' && video.videoWidth"; to = 'if (video.videoWidth' }
    @{ name = 'raised under the dock';      from = 'const raised = Math.max(box.height * 0.2, rest);'; to = 'const raised = rest;' }
    @{ name = 'not redone on a new file';   from = "      state.restartAt = null;`n      placeSubtitleLayer();"; to = "      state.restartAt = null;" }
    @{ name = 'not redone on resize';       from = 'else placeSubtitleLayer();'; to = 'else void 0;' }
)

foreach ($mutation in $mutations) {
    $mutation.file = if ($mutation.file) { 'litejelly/subtitles.py' } else { 'static/app.js' }
}
$mutations | ConvertTo-Json -Depth 5 -Compress | python "$PSScriptRoot/mutation_runner.py" --suite tests.test_subtitles
exit $LASTEXITCODE
