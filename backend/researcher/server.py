"""
Alex Researcher Service - Investment Advice Agent
"""

import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Optional

from agents import Agent, Runner, trace
from agents.extensions.models.litellm_model import LitellmModel
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Suppress LiteLLM warnings about optional dependencies
logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)

# Import from our modules
from context import DEFAULT_RESEARCH_PROMPT, get_agent_instructions
from mcp_servers import create_playwright_mcp_server
from tools import ingest_financial_document

# Load environment
load_dotenv(override=True)

app = FastAPI(title="Alex Researcher Service")

# In-process job store so POST /research can return within App Runner's 120s HTTP limit.
# See: https://docs.aws.amazon.com/apprunner/latest/dg/develop.html — 120s total request timeout.
_MAX_JOBS = 128
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = asyncio.Lock()


def _evict_terminal_jobs_unlocked() -> None:
    if len(_jobs) <= _MAX_JOBS:
        return
    excess = len(_jobs) - _MAX_JOBS
    terminal = [
        jid
        for jid, meta in _jobs.items()
        if meta.get("status") in ("completed", "failed")
    ]
    for jid in terminal[:excess]:
        _jobs.pop(jid, None)


async def _run_research_job(job_id: str, topic: Optional[str]) -> None:
    async with _jobs_lock:
        meta = _jobs.get(job_id)
        if meta is None:
            return
        meta["status"] = "running"
    try:
        output = await run_research_agent(topic)
        async with _jobs_lock:
            m = _jobs.get(job_id)
            if m is not None:
                m["status"] = "completed"
                m["result"] = output
    except Exception as e:
        logging.exception("Research job %s failed", job_id)
        async with _jobs_lock:
            m = _jobs.get(job_id)
            if m is not None:
                m["status"] = "failed"
                m["error"] = str(e)


# Request model
class ResearchRequest(BaseModel):
    topic: Optional[str] = None  # Optional - if not provided, agent picks a topic


async def run_research_agent(topic: str = None) -> str:
    """Run the research agent to generate investment advice."""

    # Prepare the user query
    if topic:
        query = f"Research this investment topic: {topic}"
    else:
        query = DEFAULT_RESEARCH_PROMPT

    # eu-west-1 matches the App Runner service region; Nova Pro supports tools/MCP.
    # OSS 120B is only available in us-west-2 — using it with us-east-1 causes silent failures.
    REGION = "eu-west-1"
    os.environ["AWS_REGION_NAME"] = REGION  # LiteLLM's preferred variable
    os.environ["AWS_REGION"] = REGION  # Boto3 standard
    os.environ["AWS_DEFAULT_REGION"] = REGION  # Fallback

    # eu.amazon.nova-pro-v1:0 — EU inference profile, supports tool calling and MCP servers.
    # nova-lite is NOT acceptable here as it does not support tool calling.
    MODEL = "bedrock/eu.amazon.nova-pro-v1:0"
    model = LitellmModel(model=MODEL)

    # Create and run the agent with MCP server
    with trace("Researcher"):
        async with create_playwright_mcp_server(timeout_seconds=60) as playwright_mcp:
            agent = Agent(
                name="Alex Investment Researcher",
                instructions=get_agent_instructions(),
                model=model,
                tools=[ingest_financial_document],
                mcp_servers=[playwright_mcp],
            )

            # Each Playwright navigate/snapshot + model reply counts as a turn.
            result = await Runner.run(agent, input=query, max_turns=30)

    return result.final_output


@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "service": "Alex Researcher",
        "status": "healthy",
        "timestamp": datetime.now(UTC).isoformat(),
    }


@app.post("/research")
async def research(
    request: ResearchRequest,
    sync: bool = Query(
        False,
        description="If true, block until research completes (hits App Runner ~120s HTTP limit).",
    ),
):
    """
    Generate investment research and advice.

    Default (async): returns 202 + job_id immediately. Poll GET /research/jobs/{job_id}
    until status is completed or failed. Required on AWS App Runner because the load
    balancer enforces a 120s timeout on the entire request.

    sync=true: returns the research text in one response (for local dev only).
    """
    if sync:
        try:
            return await run_research_agent(request.topic)
        except Exception as e:
            logging.exception("Error in research endpoint (sync)")
            raise HTTPException(status_code=500, detail=str(e))

    job_id = str(uuid.uuid4())
    async with _jobs_lock:
        _evict_terminal_jobs_unlocked()
        _jobs[job_id] = {
            "status": "pending",
            "created": datetime.now(UTC).isoformat(),
        }
    asyncio.create_task(_run_research_job(job_id, request.topic))
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "pending",
            "message": "Poll GET /research/jobs/{job_id} until status is completed or failed.",
        },
    )


@app.get("/research/jobs/{job_id}")
async def research_job_status(job_id: str):
    """Status and result for an async research job started via POST /research."""
    async with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return job


@app.get("/research/auto")
async def research_auto():
    """
    Automated research (scheduled runs). Returns 202 + job_id; poll GET /research/jobs/{job_id}.
    """
    job_id = str(uuid.uuid4())
    async with _jobs_lock:
        _evict_terminal_jobs_unlocked()
        _jobs[job_id] = {
            "status": "pending",
            "created": datetime.now(UTC).isoformat(),
        }
    asyncio.create_task(_run_research_job(job_id, None))
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "job_id": job_id,
            "message": "Poll GET /research/jobs/{job_id} until status is completed or failed.",
        },
    )


@app.get("/health")
async def health():
    """Detailed health check."""
    # Debug container detection
    container_indicators = {
        "dockerenv": os.path.exists("/.dockerenv"),
        "containerenv": os.path.exists("/run/.containerenv"),
        "aws_execution_env": os.environ.get("AWS_EXECUTION_ENV", ""),
        "ecs_container_metadata": os.environ.get("ECS_CONTAINER_METADATA_URI", ""),
        "kubernetes_service": os.environ.get("KUBERNETES_SERVICE_HOST", ""),
    }

    return {
        "service": "Alex Researcher",
        "status": "healthy",
        "alex_api_configured": bool(
            os.getenv("ALEX_API_ENDPOINT") and os.getenv("ALEX_API_KEY")
        ),
        "timestamp": datetime.now(UTC).isoformat(),
        "debug_container": container_indicators,
        "aws_region": os.environ.get("AWS_DEFAULT_REGION", "not set"),
        "bedrock_model": "bedrock/eu.amazon.nova-pro-v1:0",
    }


@app.get("/test-bedrock")
async def test_bedrock():
    """Test Bedrock connection directly."""
    try:
        import boto3

        # Set ALL region environment variables
        os.environ["AWS_REGION_NAME"] = "eu-west-1"
        os.environ["AWS_REGION"] = "eu-west-1"
        os.environ["AWS_DEFAULT_REGION"] = "eu-west-1"

        # Debug: Check what region boto3 is actually using
        session = boto3.Session()
        actual_region = session.region_name

        client = boto3.client("bedrock-runtime", region_name="eu-west-1")

        # Debug: list available Nova models in eu-west-1
        try:
            bedrock_client = boto3.client("bedrock", region_name="eu-west-1")
            models = bedrock_client.list_foundation_models()
            nova_models = [
                m["modelId"]
                for m in models["modelSummaries"]
                if "nova" in m["modelId"].lower()
            ]
        except Exception as list_error:
            nova_models = f"Error listing: {str(list_error)}"

        model = LitellmModel(model="bedrock/eu.amazon.nova-pro-v1:0")

        agent = Agent(
            name="Test Agent",
            instructions="You are a helpful assistant. Be very brief.",
            model=model,
        )

        result = await Runner.run(
            agent, input="Say hello in 5 words or less", max_turns=1
        )

        return {
            "status": "success",
            "model": str(model.model),  # Use actual model from LitellmModel
            "region": actual_region,
            "response": result.final_output,
            "debug": {
                "boto3_session_region": actual_region,
                "available_nova_models": nova_models,
            },
        }
    except Exception as e:
        import traceback

        return {
            "status": "error",
            "error": str(e),
            "type": type(e).__name__,
            "traceback": traceback.format_exc(),
            "debug": {
                "boto3_session_region": session.region_name
                if "session" in locals()
                else "unknown",
                "env_vars": {
                    "AWS_REGION_NAME": os.environ.get("AWS_REGION_NAME"),
                    "AWS_REGION": os.environ.get("AWS_REGION"),
                    "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION"),
                },
            },
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
