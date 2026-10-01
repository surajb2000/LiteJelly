# Run provider mutations in an isolated copy; any unproven mutation fails the command.
$ErrorActionPreference = 'Stop'

$mutations = @(
    @{ file = 'litejelly/opensubtitles.py'; name = 'follows any link';      from = 'validate_remote_url(link, DOWNLOAD_HOSTS)'; to = 'None' }
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
    @{ file = 'litejelly/opensubtitles.py'; name = 'saved account counts as verified'; from = 'status["verified"] = bool(self._verified_as) and self._verified_as == self.account.username'; to = 'status["verified"] = self.account.configured' }
    @{ file = 'litejelly/providers.py';     name = 'refused key cached as no match'; from = "            log.debug(`"TMDb lookup for %r not cached: %s`", title, exc)`n            return None`n        if info is None:`n            self.cache.put(`"tmdb-movie`", key, None, miss=True)"; to = "            info = None`n        if info is None:`n            self.cache.put(`"tmdb-movie`", key, None, miss=True)" }
    @{ file = 'litejelly/providers.py';     name = 'unreachable called refused'; from = 'return {"ok": False, "state": "rejected" if exc.rejected else "failed",'; to = 'return {"ok": False, "state": "rejected",' }
)

$mutations | ConvertTo-Json -Depth 5 -Compress | python "$PSScriptRoot/mutation_runner.py" --suite tests.test_opensubtitles,tests.test_providers
exit $LASTEXITCODE
