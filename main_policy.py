"""Local fallback selection used when the hidden LLM classifier is unavailable."""
import re
from router import ROUTES

RANK = {"lookup": 0, "implementation": 1, "critical_review": 2, "hard_problem": 3}
ALIASES = {"luna": "gpt-5.6-luna", "terra": "gpt-5.6-terra",
           "sol": "gpt-5.6-sol", "astra": "gpt-6-astra"}
REASONS = {
    "lookup": "짧은 설명·지정 정보 추출·서식 작업",
    "implementation": "일반 질문·구현 또는 난이도가 불명확한 요청",
    "critical_review": "보안·데이터 보존·깊은 오류 분석",
    "hard_problem": "복잡한 설계·구조 변경·요구 충돌",
}


def choose_route(prompt, previous=None, model="auto", effort="auto", catalog=None):
    """Conservative rules, with sticky context for dependent follow-ups.

    ponytail: local heuristics cannot understand every task; the native router
    normally uses its hidden classifier and keeps this only as a fail-open path.
    The complete original prompt is always sent to Codex unchanged.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("질문을 입력하세요.")
    if not isinstance(model, str) or not isinstance(effort, str):
        raise ValueError("모델과 노력 수준은 문자열이어야 합니다.")
    # Code/quotes are data, not automatic model-selection instructions.
    fence = re.escape(chr(96) * 3)
    prose = re.sub(r"(?ms)^[ \t]*" + fence + r"[^\n]*\n.*?(?:^[ \t]*" + fence + r"[ \t]*$|\Z)", "", prompt)
    instruction = "\n".join(line for line in prose.splitlines() if not line.lstrip().startswith(">")).strip()
    text = instruction.lower()
    simple = re.search(r"(뭐야|무엇인가요|무슨 뜻|뜻이 뭐|뜻을 알려|what is|what does .+ mean)", text)
    action = re.search(r"(구현|수정|만들|개발|고쳐|검토|검증|분석|설계|비교|implement|build|fix|review|analy[sz]|design|compare)", text)
    hard = re.search(r"(아키텍처|architecture|시스템\s*설계|system\s*design|분산\s*시스템|distributed\s*system|요구.*충돌|트레이드오프|trade.?off|대규모.*설계)", text)
    risk = re.search(r"(보안|취약점|인증|권한|무결성|데이터.*손실|결제|마이그레이션|원인.*불명|간헐.*오류|교착|경쟁\s*조건|security|vulnerabil|authentication|authorization|integrity|payment|migration|deadlock|race.condition|root.cause)", text)
    narrow = re.search(r"(번역|맞춤법|서식|포맷|정렬|값만|위치만|추출|translate|format|extract|sort|only the value)", text)
    greeting = re.fullmatch(r"(안녕(?:하세요)?|고마워|감사합니다|hi|hello|thanks)[.!?\s]*", text)
    if narrow and not action and len(prompt) <= 2000:
        role = "lookup"
    elif hard and action:
        role = "hard_problem"
    elif risk:
        role = "critical_review"
    elif simple and not action and len(prompt) <= 500:
        role = "lookup"
    elif action or len(prompt) > 2000:
        role = "implementation"
    elif greeting:
        role = "lookup"
    else:
        role = "implementation"
    reason = REASONS[role]
    dependent = re.match(r"^(원래\s*(?:목적|요청|작업)|앞서|방금|그럼|그러면|그거|그걸|이거|이걸|계속|진행해|응\b|네\b|좋아|오케이|이어서|다음\s*단계|해줘|다시\s*(?:해|시도|진행)|그대로|수정해|동일|also\b|then\b|continue\b|do it\b|yes\b|fix it\b|same\b|what about\b|and\b)", text)
    reset = re.match(r"^(새 질문|다른 질문|별개|new question|unrelated)", text)
    old_role = (previous or {}).get("role")
    if dependent and not reset and old_role in RANK and RANK[old_role] > RANK[role]:
        role = old_role
        reason = "앞선 작업에 이어지는 요청: " + REASONS[role]
    selected_model, selected_effort = ROUTES[role]
    # Only an explicit command at the beginning of the user prompt is parsed.
    directive = re.match(r"^/model\s+([-\w.]+)(?:\s+(none|minimal|low|medium|high|xhigh|max|ultra))?(?:\s*\n|$)", instruction, re.I)
    if directive:
        model = directive.group(1).lower()
        if directive.group(2):
            effort = directive.group(2).lower()
    natural = re.match(r"^(?:이번(?:에는|엔)?\s*)?(luna|terra|sol|astra|루나|테라|솔|아스트라)(?:로|으로)\s*(?:답|처리|진행|설명|해)", text)
    if natural and model == "auto":
        model = {"루나": "luna", "테라": "terra", "솔": "sol", "아스트라": "astra"}.get(natural.group(1), natural.group(1))
    explicit_model = model not in ("auto", "")
    explicit_effort = effort not in ("auto", "")
    if explicit_model:
        selected_model = ALIASES.get(model.lower(), model)
        reason = "사용자가 지정한 모델"
    if catalog is not None:
        entry = next((x for x in catalog if x.get("model") == selected_model), None)
        if entry is None:
            raise ValueError(f"현재 계정에서 사용할 수 없는 모델입니다: {selected_model}")
        supported = [x["reasoningEffort"] for x in entry.get("supportedReasoningEfforts", [])]
        if explicit_model and not explicit_effort:
            selected_effort = entry.get("defaultReasoningEffort") or "medium"
        if explicit_effort:
            selected_effort = effort
        if supported and selected_effort not in supported:
            raise ValueError(f"{selected_model}은 {selected_effort}를 지원하지 않습니다. 지원값: {', '.join(supported)}")
    elif explicit_effort:
        selected_effort = effort
    if explicit_effort:
        reason += " · 사용자가 지정한 노력 수준"
    return {"role": role, "model": selected_model, "effort": selected_effort,
            "reason": reason, "explicit": explicit_model or explicit_effort,
            "explicit_model": explicit_model, "explicit_effort": explicit_effort}
