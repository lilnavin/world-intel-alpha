# Offline fixture inventory and provenance

Reviewed and reconstructed on **2026-10-26**. These files are offline parser
inputs, not investment data or evidence of current events. Historical filenames
identify regression cases and table layouts; they do not make a synthetic file
an authentic filing or provider response.

## Reconstructed examples

The following files were reconstructed locally on the review date. Prose and
identities explicitly marked synthetic were written for these tests. Numeric
regression values and standard schema/table labels were retained where needed;
we do not claim those retained values were newly invented or freshly verified.

| File | Disposition and coverage |
| --- | --- |
| `amd_10q.htm` | Synthetic HTML/prose around retained segment table labels/cells. Preserves four MD&A title occurrences, a quoted cross-reference, mixed-case estimates mention, nested section boundaries, long document/section spacing, unchanged-risk refusal, glued unscaled commitment cells, and four disclosure hits. |
| `msft_10k_segments.txt` | Reconstructed vertical blocks; retained numeric cells, annual year columns and percentage-change cells. Issuer narrative removed. |
| `amzn_10q_segments.txt` | Reconstructed vertical blocks; retained numeric cells and prior-year-first/four-column ordering. Issuer narrative removed. |
| `meta_10q_segments.txt` | Reconstructed separate revenue/income tables; retained numeric cells, percentage columns, negative amounts and revenue sublines. Synthetic spacing separates the tables. |
| `aapl_10q_segments.txt` | Reconstructed transposed segment table plus revenue-only geographic and product tables. Retains numeric cells and the deliberate refusal case. Issuer narrative removed. |
| `form4_buy.xml` | Synthetic issuer/owner and SGML envelope; retained seven transaction rows and numeric/date/classification cases; escaped ampersand and officer-title precedence. Addresses, signatures, footnotes and unused filing prose removed. |
| `form4_multi_txn.xml` | Synthetic issuer/owner and SGML envelope; retained seven non-derivative rows and transaction-code order. Same address/prose removal. |
| `form4_derivative.xml` | Synthetic issuer/owner and SGML envelope; retained four non-derivative and two derivative rows with numeric/date cases. Same address/prose removal. |
| `form4_grant.xml` | Synthetic issuer/owner and SGML envelope; retained zero-price grant, director role and `0` planned-transaction spelling. Same address/prose removal. |
| `form4_planned_sale.xml` | Synthetic issuer/owner/ticker and SGML envelope; retained sale, director role and `true` planned-transaction spelling. Same address/prose removal. |
| `schedule13d_new.xml` | Synthetic issuer and three reporting persons, new purpose prose; retained numeric stakes/shares/date. Default and prefixed namespaces, nested `formData/items1To7/item4/transactionPurpose`, maximum-percent selection and word-boundary purpose truncation remain. Addresses, contact details, signatures and filing narrative removed. |
| `schedule13d_activist_amend.xml` | Synthetic issuer/filer; retained numeric stake/shares/date and absent Item 4. Addresses, contact details, signatures and filing narrative removed. Filename is a historical case label. |
| `rss_bbc.xml` | Synthetic RSS titles/descriptions and example.com links; retains two items, date and category variants. Filename identifies a format case only. |
| `rss_dw.rdf` | Synthetic RDF title/description and example.com link; preserves namespaces and date parsing. |
| `rss_atom.xml` | Synthetic Atom title/summary and example.com link; preserves link attribute and date parsing. |
| `guardian.json` | Synthetic article title/body/summary and example.com link in a Guardian-shaped schema. No publisher article is reproduced. |
| `newsdata.json` | Synthetic titles/descriptions/body, source identities and example.com links; retained response shape, country/category and pagination examples. |
| `marketaux.json` | Synthetic title/description/snippet/source and example.com link; retained entity/sentiment schema values. |
| `federal_register.json` | Synthetic rule title and example.com links; retained schema/date/agency values used by normalization. Not evidence that the rule was published. |
| `gdelt_artlist.json` | Synthetic titles on reserved example domains; retained three-entry volume case. |
| `twelvedata_quote.json` | Synthetic instrument identity; retained scalar quote regression values and response shape. Not a captured or current tradable quote. |
| `twelvedata_error.json` | Synthetic error text; retains the 404/error response shape. |

## Inherited AMD table lineage

The earlier `amd_10q.htm` fixture carried the following provenance record for
the old source of the retained numeric table. This is an **inherited,
unverified record**, not a capture record for today's reconstructed HTML,
a fresh SEC verification, proof of authenticity, or a rights clearance:

- Source URL: [AMD primary document](https://www.sec.gov/Archives/edgar/data/2488/000000248826000123/amd-20260627.htm)
- CIK: `0000002488`; accession: `0000002488-26-000123`; form: `10-Q`
- Period end: `2026-06-27`; filed: `2026-08-05`; retrieved: `2026-08-11`
- Recorded original raw size: **1,348,602 bytes**; recorded tag-stripped text:
  **248,222 characters**.
- The earlier trimming note described retaining selected whole top-level body
  children in document order, including the segment table, while stripping the
  inline XBRL hidden header and unrelated financial statements/risk pages.
  The 2026-10-26 replacement instead reconstructs synthetic prose and HTML
  around the retained table; that earlier trimming description does not
  describe the reconstructed file's current size or contents.

## Retained numeric facts and public filing metadata

| File | Provenance and limits |
| --- | --- |
| `amd_companyfacts.json` | Retained 546 numeric fact rows with all namespace/tag/unit, period, accession and filing metadata unchanged. Existing `_fixture_provenance` records [SEC companyfacts source](https://data.sec.gov/api/xbrl/companyfacts/CIK0000002488.json), capture date **2026-08-11**, raw size and trimming procedure. Those fields are inherited records, not an independent re-fetch on the review date. Removed taxonomy label/description prose and user-agent metadata. |
| `daily_index.idx` | Retained public company/form/CIK/date/accession metadata in SEC daily-index layout, with one personal-name row changed to a synthetic owner label, CIK `9999999001` and consistent valid-shape accession/path `edgar/data/9999999001/9999999001-26-000001.txt`. The prior personal CIK/accession were removed. Mixed recorded/reconstructed fixture, not an unmodified SEC response. Filing date in rows: 2026-07-31; original capture date and exact source URL were not recorded. |
| `daily_index_periodic.idx` | Retained public-company identifiers and periodic/nonperiodic form examples. Existing tests describe this as a constructed extension of the other index. Row date 2026-08-17 is a test date, not capture provenance. Exact capture lineage was not recorded. |
| `edgar.json` | Retained public filing-search metadata (company names, CIKs, forms and accession/document identifiers). No filing body or article prose. Capture date and exact search request were not recorded; row filing dates do not establish retrieval provenance. |
| `fred.json` | Retained small numeric schema example (4.33) and date fields. No source URL or capture date was recorded. These are regression inputs, not a verified historical observation. |
| `gdelt_timelinetone.json` | Retained three numeric tone examples (-1.5, -0.5, -1.0), dates and schema labels. No source request or capture date was recorded. Not a verified historical series. |

Schema/reference links describe formats rather than proving capture lineage:
[SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces),
[SEC filing indexes](https://www.sec.gov/Archives/edgar/daily-index/),
[FRED observations](https://fred.stlouisfed.org/docs/api/fred/series_observations.html).

## Rights and interpretation boundary

The project's source-code license does not establish ownership or licensing of
retained third-party facts, table labels, taxonomy identifiers or metadata.
Availability through SEC or another endpoint alone is not a rights assurance.
No new permission, complete chain of custody, or legal clearance is asserted by
this inventory. Do not treat inherited capture dates as freshly verified, or
reconstructed snippets as issuer statements. Private runtime data, paid research,
credentials and personal contact/address records are not needed by these tests.

## Historical matching examples outside these fixtures

Unchanged selector and deduplication cases in `tests/test_ownership.py` retain
public historical names as matching inputs, including named investment-manager
allowlist cases and an insider/company deduplication case. They are historical
matching scenarios, not newly synthetic identities or evidence of live activity.
The synthetic-identity descriptions above apply to the reconstructed fixture
payloads; they do not classify every literal in test source as synthetic.
