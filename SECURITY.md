# Security

Keep owner profiles, API keys, model credentials, captured articles and production
logs outside this repository. Never attach them to an issue. Configuration is
trusted local input: it controls feed URLs, filesystem paths and editorial
instructions. Do not run profiles from people you do not trust.

Feed and article acquisition accepts only unauthenticated public HTTP(S)
destinations. It checks every resolved address and redirect, then connects to a
checked address while retaining the original hostname for TLS verification.
Local, private and other non-global addresses are rejected. Acquisition does not
use caller-session credentials, environment proxies or netrc authentication.
Internal feeds and proxy-dependent sources may therefore be unavailable.

General feed/article requests use the product User-Agent without the configured
contact email. SEC requests retain the separate contactable User-Agent. Shipped
feed examples use HTTPS; keep credentials and private URLs out of custom feeds.

Private model-evidence stores can contain whole source excerpts, supplied
knowledge context, and raw model responses. They are not distributable demo
data. Keep them outside Git and media attachment directories, limit filesystem
access, and exclude them from public archives and workflow artifacts.

Source review is a bounded model-assisted check, not independent factual
certification. Article content is untrusted input. Unsupported, uncertain or
unavailable review evidence must not be promoted to owner content acceptance.
Review is opt-in; alpha live use should enable both `source_review_enabled` and
`source_review_repair_enabled` in `analysis`. Disabled review preserves legacy
behavior, including possible model KB-delta application in normal runs.

The [source-publication decision](docs/experimental-alpha.md) supersedes the
historical source-alpha eligibility checklist for the audited source release,
including its unsupported-artifact publication block. It does not change runtime
review barriers, security controls or the strict production checker, and does
not certify live output or production readiness. Under the historical policy,
a full probe HOLD is eligible only as a disclosed safe failure with complete private
evidence: no new/replaced briefing, MEDIA handoff, delivery or model KB-delta
application. Status, raw fetch caches and model captures may still be written.
Generated full output with a positively unsupported factual claim blocked that
source-alpha candidate, even if automated checks accepted it. The documented
source-release exception does not count as independent content acceptance or
production qualification.

Report vulnerabilities privately through the repository host's private reporting
channel when available. If it is unavailable, request a private contact channel
without posting the vulnerability details or credentials publicly. Rotate any
credential that may have been exposed; removing a file does not remove history.

The pattern scanner is a defense in depth, not a guarantee that every secret or
copyright issue is detected. Release requires history review and explicit owner
approval. Publication tooling never changes repository visibility.

The MIT license covers this project's software and documentation. It does not
grant rights to third-party reporting, article captures or provider output.
Keep source provenance and applicable provider/source terms separate from the
project license. The bundled demo and new alpha regression additions use invented
material. Inherited tests retain short historical headline examples in
`tests/test_fetch.py`, public historical names in `tests/test_ownership.py`,
and numeric/public-filing metadata examples. See the
[fixture provenance inventory](tests/fixtures/README.md): historical provenance
is incomplete, and no rights clearance or authenticity verification is asserted.
Full private captures/articles and live raw evidence belong outside every Git
repository, issue attachment, public archive and CI artifact. Inspect the intended
reachable Git objects and refs as well as archive paths, contents and commit
metadata before publication.

`--dry` prevents briefing/state publication, but still accesses configured
services and the analysis engine. It is not a network sandbox. `demo` uses only
invented bundled material; tests block external requests and model execution.
