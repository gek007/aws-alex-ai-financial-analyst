# Alex Researcher Agent - Code Explanation

## Overview

The **Researcher Agent** is an autonomous AI service that browses the web, analyzes financial information, and stores research findings. It runs on AWS App Runner as a containerized FastAPI application and is part of the larger Alex financial platform.

**Key Innovation**: Uses Playwright MCP (Model Context Protocol) to give the LLM the ability to browse web pages in real-time, combined with AWS Bedrock for LLM reasoning and analysis.

---

## Architecture Overview

```
FastAPI Server (Port 8000)
    ↓
REST Endpoints (/research, /research/auto, /health)
    ↓
Research Agent (OpenAI Agents SDK)
    ├── Model: AWS Bedrock (Nova Pro or custom)
    ├── Tools: ingest_financial_document()
    └── MCP Servers: Playwright (web browsing)
    ↓
Agent Loop (max 15 turns):
  1. Browse websites using Playwright
  2. Analyze findings with LLM
  3. Save to Alex knowledge base via API
```

---

## File-by-File Breakdown

### 1. **server.py** - FastAPI Application & Entry Point

**What it does**: Exposes REST endpoints for triggering research

**Key endpoints**:
- `GET /` - Health check (returns "OK")
- `POST /research` - Trigger research on a topic (request body: `{"topic": "optional_topic"}`)
- `GET /research/auto` - Scheduled research endpoint (called by EventBridge Scheduler)
- `GET /health` - Detailed health check including Bedrock connectivity
- `GET /test-bedrock` - Debug endpoint to verify Bedrock access

**Core function - `run_research_agent(topic: str = None)`**:
```python
# 1. Create LLM model connected to AWS Bedrock
model = LitellmModel(model=f"bedrock/{model_id}")

# 2. Create agent with instructions, tools, and web browsing capability
agent = Agent(
    name="Investment Research Agent",
    instructions=get_agent_instructions(),  # System prompt from context.py
    model=model,
    tools=[ingest_financial_document],  # Tool to save findings
    mcp_servers=[create_playwright_mcp_server()]  # MCP for web browsing
)

# 3. Run agent for up to 15 turns
result = await Runner.run(agent, input=task)
return result.final_output
```

**Important**: Sets `AWS_REGION_NAME` environment variable (required by LiteLLM for Bedrock)

---

### 2. **context.py** - Agent Instructions & Behavior

**What it does**: Defines what the agent should do and how it should behave

**Key function - `get_agent_instructions()`**:
Returns a detailed system prompt that:
1. **Identifies the role**: "Investment research specialist"
2. **Defines the workflow** (3-step process):
   - Research phase: Browse max 2 financial websites for trending topics
   - Analysis phase: Create 3-5 bullet-point summary
   - Storage phase: Save findings via `ingest_financial_document` tool
3. **Emphasizes speed**: "Be fast and efficient"
4. **Includes context**: Current date, currency, investment focus

**Default topic**: If no topic provided, agent picks a "trending investment topic"

---

### 3. **tools.py** - Tools the Agent Can Use

**What it does**: Defines functions the agent can invoke during execution

**Key tool - `ingest_financial_document(topic: str, analysis: str)`**:
```python
@function_tool
async def ingest_financial_document(topic: str, analysis: str) -> str:
    """Save research findings to Alex knowledge base"""
    
    # 1. Prepare payload
    payload = {
        "topic": topic,
        "analysis": analysis,
        "timestamp": datetime.utcnow().isoformat()
    }
    
    # 2. Call Alex API with retry logic (exponential backoff)
    # Up to 3 attempts with 1-10 second waits
    
    # 3. Return success/failure message
```

**Design features**:
- **Retry logic**: Automatically retries failed requests (using `tenacity`)
- **Graceful fallback**: Works in local mode if `ALEX_API_ENDPOINT` not set
- **Authentication**: Uses `ALEX_API_KEY` header for security

---

### 4. **mcp_servers.py** - Web Browsing Capability

**What it does**: Configures the Playwright MCP server so the agent can browse websites

**Key function - `create_playwright_mcp_server()`**:
```python
# Creates a stdio-based MCP server running Playwright
# Command: npx @playwright/mcp@latest [with various flags]

# Flags:
# --headless: Don't show browser UI
# --isolated: Isolated browser context
# --no-sandbox: Disable sandbox (needed in Docker)
# --ignore-https-errors: Ignore SSL warnings

# Chrome detection:
# 1. Tries to find Chrome in standard Docker paths
# 2. Fallback: /root/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome
```

**Key insight**: This gives the LLM "hands" to browse the web - the agent can:
- Visit URLs
- Read page content
- Fill forms
- Click buttons
- Extract data

---

### 5. **Dockerfile** - Containerization

**What it does**: Creates a Docker image for App Runner

**Build steps**:
1. **Base image**: Python 3.12 slim (lightweight)
2. **Install Node.js**: Required for Playwright MCP
3. **Install system deps**: Chromium, Playwright dependencies
4. **Copy code**: Application files
5. **Install Python deps**: Using `uv` (faster than pip)
6. **Expose port 8000**: FastAPI listens here
7. **Start server**: `uvicorn server:app --host 0.0.0.0 --port 8000`

**Key**: Optimized for `linux/amd64` architecture (App Runner requirement)

---

### 6. **deploy.py** - Deployment to AWS

**What it does**: Builds Docker image and deploys to AWS App Runner

**Steps**:
1. Get AWS account ID and region
2. Create/get ECR repository (via Terraform)
3. Login to ECR with AWS credentials
4. Build Docker image for linux/amd64
5. Tag and push to ECR
6. Update App Runner service to use new image

**Requirements**: AWS CLI, Docker, Terraform output values

---

### 7. **test_local.py** - Local Testing

**What it does**: Test the agent locally before deployment

**Usage**: 
```bash
uv run test_local.py
```

**Behavior**:
- Uses OpenAI `gpt-4.1-mini` (for fast local testing)
- Creates same agent setup as production
- Runs one research cycle
- Good for development and debugging

---

### 8. **test_research.py** - Remote Testing

**What it does**: Test the deployed service on AWS App Runner

**Usage**:
```bash
uv run test_research.py "Your Topic Here"
```

**Behavior**:
- Gets App Runner service URL from Terraform
- Calls `/research` endpoint with topic
- Validates response
- Shows research findings

---

## How It All Works Together - Step by Step

### Scenario: User requests research on "AI stocks"

1. **Request arrives**
   ```
   POST /research with body: {"topic": "AI stocks"}
   ```

2. **Server creates agent**
   - Model: AWS Bedrock (Nova Pro)
   - Instructions: "Research AI stocks and analyze"
   - Tools: [ingest_financial_document]
   - MCP: Playwright (can browse web)

3. **Agent runs (up to 15 turns)**
   - **Turn 1**: LLM thinks "I need to research AI stocks"
   - LLM browses websites using Playwright MCP (visits Yahoo Finance, etc.)
   - **Turn 2**: LLM reads page content and analyzes it
   - LLM creates bullet-point summary
   - **Turn 3**: LLM calls `ingest_financial_document` tool
   - Tool saves to Alex API
   - LLM finishes

4. **Response returned**
   ```
   {
     "status": "success",
     "findings": "• AI stocks up 15%\n• NVIDIA leading gains..."
   }
   ```

5. **Research data now in Alex knowledge base**
   - Accessible to other agents (Reporter, Charter, etc.)
   - Available for portfolio analysis

---

## Key Design Decisions

### Why Playwright MCP?
- Gives LLM real-time web browsing capability
- Can read dynamic content (JavaScript-rendered pages)
- More reliable than scraping with BeautifulSoup

### Why Bedrock (not OpenAI)?
- Runs on AWS (same region as other services)
- Cost-effective for batch operations
- Nova Pro model is optimized for quick analysis

### Why App Runner (not Lambda)?
- Long-running processes (up to 4 hours)
- Better for continuous research operations
- Easier to manage container lifecycle

### Why 15-turn limit?
- Prevents infinite loops
- Typical research completes in 3-5 turns
- Cost and time control

### Why 2-page limit?
- Speed: Research should be fast
- Cost: Web browsing is expensive
- Quality: Often first 2 pages have most relevant info

---

## Configuration Requirements

**Environment variables** (set in Terraform or `.env`):
- `AWS_REGION` - AWS region (default: us-east-1)
- `ALEX_API_ENDPOINT` - URL to knowledge base API
- `ALEX_API_KEY` - Authentication key

**Optional overrides**:
- `BEDROCK_MODEL_ID` - Change LLM model
- `RESEARCHER_MAX_TURNS` - Change turn limit

---

## Key Concepts Explained

### What's LiteLLM?
A Python library that provides a unified interface to multiple LLM providers. Here it's used to connect to AWS Bedrock models without writing AWS SDK code directly.

### What's OpenAI Agents SDK?
A framework for building agentic AI systems. It handles the agent loop (thinking → tool calling → observation → repeat) automatically.

### What's MCP (Model Context Protocol)?
A protocol that lets LLMs interact with external tools and systems. Playwright MCP specifically gives the LLM the ability to control a web browser.

### What's Bedrock?
AWS's managed service for accessing foundation models (LLMs). It provides a unified API for models from Anthropic, Meta, Cohere, and Amazon.

---

## Testing the Code

### Local Testing (Fast iteration)
```bash
cd backend/researcher
uv run test_local.py
```
- Uses OpenAI GPT-4 Mini
- No AWS credentials needed
- Good for development

### Remote Testing (After deployment)
```bash
uv run test_research.py "Topic Here"
```
- Tests actual App Runner deployment
- Uses Bedrock model
- Verifies end-to-end integration

---

## Common Patterns

### Agent Creation Pattern
```python
agent = Agent(
    name="descriptive name",
    instructions="system prompt",
    model=model,
    tools=[list of available tools],
    mcp_servers=[list of MCP servers for external integrations]
)
```

### Tool Definition Pattern
```python
@function_tool
async def tool_name(param1: str, param2: int) -> str:
    """Doc string explaining what the tool does"""
    # Implementation
    return result
```

### Error Handling Pattern
Uses `tenacity` library with exponential backoff:
```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10)
)
async def flaky_operation():
    # Code that might fail
```

---

## Questions to Explore?

- How does the agent decide what to do at each turn?
- What happens if the API call fails?
- How does Playwright MCP actually work under the hood?
- Why does it need Node.js in the Docker image?
- How does it avoid getting blocked by websites?
