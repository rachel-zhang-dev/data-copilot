# `docs/demo.gif` recording storyboard

> Target: a 30-second silent GIF that captures the full Data Copilot
> value loop — ask → see chart → see ⚠ critic flag → 📌 pin →
> arrange on dashboard → click back to source chat. Lands on
> README hero (line 19 of [`../README.md`](../README.md)).
>
> Output: `docs/demo.gif`, ≤ 5 MB, ≤ 1280px wide, 24-30 fps,
> looping, no audio.

## 0. Pre-flight

Make sure the latest build is running locally:

```bash
cd ~/Documents/data-copilot
docker compose --profile app build api web
docker compose --profile app up -d --no-deps --force-recreate api web
docker compose logs -f api 2>&1 | head -5     # confirm "Uvicorn running"
```

Open http://localhost:3000 in a clean browser window — incognito is
ideal, no extensions in the corner, no bookmarks bar.

Resize the window to **1280×720** so the resulting GIF fits common
README widths. macOS shortcut: install `Rectangle.app` (free) and
use `Cmd+Option+1` for "Left Half", then drag the right edge until
you see `1280` in the resize tooltip.

Pre-stage a `Northwind Sales Snapshot` dashboard with **one** card
already on it (so the "add a second card" step looks like a real
extension of an existing workflow, not a first-time empty state):

1. Visit `/dashboards` → create "Northwind Sales Snapshot".
2. Back to chat → ask "Count customers grouped by country" → 📌 →
   add to Northwind Sales Snapshot.
3. Refresh `/` to a clean chat.

## 1. Record

Tool of choice (any of these work):

| Tool | Command | Notes |
|---|---|---|
| **macOS native** | `Cmd+Shift+5` → "Record Selected Portion" | Saves a `.mov` to Desktop; no install needed |
| **Kap** (recommended) | `brew install --cask kap` | Records to .mp4 or GIF directly, has built-in cropping |
| **Loom** | desktop app | Web-friendly, but extra step to download the .mp4 |

Hit record. Then run the script below. **Each step has a target
time so the GIF doesn't drag — pause briefly between steps,
nothing should sit on screen for more than ~3 s.**

| t (s) | Action | What viewers should see |
|---|---|---|
| 0-2 | Type into chat: `Top 5 products by total revenue` and press Enter | Phase events stream in (`classify_intent` → `retrieve_schema` → `coverage_check` → `generate_sql` → `critique_sql` → `summarize` → `visualize`) |
| 2-6 | Wait for full answer to render | Bar chart appears, insight panel below with metric highlights and pattern bullets |
| 6-10 | Hover briefly over the SQL details, then click the 📌 button → click "Northwind Sales Snapshot" in the picker | Picker pops, click lands, badge changes to "✓ Added to Northwind Sales Snapshot" |
| 10-14 | Click the "Dashboards →" link in the top header | Navigates to `/dashboards` index, shows "Northwind Sales Snapshot — 2 cards" |
| 14-18 | Click "Northwind Sales Snapshot" tile | Detail page loads with the 2 cards in a grid |
| 18-22 | **Drag** the new card to the right of the existing one (use the card's title-bar handle), then resize the corner to make it slightly larger | react-grid-layout animates; cards re-flow |
| 22-26 | Click the "View source chat →" link on the bottom of the new card | Navigates back to `/?conversation=...&turn=...`, the chat reloads with the original conversation, auto-scrolls to the source turn |
| 26-30 | Type a follow-up: `And by category?` then press Enter | New turn streams in, shows the loop is alive — user can keep investigating |

Stop recording at ~30 s. If the take ran long (35-40 s is normal
on first try), re-record rather than trim — the pacing matters
more than the duration.

## 2. Convert to GIF

If you used Kap, export directly to GIF and skip this section.

If you have a `.mov` from macOS native:

```bash
# 1. Install once
brew install ffmpeg gifski

# 2. Two-step conversion (palette + gifski) for crisp output
cd ~/Desktop      # or wherever the .mov landed
ffmpeg -i screencast.mov -vf "fps=24,scale=1280:-1:flags=lanczos" -c:v png frame-%04d.png
gifski --fps 24 --width 1280 --quality 90 -o demo.gif frame-*.png
rm frame-*.png

# 3. Verify size — if > 5 MB, drop --width to 1024 or --quality to 80
ls -lh demo.gif
```

Single-step alternative (faster but worse colors):

```bash
ffmpeg -i screencast.mov -vf "fps=18,scale=1024:-1:flags=lanczos" demo.gif
```

## 3. Drop it in

```bash
mv ~/Desktop/demo.gif ~/Documents/data-copilot/docs/demo.gif
cd ~/Documents/data-copilot
git add docs/demo.gif
git commit -m "docs(readme): refresh demo.gif with Phase 2.x loop (chat → critic → pin → grid → back-link)"
git push
```

The README hero line (`![Data Copilot demo](docs/demo.gif)`)
already points here — GitHub picks up the new file automatically
on next page load.

## 4. Verification checklist

Before pushing:

- [ ] File size < 5 MB (GitHub will display anything up to 10 MB but
      auto-plays at all sizes; smaller = faster page load)
- [ ] No personal info visible (browser bookmarks, other tabs, OS
      notifications, dock icons that identify you)
- [ ] The ⚠ critic badge is visible on at least one frame (if the
      sample question yielded verdict=ok, swap to a question more
      likely to trip the critic — try
      `Show customers and how many orders each one placed in 1997`
      which sometimes flags JOIN cardinality)
- [ ] The transition `dashboard → "View source chat →" → back in
      original conversation, scrolled to the right turn` is clearly
      visible — this is the loop-closure moment that justifies all
      of Phase 2.x
- [ ] Last frame doesn't end on the input field with cursor blinking —
      that looks like the recording was cut short. End on a
      complete state (a rendered turn or the dashboard)

## 5. Common re-shoots

* If the streaming phases are too fast to read, that's actually
  fine — viewers see the page "thinking" without us slowing
  things down artificially.
* If react-grid-layout's drag feels janky during recording, that's
  often the browser dev tools being open or a chrome extension
  hooking into mouse events. Close everything, retry.
* If the GIF ends up enormous (> 8 MB), the usual culprit is
  recording at retina resolution. Force 1× via screencast.app's
  resize, or just downscale in the ffmpeg step.
