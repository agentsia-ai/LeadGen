# MCP Setup — Connecting LeadGen to Claude Desktop

LeadGen ships an MCP (Model Context Protocol) server so you can drive your
entire lead pipeline conversationally from Claude Desktop.

There are two ways to run it depending on whether you're using LeadGen
standalone or as the engine for a productized agent like Rex.

---

## Option A: Standalone (LeadGen as a generic engine)

For developers running the public LeadGen engine on its own.

### Step 1: Install LeadGen

```bash
git clone https://github.com/agentsia-ai/LeadGen.git
cd LeadGen
uv sync                                 # creates .venv and installs LeadGen
cp .env.example .env                    # fill in your API keys
cp config.example.yaml config.yaml      # customize your ICP
```

### Step 2: Configure Claude Desktop

Find your Claude Desktop config file:

| OS      | Path                                                                |
|---------|---------------------------------------------------------------------|
| macOS   | `~/Library/Application Support/Claude/claude_desktop_config.json`   |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json`                       |

Add LeadGen to the `mcpServers` section:

```json
{
  "mcpServers": {
    "leadgen": {
      "command": "python",
      "args": ["-m", "leadgen.mcp"],
      "cwd": "/absolute/path/to/your/LeadGen"
    }
  }
}
```

On Windows, because Claude Desktop doesn't inherit your shell's PATH, it's
often easiest to use the venv's executable and a matching `cwd`. Replace
`C:\\path\\to\\your\\LeadGen` with the real path to your clone (the folder
that contains `pyproject.toml`):

```json
{
  "mcpServers": {
    "leadgen": {
      "command": "C:\\path\\to\\your\\LeadGen\\.venv\\Scripts\\leadgen.exe",
      "args": ["mcp"],
      "cwd": "C:\\path\\to\\your\\LeadGen"
    }
  }
}
```

The `cwd` matters: the MCP server resolves relative paths in `config.yaml`
(database, imports folder, prompt overrides) from this working directory.

---

## Option B: Productized agent (e.g. Rex via agentsia-core)

If you're running a named-persona deployment of LeadGen, use the agent's CLI
entry point instead. This injects the agent's tuned scorer/drafter subclasses
into the MCP server automatically.

For Rex (in the `agentsia-core` private repo):

```json
{
  "mcpServers": {
    "rex": {
      "command": "agentsia",
      "args": ["rex", "mcp"]
    }
  }
}
```

For per-client deployments of Rex:

```json
{
  "mcpServers": {
    "rex-acme": {
      "command": "agentsia",
      "args": ["rex", "--client", "acme_roofing", "mcp"]
    }
  }
}
```

The `agentsia` CLI handles config layering, env loading, and working-directory
resolution itself — no `cwd` needed in the JSON.

> Heads up: Claude Desktop runs MCP commands without your shell's PATH on some
> systems. If `agentsia` isn't found, replace `"command": "agentsia"` with the
> absolute path output by `which agentsia` (or `where agentsia` on Windows).

---

## Step 3: Restart Claude Desktop

After saving the config, fully quit and relaunch Claude Desktop. You should
see a 🔧 tools icon indicating MCP tools are available.

## Step 4: Talk to your lead pipeline

You can now say things like:

> "Show me my lead pipeline summary"

> "Fetch 30 new leads from Hunter for acmecorp.com"

> "Score the unscored leads and tell me how many passed"

> "Draft outreach emails for the top 5 leads"

> "Approve the outreach for lead ID abc-123"

> "Move lead xyz-456 to 'responded' and add a note that they asked for pricing"

## Available MCP tools

| Tool                  | What it does                                                                              |
|-----------------------|-------------------------------------------------------------------------------------------|
| `get_pipeline`        | Pipeline summary with counts by status                                                    |
| `search_leads`        | Filter leads by status, score, limit                                                      |
| `fetch_new_leads`     | Pull fresh leads from Hunter (domain), PDL, or Apollo                                     |
| `score_leads`         | AI-score a batch of new leads (uses the injected scorer class)                            |
| `draft_outreach`      | Generate personalized email drafts (uses the injected drafter class)                      |
| `approve_outreach`    | Mark drafts approved for sending                                                          |
| `update_lead_status`  | Move a lead through the pipeline                                                          |
| `get_lead_detail`     | Full info on a specific lead                                                              |

## Troubleshooting

**Tools not showing up in Claude Desktop:**
- Confirm the `cwd` path is correct and absolute (Option A only)
- Confirm `agentsia` resolves on your PATH, or use the absolute path (Option B)
- Check Claude Desktop logs:
  - macOS: `~/Library/Logs/Claude/`
  - Windows: `%APPDATA%\Claude\Logs\`

**Lead source returning no results:**
- Verify the relevant API key is set in `.env` (`HUNTER_API_KEY`, `PDL_API_KEY`, etc.)
- Check your ICP geography — very narrow filters can return 0 results
- Apollo's free tier in particular is rate-limited and pagination-flaky; try Hunter or PDL first
- See the "Source maturity" table in the main `README.md`

**Score is always 0 / drafts are blank:**
- Verify `ANTHROPIC_API_KEY` is set
- Check your internet connection
- Run `leadgen --debug score` (or `agentsia --debug rex score`) to see raw Claude responses
