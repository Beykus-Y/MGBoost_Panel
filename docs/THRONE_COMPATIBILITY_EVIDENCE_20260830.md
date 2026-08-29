# Throne compatibility evidence — 2026-08-30

## Verdict

`SAFE-DEFER`, не `SUPPORTED`.

Production historical `sub_requests` содержит пять Throne observations:
versions `1.0.11`, `1.1.1`, `1.1.4`, `1.2.0`, `1.2.1`; у каждого был legacy
`fingerprint`, но отсутствовали `device_id`, `platform` и `os`. Rolling
`mgboost_hwid_compat_subjects` не содержит Throne. Ни raw HWID, ни token, ни
raw User-Agent в этот документ не внесены.

## Upstream source evidence

Источник: `throneproj/Throne`, branch `dev`, examined 2026-08-30:

- `src/global/HTTPRequestHelper.cpp`: subscription fetch sets
  `User-Agent: Throne/<version>` and, when `sub_send_hwid` enabled,
  `x-hwid`, `x-device-os`, `x-ver-os`, `x-device-model`.
- `src/configs/sub/GroupUpdater.cpp`: subscription update passes
  `sub_send_hwid` into `HttpGet`.

Therefore `src/device_headers.py` accepts `x-device-os` and derives a
platform only from an exact known OS label. Fixture
`tests/test_device_headers.py` exercises `Throne/1.2.1` + these headers using
synthetic values. Unknown OS remains platform-unknown/fail-closed.

## One remaining evidence requirement

Run one controlled Throne subscription refresh with HWID sending enabled
through the production subscription route and observe only the privacy-safe
telemetry tuple `(throne, version, platform, SUPPORTED_HWID_PRESENT)`. Then
add a `CompatibilityRecord` with `evidence_type=CONTROLLED`, current
`evidence_date`, and a test. Until then do not add a whitelist record and do
not claim compatibility support.
