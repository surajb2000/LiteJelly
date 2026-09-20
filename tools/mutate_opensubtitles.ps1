# Confirms tests/test_opensubtitles.py fails when the rules it claims to pin
# are broken. Run from the repo root; restores every file on the way out.
# unittest writes to stderr, which PowerShell turns into a NativeCommandError,
# so this stays at Continue rather than Stop.
$ErrorActionPreference = 'Continue'
$targets = @('litejelly/opensubtitles.py', 'litejelly/web.py')

$mutations = @(
    @{ file = 'litejelly/opensubtitles.py'; name = 'follows any link';      from = "if parsed.scheme != `"https`" or not parsed.hostname:"; to = 'if False:' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'password in public view'; from = '"has_password": bool(self.password),'; to = '"has_password": self.password,' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'token never reused';    from = 'fresh = self._token and (time.time() - self._token_at) < TOKEN_LIFETIME'; to = 'fresh = False' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'search without a key';  from = "        if not self.account.api_key:`n            raise OpenSubtitlesError(`"No OpenSubtitles API key has been set`")`n`n        params"; to = "        params" }
    @{ file = 'litejelly/opensubtitles.py'; name = 'quota error unexplained'; from = 'base = "Today''s OpenSubtitles download quota is used up"'; to = 'base = "error"' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'their message dropped'; from = 'return f"{base} ({detail})" if detail else base'; to = 'return base' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'no file hash sent';     from = 'params.append(("moviehash", file_hash))'; to = 'pass' }
    @{ file = 'litejelly/opensubtitles.py'; name = 'bad file_id accepted';  from = '                file_id = int(first.get("file_id"))'; to = '                file_id = int(first.get("file_id") or 0)' }
    @{ file = 'litejelly/web.py';           name = 'fetch needs no account'; from = "    def subtitle_fetch(h, query):`n        `"`"`"Download one of those results and keep it beside the video.`"`"`"`n        if not h.require_admin(query, write=True):"; to = "    def subtitle_fetch(h, query):`n        `"`"`"Download one of those results and keep it beside the video.`"`"`"`n        if False:" }
    @{ file = 'litejelly/web.py';           name = 'search needs no account'; from = "        if not h.require_admin(query, write=False):"; to = "        if False:" }
    @{ file = 'litejelly/web.py';           name = 'download skips the check'; from = 'saved = save_sidecar(path, data, language)'; to = 'saved = path.with_suffix(".srt")' }
)

$backups = @{}
foreach ($t in $targets) { $backups[$t] = "$t.mutbak"; Copy-Item $t $backups[$t] }
try {
    foreach ($m in $mutations) {
        $file = $m.file
        $text = (Get-Content $backups[$file] -Raw) -replace "`r`n", "`n"
        if (-not $text.Contains($m.from)) {
            Write-Output ("{0,-28} TARGET MISSING" -f $m.name)
            continue
        }
        Set-Content $file ($text.Replace($m.from, $m.to)) -NoNewline
        $out = & python -m unittest tests.test_opensubtitles 2>&1 | Out-String
        $verdict = if ($out -match 'FAILED \(|Error') { 'caught' } else { 'SURVIVED' }
        Write-Output ("{0,-28} {1}" -f $m.name, $verdict)
        Copy-Item $backups[$file] $file
    }
}
finally {
    foreach ($t in $targets) { Copy-Item $backups[$t] $t; Remove-Item $backups[$t] }
}
