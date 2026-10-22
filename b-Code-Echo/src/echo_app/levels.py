"""对话难度：按学习者熟悉的评价标准选（CEFR、四六级、专四专八、雅思口语小分）。

难度只改 AI 说话的方式，写进系统提示词；输入预测也用同一份系统提示词，候选词的难度跟着变。
各标准都折算到 CEFR 档位，用 CEFR 档位的说话规则；提示词里同时写上原标准，
模型对「四级水平」「雅思 6.5」本身也有概念。
"""

# CEFR 档位 → 用词和句子（怎么说）
STYLE = {
    "A1": "Use only the few hundred most common English words and the present simple, "
          "and keep every sentence under eight words.",
    "A2": "Use simple everyday words, basic tenses and short sentences.",
    "B1": "Use common everyday English with a few phrasal verbs, in clear sentences.",
    "B2": "Use natural English with a wider vocabulary, phrasal verbs and some common idioms.",
    "C1": "Speak fluent, natural English with precise vocabulary, idioms and varied sentence structures.",
    "C2": "Speak like an educated native speaker: nuanced vocabulary, idioms, subtle humour and complex "
          "sentences where natural.",
    "C2+": "Speak exactly as a witty, articulate native speaker would with a friend: rich idioms, slang, "
           "cultural references and wordplay, with no simplification at all.",
}

# 回答长度（说多少）：页面上的滑块。0 是「自动」，按难度档位取默认长度。
LENGTHS = [
    ("自动", "跟着难度走：入门一两句，高级一小段"),
    ("一句", "只说一句，而且是个问题"),
    ("简短", "一两句，问一个问题"),
    ("适中", "三四句，再接着问"),
    ("详细", "一整段：说想法、举例子"),
    ("长篇", "两三段：展开讲、给细节和观点"),
]
_LENGTH_RULES = {
    1: "Your whole reply must be a single question sentence that keeps the conversation going. "
       "Do not start with a reaction such as \"That sounds fun!\" or any other sentence.",
    2: "Reply in at most two short sentences in total, counting any short reaction such as \"Nice!\", "
       "and end with a question.",
    3: "Reply in three or four sentences, and end with a follow-up question.",
    4: "Reply in one full paragraph of five to seven sentences: share your own thoughts, an example "
       "or a short story, then ask a follow-up question.",
    5: "Reply at length, in two or three paragraphs: explain, give examples and details, share your "
       "opinions, then ask a follow-up question.",
}
# 「自动」时各档位的长度：沿用拆分前每档自带的句数
_AUTO_LENGTH = {"A1": 2, "A2": 2, "B1": 3, "B2": 3, "C1": 4, "C2": 4, "C2+": 4}

# 标准 → [(档位 id, 显示名, CEFR 档位, 中文说明, 提示词里怎么称呼)]
# 四六级和专四专八合成一页（用户定），按难度排：CET-4、CET-6、专四、专八。
_EXAMS = "China's College English Test"
_TEM = "China's Test for English Majors"
STANDARDS = [
    ("cefr", "欧洲语言框架", [
        ("cefr:A1", "A1", "A1", "入门：几百个最常用的词", None),
        ("cefr:A2", "A2", "A2", "基础：日常简单话题", None),
        ("cefr:B1", "B1", "B1", "进阶：熟悉话题能连贯表达", None),
        ("cefr:B2", "B2", "B2", "中高级：和母语者自然交流", None),
        ("cefr:C1", "C1", "C1", "高级：流利、用词精确", None),
        ("cefr:C2", "C2", "C2", "精通：接近受过教育的母语者", None),
        ("cefr:C2+", "C2+", "C2+", "母语：俚语、梗、文字游戏都来", None),
    ]),
    ("cn", "国内考试", [
        ("cet:4", "CET-4", "B1", "四级：约 4500 词，≈ B1", f"{_EXAMS} Band 4 (CET-4)"),
        ("cet:6", "CET-6", "B2", "六级：约 6000 词，≈ B2", f"{_EXAMS} Band 6 (CET-6)"),
        ("tem:4", "专四", "B2", "TEM-4：约 8000 词，≈ B2+", f"{_TEM} Band 4 (TEM-4)"),
        ("tem:8", "专八", "C1", "TEM-8：约 13000 词，≈ C1", f"{_TEM} Band 8 (TEM-8)"),
    ]),
    ("ielts", "雅思口语", [
        (f"ielts:{s}", s, band, f"口语 {s} 分，≈ {band}", f"IELTS Speaking band {s}") for s, band in [
            ("4.0", "A2"), ("4.5", "B1"), ("5.0", "B1"), ("5.5", "B2"), ("6.0", "B2"), ("6.5", "B2"),
            ("7.0", "C1"), ("7.5", "C1"), ("8.0", "C1"), ("8.5", "C2"), ("9.0", "C2"),
        ]
    ]),
]

DEFAULT = "cefr:A2"
# 上一版只有四档：旧对话里存的难度按这张表折算
LEGACY = {"starter": "cefr:A1", "basic": "cefr:A2", "intermediate": "cefr:B1", "advanced": "cefr:C1"}

_BY_ID = {
    lid: {"name": label, "band": band, "note": note, "english": english}
    for _, _, items in STANDARDS for lid, label, band, note, english in items
}


def normalize(level: str) -> str:
    level = LEGACY.get(level, level)
    return level if level in _BY_ID else DEFAULT


def catalog() -> list[dict]:
    """给页面的难度目录：标准 → 档位。"""
    return [
        {"id": std, "name": name,
         "levels": [{"id": lid, "name": label, "band": band, "note": note}
                    for lid, label, band, note, _ in items]}
        for std, name, items in STANDARDS
    ]


def lengths() -> list[dict]:
    return [{"id": i, "name": name, "note": note} for i, (name, note) in enumerate(LENGTHS)]


def describe(level: str, length: int = 0) -> str:
    """提示词里的难度和长度要求。"""
    lv = _BY_ID[normalize(level)]
    if lv["english"] is None:
        who = f"The learner's English is at CEFR level {lv['name']}."
    else:
        who = f"The learner's English is at the level of {lv['english']} (roughly CEFR {lv['band']})."
    size = length if length in _LENGTH_RULES else _AUTO_LENGTH[lv["band"]]
    return f"{who} Match that level: {STYLE[lv['band']]} {_LENGTH_RULES[size]}"
