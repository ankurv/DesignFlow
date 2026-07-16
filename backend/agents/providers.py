"""Provider adapters with native logical sessions where available."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from .base import AgentBase, AgentConfig, Usage


def _usable_model_ids(raw_ids: list[str]) -> list[str]:
    """Keep general text-generation models and remove obvious non-chat endpoints."""
    excluded = (
        "embed", "embedding", "whisper", "tts", "speech", "audio", "image",
        "dall-e", "moderation", "realtime", "transcribe", "guard", "vision-preview",
    )
    unique = []
    for raw in raw_ids:
        model_id = str(raw or "").strip()
        if not model_id or any(token in model_id.lower() for token in excluded):
            continue
        if model_id not in unique:
            unique.append(model_id)
    return unique


def discover_models(config: AgentConfig) -> list[str]:
    """Query a configured provider for currently available generation models."""
    kind = config.kind
    if kind == "openai":
        import openai
        key = config.api_key or os.environ.get("OPENAI_API_KEY", "")
        kwargs = {"api_key": key} if key else {}
        if config.base_url:
            kwargs["base_url"] = config.base_url
            if "openrouter.ai" in config.base_url:
                kwargs["default_headers"] = {
                    "HTTP-Referer": "http://localhost:8000",
                    "X-Title": "DesignFlow"
                }
        models = openai.OpenAI(**kwargs).models.list()
        ids = [item.id for item in models.data]
    elif kind == "groq":
        from groq import Groq
        key = config.api_key or os.environ.get("GROQ_API_KEY", "")
        kwargs = {"api_key": key} if key else {}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        models = Groq(**kwargs).models.list()
        ids = [item.id for item in models.data]
    elif kind == "claude":
        import anthropic
        key = config.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        kwargs = {"api_key": key} if key else {}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        models = anthropic.Anthropic(**kwargs).models.list()
        ids = [item.id for item in models.data]
    elif kind == "gemini":
        import google.generativeai as genai
        key = config.api_key or os.environ.get("GEMINI_API_KEY", "")
        if key:
            genai.configure(api_key=key)
        ids = [
            item.name.removeprefix("models/")
            for item in genai.list_models()
            if "generateContent" in (getattr(item, "supported_generation_methods", []) or [])
        ]
    elif kind == "ollama":
        import urllib.request
        base_url = (config.base_url or config.extra.get("base_url") or "http://localhost:11434").rstrip("/")
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=15) as response:
            payload = json.loads(response.read())
        ids = [item.get("name", "") for item in payload.get("models", [])]
    else:
        return [config.model] if config.model else []

    models = _usable_model_ids(ids)
    if config.model and config.model in models:
        models.remove(config.model)
        models.insert(0, config.model)
    return models[:20]


class ClaudeAgent(AgentBase):
    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._configure_client()

    def _configure_client(self):
        import anthropic

        key = self.config.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        base_url = getattr(self.config, "base_url", "")
        platform = self.config.extra.get("platform", "standard")
        
        if platform == "bedrock":
            self._client = anthropic.AnthropicBedrock()
        elif platform == "vertex":
            self._client = anthropic.AnthropicVertex()
        else:
            kwargs = {}
            if key:
                kwargs["api_key"] = key
            if base_url:
                kwargs["base_url"] = base_url
            self._client = anthropic.Anthropic(**kwargs)

    def reconfigure(self, config: AgentConfig):
        previous_key = self.config.api_key
        super().reconfigure(config)
        if config.api_key != previous_key:
            self._configure_client()

    def _raw_send(self, messages: list[dict], system: str, mcp_tools: list[dict] = None, tool_handler: Callable = None) -> tuple[str, Usage]:
        from typing import Callable
        system_value = (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if system else ""
        )
        
        current_messages = list(messages)
        total_input = 0
        total_cached = 0
        total_output = 0
        
        claude_tools = []
        if mcp_tools:
            for t in mcp_tools:
                claude_tools.append({
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["inputSchema"],
                })

        iterations = 0
        while iterations < 10:
            iterations += 1
            
            kwargs = {
                "model": self.config.model or "claude-sonnet-4-6",
                "max_tokens": self.config.extra.get("max_tokens", 2000),
                "system": system_value,
                "messages": current_messages,
            }
            if claude_tools:
                kwargs["tools"] = claude_tools

            response = self._client.messages.create(**kwargs)
            
            raw = response.usage
            cached = int(getattr(raw, "cache_read_input_tokens", 0) or 0)
            cache_write = int(getattr(raw, "cache_creation_input_tokens", 0) or 0)
            total_input += int(getattr(raw, "input_tokens", 0) or 0) + cached + cache_write
            total_cached += cached
            total_output += int(getattr(raw, "output_tokens", 0) or 0)
            
            if response.stop_reason == "tool_use":
                assistant_message = {"role": "assistant", "content": []}
                tool_results = []
                
                for block in response.content:
                    if block.type == "text":
                        assistant_message["content"].append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        assistant_message["content"].append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input
                        })
                        try:
                            if not tool_handler:
                                raise RuntimeError("No tool_handler provided")
                            res_text = tool_handler(block.name, block.input)
                            is_error = False
                        except Exception as e:
                            res_text = str(e)
                            is_error = True
                            
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": res_text,
                            "is_error": is_error
                        })
                current_messages.append(assistant_message)
                current_messages.append({"role": "user", "content": tool_results})
            else:
                text_content = [b.text for b in response.content if b.type == "text"]
                usage = Usage(
                    input_tokens=total_input,
                    cached_input_tokens=total_cached,
                    output_tokens=total_output,
                )
                return "\n".join(text_content), usage
                
        return "Error: Agent exceeded maximum tool execution limit.", Usage(
            input_tokens=total_input,
            cached_input_tokens=total_cached,
            output_tokens=total_output,
        )


class OpenAIAgent(AgentBase):
    """Responses API adapter chained with previous_response_id."""

    manages_context = True

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._configure_client()
        self._response_id: str | None = None

    def _configure_client(self):
        import openai

        key = self.config.api_key or os.environ.get("OPENAI_API_KEY", "")
        base_url = getattr(self.config, "base_url", "")
        
        kwargs = {}
        if key:
            kwargs["api_key"] = key
        if base_url:
            kwargs["base_url"] = base_url
            if "openrouter.ai" in base_url:
                kwargs["default_headers"] = {
                    "HTTP-Referer": "http://localhost:8000",
                    "X-Title": "DesignFlow"
                }
            
        self._client = openai.OpenAI(**kwargs)

    def reconfigure(self, config: AgentConfig):
        previous_key = self.config.api_key
        super().reconfigure(config)
        if config.api_key != previous_key:
            self._configure_client()

    def _raw_send(self, messages: list[dict], system: str, mcp_tools: list[dict] = None, tool_handler: Callable = None) -> tuple[str, Usage]:
        import json
        from typing import Callable
        
        tools = []
        if mcp_tools:
            for t in mcp_tools:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["inputSchema"]
                    }
                })
                
        if tools:
            request_messages = ([{"role": "system", "content": system}] if system else []) + list(messages)
            
            iterations = 0
            while iterations < 10:
                iterations += 1
                kwargs = {
                    "model": self.config.model,
                    "messages": request_messages,
                    "max_tokens": self.config.extra.get("max_tokens", 2000),
                    "tools": tools,
                }
                    
                response = self._client.chat.completions.create(**kwargs)
                raw = response.usage
                
                msg = response.choices[0].message
                if getattr(msg, "tool_calls", None):
                    assistant_msg = {"role": "assistant", "content": msg.content or "", "tool_calls": []}
                    for tc in msg.tool_calls:
                        assistant_msg["tool_calls"].append({
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                        })
                    request_messages.append(assistant_msg)
                    
                    for tc in msg.tool_calls:
                        try:
                            args = json.loads(tc.function.arguments)
                            res = tool_handler(tc.function.name, args)
                        except Exception as e:
                            res = str(e)
                        request_messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": str(res)
                        })
                else:
                    return msg.content or "", Usage(
                        input_tokens=int(getattr(raw, "prompt_tokens", 0) or 0),
                        output_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
                    )
                    
            return "Error: Agent exceeded maximum tool execution limit.", Usage(
                input_tokens=0,
                output_tokens=0,
            )
            
        if "integrate.api.nvidia.com" in (self.config.base_url or ""):
            request_messages = ([{"role": "system", "content": system}] if system else []) + list(messages)
            kwargs = {
                "model": self.config.model,
                "messages": request_messages,
                "max_tokens": self.config.extra.get("max_tokens", 2000),
            }
            response = self._client.chat.completions.create(**kwargs)
            raw = response.usage
            return response.choices[0].message.content or "", Usage(
                input_tokens=int(getattr(raw, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
            )

        kwargs = {
            "model": self.config.model or "gpt-4o",
            "input": messages[-1]["content"],
            "instructions": system or None,
            "max_output_tokens": self.config.extra.get("max_tokens", 2000),
            "previous_response_id": self._response_id,
            "store": True,
            "prompt_cache_key": f"designflow-{self._session_id}",
        }
            
        compact_threshold = int(self.config.extra.get("compact_threshold", 0) or 0)
        if compact_threshold:
            kwargs["context_management"] = [{
                "type": "compaction",
                "compact_threshold": compact_threshold,
            }]
            
        response = self._client.responses.create(**kwargs)
        self._response_id = response.id
        raw = response.usage
        details = getattr(raw, "input_tokens_details", None)
        usage = Usage(
            input_tokens=int(getattr(raw, "input_tokens", 0) or 0),
            cached_input_tokens=int(getattr(details, "cached_tokens", 0) or 0),
            output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
        )
        return response.output_text, usage

    def _reset_provider_session(self):
        self._response_id = None

    def provider_session_id(self) -> str:
        return self._response_id or ""


class GroqAgent(AgentBase):
    """Native Groq chat-completions adapter."""

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._configure_client()

    def _configure_client(self):
        from groq import Groq

        key = self.config.api_key or os.environ.get("GROQ_API_KEY", "")
        kwargs = {}
        if key:
            kwargs["api_key"] = key
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        self._client = Groq(**kwargs)

    def reconfigure(self, config: AgentConfig):
        previous = (self.config.api_key, self.config.base_url)
        super().reconfigure(config)
        if (config.api_key, config.base_url) != previous:
            self._configure_client()

    def _raw_send(self, messages: list[dict], system: str, mcp_tools: list[dict] = None, tool_handler: Callable = None) -> tuple[str, Usage]:
        import json
        groq_messages = ([{"role": "system", "content": system}] if system else []) + list(messages)
        
        tools = []
        if mcp_tools:
            for t in mcp_tools:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["inputSchema"]
                    }
                })

        total_input = 0
        total_cached = 0
        total_output = 0

        iterations = 0
        while iterations < 10:
            iterations += 1
            request = {
                "model": self.config.model or "llama-3.3-70b-versatile",
                "messages": groq_messages,
                "max_tokens": int(self.config.extra.get("max_tokens", 2000) or 2000),
            }
            if "temperature" in self.config.extra:
                request["temperature"] = float(self.config.extra["temperature"])
            if tools:
                request["tools"] = tools

            response = self._client.chat.completions.create(**request)
            raw = getattr(response, "usage", None)
            prompt_details = getattr(raw, "prompt_tokens_details", None)
            
            total_input += int(getattr(raw, "prompt_tokens", 0) or 0)
            total_cached += int(getattr(prompt_details, "cached_tokens", 0) or 0)
            total_output += int(getattr(raw, "completion_tokens", 0) or 0)

            msg = response.choices[0].message
            if getattr(msg, "tool_calls", None):
                assistant_msg = {"role": "assistant", "content": msg.content or "", "tool_calls": []}
                for tc in msg.tool_calls:
                    assistant_msg["tool_calls"].append({
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    })
                groq_messages.append(assistant_msg)
                
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                        res = tool_handler(tc.function.name, args)
                    except Exception as e:
                        res = str(e)
                    groq_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(res)
                    })
            else:
                usage = Usage(
                    input_tokens=total_input,
                    cached_input_tokens=total_cached,
                    output_tokens=total_output,
                )
                return msg.content or "", usage
        
        return "Error: Agent exceeded maximum tool execution limit.", Usage(
            input_tokens=total_input,
            cached_input_tokens=total_cached,
            output_tokens=total_output,
        )


class GeminiAgent(AgentBase):
    """Retains the provider's ChatSession and sends only the new user turn."""

    manages_context = True

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._configure_provider()
        self._chat = None

    def _configure_provider(self):
        import google.generativeai as genai

        key = self.config.api_key or os.environ.get("GEMINI_API_KEY", "")
        if key:
            genai.configure(api_key=key)
        self._genai = genai
        self._model_name = self.config.model or "gemini-2.5-flash"

    def reconfigure(self, config: AgentConfig):
        previous_key = self.config.api_key
        previous_model = self.config.model
        super().reconfigure(config)
        if config.api_key != previous_key:
            self._configure_provider()
        if config.model != previous_model:
            self._model_name = config.model or "gemini-2.5-flash"
            self._chat = None

    def _raw_send(self, messages: list[dict], system: str, *args, **kwargs) -> tuple[str, Usage]:
        if self._chat is None:
            model = self._genai.GenerativeModel(
                self._model_name,
                system_instruction=system or None,
            )
            self._chat = model.start_chat(history=[])
        response = self._chat.send_message(messages[-1]["content"])
        raw = getattr(response, "usage_metadata", None)
        usage = Usage(
            input_tokens=int(getattr(raw, "prompt_token_count", 0) or 0),
            cached_input_tokens=int(getattr(raw, "cached_content_token_count", 0) or 0),
            output_tokens=int(getattr(raw, "candidates_token_count", 0) or 0),
        )
        return response.text, usage

    def _reset_provider_session(self):
        self._chat = None

    def provider_session_id(self) -> str:
        return self._session_id if self._chat is not None else ""


class CLIAgent(AgentBase):
    """CLI adapter with resumable Codex and Antigravity conversations."""

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._provider_session_id = ""
        self._cli_usage_snapshot = Usage()
        self._configure_working_directories(config)
        self._configure_command(config)

    def _configure_working_directories(self, config: AgentConfig):
        raw = config.working_directory.strip()
        if raw:
            project = Path(raw).expanduser().resolve()
            if not project.is_dir():
                raise ValueError(f"CLI project folder does not exist: {project}")
            self._workspace_cwd = str(project)
            session_key = re.sub(r"[^A-Za-z0-9_.-]", "-", config.id or self._session_id)
            session = project / ".designflow" / "sessions" / session_key
            session.mkdir(parents=True, exist_ok=True)
            self._session_cwd = str(session)
        else:
            # Direct library users may omit a project root. The server always injects one.
            self._workspace_cwd = ""
            self._session_cwd = tempfile.mkdtemp(prefix=f"designflow-{self._session_id}-")

    def _configure_command(self, config: AgentConfig):
        previous_mode = getattr(self, "_session_mode", "")
        self._argv = shlex.split(config.cli_command)
        if not self._argv:
            self._session_mode = "invalid"
            self.manages_context = False
            if previous_mode and previous_mode != self._session_mode:
                self._provider_session_id = ""
            return
        requested = str(config.extra.get("session_mode", "auto")).lower()
        command = Path(self._argv[0]).name.lower()
        joined = " ".join(self._argv).lower()
        if requested != "auto":
            self._session_mode = requested
        elif "codex" in command or re.search(r"\bcodex\s+exec\b", joined):
            self._session_mode = "codex"
        elif (
            command in {"agy", "antigravity"}
            or "antigravity" in command
            or "antigravity" in joined
            or re.search(r"\bagy\b", joined)
        ):
            self._session_mode = "antigravity"
        else:
            self._session_mode = "stateless"
        self.manages_context = self._session_mode in {"codex", "antigravity"}
        if previous_mode and previous_mode != self._session_mode:
            self._provider_session_id = ""

    def reconfigure(self, config: AgentConfig):
        previous = (
            self.config, list(self._argv), self._session_mode, self.manages_context,
            self._workspace_cwd, self._session_cwd,
        )
        try:
            super().reconfigure(config)
            self._configure_working_directories(config)
            self._configure_command(config)
        except Exception:
            (
                self.config, self._argv, self._session_mode, self.manages_context,
                self._workspace_cwd, self._session_cwd,
            ) = previous
            raise

    def _raw_send(self, messages: list[dict], system: str, *args, **kwargs) -> tuple[str, Usage]:
        if self._session_mode == "invalid" or not getattr(self, "_argv", None):
            raise RuntimeError(f"Agent '{self.name}' has no CLI command configured. Please add one in Settings.")
        if self._session_mode == "codex":
            return self._send_codex(messages[-1]["content"], system)
        if self._session_mode == "antigravity":
            return self._send_antigravity(messages[-1]["content"], system)
        return self._send_stateless(messages, system)

    def _run(self, argv: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=int(self.config.extra.get("timeout", 300)),
            cwd=cwd,
        )
        if result.returncode != 0 and not result.stdout:
            raise RuntimeError(result.stderr.strip() or f"CLI exited {result.returncode}")
        return result

    def _initial_prompt(self, message: str, system: str) -> str:
        if system:
            return f"[SYSTEM]\n{system}\n\n[USER]\n{message}"
        return message

    def _codex_parts(self) -> tuple[list[str], list[str]]:
        args = [arg for arg in self._argv if arg not in {"--json", "--ephemeral"}]
        try:
            idx = args.index("exec")
        except ValueError:
            return args + ["exec"], []
        return args[:idx + 1], args[idx + 1:]

    def _send_codex(self, message: str, system: str) -> tuple[str, Usage]:
        prefix, options = self._codex_parts()
        prompt = self._initial_prompt(message, system) if not self._provider_session_id else message
        if self._provider_session_id:
            argv = prefix + ["resume"] + options + ["--json", self._provider_session_id, prompt]
        else:
            argv = prefix + options + ["--json", prompt]
        result = self._run(argv, cwd=self._workspace_cwd or None)

        text = ""
        usage = Usage(estimated=True)
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                self._provider_session_id = event.get("thread_id", "")
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    text = item.get("text", text)
            if event.get("type") == "turn.completed":
                raw = event.get("usage", {})
                reported = Usage(
                    input_tokens=int(raw.get("input_tokens", 0) or 0),
                    cached_input_tokens=int(raw.get("cached_input_tokens", 0) or 0),
                    output_tokens=int(raw.get("output_tokens", 0) or 0),
                )
                mode = str(self.config.extra.get("cli_usage_mode", "per_turn")).lower()
                cumulative = mode == "cumulative" or bool(raw.get("cumulative")) or raw.get("usage_type") == "cumulative"
                usage = self._normalize_cli_usage(reported, cumulative)
        if not text:
            text = result.stdout.strip()
        if usage.total_tokens == 0:
            usage = self._estimated_usage(prompt, text)
        return text, usage

    def _normalize_cli_usage(self, reported: Usage, cumulative: bool) -> Usage:
        """Convert explicitly cumulative CLI counters into safe per-turn deltas."""
        if not cumulative:
            return reported
        previous = self._cli_usage_snapshot
        normalized = Usage(
            input_tokens=max(0, reported.input_tokens - previous.input_tokens),
            cached_input_tokens=max(0, reported.cached_input_tokens - previous.cached_input_tokens),
            output_tokens=max(0, reported.output_tokens - previous.output_tokens),
            estimated=reported.estimated,
        )
        self._cli_usage_snapshot = reported
        return normalized

    def _antigravity_base_args(self) -> list[str]:
        args = []
        skip_value = False
        for arg in self._argv:
            if skip_value:
                skip_value = False
                continue
            if arg in {"--conversation", "--log-file"}:
                skip_value = True
                continue
            if arg in {"--continue", "-c"}:
                continue
            args.append(arg)
        # DesignFlow supplies the prompt, so a trailing prompt flag belongs to
        # the adapter rather than the configured base command.
        if args and args[-1] in {"-p", "--prompt"}:
            args.pop()
        return args

    def _send_antigravity(self, message: str, system: str) -> tuple[str, Usage]:
        args = self._antigravity_base_args()
        configured_dirs = {
            args[index + 1] for index, value in enumerate(args[:-1]) if value == "--add-dir"
        }
        if self._workspace_cwd and self._workspace_cwd not in configured_dirs:
            # Antigravity keeps its own active project and can otherwise inspect
            # a global scratch workspace even when the subprocess cwd is correct.
            args += ["--add-dir", self._workspace_cwd]
        prompt = self._initial_prompt(message, system) if not self._provider_session_id else message
        log_path = Path(self._session_cwd) / "agy.log"
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass
        args += ["--log-file", str(log_path)]
        if self._provider_session_id:
            args += ["--conversation", self._provider_session_id]
        result = self._run(args + ["-p", prompt], cwd=self._workspace_cwd or self._session_cwd)
        try:
            log_text = log_path.read_text(errors="replace")
        except OSError:
            log_text = ""
        combined = f"{result.stdout}\n{result.stderr}\n{log_text}"
        match = re.search(
            r"(?:conversation|session)(?:\s*|_?)(?:id)?\s*(?:=|:|\s)\s*[\"']?"
            r"([0-9a-f]{8}-[0-9a-f-]{27,})",
            combined,
            re.IGNORECASE,
        )
        if match:
            self._provider_session_id = match.group(1)
        elif not self._provider_session_id:
            raise RuntimeError(
                "Antigravity completed the turn but did not expose its conversation ID; "
                "refusing unsafe global --continue"
            )
        text = result.stdout.strip()
        return text, self._estimated_usage(prompt, text)

    def state_dict(self) -> dict:
        state = super().state_dict()
        state.update({
            "session_mode": self._session_mode,
            "context_reused": bool(self._provider_session_id and len(self.history) > 2),
            "cache_reporting": "exact" if self._session_mode == "codex" else "unavailable",
        })
        return state

    def _send_stateless(self, messages: list[dict], system: str) -> tuple[str, Usage]:
        parts = [f"[SYSTEM]\n{system}\n"] if system else []
        for message in messages:
            label = "USER" if message["role"] == "user" else "ASSISTANT"
            parts.append(f"[{label}]\n{message['content']}")
        parts.append("[ASSISTANT]")
        prompt = "\n\n".join(parts)
        result = self._run(self._argv + [prompt], cwd=self._workspace_cwd or None)
        text = result.stdout.strip()
        return text, self._estimated_usage(prompt, text)

    @staticmethod
    def _estimated_usage(prompt: str, text: str) -> Usage:
        # Better than the old output-word count while remaining clearly marked
        # as an estimate when a CLI does not expose provider usage metadata.
        return Usage(
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(text) // 4),
            estimated=True,
        )

    def _reset_provider_session(self):
        self._provider_session_id = ""
        self._cli_usage_snapshot = Usage()

    def provider_session_id(self) -> str:
        return self._provider_session_id


class OllamaAgent(AgentBase):
    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._base_url = config.extra.get("base_url", "http://localhost:11434")

    def reconfigure(self, config: AgentConfig):
        super().reconfigure(config)
        self._base_url = config.extra.get("base_url", "http://localhost:11434")

    def _raw_send(self, messages: list[dict], system: str, *args, **kwargs) -> tuple[str, Usage]:
        import urllib.request

        payload = {
            "model": self.config.model or "llama3",
            "messages": ([{"role": "system", "content": system}] if system else []) + messages,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self._base_url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read())
        text = data["message"]["content"]
        usage = Usage(
            input_tokens=int(data.get("prompt_eval_count", 0) or 0),
            output_tokens=int(data.get("eval_count", 0) or 0),
        )
        return text, usage


AGENT_KINDS: dict[str, type[AgentBase]] = {
    "claude": ClaudeAgent,
    "openai": OpenAIAgent,
    "groq": GroqAgent,
    "gemini": GeminiAgent,
    "cli": CLIAgent,
    "ollama": OllamaAgent,
}


def create_agent(config: AgentConfig) -> AgentBase:
    cls = AGENT_KINDS.get(config.kind)
    if not cls:
        raise ValueError(f"Unknown agent kind '{config.kind}'. Options: {list(AGENT_KINDS)}")
    return cls(config)
