"""
NUET question parser powered by the OpenAI (ChatGPT) API.

Mirrors the return contract of the Gemini SAT parser (src/services/parser.py) so
the frontend import logic is reused, with one difference: NUET questions may have
up to 5 options (A-E), not 4. Handles both NUET Math (LaTeX) and Critical Thinking
(verbal, with a reading passage in content_text).
"""
import os
import re
import json
import base64
import mimetypes
from typing import List, Dict, Any

NUET_MODEL = "gpt-4o"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

_LATEX_COMMANDS = [
    "times", "text", "tan", "theta", "tau",
    "frac", "forall", "phi",
    "beta", "bar", "break", "begin", "mathbf", "mathbb",
    "nu", "neq", "nabla",
    "rho", "right",
    "alpha", "sigma", "gamma", "delta", "epsilon", "lambda", "mu", "pi", "omega",
    "sqrt", "sum", "sin", "cos", "log", "ln", "lim", "int", "infty", "approx", "div", "pm", "mp", "cdot", "leq", "geq",
]


class NuetParser:
    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            if not OPENAI_API_KEY:
                raise Exception("OPENAI_API_KEY is not configured")
            self._client = OpenAI(api_key=OPENAI_API_KEY)
        return self._client

    def _build_prompt(self, correct_answers: str = None) -> str:
        prompt = """
        Analyze this document and extract all NUET (Nazarbayev University Entrance Test)
        quiz questions from it. NUET questions come in two flavors and BOTH may appear:
          1. MATH - algebra/arithmetic questions with mathematical expressions and 4 options (A-D).
          2. CRITICAL THINKING / VERBAL REASONING - a reading passage or argument followed by a
             question such as "Which one of the following is an assumption on which this argument
             depends?", with up to 5 options (A-E).

        Return the result as a JSON array of objects. Each object must have:
        - question_text: The text of the question (for critical thinking, the question line only).
        - question_type: "single_choice" for standard NUET multiple choice (both flavors).
        - options: An array of the answer choices as strings (4 for math, up to 5 for critical thinking).
        - correct_answer: The index (0-based) of the correct option. SOLVE the question to determine
          this: compute the math, or reason through the argument. If unsure, give your best answer.
        - explanation: A brief explanation of why the answer is correct.
        - content_text: For critical thinking questions, put the FULL reading passage/argument here.
          Leave empty for standalone math questions.
        - needs_image: true only if a diagram/figure is required to answer and is not transcribable.

        For MATH questions, follow these LaTeX rules STRICTLY:
        - EVERY mathematical expression, number in mathematical context, variable, or formula MUST be wrapped in $...$
        - NEVER split a mathematical expression into multiple $...$ blocks - wrap the ENTIRE expression in ONE block
        - For text inside math mode use $\\text{your text}$ with a BACKSLASH and a leading space: $15.5\\text{ inches}$
        - Fractions: $\\frac{numerator}{denominator}$; Exponents: $x^2$; Square roots: $\\sqrt{x}$
        - Operations in math context: $+$, $-$, $\\times$, $\\div$, $=$; Inequalities: $x \\leq 10$
        - Examples: "Solve $x^2 + 5x + 6 = 0$", "The value of $\\frac{3}{4}$", "$2(m-3)^2 - 7 = 43$"

        For CRITICAL THINKING questions, use PLAIN TEXT (no LaTeX) for the passage, question, and options.

        JSON OUTPUT FORMATTING:
        - Escape backslashes in LaTeX commands using DOUBLE backslash in JSON strings, e.g.
          "question_text": "The answer is $\\\\frac{1}{2}$".
        - Output ONLY the JSON array. Do NOT wrap it in markdown fences like ```json ... ```.
        """
        if correct_answers and correct_answers.strip():
            prompt += f"""

        CORRECT ANSWERS PROVIDED:
        {correct_answers}

        Use these to set the "correct_answer" field. Parse the format intelligently
        (e.g. "1.A 2.B" or "A,B,C,D,E"); letters are 0-based indices (A=0, B=1, ...).
        """
        return prompt

    def _file_content_part(self, file_path: str, mime_type: str) -> Dict[str, Any]:
        with open(file_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        if mime_type == "application/pdf":
            return {
                "type": "file",
                "file": {
                    "filename": os.path.basename(file_path) or "document.pdf",
                    "file_data": f"data:application/pdf;base64,{b64}",
                },
            }
        # default: image
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type or 'image/png'};base64,{b64}"},
        }

    def _extract_json(self, text: str) -> list:
        text = (text or "").strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        # Fix single backslashes in LaTeX commands so JSON parses (mirror SAT parser).
        for cmd in _LATEX_COMMANDS:
            text = re.sub(r'(?<!\\)\\' + cmd, r'\\\\' + cmd, text)
        text = re.sub(r'(?<!\\)\\(?![\\"/bfnrtu])', r'\\\\', text)

        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise Exception("Expected a JSON array of questions")
        return parsed

    def _process_questions(self, raw: list) -> List[Dict[str, Any]]:
        letters = ['A', 'B', 'C', 'D', 'E']
        processed = []
        for i, q in enumerate(raw):
            question_type = q.get("question_type", "single_choice")
            options = None
            correct_answer = q.get("correct_answer", 0)

            if question_type in ["single_choice", "multiple_choice", "media_question"]:
                options = []
                raw_options = q.get("options", []) or []
                for j, opt_text in enumerate(raw_options):
                    if j < 5:
                        options.append({
                            "id": f"gen_{i}_{j}",
                            "text": str(opt_text),
                            "is_correct": j == q.get("correct_answer", 0),
                            "letter": letters[j],
                        })
                # Ensure at least 4 options (pad blanks), but keep 5 when present.
                while len(options) < 4:
                    j = len(options)
                    options.append({
                        "id": f"gen_{i}_{j}",
                        "text": "",
                        "is_correct": False,
                        "letter": letters[j],
                    })
            else:
                correct_answer = q.get("correct_answer", "")

            processed.append({
                "id": f"nuet_{i}_{os.urandom(4).hex()}",
                "question_text": q.get("question_text", "Question"),
                "question_type": question_type,
                "options": options,
                "correct_answer": correct_answer,
                "points": 1,
                "explanation": q.get("explanation", ""),
                "is_sat_question": True,
                "content_text": q.get("content_text", ""),
                "needs_image": q.get("needs_image", False),
            })
        return processed

    async def parse_file(self, file_path: str, mime_type: str = None, correct_answers: str = None) -> List[Dict[str, Any]]:
        if not mime_type:
            mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            raise ValueError("Could not determine mime type of the file")

        prompt = self._build_prompt(correct_answers)
        file_part = self._file_content_part(file_path, mime_type)
        client = self._get_client()

        content = None
        last_error = None
        retry_delay = 1
        for attempt in range(3):
            try:
                completion = client.chat.completions.create(
                    model=NUET_MODEL,
                    messages=[{
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}, file_part],
                    }],
                    temperature=0,
                )
                content = completion.choices[0].message.content
                break
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt < 2:
                    import time
                    time.sleep(retry_delay)
                    retry_delay *= 2
        if content is None:
            raise Exception(f"Failed to get NUET analysis from OpenAI after 3 attempts: {last_error}")

        raw = self._extract_json(content)
        return self._process_questions(raw)


nuet_parser_service = NuetParser()
