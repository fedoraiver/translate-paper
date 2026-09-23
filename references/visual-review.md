# Final Chinese PDF visual review

This is a mandatory acceptance step after translation and PDF rendering, for
single papers, chapters, and every newly translated paper in a batch. Automated
strict/quick checks and successful compilation do not replace actual image
inspection. Assign it to the independent visual-review agent described in
[agent-workflow.md](agent-workflow.md).

## Inspect the actual output

1. Freeze the candidate PDF while it is being reviewed. Record its absolute
   path, SHA-256, and page count, plus the source PDF hash. Use the fresh page
   images and contact sheet produced by `check_translation.py --preview-dir`.
   Its default 120 DPI is a starting point; rerender a page or crop at higher
   resolution if text, subscripts, labels, or footnotes are not legible.
2. Open the contact sheet with an image-viewing tool to assess pagination,
   blank/near-blank pages, density, oversized gaps, and inconsistent styling.
3. Actually open **every output page image** at readable resolution, including
   original front/tail pages in the Chinese PDF. Work in bounded page groups and
   record progress; a contact sheet alone is insufficient. Compare relevant
   source pages/crops for figures, tables, equations, footnotes, and boundaries.
4. On each page check Chinese text legibility, missing glyphs, clipping,
   overlaps, margins, paragraph/heading breaks, inappropriate bolding, and
   continuation order. Where present, check equation symbols/subscripts and
   line wrapping, complete visuals without distortion, caption placement, and
   footnotes with their anchors. Check the first/last translated pages for
   duplicated or omitted boundary content. Use structural checks as complementary
   evidence for native/searchable mathematics; that property cannot be proved
   just by looking at the page.
5. Report defects with one-based output page number, source page/unit when
   known, visible symptom, and image path. The coordinator routes repairs to the
   owning translator or layout agent, then repeats affected automated gates.
   After rerendering, regenerate previews and review the new PDF's contact sheet
   and all pages again; pagination may have shifted. Do not change only the hash
   on an old report. A byte-identical copy of the reviewed PDF needs no new review.

Do not mark a page checked from extracted text, a script's pass status, a stale
preview, or another agent's assurance. Never fabricate a completed report to
unblock packaging. If no usable image-viewing tool is available, save progress
and report the actual blocker; do not claim visual acceptance.

## Review evidence

The visual reviewer writes `<work-dir>/visual-review.json` only from actual
inspection. This is separate from `visual-layout.json` (source layout inventory)
and `translation-validation.json` (automated checks). Required fields are:

```json
{
  "schema_version": 1,
  "status": "pass",
  "source_sha256": "<source PDF sha256>",
  "translated_pdf": "<absolute candidate PDF path>",
  "translated_sha256": "<reviewed Chinese PDF sha256>",
  "page_count": 1,
  "reviewer": {
    "role": "visual-review",
    "agent_id": "<actual reviewer agent id>",
    "mode": "subagent"
  },
  "contact_sheet_reviewed": true,
  "pages": [
    {
      "page": 1,
      "status": "pass",
      "image": "<actual inspected page image path>",
      "checks": ["<brief observations of the content and layout actually checked>"],
      "findings": []
    }
  ],
  "open_issues": []
}
```

This is a schema example, not evidence to copy as completed work. Include exactly
one record for every final output page, not just complex pages. Use `status:
"fail"` or `"in_progress"` while findings remain or pages are unreviewed, recording
their actual findings. Keep prior failed reports as iteration evidence if useful.
Resolved findings may be retained separately in `resolved_issues`; acceptance
requires empty `open_issues` and empty per-page `findings`.

If subagent tools are unavailable, the coordinator performs the same image review
and records `mode: "coordinator-fallback"`, its identity, and `fallback_reason`.
Do not use this fallback merely to save time or avoid an independent reviewer.

After a passing review, run:

```powershell
uv run --script <skill-dir>\scripts\check_visual_review.py `
  --source <source.pdf> `
  --translated <staging-dir>\中文翻译.pdf `
  --review <work-dir>\visual-review.json `
  --strict-report <work-dir>\translated-preview\validation-report.json `
  --quick-report <work-dir>\quick-validation.json
```

The checker verifies recorded coverage, PDF identity, and unresolved findings,
using the current hash-bound quick report for page count and cross-checking the
strict report. Run quick validation before this command. It does not perform
visual inspection or certify the truth of the observations.
Only after both actual review and this check pass may the coordinator package,
update delivery links, ingest into Zotero, or report completion. Include
`visual-review.json` in the final supporting artifacts. Keep the inspected
preview images in the work directory for traceability.
