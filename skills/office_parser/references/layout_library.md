# SVG Slide Layout Library

> Paste-ready SVG skeletons for AI-driven PPT generation.
> Each layout is self-contained within a `<svg viewBox="0 0 960 540">` container.
> Slots marked `<!-- SLOT:name -->` are insertion points — replace the placeholder
> elements between the markers with real content.

## Usage

1. Read this file to pick a layout matching the slide's purpose
2. Copy the skeleton into your SVG output
3. Replace each `<!-- SLOT:name -->` block with real content
4. Reference `spec_lock.md` for colors, fonts, and spacing tokens
5. Keep the original `<svg>` attributes and viewBox intact

## Design Tokens

All layouts use these CSS custom properties (defined at `<svg>` root):

```
--color-primary, --color-secondary, --color-accent,
--color-bg, --color-text, --color-text-light,
--font-title, --font-body, --font-mono,
--space-xs(4), --space-sm(8), --space-md(16),
--space-lg(24), --space-xl(32), --space-2xl(48)
```

If spec_lock values are not injected, layouts use the fallback values shown.

---

## L01 — Cover Dark

**Use**: Opening slide. Bold statement with subtitle and date.
**Mood**: Dramatic, confident.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="bg-cover" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="var(--color-primary, #1a1a2e)"/>
      <stop offset="100%" stop-color="var(--color-secondary, #0f0f1a)"/>
    </linearGradient>
  </defs>
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="url(#bg-cover)"/>
  <!-- /SLOT -->

  <!-- SLOT:title -->
  <text x="80" y="200" font-family="var(--font-title, sans-serif)" font-size="56"
        font-weight="700" fill="var(--color-text, #ffffff)" letter-spacing="-1">
    Presentation Title Here
  </text>
  <!-- /SLOT -->

  <!-- SLOT:subtitle -->
  <text x="80" y="270" font-family="var(--font-body, sans-serif)" font-size="24"
        font-weight="300" fill="var(--color-text-light, #a0a0b0)">
    Subtitle or tagline goes here
  </text>
  <!-- /SLOT -->

  <!-- SLOT:meta -->
  <text x="80" y="460" font-family="var(--font-body, sans-serif)" font-size="14"
        fill="var(--color-text-light, #606070)">
    Presenter Name · Date · Event
  </text>
  <!-- /SLOT -->
</svg>
```

---

## L02 — Cover Light

**Use**: Clean, minimal opening. White/light background.
**Mood**: Professional, modern.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #fafafa)"/>
  <!-- /SLOT -->
  <!-- SLOT:accent-bar -->
  <rect x="80" y="180" width="80" height="4" fill="var(--color-accent, #4c9aff)"/>
  <!-- /SLOT -->

  <!-- SLOT:title -->
  <text x="80" y="230" font-family="var(--font-title, sans-serif)" font-size="52"
        font-weight="700" fill="var(--color-text, #1a1a2e)">
    Presentation Title
  </text>
  <!-- /SLOT -->

  <!-- SLOT:subtitle -->
  <text x="80" y="290" font-family="var(--font-body, sans-serif)" font-size="22"
        font-weight="300" fill="var(--color-text-light, #606070)">
    Subtitle or description goes here
  </text>
  <!-- /SLOT -->

  <!-- SLOT:meta -->
  <text x="80" y="460" font-family="var(--font-body, sans-serif)" font-size="14"
        fill="var(--color-text-light, #909090)">
    Author &amp; Date
  </text>
  <!-- /SLOT -->
</svg>
```

---

## L03 — Section Divider

**Use**: Transition between major sections. Large number + heading.
**Mood**: Rhythmic, structural.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #1a1a2e)"/>
  <!-- /SLOT -->

  <!-- SLOT:section-number -->
  <text x="80" y="320" font-family="var(--font-title, sans-serif)" font-size="120"
        font-weight="900" fill="var(--color-accent, #4c9aff)" opacity="0.3">
    01
  </text>
  <!-- /SLOT -->

  <!-- SLOT:section-title -->
  <text x="80" y="200" font-family="var(--font-title, sans-serif)" font-size="42"
        font-weight="700" fill="var(--color-text, #ffffff)">
    Section Title
  </text>
  <!-- /SLOT -->

  <!-- SLOT:section-subtitle -->
  <text x="80" y="250" font-family="var(--font-body, sans-serif)" font-size="18"
        font-weight="300" fill="var(--color-text-light, #a0a0b0)">
    Brief section description
  </text>
  <!-- /SLOT -->
</svg>
```

---

## L04 — Title + Body

**Use**: Standard content slide. Heading with bullet points or paragraphs.
**Mood**: Clear, informational.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #fafafa)"/>
  <!-- /SLOT -->

  <!-- SLOT:slide-title -->
  <text x="80" y="80" font-family="var(--font-title, sans-serif)" font-size="36"
        font-weight="700" fill="var(--color-text, #1a1a2e)">
    Slide Title
  </text>
  <!-- /SLOT -->

  <!-- SLOT:body-text -->
  <text x="80" y="140" font-family="var(--font-body, sans-serif)" font-size="18"
        fill="var(--color-text, #333344)" line-height="1.6">
    <tspan x="80" dy="0">• First bullet point or paragraph text</tspan>
    <tspan x="80" dy="32">• Second bullet point with key information</tspan>
    <tspan x="80" dy="32">• Third bullet point highlighting a detail</tspan>
    <tspan x="80" dy="32">• Fourth bullet point if needed</tspan>
  </text>
  <!-- /SLOT -->
</svg>
```

---

## L05 — Big Number (KPI Hero)

**Use**: Single critical metric. One number that tells the story.
**Mood**: Impactful, data-driven.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #1a1a2e)"/>
  <!-- /SLOT -->
  <!-- SLOT:accent-circle -->
  <circle cx="480" cy="250" r="140" fill="var(--color-accent, #4c9aff)" opacity="0.08"/>
  <!-- /SLOT -->

  <!-- SLOT:kpi-value -->
  <text x="480" y="240" font-family="var(--font-title, sans-serif)" font-size="96"
        font-weight="900" fill="var(--color-text, #ffffff)" text-anchor="middle">
    87%
  </text>
  <!-- /SLOT -->

  <!-- SLOT:kpi-label -->
  <text x="480" y="300" font-family="var(--font-body, sans-serif)" font-size="20"
        font-weight="400" fill="var(--color-text-light, #a0a0b0)" text-anchor="middle">
    Metric Label / What This Number Means
  </text>
  <!-- /SLOT -->

  <!-- SLOT:kpi-note -->
  <text x="480" y="350" font-family="var(--font-body, sans-serif)" font-size="14"
        fill="var(--color-text-light, #606070)" text-anchor="middle">
    Source / Time Period / Comparison
  </text>
  <!-- /SLOT -->
</svg>
```

---

## L06 — KPI Grid 3×1

**Use**: Three key metrics side-by-side.
**Mood**: Dashboard, overview.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #fafafa)"/>
  <!-- /SLOT -->

  <!-- SLOT:slide-title -->
  <text x="80" y="70" font-family="var(--font-title, sans-serif)" font-size="32"
        font-weight="700" fill="var(--color-text, #1a1a2e)">
    Key Metrics
  </text>
  <!-- /SLOT -->

  <!-- Metric 1 -->
  <!-- SLOT:kpi1 -->
  <rect x="80" y="130" width="240" height="320" rx="12" fill="var(--color-bg, #ffffff)" stroke="var(--color-text-light, #e0e0e0)" stroke-width="1"/>
  <text x="200" y="250" font-family="var(--font-title, sans-serif)" font-size="56"
        font-weight="900" fill="var(--color-accent, #4c9aff)" text-anchor="middle">3.2M</text>
  <text x="200" y="300" font-family="var(--font-body, sans-serif)" font-size="16"
        fill="var(--color-text, #333)" text-anchor="middle">Active Users</text>
  <text x="200" y="340" font-family="var(--font-body, sans-serif)" font-size="13"
        fill="var(--color-text-light, #909090)" text-anchor="middle">+12% vs last quarter</text>
  <!-- /SLOT -->

  <!-- Metric 2 -->
  <!-- SLOT:kpi2 -->
  <rect x="360" y="130" width="240" height="320" rx="12" fill="var(--color-bg, #ffffff)" stroke="var(--color-text-light, #e0e0e0)" stroke-width="1"/>
  <text x="480" y="250" font-family="var(--font-title, sans-serif)" font-size="56"
        font-weight="900" fill="var(--color-accent, #4c9aff)" text-anchor="middle">98.7%</text>
  <text x="480" y="300" font-family="var(--font-body, sans-serif)" font-size="16"
        fill="var(--color-text, #333)" text-anchor="middle">Uptime SLA</text>
  <text x="480" y="340" font-family="var(--font-body, sans-serif)" font-size="13"
        fill="var(--color-text-light, #909090)" text-anchor="middle">Target: 99.9%</text>
  <!-- /SLOT -->

  <!-- Metric 3 -->
  <!-- SLOT:kpi3 -->
  <rect x="640" y="130" width="240" height="320" rx="12" fill="var(--color-bg, #ffffff)" stroke="var(--color-text-light, #e0e0e0)" stroke-width="1"/>
  <text x="760" y="250" font-family="var(--font-title, sans-serif)" font-size="56"
        font-weight="900" fill="var(--color-accent, #4c9aff)" text-anchor="middle">¥4.8M</text>
  <text x="760" y="300" font-family="var(--font-body, sans-serif)" font-size="16"
        fill="var(--color-text, #333)" text-anchor="middle">Revenue</text>
  <text x="760" y="340" font-family="var(--font-body, sans-serif)" font-size="13"
        fill="var(--color-text-light, #909090)" text-anchor="middle">Q2 2026</text>
  <!-- /SLOT -->
</svg>
```

---

## L07 — KPI Grid 2×2

**Use**: Four metrics in a 2-by-2 grid.
**Mood**: Compact dashboard.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #1a1a2e)"/>
  <!-- /SLOT -->

  <!-- Grid: 2 cols × 2 rows, each cell 400×210, gap 20 -->
  <!-- Cell (0,0) -->
  <!-- SLOT:kpi-tl -->
  <rect x="60" y="60" width="410" height="220" rx="10" fill="var(--color-bg, #222240)" opacity="0.6"/>
  <text x="265" y="150" font-family="var(--font-title, sans-serif)" font-size="64"
        font-weight="900" fill="var(--color-accent, #4c9aff)" text-anchor="middle">+240%</text>
  <text x="265" y="210" font-family="var(--font-body, sans-serif)" font-size="16"
        fill="var(--color-text, #ffffff)" text-anchor="middle">Growth Rate</text>
  <!-- /SLOT -->

  <!-- Cell (1,0) -->
  <!-- SLOT:kpi-tr -->
  <rect x="490" y="60" width="410" height="220" rx="10" fill="var(--color-bg, #222240)" opacity="0.6"/>
  <text x="695" y="150" font-family="var(--font-title, sans-serif)" font-size="64"
        font-weight="900" fill="var(--color-accent, #4c9aff)" text-anchor="middle">50K</text>
  <text x="695" y="210" font-family="var(--font-body, sans-serif)" font-size="16"
        fill="var(--color-text, #ffffff)" text-anchor="middle">Daily Active Users</text>
  <!-- /SLOT -->

  <!-- Cell (0,1) -->
  <!-- SLOT:kpi-bl -->
  <rect x="60" y="300" width="410" height="220" rx="10" fill="var(--color-bg, #222240)" opacity="0.6"/>
  <text x="265" y="390" font-family="var(--font-title, sans-serif)" font-size="64"
        font-weight="900" fill="var(--color-accent, #4c9aff)" text-anchor="middle">4.2min</text>
  <text x="265" y="450" font-family="var(--font-body, sans-serif)" font-size="16"
        fill="var(--color-text, #ffffff)" text-anchor="middle">Avg. Session Duration</text>
  <!-- /SLOT -->

  <!-- Cell (1,1) -->
  <!-- SLOT:kpi-br -->
  <rect x="490" y="300" width="410" height="220" rx="10" fill="var(--color-bg, #222240)" opacity="0.6"/>
  <text x="695" y="390" font-family="var(--font-title, sans-serif)" font-size="64"
        font-weight="900" fill="var(--color-accent, #4c9aff)" text-anchor="middle">NPS 72</text>
  <text x="695" y="450" font-family="var(--font-body, sans-serif)" font-size="16"
        fill="var(--color-text, #ffffff)" text-anchor="middle">Net Promoter Score</text>
  <!-- /SLOT -->
</svg>
```

---

## L08 — Title + Full Image (21:9 Hero)

**Use**: Title overlay on a full-width cinematic image.
**Mood**: Visual, immersive.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #1a1a2e)"/>
  <!-- /SLOT -->

  <!-- SLOT:image -->
  <image x="0" y="0" width="960" height="410" preserveAspectRatio="xMidYMid slice"
         href="placeholder_21x9.jpg"/>
  <!-- Overlay gradient for text readability -->
  <rect x="0" y="280" width="960" height="130" fill="url(#img-overlay)"/>
  <!-- /SLOT -->

  <defs>
    <linearGradient id="img-overlay" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#000000" stop-opacity="0"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0.6"/>
    </linearGradient>
  </defs>

  <!-- SLOT:slide-title -->
  <text x="80" y="480" font-family="var(--font-title, sans-serif)" font-size="32"
        font-weight="700" fill="var(--color-text, #ffffff)">
    Slide Title Over Image
  </text>
  <!-- /SLOT -->

  <!-- SLOT:caption -->
  <text x="80" y="510" font-family="var(--font-body, sans-serif)" font-size="14"
        fill="var(--color-text-light, #a0a0b0)">
    Image source or caption
  </text>
  <!-- /SLOT -->
</svg>
```

---

## L09 — Title + Image (Left Text, Right Image)

**Use**: Explanatory text on the left, supporting image on the right.
**Mood**: Balanced, explanatory.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #fafafa)"/>
  <!-- /SLOT -->

  <!-- SLOT:slide-title -->
  <text x="80" y="70" font-family="var(--font-title, sans-serif)" font-size="32"
        font-weight="700" fill="var(--color-text, #1a1a2e)">
    Topic Title
  </text>
  <!-- /SLOT -->

  <!-- SLOT:body-text -->
  <text x="80" y="130" font-family="var(--font-body, sans-serif)" font-size="17"
        fill="var(--color-text, #333344)">
    <tspan x="80" dy="0">Key insight or explanation text.</tspan>
    <tspan x="80" dy="28">Supporting detail that reinforces the message.</tspan>
    <tspan x="80" dy="28">Additional context or data point.</tspan>
    <tspan x="80" dy="28">Takeaway or call to action.</tspan>
  </text>
  <!-- /SLOT -->

  <!-- SLOT:image -->
  <rect x="540" y="100" width="360" height="360" rx="8" fill="var(--color-text-light, #e0e0e0)"/>
  <image x="540" y="100" width="360" height="360" rx="8"
         preserveAspectRatio="xMidYMid slice" href="placeholder_16x9.jpg"/>
  <text x="720" y="290" font-family="var(--font-body, sans-serif)" font-size="13"
        fill="var(--color-text-light, #909090)" text-anchor="middle">[Image]</text>
  <!-- /SLOT -->
</svg>
```

---

## L10 — Image Grid (3-up)

**Use**: Three images side-by-side with captions.
**Mood**: Visual gallery, showcase.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #fafafa)"/>
  <!-- /SLOT -->

  <!-- SLOT:slide-title -->
  <text x="80" y="60" font-family="var(--font-title, sans-serif)" font-size="28"
        font-weight="700" fill="var(--color-text, #1a1a2e)">
    Gallery Title
  </text>
  <!-- /SLOT -->

  <!-- Image 1 -->
  <!-- SLOT:img1 -->
  <rect x="60" y="100" width="260" height="260" rx="6" fill="var(--color-text-light, #e0e0e0)"/>
  <image x="60" y="100" width="260" height="200" rx="6"
         preserveAspectRatio="xMidYMid slice" href="img1.jpg"/>
  <text x="190" y="340" font-family="var(--font-body, sans-serif)" font-size="14"
        font-weight="600" fill="var(--color-text, #333)" text-anchor="middle">Image One</text>
  <text x="190" y="362" font-family="var(--font-body, sans-serif)" font-size="12"
        fill="var(--color-text-light, #909090)" text-anchor="middle">Brief caption</text>
  <!-- /SLOT -->

  <!-- Image 2 -->
  <!-- SLOT:img2 -->
  <rect x="350" y="100" width="260" height="260" rx="6" fill="var(--color-text-light, #e0e0e0)"/>
  <image x="350" y="100" width="260" height="200" rx="6"
         preserveAspectRatio="xMidYMid slice" href="img2.jpg"/>
  <text x="480" y="340" font-family="var(--font-body, sans-serif)" font-size="14"
        font-weight="600" fill="var(--color-text, #333)" text-anchor="middle">Image Two</text>
  <text x="480" y="362" font-family="var(--font-body, sans-serif)" font-size="12"
        fill="var(--color-text-light, #909090)" text-anchor="middle">Brief caption</text>
  <!-- /SLOT -->

  <!-- Image 3 -->
  <!-- SLOT:img3 -->
  <rect x="640" y="100" width="260" height="260" rx="6" fill="var(--color-text-light, #e0e0e0)"/>
  <image x="640" y="100" width="260" height="200" rx="6"
         preserveAspectRatio="xMidYMid slice" href="img3.jpg"/>
  <text x="770" y="340" font-family="var(--font-body, sans-serif)" font-size="14"
        font-weight="600" fill="var(--color-text, #333)" text-anchor="middle">Image Three</text>
  <text x="770" y="362" font-family="var(--font-body, sans-serif)" font-size="12"
        fill="var(--color-text-light, #909090)" text-anchor="middle">Brief caption</text>
  <!-- /SLOT -->
</svg>
```

---

## L11 — Timeline (Horizontal)

**Use**: Chronological events, roadmap, process flow.
**Mood**: Narrative, progressive.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #fafafa)"/>
  <!-- /SLOT -->

  <!-- SLOT:slide-title -->
  <text x="80" y="60" font-family="var(--font-title, sans-serif)" font-size="28"
        font-weight="700" fill="var(--color-text, #1a1a2e)">
    Timeline / Roadmap
  </text>
  <!-- /SLOT -->

  <!-- Timeline spine -->
  <line x1="80" y1="280" x2="880" y2="280" stroke="var(--color-accent, #4c9aff)" stroke-width="2"/>

  <!-- Node 1 -->
  <!-- SLOT:node1 -->
  <circle cx="160" cy="280" r="10" fill="var(--color-accent, #4c9aff)"/>
  <text x="160" y="240" font-family="var(--font-title, sans-serif)" font-size="16"
        font-weight="700" fill="var(--color-text, #1a1a2e)" text-anchor="middle">2024 Q1</text>
  <text x="160" y="320" font-family="var(--font-body, sans-serif)" font-size="14"
        fill="var(--color-text, #333)" text-anchor="middle">Milestone description one</text>
  <!-- /SLOT -->

  <!-- Node 2 -->
  <!-- SLOT:node2 -->
  <circle cx="400" cy="280" r="10" fill="var(--color-accent, #4c9aff)"/>
  <text x="400" y="240" font-family="var(--font-title, sans-serif)" font-size="16"
        font-weight="700" fill="var(--color-text, #1a1a2e)" text-anchor="middle">2024 Q3</text>
  <text x="400" y="320" font-family="var(--font-body, sans-serif)" font-size="14"
        fill="var(--color-text, #333)" text-anchor="middle">Milestone description two</text>
  <!-- /SLOT -->

  <!-- Node 3 -->
  <!-- SLOT:node3 -->
  <circle cx="640" cy="280" r="10" fill="var(--color-accent, #4c9aff)"/>
  <text x="640" y="240" font-family="var(--font-title, sans-serif)" font-size="16"
        font-weight="700" fill="var(--color-text, #1a1a2e)" text-anchor="middle">2025 Q1</text>
  <text x="640" y="320" font-family="var(--font-body, sans-serif)" font-size="14"
        fill="var(--color-text, #333)" text-anchor="middle">Milestone description three</text>
  <!-- /SLOT -->

  <!-- Node 4 -->
  <!-- SLOT:node4 -->
  <circle cx="880" cy="280" r="10" fill="var(--color-accent, #4c9aff)" opacity="0.3"/>
  <text x="880" y="240" font-family="var(--font-title, sans-serif)" font-size="16"
        font-weight="700" fill="var(--color-text-light, #909090)" text-anchor="middle">2025 Q3</text>
  <text x="880" y="320" font-family="var(--font-body, sans-serif)" font-size="14"
        fill="var(--color-text-light, #909090)" text-anchor="middle">Upcoming milestone</text>
  <!-- /SLOT -->
</svg>
```

---

## L12 — Two Column Comparison

**Use**: Side-by-side comparison (before/after, pro/con, old/new).
**Mood**: Analytical, comparative.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #fafafa)"/>
  <!-- /SLOT -->

  <!-- SLOT:slide-title -->
  <text x="80" y="60" font-family="var(--font-title, sans-serif)" font-size="28"
        font-weight="700" fill="var(--color-text, #1a1a2e)">
    Comparison Title
  </text>
  <!-- /SLOT -->

  <!-- Left column -->
  <!-- SLOT:col-left -->
  <rect x="60" y="100" width="400" height="400" rx="10" fill="var(--color-bg, #ffffff)" stroke="var(--color-text-light, #e0e0e0)" stroke-width="1"/>
  <rect x="60" y="100" width="400" height="48" rx="10" fill="var(--color-primary, #1a1a2e)"/>
  <rect x="60" y="138" width="400" height="10" fill="var(--color-primary, #1a1a2e)"/>
  <text x="260" y="130" font-family="var(--font-title, sans-serif)" font-size="20"
        font-weight="700" fill="var(--color-text, #ffffff)" text-anchor="middle">Option A</text>
  <text x="90" y="190" font-family="var(--font-body, sans-serif)" font-size="16"
        fill="var(--color-text, #333)">
    <tspan x="90" dy="0">• Key point one about Option A</tspan>
    <tspan x="90" dy="28">• Key point two about Option A</tspan>
    <tspan x="90" dy="28">• Key point three about Option A</tspan>
  </text>
  <!-- /SLOT -->

  <!-- Right column -->
  <!-- SLOT:col-right -->
  <rect x="500" y="100" width="400" height="400" rx="10" fill="var(--color-bg, #ffffff)" stroke="var(--color-text-light, #e0e0e0)" stroke-width="1"/>
  <rect x="500" y="100" width="400" height="48" rx="10" fill="var(--color-secondary, #0f0f1a)"/>
  <rect x="500" y="138" width="400" height="10" fill="var(--color-secondary, #0f0f1a)"/>
  <text x="700" y="130" font-family="var(--font-title, sans-serif)" font-size="20"
        font-weight="700" fill="var(--color-text, #ffffff)" text-anchor="middle">Option B</text>
  <text x="530" y="190" font-family="var(--font-body, sans-serif)" font-size="16"
        fill="var(--color-text, #333)">
    <tspan x="530" dy="0">• Key point one about Option B</tspan>
    <tspan x="530" dy="28">• Key point two about Option B</tspan>
    <tspan x="530" dy="28">• Key point three about Option B</tspan>
  </text>
  <!-- /SLOT -->
</svg>
```

---

## L13 — Quote

**Use**: Prominent quote with attribution.
**Mood**: Emotional, authoritative.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #1a1a2e)"/>
  <!-- /SLOT -->

  <!-- SLOT:quote-mark -->
  <text x="480" y="160" font-family="var(--font-title, sans-serif)" font-size="120"
        font-weight="900" fill="var(--color-accent, #4c9aff)" opacity="0.15" text-anchor="middle">
    "
  </text>
  <!-- /SLOT -->

  <!-- SLOT:quote-text -->
  <text x="480" y="250" font-family="var(--font-title, sans-serif)" font-size="28"
        font-weight="300" fill="var(--color-text, #ffffff)" text-anchor="middle"
        font-style="italic">
    <tspan x="480" dy="0">"The best way to predict the future</tspan>
    <tspan x="480" dy="40">is to create it."</tspan>
  </text>
  <!-- /SLOT -->

  <!-- SLOT:quote-attribution -->
  <text x="480" y="360" font-family="var(--font-body, sans-serif)" font-size="18"
        font-weight="400" fill="var(--color-text-light, #a0a0b0)" text-anchor="middle">
    — Author Name, Title / Organization
  </text>
  <!-- /SLOT -->
</svg>
```

---

## L14 — Chart + Explanation

**Use**: Chart/visualization on the left, explanation text on the right.
**Mood**: Data storytelling.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #fafafa)"/>
  <!-- /SLOT -->

  <!-- SLOT:slide-title -->
  <text x="80" y="60" font-family="var(--font-title, sans-serif)" font-size="28"
        font-weight="700" fill="var(--color-text, #1a1a2e)">
    Data Insight Title
  </text>
  <!-- /SLOT -->

  <!-- Chart area (left) -->
  <!-- SLOT:chart -->
  <rect x="60" y="100" width="440" height="380" rx="8" fill="var(--color-bg, #ffffff)"
        stroke="var(--color-text-light, #e8e8e8)" stroke-width="1"/>
  <!-- Bar chart example — replace with real chart SVG -->
  <rect x="100" y="300" width="60" height="140" rx="3" fill="var(--color-accent, #4c9aff)"/>
  <rect x="190" y="240" width="60" height="200" rx="3" fill="var(--color-accent, #4c9aff)"/>
  <rect x="280" y="180" width="60" height="260" rx="3" fill="var(--color-accent, #4c9aff)"/>
  <rect x="370" y="280" width="60" height="160" rx="3" fill="var(--color-accent, #4c9aff)"/>
  <text x="130" y="470" font-family="var(--font-body, sans-serif)" font-size="12"
        fill="var(--color-text-light, #909090)" text-anchor="middle">Q1</text>
  <text x="220" y="470" font-family="var(--font-body, sans-serif)" font-size="12"
        fill="var(--color-text-light, #909090)" text-anchor="middle">Q2</text>
  <text x="310" y="470" font-family="var(--font-body, sans-serif)" font-size="12"
        fill="var(--color-text-light, #909090)" text-anchor="middle">Q3</text>
  <text x="400" y="470" font-family="var(--font-body, sans-serif)" font-size="12"
        fill="var(--color-text-light, #909090)" text-anchor="middle">Q4</text>
  <!-- /SLOT -->

  <!-- Explanation (right) -->
  <!-- SLOT:explanation -->
  <text x="540" y="130" font-family="var(--font-body, sans-serif)" font-size="17"
        fill="var(--color-text, #333)">
    <tspan x="540" dy="0" font-weight="700">Key Takeaway</tspan>
    <tspan x="540" dy="32">What this data shows and why</tspan>
    <tspan x="540" dy="24">it matters to the audience.</tspan>
    <tspan x="540" dy="48" font-weight="700">Insight 2</tspan>
    <tspan x="540" dy="32">Secondary observation or trend</tspan>
    <tspan x="540" dy="24">worth highlighting.</tspan>
    <tspan x="540" dy="48" font-weight="700">Action</tspan>
    <tspan x="540" dy="32">Recommended next step based</tspan>
    <tspan x="540" dy="24">on this analysis.</tspan>
  </text>
  <!-- /SLOT -->
</svg>
```

---

## L15 — Profile Cards (4-up)

**Use**: Team introduction, speaker bios, persona profiles.
**Mood**: Personal, human.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #fafafa)"/>
  <!-- /SLOT -->

  <!-- SLOT:slide-title -->
  <text x="80" y="55" font-family="var(--font-title, sans-serif)" font-size="26"
        font-weight="700" fill="var(--color-text, #1a1a2e)">
    Team / People
  </text>
  <!-- /SLOT -->

  <!-- Card 1 -->
  <!-- SLOT:card1 -->
  <rect x="40" y="90" width="200" height="280" rx="10" fill="var(--color-bg, #ffffff)" stroke="var(--color-text-light, #e8e8e8)" stroke-width="1"/>
  <circle cx="140" cy="150" r="40" fill="var(--color-text-light, #e0e0e0)"/>
  <text x="140" y="210" font-family="var(--font-title, sans-serif)" font-size="16"
        font-weight="700" fill="var(--color-text, #333)" text-anchor="middle">Name One</text>
  <text x="140" y="235" font-family="var(--font-body, sans-serif)" font-size="13"
        fill="var(--color-accent, #4c9aff)" text-anchor="middle">Role / Title</text>
  <text x="140" y="270" font-family="var(--font-body, sans-serif)" font-size="12"
        fill="var(--color-text-light, #909090)" text-anchor="middle">
    <tspan x="140" dy="0">Brief description or</tspan>
    <tspan x="140" dy="18">key contribution.</tspan>
  </text>
  <!-- /SLOT -->

  <!-- Card 2 -->
  <!-- SLOT:card2 -->
  <rect x="260" y="90" width="200" height="280" rx="10" fill="var(--color-bg, #ffffff)" stroke="var(--color-text-light, #e8e8e8)" stroke-width="1"/>
  <circle cx="360" cy="150" r="40" fill="var(--color-text-light, #e0e0e0)"/>
  <text x="360" y="210" font-family="var(--font-title, sans-serif)" font-size="16"
        font-weight="700" fill="var(--color-text, #333)" text-anchor="middle">Name Two</text>
  <text x="360" y="235" font-family="var(--font-body, sans-serif)" font-size="13"
        fill="var(--color-accent, #4c9aff)" text-anchor="middle">Role / Title</text>
  <text x="360" y="270" font-family="var(--font-body, sans-serif)" font-size="12"
        fill="var(--color-text-light, #909090)" text-anchor="middle">
    <tspan x="360" dy="0">Brief description or</tspan>
    <tspan x="360" dy="18">key contribution.</tspan>
  </text>
  <!-- /SLOT -->

  <!-- Card 3 -->
  <!-- SLOT:card3 -->
  <rect x="480" y="90" width="200" height="280" rx="10" fill="var(--color-bg, #ffffff)" stroke="var(--color-text-light, #e8e8e8)" stroke-width="1"/>
  <circle cx="580" cy="150" r="40" fill="var(--color-text-light, #e0e0e0)"/>
  <text x="580" y="210" font-family="var(--font-title, sans-serif)" font-size="16"
        font-weight="700" fill="var(--color-text, #333)" text-anchor="middle">Name Three</text>
  <text x="580" y="235" font-family="var(--font-body, sans-serif)" font-size="13"
        fill="var(--color-accent, #4c9aff)" text-anchor="middle">Role / Title</text>
  <text x="580" y="270" font-family="var(--font-body, sans-serif)" font-size="12"
        fill="var(--color-text-light, #909090)" text-anchor="middle">
    <tspan x="580" dy="0">Brief description or</tspan>
    <tspan x="580" dy="18">key contribution.</tspan>
  </text>
  <!-- /SLOT -->

  <!-- Card 4 -->
  <!-- SLOT:card4 -->
  <rect x="700" y="90" width="200" height="280" rx="10" fill="var(--color-bg, #ffffff)" stroke="var(--color-text-light, #e8e8e8)" stroke-width="1"/>
  <circle cx="800" cy="150" r="40" fill="var(--color-text-light, #e0e0e0)"/>
  <text x="800" y="210" font-family="var(--font-title, sans-serif)" font-size="16"
        font-weight="700" fill="var(--color-text, #333)" text-anchor="middle">Name Four</text>
  <text x="800" y="235" font-family="var(--font-body, sans-serif)" font-size="13"
        fill="var(--color-accent, #4c9aff)" text-anchor="middle">Role / Title</text>
  <text x="800" y="270" font-family="var(--font-body, sans-serif)" font-size="12"
        fill="var(--color-text-light, #909090)" text-anchor="middle">
    <tspan x="800" dy="0">Brief description or</tspan>
    <tspan x="800" dy="18">key contribution.</tspan>
  </text>
  <!-- /SLOT -->
</svg>
```

---

## L16 — Closing / Thank You

**Use**: Final slide with CTA and contact information.
**Mood**: Conclusive, actionable.

```svg
<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">
  <!-- SLOT:background -->
  <rect width="960" height="540" fill="var(--color-bg, #1a1a2e)"/>
  <!-- /SLOT -->

  <!-- SLOT:closing-text -->
  <text x="480" y="200" font-family="var(--font-title, sans-serif)" font-size="56"
        font-weight="700" fill="var(--color-text, #ffffff)" text-anchor="middle">
    Thank You
  </text>
  <!-- /SLOT -->

  <!-- SLOT:cta -->
  <text x="480" y="270" font-family="var(--font-body, sans-serif)" font-size="20"
        fill="var(--color-text-light, #a0a0b0)" text-anchor="middle">
    Questions? Let's connect.
  </text>
  <!-- /SLOT -->

  <!-- SLOT:contact -->
  <text x="480" y="380" font-family="var(--font-body, sans-serif)" font-size="16"
        fill="var(--color-text-light, #707080)" text-anchor="middle">
    <tspan x="480" dy="0">email@example.com</tspan>
    <tspan x="480" dy="28">@handle · website.com</tspan>
  </text>
  <!-- /SLOT -->
</svg>
```

---

## How to Use This Library

1. **Pick a layout** matching the slide's purpose
2. **Read `spec_lock.md`** for the current design tokens
3. **Copy the SVG skeleton**, keeping `viewBox="0 0 960 540"` intact
4. **Replace each `<!-- SLOT:* -->` block** with real content
5. **Apply spec colors** by replacing `var(--color-*, fallback)` with actual hex values from spec_lock
6. **Validate**: keep text between 80-880 X range, 40-520 Y range

## Image Ratios

| Slot Type | Ratio | SVG Width | SVG Height |
|-----------|-------|-----------|-------------|
| Hero / full-width | 21:9 | 960 | 410 |
| Side image | 16:10 | 360 | 225 |
| Grid image | 16:9 | varies | varies |
| Portrait accent | 3:4 | 160 | 213 |

## Anti-Drift Rules

- **Never** change the viewBox
- **Never** use custom hex colors — only spec_lock values
- **Never** place text outside 80-880 X × 40-520 Y safe area
- **Never** use fonts outside the spec_lock font family
- KPI numbers use `font-weight="900"`, titles use `700`, body uses `400`
