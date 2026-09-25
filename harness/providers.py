"""One interface, several models behind it.

Anthropic and Ollama disagree about nearly everything that matters here: what a
tool definition looks like, how the model says it wants to call one, and how you
hand the answer back. The loop should not know any of that. So the loop speaks
one neutral vocabulary and each provider translates at its own edge — the same
shape of work the vehicle gateway does between protobuf and MAVLink, and worth
recognising as a connector rather than as glue.

The neutral vocabulary:

    tool definition   {"name", "description", "input_schema"}   (straight from MCP)
    message           {"role": "user",      "content": str}
                      {"role": "assistant", "content": str|None, "tool_calls": [ToolCall]}
                      {"role": "tool",      "tool_call_id": str, "name": str, "content": str}
    reply             ModelReply(text, tool_calls, usage)
"""
import json
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_OLLAMA_MODEL = "granite4.1:3b"
# Not a cost knob: max_tokens is a ceiling, and only tokens actually produced
# are billed. Too low truncates a turn mid-thought, and on a model that thinks
# adaptively the reasoning counts against it too.
DEFAULT_MAX_TOKENS = 16000


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ModelReply:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    # The provider's own content blocks, kept verbatim. Current Anthropic models
    # think adaptively, and a thinking block must be replayed unchanged (its
    # signature included) on the next turn of the same conversation. Rebuilding
    # the assistant turn from text plus tool calls silently drops them.
    provider_blocks: list = field(default_factory=list)
    thinking: str = ""           # a summary, when the provider offers one

    @property
    def wants_tools(self):
        return bool(self.tool_calls)


class Provider(ABC):
    name = "provider"
    model = ""

    @abstractmethod
    def complete(self, system, messages, tools):
        """One turn: conversation plus tool definitions in, ModelReply out."""

    def describe(self):
        return f"{self.name}:{self.model}"


# --- Anthropic -------------------------------------------------------------

class AnthropicProvider(Provider):
    """Tools are top-level objects; tool results are content blocks in a user turn."""

    name = "anthropic"

    def __init__(self, model=DEFAULT_ANTHROPIC_MODEL, max_tokens=DEFAULT_MAX_TOKENS,
                 api_key=None, show_thinking=True):
        import anthropic
        self.model = model
        self.max_tokens = max_tokens
        # Thinking happens either way and is billed either way; display only
        # controls whether we get to read a summary of it. For a harness whose
        # whole job is explaining what an agent did, that summary belongs in
        # the trace.
        self.show_thinking = show_thinking
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def complete(self, system, messages, tools):
        request = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=self._to_anthropic(messages),
            tools=[{"name": tool["name"],
                    "description": tool["description"],
                    "input_schema": tool["input_schema"]} for tool in tools],
        )
        if self.show_thinking:
            request["thinking"] = {"type": "adaptive", "display": "summarized"}
        response = self._client.messages.create(**request)

        text_parts, tool_calls, thinking_parts = [], [], []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                if getattr(block, "thinking", ""):
                    thinking_parts.append(block.thinking)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name,
                                           arguments=dict(block.input)))
        usage = {"input_tokens": getattr(response.usage, "input_tokens", None),
                 "output_tokens": getattr(response.usage, "output_tokens", None)}
        blocks = [block.model_dump(exclude_none=True) for block in response.content]
        return ModelReply("\n".join(text_parts).strip(), tool_calls, usage,
                          provider_blocks=blocks,
                          thinking="\n".join(thinking_parts).strip())

    @staticmethod
    def _to_anthropic(messages):
        """Neutral to Anthropic, merging consecutive tool results into one turn.

        Anthropic expects every tool_result for a given assistant turn to arrive
        together in the next user message; sending them one per message is
        rejected.
        """
        converted, pending_results = [], []

        def flush():
            if pending_results:
                converted.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for message in messages:
            role = message["role"]
            if role == "tool":
                pending_results.append({
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": message["content"],
                })
                continue

            flush()
            if role == "user":
                converted.append({"role": "user", "content": message["content"]})
            elif role == "assistant":
                # Replay exactly what the model produced when we have it, so
                # thinking blocks and their signatures survive the round trip.
                blocks = list(message.get("provider_blocks") or [])
                if not blocks:
                    if message.get("content"):
                        blocks.append({"type": "text", "text": message["content"]})
                    for call in message.get("tool_calls", []):
                        blocks.append({"type": "tool_use", "id": call.id,
                                       "name": call.name, "input": call.arguments})
                # An assistant turn must not be empty, even if the model said
                # nothing but a tool call with no text alongside it.
                converted.append({"role": "assistant",
                                  "content": blocks or [{"type": "text", "text": "."}]})
        flush()
        return converted


# --- Ollama ----------------------------------------------------------------

class OllamaProvider(Provider):
    """OpenAI-shaped tools; tool results come back as their own role."""

    name = "ollama"

    def __init__(self, model=DEFAULT_OLLAMA_MODEL, host=None, options=None):
        import ollama
        self.model = model
        self.options = options or {"temperature": 0.0}
        self._client = ollama.Client(host=host) if host else ollama

    def complete(self, system, messages, tools):
        response = self._client.chat(
            model=self.model,
            messages=self._to_ollama(system, messages),
            tools=[{"type": "function",
                    "function": {"name": tool["name"],
                                 "description": tool["description"],
                                 "parameters": tool["input_schema"]}}
                   for tool in tools],
            options=self.options,
        )

        message = response.message
        tool_calls = []
        for index, raw in enumerate(message.tool_calls or []):
            arguments = raw.function.arguments
            if isinstance(arguments, str):        # some models emit a JSON string
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"_unparsed": arguments}
            # Ollama does not issue call ids, but the loop needs to correlate a
            # result with its request, so mint one.
            tool_calls.append(ToolCall(id=f"call-{uuid.uuid4().hex[:8]}-{index}",
                                       name=raw.function.name,
                                       arguments=dict(arguments)))
        usage = {"input_tokens": getattr(response, "prompt_eval_count", None),
                 "output_tokens": getattr(response, "eval_count", None)}
        return ModelReply((message.content or "").strip(), tool_calls, usage)

    @staticmethod
    def _to_ollama(system, messages):
        converted = [{"role": "system", "content": system}]
        for message in messages:
            role = message["role"]
            if role == "user":
                converted.append({"role": "user", "content": message["content"]})
            elif role == "assistant":
                turn = {"role": "assistant", "content": message.get("content") or ""}
                if message.get("tool_calls"):
                    turn["tool_calls"] = [
                        {"function": {"name": call.name, "arguments": call.arguments}}
                        for call in message["tool_calls"]]
                converted.append(turn)
            elif role == "tool":
                converted.append({"role": "tool",
                                  "tool_name": message["name"],
                                  "content": message["content"]})
        return converted


# --- a model that does exactly what the test says --------------------------

class ScriptedProvider(Provider):
    """A fake model, for proving the harness rather than measuring a model.

    Each script entry is either a string (a final answer) or a list of
    (tool_name, arguments) pairs. With repeat_last=True the final entry is
    returned forever, which is how a test proves the step budget actually ends
    a runaway loop.
    """

    name = "scripted"

    def __init__(self, script, repeat_last=False, model="scripted"):
        self.script = list(script)
        self.repeat_last = repeat_last
        self.model = model
        self.calls_seen = []          # what the loop asked of us, for assertions
        self._index = 0

    def complete(self, system, messages, tools):
        self.calls_seen.append({"messages": len(messages),
                                "tools": [tool["name"] for tool in tools]})
        if self._index < len(self.script):
            entry = self.script[self._index]
            self._index += 1
        elif self.repeat_last and self.script:
            entry = self.script[-1]
        else:
            entry = "the script ran out"

        if isinstance(entry, str):
            return ModelReply(text=entry)
        return ModelReply(tool_calls=[
            ToolCall(id=f"call-{uuid.uuid4().hex[:8]}", name=name,
                     arguments=dict(arguments)) for name, arguments in entry])


def build_provider(name, model=None, **kwargs):
    if name == "anthropic":
        return AnthropicProvider(model=model or DEFAULT_ANTHROPIC_MODEL, **kwargs)
    if name == "ollama":
        return OllamaProvider(model=model or DEFAULT_OLLAMA_MODEL, **kwargs)
    raise ValueError(f"unknown provider '{name}'; try anthropic or ollama")
