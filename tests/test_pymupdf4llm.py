"""Compare extract_text_with_sections vs extract_text_pymupdf4llm on real papers.

Run directly (no pytest needed):

    .venv/bin/python tests/test_pymupdf4llm.py

Or with specific DOIs to fetch+compare:

    .venv/bin/python tests/test_pymupdf4llm.py --doi 10.48550/arXiv.1706.03762

PDFs are looked up in PDF_CACHE_DIR (default ~/.cache/academic-mcp/pdfs).
If a DOI's PDF is not cached, the script skips it with a message.
"""

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from academic_mcp.pdf_extractor import (
    _determine_body_font_size,
    _open_doc,
    extract_text_pymupdf4llm,
    extract_text_with_sections,
)
from academic_mcp.config import config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doi_to_cache_path(doi: str) -> Path | None:
    """Attempt to find a cached PDF for a DOI via the text cache index."""
    try:
        from academic_mcp import text_cache
        cached = text_cache.get_cached(doi)
        if cached and cached.source and cached.source.endswith(".pdf"):
            p = Path(cached.source)
            if p.exists():
                return p
    except Exception:
        pass

    # Fallback: look for any .pdf whose name is derived from the DOI hash
    import hashlib
    doi_hash = hashlib.md5(doi.encode()).hexdigest()
    candidate = config.pdf_cache_dir / f"{doi_hash}.pdf"
    return candidate if candidate.exists() else None


def compare(pdf_path: Path, label: str) -> bool:
    """Run both extractors on *pdf_path* and print a side-by-side report.

    Returns True if the pymupdf4llm extraction passes all checks.
    """
    print(f"\n{'='*70}")
    print(f"TEST: {label}")
    print(f"File: {pdf_path.name}")
    print(f"{'='*70}")

    # ── Current system ────────────────────────────────────────────────
    t0 = time.perf_counter()
    current = extract_text_with_sections(pdf_path)
    t_current = time.perf_counter() - t0

    # ── pymupdf4llm system ────────────────────────────────────────────
    t0 = time.perf_counter()
    p4l = extract_text_pymupdf4llm(pdf_path)
    t_p4l = time.perf_counter() - t0

    print(f"\n  Current:     {len(current['text']):>8,} chars | "
          f"{len(current['sections']):>3} sections | "
          f"detection: {current.get('section_detection', '?'):25s} | {t_current:.1f}s")
    print(f"  pymupdf4llm: {len(p4l['text']):>8,} chars | "
          f"{len(p4l['sections']):>3} sections | "
          f"detection: {p4l.get('section_detection', '?'):25s} | {t_p4l:.1f}s")

    all_pass = True

    # --- Check 1: schema completeness ---
    required_keys = {"text", "pages", "truncated", "metadata", "sections", "section_detection"}
    missing = required_keys - p4l.keys()
    if missing:
        print(f"\n  ✗ Missing keys in result: {missing}")
        all_pass = False
    else:
        print(f"\n  ✓ Result schema complete")

    # --- Check 2: section offsets are valid ---
    bad_offsets = [
        s for s in p4l["sections"]
        if not (0 <= s["start"] <= s["end"] <= len(p4l["text"]))
    ]
    if bad_offsets:
        print(f"  ✗ {len(bad_offsets)} sections have out-of-range offsets")
        all_pass = False
    else:
        print(f"  ✓ All section offsets valid")

    # --- Check 3: broken words (LaTeX kerning test) ---
    # Suspicious: 4+ consecutive 1-2-char tokens in the first 2000 chars
    sample = re.sub(r'[|*`#\-_]', ' ', p4l["text"][:2000])
    broken = re.findall(r'(?:\b[a-zA-Z]{1,2}\s){4,}', sample)
    if broken:
        print(f"  ⚠ Possible broken words (LaTeX kerning): {broken[:3]}")
    else:
        print(f"  ✓ No broken-word patterns in first 2000 chars")

    # --- Check 4: table detection ---
    table_lines_p4l = [
        l for l in p4l["text"].splitlines()
        if l.strip().startswith("|") and l.count("|") >= 3
    ]
    table_lines_cur = [
        l for l in current["text"].splitlines()
        if l.strip().startswith("|") and l.count("|") >= 3
    ]
    print(f"  Table lines — current: {len(table_lines_cur):>4}, pymupdf4llm: {len(table_lines_p4l):>4}", end="")
    if table_lines_p4l:
        print(f"  ✓  (sample: {table_lines_p4l[0][:60]})")
    else:
        print()

    # --- Check 5: footnote leak ---
    fn_pattern = re.compile(r'^\s*\d{1,3}[\.\)]\s+\S', re.MULTILINE)
    fns_cur = len(fn_pattern.findall(current["text"]))
    fns_p4l = len(fn_pattern.findall(p4l["text"]))
    fn_ratio = fns_p4l / max(fns_cur, 1)
    status = "✓" if fn_ratio <= 2.0 else "⚠"
    print(f"  {status} Footnote-like lines — current: {fns_cur}, pymupdf4llm: {fns_p4l} (ratio {fn_ratio:.1f}x)")
    if fn_ratio > 2.0:
        print(f"    → pymupdf4llm may be leaking footnotes; consider tightening fontsize_limit")

    # --- Check 6: bold/italic (pymupdf4llm advantage) ---
    bold_count = len(re.findall(r'\*\*[^*\n]+\*\*', p4l["text"]))
    italic_count = len(re.findall(r'(?<!\*)\*[^*\n]+\*(?!\*)', p4l["text"]))
    print(f"  Formatting: {bold_count} bold spans, {italic_count} italic spans")

    # --- Check 7: section headings ---
    print(f"  Sections (current, first 6):")
    for s in current["sections"][:6]:
        print(f"    [L{s.get('level',2)}] {s['title'][:65]}")
    print(f"  Sections (pymupdf4llm, first 6):")
    for s in p4l["sections"][:6]:
        print(f"    [L{s.get('level',2)}] {s['title'][:65]}")

    # --- Write outputs for manual inspection ---
    out_dir = Path("test_output")
    out_dir.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w-]", "_", label)
    (out_dir / f"{safe}_current.txt").write_text(current["text"], encoding="utf-8")
    (out_dir / f"{safe}_pymupdf4llm.md").write_text(p4l["text"], encoding="utf-8")
    print(f"\n  Written: test_output/{safe}_current.txt  +  _pymupdf4llm.md")

    return all_pass


# ---------------------------------------------------------------------------
# Test corpus
# ---------------------------------------------------------------------------

# Papers to test. Grouped by category — see the docstring at the top.
# Values are DOIs; the script looks them up in the PDF cache.
TEST_CORPUS = {
    # Category 1: LaTeX two-column + benchmark tables
    "latex_attention":    "10.48550/arXiv.1706.03762",
    "latex_gpt3":         "10.48550/arXiv.2005.14165",
    "latex_parrots":      "10.1145/3442188.3445922",
    # Category 2: Publisher single-column + data tables
    "lancet_covid":       "10.1016/S0140-6736(20)30566-3",
    "nature_alphafold":   "10.1038/s41586-021-03819-2",
    # Category 3: Humanities / heavy footnotes
    "politics_society":   "10.1177/0032329219838932",
    "modern_law_review":  "10.1111/1468-2230.70009",
    # Category 4: Scanned / OCR
    "jstor_1980s":        "10.2307/1229039",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--doi", nargs="*",
        help="Run only these DOIs (looks them up in PDF_CACHE_DIR)",
    )
    parser.add_argument(
        "--pdf", nargs="*",
        help="Run on these PDF file paths directly",
    )
    parser.add_argument(
        "--all-cached", action="store_true",
        help="Run on every .pdf found in PDF_CACHE_DIR",
    )
    args = parser.parse_args()

    jobs: list[tuple[Path, str]] = []

    if args.pdf:
        for p in args.pdf:
            path = Path(p)
            jobs.append((path, path.stem))

    if args.doi:
        for doi in args.doi:
            path = _doi_to_cache_path(doi)
            if path:
                jobs.append((path, doi))
            else:
                print(f"SKIP (not cached): {doi}")

    if args.all_cached:
        for p in sorted(config.pdf_cache_dir.glob("*.pdf")):
            jobs.append((p, p.stem))

    if not jobs:
        # Default: run on the predefined corpus
        for label, doi in TEST_CORPUS.items():
            path = _doi_to_cache_path(doi)
            if path:
                jobs.append((path, label))
            else:
                print(f"SKIP (not cached): {label} ({doi})")

    if not jobs:
        print("\nNo PDFs to test. Fetch some papers first, or use --all-cached.")
        sys.exit(0)

    passed = 0
    failed = []
    for pdf_path, label in jobs:
        ok = compare(pdf_path, label)
        if ok:
            passed += 1
        else:
            failed.append(label)

    print(f"\n{'='*70}")
    print(f"SUMMARY: {passed}/{len(jobs)} passed")
    if failed:
        print(f"FAILED:  {', '.join(failed)}")
    print(f"{'='*70}")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
