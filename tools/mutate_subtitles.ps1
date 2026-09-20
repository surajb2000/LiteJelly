# Confirms tests/test_subtitles.py fails when the behaviour it claims to pin is
# broken. Run from the repo root; restores static/app.js on the way out.
# unittest writes to stderr, which PowerShell turns into a NativeCommandError,
# so this stays at Continue rather than Stop.
$ErrorActionPreference = 'Continue'
$target = 'static/app.js'
$backup = 'static/app.js.mutbak'

$mutations = @(
    @{ name = 'offset back in the URL';     from = "'&track=' + encodeURIComponent(track.id) +";            to = "'&track=' + encodeURIComponent(track.id) + '&offset=0.00' +" }
    @{ name = 'lookup by currentTime';      from = 'cuesAt(cues, displayTime()';                            to = 'cuesAt(cues, el.video.currentTime' }
    @{ name = 'rebuild on every restart';   from = 'if (state.subtitleSignature === subtitleSignature(plan) && $$(';  to = 'if (false && $$(' }
    @{ name = 'clear cues on every restart'; from = 'if (state.subtitleSignature && state.subtitleSignature !== subtitleSignature(plan)) {'; to = 'if (true) {' }
    @{ name = 'deselect on restart';        from = '    state.appliedAudioOffset = state.audioOffset;';     to = "    state.activeSubtitle = 'off';`n    state.appliedAudioOffset = state.audioOffset;" }
    @{ name = 'no retry on failure';        from = 'addSubtitleTrack(plan, track, attempt + 1);';           to = 'void 0;' }
    @{ name = 'silent give-up';             from = "showToast('Could not load ' + track.label + ' subtitles', 4000);"; to = 'void 0;' }
    @{ name = 'reuse the dead element';     from = '      element.remove();';                               to = '      void 0;' }
    @{ name = 'retry duplicates the track'; from = 'node.dataset.trackId === track.id';                     to = 'false' }
)

Copy-Item $target $backup
try {
    foreach ($m in $mutations) {
        $text = Get-Content $backup -Raw
        if (-not $text.Contains($m.from)) {
            Write-Output ("{0,-30} TARGET MISSING" -f $m.name)
            continue
        }
        Set-Content $target ($text.Replace($m.from, $m.to)) -NoNewline
        $out = & python -m unittest tests.test_subtitles 2>&1 | Out-String
        $verdict = if ($out -match 'FAILED \(') { 'caught' } else { 'SURVIVED' }
        Write-Output ("{0,-30} {1}" -f $m.name, $verdict)
    }
}
finally {
    Copy-Item $backup $target
    Remove-Item $backup
}
