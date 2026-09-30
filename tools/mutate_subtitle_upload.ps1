# Run upload mutations in an isolated copy; any unproven mutation fails the command.
$ErrorActionPreference = 'Stop'
$mutations = @(
    @{ file = 'litejelly/web.py';       name = 'upload needs no account';   from = "if not h.require_admin(query, write=True):`n            return`n        body = h.read_json_body(limit=MAX_UPLOAD_BYTES)"; to = "if False:`n            return`n        body = h.read_json_body(limit=MAX_UPLOAD_BYTES)" }
    @{ file = 'litejelly/web.py';       name = 'shared body limit reused';  from = 'limit=MAX_UPLOAD_BYTES';                     to = 'limit=MAX_BODY_BYTES' }
    @{ file = 'litejelly/web.py';       name = 'sloppy base64';             from = 'validate=True';                             to = 'validate=False' }
    @{ file = 'litejelly/subtitles.py'; name = 'anything is a subtitle';    from = '    if _SRT_CUE.search(text):';             to = '    if True:' }
    @{ file = 'litejelly/subtitles.py'; name = 'language unchecked';        from = 'if not _LANGUAGE_TAG.match(tag):';          to = 'if False:' }
    @{ file = 'litejelly/subtitles.py'; name = 'overwrites an existing';    from = '    while candidate.exists():';             to = '    while False:' }
    @{ file = 'litejelly/subtitles.py'; name = 'empty file accepted';       from = "    if not data:`n        raise ValueError(`"That file is empty`")"; to = "    if not data:`n        data = b'WEBVTT\n\n'" }
    @{ file = 'litejelly/subtitles.py'; name = 'size cap removed';          from = '    if len(data) > MAX_SUBTITLE_BYTES:';    to = '    if False:' }
    @{ file = 'litejelly/subtitles.py'; name = 'newlines translated again'; platform = 'win32'; from = 'temp.write_text(text, encoding="utf-8", newline="\n")'; to = 'temp.write_text(text, encoding="utf-8")' }
    @{ file = 'litejelly/subtitles.py'; name = 'crlf not normalised';       from = 'text = _decode_text(data).replace("\r\n", "\n").replace("\r", "\n")'; to = 'text = _decode_text(data)' }
    @{ file = 'static/app.js';          name = 'no add when list is empty'; from = 'menu.appendChild(empty);';                  to = 'menu.appendChild(empty); return;' }
    @{ file = 'static/app.js';          name = 'add action not offered';    from = "    appendSubtitleSources(menu);`n  }"; to = "  }" }
    @{ file = 'static/app.js';          name = 'track list patched not replaced'; from = "    clearSubtitleTracks();`n    if (selectId)"; to = "    if (selectId)" }
)

$mutations | ConvertTo-Json -Depth 5 -Compress | python "$PSScriptRoot/mutation_runner.py" --suite tests.test_subtitle_upload
exit $LASTEXITCODE
