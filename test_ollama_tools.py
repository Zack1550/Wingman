import ollama

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_telemetry",
        "description": "Read current telemetry for one vehicle.",
        "parameters": {
            "type": "object",
            "properties": {
                "vehicle_id": {"type": "string", "description": 'Vehicle name, e.g. "alpha"'}
            },
            "required": ["vehicle_id"],
        },
    },
}]

r = ollama.chat(
    model="granite4.1:3b",
    messages=[{"role": "user", "content": "What is the battery level of vehicle alpha?"}],
    tools=TOOLS,
)

msg = r["message"] if isinstance(r, dict) else r.message
print("content:  ", repr(getattr(msg, "content", None) or msg.get("content")))
print("tool_calls:", getattr(msg, "tool_calls", None) or msg.get("tool_calls"))
