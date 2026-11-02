# Design Specification Lock (spec_lock)

> **CRITICAL**: Read this file at the start of EVERY slide generation session and
> re-read it BEFORE generating each slide. The values defined here are the single
> source of truth for this deck's visual identity. Never deviate.

## Anti-Drift Protocol

The LLM MUST follow this protocol to prevent visual drift across slides:

1. **Before slide 1**: Read this entire file. Internalize the contract.
2. **Before each subsequent slide**: Re-read the "Quick Reference" section at minimum.
3. **After every 3 slides**: Re-read the full Colors and Fonts sections.
4. **When uncertain**: Invoke `get_design_context(session_id)` for the authoritative snapshot.

## Quick Reference Card

Copy this block into your working memory before generating each slide:

```
COLORS: primary=[P]  secondary=[S]  accent=[A]  bg=[BG]  text=[T]  text-light=[TL]
FONTS:  title=[TF]  body=[BF]  title-size=[TS]px  body-size=[BS]px
LAYOUT: margins=[M]px  safe-area=X:80-880 Y:40-520
```

Replace `[...]` with actual values from the sections below.

---

## Theme Presets

### Dark Professional (Default)
```yaml
colors:
  primary: "#1a1a2e"
  secondary: "#16213e"
  accent: "#4c9aff"
  background: "#0f0f1a"
  text: "#ffffff"
  text_light: "#a0a0b0"
fonts:
  title: "Inter, Noto Sans SC, sans-serif"
  body: "Inter, Noto Sans SC, sans-serif"
  mono: "JetBrains Mono, Cascadia Code, monospace"
  title_size: 36
  body_size: 18
layout:
  margins: [80, 60, 80, 60]
  grid_columns: 12
  spacing_scale: "standard"
```

### Light Professional
```yaml
colors:
  primary: "#1a1a2e"
  secondary: "#2d2d44"
  accent: "#4c9aff"
  background: "#fafafa"
  text: "#1a1a2e"
  text_light: "#606070"
fonts:
  title: "Inter, Noto Sans SC, sans-serif"
  body: "Inter, Noto Sans SC, sans-serif"
  mono: "JetBrains Mono, Cascadia Code, monospace"
  title_size: 36
  body_size: 18
layout:
  margins: [80, 60, 80, 60]
  grid_columns: 12
  spacing_scale: "standard"
```

### Warm / Human
```yaml
colors:
  primary: "#2d1f1a"
  secondary: "#4a3528"
  accent: "#e8945a"
  background: "#fdf8f4"
  text: "#2d1f1a"
  text_light: "#7a6a5e"
fonts:
  title: "Playfair Display, Noto Serif SC, serif"
  body: "Inter, Noto Sans SC, sans-serif"
  mono: "JetBrains Mono, monospace"
  title_size: 38
  body_size: 18
layout:
  margins: [80, 60, 80, 60]
  grid_columns: 12
  spacing_scale: "breathing"
```

### Minimal Corporate
```yaml
colors:
  primary: "#000000"
  secondary: "#333333"
  accent: "#0066cc"
  background: "#ffffff"
  text: "#000000"
  text_light: "#666666"
fonts:
  title: "Helvetica Neue, Arial, sans-serif"
  body: "Helvetica Neue, Arial, sans-serif"
  mono: "Menlo, Consolas, monospace"
  title_size: 34
  body_size: 17
layout:
  margins: [100, 80, 100, 60]
  grid_columns: 12
  spacing_scale: "compact"
```

---

## Spacing Scale

Spacing tokens for consistent rhythm across slides:

| Token | compact | standard | breathing |
|-------|---------|----------|-----------|
| `--space-xs` | 2px | 4px | 6px |
| `--space-sm` | 4px | 8px | 12px |
| `--space-md` | 8px | 16px | 24px |
| `--space-lg` | 16px | 24px | 36px |
| `--space-xl` | 24px | 32px | 48px |
| `--space-2xl` | 36px | 48px | 72px |

Use the `spacing_scale` value from the active theme to select the column.

---

## Typography Ladder

### Title Weight Rule
Larger text = lighter weight. Smaller text = heavier weight.

| Font Size | Weight | Use |
|-----------|--------|-----|
| ≥ 56px | 700 (Bold) | Cover titles, hero numbers |
| 42-55px | 600 (SemiBold) | Section headers |
| 32-41px | 600 (SemiBold) | Slide titles |
| 24-31px | 500 (Medium) | Subtitles |
| 18-23px | 400 (Regular) | Body text, bullets |
| 14-17px | 400 (Regular) | Captions, notes |
| ≤ 13px | 500 (Medium) | Metadata (small text needs weight to be readable) |

### Font Stack Fallback
Always include these fallbacks in font-family:
- Sans-serif titles: `"Inter", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif`
- Serif titles: `"Playfair Display", "Noto Serif SC", "SimSun", serif`
- Monospace: `"JetBrains Mono", "Cascadia Code", "Consolas", "Courier New", monospace`

---

## Layout Grid (12-column, 80px gutters)

```
X-axis divisions (960px total, 80px margins):
  col-0: 80-153    col-4: 373-446    col-8:  666-740
  col-1: 153-226   col-5: 446-520    col-9:  740-813
  col-2: 226-300   col-6: 520-593    col-10: 813-880
  col-3: 300-373   col-7: 593-666    col-11: (unused)

Safe text area: X 80..880, Y 40..520
Title band: Y 40..90
Content band: Y 100..480
Footer band: Y 490..520
```

---

## Image Standards

| Use Case | Ratio | Width | Height | object-position |
|----------|-------|-------|--------|-----------------|
| Cover / hero background | 21:9 | 960 | 410 | center center |
| Side image (text+image layout) | 16:10 | 400 | 250 | center center |
| Grid thumbnail (3-up) | 16:9 | 260 | 146 | top center |
| Portrait / profile | 3:4 | 160 | 213 | center top |
| Square accent | 1:1 | 200 | 200 | center center |

### Image Rules
- **Crop**: Always use `object-fit: cover` with `object-position: top center` for images taller than slot
- **Quality**: Images below 200px in the smaller dimension should not be enlarged
- **Placeholder**: Use `mock_image(width, height, description)` when no real image is available
- **Background**: Use a neutral fill behind images to prevent white-background surprises on dark slides

---

## Color Usage Rules

### Don't
- Mix warm and cool accent colors within the same deck
- Use `text_light` on light backgrounds below 14px (accessibility fail)
- Apply `accent` to body text — accent is for highlights, KPI numbers, and dividers only
- Use more than 3 distinct colors on a single slide (excluding neutral greys)

### Do
- Use `primary` for headers, dark backgrounds, card headers
- Use `secondary` for secondary card backgrounds, subtle fills
- Use `accent` for the ONE thing you want the audience to notice first
- Use `text` for body copy on the appropriate background
- Use `text_light` for metadata, captions, and secondary information

---

## Validation Checklist

Before finalizing a slide, verify:

- [ ] All colors match spec_lock values (grep for `#` and verify each hex)
- [ ] All font-family values include the required fallbacks
- [ ] Text elements are within safe area (X: 80-880, Y: 40-520)
- [ ] Images use standard ratios from the Image Standards table
- [ ] No text below 13px font size
- [ ] KPI numbers use `font-weight="900"`
- [ ] Slide title uses `font-weight="600"` or `"700"`
- [ ] Body text uses `font-weight="400"`
- [ ] Only ONE accent-colored element per slide (KPI numbers excluded)
- [ ] The layout used comes from `layout_library.md` (not invented)

## Working with the Layout Library

When using layouts from `layout_library.md`:

1. **Pick by purpose**, not by aesthetics — each layout has a "Use" and "Mood" tag
2. **Keep the viewBox**: `viewBox="0 0 960 540"` is the contract
3. **Replace slots, not structure**: change content between `<!-- SLOT: -->` markers, keep the HTML
4. **Inject spec values**: replace `var(--color-*, fallback)` with the hex from this spec_lock
5. **One layout per slide**: never mix skeletons from two different layouts

If no layout matches your need, prefer modifying the CLOSEST layout over inventing from scratch.
