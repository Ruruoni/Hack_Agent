"""Executed in a CHILD interpreter (`python -I -S`) by the python_sandbox tool. Reads {"code","data","cpu_s","mem_mb"} as
JSON on stdin, applies OS limits where available, runs the code with a restricted builtins table and prints a JSON
result on the real stdout. The parent additionally enforces wall-clock, memory and output limits.

This is defence in depth, NOT a hardened security boundary: use a VM/container for truly hostile code."""
import ast
import builtins
import io
import json
import sys

SAFE_IMPORTS = {"math", "statistics", "json", "re", "csv", "itertools", "collections", "datetime", "decimal", "fractions",
                "random", "string", "textwrap", "functools", "operator", "heapq", "bisect", "hashlib", "base64", "io",
                "time", "typing", "dataclasses", "enum", "uuid", "ipaddress", "difflib", "unicodedata", "copy", "array"}
BANNED_CALLS = {"eval", "exec", "compile", "open", "__import__", "input", "getattr", "setattr", "delattr", "globals",
                "locals", "vars", "breakpoint", "memoryview", "exit", "quit", "help"}
BANNED_ATTRS = {"__class__", "__bases__", "__subclasses__", "__globals__", "__builtins__", "__code__", "__closure__",
                "__mro__", "__dict__", "__import__", "__loader__", "__spec__", "gi_frame", "f_globals", "f_back", "tb_frame",
                "func_globals", "__getattribute__", "__reduce__", "__reduce_ex__"}


def validate(code):
    tree = ast.parse(code, "<sandbox>")
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] not in SAFE_IMPORTS:
                    raise ValueError("import of '%s' is not allowed" % a.name)
        elif isinstance(n, ast.ImportFrom):
            if (n.module or "").split(".")[0] not in SAFE_IMPORTS or n.level:
                raise ValueError("import from '%s' is not allowed" % n.module)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in BANNED_CALLS:
            raise ValueError("call to '%s' is not allowed" % n.func.id)
        elif isinstance(n, ast.Attribute) and (n.attr in BANNED_ATTRS or (n.attr.startswith("__") and n.attr.endswith("__"))):
            raise ValueError("attribute '%s' is not allowed" % n.attr)
        elif isinstance(n, ast.Name) and n.id in BANNED_ATTRS | {"__builtins__"}:
            raise ValueError("name '%s' is not allowed" % n.id)
    return tree


def limits(cpu_s, mem_mb):
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
        resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 2**20, mem_mb * 2**20))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1 * 2**20, 1 * 2**20))
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0)) if hasattr(resource, "RLIMIT_NPROC") else None
    except Exception:  # Windows or restricted platform: the parent enforces time/memory instead
        pass


def main():
    req = json.loads(sys.stdin.read())
    real_out = sys.stdout
    try:
        tree = validate(req["code"])
    except Exception as e:
        real_out.write(json.dumps({"ok": False, "error": "rejected by static check: %s" % e}))
        return
    limits(int(req.get("cpu_s", 10)), int(req.get("mem_mb", 512)))
    allowed = {k: getattr(builtins, k) for k in dir(builtins) if k not in BANNED_CALLS and not k.startswith("_")}
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.split(".")[0] not in SAFE_IMPORTS:
            raise ImportError("import of '%s' is not allowed" % name)
        return real_import(name, globals, locals, fromlist, level)
    allowed["__import__"] = guarded_import
    allowed["__build_class__"] = builtins.__build_class__
    allowed["__name__"] = "__sandbox__"
    buf = io.StringIO()
    sys.stdout = buf
    env = {"__builtins__": allowed, "DATA": req.get("data")}
    try:
        exec(compile(tree, "<sandbox>", "exec"), env)
        result = env.get("RESULT")
        sys.stdout = real_out
        real_out.write(json.dumps({"ok": True, "stdout": buf.getvalue()[:20000], "result": json.loads(json.dumps(result, default=str))}))
    except BaseException as e:
        sys.stdout = real_out
        real_out.write(json.dumps({"ok": False, "error": "%s: %s" % (e.__class__.__name__, e), "stdout": buf.getvalue()[:20000]}))


main()
