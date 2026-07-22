# X-SaaS Read-Only OpenCLI A/B

This pilot compares the current ASA X-SaaS CDP runner with a private OpenCLI
adapter. It does not call intake, database apply, outreach, or any remote write
action.

## Strategy Note

Strategy: `UI_SELECTOR`

Contract: `visible-ui`

Evidence:

- Observed controls: `input.search-input[ng-model="ngkeyword"]` and
  `[ng-click="fnQuerySearch();"]`.
- Observed rows: `table.candidate-list tbody tr` with stable candidate detail
  URLs containing `candidate/info/<id>`.
- Authentication source: the existing signed-in local X-SaaS tab on CDP port
  `9223`. The experiment creates an isolated authenticated tab using the same
  local session-copy mechanism as the production runner, then gives OpenCLI
  only that tab's CDP endpoint through the child process environment. Session
  values are never printed or written to the report, and the isolated tab is
  closed after the experiment.
- Replay validation: `opencli browser verify xsaas/candidate-search` plus the
  real A/B report produced by `experiments/xsaas_opencli_ab.py`.

The visible search form and candidate table are the user-facing contract, so a
private internal API is not assumed to be more stable. Login, stale-query,
parse-empty, and timeout states are surfaced as typed OpenCLI errors.

## Metrics

- Successful run rate.
- Repeat consistency, measured as mean pairwise Jaccard overlap.
- Relative recall against the deduplicated union of both engines.
- Required-field completeness for candidate ID, name, company, title, and URL.
- Mean successful-run duration.

Execution actions remain on the current ASA runner unless OpenCLI is strictly
better on stability and relative recall, with field completeness no worse.

## Run

```bash
./opencli/install-private-adapters.sh
python3 -m unittest tests/test_xsaas_opencli_ab.py -v
python3 experiments/xsaas_opencli_ab.py \
  --query "server power" \
  --repeats 3 \
  --limit 30
```

The output JSON is written under `work/`. Candidate identities are represented
by one-way short hashes in comparison-only lists.

The same experiment surface is available for Liepin through
`experiments/liepin_opencli_ab.py`. Liepin uses the production runner in
`--dry-run` mode as its baseline and never opens candidate links.
