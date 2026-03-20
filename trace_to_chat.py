#!/usr/bin/env python3
"""
Convert orchestrator trace output to OpenAI chat messages format.

Usage:
    python trace_to_chat.py response-1.json                    # Print to stdout
    python trace_to_chat.py response-1.json -o chat.json       # Write to file
    python trace_to_chat.py response-1.json --pretty           # Pretty-print output

Includes: system prompts, tool definitions, messages, tool calls, tool results, thinking.
"""
import argparse
import json
import sys
from typing import Any


def extract_chat_history(trace: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Extract complete chat history from trace entries.

    Each trace entry contains the full conversation up to that point plus
    the new response. We take the final trace entry's messages and append
    its response to get the complete history.

    Returns a dict with: system, tools, messages
    """
    if not trace:
        return {"system": None, "tools": [], "messages": []}

    # Use the last trace entry - it has the most complete conversation
    final_trace = trace[-1]
    request_body = final_trace.get("request", {}).get("body", {})
    response_body = final_trace.get("response", {}).get("body", {})

    # Extract system prompt
    system_blocks = request_body.get("system", [])
    system_text = "\n\n".join(
        block.get("text", "") for block in system_blocks if block.get("type") == "text"
    )

    # Extract tool definitions (convert to OpenAI function format)
    anthropic_tools = request_body.get("tools", [])
    tools = [convert_tool_definition(t) for t in anthropic_tools]

    # Extract messages
    messages: list[dict[str, Any]] = []

    for msg in request_body.get("messages", []):
        converted = convert_message(msg)
        if converted:
            messages.extend(converted) if isinstance(converted, list) else messages.append(converted)

    # Append the final response
    response_content = response_body.get("content", [])
    if response_content:
        assistant_msgs = convert_assistant_content(response_content)
        messages.extend(assistant_msgs)

    return {
        "system": system_text if system_text else None,
        "tools": tools,
        "messages": messages,
    }


def convert_tool_definition(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert Anthropic tool definition to OpenAI function format."""
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {}),
        }
    }


def convert_message(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert an Anthropic message to OpenAI format. May return multiple messages."""
    role = msg.get("role")
    content = msg.get("content", [])

    if role == "user":
        return convert_user_message(content)
    elif role == "assistant":
        return convert_assistant_content(content)
    return []


def convert_user_message(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert user message content blocks to OpenAI format."""
    messages = []
    text_parts = []
    tool_results = []

    for block in content:
        block_type = block.get("type")

        if block_type == "text":
            text = block.get("text", "")
            if text:
                text_parts.append(text)

        elif block_type == "tool_result":
            tool_results.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": format_tool_result_content(block.get("content", "")),
            })

        elif block_type == "image":
            # Include image as separate content block
            text_parts.append(f"[Image: {block.get('source', {}).get('type', 'unknown')}]")

    # Add text message if any
    if text_parts:
        messages.append({"role": "user", "content": "\n\n".join(text_parts)})

    # Add tool results
    messages.extend(tool_results)

    return messages


def format_tool_result_content(content: Any) -> str:
    """Format tool result content as string."""
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    parts.append("[Image]")
                else:
                    parts.append(json.dumps(block))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    else:
        return json.dumps(content)


def convert_assistant_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert assistant content blocks to OpenAI format with tool_calls."""
    messages = []
    text_parts = []
    thinking_parts = []
    tool_calls = []

    for block in content:
        block_type = block.get("type")

        if block_type == "text":
            text = block.get("text", "")
            if text:
                text_parts.append(text)

        elif block_type == "thinking":
            thinking = block.get("thinking", "")
            if thinking:
                thinking_parts.append(thinking)

        elif block_type == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                }
            })

    # Build assistant message
    assistant_msg: dict[str, Any] = {"role": "assistant"}

    if text_parts:
        assistant_msg["content"] = "\n\n".join(text_parts)
    else:
        assistant_msg["content"] = None

    if thinking_parts:
        assistant_msg["reasoning_content"] = "\n\n".join(thinking_parts)

    if tool_calls:
        assistant_msg["tool_calls"] = tool_calls

    if text_parts or thinking_parts or tool_calls:
        messages.append(assistant_msg)

    return messages


def main():
    parser = argparse.ArgumentParser(
        description="Convert orchestrator trace to OpenAI chat format (full fidelity)"
    )
    parser.add_argument("input", help="Input JSON file (orchestrator response)")
    parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--no-tools", action="store_true", help="Exclude tool definitions")
    parser.add_argument("--no-system", action="store_true", help="Exclude system prompt")

    args = parser.parse_args()

    # Load input
    with open(args.input) as f:
        data = json.load(f)

    trace = data.get("trace", [])
    if not trace:
        print("Error: No trace data found", file=sys.stderr)
        sys.exit(1)

    # Convert
    result = extract_chat_history(trace)

    # Build output
    output: dict[str, Any] = {}

    if not args.no_system and result["system"]:
        output["system"] = result["system"]

    if not args.no_tools and result["tools"]:
        output["tools"] = result["tools"]

    output["messages"] = result["messages"]

    # Output
    indent = 2 if args.pretty else None

    if args.output:
        with open(args.output, "w") as f:
            json.dump(output, f, indent=indent, ensure_ascii=False)
        print(f"Wrote {len(result['messages'])} messages to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(output, indent=indent, ensure_ascii=False))


if __name__ == "__main__":
    main()
