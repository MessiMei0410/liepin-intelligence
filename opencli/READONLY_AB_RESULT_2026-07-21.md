# ASA OpenCLI Read-Only Sourcing A/B Result

Date: 2026-07-21

Scope: read-only candidate search. No intake, database apply, resume opening,
outreach, or workflow action was executed.

## Decision

Keep the current ASA executor for business actions. The private OpenCLI
adapters are eligible for continued read-only observation, but the action
migration gate remains closed because stability and relative recall were equal,
not strictly better.

## Results

| Channel | Query | Repeats | Baseline success / consistency | OpenCLI success / consistency | Baseline / OpenCLI unique | Overlap | Mean duration | Gate |
| --- | --- | ---: | --- | --- | --- | ---: | --- | --- |
| Liepin | `PC 电源 TME` | 3 | 100% / 1.0 | 100% / 1.0 | 10 / 10 | 10 | 9807ms / 6174ms | Closed |
| X-SaaS | `工程师` | 3 | 100% / 1.0 | 100% / 1.0 | 30 / 30 | 30 | 4567ms / 3743ms | Closed |
| X-SaaS job probe | `电源` | 1 | 100% | 100% | 30 / 30 | 30 | 4518ms / 3741ms | Closed |

OpenCLI was about 37% faster on the Liepin sample and 18% faster on the
non-empty X-SaaS sample. Speed alone does not satisfy the migration rule.

The final `电源` probe reported the same X-SaaS total result count (`11,565`)
and the same first 30 candidates on both paths. Earlier transient zero-row
probes were rejected after trace screenshots showed the page was still in its
`loading...` state; the adapter now waits for that state to clear.

Relative recall is measured against the deduplicated union of both engines.
It is a controlled comparison metric, not labeled ground-truth recall.

## Production Finding

The X-SaaS page still returned 40 candidate rows, but no longer rendered
candidate detail links in those rows. The production parser required a link ID
and therefore filtered every row out. Both readers now use the Angular row
scope as the primary source:

- `ipersonid` for the X-SaaS candidate ID.
- `sNameView` / `sName` / `sname` for the display name.
- `scompany` / `sposition` and `arrJobDetail` for work evidence.
- A reconstructed `/app/candidate/info/<id>` source URL.

The previous link and cell parser remains as a compatibility fallback.

## Artifacts

- `work/liepin-opencli-ab-job154.json`
- `work/xsaas-opencli-ab-job154.json`
- `work/xsaas-opencli-ab-smoke-power.json`

Reports contain aggregate metrics, diagnostics, and one-way candidate-key
hashes only. They do not contain candidate names or session values.

## Migration Rule

Business actions may enter a separate pilot only when all conditions are true:

1. OpenCLI stability score is strictly higher than the production baseline.
2. OpenCLI relative recall is strictly higher than the production baseline.
3. Required-field completeness is no worse.
4. Existing ASA approval, deduplication, intake, attribution, and audit layers
   remain authoritative.
