# Theme: Crustdata-inspired colors and typography

Reference: [Crustdata's public site](https://crustdata.com/), checked 2026-11-06. Its published styles use Stack Sans Headline for major headings, Inter for much of the body text, near-black surfaces and violet accents including `#5752ca`. This update adapts those elements to the existing application rather than copying its page designs.

- Headings and brand: **Stack Sans Headline**; body: **Inter**; code and ledger numbers: **JetBrains Mono**. Google Fonts serves the requested families/weights with `display=swap` and local fallbacks.
- Light mode: neutral off-white backgrounds, white panels, charcoal text and violet actions.
- Dark mode: near-black backgrounds, charcoal panels and lighter violet actions.
- Paid, held and rejected states retain separate green, amber and red colors, with text labels and icons.
- Sidebar, cards, forms, tables, tabs, breakpoints, spacing and font sizes keep their existing rules. Only font families/weights/tracking, colors and favicon colors changed.

Verification: all non-theme Python AST nodes were identical to the previous commit; non-theme CSS declarations also matched. Four existing page-rendering tests passed. Tested foreground/background token pairs had a minimum contrast of **5.52:1 in light mode** and **7.94:1 in dark mode**; input borders exceeded **3:1**. These are calculated token checks, not a claim of a full browser accessibility audit. Browser automation had no available session for visual inspection.

The temporary local preview at `http://127.0.0.1:8001/` uses copied databases under the ignored `research_data/theme_preview/` directory and disables external APIs/models. Pages and research tabs were checked over HTTP. The user's manual-test server on port 8000 was not restarted or modified in memory. It will load the new theme at its next normal restart. Preview actions must not be treated as records from the main app.
