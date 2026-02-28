# MCP Setup — Connecting LeadGen to Claude Desktop

Once LeadGen is installed, you can connect it to Claude Desktop as an MCP server.
This lets you control your entire lead pipeline conversationally.

## Step 1: Install LeadGen

```bash
git clone https://github.com/yourusername/LeadGen.git
cd LeadGen
pip install -e .
cp .env.example .env        # fill in your API keys
cp config.example.yaml config.yaml  # customize your ICP
```

## Step 2: Configure Claude Desktop

Find your Claude Desktop config file:

| OS | Path |
|----|------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

Add LeadGen to the `mcpServers` section:

```json
{
  "mcpServers": {
    "leadgen": {
      "command": "python",
      "args": ["-m", "src.mcp_server.server"],
      "cwd": "/path/to/your/LeadGen"
    }
  }
}
```

## Step 3: Restart Claude Desktop

After saving the config, fully quit and relaunch Claude Desktop.
You should see a 🔧 tools icon indicating MCP tools are available.

## Step 4: Talk to Your Lead Pipeline

You can now say things like:

> "Show me my lead pipeline summary"

> "Fetch 30 new leads from Apollo for restaurants in Chicago"

> "Score the unscored leads and tell me how many passed"

> "Draft outreach emails for the top 5 leads"

> "Approve the outreach for lead ID abc-123"

> "Move lead xyz-456 to 'responded' and add a note that they asked for pricing"

## Available MCP Tools

| Tool | What it does |
|------|-------------|
| `get_pipeline` | Pipeline summary with counts by status |
| `search_leads` | Filter leads by status, score, limit |
| `fetch_new_leads` | Pull fresh leads from Apollo (ICP search) or Hunter (domain search; pass `domain` param) |
| `score_leads` | AI-score a batch of new leads |
| `draft_outreach` | Generate personalized email drafts |
| `approve_outreach` | Mark drafts approved for sending |
| `update_lead_status` | Move lead through pipeline |
| `get_lead_detail` | Full info on a specific lead |

## Troubleshooting

**Tools not showing up in Claude Desktop:**
- Make sure the `cwd` path is correct and absolute
- Check that Python can find the `src` module: `cd /path/to/LeadGen && python -c "from src.mcp_server.server import app"`
- Check Claude Desktop logs: `~/Library/Logs/Claude/` (macOS)

**Apollo returning no results:**
- Verify `APOLLO_API_KEY` is set in your `.env`
- Check your ICP geography — very narrow filters can return 0 results
- Apollo free tier has limited requests per hour

**Score is always 0:**
- Verify `ANTHROPIC_API_KEY` is set
- Check your internet connection
- Run `leadgen score --debug` to see raw Claude responses
