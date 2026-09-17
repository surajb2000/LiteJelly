# Confirms tests/test_browse.py actually fails when the behaviour it claims to
# pin is broken. Run from the repo root; restores static/app.js on the way out.
# unittest writes its report to stderr, which PowerShell turns into a
# NativeCommandError. Leaving this at Continue keeps that from aborting the run.
$ErrorActionPreference = 'Continue'
$target = 'static/app.js'
$backup = 'static/app.js.mutbak'

$mutations = @(
    @{ name = 'per-episode started check';   from = 'if (!group.started) {';          to = 'if (!state.progress[video.id]) {' }
    @{ name = 'per-episode, group kept';     from = 'if (!group.started) {';          to = 'if (!state.progress[video.id] && !group.started) {' }
    @{ name = 'part-watched comes back';     from = 'group.done === group.total';     to = 'group.done > 0' }
    @{ name = 'eyebrow claims a suggestion'; from = "'Show you have not started'";    to = "'Suggested show'" }
    @{ name = 'hero repeated in the rail';   from = 'if (key === skip) return;';      to = 'if (!key) return;' }
    @{ name = 'counts from the pool only';   from = 'groupIntoSeries(state.videos)';  to = 'groupIntoSeries(pool.ranked)' }
)

Copy-Item $target $backup
try {
    foreach ($m in $mutations) {
        $text = Get-Content $backup -Raw
        if (-not $text.Contains($m.from)) {
            Write-Output ("{0,-32} TARGET MISSING" -f $m.name)
            continue
        }
        Set-Content $target ($text.Replace($m.from, $m.to)) -NoNewline
        $out = & python -m unittest tests.test_browse 2>&1 | Out-String
        $verdict = if ($out -match 'FAILED \(') { 'caught' } else { 'SURVIVED' }
        Write-Output ("{0,-32} {1}" -f $m.name, $verdict)
    }
}
finally {
    Copy-Item $backup $target
    Remove-Item $backup
}
