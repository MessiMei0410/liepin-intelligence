# ASA OpenCLI Read-Only Shadow Mode

## Runtime Contract

Every approved `multi_channel_sourcing` execution keeps the existing ASA
channel runners, intake, attribution, sync, and audit path authoritative.
After the Liepin and X-SaaS baseline files are produced, the runtime samples
the first query from each channel and runs the matching private OpenCLI adapter.
Adapter rows are recall data only. The shadow runtime then reuses the production
detail-page capture for each selected row and records `complete`, `partial`, or
`failed` using the same full text, work history, and education requirements as
the authoritative runners.

Shadow output never enters the combined candidate file and exposes:

```json
{
  "mode": "read_only_shadow",
  "affects_intake": false,
  "affects_outreach": false,
  "sample_policy": "first_query_per_channel"
}
```

Each channel comparison contains counts, relative recall, card-field
completeness, resume completeness, capture status counts, and one-way hashes
for differences. Candidate names, resume content, URLs, IDs, and session values
are not written to shadow artifacts.

## Failure Behavior

OpenCLI errors are recorded as a channel-level `blocked` result. They do not
raise out of `multi_channel_sourcing`, change the baseline candidate set, or
block intake and audit.

## Artifacts

Per-run artifact:

```text
<asa_artifacts>/sourcing/<run>-opencli-shadow.json
```

Append-only aggregate history:

```text
<asa_artifacts>/sourcing/opencli-shadow-history.jsonl
```

The ASA workflow panel shows a compact line such as `猎聘 重合 10/10` and
explicitly states that the shadow result did not participate in intake.

## Controls

Shadow mode is enabled by default. Disable one request with:

```json
{"opencli_shadow": false}
```

Disable it for the service environment with:

```bash
ASA_OPENCLI_SHADOW=0
```

No business action should migrate to OpenCLI based on a single shadow sample.
Use cross-workflow history to evaluate stability and recall before creating a
separate action pilot.
