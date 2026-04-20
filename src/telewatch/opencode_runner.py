import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

class OpenCodeRunner:
    """
    Thin async wrapper around `opencode run`.
    Uses whatever model/provider is set in the local opencode config.
    """

    def __init__(self, working_dir: str | Path, timeout: int = 120, model: str = None):
        self.working_dir = Path(working_dir)
        self.timeout     = timeout
        self.model       = model

    # ── public ──────────────────────────────────────────────────────────────

    @staticmethod
    def is_available() -> bool:
        """Returns True if the opencode binary is on PATH."""
        return shutil.which('opencode') is not None

    async def run(self, prompt: str) -> str:
        """
        Run opencode non-interactively with the given prompt.
        Returns stdout on success, or a descriptive error string.
        """
        if not self.is_available():
            return (
                '[OpenCode not installed — falling back to direct LLM]\n'
                'Install: curl -fsSL https://opencode.ai/install | sh'
            )

        try:
            cmd_args = ['opencode', 'run']
            if self.model:
                cmd_args.extend(['--model', self.model])
            cmd_args.append(prompt)
            
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=str(self.working_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return (
                    f'[OpenCode timed out after {self.timeout}s]\n'
                    'Try a more specific query or increase timeout_seconds in config.'
                )

            output = stdout.decode('utf-8', errors='replace').strip()
            if not output:
                err = stderr.decode('utf-8', errors='replace').strip()
                return f'[OpenCode returned no output]\n{err}' if err else '[No output]'

            return output

        except FileNotFoundError:
            return '[opencode binary not found in PATH]'
        except Exception as exc:  # noqa: BLE001
            logger.exception('OpenCode runner error')
            return f'[OpenCode error: {exc}]'

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def chunk(text: str, max_len: int = 4000) -> list[str]:
        """Split long output into Telegram-safe chunks (< 4096 chars)."""
        lines, current, chunks = text.splitlines(), [], []
        for line in lines:
            if sum(len(l) + 1 for l in current) + len(line) > max_len:
                chunks.append('\n'.join(current))
                current = []
            current.append(line)
        if current:
            chunks.append('\n'.join(current))
        return chunks or ['[empty response]']
