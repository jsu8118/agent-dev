# Runbook RB-04: Rolling back a deploy

1. Confirm the suspected deploy ID and service from `deploys.csv` / the deploy dashboard.
2. Run `deployctl rollback <service> --to <previous_version>` (requires the on-call role). Rollbacks take ~3 minutes per service (rolling).
3. Watch the error rate and latency for 10 minutes; confirm recovery in api-gateway logs.
4. Record the rollback in the incident channel with timestamps.
5. Freeze further deploys of that service until the post-incident review.
