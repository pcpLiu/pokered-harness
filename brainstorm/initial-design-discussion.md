# Pokemon Red AI Harness — Initial Brainstorm

Date: 2026-05-19

## Seed idea

Inspired by a tinygrad tweet asking "Has AI beaten Pokemon Red yet?" — explore whether there's room for an open-source harness environment where AI agents can play Pokemon Red, leveraging the `pret/pokered` disassembly for structured game state access.

## Landscape of existing projects

The "AI plays Pokemon" niche is occupied, but a shared, reproducible *harness* — separate from any one agent — is surprisingly underbuilt.

**Foundations**
- [pret/pokered](https://github.com/pret/pokered) — reverse-engineered disassembly with RAM symbol files. The secret weapon for any harness: lets you read `wPartyCount` instead of byte `0xD163`.
- [Baekalfen/PyBoy](https://github.com/Baekalfen/PyBoy) — Python Game Boy emulator, scriptable, explicitly built for AI training. ~3160 hours of gameplay per hour on 8 cores.

**RL approach**
- [PWhiddy/PokemonRedExperiments](https://github.com/PWhiddy/PokemonRedExperiments) — Peter Whidden's RL run, the famous YouTube one. ~7.8k stars.

**LLM agents — Claude side**
- [davidhershey/ClaudePlaysPokemonStarter](https://github.com/davidhershey/ClaudePlaysPokemonStarter) — minimal starter from an Anthropic engineer. Closest to "official" Claude Plays Pokemon code (the actual Twitch harness was never open-sourced).
- [jmurth1234/ClaudePlayer](https://github.com/jmurth1234/ClaudePlayer) — Claude + PyBoy with more configuration.
- [roman01la/claude-plays-pokemon](https://github.com/roman01la/claude-plays-pokemon) — community version on Yellow.

**LLM agents — Gemini side**
- [waylaidwanderer/gemini-plays-pokemon-public](https://github.com/waylaidwanderer/gemini-plays-pokemon-public) — the famous Gemini Plays Pokemon (Gemini 3.1 Pro).
- [nichosta/GeminiPlaysPokemonLive](https://github.com/nichosta/GeminiPlaysPokemonLive) — full harness, FireRed/LeafGreen/Emerald with Twitch chat assistance.
- [HarshNarayanJha/AIPlaysPokemon](https://github.com/HarshNarayanJha/AIPlaysPokemon) — Gemini-focused, smaller.

**Model-agnostic / benchmarks (closest to what's worth building)**
- [benchflow-ai/pokemon-gym](https://github.com/benchflow-ai/pokemon-gym) — benchmark-style gym wrapper.
- [cicero225/llm_pokemon_scaffold](https://github.com/cicero225/llm_pokemon_scaffold) — generic scaffold for any LLM.
- [martoast/LLM-Pokemon-Red](https://github.com/martoast/LLM-Pokemon-Red) — pixels-only ("like a human") constraint.
- [comex/poyo](https://github.com/comex/poyo) — explicitly framed as replicating Claude Plays Pokemon with other LLMs.

Recommended reading order to scope prior art: benchflow-ai/pokemon-gym, cicero225/llm_pokemon_scaffold, davidhershey/ClaudePlaysPokemonStarter, PWhiddy/PokemonRedExperiments.

## Key design questions explored

### Model input: text or image?

Both, usually. The pattern across Claude Plays Pokemon, Gemini Plays Pokemon, and most harnesses:

1. Screenshot of the current Game Boy frame (image).
2. Structured ASCII/tile map of the visible area (text, from RAM via pret symbols).
3. Party / inventory / badges / dialogue (text, from RAM).
4. Long-running scratchpad / journal the agent maintains itself (text).

Pure-vision is rare because Game Boy text is small and tile-grid reasoning from raw pixels burns tokens on perception. Once you have the disassembly, RAM extraction is essentially free and dramatically improves competence.

The real design question isn't "text or image" but **how much state to pre-digest before handing to the model**. Spectrum:

- Raw pixels only.
- Pixels + party/badges/coords text.
- Full structured map view, NPC scripts decoded, dialogue extracted, valid actions enumerated.
- Tool-based: model never sees state directly, calls tools like `look()`, `talk_to(npc)`, `move_to(x,y)`.

Where you sit on this spectrum defines what you're testing — heavier pre-digestion shifts the eval from perception toward planning.

### Do you need an emulator?

`pret/pokered` is a disassembly, not a ROM — it's text that, when assembled, produces a byte-identical ROM to Nintendo's original. The ROM is still copyrighted; the disassembly being on GitHub doesn't change that.

To execute that ROM you need Game Boy hardware (real or emulated). Practically: yes, emulator. PyBoy is the standard choice.

Theoretical alternatives, mostly not worth doing:
- Real Game Boy + flash cart + capture rig. Slow, not parallelizable.
- Native port of the disassembly to Python/C. Massive engineering project, nobody has done it end-to-end.
- Reimplement just the game logic as an abstract state-machine simulator. Interesting research angle, but a serious engineering project.

The disassembly's real value in a harness isn't replacing the emulator — it's making the emulator's RAM *legible*.

### Why does Nintendo tolerate emulators and disassemblies?

They don't, exactly. Nintendo is one of the most aggressive IP enforcers in gaming:

- ROM distribution sites: LoveROMs ($12M settlement), EmuParadise (preemptively wiped Nintendo titles), RomUniverse ($2.1M), Team Xecuter (federal prison).
- Modern-console emulators: Yuzu sued in 2024 ($2.4M settlement, project destroyed); Ryujinx shut down soon after; Dolphin's Steam release blocked in 2023.
- Romhacks of currently-sold games: DMCA'd within hours.

What survives, and why:

**Emulators in general** are legally protected by *Sony v. Connectix* and *Sony v. Bleem* (2000) — clean-room emulation of console hardware is legal. What Nintendo successfully attacks is DMCA §1201 (anti-circumvention): bundled decryption keys, signed firmware. Game Boy is from 1989, has no meaningful DRM, so there's no §1201 hook.

**Disassemblies like pret/pokered** sit in a contested legal zone: the source contains zero ROM bytes, just assembly mnemonics + labels + comments. Whether that's infringing turns on unsettled questions about reverse engineering and originality. *Sega v. Accolade* (1992) protects reverse engineering for interoperability as fair use. Nintendo has DMCA'd specific decomps (SM64, OoT) but they get Streisanded onto hundreds of mirrors. Game Boy era games aren't generating revenue, so the cost/benefit doesn't favor aggressive enforcement.

Equilibrium: not approval, just absence of cost-effective attack vectors.

### Shipping an Electron desktop app

The "BYO ROM" pattern is fine — PyBoy on PyPI, mGBA on Steam, Delta on iOS App Store all follow it.

**Safe to ship:**
- Your harness code (your own license).
- PyBoy bundled as dependency (LGPL-3.0 — respect license terms).
- Symbol files (`.sym`).
- Documentation, scoring rubric, save-state metadata.

**Gray zone — be careful:**
- Bundling pret/pokered source inside your binary. Most projects don't; they reference it as a separate dep the user pulls themselves, or extract only the symbol tables.
- Game graphics in marketing material.

**Don't ship:**
- The ROM itself. Not encrypted, not "downloaded on first launch," ever.
- Original boot ROM (DMG bootstrap). PyBoy handles this.
- Nintendo trademarks in product name — that's trademark infringement, distinct from copyright and more actionable.

**Branding heuristic:** be the "tool," not the "Pokemon tool." mGBA is "a Game Boy Advance emulator." Position the harness as "a benchmark suite for AI agents playing Game Boy games, with first-class support for Pokemon Red via the pret disassembly."

The "never fully built" framing is correct: the app is useless without user-supplied ROM, no path provided to obtain one.

Direct download from own site is the standard pattern; app stores are riskier and sometimes preemptively reject Nintendo-adjacent tools.

## Converging design

**Two-part product:**

### 1. Desktop app (Electron)

- Visual display of current game state (screen, structured state panel, journal view).
- Runs the emulator (single source of truth for game state).
- Exposes an MCP server on a local port.
- Owns save states, milestones, scoring, trajectory recording.
- ROM stays user-supplied.

### 2. `pokemon-red-harness.plugin` — Claude Code plugin

A single installable file containing:

- `.mcp.json` pointing at the desktop app's local MCP port.
- **MCP tools** (the capability layer): `press_button`, `get_screen`, `get_game_state`, `save_state`, `load_state`, etc.
- **Skills** (the knowledge layer): markdown playbooks the agent loads when relevant.
  - `battling` — battle strategy, type matchups.
  - `navigation` — reading maps, planning routes.
  - `menus` — efficient menu navigation.
  - `journaling` — how to maintain notes.
  - `progress-tracking` — milestones, what to remember.
- Optionally slash commands: `/pokemon:status`, `/pokemon:plan`.

### User flow

1. Launch desktop app, load ROM.
2. Install plugin into Claude Code.
3. Open Claude Code in any folder, say "play Pokemon Red."
4. Agent loads skills, calls MCP tools, plays.
5. Desktop app displays the live state.

### Architectural decisions to make

**Where does the emulator run?**

- **(a)** Python child process running PyBoy, Electron talks to it over a local socket. Works, but adds Python dependency — friction for non-developers.
- **(b)** Pure-JS Game Boy emulator in Electron (gameboy-online, binjgb's JS build). Single language, easy install, but you redo some RAM extraction in JS using pret symbols.
- **(c)** Bundle Python (pyodide or frozen interpreter). Self-contained but heavier install.

Lean toward (b) for AI-hobbyist audience; (a) for ML/RL-researcher audience.

**Desktop app as spectator or runtime?**

Pick *runtime*: desktop app owns the emulator, Claude Code is a thin client. Single source of truth. Headless mode can be added later for benchmark-style usage.

**Plugin failure mode**

When desktop app isn't running, MCP tools fail. Skills should instruct the agent to call `get_screen` first to verify the harness is up, and tell the user to launch the app if not.

## Differentiation vs. existing projects

The thin spot in current ecosystem is **shared, reproducible harness as substrate**. Most projects bundle their agent and their environment. A clean separation:

- Per-task save-state library + scoring rubric (turn "beat Pokemon Red" into a battery of small evals: "beat Brock from fresh save," "navigate Mt. Moon," "complete SS Anne").
- Pluggable agents — Claude, Gemini, OSS models, GPT all run against the same harness.
- Reproducible seeds and deterministic replay.

Elevator pitch: *"A Claude Code plugin that turns your AI coding assistant into a Pokemon-playing agent. Bring your own ROM, install the plugin, watch it play."*

## Suggested prototyping order

1. **Weekend 1:** Bare MCP server wrapping PyBoy. Expose 4-5 tools. Test with Claude Code from a terminal. Confirm the agent loop is viable and see which tools/observations the model actually wants.
2. **Weekend 2:** Add structured state extraction using pret symbols. Replace pixel-only observation with text + image. Measure improvement.
3. **Weekend 3:** Wrap in Electron desktop app with live state view. Move MCP server into the app.
4. **Weekend 4:** First skills bundle. Package as `.plugin`. First milestone save-states.
5. **Later:** Headless mode, benchmark suite, scoring, leaderboard.
