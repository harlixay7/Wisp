---
name: ui-review
aliases:
  - interface-craft-audit
version: 4.0.0
description: >-
  Use when a UI change, screenshot set or running page needs review for craft
  and usability: hierarchy, typography, spacing, color and contrast,
  interaction states, motion, keyboard and screen-reader paths,
  responsiveness, and consistency with the existing design system. Produces a
  one-line design read, a screen-by-screen review table backed by renders and
  measurements, and findings with concrete CSS or markup fixes. Not for
  documentation text accuracy (use docs-accuracy-review),
  rendering performance (use performance-profiling), or UI code
  wiring and dead handlers (use wiring-audit).
brief: |
  Mission: judge whether the interface is well crafted and usable for its intended audience, with evidence from the rendered result, and return fixes as concrete CSS or markup.
  - Start with a one-line design read: product, audience, primary task, visual register, brand constraints. Judge every choice against it, not against a universal taste list.
  - Hierarchy and type: one clear focal point and primary action per region; a deliberate type scale with few sizes; comfortable line length and line height; tabular numerals for changing numbers.
  - Spacing on a consistent scale; related items grouped; shared edges aligned.
  - Color through tokens; text contrast meets WCAG AA (4.5:1, or 3:1 for large text); component boundaries, states and focus indicators meet 3:1; measure over the real composited background in every theme.
  - Every interactive element has hover, focus-visible, active and disabled states, plus loading, empty and error states where data is involved.
  - Motion is purposeful, consistent in duration and easing, interruptible, and reduced or removed under prefers-reduced-motion.
  - Native semantics, accessible names, logical tab order, no focus traps, Escape closes overlays; layout holds at 320 px width, 200% zoom and with long strings.
  - Generic AI-design tells and effects such as glass, glow or pills are findings only when they conflict with the brief or hurt legibility.
  Emit the design read, a screen-by-screen table and a contrast table before the findings.
  PASS: primary tasks work for keyboard and screen-reader users and craft matches the brief. PASS_WITH_FIXES: local defects with concrete fixes. BLOCK: a primary task is unusable by keyboard or assistive technology, primary text fails contrast, or layout breaks at a supported size.
activation_triggers:
  task_modes:
    - UI_CRAFT_REVIEW
    - VISUAL_QA
    - INTERFACE_ACCESSIBILITY_AUDIT
  keywords:
    - ui review
    - design read
    - color contrast
    - type scale
    - focus-visible
    - prefers-reduced-motion
    - design tokens
    - visual hierarchy
    - screenshot review
    - interface polish
    - responsive layout
  do_not_use_when:
    - The concern is whether documentation or help text is accurate (use docs-accuracy-review).
    - The concern is frame rate, long tasks or rendering cost (use performance-profiling).
    - The concern is whether UI handlers and components are wired to real code paths (use wiring-audit).
input_contract:
  requires_worktree: false
  required_inputs:
    - The UI change (diff, branch or files), screenshots, or a URL or command that runs the interface
  optional_inputs:
    - The product brief, brand guidelines or design system location (tokens, component library)
    - Supported viewports, themes, browsers and input methods
    - Before screenshots or the previous version for comparison
output_contract:
  sections:
    - Design read
    - Screen-by-screen review table
    - Contrast table
  findings: shared format
  verdict: shared verdict block
---

# Interface craft audit

## Mission

Tell the calling agent whether a UI change serves its audience and holds up under real
conditions (other themes, small screens, keyboard, slow data, reduced motion), and give
fixes precise enough to paste. An excellent review rests on renders and computed values, judged
against an explicit reading of the brief and the design system already in the repo. The two common failures are reviewing
source code instead of the rendered result, and presenting personal taste or a generic
ban list as defects.

## Inputs to establish first

- The brief: stated in the request, or inferred from the README, existing screens, brand
  assets and tokens. When inferred, say so and give the confidence.
- The design system: token files (global CSS custom properties, Tailwind config,
  theme objects), shared components, icon set, existing motion conventions. New code is
  judged first for consistency with these.
- Scope: which screens, components and states the change touches (from the diff), and
  which viewports, themes, browsers and input methods are supported.
- How to render (dev server, static file, desktop shell, or screenshots only). If nothing
  renders, findings that depend on computed values are medium confidence.

## Method

1. **Design read.** Write one line: `Design read: <product> for <audience> doing <primary
   task> | register: <for example dense operator tool, calm consumer app, playful
   companion> | brand constraints: <signature colors, effects, type> | implies:
   <density, motion budget, tone>`. Done when later judgments can cite it.
2. **Inventory the change.** Map the diff to screens, components and states; list tokens
   added or changed; find values that bypass tokens:
   `rg -n '#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|oklch\(|\b[0-9]+px\b' <changed style files>`
   compared against the token file. Done when every affected screen and state is listed.
3. **Render and capture.** For each affected screen: widths around 320 to 375, 768 and
   1280 to 1440 CSS px, every supported theme, 200% zoom, and the states the change
   affects. Playwright works well: `page.setViewportSize`, `page.emulateMedia({ colorScheme:
   'dark', reducedMotion: 'reduce' })`, `page.screenshot({ fullPage: true })`, and
   `page.evaluate` with `getComputedStyle` for exact values. Done when each screen has
   labeled captures or a stated reason it could not be rendered.
4. **Measure.** Contrast for each text and control pair from computed colors, composited
   over the actual background (alpha, gradients, images and translucent surfaces use the
   worst-case backdrop). Run axe-core (`@axe-core/playwright`) knowing it finds only part
   of the issues. Walk the keyboard: Tab and Shift+Tab through the screen, activate with
   Enter and Space, close with Escape, and record order, visibility of focus and any
   trap. Read accessible names from the accessibility tree. Done when the contrast table
   and keyboard transcript exist.
5. **Review against the checklist**, comparing with neighboring screens and existing
   components rather than with an ideal. Done when every screen row has a verdict.
6. **Write fixes.** Each finding carries a minimal CSS or markup diff using existing
   tokens; introduce a new token only when the scale truly lacks the value. Done when no
   finding is a bare opinion.

## Checklist

**Hierarchy and typography**
- One focal point per view and one visually dominant primary action per region;
  secondary and destructive actions styled distinctly.
- A real type scale: count distinct font sizes in the changed styles; more than six to
  eight in one product surface usually means no scale, as a rule of thumb. Sizes come
  from tokens or a consistent ratio; weight and color carry hierarchy as well as size.
- Body line length roughly 45 to 90 characters (`max-width` in `ch`); body line height
  around 1.4 to 1.6 and tighter for large headings; content text not below about 12 px.
- `font-variant-numeric: tabular-nums` for counters, timers, tables and live values so
  digits do not jitter.
- Truncation is deliberate: ellipsis with the full value available (title, tooltip,
  expansion), or wrapping with `overflow-wrap: anywhere` for long tokens such as paths.

**Spacing and alignment**
- Spacing values from the scale; related items closer than unrelated ones; equal padding
  inside components of the same kind.
- Shared edges and baselines across rows; icons optically centered with their text.

**Color and contrast**
- Semantic tokens (`--text-muted`, `--surface-raised`) rather than raw values in components.
- Text contrast: 4.5:1 for normal text, 3:1 for large text (about 24 px regular or 18.7 px
  bold) under WCAG 2.x 1.4.3. Muted and placeholder text are the usual failures, more often
  in dark themes.
- Non-text contrast (1.4.11): input borders, toggle states, icons that convey meaning and
  focus indicators at 3:1 against adjacent colors.
- Color is never the only signal of state or error (1.4.1); pair it with text, icon or shape.
- Dark theme designed, not inverted: raised surfaces lighter than the base, saturated
  accents toned down, shadows replaced by borders or lighter surfaces where they vanish.
- Translucent surfaces (frosted glass, overlays): contrast measured over the busiest
  realistic backdrop; `backdrop-filter` guarded with `@supports` and an opaque fallback.

**Interaction states**
- Hover styles inside `@media (hover: hover)` so touch devices do not get stuck states.
- Focus: `outline: none` without a `:focus-visible` replacement is a defect; the indicator
  is visible (2.4.7), meets 3:1, and is not hidden under sticky headers (2.4.11).
- Active or pressed feedback; disabled controls both styled and inert (`disabled`, or
  `aria-disabled="true"` with guarded handlers).
- Loading keeps dimensions stable (button width fixed while a spinner shows; skeletons
  match final layout) to avoid layout shift; long operations show progress or status.
- Empty states explain and offer the next action; error states sit next to the cause,
  are linked with `aria-describedby`, and preserve user input.

**Motion**
- Each animation has a job (feedback, orientation, relationship); decorative loops need the brief's backing.
- Consistent durations and easing from tokens; as a rule of thumb about 100 to 200 ms for
  small feedback and 200 to 400 ms for larger transitions; decelerating curves for entry.
- Animate `transform` and `opacity`; animating `width`, `height`, `top` or `left` forces
  layout every frame.
- Interruptible: reversing a hover or toggling mid-transition continues smoothly from the
  current state instead of jumping.
- `@media (prefers-reduced-motion: reduce)` removes parallax, large movement and auto-
  playing loops, keeping state changes as instant swaps or short fades. Auto-moving content
  longer than five seconds needs a pause control (2.2.2); nothing flashes more than three
  times per second (2.3.1).

**Semantics and keyboard**
- Buttons act, links navigate; no clickable `div` without role, tabindex and key handling.
- Icon-only buttons have an accessible name; decorative SVG has `aria-hidden="true"`;
  inputs have real labels (placeholders are not labels); headings form an outline.
- Dialogs move focus in, contain it, restore it on close, and close on Escape (native
  `<dialog>` with `showModal()` provides most of this); menus, tabs and listboxes follow
  the ARIA Authoring Practices keyboard patterns.
- Async status announced through a polite live region; target size at least 24 by 24 CSS
  px for pointer targets (2.5.8), larger for frequent touch actions.

**Responsiveness**
- Content reflows at 320 CSS px without horizontal scrolling (1.4.10); text spacing
  overrides (1.4.12) and 200% zoom do not clip text.
- `100dvh` instead of `100vh` where mobile browser chrome matters; media constrained with
  `max-width: 100%` and `aspect-ratio` to prevent layout shift.
- Long and localized strings: labels and buttons tested with text roughly a third longer,
  user-generated names, and numbers with many digits.

**Consistency and generic-AI tells**
- One icon family with consistent grid, stroke width and `currentColor`; emoji used as
  functional icons render differently per platform and break tone in most products.
- Radii, shadows, borders and elevation drawn from the existing scale; new components do
  not duplicate existing ones with slight differences.
- Common generated-UI defaults: indigo-to-violet gradients, a centered hero over three
  identical icon cards, one large radius on everything, glow on every surface, a single
  typeface at many sizes with no hierarchy, gradient text, filler copy such as "unlock the
  power of". Report one only when it contradicts the design read, reduces clarity or
  contrast, or diverges from the existing system. When such an effect is part of the
  brand (a glass overlay, a soft glow, pill controls), review its execution instead:
  legibility, fallbacks, consistency and rendering cost.

## Evidence standard

Screenshots labeled with screen, state, width, theme and zoom; computed style values
quoted from the browser; contrast ratios computed from relative luminance of the
composited colors actually rendered; axe output with rule IDs; a keyboard transcript
(Tab 1 lands on X, Tab 2 on Y, focus invisible on Z). Not evidence: taste statements,
contrast read from token values when opacity or backdrops change the result, or a
Lighthouse score as proof of accessibility.

## Severity guide

- P0: a primary task cannot be completed by keyboard or screen reader (unlabeled control,
  focus trap, focus lost after a dialog); primary content text far below AA (for example
  under 3:1); a supported viewport hides or overlaps the primary action; flashing content.
- P1: focus indicator removed or invisible; missing error or loading state on a primary
  flow; secondary text below AA; reduced-motion preference ignored for large movement;
  two conflicting primary-button styles; frequent targets below 24 px.
- P2: off-scale spacing or type; hover-only affordances; layout shift on load; minor
  inconsistency with existing components; a tell that conflicts with the brief.
- P3: optical alignment, easing and duration polish, copy tone.

## Skill-specific output

**Design read**: the one line from phase 1, with the confidence of any inferred element.

**Screen-by-screen review table** (the review matrix):

| Screen and state | Captured at | Works well | Issues (finding IDs) | Verdict |
| --- | --- | --- | --- | --- |

Captured at lists width, theme and zoom combinations. Verdict is OK, FIX or BLOCK for
that screen.

**Contrast table**: Element | Foreground | Background (composited) | Ratio | Required |
Pass; failing or near-threshold pairs plus one row per primary text style.

## Anti-patterns

- **Taste as a defect.** "Remove the glassmorphism" is not a finding. Tie every craft
  judgment to the design read, a measurement or the existing system.
- **Source-only review.** CSS that looks right can render wrong through inheritance,
  cascade order or container width. Render it, or lower confidence.
- **Automated scan as a pass.** axe and Lighthouse catch a minority of accessibility
  issues; the keyboard walkthrough and name check are mandatory.
- **Token-value contrast.** Measuring `--text-muted` against `--surface` while the surface
  is 70% transparent over an image. Measure the composited pixels.
- **Redesign instead of repair.** Stay within the change and the system when local fixes
  resolve the problem.
- **Happy path only.** Desktop, light theme, populated data, mouse. Check the other themes,
  small widths, empty and error data, keyboard and reduced motion.

## Done when

- [ ] The design read is written and later judgments cite it.
- [ ] Every affected screen and state has labeled captures, or a reason it could not be rendered.
- [ ] Contrast is measured on composited colors in every supported theme.
- [ ] A keyboard transcript and accessible-name check cover each primary task.
- [ ] Reduced motion, 320 px width and 200% zoom were checked where relevant.
- [ ] Every finding has a concrete CSS or markup fix using existing tokens.
