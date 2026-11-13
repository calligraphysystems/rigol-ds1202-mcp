# Troubleshooting

| Symptom | Check |
| --- | --- |
| No USB device | Device port, powered scope, cable, libusb, group permissions/fresh login |
| SSH fails | Host reachability, correct dedicated key, verified host key, forced-command path |
| Scope busy | Another operation owns the lock; close other control applications |
| Unavailable measurement | Trigger/run state, enabled input, scaling and signal; null is not zero |
| Screenshot issue | Use implemented BMP24 conversion; native PNG CRCs failed on tested firmware |
| Memory setting times out | Memory-depth writes require running acquisition; do not bypass checks |
| Wrong hardware decoder bytes | Wide display views produced errors; use supported host decoding |
| Empty host decode | Confirm format/rate, valid idle/framing boundary, voltage, probe ratio and window |
| Restore fails | Preserve setup ID/error, inspect actual state, follow supported restore prerequisites |
| Result missing | Check correct account, artifact type, expiry and integrity |

Keep the front panel and other applications idle during workflows. On a lost SSH
response, inspect state before another control: cleanup after process death is not
guaranteed. Native setup import is intentionally disabled; core restore is scoped
and decoder restore separate. Do not use reset as an undocumented recovery action.

Report a minimal reproduction with model/firmware, server version, operation and
sanitized response. Strip serials, private paths and data before posting. Synthetic
or known fixture captures are preferred over application/customer signals.
