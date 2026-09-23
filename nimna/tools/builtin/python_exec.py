"""`run_python` tool backed by the sandbox module."""
from typing import Optional

from pydantic import BaseModel, Field

from ..base import Risk, ToolContext, ToolRegistry
from ..sandbox import run_python_code


class RunPythonParams(BaseModel):
    code: str = Field(..., description="Python 3 source code to execute. Print what you want to see.")
    timeout_seconds: Optional[int] = Field(None, ge=1, le=300, description="Override the default timeout.")
    purpose: str = Field("", description="One line explaining what the code does (shown to the user).")


def _python_risk(params: BaseModel, ctx: ToolContext) -> Risk:
    # Docker with --network none is real isolation (safe). Bare subprocess
    # is NOT a security boundary – keep it behind an approval gate.
    return "safe" if ctx.settings.sandbox_backend == "docker" else "confirm"


def register(registry: ToolRegistry) -> None:
    @registry.tool(
        "run_python",
        "Execute Python code in an isolated sandbox with the workspace as working directory. "
        "No network in docker mode. Returns stdout/stderr/exit code. Keep scripts short and print results.",
        RunPythonParams, risk_fn=_python_risk, tags=["code", "execute"],
    )
    def run_python(params: RunPythonParams, ctx: ToolContext):
        result = run_python_code(params.code, ctx.settings, ctx.workspace, timeout=params.timeout_seconds)
        payload = result.model_dump()
        payload["ok"] = result.ok
        return payload
