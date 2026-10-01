# Prove each viewer-profile guarantee is tested; any unproven mutation fails the command.
$ErrorActionPreference = 'Stop'
$mutations = @(
    @{ file = 'litejelly/store.py'; name = 'history shared by everyone'; from = 'rows = self._conn.execute("SELECT * FROM progress WHERE profile_id = ?",'; to = 'rows = self._conn.execute("SELECT * FROM progress WHERE ? IS NOT NULL",' }
    @{ file = 'litejelly/store.py'; name = 'resume point shared'; from = '"SELECT * FROM progress WHERE profile_id = ? AND video_id = ?",'; to = '"SELECT * FROM progress WHERE ? IS NOT NULL AND video_id = ?",' }
    @{ file = 'litejelly/store.py'; name = 'deleted profile leaves history'; from = 'removed = self._conn.execute("DELETE FROM progress WHERE profile_id = ?",'; to = 'removed = self._conn.execute("DELETE FROM progress WHERE 0 AND profile_id = ?",' }
    @{ file = 'litejelly/store.py'; name = 'last profile deletable'; from = '.fetchone()[0] <= 1:'; to = '.fetchone()[0] <= 0:' }
    @{ file = 'litejelly/store.py'; name = 'upgrade drops history'; from = '                    FROM progress_single'; to = '                    FROM progress_single WHERE 0' }
    @{ file = 'litejelly/store.py'; name = 'upgrade outside a transaction'; from = '        conn.execute("BEGIN IMMEDIATE")'; to = '        pass' }
    @{ file = 'litejelly/store.py'; name = 'control characters in names'; from = 'if any(not ch.isprintable() for ch in name):'; to = 'if False:' }
    @{ file = 'litejelly/store.py'; name = 'no profile limit'; from = 'if count >= MAX_PROFILES:'; to = 'if False:' }
    @{ file = 'litejelly/web.py'; name = 'library ignores profile'; from = '        progress = h.app.progress.all(profile)'; to = '        progress = h.app.progress.all()' }
    @{ file = 'litejelly/web.py'; name = 'series ignores profile'; from = '        progress = app.progress.all(profile)'; to = '        progress = app.progress.all()' }
    @{ file = 'litejelly/web.py'; name = 'resume ignores profile'; from = '"resume": app.progress.get(video.id, profile) or {},'; to = '"resume": app.progress.get(video.id) or {},' }
    @{ file = 'litejelly/web.py'; name = 'saving ignores profile'; from = "            bool(finished) if finished is not None else None,`n            profile=profile,"; to = '            bool(finished) if finished is not None else None,' }
    @{ file = 'litejelly/web.py'; name = 'unknown profile becomes default'; from = 'profile = h.app.progress.resolve_profile(value)'; to = 'profile = h.app.progress.resolve_profile(value) or h.app.progress.default_profile()' }
    @{ file = 'litejelly/web.py'; name = 'mid-request deletion crashes'; from = "            # Deleted between resolving the request and using it.`n            self.send_api_error(HTTPStatus.NOT_FOUND,"; to = "            # Deleted between resolving the request and using it.`n            self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR," }
    @{ file = 'litejelly/admin_routes.py'; name = 'create without admin'; from = "    def admin_profile_create(h, query):`n        if not h.require_admin(query, write=True):"; to = "    def admin_profile_create(h, query):`n        if False:" }
    @{ file = 'litejelly/admin_routes.py'; name = 'rename from another site'; from = "    def admin_profile_rename(h, query):`n        if not h.require_admin(query, write=True):"; to = "    def admin_profile_rename(h, query):`n        if not h.require_admin(query):" }
    @{ file = 'litejelly/admin_routes.py'; name = 'blank delete guesses default'; from = "# Blank resolves to the default, which a delete must never guess at.`n        if profile_id is None or body.get(`"id`") in (None, `"`"):"; to = "# Blank resolves to the default, which a delete must never guess at.`n        if profile_id is None:" }
)
$mutations | ConvertTo-Json -Depth 5 -Compress | python "$PSScriptRoot/mutation_runner.py" --suite tests.test_profiles,tests.test_identity
exit $LASTEXITCODE
