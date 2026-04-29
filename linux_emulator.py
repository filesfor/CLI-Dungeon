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
  - Shell functions, output redirection (> >>), pipes, arithmetic $((...))

The sandbox root lives at:  ./sandbox/
Everything written by scripts stays inside that folder.

Changes from original:
  - Shell function definitions (funcname() { ... }) and calls
  - Output redirection: cmd > file  and  cmd >> file
  - Arithmetic expansion: $(( expr ))
  - Pipe handling inside command substitution and run_line
  - Fixed cd $HOME to use $HOME env var
  - Fixed virtual() to display / instead of /.
  - read built-in no-op (avoids crash on interactive prompts in scripts)
"""

import os
import re
import sys
import shutil
import random
import hashlib
import urllib.request
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Sandbox root – all virtual FS operations happen inside here
# ---------------------------------------------------------------------------
SANDBOX_ROOT = Path(__file__).resolve().parent / "sandbox"
SANDBOX_ROOT.mkdir(exist_ok=True)
if sys.platform == "win32":
    import ctypes
    ctypes.windll.kernel32.SetFileAttributesW(str(SANDBOX_ROOT), 0x02)

# ---------------------------------------------------------------------------
# Shell state
# ---------------------------------------------------------------------------
class ScriptExit(Exception):
    """Raised by the exit built-in; caught by run_script_text and the REPL."""
    def __init__(self, code=0):
        self.code = code


class Shell:
    def __init__(self):
        self.cwd = SANDBOX_ROOT
        self.env = {
            "HOME": "/",
            "PATH": "/usr/bin:/bin",
            "PWD":  "/",
        }
        self.last_exit = 0
        self.functions  = {}   # name -> script-body string

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------
    def real(self, virtual_path: str) -> Path:
        """Convert a virtual path (relative or /absolute) to a real sandbox path."""
        if not virtual_path:
            return self.cwd
        rp = Path(virtual_path)
        try:
            rp.resolve().relative_to(SANDBOX_ROOT)
            return rp.resolve()
        except (ValueError, OSError):
            pass
        if virtual_path.startswith("/"):
            rel = virtual_path.lstrip("/")
            return (SANDBOX_ROOT / rel).resolve() if rel else SANDBOX_ROOT
        return (self.cwd / rp).resolve()

    def virtual(self, real_path: Path) -> str:
        """Convert a real sandbox path back to a virtual /path string."""
        try:
            rel = real_path.relative_to(SANDBOX_ROOT)
            rel_str = str(rel).replace("\\", "/")
            return "/" if rel_str in (".", "") else "/" + rel_str
        except ValueError:
            return str(real_path)

    def _safe(self, real_path: Path) -> Path:
        """Clamp path to sandbox root if it would escape."""
        try:
            real_path.relative_to(SANDBOX_ROOT)
            return real_path
        except ValueError:
            return SANDBOX_ROOT

    # ------------------------------------------------------------------
    # Variable / arithmetic expansion
    # ------------------------------------------------------------------
    def expand(self, text: str) -> str:
        """Expand $((...)) arithmetic, $VAR, and ${VAR} references."""
        # Arithmetic: $(( expr ))
        def arith_replace(m):
            inner = self.expand(m.group(1))
            safe  = re.sub(r'[^0-9+\-*/%() ]', '0', inner)
            try:
                return str(int(eval(safe, {"__builtins__": {}})))
            except Exception:
                return "0"
        text = re.sub(r'\$\(\((.+?)\)\)', arith_replace, text)

        def replace(m):
            name = m.group(1) or m.group(2)
            if name == "HOME":
                return self.env.get("HOME", "/")
            if name == "PWD":
                return self.virtual(self.cwd)
            return self.env.get(name, "")
        text = re.sub(r'\$\{(\w+)\}|\$(\w+)', replace, text)
        return text

    # ------------------------------------------------------------------
    # Pipe splitting (respects quotes)
    # ------------------------------------------------------------------
    def _split_pipes(self, line: str) -> list:
        """Split on | not inside quotes."""
        parts, current = [], []
        in_single = in_double = False
        for c in line:
            if   c == "'" and not in_double: in_single = not in_single; current.append(c)
            elif c == '"' and not in_single: in_double = not in_double; current.append(c)
            elif c == '|' and not in_single and not in_double:
                parts.append(''.join(current)); current = []
            else:
                current.append(c)
        if current:
            parts.append(''.join(current))
        return parts

    # ------------------------------------------------------------------
    # Redirection parsing (respects quotes)
    # ------------------------------------------------------------------
    def _parse_redirection(self, line: str):
        """
        Return (cmd_part, op, filename) for the LAST unquoted > or >> in the
        line, or None if there is none.
        """
        in_single = in_double = False
        tagged = []
        for c in line:
            if   c == "'" and not in_double: in_single = not in_single
            elif c == '"' and not in_single: in_double = not in_double
            tagged.append((c, in_single or in_double))

        result = None
        i = 0
        while i < len(tagged):
            c, quoted = tagged[i]
            if not quoted and c == '>':
                if i + 1 < len(tagged) and tagged[i+1][0] == '>' and not tagged[i+1][1]:
                    op   = '>>'
                    rest = ''.join(ch for ch, _ in tagged[i+2:]).strip()
                    cmd  = ''.join(ch for ch, _ in tagged[:i]).strip()
                    result = (cmd, op, rest.split()[0] if rest.split() else '')
                    i += 2
                else:
                    op   = '>'
                    rest = ''.join(ch for ch, _ in tagged[i+1:]).strip()
                    cmd  = ''.join(ch for ch, _ in tagged[:i]).strip()
                    result = (cmd, op, rest.split()[0] if rest.split() else '')
                    i += 1
            else:
                i += 1
        return result

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------
    def run_line(self, line: str) -> str:
        """Parse and execute a single shell line. Returns stdout string."""
        line = line.strip()
        if not line or line.startswith("#"):
            return ""

        # source <(curl -s url)
        m = re.match(r'source\s+<\((.+)\)', line)
        if m:
            inner       = m.group(1).strip()
            script_text = self._run_command_capture(inner)
            if script_text.startswith("#curl-error:"):
                return script_text.replace("#curl-error:", "curl error:") + "\n"
            return self.run_script_text(script_text)

        # VAR=value  (simple assignment, no space before =)
        if re.match(r'^[A-Za-z_]\w*=', line) and " " not in line.split("=")[0]:
            k, _, v = line.partition("=")
            self.env[k] = self.expand(v.strip("'\""))
            return ""

        # ---- Pipes ----
        pipe_parts = self._split_pipes(line)
        if len(pipe_parts) > 1:
            # Check for trailing redirection on last segment
            last = pipe_parts[-1].strip()
            redir = self._parse_redirection(last)
            if redir:
                last_cmd, redir_op, redir_file = redir
                pipe_parts[-1] = last_cmd
            else:
                redir_op = redir_file = None

            data = ""
            for part in pipe_parts:
                data = self._run_with_stdin(part.strip(), data)

            if redir_op and redir_file:
                redir_file = self.expand(redir_file)
                dest = self._safe(self.real(redir_file))
                dest.parent.mkdir(parents=True, exist_ok=True)
                mode = 'a' if redir_op == '>>' else 'w'
                with open(dest, mode, encoding='utf-8') as f:
                    f.write(data or '')
                return ""
            return data

        # ---- Output redirection ----
        redir = self._parse_redirection(line)
        if redir:
            cmd_part, redir_op, redir_file = redir
            if cmd_part and redir_file:
                redir_file = self.expand(redir_file)
                output = self.run_line(cmd_part)
                dest   = self._safe(self.real(redir_file))
                dest.parent.mkdir(parents=True, exist_ok=True)
                mode = 'a' if redir_op == '>>' else 'w'
                with open(dest, mode, encoding='utf-8') as f:
                    f.write(output or '')
                return ""

        # ---- Normal command dispatch ----
        parts = self._split(line)
        if not parts:
            return ""
        parts = [self.expand(p) for p in parts]
        cmd, args = parts[0], parts[1:]

        builtins = {
            "ls":      self._ls,
            "cd":      self._cd,
            "pwd":     self._pwd,
            "cat":     self._cat,
            "mv":      self._mv,
            "cp":      self._cp,
            "rm":      self._rm,
            "mkdir":   self._mkdir,
            "touch":   self._touch,
            "echo":    self._echo,
            "source":  self._source,
            "chmod":   self._chmod,
            "shuf":    self._shuf,
            "head":    self._head,
            "tail":    self._tail,
            "grep":    self._grep,
            "find":    self._find,
            "wc":      self._wc,
            "hexdump": self._hexdump,
            "md5sum":  self._md5sum,
            "awk":     self._awk,
            "curl":    self._curl,
            "exit":    self._exit,
            "clear":   lambda a: "",
            "read":    lambda a: "",     # no-op; scripts use it for interactive prompts
        }

        if cmd in builtins:
            return builtins[cmd](args) or ""

        # User-defined shell functions
        if cmd in self.functions:
            extra = {"0": cmd, **{str(i + 1): a for i, a in enumerate(args)}}
            return self.run_script_text(self.functions[cmd], extra, restore_env=False)

        # ./script.sh or /abs/path
        if cmd.startswith("./") or cmd.startswith("/"):
            return self._exec_script(cmd, args)

        # Bare name in cwd
        script_path = self.cwd / cmd
        if script_path.exists():
            return self._exec_script("./" + cmd, args)

        return f"emulator: command not found: {cmd}\n"

    # ------------------------------------------------------------------
    # Pipe helper: run a command with piped stdin injected as a temp file
    # ------------------------------------------------------------------
    def _run_with_stdin(self, cmd_str: str, stdin_data: str) -> str:
        cmd_str = cmd_str.strip()
        if not cmd_str:
            return stdin_data
        parts = self._split(cmd_str)
        if not parts:
            return stdin_data
        cmd = self.expand(parts[0])
        stdin_cmds = {"grep", "wc", "md5sum", "awk", "cat", "head", "tail", "sort", "uniq"}
        if cmd in stdin_cmds and stdin_data.strip():
            tmp = tempfile.NamedTemporaryFile(
                mode='w', suffix='.tmp', dir=str(SANDBOX_ROOT),
                delete=False, encoding='utf-8')
            tmp.write(stdin_data)
            tmp.close()
            out = self.run_line(cmd_str + " " + repr(tmp.name))
            Path(tmp.name).unlink(missing_ok=True)
            return out
        return self.run_line(cmd_str)

    # ------------------------------------------------------------------
    # Function definition extraction
    # ------------------------------------------------------------------
    def _extract_functions(self, text: str) -> str:
        """
        Scan script text for shell function definitions, store them in
        self.functions, and return the text with those blocks removed.

        Handles both:
            funcname() {        (brace on same line)
            funcname()          (brace on next line)
            {
        """
        lines  = text.splitlines()
        result = []
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            m = re.match(r'^(\w+)\(\)\s*(\{?)\s*$', stripped)
            if m:
                fname     = m.group(1)
                has_brace = bool(m.group(2))
                i += 1
                if not has_brace:
                    # Skip blank lines until we find the opening {
                    while i < len(lines) and lines[i].strip() == '':
                        i += 1
                    if i < len(lines) and lines[i].strip() == '{':
                        i += 1
                # Collect body until depth reaches 0
                body_lines = []
                depth = 1
                while i < len(lines):
                    ls = lines[i].strip()
                    if ls == '{':
                        depth += 1
                        body_lines.append(lines[i])
                    elif ls == '}':
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                        else:
                            body_lines.append(lines[i])
                    else:
                        body_lines.append(lines[i])
                    i += 1
                self.functions[fname] = '\n'.join(body_lines)
                continue
            result.append(lines[i])
            i += 1
        return '\n'.join(result)

    # ------------------------------------------------------------------
    # Script execution
    # ------------------------------------------------------------------
    def run_script_text(self, text: str, extra_env: dict = None, restore_env: bool = True) -> str:
        """Execute multi-line shell script text inside the emulator."""
        text = self._extract_functions(text)
        saved_env = dict(self.env)
        if extra_env:
            self.env.update(extra_env)
        try:
            output = self._run_lines(text.splitlines())
        except ScriptExit:
            output = []
        if restore_env:
            self.env = saved_env
        return "".join(output)

    def _run_lines(self, lines: list) -> list:
        """Execute a list of script lines, handling if/for blocks lazily."""
        output = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line or line.startswith("#"):
                i += 1
                continue
            if re.match(r'^if\b', line):
                block, i = self._collect_block(lines, i, 'if', 'fi')
                branch = self._expand_if(block)
                output.extend(self._run_lines(branch))
            elif re.match(r'^for\b', line):
                block, i = self._collect_block(lines, i, 'for', 'done')
                output.extend(self._run_for_block(block))
            elif re.match(r'^while\b', line):
                block, i = self._collect_block(lines, i, 'while', 'done')
                # no-op: while loops not needed for CLI Dungeon
                pass
            else:
                line, _ = self._resolve_command_substitutions(line)
                try:
                    out = self.run_line(line)
                except ScriptExit:
                    raise
                if out:
                    output.append(out)
                i += 1
        return output

    def _run_for_block(self, block: list) -> list:
        """Execute a for loop block with current env variable values."""
        lines = [l.strip() for l in block]
        m = re.match(r'for\s+(\w+)\s+in\s+(.+?)(?:;|\s*$)', lines[0])
        if not m:
            return []
        var       = m.group(1)
        items_str = self.expand(m.group(2)).strip()
        range_m   = re.match(r'\{(\d+)\.\.(\d+)\}', items_str)
        if range_m:
            items = list(range(int(range_m.group(1)), int(range_m.group(2)) + 1))
        else:
            items = items_str.split()
        body = [l for l in lines[1:] if l.strip() not in ('do', 'done')]
        output = []
        for val in items:
            iter_body = [re.sub(r'\$\{?' + var + r'\}?', str(val), l) for l in body]
            output.extend(self._run_lines(iter_body))
        return output

    def _collect_block(self, lines, start, open_kw, close_kw):
        block = [lines[start]]
        depth = 1
        i     = start + 1
        while i < len(lines) and depth > 0:
            l = lines[i].strip()
            if re.match(r'^' + re.escape(open_kw)  + r'\b', l): depth += 1
            elif re.match(r'^' + re.escape(close_kw) + r'\b', l): depth -= 1
            block.append(lines[i])
            i += 1
        return block, i

    def _expand_if(self, block):
        lines     = [l.strip() for l in block]
        header    = lines[0]
        condition = True
        cond_m    = re.search(r'\[\s*(.+?)\s*\]', header)
        if cond_m:
            condition = self._eval_condition(cond_m.group(1))
        else:
            cmd_m = re.match(r'if\s+(.+)', header)
            if cmd_m:
                out       = self._run_command_capture(cmd_m.group(1))
                condition = bool(out.strip())

        then_lines, else_lines, in_else = [], [], False
        depth = 0   # track nested if depth so inner fi does not terminate the scan
        for l in lines[1:]:
            import re as _re
            if _re.match(r"^if\b", l):
                depth += 1
                (else_lines if in_else else then_lines).append(l)
            elif _re.match(r"^fi\b", l):
                if depth == 0:
                    break           # this fi closes the current if — done
                depth -= 1
                (else_lines if in_else else then_lines).append(l)
            elif depth == 0 and l == 'then':
                continue
            elif depth == 0 and l == 'else':
                in_else = True
            else:
                (else_lines if in_else else then_lines).append(l)
        return then_lines if condition else else_lines

    def _eval_condition(self, expr: str) -> bool:
        expr = self.expand(expr).strip()
        m = re.match(r'-d\s+"?(.+?)"?$', expr)
        if m: p = self.real(m.group(1)); return p.exists() and p.is_dir()
        m = re.match(r'-f\s+"?(.+?)"?$', expr)
        if m: p = self.real(m.group(1)); return p.exists() and p.is_file()
        m = re.match(r'-z\s+"?(.*?)"?$', expr)
        if m: return len(m.group(1)) == 0
        m = re.match(r'(.+?)\s+(-eq|-ne|-lt|-gt|-le|-ge)\s+(.+)', expr)
        if m:
            try:
                a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
                return {'eq':a==b,'ne':a!=b,'lt':a<b,'gt':a>b,'le':a<=b,'ge':a>=b}[op[1:]]
            except ValueError: return False
        m = re.match(r'"?(.+?)"?\s*=\s*"?(.+?)"?$', expr)
        if m: return m.group(1) == m.group(2)
        m = re.match(r'"?(.+?)"?\s*!=\s*"?(.+?)"?$', expr)
        if m: return m.group(1) != m.group(2)
        return False

    def _resolve_command_substitutions(self, line: str):
        """Replace `cmd` and $(cmd) with their output. Skips $((...)) arithmetic."""
        def replacer(m):
            inner = m.group(1) or m.group(2)
            return self._run_command_capture(inner).strip()
        line = re.sub(r'`([^`]+)`|\$\((?!\()([^)]+)\)', replacer, line)
        return line, {}

    def _run_command_capture(self, cmd_str: str) -> str:
        """Run a command (including pipes) and return its stdout."""
        saved_cwd  = self.cwd
        pipe_parts = self._split_pipes(cmd_str)
        if len(pipe_parts) > 1:
            data = ""
            for part in pipe_parts:
                data = self._run_with_stdin(part.strip(), data)
            self.cwd = saved_cwd
            return data or ""
        out = self.run_line(cmd_str)
        self.cwd = saved_cwd
        return out or ""

    def _exec_script(self, cmd: str, args: list) -> str:
        """Execute a .sh file inside the emulator."""
        real_path = self.cwd / cmd[2:] if cmd.startswith("./") else self.real(cmd)
        self._safe(real_path)
        if not real_path.exists():
            return f"emulator: {cmd}: No such file or directory\n"
        text = real_path.read_text(encoding="utf-8", errors="replace")
        return self.run_script_text(
            text, {"0": cmd, **{str(i + 1): a for i, a in enumerate(args)}})

    # ------------------------------------------------------------------
    # Tokenizer
    # ------------------------------------------------------------------
    def _split(self, line: str) -> list:
        """Split a shell line into tokens, respecting single and double quotes."""
        tokens, current = [], []
        in_single = in_double = False
        for c in line:
            if   c == "'" and not in_double: in_single = not in_single
            elif c == '"' and not in_single: in_double = not in_double
            elif c == ' ' and not in_single and not in_double:
                if current: tokens.append("".join(current)); current = []
            else:
                current.append(c)
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
        lines   = [e.name + ("/" if e.is_dir() else "") for e in entries]
        return "  ".join(lines) + "\n" if lines else "\n"

    def _cd(self, args):
        if not args:
            home = self.env.get("HOME", "/")
            self.cwd = self._safe(self.real(home))
        else:
            target = self._safe(self.real(args[0]))
            if not target.exists() or not target.is_dir():
                return f"cd: {args[0]}: No such file or directory\n"
            self.cwd = target
        self.env["PWD"] = self.virtual(self.cwd)
        return ""

    def _pwd(self, args):
        return self.virtual(self.cwd) + "\n"

    def _cat(self, args):
        if not args: return ""
        out = []
        for a in args:
            if a.startswith("-"): continue
            p = self._safe(self.real(a))
            if not p.exists():    out.append(f"cat: {a}: No such file or directory\n")
            elif p.is_dir():      out.append(f"cat: {a}: Is a directory\n")
            else:                 out.append(p.read_text(encoding="utf-8", errors="replace"))
        return "".join(out)

    def _mv(self, args):
        if len(args) < 2: return "mv: missing operand\n"
        src = self._safe(self.real(args[0]))
        dst = self._safe(self.real(args[1]))
        if not src.exists():
            return f"mv: cannot stat '{args[0]}': No such file or directory\n"
        if dst.is_dir(): dst = dst / src.name
        shutil.move(str(src), str(dst))
        return ""

    def _cp(self, args):
        paths = [a for a in args if not a.startswith("-")]
        if len(paths) < 2: return "cp: missing operand\n"
        src = self._safe(self.real(paths[0]))
        dst = self._safe(self.real(paths[1]))
        if not src.exists():
            return f"cp: cannot stat '{paths[0]}': No such file or directory\n"
        if src.is_dir(): shutil.copytree(str(src), str(dst))
        else:
            if dst.is_dir(): dst = dst / src.name
            shutil.copy2(str(src), str(dst))
        return ""

    def _rm(self, args):
        flags     = [a for a in args if a.startswith("-")]
        paths     = [a for a in args if not a.startswith("-")]
        recursive = any(f in flags for f in ("-r", "-rf", "-fr"))
        out = []
        for p_str in paths:
            p = self._safe(self.real(p_str))
            if not p.exists():
                out.append(f"rm: cannot remove '{p_str}': No such file or directory\n")
                continue
            if p.is_dir():
                if recursive: shutil.rmtree(str(p))
                else: out.append(f"rm: cannot remove '{p_str}': Is a directory (use -r)\n")
            else:
                p.unlink()
        return "".join(out)

    def _mkdir(self, args):
        for p_str in (a for a in args if not a.startswith("-")):
            self._safe(self.real(p_str)).mkdir(parents=True, exist_ok=True)
        return ""

    def _touch(self, args):
        for a in args:
            if not a.startswith("-"):
                self._safe(self.real(a)).touch()
        return ""

    def _echo(self, args):
        no_newline = "-n" in args
        parts      = [a for a in args if a != "-n"]
        return " ".join(parts) + ("" if no_newline else "\n")

    def _source(self, args):
        if not args: return ""
        return self._exec_script(args[0], args[1:])

    def _chmod(self, args):
        return ""   # no-op (no POSIX perms in the sandbox)

    def _shuf(self, args):
        n, input_range, files = None, None, []
        i = 0
        while i < len(args):
            if args[i] == "-n":
                n = int(args[i+1]); i += 2
            elif args[i] == "-i":
                lo, hi       = args[i+1].split("-")
                input_range  = (int(lo), int(hi)); i += 2
            else:
                files.append(args[i]); i += 1
        if input_range:
            pop = list(range(input_range[0], input_range[1] + 1))
            k   = n if n is not None else len(pop)
            return "\n".join(str(x) for x in random.sample(pop, min(k, len(pop)))) + "\n"
        lines = []
        for f in files:
            p = self._safe(self.real(f))
            if p.exists() and p.is_file():
                lines.extend(l for l in p.read_text(encoding="utf-8").splitlines() if l.strip())
        random.shuffle(lines)
        if n is not None: lines = lines[:n]
        return "\n".join(lines) + "\n"

    def _head(self, args):
        n, files, i = 10, [], 0
        while i < len(args):
            if args[i] == "-n":               n = int(args[i+1]); i += 2
            elif re.match(r'^-\d+$', args[i]): n = int(args[i][1:]); i += 1
            else:                              files.append(args[i]); i += 1
        out = []
        for f in files:
            p = self._safe(self.real(f))
            if p.exists():
                out.extend(p.read_text(encoding="utf-8").splitlines()[:n])
        return "\n".join(out) + "\n" if out else ""

    def _tail(self, args):
        n, files, i = 10, [], 0
        while i < len(args):
            if args[i] == "-n":               n = int(args[i+1]); i += 2
            elif re.match(r'^-\d+$', args[i]): n = int(args[i][1:]); i += 1
            else:                              files.append(args[i]); i += 1
        out = []
        for f in files:
            p = self._safe(self.real(f))
            if p.exists():
                out.extend(p.read_text(encoding="utf-8").splitlines()[-n:])
        return "\n".join(out) + "\n" if out else ""

    def _grep(self, args):
        flags     = [a for a in args if a.startswith("-")]
        non_flags = [a for a in args if not a.startswith("-")]
        if not non_flags: return ""
        pattern, targets = non_flags[0], non_flags[1:]
        recursive    = "-R" in flags or "-r" in flags
        exclude_dirs = [re.match(r'--exclude-dir=(.*)', f).group(1)
                        for f in flags if re.match(r'--exclude-dir=(.*)', f)]
        results = []
        def search_file(p, label):
            try:
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    if re.search(pattern, line): results.append(f"{label}:{line}")
            except Exception: pass
        for t in targets:
            tp = self._safe(self.real(t))
            if tp.is_file(): search_file(tp, t)
            elif tp.is_dir() and recursive:
                for fp in tp.rglob("*"):
                    if fp.is_file() and not any(excl in fp.parts for excl in exclude_dirs):
                        search_file(fp, self.virtual(fp))
        return "\n".join(results) + "\n" if results else ""

    def _find(self, args):
        start, name_pat, i = ".", None, 0
        while i < len(args):
            if args[i] == "-name": name_pat = args[i+1]; i += 2
            else:                  start    = args[i];   i += 1
        base    = self._safe(self.real(start))
        results = []
        for p in base.rglob("*"):
            if name_pat:
                import fnmatch
                if fnmatch.fnmatch(p.name, name_pat): results.append(self.virtual(p))
            else:
                results.append(self.virtual(p))
        return "\n".join(results) + "\n" if results else ""

    def _wc(self, args):
        flags = [a for a in args if a.startswith("-")]
        files = [a for a in args if not a.startswith("-")]
        out   = []
        for f in files:
            p = self._safe(self.real(f))
            if p.exists():
                text = p.read_text(encoding="utf-8")
                l, w, c = text.count("\n"), len(text.split()), len(text)
                if   "-l" in flags: out.append(str(l))
                elif "-w" in flags: out.append(str(w))
                elif "-c" in flags: out.append(str(c))
                else:               out.append(f"{l} {w} {c} {f}")
        return "\n".join(out) + "\n" if out else ""

    def _hexdump(self, args):
        import secrets
        data = secrets.token_bytes(16)
        return "".join(data[i:i+4].hex().upper() for i in range(0, 16, 4)) + "\n"

    def _md5sum(self, args):
        if not args: return ""
        p = self._safe(self.real(args[0]))
        if p.exists() and p.is_file(): data = p.read_bytes()
        else:                          data = " ".join(args).encode("utf-8")
        return f"{hashlib.md5(data).hexdigest()}  -\n"

    def _awk(self, args):
        prog, files, i = "", [], 0
        while i < len(args):
            if args[i] == "-F": i += 2
            elif prog == "":    prog = args[i]; i += 1
            else:               files.append(args[i]); i += 1
        m     = re.search(r'\{print \$(\d+)\}', prog)
        field = int(m.group(1)) - 1 if m else 0
        out   = []
        for f in files:
            p = self._safe(self.real(f))
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if field < len(parts): out.append(parts[field])
        return "\n".join(out) + "\n" if out else ""

    def _curl(self, args):
        flags, url, output_file, silent = set(), None, None, False
        i = 0
        while i < len(args):
            a = args[i]
            if   a in ("-s", "--silent"):              silent = True; i += 1
            elif a in ("-L", "--location"):             i += 1
            elif a in ("-J", "-O", "-Os", "-LJOs", "-LJO"): flags.add("save"); i += 1
            elif a == "-o":                            output_file = args[i+1]; i += 2
            elif not a.startswith("-"):                url = a; i += 1
            else:                                      i += 1
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
            fname = output_file or url.split("/")[-1].split("?")[0]
            dest  = self._safe(self.cwd / fname)
            dest.write_text(text, encoding="utf-8")
            return ""
        return text

    def _exit(self, args):
        code = int(args[0]) if args else 0
        raise ScriptExit(code)


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
            line  = input(f"\033[1;32memulator\033[0m:\033[1;34m{vpath}\033[0m$ ")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        line = line.strip()
        if not line:
            continue

        try:
            out = shell.run_line(line)
        except ScriptExit:
            print("\nGoodbye!")
            break
        if out:
            print(out, end="" if out.endswith("\n") else "\n")


if __name__ == "__main__":
    main()