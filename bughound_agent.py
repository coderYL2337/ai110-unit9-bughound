import json
import re
from typing import Any, Dict, List, Optional, Tuple

from reliability.risk_assessor import assess_risk


class BugHoundAgent:
    """
    BugHound runs a small agentic workflow:

    1) PLAN: decide what to look for
    2) ANALYZE: detect issues (heuristics or LLM)
    3) ACT: propose a fix (heuristics or LLM)
    4) TEST: run simple reliability checks
    5) REFLECT: decide whether to apply the fix automatically
    """

    def __init__(self, client: Optional[Any] = None):
        # client should implement: complete(system_prompt: str, user_prompt: str) -> str
        self.client = client
        self.logs: List[Dict[str, str]] = []

    # ----------------------------
    # Public API
    # ----------------------------
    def run(self, code_snippet: str) -> Dict[str, Any]:
        self.logs = []
        self._log("PLAN", "Planning a quick scan + fix proposal workflow.")

        issues = self.analyze(code_snippet)
        self._log("ANALYZE", f"Found {len(issues)} issue(s).")

        fixed_code = self.propose_fix(code_snippet, issues)
        if fixed_code.strip() == "":
            self._log("ACT", "No fix produced (refused, error, or empty output).")

        risk = assess_risk(original_code=code_snippet, fixed_code=fixed_code, issues=issues)
        self._log("TEST", f"Risk assessed as {risk.get('level', 'unknown')} (score={risk.get('score', '-')}).")

        if risk.get("should_autofix"):
            self._log("REFLECT", "Fix appears safe enough to auto-apply under current policy.")
        else:
            self._log("REFLECT", "Fix is not safe enough to auto-apply. Human review recommended.")

        return {
            "issues": issues,
            "fixed_code": fixed_code,
            "risk": risk,
            "logs": self.logs,
        }

    # ----------------------------
    # Workflow steps
    # ----------------------------
    def analyze(self, code_snippet: str) -> List[Dict[str, str]]:
        if not self._can_call_llm():
            self._log("ANALYZE", "Using heuristic analyzer (offline mode).")
            return self._heuristic_analyze(code_snippet)

        self._log("ANALYZE", "Using LLM analyzer.")
        system_prompt = (
            "You are BugHound, a code review assistant. "
            "Return ONLY valid JSON. No markdown, no backticks."
        )
        user_prompt = (
            "Analyze this Python code for potential issues. "
            "Return a JSON array of issue objects with keys: type, severity, msg.\n\n"
            f"CODE:\n{code_snippet}"
        )

        # UPDATED: Added exception handling for API errors/rate limits
        try:
            raw = self.client.complete(system_prompt=system_prompt, user_prompt=user_prompt)
        except Exception as e:
            self._log("ANALYZE", f"API Error: {str(e)}. Falling back to heuristics.")
            return self._heuristic_analyze(code_snippet)

        issues = self._parse_json_array_of_issues(raw)

        if issues is None:
            self._log("ANALYZE", "LLM output was not parseable JSON. Falling back to heuristics.")
            return self._heuristic_analyze(code_snippet)

        # Always run heuristics and merge so syntax errors and known
        # patterns are never missed just because the LLM is available.
        heuristic_issues = self._heuristic_analyze(code_snippet)
        seen_msgs = {i["msg"] for i in issues}
        for h in heuristic_issues:
            if h["msg"] not in seen_msgs:
                issues.append(h)

        return issues

    def propose_fix(self, code_snippet: str, issues: List[Dict[str, str]]) -> str:
        if not issues:
            self._log("ACT", "No issues, returning original code unchanged.")
            return code_snippet

        if not self._can_call_llm():
            self._log("ACT", "Using heuristic fixer (offline mode).")
            return self._heuristic_fix(code_snippet, issues)

        self._log("ACT", "Using LLM fixer.")
        system_prompt = (
            "You are BugHound, a careful refactoring assistant. "
            "Return ONLY the full rewritten Python code. No markdown, no backticks."
        )
        user_prompt = (
            "Rewrite the code to address the issues listed. "
            "Fix any syntax errors such as missing indentation first. "
            "Ensure the output is valid, properly indented Python. "
            "Preserve behavior when possible. Keep changes minimal.\n\n"
            f"ISSUES (JSON):\n{json.dumps(issues)}\n\n"
            f"CODE:\n{code_snippet}"
        )

        # UPDATED: Added exception handling for API errors/rate limits
        try:
            raw = self.client.complete(system_prompt=system_prompt, user_prompt=user_prompt)
        except Exception as e:
            self._log("ACT", f"API Error: {str(e)}. Falling back to heuristic fixer.")
            return self._heuristic_fix(code_snippet, issues)

        cleaned = self._strip_code_fences(raw).strip()

        if not cleaned:
            self._log("ACT", "LLM returned empty output. Falling back to heuristic fixer.")
            return self._heuristic_fix(code_snippet, issues)

        # Validate that the LLM fix actually compiles
        try:
            compile(cleaned, "<llm_fix>", "exec")
        except SyntaxError as e:
            self._log("ACT", f"LLM fix still has syntax errors: {e.msg} (line {e.lineno}). Retrying with stronger prompt.")
            retry_prompt = (
                "The following Python code has a syntax error. "
                "Fix ALL indentation and syntax errors. "
                "Return ONLY the corrected Python code. No markdown, no backticks.\n\n"
                f"CODE:\n{cleaned}"
            )
            try:
                raw2 = self.client.complete(system_prompt=system_prompt, user_prompt=retry_prompt)
                cleaned2 = self._strip_code_fences(raw2).strip()
                compile(cleaned2, "<llm_fix_retry>", "exec")
                return cleaned2
            except Exception:
                self._log("ACT", "Retry also failed. Falling back to heuristic fixer.")
                return self._heuristic_fix(code_snippet, issues)

        return cleaned

    # ----------------------------
    # Heuristic analyzer/fixer
    # ----------------------------
    def _heuristic_analyze(self, code: str) -> List[Dict[str, str]]:
        issues: List[Dict[str, str]] = []

        # IndentationError (and TabError) are subclasses of SyntaxError — catch them first.
        try:
            compile(code, "<input>", "exec")
        except IndentationError as e:
            issues.append(
                {
                    "type": "Indentation",
                    "severity": "High",
                    "msg": f"Indentation error: {e.msg} (line {e.lineno}).",
                }
            )
        except SyntaxError as e:
            issues.append(
                {
                    "type": "Syntax",
                    "severity": "High",
                    "msg": f"Code has a syntax error: {e.msg} (line {e.lineno}).",
                }
            )

        # Detect tab characters used for indentation (PEP 8 requires spaces).
        lines = code.split("\n")
        tab_indented = [
            i + 1
            for i, line in enumerate(lines)
            if line and "\t" in line[: len(line) - len(line.lstrip())]
        ]
        space_indented = [
            i + 1
            for i, line in enumerate(lines)
            if line and line[0] == " "
        ]
        if tab_indented and space_indented:
            issues.append(
                {
                    "type": "Indentation",
                    "severity": "High",
                    "msg": (
                        f"Mixed tabs and spaces for indentation "
                        f"(tab on line(s) {tab_indented[:5]}). Use spaces only (PEP 8)."
                    ),
                }
            )
        elif tab_indented:
            issues.append(
                {
                    "type": "Indentation",
                    "severity": "Medium",
                    "msg": (
                        f"Tab characters used for indentation on line(s) {tab_indented[:5]}. "
                        "Use 4 spaces per PEP 8."
                    ),
                }
            )

        # Detect block-keyword lines missing a trailing colon.
        BLOCK_KW = re.compile(r"^\s*(if|elif|else|for|while|def|class|with|try|except|finally)\b")
        missing_colon_lines = []
        for i, line in enumerate(lines):
            if not BLOCK_KW.match(line):
                continue
            code_part = line.split("#")[0].rstrip()
            if code_part and not code_part.endswith(":") and not code_part.endswith("\\"):
                missing_colon_lines.append(i + 1)
        if missing_colon_lines:
            issues.append(
                {
                    "type": "Syntax",
                    "severity": "High",
                    "msg": (
                        f"Missing colon `:` after block statement on "
                        f"line(s) {missing_colon_lines[:5]}."
                    ),
                }
            )

        # Detect def/class blocks whose body contains only comments (no executable code).
        OPENER = re.compile(r"^(\s*)(def |class )")
        for i, line in enumerate(lines):
            m = OPENER.match(line)
            if not m or not line.rstrip().endswith(":"):
                continue
            opener_indent = len(m.group(1))
            has_executable = False
            j = i + 1
            while j < len(lines):
                l = lines[j]
                if not l.strip():
                    j += 1
                    continue
                if len(l) - len(l.lstrip()) <= opener_indent:
                    break
                if not l.strip().startswith("#"):
                    has_executable = True
                    break
                j += 1
            if not has_executable:
                label = line.strip()[:50]
                issues.append(
                    {
                        "type": "Maintainability",
                        "severity": "Medium",
                        "msg": (
                            f"Block `{label}` (line {i + 1}) has no executable code — "
                            "body is empty or only contains comments. Add `pass` or real code."
                        ),
                    }
                )

        if "print(" in code:
            issues.append(
                {
                    "type": "Code Quality",
                    "severity": "Low",
                    "msg": "Found print statements. Consider using logging for non-toy code.",
                }
            )

        if re.search(r"\bexcept\s*:\s*(\n|#|$)", code):
            issues.append(
                {
                    "type": "Reliability",
                    "severity": "High",
                    "msg": "Found a bare `except:`. Catch a specific exception or use `except Exception as e:`.",
                }
            )

        if "TODO" in code:
            issues.append(
                {
                    "type": "Maintainability",
                    "severity": "Medium",
                    "msg": "Found TODO comments. Unfinished logic can hide bugs or missing cases.",
                }
            )

        return issues

    def _heuristic_fix(self, code: str, issues: List[Dict[str, str]]) -> str:
        fixed = code

        if any(i.get("type") == "Syntax" for i in issues):
            fixed = self._fix_missing_colons(fixed)

        if any(i.get("type") == "Indentation" for i in issues):
            fixed = fixed.expandtabs(4)
            fixed = self._fix_indentation_error(fixed)

        if any(i.get("type") == "Reliability" for i in issues):
            fixed = re.sub(r"\bexcept\s*:\s*", "except Exception as e:\n        # [BugHound] log or handle the error\n        ", fixed)

        if any(i.get("type") == "Code Quality" for i in issues):
            if "import logging" not in fixed:
                fixed = "import logging\n\n" + fixed
            fixed = fixed.replace("print(", "logging.info(")

        return fixed

    def _fix_missing_colons(self, code: str) -> str:
        """Add a missing trailing colon to block-opening keyword lines."""
        BLOCK_KW = re.compile(r"^\s*(if|elif|else|for|while|def|class|with|try|except|finally)\b")
        lines = code.split("\n")
        for i, line in enumerate(lines):
            if not BLOCK_KW.match(line):
                continue
            comment_idx = line.find("#")
            code_part = line[:comment_idx].rstrip() if comment_idx != -1 else line.rstrip()
            comment_part = (" " + line[comment_idx:]) if comment_idx != -1 else ""
            if code_part and not code_part.endswith(":") and not code_part.endswith("\\"):
                lines[i] = code_part + ":" + comment_part
        return "\n".join(lines)

    def _fix_indentation_error(self, code: str) -> str:
        """Re-indent the block that caused an IndentationError.

        Runs up to 5 times so nested or sequential errors are also repaired.
        Handles the special case where a block body contains only comments by
        inserting a ``pass`` statement so the block is syntactically valid.
        """
        for _ in range(5):
            try:
                compile(code, "<input>", "exec")
                return code
            except IndentationError as e:
                if e.lineno is None:
                    return code
                lines = code.split("\n")
                error_idx = min(e.lineno - 1, len(lines) - 1)

                # "unindent does not match any outer indentation level" — the
                # error line's indent level doesn't match an enclosing scope.
                # Re-align it to the nearest valid indentation level above it.
                if "unindent" in (e.msg or "").lower():
                    error_line_indent = len(lines[error_idx]) - len(lines[error_idx].lstrip())
                    seen_levels = sorted(set(
                        len(l) - len(l.lstrip())
                        for l in lines[:error_idx]
                        if l.strip()
                    ))
                    candidates = [lvl for lvl in seen_levels if lvl > error_line_indent]
                    if not candidates:
                        return code
                    lines[error_idx] = " " * min(candidates) + lines[error_idx].lstrip()
                    code = "\n".join(lines)
                    continue

                # Find the block-opening line above the error, skipping blank
                # lines AND comment lines (Python tokenizer ignores both).
                prev_idx = error_idx - 1
                while prev_idx >= 0 and (
                    not lines[prev_idx].strip()
                    or lines[prev_idx].strip().startswith("#")
                ):
                    prev_idx -= 1
                if prev_idx < 0:
                    return code

                opener_line = lines[prev_idx]
                opener_indent = len(opener_line) - len(opener_line.lstrip())
                expected_indent = opener_indent + 4

                # If every line between the opener and the error is blank or a
                # comment, the block has no executable body.  Insert ``pass``.
                between = lines[prev_idx + 1 : error_idx + 1]
                all_comments = all(
                    not l.strip() or l.strip().startswith("#") for l in between
                )
                if all_comments and opener_line.rstrip().endswith(":"):
                    lines.insert(prev_idx + 1, " " * expected_indent + "pass")
                    code = "\n".join(lines)
                    continue

                # Normal case: the error line just needs more indentation.
                error_line_indent = len(lines[error_idx]) - len(lines[error_idx].lstrip())
                delta = expected_indent - error_line_indent
                if delta <= 0:
                    return code

                for i in range(error_idx, len(lines)):
                    if not lines[i].strip():
                        continue
                    line_indent = len(lines[i]) - len(lines[i].lstrip())
                    # Stop when we reach a line at or below the opener's level —
                    # it belongs to an outer scope and must not be re-indented.
                    if i > error_idx and line_indent <= opener_indent:
                        break
                    lines[i] = " " * (line_indent + delta) + lines[i].lstrip()
                code = "\n".join(lines)
            except SyntaxError as e:
                # 'return'/'break'/'continue'/'yield' outside its block looks
                # like a SyntaxError, not an IndentationError, when the keyword
                # landed at the wrong indent level.  Re-align it the same way
                # we handle unindent errors.
                _outside = ("'return' outside function", "'break' outside loop",
                            "'continue' outside loop", "'yield' outside function")
                if e.lineno and any(msg in (e.msg or "") for msg in _outside):
                    lines = code.split("\n")
                    error_idx = min(e.lineno - 1, len(lines) - 1)
                    error_line_indent = len(lines[error_idx]) - len(lines[error_idx].lstrip())
                    seen_levels = sorted(set(
                        len(l) - len(l.lstrip())
                        for l in lines[:error_idx]
                        if l.strip()
                    ))
                    candidates = [lvl for lvl in seen_levels if lvl > error_line_indent]
                    if candidates:
                        lines[error_idx] = " " * min(candidates) + lines[error_idx].lstrip()
                        code = "\n".join(lines)
                        continue
                return code
        return code

    # ----------------------------
    # Parsing + utilities
    # ----------------------------
    def _parse_json_array_of_issues(self, text: str) -> Optional[List[Dict[str, str]]]:
        text = text.strip()
        parsed = self._try_json_loads(text)
        if isinstance(parsed, list):
            return self._normalize_issues(parsed)

        array_str = self._extract_first_json_array(text)
        if array_str:
            parsed2 = self._try_json_loads(array_str)
            if isinstance(parsed2, list):
                return self._normalize_issues(parsed2)

        return None

    def _normalize_issues(self, arr: List[Any]) -> List[Dict[str, str]]:
        issues: List[Dict[str, str]] = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            issues.append(
                {
                    "type": str(item.get("type", "Issue")),
                    "severity": self._normalize_severity(item.get("severity", "")),
                    "msg": str(item.get("msg", "")).strip(),
                }
            )
        return issues

    def _normalize_severity(self, raw: Any) -> str:
        """Map LLM severity synonyms to the three levels the risk assessor recognizes."""
        key = str(raw).strip().lower()
        mapping = {
            "high": "High", "critical": "High", "severe": "High", "error": "High",
            "medium": "Medium", "moderate": "Medium", "warning": "Medium",
            "low": "Low", "minor": "Low", "info": "Low", "trivial": "Low", "negligible": "Low",
        }
        return mapping.get(key, "Medium")

    def _try_json_loads(self, s: str) -> Any:
        try:
            return json.loads(s)
        except Exception:
            return None

    def _extract_first_json_array(self, s: str) -> Optional[str]:
        start = s.find("[")
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "[":
                depth += 1
            elif s[i] == "]":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
        return None

    def _strip_code_fences(self, text: str) -> str:
        text = text.strip()
        match = re.search(r"```(?:python)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1)
        return text

    def _can_call_llm(self) -> bool:
        return self.client is not None and hasattr(self.client, "complete")

    def _log(self, step: str, message: str) -> None:
        self.logs.append({"step": step, "message": message})