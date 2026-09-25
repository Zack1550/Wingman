"""Argument checking, before anything is dispatched.

A model's tool call is untrusted input that happens to be well formatted. It can
name a tool that does not exist, omit a required field, pass a string where a
number belongs, or invent a field the host is supposed to own. None of that
should reach a vehicle, and none of it should raise: each one becomes a message
the model can read and correct.

Deliberately a small hand-rolled subset of JSON Schema rather than a dependency.
Tool schemas here are flat objects of scalars; anything richer would be a sign
the tool wants splitting.
"""

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def validate_arguments(arguments, schema):
    """Return None if the arguments fit the schema, else a sentence saying why.

    The sentence goes back to the model verbatim, so it names the field and what
    was expected rather than merely reporting that something was wrong.
    """
    if not isinstance(arguments, dict):
        return f"arguments must be an object, got {type(arguments).__name__}"

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    missing = [name for name in required if name not in arguments]
    if missing:
        return (f"missing required argument(s): {', '.join(sorted(missing))}. "
                f"This tool takes: {', '.join(sorted(properties)) or 'nothing'}")

    # Extra fields are refused rather than ignored. A tool schema that omits
    # op_id omits it on purpose, and silently dropping an invented one would
    # hide the fact that the model tried to set it.
    unexpected = [name for name in arguments if name not in properties]
    if unexpected:
        return (f"unexpected argument(s): {', '.join(sorted(unexpected))}. "
                f"This tool takes only: "
                f"{', '.join(sorted(properties)) or 'nothing'}")

    for name, value in arguments.items():
        expected = properties[name].get("type")
        if expected is None:
            continue
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS.get(one, lambda _: True)(value) for one in allowed):
            return (f"argument '{name}' must be {' or '.join(allowed)}, "
                    f"got {type(value).__name__} ({value!r})")

    return None


def coerce_arguments(arguments, schema):
    """Repair the one mistake worth repairing: a number sent as a string.

    Small models routinely emit {"target_altitude_m": "15"}. Rejecting that
    teaches the model nothing useful and costs a round trip, while accepting
    anything looser would defeat the point of validating at all. Numbers only,
    and only when the string is unambiguously a number.
    """
    if not isinstance(arguments, dict):
        return arguments
    properties = schema.get("properties", {})
    repaired = dict(arguments)
    for name, value in arguments.items():
        expected = properties.get(name, {}).get("type")
        if not isinstance(value, str) or expected not in ("number", "integer"):
            continue
        try:
            repaired[name] = float(value) if expected == "number" else int(value)
        except ValueError:
            pass          # leave it; validation will reject it with a reason
    return repaired
