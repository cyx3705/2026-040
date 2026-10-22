"""练词对话：从导入的单词表里挑几个词，整段对话都围着它们转。

练词要求写进系统提示词（AI 回复和输入预测共用），所以 AI 会把话题往这些词上带，
候选词里也更容易出现它们。「练过」只算学习者自己说出口的：AI 用了不算。
"""

import re

PROMPT = (
    "This conversation is vocabulary practice. The learner is practising these target words: {all}. "
    "Pick a natural, everyday topic where these words fit, and stay with them for the whole conversation. "
    "In each reply, use one or two target words naturally, and ask a question that invites the learner "
    "to use a target word in their answer. {focus}"
    "Do not list the words or explain them unless the learner asks."
)
OPENING = "(The learner is ready. Start the conversation now, using one of the target words.)"


def words_of(practice: dict | None) -> list[str]:
    return [w["word"] for w in (practice or {}).get("words", [])]


def prompt(practice: dict | None, used: set[str]) -> str:
    words = words_of(practice)
    if not words:
        return ""
    left = [w for w in words if w not in used]
    if left:
        focus = f"The learner has not used these yet, so steer towards them first: {', '.join(left)}. "
    else:
        focus = "The learner has already used every target word; keep reviewing them in new contexts. "
    return " " + PROMPT.format(all=", ".join(words), focus=focus)


_TOKEN = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")


def used_words(practice: dict | None, messages: list[dict], lookup) -> set[str]:
    """学习者在这些消息里用上了哪些目标词。

    单词按原形比：说 went 也算练了 go（和「学会」一样按 lemma 记）。词组按整段原文找。
    lookup(word) 返回词典条目（带 lemma），查不到返回 None。
    """
    words = words_of(practice)
    text = " ".join(m["content"] for m in messages if m["role"] == "user")
    if not words or not text:
        return set()
    low = text.lower().replace("’", "'")
    said = set()
    for tok in _TOKEN.findall(low):
        said.add(tok)
        entry = lookup(tok)
        if entry:
            said.add(entry["lemma"])
    used = set()
    for w in words:
        key = w.lower()
        if " " in key or "-" in key:
            if re.search(rf"\b{re.escape(key)}\b", low):
                used.add(w)
            continue
        entry = lookup(key)
        if key in said or (entry and entry["lemma"] in said):
            used.add(w)
    return used
