# TISMA OS — notes for coding sessions

## Code style: Marlin style

Every piece of code in TISMA projects is written and documented "Marlin style" (like the Marlin 3D-printer
firmware): with as much detail as needed for anyone to understand it just by reading it.

Applies FROM NOW ON to new code and to everything that gets touched. Existing code does not need to be rewritten
all at once; when a file is modified, bring the parts you touch up to this style.

1. **File header** in every file: what it is, what it is for, how it fits with the rest of the system,
   units/conventions used, and a usage example.
2. **Sections** separated by large Marlin-style banners (lines of `=====` with the section name), and
   subsections marked `--- name ---`.
3. **Every configuration parameter explained where it is defined**, like Marlin's `Configuration.h`: what it
   does, unit, sensible range, default value, and what happens if it is pushed too far.
4. **Every function/class documented**: what it does, parameters with units, what it returns, errors it can
   raise, and — when it is not obvious — why it is done this way.
5. **Inline comments for what is not evident**: formulas explained, performance shortcuts, compatibility and
   security reasons. Do not comment the obvious.
6. **Descriptive names**, no cryptic abbreviations.
7. **Comments and documentation in English.** Text shown to users (messages, UI, logs meant for them) stays in
   Spanish, like the rest of the product.

Example of the expected shape (shell):

```sh
#!/usr/bin/env bash
# =====================================================================================================
# example-tool.sh — one-line summary of what this tool is
# -----------------------------------------------------------------------------------------------------
# Purpose:  why it exists and who calls it.
# Fits in:  which services/scripts use it and what it depends on.
# Units:    seconds for timeouts, MiB for sizes.
# Example:  TIMEOUT_SECONDS=30 ./example-tool.sh /dev/vdb
# =====================================================================================================

# =====================================================================================================
# Configuration
# =====================================================================================================

# --- Timeout ---
# How long to wait for the device before giving up.
# Unit: seconds. Sensible range: 5–120. Default: 30.
# Too low: slow disks are reported as missing. Too high: a broken device blocks boot for that long.
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-30}"
```
