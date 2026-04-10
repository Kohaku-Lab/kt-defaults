# kt-defaults

Default creatures, terrariums, and plugins for [KohakuTerrarium](https://github.com/Kohaku-Lab/KohakuTerrarium).

## Install

```bash
# Install as a KohakuTerrarium package
kt install https://github.com/Kohaku-Lab/kt-defaults.git

# Or install as editable (for development)
kt install ./kt-defaults -e
```

## Creatures

| Name | Description | Base |
|------|-------------|------|
| `general` | Base creature: 22 tools, 6 sub-agents, web search/fetch, memory search | (none) |
| `swe` | Software engineering specialist | general |
| `reviewer` | Code review specialist | general |
| `ops` | Infrastructure and operations specialist | general |
| `researcher` | Research and analysis specialist | general |
| `creative` | Creative writing specialist | general |
| `root` | Terrarium management, task delegation | general |

## Terrariums

| Name | Description | Creatures |
|------|-------------|-----------|
| `swe_team` | SWE team with root agent | root, swe, reviewer |
| `auto_research` | Automated experiment loop (Karpathy's autoresearch pattern) | ideator, coder, runner, analyzer |
| `deep_research` | Multi-agent web research with citations | planner, researcher, synthesizer, critic |

## Plugins

| Name | Description |
|------|-------------|
| `cost_tracker` | Track LLM token usage and estimated cost per session |
| `event_logger` | Structured JSONL event log of all agent activity |

## Usage

```bash
# Set your default model
kt model default gemini-3.1-pro

# Run a creature directly
kt run @kt-defaults/creatures/swe

# Override model per-run
kt run @kt-defaults/creatures/swe --llm mimo-v2-pro

# Run a terrarium
kt terrarium run @kt-defaults/terrariums/swe_team

# Edit a creature config
kt edit @kt-defaults/creatures/general
```

### Using plugins

Add to your creature's `config.yaml`:

```yaml
plugins:
  - name: cost_tracker
    type: package
    module: kt_defaults.cost_tracker
    class: CostTrackerPlugin
    options:
      budget_usd: 5.0
      warn_at: 0.8

  - name: event_logger
    type: package
    module: kt_defaults.event_logger
    class: EventLoggerPlugin
    options:
      path: ./logs/events.jsonl
```

## Creating Your Own Package

A package is a directory with:

```
my-package/
  kohaku.yaml          # manifest (name, creatures, terrariums, plugins)
  creatures/           # creature configs
  terrariums/          # terrarium configs
  my_package/          # Python package (for plugins/tools)
    __init__.py
    my_plugin.py
  pyproject.toml       # makes Python code importable
```

Cross-package references use `@package-name/path` syntax:

```yaml
# In your creature's config.yaml
base_config: "@kt-defaults/creatures/swe"
```

## License

KohakuTerrarium License 1.0 (see [LICENSE](https://github.com/Kohaku-Lab/KohakuTerrarium/blob/main/LICENSE))
