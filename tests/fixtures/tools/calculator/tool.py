import ast
import math
import operator

OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}
FUNCS = {"sqrt": math.sqrt, "log": math.log, "exp": math.exp, "sin": math.sin, "cos": math.cos,
         "abs": abs, "round": round, "min": min, "max": max}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        exponent = _eval(node.right)
        if abs(exponent) > 1000:
            raise ValueError("exponent too large")
        return _eval(node.left) ** exponent
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        return OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS:
        return FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError("unsupported expression")


def run(expression: str) -> str:
    """Evaluate arithmetic, including sqrt, log, exp, sin, cos, abs, round, min and max.

    Args:
        expression: For example (2 + 3) * sqrt(16).
    """
    return str(_eval(ast.parse(expression, mode="eval").body))
