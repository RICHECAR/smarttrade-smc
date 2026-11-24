"""Simple Ollama (DeepSeek) client wrapper for local analysis.

This module executes the local `ollama` CLI against the `deepseek-r1` model
and returns the raw text response. Ollama must be installed and the model
available locally.
"""
import subprocess
import shlex
from typing import Optional


def analyze_text(prompt: str, timeout: int = 30) -> str:
    """Send a prompt to the local Ollama DeepSeek model and return output text.

    Note: behavior depends on local ollama CLI version. This uses the same
    approach as the backend (exec 'ollama run deepseek-r1 <prompt>').
    """
    try:
        proc = subprocess.run(
            ['ollama', 'run', 'deepseek-r1', prompt],
            capture_output=True,
            text=True,
            timeout=timeout
        )

        if proc.returncode != 0:
            # Return stderr for debugging
            return f"OLLAMA_ERROR: {proc.stderr.strip()}"

        return proc.stdout.strip()
    except FileNotFoundError:
        return "OLLAMA_NOT_FOUND: Ensure 'ollama' is installed and in PATH"
    except subprocess.TimeoutExpired:
        return "OLLAMA_TIMEOUT: Model did not respond within timeout"
    except Exception as e:
        return f"OLLAMA_EXCEPTION: {str(e)}"


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Send a prompt to local Ollama deepseek-r1')
    parser.add_argument('prompt', nargs='+')
    args = parser.parse_args()

    prompt_text = ' '.join(args.prompt)
    print(analyze_text(prompt_text))
