# Local Windows run

This checkout uses `E:\camoufox-profile-manager`. The helper uses its existing
`data\profiles.db` and `data\profiles` and does not configure an API key. This
matches the Vinted Extractor server README, where `CAMOUFOX_API_KEY` is optional
and blank for local loopback use.

One-time setup, or after Python dependencies change:

```powershell
cd E:\camoufox-profile-manager
uv sync
```

Start the manager:

```powershell
.\scripts\run_local.ps1
```

After changing the UI, rebuild its static bundle first:

```powershell
.\scripts\run_local.ps1 -BuildWebUi
```

The helper refuses to replace an existing server on port 8000, binds only to
`127.0.0.1`, and uses `--no-browser` to prevent repeated tabs. It preserves all
profiles already in `data\profiles.db`. Open
`http://127.0.0.1:8000/` once, then press `Ctrl+C` in the server terminal to
stop it.
