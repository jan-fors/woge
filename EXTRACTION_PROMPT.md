# Extraction prompt for woge

Paste everything below the line into a new Claude chat and attach the worksheet PDF (and, if you have it, the official solution sheet).

---

You convert worksheets into an exercise bank JSON for my CLI tool `woge`. Read the attached worksheet, then output **one JSON object** (in a ```json code block, nothing else except a short list of doubts at the end) following exactly this schema and these rules.

## Schema

```json
{
  "schema_version": 1,
  "source": {"title": "<worksheet title>", "subject": "<subject>", "language": "<en|de|…>"},
  "exercises": [
    {
      "id": "<short-unique-id, e.g. ws3-2b>",
      "title": "<short human title>",
      "type": "<snake_case category, e.g. quadratic_equation>",
      "topic": "<broader topic, e.g. algebra>",
      "tags": ["<optional>"],
      "difficulty": 3,
      "minutes": 10,
      "original": {
        "statement": "<exercise text exactly as on the sheet, Markdown + LaTeX>",
        "solution": "<ONLY the final answer, short>",
        "explanation": "<step-by-step derivation of that answer>"
      },
      "template": {
        "parameters": {
          "<name>": {"type": "int", "min": -9, "max": 9, "step": 1, "exclude": [0]},
          "<name>": {"type": "float", "min": 0.5, "max": 5, "decimals": 1},
          "<name>": {"type": "choice", "values": ["Anna", "Ben"]}
        },
        "derived": {"<name>": "<Python/sympy expression using parameters and earlier derived values>"},
        "constraints": ["<boolean expression>", "..."],
        "statement": "<same text with {{placeholders}}>",
        "solution": "<final answer with {{placeholders}}>",
        "explanation": "<same steps with {{placeholders}} for every number that changes>"
      }
    }
  ]
}
```

## Rules

1. **One entry per atomic task.** Sub-tasks (a, b, c…) become separate entries. If a sub-task depends on shared context, copy that context into its statement so it stands alone.
2. **Solve every task yourself** and double-check the result. `solution` is only the final answer; `explanation` is the full reasoning, written so a student can follow it.
3. **`type`** names the *kind* of exercise, so reuse the same type string for exercises that work the same way.
4. **Rate every exercise** so sheets can be balanced:
   - `difficulty` (integer 1–5), judged for a student who attended the lecture:
     1 = direct application, one step or a formula lookup;
     2 = routine, a few standard steps;
     3 = typical exam task, you must choose the method yourself;
     4 = combines several concepts or needs a non-obvious trick;
     5 = open-ended design, deep conceptual insight or a known trap.
   - `minutes` (integer): realistic working time for that student, including writing down the solution.
   - Rate generated number variants by their own effort, not by the task they came from. Use the full scale across the sheet; don't put everything at 2–3.
5. **Templates:** create one whenever the task can meaningfully vary (numbers, coefficients, names, units, small wording changes). Set `"template": null` for proofs, open questions or anything that doesn't vary sensibly.
6. **Never hardcode results in a template.** Every answer and every intermediate value in the explanation must be computed in `derived` so it is correct for all parameter values.
7. **Build backwards for nice numbers:** choose the answer first (e.g. roots `r1`, `r2`) and derive the problem from it (`expand((x-r1)*(x-r2))`). Use `constraints` to exclude degenerate or ugly cases (division by zero, zero coefficients, huge values, equal roots…).
8. **Placeholders:** `{{name}}` or `{{name:.2f}}` (Python format spec). Only plain names are allowed inside `{{ }}` – put any function call into `derived` first. Sympy values render as LaTeX, so place them inside `$…$`.
9. **Available in expressions:** sympy (`Rational, sqrt, expand, factor, simplify, solve, diff, integrate, limit, latex, Matrix, gcd, lcm, binomial, factorial, sin, cos, exp, log, pi, …`), symbols `x y z t u v w`, Python basics (`abs, min, max, round, int, float, range, sum, len, sorted, all, any`), plus helpers `num(v, digits=4)` (decimal without trailing zeros), `signed(v)` (`"+ 5"` / `"- 5"`), `tex(v)` (LaTeX of anything). Integer parameters are sympy Integers, so `a/b` is an exact fraction. `derived` is evaluated top to bottom: define a value before using it, otherwise a name like `u` silently becomes the sympy symbol $u$.
10. **Consistency check:** plugging the original numbers into the template must reproduce the original statement and solution.
11. **JSON escaping:** every LaTeX backslash must be doubled (`\\frac`, `\\sqrt`), newlines as `\n`. Avoid `{{` in plain LaTeX except around placeholders; for `\frac{a}{b}` with placeholders write `\\frac{{{a}}}{{{b}}}`.
12. **Figures / tables:** describe them in words in the statement and add the tag `"needs_figure"`.
13. After the JSON, list anything you were unsure about (unreadable text, ambiguous tasks, missing data).

## Example entry

```json
{
  "id": "ex-1a",
  "title": "Quadratic equation",
  "type": "quadratic_equation",
  "topic": "algebra",
  "difficulty": 2,
  "minutes": 6,
  "original": {
    "statement": "Solve the equation $x^{2} - 5 x + 6 = 0$.",
    "solution": "$x_1 = 2,\\; x_2 = 3$",
    "explanation": "Use the pq-formula with $p = -5$ and $q = 6$: $x_{1,2} = \\frac{5}{2} \\pm \\frac{1}{2}$, so $x_1 = 2$, $x_2 = 3$."
  },
  "template": {
    "parameters": {
      "r1": {"type": "int", "min": -9, "max": 9},
      "r2": {"type": "int", "min": -9, "max": 9}
    },
    "derived": {
      "poly": "latex(expand((x - r1) * (x - r2)))",
      "p": "-(r1 + r2)",
      "q": "r1 * r2",
      "mp2": "-Rational(p, 2)",
      "sq": "sqrt(Rational(p, 2)**2 - q)"
    },
    "constraints": ["r1 < r2", "p != 0", "q != 0"],
    "statement": "Solve the equation ${{poly}} = 0$.",
    "solution": "$x_1 = {{r1}},\\; x_2 = {{r2}}$",
    "explanation": "Use the pq-formula with $p = {{p}}$ and $q = {{q}}$: $x_{1,2} = {{mp2}} \\pm {{sq}}$, so $x_1 = {{r1}}$, $x_2 = {{r2}}$."
  }
}
```
