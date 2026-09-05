# Coach Skill

Agent skill for personalized running and trail running coaching, with optional support for complementary work (strength/core, cycling, swimming) as adjuncts to the main running goal. Analyzes fitness status, creates periodized training plans, provides post-workout feedback, and advises on nutrition, technique, and recovery.

Designed to be used with AI coding agents (Claude Code, Cursor, etc.) as a [skill](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/tutorials#create-custom-slash-commands). The agent reads `SKILL.md`, follows the instructions, and acts as a data-driven coach that adapts to each athlete's profile.

## What It Does

| Capability | Description |
|------------|-------------|
| **Athlete profiling** | Structured onboarding and persistent athlete profile (ATHLETE.md) |
| **Training plans** | Periodized plans (linear, block, funnel, reverse, undulating) with export to Garmin, iCal, or Markdown |
| **Post-workout feedback** | Analyzes Garmin data vs planned session, gives actionable feedback |
| **Status analysis** | Evaluates training load, HRV, recovery, and fitness trends |
| **Tips & consulting** | Nutrition, technique, injury prevention, race strategy — adapted to athlete context |
| **Cross-training** | Strength/core, cycling, and swimming as complementary work — loaded on demand |
| **Profile updates** | Tracks goal changes, injuries, and evolving fitness over time |

## Requirements

- **[Coach Memory MCP](https://github.com/barcia/running-coach-memory-mcp)** — SQLite-based memory server for plans and semantic memory (RAG)

### Companion skills (optional)

- **`garmin`** — Read/write Garmin Connect data via the Python `garminconnect` library (activities, training status, HRV, scheduling workouts, etc.)
- **`ical`** — Export training plans as `.ics` calendar files

## Setup

### 1. Install the Coach Memory MCP server

Add to your MCP client configuration (Claude Desktop, Cursor, etc.):

```json
{
  "mcpServers": {
    "Coach Memory": {
      "command": "uvx",
      "args": [
        "--python", "3.12",
        "--from", "git+https://github.com/barcia/running-coach-memory-mcp",
        "running-coach-memory-mcp"
      ]
    }
  }
}
```

### 2. Install the skill

Clone the repo and register `SKILL.md` as a skill in your agent:

```bash
git clone git@github.com:barcia/running-coach-skill.git
```

The skill path to register is `SKILL.md`.

## How It Works

On every interaction, the agent:

1. **Loads the athlete profile** from `~/.local/share/coach/ATHLETE.md` (or runs onboarding if missing)
2. **Fetches current status** via Coach Memory MCP (recent plans + memories)
3. **Pulls Garmin data** if available, by invoking the `garmin` skill (no specific endpoints documented here — that skill owns the surface)
4. **Detects intent** and follows the appropriate workflow (plan, feedback, analysis, tips, cross-training, or profile update)
5. **Persists insights** — updates ATHLETE.md, saves memories, and manages plan lifecycle

The methodology reference covers periodization models, volume/intensity management, load monitoring (ACWR), recovery (HRV, sleep), trail-specific adjustments, nutrition periodization, taper protocols, and post-race recovery. Cross-training references (strength, cycling, swimming) are loaded only when the athlete asks about those disciplines.

## Project Structure

```
├── SKILL.md                        # Agent skill definition (entry point)
└── references/
    ├── ATHLETE.md                  # Athlete profile template
    ├── methodology.md              # Running/trail methodology reference
    ├── onboarding.md               # New athlete onboarding process
    ├── strength.md                 # Strength & core for runners (cross-training)
    ├── cycling.md                  # Cycling as cross-training
    └── swimming.md                 # Swimming as cross-training
```

## License

[GPL-3.0](LICENSE)
