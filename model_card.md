# BugHound Mini Model Card (Reflection)

Fill this out after you run BugHound in **both** modes (Heuristic and Gemini).

---

## 1) What is this system?

**Name:** BugHound  
**Purpose:** Analyze a Python snippet, propose a fix, and run reliability checks before suggesting whether the fix should be auto-applied.

**Intended users:** Students learning agentic workflows and AI reliability concepts.

---

## 2) How does it work?

BugHound follows a five-step agentic workflow:

1. **PLAN** — Decides to run a scan-and-fix workflow on the input code.
2. **ANALYZE** — Detects issues. In heuristic mode, it uses pattern matching (`print(`, bare `except:`, `TODO`) and `compile()` to catch syntax/indentation errors. In Gemini mode, it sends the code to the LLM for analysis, then merges in heuristic results so syntax errors and known patterns are never missed.
3. **ACT** — Proposes a fix. In heuristic mode, it applies simple rewrites (e.g., `print(` → `logging.info(`, bare `except:` → `except Exception as e:`, re-indentation). In Gemini mode, it sends the issues and code to the LLM with a prompt to rewrite. The LLM output is validated with `compile()` — if it still has syntax errors, Gemini retries once, and if that also fails, the heuristic fixer runs as a final fallback.
4. **TEST** — Runs `assess_risk()` to score the fix on a 0–100 scale based on issue severity, structural changes (code growth/shrinkage, removed returns, modified exception handling).
5. **REFLECT** — If the risk level is "low" (score ≥ 75), the fix is marked safe to auto-apply. Otherwise, human review is recommended.

---

## 3) Inputs and outputs

**Inputs:**

- Short Python functions and scripts (3–15 lines).
- Included: functions with `print()` calls, bare `except:` blocks, `TODO` comments, missing indentation, and clean code with no issues.

**Outputs:**

- **Detected issues:** A list of issue objects with `type` (e.g., Syntax, Indentation, Code Quality, Reliability, Maintainability), `severity` (High/Medium/Low), and a human-readable message.
- **Proposed fix:** The rewritten Python code addressing the detected issues.
- **Risk report:** A score (0–100), risk level (low/medium/high), list of reasons for deductions, and an auto-fix recommendation (yes/no).

---

## 4) Reliability and safety rules

**Rule 1: High-severity issue deduction (−40 points)**

- **What it checks:** Whether any detected issue has severity "High" (e.g., bare `except:`, syntax errors).
- **Why it matters:** High-severity issues often indicate behavioral bugs or security problems. A fix for such an issue is more likely to change program semantics, so it should not be auto-applied without review.
- **False positive:** A bare `except:` in a simple script where the fix is straightforward (`except Exception as e:`) — the rule blocks auto-fix even though the change is safe.
- **False negative:** A subtle logic bug with severity "Low" (misassigned by the LLM) that actually changes behavior significantly would not trigger a meaningful deduction.

**Rule 2: Return statement removal check (−30 points)**

- **What it checks:** Whether the original code contains `return` but the fixed code does not.
- **Why it matters:** Removing a return statement changes a function's contract — callers that depend on the return value would silently get `None` instead.
- **False positive:** Code that intentionally refactors a function to use side effects (e.g., writing to a file) instead of returning a value would be penalized even though the change is correct.
- **False negative:** A fix that changes `return x + 1` to `return x` still contains a `return` keyword, so the rule wouldn't flag it — even though the return value changed.

---

## 5) Observed failure modes

**1. Gemini returned no issues for code with missing indentation**

- **Snippet:** `def add(a, b):\nlogging.info("Adding numbers")\nreturn a + b` (no indentation inside the function body).
- **What went wrong:** Gemini returned a valid but empty JSON array `[]`, and the agent originally trusted it without running any heuristic checks. The syntax error was completely missed. Fixed by always merging heuristic results alongside LLM results.

**2. Gemini "fixed" code but the fix still had no indentation**

- **Snippet:** Same unindented code as above.
- **What went wrong:** Gemini's proposed fix echoed back the code without actually fixing the indentation. The agent returned it as-is because there was no validation that the output compiles. Fixed by adding a `compile()` check on LLM output with a retry prompt, and falling back to the heuristic fixer if the retry also fails.

---

## 6) Heuristic vs Gemini comparison

- **What Gemini detected that heuristics did not:** Gemini can identify nuanced issues like potential `ZeroDivisionError` in `x / y`, missing type hints, or poor variable naming — things that require semantic understanding beyond pattern matching.
- **What heuristics caught consistently:** Syntax/indentation errors (via `compile()`), `print()` statements, bare `except:` blocks, and `TODO` comments. These never depend on LLM quality or availability.
- **How the proposed fixes differed:** Heuristic fixes are mechanical rewrites (e.g., `print(` → `logging.info(`). Gemini fixes are more flexible and context-aware but occasionally introduce unnecessary changes or fail to fix the actual problem.
- **Did the risk scorer agree with intuition?** Mostly yes — high-severity issues correctly blocked auto-fix. However, the scorer couldn't detect when Gemini's "fix" didn't actually fix anything (e.g., returning unindented code unchanged), which is why we added the `compile()` validation.

---

## 7) Human-in-the-loop decision

**Scenario:** BugHound should refuse to auto-fix when the LLM's proposed fix fails `compile()` validation and the retry also fails.

- **Trigger:** The `compile()` check in `propose_fix()` fails on both the initial LLM output and the retry. This means neither Gemini nor the retry prompt could produce valid Python.
- **Where to implement:** In the agent workflow (`propose_fix` in `bughound_agent.py`), which already has this logic — it falls back to the heuristic fixer, and the risk assessor then evaluates the result. If the heuristic fix also can't resolve the syntax error, the high-severity issue drives the risk score below the autofix threshold.
- **Message:** "BugHound could not produce a valid fix for the syntax errors in this code. Please review and fix the indentation/syntax manually before re-running."

---

## 8) Improvement idea

**Add a `compile()` validation guardrail in `assess_risk()`** — Currently the risk assessor checks structural properties (line count, return statements) but never verifies that the proposed fix is actually valid Python. Adding a `compile()` check directly in the risk assessor would be a simple, high-value guardrail: if the fixed code doesn't compile, set score to 0 and block auto-fix. This catches any case where the LLM or heuristic fixer produces broken output, regardless of how it got there. It's a single `try/compile/except` block — minimal complexity, maximum safety.
