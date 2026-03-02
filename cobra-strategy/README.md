# COBRA — Capital Orchestrator with Brain-Regulated Agents

One brain. Many hunters. Dynamic capital. Minimal tokens.

## What is COBRA?

COBRA is a meta-orchestrator for Senpi that spawns and manages WOLF and TIGER trading instances as subagents. It dynamically allocates capital based on market regime and optimizes LLM token usage across all managed instances.

## Key Features

- **Market Regime Detection** — Classifies BTC macro into TRENDING/RANGING/VOLATILE
- **Dynamic Subagent Spawning** — Creates WOLF/TIGER instances with independent wallets
- **Signal Pressure Tracking** — Reads screener outputs to detect missed opportunities
- **Kill vs Keep Decisions** — Smart capital reallocation when partial withdrawal isn't possible
- **VIPER Token Optimization** — 40-60% reduction in LLM API costs

## Setup

```bash
python3 scripts/cobra-setup.py --chat-id 12345 --total-budget 10000 \
    --max-wolves 2 --max-tigers 1
```

Then create the 4 output cron jobs in OpenClaw.

## Architecture

```
COBRA (4 crons)
├── Brain (15min)    → Regime + signals + spawn/kill decisions
├── Monitor (30min)  → Performance aggregation
├── Signals (5min)   → Signal pressure (reads local files, no MCP)
└── Token Audit (1h) → Budget tracking

Spawned by COBRA (subagent model — each in own isolated session):
├── WOLF Instance 1 (1 subagent + 3 wake crons) → Momentum hunting
├── WOLF Instance 2 (1 subagent + 3 wake crons) → Conservative momentum
└── TIGER Instance 1 (1 subagent + 2 wake crons) → Calculated entries
```

## Capital Allocation by Regime

| Regime | WOLF | TIGER | Reserve |
|--------|------|-------|---------|
| TRENDING | 60% | 25% | 15% |
| RANGING | 20% | 50% | 30% |
| VOLATILE | 40% | 30% | 30% |

## Dependencies

- Python 3.8+
- `mcporter` CLI with Senpi MCP connection
- OpenClaw cron system
- `wolf-strategy` skill (for WOLF instances)
- `tiger` skill (for TIGER instances)

## Files

```
cobra-strategy/
├── SKILL.md                    # Full instructions
├── README.md                   # This file
├── scripts/
│   ├── cobra_config.py         # Shared config + MCP helpers
│   ├── viper_gate.py           # Token optimization module
│   ├── cobra-regime.py         # Market regime classifier
│   ├── cobra-signals.py        # Signal pressure aggregator
│   ├── cobra-monitor.py        # Performance tracker
│   ├── cobra-spawner.py        # Instance lifecycle manager
│   ├── cobra-brain.py          # Main orchestrator
│   └── cobra-setup.py          # Setup wizard
└── references/
    ├── cron-templates.md       # COBRA's 4 cron templates
    ├── state-schema.md         # All state file schemas
    ├── regime-rules.md         # Regime classification rules
    └── spawn-templates.md      # Spawn/kill flow documentation
```

## Changelog

### v1.0 (2026-02-28)
- Initial release
- 3-regime market classifier (TRENDING, RANGING, VOLATILE)
- Dynamic WOLF/TIGER spawning with VIPER-wrapped crons
- Signal pressure aggregation from WOLF/TIGER screener outputs
- Kill-vs-keep decision framework with opportunity EV calculation
- Token budget tracking and frequency recommendations
