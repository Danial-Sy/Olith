# Olith design notes

Keep the app calm and easy to read. Use the faceted O for the logo, with blue, violet
and rose for accents. Keep text and reading areas neutral.

Use `static/assets/olith-mark.svg` for the warm-up screen, replies, greetings,
connections and favicon. It has a transparent background and a tight frame.
`static/assets/olith.svg` includes the rounded desktop tile and supplies the PNG and ICO
files. Keep the logo's colors, shape and facets as they are. Don't redraw it inline or
add color, saturation or opacity filters.

Reply logos use a 32 px slot. Put any animated glow behind the logo so the artwork
itself stays unchanged.

## References

- [Google's Gemini design notes](https://design.google/library/gemini-ai-visual-design)
  informed the small gradient glow and motion with a clear start and end.
- [Claude's product examples](https://claude.com/product/overview) informed the
  short activity updates and stable reading area. They aren't a source for
  exact animation timings or implementation details.

## Motion

| State | What happens |
| --- | --- |
| Greeting or finished reply | Still logo |
| Thinking or tool work | Blue-to-rose glow behind the logo, pulsing every 5.6 seconds |
| Streaming text | Same glow, dimmer |
| Loading a model | Glow pulses every 4.2 seconds |
| Reply finishes | Glow fades out over 500 ms |
| New message | 240 ms fade with a 4 px lift |
| Controls and panels | 160–260 ms transitions; input works immediately |
| Reduced motion | No decorative animation |
| Window in the background | Glow pauses |

## Keep it usable

- Show tokens as they arrive. Animation must never delay Send, Stop, Copy or approvals.
- Keep the reading column in place while a reply loads and finishes.
- Follow new text only while the reader is near the bottom.
- Keep text and status labels solid. Use gradients for the logo and accents.
- Skip moving backgrounds, particles, per-token fades and large blurs.
- Animate a separate blurred gradient behind the SVG. Use a short Web Animation
  to fade it out when work finishes.
- Use the logo's cyan, blue, violet and rose colors for the glow.
- Keep keyboard focus visible and preserve the existing controls.
