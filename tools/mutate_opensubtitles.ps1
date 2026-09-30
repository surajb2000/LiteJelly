# Run provider mutations in an isolated copy; any unproven mutation fails the command.
$ErrorActionPreference = 'Stop'

$mutations = @(
    @{ file = 'litejelly/opensubtitles.py'; name = 'follows any link';      from = "if parsed.scheme != `"https`" or not parsed.hostname:"; to = 'if False:' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'password in public view'; from = '"has_password": bool(self.password),'; to = '"has_password": self.password,' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'token never reused';    from = 'fresh = self._token and (time.time() - self._token_at) < TOKEN_LIFETIME'; to = 'fresh = False' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'API key omitted'; from = '"Api-Key": self.account.api_key,'; to = '"Api-Key": "",' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'quota error unexplained'; from = 'base = "Today''s OpenSubtitles download quota is used up"'; to = 'base = "error"' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'their message dropped'; from = 'return f"{base} ({detail})" if detail else base'; to = 'return base' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'no file hash sent';     from = 'params.append(("moviehash", file_hash))'; to = 'pass' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'bad file_id accepted';  from = '                file_id = int(first.get("file_id"))'; to = '                file_id = int(first.get("file_id") or 0)' }
    @{ file = 'litejelly/web.py';           name = 'fetch needs no account'; from = "    def subtitle_fetch(h, query):`n        `"`"`"Download one of those results and keep it beside the video.`"`"`"`n        if not h.require_admin(query, write=True):"; to = "    def subtitle_fetch(h, query):`n        `"`"`"Download one of those results and keep it beside the video.`"`"`"`n        if False:" }
    @{ file = 'litejelly/web.py';           name = 'search needs no account'; from = "        if not h.require_admin(query, write=False):"; to = "        if False:" }
    @{ file = 'litejelly/web.py';           name = 'download skips the check'; from = "language = str(body.get(`"language`") or `"`") or language_from_name(remote_name)`n        try:`n            saved = save_sidecar(path, data, language)"; to = "language = str(body.get(`"language`") or `"`") or language_from_name(remote_name)`n        try:`n            saved = path.with_suffix(`".srt`")" }
)

$mutations | ConvertTo-Json -Depth 5 -Compress | python "$PSScriptRoot/mutation_runner.py" --suite tests.test_opensubtitles
exit $LASTEXITCODE
