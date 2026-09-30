# Prove phase-two safety contracts through isolated assertion failures.
$ErrorActionPreference = 'Stop'
$mutations = @(
    @{ file = 'litejelly/library.py'; name = 'resolve against reordered roots'; from = 'if video.root_path:'; to = 'if False:' }
    @{ file = 'litejelly/library.py'; name = 'forget persisted media IDs'; from = 'self._identify(videos)'; to = 'None' }
    @{ file = 'litejelly/web.py'; name = 'reset occupied stream capacity'; from = 'tools.transcode_sem = old_tools.transcode_sem'; to = 'pass' }
    @{ file = 'litejelly/web.py'; name = 'drop refreshed metadata callback'; from = '                enricher.on_updated = lambda: self.library.request_scan(force=True)'; to = '                enricher.on_updated = None' }
    @{ file = 'litejelly/web.py'; name = 'allow nonfinite seek values'; from = 'if not math.isfinite(value) or value > MAX_SECONDS:'; to = 'if False:' }
    @{ file = 'litejelly/net.py'; name = 'allow private DNS answers'; from = 'any(not ipaddress.ip_address(item[4][0]).is_global for item in candidates)'; to = 'False' }
    @{ file = 'litejelly/net.py'; name = 'redirect credentials to another host'; from = 'if sensitive and hostname != validate_remote_url(request.full_url, self.allowed_hosts):'; to = 'if False:' }
    @{ file = 'litejelly/subtitles.py'; name = 'skip sidecar containment'; from = 'if not is_within(video_path.parent, entry) or not entry.is_file():'; to = 'if not entry.is_file():' }
)
$mutations | ConvertTo-Json -Depth 5 -Compress | python "$PSScriptRoot/mutation_runner.py" --suite tests.test_identity,tests.test_lifecycle,tests.test_net,tests.test_subtitle_upload
exit $LASTEXITCODE
