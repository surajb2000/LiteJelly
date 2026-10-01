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
    @{ file = 'litejelly/streaming.py'; name = 'ignore viewer byte offset'; from = 'handle.seek(start)'; to = 'handle.seek(0)' }
    @{ file = 'litejelly/playback.py'; name = 'reset viewer stream offset'; from = 'config.transcode, start=start,'; to = 'config.transcode, start=0,' }
    @{ file = 'litejelly/playback.py'; name = 'drop playback audio delay'; from = '"audio_delay_ms": round(_audio_delay_ms(info, plan, query), 1),'; to = '"audio_delay_ms": 0.0,' }
    @{ file = 'litejelly/web.py'; name = 'damaged credentials reopen setup'; from = "        self.credentials_damaged = (self.credentials is None`n                                    and admin_accounts.credentials_damaged(config.app_dir))"; to = '        self.credentials_damaged = False' }
    @{ file = 'litejelly/web.py'; name = 'failed apply keeps new settings file'; from = "    except BaseException:`n        user_settings.save_overrides(app_dir, previous)`n        raise"; to = "    except BaseException:`n        raise" }
    @{ file = 'litejelly/web.py'; name = 'limit resized before the build'; from = "            # Build everything first, so a failure leaves the running services untouched.`n            built = []"; to = "            self.tools.transcode_sem.resize(new_config.transcode.max_concurrent)`n            built = []" }
    @{ file = 'server.py'; name = 'no version guard'; from = 'if sys.version_info < (3, 10):'; to = 'if False:' }
    @{ file = 'litejelly/store.py'; name = 'progress writes unlocked'; from = "            position = 0.0`n`n        with self._lock:"; to = "            position = 0.0`n`n        if True:" }
    @{ file = 'litejelly/settings.py'; name = 'settings temp left behind'; from = "        Path(temporary).unlink(missing_ok=True)`n        raise"; to = '        raise' }
    @{ file = 'litejelly/auth.py'; name = 'credentials temp left behind'; from = "        Path(temporary).unlink(missing_ok=True)`n        raise"; to = '        raise' }
    @{ file = 'litejelly/subtitles.py'; name = 'cache failure breaks subtitles'; from = "            except OSError as exc:`n                log.debug(`"Could not cache subtitle: %s`", exc)"; to = "            except ValueError as exc:`n                log.debug(`"Could not cache subtitle: %s`", exc)" }
    @{ file = 'litejelly/web.py'; name = 'password change revokes before saving'; from = "        try:`n            admin_accounts.save_credentials(app.config.app_dir, credentials)"; to = "        app.sessions.revoke_all()`n        try:`n            admin_accounts.save_credentials(app.config.app_dir, credentials)" }
)
$mutations | ConvertTo-Json -Depth 5 -Compress | python "$PSScriptRoot/mutation_runner.py" --suite tests.test_identity,tests.test_lifecycle,tests.test_net,tests.test_subtitle_upload,tests.test_http,tests.test_admin,tests.test_failures
exit $LASTEXITCODE
