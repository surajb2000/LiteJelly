# Confirms tests/test_restart.py fails when the behaviour it pins is broken.
# Run from the repo root; restores static/app.js on the way out.
# unittest writes its report to stderr, which PowerShell turns into a
# NativeCommandError, so this stays at Continue.
$ErrorActionPreference = 'Continue'
$target = 'static/app.js'
$backup = 'static/app.js.mutbak'

$mutations = @(
    @{ name = 'clock guard removed';      from = 'if (state.restartAt !== null) return state.restartAt;'; to = '' }
    @{ name = 'guard after the element';  from = "if (state.restartAt !== null) return state.restartAt;`r`n    const current = el.video.currentTime || 0;"; to = "const current = el.video.currentTime || 0;`r`n    if (state.restartAt !== null) return state.restartAt;" }
    @{ name = 'stale restart can win';    from = 'if (token !== state.restartToken) return null;'; to = '' }
    @{ name = 'released by the old source'; from = "video.addEventListener('loadedmetadata', () => { state.restartAt = null; });"; to = "video.addEventListener('timeupdate', () => { state.restartAt = null; });" }
    @{ name = 'seek leaves the target';   from = "    // The clock now belongs to this seek, not to whatever restart was pending.`r`n    state.restartAt = null;`r`n"; to = '' }
    @{ name = 'fresh play keeps a target'; from = "state.restartAt = typeof startAt === 'number' ? startAt : null;"; to = 'state.restartAt = startAt || 0;' }
)

Copy-Item $target $backup
try {
    foreach ($m in $mutations) {
        $text = Get-Content $backup -Raw
        if (-not $text.Contains($m.from)) {
            Write-Output ("{0,-26} TARGET MISSING" -f $m.name)
            continue
        }
        Set-Content $target ($text.Replace($m.from, $m.to)) -NoNewline
        $out = & python -m unittest tests.test_restart 2>&1 | Out-String
        $verdict = if ($out -match 'FAILED \(') { 'caught' } else { 'SURVIVED' }
        Write-Output ("{0,-26} {1}" -f $m.name, $verdict)
    }
}
finally {
    Copy-Item $backup $target
    Remove-Item $backup
}
