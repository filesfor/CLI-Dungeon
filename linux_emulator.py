#!/usr/bin/env python3
"""
linux_emulator.py
-----------------
A sandboxed POSIX-like shell emulator that runs entirely inside a virtual
filesystem stored in memory (and optionally persisted to a sandbox/ folder).

Supported built-in commands:
  ls, cd, pwd, cat, mv, cp, rm, mkdir, touch, echo, source, chmod,
  shuf, head, tail, grep, find, wc, hexdump, md5sum, awk, curl, exit

You can also:
  - Run .sh files:  ./script.sh  or  source script.sh
  - Pipe curl output straight into source to install a dungeon:
      source <(curl -s <url>)

The sandbox root lives at:  ./sandbox/
Everything written by scripts stays inside that folder.
"""

import os
import re
import sys
import shutil
import random
import hashlib
import subprocess
import urllib.request
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Sandbox root – all virtual FS operations happen inside here
# ---------------------------------------------------------------------------
SANDBOX_ROOT = Path(__file__).resolve().parent / "sandbox"
SANDBOX_ROOT.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Shell state
# ---------------------------------------------------------------------------
class Shell:
    def __init__(self):
        self.cwd = SANDBOX_ROOT          # real path, always inside SANDBOX_ROOT
        self.env = {
            "HOME": "/",
            "PATH": "/usr/bin:/bin",
            "PWD": "/",
        }
        self.last_exit = 0

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------
    def real(self, virtual_path: str) -> Path:\n        \"\"\"Convert a virtual path (relative or /absolute) to a real sandbox path.\"\"\"\n        if not virtual_path:\n            return self.cwd\n        # If the path is already a real absolute path inside the sandbox, use directly\n        rp = Path(virtual_path)\n        try:\n            rp.resolve().relative_to(SANDBOX_ROOT)\n            return rp.resolve()\n        except (ValueError, OSError):\n            pass\n        # Treat POSIX-style /path as virtual absolute (even on Windows)\n        if virtual_path.startswith("/"):\n            rel = virtual_path.lstrip("/")\n            return (SANDBOX_ROOT / rel).resolve() if rel else SANDBOX_ROOT\n        else:\n            return (self.cwd / rp).resolve()

    def virtual(self, real_path: Path) -> str:
        """Convert a real sandbox path back to a virtual /path string."""
        try:
            rel = real_path.relative_to(SANDBOX_ROOT)
            return "/" + str(rel).replace("\\", "/")
        except ValueError:
            return str(real_path)

    def _safe(self, real_path: Path) -> Path:
        """Raise if path escapes sandbox."""
        try:
            real_path.relative_to(SANDBOX_ROOT)
        except ValueError:
            raise PermissionError(f"Access outside sandbox denied: {real_path}")
        return real_path

    # ------------------------------------------------------------------
    # Variable expansion
    # ------------------------------------------------------------------
    def expand(self, text: str) -> str:
        """Expand $VAR and ${VAR} references."""
        def replace(m):
            name = m.group(1) or m.group(2)
            if name == "HOME":
                return "/"  # virtual root = sandbox root
            if name == "PWD":
                return self.virtual(self.cwd)
            return self.env.get(name, "")
        text = re.sub(r'\$\{(\w+)\}|\$(\w+)', replace, text)
        return text

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------
    def run_line(self, line: str) -> str:
        """Parse and execute a single shell line. Returns stdout string."""
        line = line.strip()
        if not line or line.startswith("#"):
            return ""
        # Handle:  source <(curl -s url)
        m = re.match(r'source\s+<\((.+)\)', line)
        if m:
            inner = m.group(1).strip()
            script_text = self._run_command_capture(inner)
            if script_text.startswith("#curl-error:"):
                return script_text.replace("#curl-error:", "curl error:") + "\n"
            return self.run_script_text(script_text)

        # Handle:  VAR=value
        if re.match(r'^[A-Za-z_]\w*=', line) and " " not in line.split("=")[0]:
            k, _, v = line.partition("=")
            v = self.expand(v.strip("'\""))
            self.env[k] = v
            return ""

        # Handle inline var assignments before command:  VAR=val cmd …
        # (simple version: just strip them)
        parts = self._split(line)
        if not parts:
            return ""

        # Expand variables in all parts
        parts = [self.expand(p) for p in parts]

        cmd = parts[0]
        args = parts[1:]

        builtins = {
            "ls": self._ls,
            "cd": self._cd,
            "pwd": self._pwd,
            "cat": self._cat,
            "mv": self._mv,
            "cp": self._cp,
            "rm": self._rm,
            "mkdir": self._mkdir,
            "touch": self._touch,
            "echo": self._echo,
            "source": self._source,
            "chmod": self._chmod,
            "shuf": self._shuf,
            "head": self._head,
            "tail": self._tail,
            "grep": self._grep,
            "find": self._find,
            "wc": self._wc,
            "hexdump": self._hexdump,
            "md5sum": self._md5sum,
            "awk": self._awk,
            "curl": self._curl,
            "exit": self._exit,
            "clear": lambda a: "",
        }

        if cmd in builtins:
            return builtins[cmd](args) or ""

        # ./script.sh or relative path scripts
        if cmd.startswith("./") or cmd.startswith("/"):
            return self._exec_script(cmd, args)

        # Unknown – try anyway as a script name in cwd
        script_path = self.cwd / cmd
        if script_path.exists():
            return self._exec_script("./" + cmd, args)

        return f"emulator: command not found: {cmd}\n"

    # ------------------------------------------------------------------
    # Script execution
    # ------------------------------------------------------------------
    def run_script_text(self, text: str, extra_env: dict = None) -> str:
        """Execute multi-line shell script text inside the emulator."""
        output_lines = []
        lines = text.splitlines()
        i = 0
        # Simple variable scope overlay
        saved_env = dict(self.env)
        if extra_env:
            self.env.update(extra_env)

        # We need to handle multi-line constructs: if/fi, for/do/done, while/do/done
        # Build a flat list of logical lines by joining continuation lines
        logical = self._preprocess_script(lines)

        for stmt in logical:
            stmt = stmt.strip()
            if not stmt or stmt.startswith("#"):
                continue
            # Handle VAR=`cmd` or VAR=$(cmd) assignments
            stmt, captured = self._resolve_command_substitutions(stmt)
            out = self.run_line(stmt)
            if out:
                output_lines.append(out)

        self.env = saved_env
        return "".join(output_lines)

    def _preprocess_script(self, lines):
        """Very simple script preprocessor that handles if/for/while blocks."""
        # Return lines as-is for now; complex control flow handled in run_script_text
        result = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            # if block
            if re.match(r'^if\b', line):
                block, i = self._collect_block(lines, i, 'if', 'fi')
                result.append(("IF_BLOCK", block))
                continue
            # for block
            if re.match(r'^for\b', line):
                block, i = self._collect_block(lines, i, 'for', 'done')
                result.append(("FOR_BLOCK", block))
                continue
            # while block
            if re.match(r'^while\b', line):
                block, i = self._collect_block(lines, i, 'while', 'done')
                result.append(("WHILE_BLOCK", block))
                continue
            result.append(line)
            i += 1
        return self._execute_preprocessed(result)

    def _collect_block(self, lines, start, open_kw, close_kw):
        """Collect lines of a block from start index until matching close keyword."""
        block = [lines[start]]
        depth = 1
        i = start + 1
        while i < len(lines) and depth > 0:
            l = lines[i].strip()
            if re.match(r'^' + re.escape(open_kw) + r'\b', l):
                depth += 1
            elif re.match(r'^' + re.escape(close_kw) + r'\b', l):
                depth -= 1
            block.append(lines[i])
            i += 1
        return block, i

    def _execute_preprocessed(self, items):
        """Flatten preprocessed items back to executable lines."""
        result = []
        for item in items:
            if isinstance(item, tuple):
                kind, block = item
                if kind == "IF_BLOCK":
                    result.extend(self._expand_if(block))
                elif kind == "FOR_BLOCK":
                    result.extend(self._expand_for(block))
                elif kind == "WHILE_BLOCK":
                    result.extend(self._expand_while(block))
            else:
                result.append(item)
        return result

    def _expand_if(self, block):
        """Evaluate if block and return lines of the taken branch."""
        # Parse: if [ condition ]; then ... [else ...] fi
        lines = [l.strip() for l in block]
        # Find condition
        header = lines[0]
        cond_match = re.search(r'\[\s*(.+?)\s*\]', header)
        condition = True
        if cond_match:
            condition = self._eval_condition(cond_match.group(1))
        else:
            # if cmd
            cmd_match = re.match(r'if\s+(.+)', header)
            if cmd_match:
                out = self._run_command_capture(cmd_match.group(1))
                condition = bool(out.strip())

        then_lines = []
        else_lines = []
        in_else = False
        for l in lines[1:]:
            if l in ('then', 'else'):
                if l == 'else':
                    in_else = True
                continue
            if l == 'fi':
                break
            if in_else:
                else_lines.append(l)
            else:
                then_lines.append(l)
        return then_lines if condition else else_lines

    def _eval_condition(self, expr: str) -> bool:
        expr = self.expand(expr).strip()
        # -d path
        m = re.match(r'-d\s+"?(.+?)"?$', expr)
        if m:
            p = self.real(m.group(1))
            return p.exists() and p.is_dir()
        # -f path
        m = re.match(r'-f\s+"?(.+?)"?$', expr)
        if m:
            p = self.real(m.group(1))
            return p.exists() and p.is_file()
        # -z string
        m = re.match(r'-z\s+"?(.*?)"?$', expr)
        if m:
            return len(m.group(1)) == 0
        # -eq / -ne / -lt / -gt
        m = re.match(r'(.+?)\s+(-eq|-ne|-lt|-gt|-le|-ge)\s+(.+)', expr)
        if m:
            try:
                a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
                return {'eq': a==b, 'ne': a!=b, 'lt': a<b, 'gt': a>b, 'le': a<=b, 'ge': a>=b}[op[1:]]
            except ValueError:
                return False
        # string = string
        m = re.match(r'"?(.+?)"?\s*=\s*"?(.+?)"?$', expr)
        if m:
            return m.group(1) == m.group(2)
        # string != string
        m = re.match(r'"?(.+?)"?\s*!=\s*"?(.+?)"?$', expr)
        if m:
            return m.group(1) != m.group(2)
        return False

    def _expand_for(self, block):
        """Expand a for loop into individual lines."""
        lines = [l.strip() for l in block]
        header = lines[0]
        # for VAR in LIST; do
        m = re.match(r'for\s+(\w+)\s+in\s+(.+?)(?:;|\s*$)', header)
        if not m:
            return []
        var = m.group(1)
        items_str = self.expand(m.group(2)).strip()
        # Handle {A..B} range
        range_m = re.match(r'\{(\d+)\.\.(\d+)\}', items_str)
        if range_m:
            items = list(range(int(range_m.group(1)), int(range_m.group(2)) + 1))
        else:
            items = items_str.split()
        body = []
        for l in lines[1:]:
            if l in ('do', 'done'):
                continue
            body.append(l)
        result = []
        for val in items:
            for l in body:
                result.append(re.sub(r'\$\{?' + var + r'\}?', str(val), l))
        return result

    def _expand_while(self, block):
        """Expand a while loop (runs up to 1000 iterations)."""
        # For simplicity, we don't support while loops in the dungeon scripts
        return []

    def _resolve_command_substitutions(self, line: str):
        """Replace `cmd` and $(cmd) with their output."""
        def replacer(m):
            inner = m.group(1) or m.group(2)
            return self._run_command_capture(inner).strip()
        line = re.sub(r'`([^`]+)`|\$\(([^)]+)\)', replacer, line)
        return line, {}

    def _run_command_capture(self, cmd_str: str) -> str:
        """Run a command string and return its stdout."""
        saved_cwd = self.cwd
        out = self.run_line(cmd_str)
        self.cwd = saved_cwd
        return out or ""

    def _exec_script(self, cmd: str, args: list) -> str:
        """Execute a .sh file inside the emulator."""
        if cmd.startswith("./"):
            real_path = self.cwd / cmd[2:]
        else:
            real_path = self.real(cmd)
        self._safe(real_path)
        if not real_path.exists():
            return f"emulator: {cmd}: No such file or directory\n"
        text = real_path.read_text(encoding="utf-8", errors="replace")
        return self.run_script_text(text, {"0": cmd, **{str(i+1): a for i, a in enumerate(args)}})

    # ------------------------------------------------------------------
    # Tokenizer
    # ------------------------------------------------------------------
    def _split(self, line: str) -> list:
        """Split a shell line into tokens, respecting quotes."""
        tokens = []
        current = []
        in_single = in_double = False
        i = 0
        while i < len(line):
            c = line[i]
            if c == "'" and not in_double:
                in_single = not in_single
            elif c == '"' and not in_single:
                in_double = not in_double
            elif c == ' ' and not in_single and not in_double:
                if current:
                    tokens.append("".join(current))
                    current = []
            else:
                current.append(c)
            i += 1
        if current:
            tokens.append("".join(current))
        return tokens

    # ------------------------------------------------------------------
    # Built-in commands
    # ------------------------------------------------------------------
    def _ls(self, args):
        target = self.cwd
        for a in args:
            if not a.startswith("-"):
                target = self._safe(self.real(a))
        if not target.exists():
            return f"ls: cannot access '{args[0] if args else ''}': No such file or directory\n"
        if target.is_file():
            return target.name + "\n"
        entries = sorted(target.iterdir(), key=lambda p: p.name)
        lines = []
        for e in entries:
            lines.append(e.name + ("/" if e.is_dir() else ""))
        return "  ".join(lines) + "\n" if lines else "\n"

    def _cd(self, args):
        if not args:
            self.cwd = SANDBOX_ROOT
            return ""
        target = self._safe(self.real(args[0]))
        if not target.exists() or not target.is_dir():
            return f"cd: {args[0]}: No such file or directory\n"
        self.cwd = target
        self.env["PWD"] = self.virtual(self.cwd)
        return ""

    def _pwd(self, args):
        return self.virtual(self.cwd) + "\n"

    def _cat(self, args):
        if not args:
            return ""
        out = []
        for a in args:
            if a.startswith("-"):
                continue
            p = self._safe(self.real(a))
            if not p.exists():
                out.append(f"cat: {a}: No such file or directory\n")
            elif p.is_dir():
                out.append(f"cat: {a}: Is a directory\n")
            else:
                out.append(p.read_text(encoding="utf-8", errors="replace"))
        return "".join(out)

    def _mv(self, args):
        if len(args) < 2:
            return "mv: missing operand\n"
        src = self._safe(self.real(args[0]))
        dst = self._safe(self.real(args[1]))
        if not src.exists():
            return f"mv: cannot stat '{args[0]}': No such file or directory\n"
        if dst.is_dir():
            dst = dst / src.name
        shutil.move(str(src), str(dst))
        return ""

    def _cp(self, args):
        flags = [a for a in args if a.startswith("-")]
        paths = [a for a in args if not a.startswith("-")]
        if len(paths) < 2:
            return "cp: missing operand\n"
        src = self._safe(self.real(paths[0]))
        dst = self._safe(self.real(paths[1]))
        if not src.exists():
            return f"cp: cannot stat '{paths[0]}': No such file or directory\n"
        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            if dst.is_dir():
                dst = dst / src.name
            shutil.copy2(str(src), str(dst))
        return ""

    def _rm(self, args):
        flags = [a for a in args if a.startswith("-")]
        paths = [a for a in args if not a.startswith("-")]
        recursive = "-r" in flags or "-rf" in flags or "-fr" in flags
        out = []
        for p_str in paths:
            p = self._safe(self.real(p_str))
            if not p.exists():
                out.append(f"rm: cannot remove '{p_str}': No such file or directory\n")
                continue
            if p.is_dir():
                if recursive:
                    shutil.rmtree(str(p))
                else:
                    out.append(f"rm: cannot remove '{p_str}': Is a directory (use -r)\n")
            else:
                p.unlink()
        return "".join(out)

    def _mkdir(self, args):
        flags = [a for a in args if a.startswith("-")]
        paths = [a for a in args if not a.startswith("-")]
        parents = "-p" in flags
        for p_str in paths:
            p = self._safe(self.real(p_str))
            p.mkdir(parents=parents or True, exist_ok=True)
        return ""

    def _touch(self, args):
        for a in args:
            if a.startswith("-"):
                continue
            p = self._safe(self.real(a))
            p.touch()
        return ""

    def _echo(self, args):
        no_newline = "-n" in args
        parts = [a for a in args if a != "-n"]
        text = " ".join(parts)
        return text + ("" if no_newline else "\n")

    def _source(self, args):
        if not args:
            return ""
        return self._exec_script(args[0], args[1:])

    def _chmod(self, args):
        # No-op in emulator (files don't have POSIX permissions on Windows)
        return ""

    def _shuf(self, args):
        """shuf -n N file  or  shuf -i LO-HI -n N"""
        n = None
        input_range = None
        files = []
        i = 0
        while i < len(args):
            if args[i] == "-n":
                n = int(args[i+1]); i += 2
            elif args[i] == "-i":
                parts = args[i+1].split("-")
                input_range = (int(parts[0]), int(parts[1])); i += 2
            else:
                files.append(args[i]); i += 1
        if input_range:
            population = list(range(input_range[0], input_range[1] + 1))
            k = n if n is not None else len(population)
            selected = random.sample(population, min(k, len(population)))
            return "\n".join(str(x) for x in selected) + "\n"
        else:
            lines = []
            for f in files:
                p = self._safe(self.real(f))
                if p.exists() and p.is_file():
                    lines.extend(l for l in p.read_text(encoding="utf-8").splitlines() if l.strip())
            random.shuffle(lines)
            if n is not None:
                lines = lines[:n]
            return "\n".join(lines) + "\n"

    def _head(self, args):
        n = 10
        files = []
        i = 0
        while i < len(args):
            if args[i] in ("-n", "-1", "-2", "-3", "-4", "-5"):
                if args[i] == "-n":
                    n = int(args[i+1]); i += 2
                else:
                    n = int(args[i][1:]); i += 1
            else:
                files.append(args[i]); i += 1
        out = []
        for f in files:
            p = self._safe(self.real(f))
            if p.exists():
                lines = p.read_text(encoding="utf-8").splitlines()
                out.extend(lines[:n])
        return "\n".join(out) + "\n" if out else ""

    def _tail(self, args):
        n = 10
        files = []
        i = 0
        while i < len(args):
            if args[i] == "-n":
                n = int(args[i+1]); i += 2
            elif args[i] == "-1":
                n = 1; i += 1
            else:
                files.append(args[i]); i += 1
        out = []
        for f in files:
            p = self._safe(self.real(f))
            if p.exists():
                lines = p.read_text(encoding="utf-8").splitlines()
                out.extend(lines[-n:])
        return "\n".join(out) + "\n" if out else ""

    def _grep(self, args):
        flags = [a for a in args if a.startswith("-")]
        non_flags = [a for a in args if not a.startswith("-")]
        if not non_flags:
            return ""
        pattern = non_flags[0]
        targets = non_flags[1:]
        recursive = "-R" in flags or "-r" in flags
        exclude_dirs = []
        for f in flags:
            m = re.match(r'--exclude-dir=(.*)', f)
            if m:
                exclude_dirs.append(m.group(1))
        results = []
        def search_file(p: Path, label: str):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    if re.search(pattern, line):
                        results.append(f"{label}:{line}")
            except Exception:
                pass
        for t in targets:
            tp = self._safe(self.real(t))
            if tp.is_file():
                search_file(tp, t)
            elif tp.is_dir() and recursive:
                for fp in tp.rglob("*"):
                    if fp.is_file():
                        skip = any(excl in fp.parts for excl in exclude_dirs)
                        if not skip:
                            label = self.virtual(fp)
                            search_file(fp, label)
        return "\n".join(results) + "\n" if results else ""

    def _find(self, args):
        start = "."
        name_pat = None
        i = 0
        while i < len(args):
            if args[i] == "-name":
                name_pat = args[i+1]; i += 2
            else:
                start = args[i]; i += 1
        base = self._safe(self.real(start))
        results = []
        for p in base.rglob("*"):
            try:
                self._safe(p)
            except PermissionError:
                continue
            if name_pat:
                import fnmatch
                if fnmatch.fnmatch(p.name, name_pat):
                    results.append(self.virtual(p))
            else:
                results.append(self.virtual(p))
        return "\n".join(results) + "\n" if results else ""

    def _wc(self, args):
        flags = [a for a in args if a.startswith("-")]
        files = [a for a in args if not a.startswith("-")]
        out = []
        for f in files:
            p = self._safe(self.real(f))
            if p.exists():
                text = p.read_text(encoding="utf-8")
                lines = text.count("\n")
                words = len(text.split())
                chars = len(text)
                if "-l" in flags:
                    out.append(f"{lines}")
                elif "-w" in flags:
                    out.append(f"{words}")
                elif "-c" in flags:
                    out.append(f"{chars}")
                else:
                    out.append(f"{lines} {words} {chars} {f}")
        return "\n".join(out) + "\n" if out else ""

    def _hexdump(self, args):
        """hexdump -vn16 -e'4/4 "%08X" 1 "\n"' /dev/urandom  -> random hex string"""
        # We just generate a random 128-bit hex string regardless of args
        import secrets
        data = secrets.token_bytes(16)
        groups = [data[i:i+4] for i in range(0, 16, 4)]
        return "".join(g.hex().upper() for g in groups) + "\n"

    def _md5sum(self, args):
        # Can receive piped input via args as raw text (we pass it as a special arg)
        # We treat the last non-flag arg as a file, or if empty compute on nothing
        if not args:
            return ""
        # Check if it looks like a file path
        p = self._safe(self.real(args[0]))
        if p.exists() and p.is_file():
            data = p.read_bytes()
        else:
            # treat arg as literal string (piped data)
            data = " ".join(args).encode("utf-8")
        h = hashlib.md5(data).hexdigest()
        return f"{h}  -\n"

    def _awk(self, args):
        """Very minimal awk: supports {print $N} patterns."""
        prog = ""
        files = []
        i = 0
        while i < len(args):
            if args[i] == "-F":
                i += 2  # skip field separator (not implemented)
            elif prog == "":
                prog = args[i]; i += 1
            else:
                files.append(args[i]); i += 1
        m = re.search(r'\{print \$(\d+)\}', prog)
        field = int(m.group(1)) - 1 if m else 0
        out = []
        for f in files:
            p = self._safe(self.real(f))
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if field < len(parts):
                        out.append(parts[field])
        return "\n".join(out) + "\n" if out else ""

    def _curl(self, args):
        """Download a URL and save to sandbox or return content."""
        flags = set()
        url = None
        output_file = None
        i = 0
        silent = False
        while i < len(args):
            a = args[i]
            if a in ("-s", "--silent"):
                silent = True; i += 1
            elif a in ("-L", "--location"):
                i += 1
            elif a in ("-J", "-O", "-Os", "-LJOs", "-LJO"):
                flags.add("save"); i += 1
            elif a == "-o":
                output_file = args[i+1]; i += 2
            elif not a.startswith("-"):
                url = a; i += 1
            else:
                i += 1
        if not url:
            return "curl: no URL specified\n"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "linux-emulator/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            text = data.decode("utf-8", errors="replace")
        except Exception as e:
            return f"#curl-error: ({url}) {e}\n"

        if "save" in flags or output_file:
            if output_file:
                fname = output_file
            else:
                fname = url.split("/")[-1].split("?")[0]
            dest = self._safe(self.cwd / fname)
            dest.write_text(text, encoding="utf-8")
            return ""
        else:
            return text

    def _exit(self, args):
        print("\nGoodbye!")
        sys.exit(0)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------
def main():
    shell = Shell()
    print("=" * 60)
    print("  Linux Emulator  (sandboxed — all writes go to ./sandbox/)")
    print("  Type 'exit' to quit.")
    print()
    print("  To install & play CLI Dungeon:")
    print('  source <(curl -s https://raw.githubusercontent.com/paralinguist/CLI-Dungeon/main/generate_dungeon.sh)')
    print()
    print("  To run any other script from a URL:")
    print('  curl -s <url> -o myscript.sh')
    print('  source myscript.sh')
    print("=" * 60)

    while True:
        try:
            vpath = shell.virtual(shell.cwd)
            line = input(f"\033[1;32memulator\033[0m:\033[1;34m{vpath}\033[0m$ ")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        # Handle pipe: cmd1 | cmd2
        if "|" in line and not any(q in line for q in ["'|'", '">"']):
            parts = [p.strip() for p in line.split("|")]
            data = ""
            for part in parts:
                # inject piped data as trailing argument (hacky but works for grep/md5sum/awk/wc)
                if data.strip():
                    part = part + " " + repr(data.strip())
                data = shell.run_line(part)
            if data:
                print(data, end="" if data.endswith("\n") else "\n")
        else:
            out = shell.run_line(line)
            if out:
                print(out, end="" if out.endswith("\n") else "\n")


if __name__ == "__main__":
    main()
