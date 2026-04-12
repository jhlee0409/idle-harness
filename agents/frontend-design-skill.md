# Frontend Design Skill

Guide for creating distinctive, production-grade frontend interfaces that avoid generic AI aesthetics.

## Design Thinking

Before defining the visual design language, commit to a **BOLD aesthetic direction**:

- **Purpose**: What problem does this interface solve? Who uses it?
- **Tone**: Pick ONE direction and commit fully. See the direction catalog below.
- **Constraints**: Technical requirements (framework, performance, accessibility)
- **Differentiation**: What makes this UNFORGETTABLE? What's the one visual element someone will remember?

**Choose a clear conceptual direction and execute it with precision.** Bold maximalism and refined minimalism both work — the key is intentionality, not intensity.

## Direction Catalog

Pick ONE. Each direction includes what it looks like, what it does NOT look like, and a concrete reference so two different designers would converge on similar output.

### Light & Warm
- **Analog journal** — Cream/parchment backgrounds, hand-drawn borders, stamp-like icons, ink-wash textures. Think Kinfolk magazine or Field Notes. NOT: dark mode, neon accents, geometric patterns.
- **Soft pastel** — Muted pinks, lavenders, sage greens. Rounded shapes, gentle gradients, playful illustrations. Think Notion's lighter side or Linear's pastel mode. NOT: high contrast, sharp corners, bold type.
- **Clean editorial** — Lots of white space, strong typographic hierarchy, minimal color. Think Bloomberg Businessweek or Stripe's docs. NOT: textures, gradients, decorative elements.

### Dark & Rich
- **Luxury dark** — Deep blacks (#0A0A0A), gold/champagne accents, serif display fonts, noise grain. Think a private members' club app. NOT: neon, gradients, playful elements.
- **Neon terminal** — True black background, neon green/cyan/magenta monospace type, scanline effects. Think a hacker's dashboard. NOT: serif fonts, warm colors, rounded corners.
- **Deep space** — Navy/indigo backgrounds, subtle star fields, glass morphism cards, cool blue accents. Think a space station UI. NOT: warm tones, paper textures, serif fonts.

### Bold & Graphic
- **Brutalist** — Raw HTML energy. System fonts at extreme sizes, harsh borders, no rounded corners, exposed grid, deliberate "ugly." Think Craigslist redesigned by a graphic designer. NOT: polished, gradient, drop shadow.
- **Maximalist pop** — Clashing bright colors, overlapping elements, mixed fonts (3+), sticker/collage energy. Think 90s web revival or Memphis design. NOT: minimalist, monochrome, orderly.
- **Retro-futuristic** — CRT glow effects, amber-on-black, chunky pixel borders, VHS tracking lines. Think Alien (1979) computer screens. NOT: modern clean, flat design.

### Structural
- **Industrial** — Exposed grid lines, monospace labels, toolbar-heavy, dense information. Think Bloomberg Terminal or Figma's UI. NOT: decorative, atmospheric, sparse.
- **Organic/natural** — Earthy tones, irregular shapes, leaf/branch motifs, hand-drawn lines. Think a botany journal app. NOT: geometric, dark, high-tech.
- **Art deco geometric** — Symmetrical patterns, gold lines on dark, chevron/fan motifs, Gatsby era. Think The Grand Budapest Hotel credits. NOT: organic shapes, pastel, casual.

## Forced Variety
For each design, randomly vary at least 3 of these 5 axes:
- **Mode**: light vs dark (alternate between runs)
- **Font category**: serif vs sans-serif vs mono vs display/decorative
- **Layout**: sidebar vs top-nav vs bottom-nav vs no-nav vs split
- **Density**: sparse (lots of white space) vs dense (information-rich)
- **Texture**: none (clean flat) vs subtle (grain/gradient) vs heavy (patterns/illustrations)

## Typography

- Choose fonts that are **beautiful, unique, and interesting**
- **NEVER use**: Inter, Roboto, Arial, system fonts — these are generic AI defaults
- **AVOID overused AI fonts**: DM Serif Display, Playfair Display, Space Grotesk, Poppins, Montserrat
- **Strategy**: Pair a distinctive display font (headings) with a refined body font (text)
- **Font discovery**: Pick from these underused categories:
  - **Geometric sans**: Outfit, Syne, Cabinet Grotesk, Satoshi, General Sans
  - **Neo-grotesque**: Switzer, Synonym, Nacelle, Plus Jakarta Sans
  - **Modern serif**: Instrument Serif, Gambarino, Newsreader, Lora
  - **Display/decorative**: Bricolage Grotesque, Fraunces, Anybody, Rubik Mono One
  - **Monospace**: JetBrains Mono, IBM Plex Mono, Fira Code, Space Mono
- Specify sizes, weights, and hierarchy

## Color & Theme

- Commit to a **cohesive aesthetic** with CSS variables
- **Dominant colors with sharp accents** outperform timid, evenly-distributed palettes
- Specify exact hex codes for primary, secondary, and accent colors
- Define dark/light mode preference
- **Avoid**: pure black (#000000), pure white (#FFFFFF), generic gold (#FFD700, #D4AF37)
- **Try**: tinted blacks (#0A1628 navy-black, #1A0A2E violet-black), tinted whites (#F0EDE6 warm, #E8ECF0 cool)

## Spatial Composition

- Unexpected layouts
- Asymmetry
- Overlap
- Diagonal flow
- Grid-breaking elements
- Generous negative space OR controlled density

## Backgrounds & Atmosphere

Create atmosphere and depth rather than defaulting to solid colors:
- Gradient meshes
- Noise textures (but NOT the default "4% opacity feTurbulence" — that's become a cliche)
- Geometric patterns
- Layered transparencies
- Dramatic shadows
- Decorative borders
- Custom cursors
- Grain overlays

## Motion

- One well-orchestrated page load with staggered reveals (animation-delay) creates more delight than scattered micro-interactions
- Scroll-triggering and hover states that surprise
- Define the motion personality: snappy, smooth, bouncy, or restrained

## Anti-Patterns (NEVER do these)

- Overused fonts: Inter, Roboto, Arial, system fonts
- Purple/blue gradients on white backgrounds
- Predictable layouts and component patterns
- Cookie-cutter card grids with identical rounded corners
- Default Tailwind blue (#3b82f6) buttons
- Bare solid white/gray backgrounds with no texture
- No animations or transitions anywhere (static, lifeless feel)

Interpret creatively and make unexpected choices that feel genuinely designed for the context. No design should be the same. Vary between light and dark themes, different fonts, different aesthetics.

## Complexity-Aesthetic Matching

- **Maximalist designs**: Need elaborate code with extensive animations and effects
- **Minimalist designs**: Need restraint, precision, and careful attention to spacing, typography, and subtle details

Elegance comes from executing the vision well.

Remember: Claude is capable of extraordinary creative work. Don't hold back, show what can truly be created when thinking outside the box and committing fully to a distinctive vision.
