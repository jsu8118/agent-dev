# Runbook RB-03: TLS certificate expiry warnings

**Symptom:** `certificate expires in N days` warnings in service logs.

* Warnings start **30 days** before expiry and repeat daily. They are **not** errors and do not affect traffic until the certificate actually expires.
* Our certificates auto-renew via the cert-manager job 14 days before expiry. Only escalate if the warning shows **fewer than 7 days** remaining, or if a renewal job failure is logged.
* Do not confuse these warnings with an active incident: correlate with actual error rates first.
