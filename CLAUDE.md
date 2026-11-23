# TISMA OS — notes for coding sessions

## Code style: Marlin-style

Rule set by the project owner for every TISMA project. It applies **from now on** to all new code and to
everything that gets touched. Existing code does not need to be rewritten all at once.

Write and document code the way the Marlin firmware does: in maximum detail, so anyone can understand it just by
reading it.

1. **File header** in every file: what it is, what it is for, how it fits with the rest of the system,
   units and conventions used, and a short usage example.
2. **Big section separators** like Marlin (lines of `=====` with the section name) and subsections with
   `--- name ---`.
3. **Every configuration parameter explained where it is defined**, like Marlin's `Configuration.h`: what it
   does, its unit, a sensible range, its default value and what happens if it is pushed too far.
4. **Every function and class documented**: what it does, parameters with units, what it returns, the errors it
   can raise and, when not obvious, *why* it is done this way.
5. **Inline comments for what is not evident**: formulas explained, performance shortcuts, compatibility
   workarounds, security decisions. Do not comment the obvious.
6. **Descriptive names**, no cryptic abbreviations.
7. **Language**: code comments and code documentation in **English**. Everything the user sees (interface
   texts, messages, notifications, release notes, user guides) stays in **Spanish**.
