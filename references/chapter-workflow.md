# Chapters and excerpts

Use this reference only for an explicitly requested chapter or source range.
The translation engine, fidelity, native mathematics, and validation gates stay
the same as for a paper. Project paths and local-only rules take precedence over
the default packaging and Zotero workflow.

## Establish the source once

Locate the complete source and inspect its table of contents, PDF page labels,
first requested page, final requested page, and next section start. Printed page
numbers and one-based PDF page numbers may differ; verify the mapping instead of
assuming one offset applies throughout the book. Include chapter introductions,
final exercises, notes, and closing paragraphs. Do not append the next chapter
or a blank page just to supply a stop-before sentinel.

Create a vector-preserving excerpt without changing the complete source. Reuse
an existing excerpt when its range and hash match. Record `source-provenance.json`
beside the work artifacts with:

- complete source path and SHA-256;
- requested chapter/title and printed page range;
- exact one-based PDF page range and any reviewed mid-page crop;
- excerpt path and SHA-256.

The excerpt is the immutable input to `prepare_paper.py`. Its packaged original
must be byte-identical to that excerpt; it is not a byte-identical copy of the
whole book. Hash-bind boundary/source/math reviews to the excerpt. Reuse the
complete book's bibliographic metadata and record the chapter designation and
pages; do not invent a paper DOI, author, or publication for a chapter.

## Review chapter boundaries

Chapters often have neither Introduction nor Conclusion. Keep the chapter title
as original front matter by default and start translation at its opening prose,
not the first numbered subsection. In `boundary-review.json` set the verified
`start_id`, `stop_before_id`, `allow_missing_conclusion: true`, and a reason
describing the full requested range.

`stop_before_id` is exclusive and must name an existing unit. If the last unit
is itself translatable and there is no following unit, use that final ID for
`stop_before_id` and also list it in `include_ids`. Check that it appears exactly
once in the translated body and not again in an original tail. For a final
visual/equation or a one-unit excerpt that cannot satisfy the current boundary
schema, treat that as a pipeline limitation to resolve, not permission to omit
content, add a fake unit, or append an unrelated page.

Translate chapter prose, section headings, definitions, theorems, proof text,
exercises, and body footnotes. Preserve code, notation, rule labels, figures,
tables, and their captions. Do not answer exercises. A closing quotation or
chapter note is not automatically bibliography; classify it from the requested
range and actual source. Preserve a genuine bibliography as original material.

## Extraction review before translating

Review pages containing any of these patterns before committing their translations:

- Exercise/definition numbers mistaken for headings, footnote markers, or math.
- Running headers mixed into body text and sentences split across page breaks.
- Subscripts, primes, lambda terms, substitution brackets, inference bars, and
  reduction arrows separated into unrelated units.
- Numbered rule diagrams and syntax trees split into several clips, or clips
  whose bounds include adjacent prose. Preserve the complete figure once.
- Unnumbered correspondence tables and chapter-opening notes. An unnumbered
  note without an anchor is not an ordinary numbered footnote; preserve its
  reading order and do not attach it to the middle of a continuing sentence.

Apply the operations in [source-review.md](source-review.md). In particular,
`group_visual` requires an existing caption ID; do not invent a caption for an
uncaptioned object. Preserve its actual kind and correct its bounds with a
reviewed `replace_unit`, suppressing only duplicate extracted pieces. If the
renderer cannot handle that representation, diagnose the general limitation;
do not relabel a table as an equation solely to bypass a failing gate.

Review math from the source image together with span font/baseline information.
Code character encodings and OCR candidates are not evidence of the intended
symbol. Verify each repaired expression before marking it `manually_reviewed`;
do not mark an entire file reviewed merely because it compiles. Rule names stay
literal, variables retain subscripts/primes, and program keywords remain code.

## Delivery

Use the project's final PDF and intermediate-artifact directories from the
start. Retain the provenance and replayable review files with the work state.
Create a chapter summary from the requested chapter only. Check the final PDF's
opening and closing pages, exercise/proof paragraph breaks, diagram captions,
footnote anchors, and long expressions even after strict validation passes.

Update course/index links only when required by the user or project instructions.
Check all occurrences of the chapter, including repeated reading assignments.
Link the final verified PDF; preserve existing buttons, original links, and
completion controls. Do not infer permission to ingest into Zotero from the
presence of `zotero-item.json` in a local-only bundle.
