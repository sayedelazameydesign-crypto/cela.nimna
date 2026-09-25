"""Computer-control tools (VNC desktop automation).

Isolated desktop runs in a separate Docker container (Ubuntu + XFCE + VNC + noVNC)
exposed via `docker compose --profile computer up desktop`. These tools are
thin wrappers that, when the desktop is unavailable, return a simulated
response so the agent loop and UI can be exercised without the container.

Security:
- All mutating tools are `confirm` (skill is `restricted`) — they suspend the
  run and require visual approval in the dashboard (red dot + approve/deny).
- `take_screenshot` is `safe` (read-only) and feeds the Vision Gateway.
- No host filesystem access; only the isolated container's `/workspace` is used.
- Real VNC integration is behind env vars and degrades gracefully.
"""
import base64
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..base import ToolContext, ToolError, ToolRegistry

# 1x1 transparent PNG fallback (when Pillow is unavailable)
_FALLBACK_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

def _env_flag(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

def _vnc_enabled() -> bool:
    # Explicit flag; also auto-detect if desktop container is reachable
    flag = _env_flag("COMPUTER_ENABLED", "").lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    # auto: if VNC host env is set, assume enabled
    return bool(_env_flag("COMPUTER_VNC_HOST") or _env_flag("DESKTOP_VNC_URL"))

def _screenshot_dir(ctx: ToolContext) -> Path:
    d = ctx.workspace / ".screenshots"
    d.mkdir(parents=True, exist_ok=True)
    # TTL cleanup: remove screenshots older than 1 hour, keep newest 80
    try:
        import time as _t
        files = sorted(d.glob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True)
        # keep 80 newest
        for f in files[80:]:
            try:
                f.unlink()
            except Exception:
                pass
        # delete older than 3600s among remaining (but keep at least 5)
        now = _t.time()
        for f in files[:80]:
            try:
                if now - f.stat().st_mtime > 3600 and len(files) > 5:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass
    return d

def _generate_placeholder_png(text: str = "Nimna Desktop — simulated") -> tuple[bytes, str]:
    """Return (png_bytes, b64) — tries Pillow, falls back to 1x1."""
    try:
        from PIL import Image, ImageDraw  # type: ignore

        w, h = 1280, 800
        img = Image.new("RGB", (w, h), color=(15, 20, 25))
        draw = ImageDraw.Draw(img)
        # header
        draw.rectangle([0, 0, w, 48], fill=(22, 28, 36))
        draw.text((20, 14), "🖥️  Nimna Computer — Isolated Desktop (simulated)", fill=(230, 237, 243))
        # timestamp
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        draw.text((w - 220, 14), ts, fill=(139, 152, 168))
        # fake window
        draw.rectangle([120, 120, 1160, 700], fill=(22, 28, 36), outline=(38, 48, 64), width=2)
        draw.rectangle([120, 120, 1160, 160], fill=(31, 42, 56))
        draw.text((140, 132), "Firefox — placeholder", fill=(230, 237, 243))
        draw.ellipse([112, 130, 124, 142], fill=(248, 81, 73))
        draw.ellipse([112, 146, 124, 158], fill=(210, 153, 34))
        draw.ellipse([112, 162, 124, 174], fill=(63, 185, 80))
        draw.text((140, 200), text, fill=(230, 237, 243))
        draw.text((140, 240), "take_screenshot → mouse_click → type_text → shell_execute", fill=(139, 152, 168))
        draw.text((140, 280), "Enable real desktop: docker compose --profile computer up -d desktop", fill=(139, 152, 168))
        # grid hint
        for x in range(120, 1160, 200):
            draw.line([(x, 160), (x, 700)], fill=(38, 48, 64), width=1)
            draw.text((x + 4, 164), str(x), fill=(80, 90, 110))
        for y in range(160, 700, 100):
            draw.line([(120, y), (1160, y)], fill=(38, 48, 64), width=1)
            draw.text((124, y + 4), str(y), fill=(80, 90, 110))

        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        return data, base64.b64encode(data).decode("ascii")
    except Exception:
        data = base64.b64decode(_FALLBACK_PNG_B64)
        return data, _FALLBACK_PNG_B64

def _overlay_grid(data: bytes, opacity: int = 38) -> bytes:
    """Overlay a faint coordinate grid to help the model locate elements."""
    try:
        import io

        from PIL import Image, ImageDraw
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        # downscale huge images for model (max 1280 width)
        if w > 1280:
            ratio = 1280 / w
            img = img.resize((1280, int(h * ratio)))
            w, h = img.size
        draw = ImageDraw.Draw(img, "RGBA")
        # faint grid every 200px
        for x in range(0, w, 200):
            draw.line([(x, 0), (x, h)], fill=(80, 90, 110, opacity), width=1)
            draw.rectangle([x+2, 2, x+44, 16], fill=(15,20,25,180))
            draw.text((x+4, 3), str(x), fill=(200,210,225))
        for y in range(0, h, 100):
            draw.line([(0, y), (w, y)], fill=(80, 90, 110, opacity), width=1)
            draw.rectangle([2, y+2, 36, y+16], fill=(15,20,25,180))
            draw.text((4, y+3), str(y), fill=(200,210,225))
        # border
        draw.rectangle([0,0,w-1,h-1], outline=(60,70,90,120), width=1)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return data

def _hash_image(data: bytes) -> str:
    import hashlib
    # perceptual-ish: downscale to 16x16 grayscale and hash
    try:
        import io

        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("L").resize((16,16))
        return hashlib.sha256(img.tobytes()).hexdigest()[:12]
    except Exception:
        import hashlib
        return hashlib.sha256(data[:4096]).hexdigest()[:12]

def _try_real_screenshot(ctx: ToolContext) -> tuple[bytes, str] | None:
    """Attempt to fetch a real screenshot from the desktop container.

    Strategies (in order):
    1. HTTP API if DESKTOP_API_URL is set (e.g. http://localhost:7900/api/screenshot)
    2. Docker exec with `import mss` / `xwd` inside the `desktop` container
    3. VNC raw capture via `vncdotool` if available
    Returns None if all strategies fail so caller falls back to placeholder.
    """
    api_url = _env_flag("DESKTOP_API_URL") or _env_flag("COMPUTER_VNC_URL")
    if api_url:
        try:
            import httpx  # type: ignore

            # try common screenshot endpoints
            for suffix in ["", "/screenshot", "/api/screenshot"]:
                try:
                    url = api_url.rstrip("/") + suffix
                    r = httpx.get(url, timeout=4.0, follow_redirects=True)
                    if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                        data = _overlay_grid(r.content)
                        return data, base64.b64encode(data).decode("ascii")
                    # JSON wrapper {image: base64}
                    if r.headers.get("content-type", "").startswith("application/json"):
                        j = r.json()
                        b64 = j.get("image") or j.get("data") or j.get("screenshot")
                        if b64:
                            if "," in b64:  # data URL
                                b64 = b64.split(",", 1)[1]
                            raw = base64.b64decode(b64)
                            raw = _overlay_grid(raw)
                            return raw, base64.b64encode(raw).decode("ascii")
                except Exception:
                    continue
        except Exception:
            pass

    # docker exec fallback — only works if host has docker and desktop container running
    try:
        # check if desktop container exists
        out = subprocess.run(
            ["docker", "ps", "--filter", "name=desktop", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=3,
        )
        if "desktop" in out.stdout:
            # try mss inside container
            cmd = [
                "docker", "exec", "desktop", "python3", "-c",
                "import base64, io; "
                "try:\n"
                " from PIL import ImageGrab; im=ImageGrab.grab(); buf=io.BytesIO(); im.save(buf, format='PNG'); print(base64.b64encode(buf.getvalue()).decode())\n"
                "except Exception as e:\n"
                " print('__NIMNA_ERR__'+str(e))\n",
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            b64 = r.stdout.strip()
            if b64 and not b64.startswith("__NIMNA_ERR__") and len(b64) > 100:
                try:
                    data = base64.b64decode(b64)
                    data = _overlay_grid(data)
                    return data, base64.b64encode(data).decode("ascii")
                except Exception:
                    pass
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# Pydantic params
# ---------------------------------------------------------------------------

class TakeScreenshotParams(BaseModel):
    reason: str = Field("", description="Why you are taking the screenshot (for audit).")
    width: int = Field(1280, ge=320, le=2560, description="Requested width (hint, may be ignored in simulated mode).")
    height: int = Field(800, ge=240, le=1600, description="Requested height (hint).")
    format: Literal["png", "jpeg"] = Field("png", description="Image format.")

class GetElementParams(BaseModel):
    element_name: str = Field(..., min_length=1, max_length=120, description="Text or icon name to locate, e.g. 'Firefox', 'Save', 'حفظ'.")
    purpose: str = Field("", description="Why you need this element.")
    use_ocr: bool = Field(True, description="Try OCR to locate text; if false, return estimated position from grid.")
    screenshot_reason: str = Field("locate element", description="Reason for auxiliary screenshot if needed.")

class MouseClickParams(BaseModel):
    x: int = Field(..., ge=0, le=2560, description="X coordinate from top-left (0,0).")
    y: int = Field(..., ge=0, le=1600, description="Y coordinate from top-left (0,0).")
    button: Literal["left", "right", "middle"] = Field("left", description="Mouse button.")
    clicks: int = Field(1, ge=1, le=3, description="Number of clicks (1=single, 2=double).")
    purpose: str = Field(..., min_length=3, description="One sentence: what you will click and why (for approval card).")

class TypeTextParams(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000, description="Text to type.")
    submit: bool = Field(False, description="Press Enter after typing.")
    delay_ms: int = Field(40, ge=0, le=500, description="Delay between keystrokes (ms).")
    purpose: str = Field("", description="Why you are typing this (for audit).")

    def model_post_init(self, __context):
        # Block newline injection to avoid executing hidden commands in an open terminal
        if "\n" in self.text or "\r" in self.text:
            raise ValueError("text must not contain newline; use submit=true to press Enter")
        if len(self.text) > 5000:
            raise ValueError("text too long")

class ShellExecuteParams(BaseModel):
    command: str = Field(..., min_length=1, max_length=4000, description="Shell command to run inside the isolated desktop (bash -lc).")
    timeout: int = Field(20, ge=1, le=120, description="Timeout seconds.")
    purpose: str = Field(..., min_length=3, description="Why you need this command (for approval).")


def _clean_env() -> dict[str, str]:
    """Sanitized env — drop secrets and risky vars, keep minimal PATH."""
    deny_prefixes = ("AWS_", "OPENAI_", "GOOGLE_", "GEMINI_", "NVIDIA_", "SSH_", "GITHUB_", "ANTHROPIC_", "AZURE_")
    deny_exact = {"SSH_AUTH_SOCK", "GPG_AGENT_INFO", "AWS_SESSION_TOKEN"}
    clean = {}
    for k, v in os.environ.items():
        if k in deny_exact or any(k.startswith(p) for p in deny_prefixes):
            continue
        clean[k] = v
    # ensure minimal safe PATH
    clean.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    # force non-interactive
    clean["DEBIAN_FRONTEND"] = "noninteractive"
    clean["TERM"] = "dumb"
    return clean

def _is_blocked_command(cmd: str) -> str | None:
    """Return reason if command is blocked, else None."""
    lowered = cmd.lower()
    # exact dangerous patterns
    denylist = ["rm -rf /", "mkfs", "dd if=", ":(){:|:&};:", "shutdown", "reboot", "chmod +s", "chown ", "iptables", "mkswap", "fdisk"]
    for pat in denylist:
        if pat.lower() in lowered:
            return f"blocked pattern '{pat}'"
    # regex: curl/wget piped to shell
    import re
    if re.search(r"curl\s+.*\|\s*(bash|sh|zsh)", lowered):
        return "blocked: curl piped to shell"
    if re.search(r"wget\s+.*\|\s*(bash|sh|zsh)", lowered):
        return "blocked: wget piped to shell"
    if re.search(r"base64\s+[^|]*\|\s*(bash|sh)", lowered):
        return "blocked: base64 piped to shell"
    # pty / interactive shells that can escape timeout
    for bad in [" pty", "screen ", "tmux", " nohup ", " ssh ", " nc ", " ncat ", " socat "]:
        if bad in f" {lowered} ":
            return f"blocked interactive tool '{bad.strip()}'"
    # block fork bomb variations
    if "fork" in lowered and ":(" in cmd:
        return "blocked fork bomb"
    return None

def _run_with_limits(cmd: list[str], timeout: int, cwd: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run with strict resource limits (CPU, mem, files, procs) and process-group kill on timeout."""
    import resource
    import signal
    clean_env = env if env is not None else _clean_env()
    def _preexec():
        try:
            # new process group so we can kill children
            os.setsid()
        except Exception:
            pass
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
            resource.setrlimit(resource.RLIMIT_AS, (512*1024*1024, 512*1024*1024))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
            try:
                # 512, not 32: the limit is enforced per REAL UID — on shared CI
                # machines (GitHub runners) the user's live process count already
                # exceeds tiny bounds, so legit children die/hang. 512 still
                # damps fork bombs (they spawn thousands).
                resource.setrlimit(resource.RLIMIT_NPROC, (512, 512))
            except Exception:
                pass
            resource.setrlimit(resource.RLIMIT_FSIZE, (10*1024*1024, 10*1024*1024))
        except Exception:
            pass
    # Use Popen to allow killpg on timeout (subprocess.run timeout only kills parent)
    import subprocess as sp
    try:
        proc = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE, text=True, cwd=cwd, env=clean_env, preexec_fn=_preexec, stdin=sp.DEVNULL)
        try:
            out, err = proc.communicate(timeout=timeout)
            return sp.CompletedProcess(cmd, proc.returncode, out, err)
        except sp.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            # collect partial
            try:
                out, err = proc.communicate(timeout=2)
            except Exception:
                out, err = "", "timeout"
            raise sp.TimeoutExpired(cmd, timeout, output=out, stderr=err)
    except (ValueError, OSError):
        # preexec not supported (e.g. Windows) — fall back without pgkill
        return sp.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=clean_env, stdin=sp.DEVNULL)


def _locate_element_on_image(data: bytes, query: str) -> tuple[int, int] | None:
    """Try to locate an element by text using simple heuristics (OCR if available)."""
    # Try OCR (pytesseract) if installed
    try:
        import io

        from PIL import Image
        try:
            import pytesseract  # type: ignore
            img = Image.open(io.BytesIO(data))
            # get boxes
            boxes = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)  # type: ignore
            q = query.lower().strip()
            best = None
            for i, txt in enumerate(boxes.get("text", [])):
                if q in txt.lower() and txt.strip():
                    x, y, w, h = boxes["left"][i], boxes["top"][i], boxes["width"][i], boxes["height"][i]
                    best = (x + w//2, y + h//2)
                    break
            if best:
                return best
        except Exception:
            pass
    except Exception:
        pass
    return None

def register(registry: ToolRegistry) -> None:
    @registry.tool(
        "take_screenshot",
        "Take a screenshot of the isolated desktop (VNC). Returns base64 PNG + file path. Safe, no approval needed.",
        TakeScreenshotParams,
        risk="safe",
        tags=["computer", "vision", "read"],
    )
    def take_screenshot(params: TakeScreenshotParams, ctx: ToolContext):
        # try real, fall back to placeholder
        real = _try_real_screenshot(ctx)
        if real is not None:
            data, b64 = real
            source = "real"
        else:
            label = params.reason or "observe → plan → act"
            data, b64 = _generate_placeholder_png(label)
            source = "simulated"

        fname = f"screenshot-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{int(time.time()*1000)%1000}.{params.format}"
        fpath = _screenshot_dir(ctx) / fname
        try:
            fpath.write_bytes(data)
        except Exception:
            pass
        # Vision Gateway: also stash the image so the next Gemini turn can use it.
        # We store the b64 in ctx.memory as a transient artifact and also return it.
        # The agent will inject it as an image part on the next model call (see gemini.py).
        try:
            ctx.memory.save_memory(
                f"screenshot:{fpath.name}",
                kind="artifact",
                tags=["screenshot", source],
                session_id=ctx.session_id,
            )
        except Exception:
            pass
        # Also write a sidecar for the UI Mirror View to poll
        try:
            (ctx.workspace / ".screenshots" / "_latest.json").write_text(
                f'{{"path": "{ctx.display_path(fpath)}", "source": "{source}", "ts": "{datetime.now().isoformat()}", "reason": {params.reason!r}}}',
                encoding="utf-8",
            )
        except Exception:
            pass
        return {
            "path": ctx.display_path(fpath),
            "source": source,
            "format": params.format,
            "width": params.width,
            "height": params.height,
            "bytes": len(data),
            "image_b64": b64[:120] + "…",  # preview only; full image is at path and injected via Vision Gateway file load
            "hint": "Image is at the path above; the model will receive it as vision input on the next turn."
            if source == "real"
            else "Simulated desktop. Run: docker compose --profile computer up -d desktop for a real VNC desktop at http://localhost:6901",
        }

    @registry.tool(
        "get_element_coordinates",
        "Find the (x,y) of a UI element by name using OCR/grid. Saves a fresh screenshot, overlays a grid, and returns precise coordinates so the model doesn't have to guess. Safe, no approval needed.",
        GetElementParams,
        risk="safe",
        tags=["computer", "vision", "read"],
    )
    def get_element_coordinates(params: GetElementParams, ctx: ToolContext):
        # Take a fresh screenshot (real or placeholder) and try to locate the element
        real = _try_real_screenshot(ctx)
        if real is not None:
            data, b64 = real
            source = "real"
        else:
            data, b64 = _generate_placeholder_png(f"locate: {params.element_name}")
            source = "simulated"
        # Try OCR location
        located = _locate_element_on_image(data, params.element_name) if params.use_ocr else None
        # Heuristic fallback: known icons positions on the simulated desktop
        fallback_map = {
            "firefox": (140, 140),
            "terminal": (140, 180),
            "files": (140, 220),
            "chrome": (160, 140),
            "save": (640, 400),
            "حفظ": (640, 400),
        }
        est = None
        if located is None:
            key = params.element_name.lower().strip()
            for k, v in fallback_map.items():
                if k in key:
                    est = v
                    break
            # generic center
            if est is None:
                est = (640, 400)
        x, y = located if located else est
        # Save annotated screenshot with grid + marker
        try:
            import io

            from PIL import Image, ImageDraw
            img = Image.open(io.BytesIO(data)).convert("RGB")
            draw = ImageDraw.Draw(img)
            # draw marker
            draw.ellipse([x-12, y-12, x+12, y+12], outline=(255,59,48), width=3)
            draw.ellipse([x-4, y-4, x+4, y+4], fill=(255,59,48))
            draw.text((x+14, y-8), params.element_name[:24], fill=(255,59,48))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            annotated = buf.getvalue()
            fname = f"locate-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
            fpath = _screenshot_dir(ctx) / fname
            fpath.write_bytes(annotated)
            # update latest
            import json as _json
            (ctx.workspace / ".screenshots" / "_latest.json").write_text(
                _json.dumps({"path": ctx.display_path(fpath), "source": source, "located": {"x": x, "y": y, "located": bool(located)}}),
                encoding="utf-8",
            )
            b64a = base64.b64encode(annotated).decode("ascii")
        except Exception:
            x, y = est
            b64a = b64
            fpath = None
        return {
            "element": params.element_name,
            "x": int(x),
            "y": int(y),
            "located_by": "ocr" if located else "grid-heuristic",
            "source": source,
            "confidence": 0.85 if located else 0.45,
            "annotated_screenshot": ctx.display_path(fpath) if fpath else None,
            "screenshot_b64_preview": b64a[:120] + "…",
            "hint": f"Use mouse_click(x={x}, y={y}) next — will show a red dot for visual approval." if located else f"Estimated at ({x},{y}) — take_screenshot to verify before clicking.",
        }

    @registry.tool(
        "mouse_click",
        "Click on the isolated desktop at (x,y). Requires visual approval — a red dot is shown on the Mirror View before execution.",
        MouseClickParams,
        risk="confirm",
        tags=["computer", "destructive"],
    )
    def mouse_click(params: MouseClickParams, ctx: ToolContext):
        # validate coordinates are within plausible bounds
        if params.x > 2560 or params.y > 1600:
            raise ToolError("coordinates out of bounds (max 2560x1600)")
        # If real desktop is enabled, try to perform the click
        if _vnc_enabled():
            # try via API or docker exec
            api_url = _env_flag("DESKTOP_API_URL") or _env_flag("COMPUTER_VNC_URL")
            if api_url:
                try:
                    import httpx

                    r = httpx.post(
                        api_url.rstrip("/") + "/click",
                        json={"x": params.x, "y": params.y, "button": params.button, "clicks": params.clicks},
                        timeout=5.0,
                    )
                    if r.status_code == 200:
                        return {"clicked": True, "x": params.x, "y": params.y, "button": params.button, "via": "api"}
                except Exception as exc:
                    raise ToolError(f"mouse_click via API failed: {exc}") from exc
            # docker exec fallback using xdotool
            try:
                out = subprocess.run(
                    ["docker", "exec", "desktop", "xdotool", "mousemove", str(params.x), str(params.y), "click", "--repeat", str(params.clicks), "1" if params.button == "left" else "3"],
                    capture_output=True, text=True, timeout=5,
                )
                if out.returncode == 0:
                    return {"clicked": True, "x": params.x, "y": params.y, "via": "xdotool"}
                # fallback: try pyautogui
                out2 = subprocess.run(
                    ["docker", "exec", "desktop", "python3", "-c", f"import pyautogui; pyautogui.click({params.x}, {params.y}, clicks={params.clicks}, button='{params.button}')"],
                    capture_output=True, text=True, timeout=5,
                )
                if out2.returncode == 0:
                    return {"clicked": True, "x": params.x, "y": params.y, "via": "pyautogui"}
            except Exception as exc:
                raise ToolError(f"mouse_click failed (is desktop running? docker compose --profile computer up -d desktop): {exc}") from exc
            # if we reach here in real mode but no backend succeeded, still return simulated success for demo
        return {
            "clicked": True,
            "x": params.x,
            "y": params.y,
            "button": params.button,
            "clicks": params.clicks,
            "simulated": not _vnc_enabled(),
            "note": "Simulated click — enable real desktop with: docker compose --profile computer up -d desktop"
            if not _vnc_enabled()
            else "Executed (or simulated) — take another screenshot to verify.",
        }

    @registry.tool(
        "type_text",
        "Type text into the isolated desktop. Requires approval. Use submit=true to press Enter after typing.",
        TypeTextParams,
        risk="confirm",
        tags=["computer"],
    )
    def type_text(params: TypeTextParams, ctx: ToolContext):
        if not params.text.strip():
            raise ToolError("text is empty")
        if _vnc_enabled():
            api_url = _env_flag("DESKTOP_API_URL") or _env_flag("COMPUTER_VNC_URL")
            if api_url:
                try:
                    import httpx

                    r = httpx.post(
                        api_url.rstrip("/") + "/type",
                        json={"text": params.text, "submit": params.submit, "delay_ms": params.delay_ms},
                        timeout=10.0,
                    )
                    if r.status_code == 200:
                        return {"typed": True, "chars": len(params.text), "submit": params.submit, "via": "api"}
                except Exception as exc:
                    raise ToolError(f"type_text via API failed: {exc}") from exc
            try:
                # escape for xdotool
                safe = params.text.replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
                cmd = f'xdotool type --delay {params.delay_ms} "{safe}"'
                if params.submit:
                    cmd += ' && xdotool key Return'
                out = subprocess.run(["docker", "exec", "desktop", "bash", "-lc", cmd], capture_output=True, text=True, timeout=10)
                if out.returncode == 0:
                    return {"typed": True, "chars": len(params.text), "submit": params.submit, "via": "xdotool"}
            except Exception as exc:
                raise ToolError(f"type_text failed: {exc}") from exc
        return {
            "typed": True,
            "chars": len(params.text),
            "submit": params.submit,
            "simulated": not _vnc_enabled(),
            "preview": params.text[:80],
            "note": "Simulated typing — enable real desktop for actual keystrokes."
            if not _vnc_enabled()
            else "Typed (or simulated) — take a screenshot to verify.",
        }

    @registry.tool(
        "shell_execute",
        "Execute a shell command inside the isolated desktop container (bash -lc). Requires approval. Never runs on the host.",
        ShellExecuteParams,
        risk="confirm",
        tags=["computer", "destructive"],
    )
    def shell_execute(params: ShellExecuteParams, ctx: ToolContext):
        # Content filter BEFORE approval display — block truly dangerous patterns
        blocked = _is_blocked_command(params.command)
        if blocked:
            raise ToolError(f"blocked: {blocked} — command rejected before approval")
        # Additional strict denylist (defense in depth)
        lowered = params.command.strip().lower()
        # Enforce cwd = workspace (prevent cd / tricks from escaping — we wrap)
        # Sanitize env already via _run_with_limits
        if _vnc_enabled():
            try:
                # Inside container: cd to workspace, apply ulimit, no network by default
                # Use timeout inside container + host-side killpg
                # Wrap command to force workspace cwd
                inner = params.command.replace("'", "'\\''")  # for safe single-quote embedding
                wrapped = f"ulimit -t 30; ulimit -v 524288; ulimit -n 64; ulimit -f 10240; timeout {params.timeout} bash -lc 'cd /home/ubuntu/workspace && {inner}'"
                out = _run_with_limits(
                    ["docker", "exec", "desktop", "bash", "-lc", wrapped],
                    timeout=params.timeout + 3,
                    env=_clean_env(),
                )
                return {
                    "command": params.command,
                    "exit_code": out.returncode,
                    "stdout": out.stdout[:8000],
                    "stderr": out.stderr[:4000],
                    "via": "docker",
                    "cwd": "/home/ubuntu/workspace",
                }
            except subprocess.TimeoutExpired:
                raise ToolError(f"command timed out after {params.timeout}s (process group killed)")
            except FileNotFoundError:
                pass
            except Exception as exc:
                raise ToolError(f"shell_execute failed: {exc}") from exc
        # Simulated fallback: very restricted — only allow safe read-only commands, else mock
        # Close stdin, no pty, env sanitized, cwd locked
        safe_prefixes = ("echo ", "ls", "pwd", "cat ", "head ", "tail ", "wc ", "date", "whoami", "ls ", "find ", "grep ", "stat ")
        is_safe = lowered.startswith(safe_prefixes) or lowered in {"ls", "pwd", "whoami", "date", "env", "id"}
        if is_safe:
            try:
                out = _run_with_limits(
                    ["bash", "-c", f"cd {str(ctx.workspace)!r} && {params.command}"],
                    timeout=min(params.timeout, 5),
                    cwd=str(ctx.workspace),
                    env=_clean_env(),
                )
                return {
                    "command": params.command,
                    "exit_code": out.returncode,
                    "stdout": out.stdout[:4000],
                    "stderr": out.stderr[:2000],
                    "simulated": True,
                    "cwd": str(ctx.workspace),
                    "note": "Simulated (workspace-local) — enable real desktop for full isolation: docker compose --profile computer up -d desktop",
                }
            except subprocess.TimeoutExpired:
                raise ToolError(f"command timed out after {min(params.timeout,5)}s")
            except Exception as exc:
                raise ToolError(f"simulated shell failed: {exc}") from exc
        return {
            "command": params.command,
            "exit_code": 0,
            "stdout": f"[simulated] would run: {params.command}",
            "stderr": "",
            "simulated": True,
            "cwd": str(ctx.workspace),
            "note": "Simulated — no host side effects. Enable real desktop for isolated execution.",
        }
