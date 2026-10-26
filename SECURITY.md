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

Report vulnerabilities privately through the repository host's private reporting
channel when available. If it is unavailable, request a private contact channel
without posting the vulnerability details or credentials publicly. Rotate any
credential that may have been exposed; removing a file does not remove history.

The pattern scanner is a defense in depth, not a guarantee that every secret or
copyright issue is detected. Release requires history review and explicit owner
approval. Publication tooling never changes repository visibility.

`--dry` prevents briefing/state publication, but still accesses configured
services and the analysis engine. It is not a network sandbox. `demo` uses only
invented bundled material; tests block external requests and model execution.
