# Run restart mutations in an isolated copy; any unproven mutation fails the command.
$ErrorActionPreference = 'Stop'

$mutations = @(
    @{ name = 'clock guard removed';      from = 'if (state.restartAt !== null) return state.restartAt;'; to = '' }
    @{ name = 'guard after the element';  from = "if (state.restartAt !== null) return state.restartAt;`n    const current = el.video.currentTime || 0;"; to = "const current = el.video.currentTime || 0;`n    if (state.restartAt !== null) return state.restartAt;" }
    @{ name = 'stale restart can win'; from = "if (!playerSession.current(operation) || token !== state.restartToken) return null;"; to = '' }
    @{ name = 'released by the old source'; from = "playerSession.once(operation, video, 'playing', () => { state.restartAt = null; });"; to = "video.addEventListener('timeupdate', () => { state.restartAt = null; });" }
    @{ name = 'seek leaves the target';   from = "    // The clock now belongs to this seek, not to whatever restart was pending.`n    state.restartAt = null;`n"; to = '' }
    @{ name = 'fresh play keeps a target'; from = "state.restartAt = typeof startAt === 'number' ? startAt : null;"; to = 'state.restartAt = startAt || 0;' }
)

foreach ($mutation in $mutations) { $mutation.file = 'static/app.js' }
$mutations | ConvertTo-Json -Depth 5 -Compress | python "$PSScriptRoot/mutation_runner.py" --suite tests.test_restart
exit $LASTEXITCODE
