# v2raytun Windows compatibility evidence — 2026-08-30

## Verdict

`SUPPORTED` начиная с numeric version `3.8.11` только для `windows`.

Это не blanket allow для Windows и не классификация для других платформ,
нечисловых версий или более старых версий. Для них `compat_registry` по-прежнему
возвращает `UNKNOWN`, а opaque path остаётся fail-closed.

## Evidence

Источник: read-only production historical `sub_requests`, examined
2026-08-30. Агрегированно, без raw HWID, token, URL, username или User-Agent:

| Client | Version | Platform | Requests | Well-formed device id | Legacy fingerprint |
|---|---:|---|---:|---:|---:|
| v2raytun | 3.8.11 | windows | 7 | 7 | 7 |
| v2raytun | 3.8.12 | windows | 5 | 5 | 5 |

`CompatibilityRecord("v2raytun", "3.8.11", "windows", SUPPORTED,
"HISTORICAL", "2026-08-30", ...)` is the reviewed minimum-version baseline.
The registry's established numeric minimum-version rule permits `3.8.11+` on
Windows and rejects `3.8.10`, a different platform, or an unparseable version.

## Incident and safe scope

The confirmed opaque-404 incident was caused by the absence of this exact
registry tuple: a valid opaque credential resolved its account, then
`hwid_gate` returned `DENY_UNSUPPORTED_CLIENT`; the anti-oracle response
correctly collapsed that denial to `404`. Alias, credential generation,
subscription and provisioning wiring were intact. No raw-SQL repair, token
rotation, rebind, entitlement change or device cleanup is required.

## Regression contract

- A real-format `v2raytun/3.8.12/Windows` request carrying `x-hwid` reaches
  the allowed opaque HWID path.
- A recent `Happ/Windows` request remains supported.
- An unknown client and a supported client without HWID remain denied.
- Opaque compatibility observation is privacy-safe and fail-open: it records
  only normalized aggregate dimensions and the HWID-presence classification,
  never raw HWID, token or URL, and cannot change resolver outcome.

