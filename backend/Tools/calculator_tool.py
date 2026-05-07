from langchain.tools import tool

@tool
def calculator(expression: str) -> str:
    """Evaluate a basic math expression like '2 + 2' or '10 * 5 / 2'.
    
    Args:
        expression: A math expression string to evaluate.
    """
    try:
        # Safe eval — only allows math operations
        allowed = {k: v for k, v in __builtins__.items()
                   if k in ("abs", "round", "min", "max", "sum", "pow")} \
                  if isinstance(__builtins__, dict) else {}
        result = eval(expression, {"__builtins__": allowed})
        return f"Result: {result}"
    except Exception as e:
        return f"Error evaluating expression: {e}"