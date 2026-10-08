"""Tiny YAML-frontmatter reader/writer for the subset Obsidian notes use
(scalars, quoted strings, flow lists, block lists, one level of nested maps)."""
import json
import re

FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)


def split(text):
    """-> (frontmatter dict, body str)"""
    text = text.lstrip("﻿")
    m = FM_RE.match(text)
    if not m:
        return {}, text
    return parse(m.group(1)), text[m.end():]


def _scalar(v):
    v = v.strip()
    if v == "":
        return None
    if v[0] == '"' and v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    if v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if v[0] == "[" and v[-1] == "]":
        inner = v[1:-1].strip()
        if not inner:
            return []
        try:
            return json.loads(v)
        except ValueError:
            return [_scalar(x) for x in _split_flow(inner)]
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", v) and not (len(v) > 1 and v.lstrip("-").startswith("0")):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def _split_flow(s):
    out, cur, q, depth = [], "", None, 0
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur += ch
        elif ch == "[":
            depth += 1
            cur += ch
        elif ch == "]":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def parse(src):
    data, lines, i = {}, src.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#") or line.startswith((" ", "\t")):
            i += 1
            continue
        m = re.match(r"([^:]+):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, val = m.group(1).strip(), m.group(2)
        i += 1
        if val.strip() not in ("", "|", ">"):
            data[key] = _scalar(val)
            continue
        # block content
        block = []
        while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
            if lines[i].strip():
                block.append(lines[i])
            i += 1
        if not block:
            data[key] = None
        elif block[0].strip().startswith("- ") or block[0].strip() == "-":
            data[key] = [_scalar(b.strip()[1:]) for b in block if b.strip().startswith("-")]
        elif val.strip() in ("|", ">"):
            data[key] = "\n".join(b.strip() for b in block)
        else:
            sub = {}
            for b in block:
                mm = re.match(r"\s+([^:]+):\s*(.*)$", b)
                if mm:
                    sub[mm.group(1).strip()] = _scalar(mm.group(2))
            data[key] = sub
    return data


def _emit_scalar(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v) if isinstance(v, float) else str(v)
    return json.dumps(str(v), ensure_ascii=False)


def dump(d):
    out = []
    for k, v in d.items():
        if isinstance(v, (list, tuple)):
            if not v:
                out.append(f"{k}: []")
            else:
                out.append(f"{k}:")
                out.extend(f"  - {_emit_scalar(x)}" for x in v)
        elif isinstance(v, dict):
            if not v:
                out.append(f"{k}: {{}}")
            else:
                out.append(f"{k}:")
                out.extend(f"  {kk}: {_emit_scalar(vv)}" for kk, vv in v.items())
        else:
            out.append(f"{k}: {_emit_scalar(v)}".rstrip())
    return "---\n" + "\n".join(out) + "\n---\n"
