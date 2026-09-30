# Run browse mutations in an isolated copy; any unproven mutation fails the command.
$ErrorActionPreference = 'Stop'

$mutations = @(
    @{ name = 'per-episode started check';   from = 'if (!group.started) {';          to = 'if (!state.progress[video.id]) {' }
    @{ name = 'per-episode, group kept';     from = 'if (!group.started) {';          to = 'if (!state.progress[video.id] && !group.started) {' }
    @{ name = 'part-watched comes back';     from = 'group.done === group.total';     to = 'group.done > 0' }
    @{ name = 'eyebrow claims a suggestion'; from = "'Show you have not started'";    to = "'Suggested show'" }
    @{ name = 'hero repeated in the rail';   from = 'if (key === skip) return;';      to = 'if (!key) return;' }
    @{ name = 'counts from the pool only';   from = 'const series = groupIntoSeries(state.videos);';  to = 'const series = groupIntoSeries(pool.ranked);' }
)

foreach ($mutation in $mutations) { $mutation.file = 'static/app.js' }
$mutations | ConvertTo-Json -Depth 5 -Compress | python "$PSScriptRoot/mutation_runner.py" --suite tests.test_browse
exit $LASTEXITCODE
