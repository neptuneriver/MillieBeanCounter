# Test data

All the content here is made up. Only the email layouts are real.

* `forwarded-oct-12.eml` and `original-oct-12.eml`: a daily sheet for a child called
  Josephine ("Josie" in Huckleberry) for Monday, October 12, 2026. They keep the exact
  layout of a real Kaymbu email, with every personal detail replaced: names, times,
  amounts, the teacher's comment, and any link that could identify a school or family
  (those point to `example.invalid`). The first is the sheet as Gmail forwards it, the
  second as Kaymbu sends it.
* `moments-oct-14.eml`: a "Shared Moments" photo email in Kaymbu's exact layout, dated
  October 14, 2026. The photo, the school banner and every tracking link point to
  `example.invalid`; only Kaymbu's public icons are left as they were. The tests build
  other days, several moments, a Gmail forward and a "forward as attachment" from it.
* `huckleberry-sample.csv`: 22 made-up entries in the layout of Huckleberry's own CSV
  export, chosen to cover its quirks (quoted notes, a trailing space, a feed side
  shorter than a minute, diaper parts in either order, entries with and without an end).
