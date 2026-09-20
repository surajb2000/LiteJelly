# Confirms tests/test_subtitle_upload.py fails when the rules it claims to pin
# are broken. Run from the repo root; restores both files on the way out.
# unittest writes to stderr, which PowerShell turns into a NativeCommandError,
# so this stays at Continue rather than Stop.
$ErrorActionPreference = 'Continue'
$targets = @('litejelly/subtitles.py', 'litejelly/web.py', 'static/app.js')

# Targets are written with \n; the working copy may be CRLF, so the text is
# normalised before matching. The originals are restored from the backups.
$mutations = @(
    @{ file = 'litejelly/web.py';       name = 'upload needs no account';   from = 'if not h.require_admin(query, write=True):'; to = 'if False:' }
    @{ file = 'litejelly/web.py';       name = 'shared body limit reused';  from = 'limit=MAX_UPLOAD_BYTES';                     to = 'limit=MAX_BODY_BYTES' }
    @{ file = 'litejelly/web.py';       name = 'sloppy base64';             from = 'validate=True';                             to = 'validate=False' }
    @{ file = 'litejelly/subtitles.py'; name = 'anything is a subtitle';    from = '    if _SRT_CUE.search(text):';             to = '    if True:' }
    @{ file = 'litejelly/subtitles.py'; name = 'language unchecked';        from = 'if not _LANGUAGE_TAG.match(tag):';          to = 'if False:' }
    @{ file = 'litejelly/subtitles.py'; name = 'overwrites an existing';    from = '    while candidate.exists():';             to = '    while False:' }
    @{ file = 'litejelly/subtitles.py'; name = 'empty file accepted';       from = '    if not data:';                          to = '    if False:' }
    @{ file = 'litejelly/subtitles.py'; name = 'size cap removed';          from = '    if len(data) > MAX_SUBTITLE_BYTES:';    to = '    if False:' }
    @{ file = 'litejelly/subtitles.py'; name = 'newlines translated again'; from = 'temp.write_text(text, encoding="utf-8", newline="\n")'; to = 'temp.write_text(text, encoding="utf-8")' }
    @{ file = 'litejelly/subtitles.py'; name = 'crlf not normalised';       from = 'text = _decode_text(data).replace("\r\n", "\n").replace("\r", "\n")'; to = 'text = _decode_text(data)' }
    @{ file = 'static/app.js';          name = 'no add when list is empty'; from = 'menu.appendChild(empty);';                  to = 'menu.appendChild(empty); return;' }
    @{ file = 'static/app.js';          name = 'add action not offered';    from = "    appendSubtitleSources(menu);`n  }"; to = "  }" }
    @{ file = 'static/app.js';          name = 'track list patched not replaced'; from = "    clearSubtitleTracks();`n    if (selectId)"; to = "    if (selectId)" }
)

$backups = @{}
foreach ($t in $targets) { $backups[$t] = "$t.mutbak"; Copy-Item $t $backups[$t] }
try {
    foreach ($m in $mutations) {
        $file = $m.file
        $text = (Get-Content $backups[$file] -Raw) -replace "`r`n", "`n"
        if (-not $text.Contains($m.from)) {
            Write-Output ("{0,-32} TARGET MISSING" -f $m.name)
            continue
        }
        Set-Content $file ($text.Replace($m.from, $m.to)) -NoNewline
        $out = & python -m unittest tests.test_subtitle_upload 2>&1 | Out-String
        $verdict = if ($out -match 'FAILED \(|Error') { 'caught' } else { 'SURVIVED' }
        Write-Output ("{0,-32} {1}" -f $m.name, $verdict)
        Copy-Item $backups[$file] $file
    }
}
finally {
    foreach ($t in $targets) { Copy-Item $backups[$t] $t; Remove-Item $backups[$t] }
}
