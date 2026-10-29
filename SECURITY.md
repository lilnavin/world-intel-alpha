# Security

Keep owner profiles, API keys, model credentials, captured articles and production
logs outside this repository. Never attach them to an issue. Configuration is
trusted local input: it controls feed URLs, filesystem paths and editorial
instructions. Do not run profiles from people you do not trust.

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

The [manual experimental-alpha policy](docs/experimental-alpha.md) covers an
audited source commit and source ZIP only. It neither changes the strict
publication checker nor certifies live output or production readiness. A full
probe HOLD is eligible only as a disclosed safe failure with complete private
evidence: no new/replaced briefing, MEDIA handoff, delivery or model KB-delta
application. Status, raw fetch caches and model captures may still be written.
Generated full output with a positively unsupported factual claim blocks that
candidate, even if automated checks accepted it.

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
project license. Public demos and regression examples use invented material;
live raw evidence belongs outside every Git repository, issue attachment,
public archive and CI artifact. Inspect the intended reachable Git objects and
refs as well as archive paths, contents and commit metadata before publication.

`--dry` prevents briefing/state publication, but still accesses configured
services and the analysis engine. It is not a network sandbox. `demo` uses only
invented bundled material; tests block external requests and model execution.
